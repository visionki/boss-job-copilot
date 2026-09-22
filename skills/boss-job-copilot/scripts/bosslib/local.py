from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit


class Stopped(Exception):
    pass


RUNTIME_VERSION = 16
ACTIVITY_POLICY_VERSION = 2
DEFAULT_ACTIVITY = "week"

ACTIVITY_WAITING = {"matched_inactive", "matched_activity_unknown"}  # Preserve legacy holds until explicitly reset.
OUTREACH_ATTEMPTED = {"sending", "sent", "uncertain", "already_contacted", "contacted_external"}

ACTIVITY_FILTERS = {
    "today": {"在线", "刚刚活跃", "今日活跃"},
    "3d": {"在线", "刚刚活跃", "今日活跃", "3日内活跃"},
    "week": {"在线", "刚刚活跃", "今日活跃", "3日内活跃", "本周活跃"},
    "month": {"在线", "刚刚活跃", "今日活跃", "3日内活跃", "本周活跃", "本月活跃"},
    "any": None,
}


def activity_gate(observation, within=DEFAULT_ACTIVITY, at=None):
    """Use the website's coarse label, never invent a last-login timestamp."""
    if within not in ACTIVITY_FILTERS:
        raise Stopped("invalid_recruiter_activity_policy")
    observation = observation or {}
    label = (observation.get("label") or "").strip()
    observed = observation.get("observed_at")
    stamp = time.time() if at is None else at
    site_zone = timezone(timedelta(hours=8))
    if within == "any":
        state = "eligible"
    elif not isinstance(observed, (int, float)) or not math.isfinite(observed) or observed <= 0:
        state = "unchecked"
    elif observed > stamp or datetime.fromtimestamp(observed, site_zone).date() != datetime.fromtimestamp(stamp, site_zone).date():
        state = "stale"
    elif label in ACTIVITY_FILTERS[within]:
        state = "eligible"
    elif re.fullmatch(r"\d+[日天]内活跃", label):
        days = int(re.match(r"\d+", label)[0])
        state = "eligible" if days <= {"today": 0, "3d": 3, "week": 7, "month": 30}[within] else "deferred"
    elif label in ACTIVITY_FILTERS["month"] or re.fullmatch(r"(?:\d+[日天周月年]内|近半年|半年前?|\d+[日天周月年]前)活跃", label):
        state = "deferred"
    else:
        state = "unknown"
    # Missing/unrecognised text on a freshly read detail is uncertainty, not inactivity.
    # Absent or stale observations still require the normal detail read before contact.
    return {**observation, "label": label, "within": within, "state": state,
            "eligible": state in ("eligible", "unknown")}


def page_interval(value=5):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 5:
        raise Stopped("invalid_interval:minimum_5_seconds")
    return value


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(dumps(value), encoding="utf-8")
    temp.replace(path)


class FileLock:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise Stopped("resource_in_use") from exc

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Workspace:
    def __init__(self, path):
        self.root = Path(path).expanduser().resolve()
        self.runtime = self.root / "runtime"
        self.settings = read_json(self.root / "settings.json")

    def require(self):
        if not self.settings:
            raise Stopped("workspace_not_initialized")
        return self

    @property
    def profile(self):
        return Path(self.settings["browser_profile"]).resolve()

    @property
    def rate(self):
        # Legacy hourly/daily and batch limits are no longer applied.
        return {"interval_seconds": page_interval(self.settings.get("rate", {}).get("interval_seconds", 5))}

    def user_hash(self):
        # Audit only. Changes never invalidate a saved review or requeue a job.
        return digest([read_json(self.root / "user/search-plan-confirmation.json", {}),
                       (self.root / "user/PROFILE.md").read_text(encoding="utf-8-sig")
                       if (self.root / "user/PROFILE.md").is_file() else ""])

    def outreach_activity(self, within=None):
        path = self.root / "user/outreach-config.json"
        config = read_json(path, {})
        chosen = config.get("recruiter_activity", DEFAULT_ACTIVITY) if within is None else within
        if chosen not in ACTIVITY_FILTERS:
            raise Stopped("invalid_recruiter_activity_policy")
        if within is not None:
            config["recruiter_activity"] = chosen
            write_json(path, config)
        return chosen

    def initial_review_count(self, count=None):
        path = self.root / "user/outreach-config.json"
        config = read_json(path, {})
        chosen = config.get("initial_review_count", 10) if count is None else count
        if isinstance(chosen, bool) or not isinstance(chosen, int) or chosen < 1:
            raise Stopped("invalid_initial_review_count")
        if count is not None:
            config["initial_review_count"] = chosen
            write_json(path, config)
        return chosen

    def status(self):
        state = read_json(self.runtime / "state.json", {})
        state["alive"] = (time.time() - state.get("heartbeat", 0) < 8
                          and state.get("mode") not in ("closed", "failed"))
        return state

    def search_plan_status(self):
        plan = self.root / "user/SEARCH_PLAN.md"
        result = {"workspace": str(self.root), "plan_file": str(plan), "state": "missing"}
        if not plan.is_file() or not plan.read_text(encoding="utf-8-sig").strip():
            return result
        text = "\n".join(line.rstrip() for line in plan.read_text(encoding="utf-8-sig").splitlines() if line.strip())
        result.update(state="unconfirmed", plan_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        confirmation = read_json(self.root / "user/search-plan-confirmation.json")
        if (not isinstance(confirmation, dict) or not isinstance(confirmation.get("user_message"), str)
                or not confirmation["user_message"].strip() or not confirmation.get("confirmed_at")):
            return result
        # Accept genuine confirmations of unchanged documents from older installs.
        hashes = {result["plan_sha256"], hashlib.sha256(plan.read_bytes()).hexdigest()}
        result["state"] = ("confirmed" if confirmation.get("workspace") == str(self.root)
                           and confirmation.get("plan_sha256") in hashes else "changed")
        result["confirmation"] = confirmation
        return result

    def confirm_search_plan(self, user_message):
        self.require()
        if not user_message.strip():
            raise Stopped("user_confirmation_message_required")
        assets = Path(__file__).resolve().parents[2] / "assets"
        for name, reason in (("PROFILE", "profile_analysis_required"), ("SEARCH_PLAN", "search_plan_document_required")):
            document = self.root / "user" / (name + ".md")
            content = document.read_text(encoding="utf-8-sig").strip() if document.is_file() else ""
            if not content or content == (assets / (name + ".template.md")).read_text(encoding="utf-8").strip():
                raise Stopped(reason)
        status = self.search_plan_status()
        if status["state"] != "confirmed":
            write_json(self.root / "user/search-plan-confirmation.json", {
                "workspace": str(self.root), "plan_sha256": status["plan_sha256"],
                "confirmed_at": now(), "user_message": user_message.strip(),
            })
        return self.search_plan_status()

    def require_search_plan(self):
        status = self.search_plan_status()
        if status["state"] != "confirmed":
            raise Stopped("search_plan_changed_requires_confirmation" if status["state"] == "changed"
                          else "search_plan_confirmation_required")
        return status


def initialize(path, chrome=None, profile=None):
    ws = Workspace(path)
    # Prevent personal files from ever being created inside the distributable skill.
    skill = Path(__file__).resolve().parents[2]
    if ws.root == skill or skill in ws.root.parents:
        raise Stopped("workspace_must_be_outside_skill")
    ws.root.mkdir(parents=True, exist_ok=True)
    (ws.root / "user").mkdir(exist_ok=True)
    for name in ("USER", "PROFILE", "SEARCH_PLAN", "OUTREACH"):
        document = ws.root / "user" / (name + ".md")
        if not document.exists():
            document.write_text((skill / "assets" / (name + ".template.md")).read_text(encoding="utf-8"), encoding="utf-8")
    if ws.settings:
        return {"workspace": str(ws.root), "existing": True}
    for directory in ("user/materials", "user/examples", "runtime/requests", "runtime/results", "outputs"):
        (ws.root / directory).mkdir(parents=True, exist_ok=True)
    (ws.root / ".gitignore").write_text("*\n", encoding="utf-8")
    user_file = ws.root / "user" / "USER.md"
    settings = {
        "schema": 1, "profile_schema": 2, "chrome": chrome,
        "browser_profile": str(Path(profile).resolve() if profile else ws.root / "browser-profile"),
        "rate": {"interval_seconds": 5},
        "detail_cache_hours": 72,
    }
    write_json(ws.root / "settings.json", settings)
    with database(ws.root) as db:
        db.stats()
    return {"workspace": str(ws.root), "user_document": str(user_file), "existing": False}


JOB_ID = re.compile(r"(?=.{1,200}\Z)[A-Za-z0-9_-]+~{0,2}\Z")
FIELDS = {
    "encryptJobId": "id", "jobName": "title", "salaryDesc": "salary",
    "jobExperience": "experience", "jobDegree": "degree", "cityName": "city",
    "areaDistrict": "district", "businessDistrict": "business_district",
    "skills": "skills", "jobLabels": "labels", "welfareList": "benefits",
    "brandName": "company", "brandIndustry": "industry", "brandScaleName": "company_size",
    "brandStageName": "funding", "encryptBrandId": "company_id", "jobType": "job_type",
    "bossName": "recruiter", "bossTitle": "recruiter_title", "contact": "contact_observed",
}


def normalize(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("encryptJobId"), str) or not JOB_ID.fullmatch(raw["encryptJobId"]):
        raise Stopped("invalid_job_id")
    result = {target: raw.get(source) for source, target in FIELDS.items()}
    for key, value in result.items():
        if isinstance(value, dict) or isinstance(value, list) and key not in ("skills", "labels", "benefits"):
            raise Stopped("api_field_type_changed:" + key)
    # Observed list JSON exposes a boolean, not the detail page's recency label.
    if isinstance(raw.get("bossOnline"), bool):
        result["recruiter_online"] = raw["bossOnline"]
    # Deliberately omit cookies, securityId, request headers, avatars and coordinates.
    return result


def parse_response(payload, *, issues=None):
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise Stopped("api_rejected")
    data = payload.get("zpData")
    if not isinstance(data, dict):
        raise Stopped("api_schema_changed")
    rows = data.get("jobList", data.get("list"))
    if not isinstance(rows, list):
        raise Stopped("api_schema_changed")
    result = []
    for index, row in enumerate(rows):
        try:
            result.append(normalize(row))
        except Stopped as exc:
            if issues is None:
                raise
            issues.append({"row": index, "reason": str(exc)})
    return result


FILTER_KEYS = {"query", "city", "salary", "experience", "degree", "scale", "stage", "industry", "position",
               "areaBusiness", "multiBusinessDistrict", "multiSubway", "jobType", "payType", "partTime"}


def search_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "www.zhipin.com"
            or parts.path.rstrip("/") != "/web/geek/jobs" or parts.fragment):
        raise Stopped("invalid_search_url")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(k not in FILTER_KEYS for k, _ in pairs) or len(dict(pairs)) != len(pairs):
        raise Stopped("unsupported_or_duplicate_filter_parameter")
    return "https://www.zhipin.com/web/geek/jobs?" + urlencode(pairs)


class Store:
    def __init__(self, path):
        self.ws = Workspace(Path(path).parent)
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, facts TEXT NOT NULL, list_hash TEXT NOT NULL,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          detail TEXT, detail_state TEXT NOT NULL DEFAULT 'pending',
          detail_seen REAL, detail_list_hash TEXT);
        CREATE TABLE IF NOT EXISTS observations (
          batch TEXT, job_id TEXT, source TEXT, observed_at TEXT,
          PRIMARY KEY(batch,job_id,source));
        CREATE TABLE IF NOT EXISTS reviews (
          id INTEGER PRIMARY KEY, job_id TEXT, created_at TEXT,
          user_hash TEXT, facts_hash TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS outreach (
          job_id TEXT PRIMARY KEY, review_id INTEGER, status TEXT NOT NULL,
          message TEXT NOT NULL, message_hash TEXT NOT NULL, recipient TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS recruiter_activity (
          job_id TEXT PRIMARY KEY, label TEXT, observed_at REAL NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activity_holds (
          job_id TEXT PRIMARY KEY, state TEXT NOT NULL, review_id INTEGER,
          suspended_at REAL, awakened_at REAL, awakened_batch TEXT,
          generation INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS outreach_recipient_once ON outreach(recipient)
          WHERE recipient IS NOT NULL AND status IN ('sending','sent','uncertain','already_contacted');
        """)
        # Backfill contact evidence from cached complete details without opening a page.
        cached_contacts = self.conn.execute("""SELECT id,detail FROM jobs WHERE detail_state='complete'
            AND detail LIKE '%继续沟通%' AND NOT EXISTS (SELECT 1 FROM outreach WHERE job_id=jobs.id
            AND status IN ('sending','sent','uncertain','already_contacted','contacted_external'))""").fetchall()
        for row in cached_contacts:
            if (json.loads(row["detail"]).get("chat_button") or "").strip() == "继续沟通":
                self.observe_existing_contact(row["id"])

    def save_jobs(self, rows, batch, source, observed_at=None):
        observed_at = time.time() if observed_at is None else observed_at
        with self.conn:
            for facts in rows:
                facts = dict(facts)
                online = facts.pop("recruiter_online", None)
                stamp = now()
                self.conn.execute("""INSERT INTO jobs(id,facts,list_hash,first_seen,last_seen) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET facts=excluded.facts,list_hash=excluded.list_hash,last_seen=excluded.last_seen""",
                                  (facts["id"], dumps(facts), digest(facts), stamp, stamp))
                self.conn.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?)", (batch, facts["id"], source, stamp))
                if online is True:
                    # Positive online evidence is useful; offline does not mean inactive today.
                    self.conn.execute("""INSERT INTO recruiter_activity VALUES(?,?,?,?)
                        ON CONFLICT(job_id) DO UPDATE SET label=excluded.label,observed_at=excluded.observed_at,source=excluded.source""",
                        (facts["id"], "在线", observed_at, "list_json.bossOnline"))

    def activity_hold(self, identifier):
        row = self.conn.execute("SELECT * FROM activity_holds WHERE job_id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    def suspend_match(self, identifier, review_id, state):
        if state not in ACTIVITY_WAITING:
            raise Stopped("invalid_activity_hold")
        existing = self.activity_hold(identifier)
        if existing and existing["state"] in ACTIVITY_WAITING:
            return existing  # Preserve the saved decision until the user explicitly reopens it.
        with self.conn:
            self.conn.execute("""INSERT INTO activity_holds(job_id,state,review_id,suspended_at)
                VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,
                review_id=excluded.review_id,suspended_at=excluded.suspended_at,
                awakened_at=NULL,awakened_batch=NULL""", (identifier, state, review_id, time.time()))
        return self.activity_hold(identifier)

    def save_detail(self, identifier, detail, state):
        detail = dict(detail)
        activity_present = "recruiter_active_label" in detail
        activity_label = detail.pop("recruiter_active_label", None)
        with self.conn:
            if activity_present:
                self.conn.execute("""INSERT INTO recruiter_activity VALUES(?,?,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET label=excluded.label,observed_at=excluded.observed_at,source=excluded.source""",
                    (identifier, activity_label if isinstance(activity_label, str) else None, time.time(), "detail_dom"))
            self.conn.execute("UPDATE jobs SET detail=?,detail_state=?,detail_seen=?,detail_list_hash=list_hash WHERE id=?",
                              (dumps(detail), state, time.time(), identifier))
            if state == "complete" and (detail.get("chat_button") or "").strip() == "继续沟通":
                self.observe_existing_contact(identifier)

    def observe_existing_contact(self, identifier):
        """A visible existing-chat button proves contact, never who sent which message."""
        self.job(identifier)
        current = self.conn.execute("SELECT * FROM outreach WHERE job_id=?", (identifier,)).fetchone()
        if current and current["status"] in OUTREACH_ATTEMPTED:
            return dict(current)  # Preserve verified sends and unresolved Skill attempts.
        result = dumps({"evidence": "detail_continue_button", "contact_source": "external_or_history",
                        "external_actions": 0, "next_action": "skip_already_contacted"})
        stamp = now()
        with self.conn:
            self.conn.execute("""INSERT INTO outreach
                (job_id,review_id,status,message,message_hash,recipient,created_at,updated_at,result)
                VALUES(?,NULL,'contacted_external','',?,NULL,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                status='contacted_external',updated_at=excluded.updated_at,result=excluded.result""",
                (identifier, digest(""), stamp, stamp, result))
        return dict(self.conn.execute("SELECT * FROM outreach WHERE job_id=?", (identifier,)).fetchone())

    def job(self, identifier):
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise Stopped("unknown_job_id:" + identifier)
        result = dict(row)
        result["facts"] = json.loads(result["facts"])
        result["detail"] = json.loads(result["detail"] or "null")
        result["detail_stale"] = bool(result["detail_list_hash"] and result["list_hash"] != result["detail_list_hash"])
        hold = self.activity_hold(identifier)
        result["activity_hold"] = hold
        result["needs_detail_refresh"] = False  # Legacy field; no automatic activity reactivation.
        version = [result["facts"], result["detail"], result["detail_state"]]
        # Keep old fingerprint formats readable; this legacy counter is never advanced.
        if hold and hold["generation"]:
            version.append({"activity_recheck_generation": hold["generation"]})
        result["facts_hash"] = digest(version)
        activity = self.conn.execute("SELECT label,observed_at,source FROM recruiter_activity WHERE job_id=?", (identifier,)).fetchone()
        result["recruiter_activity"] = dict(activity) if activity else None
        result["url"] = f"https://www.zhipin.com/job_detail/{identifier}.html"
        result["sources"] = [dict(r) for r in self.conn.execute("SELECT batch,source,observed_at FROM observations WHERE job_id=?", (identifier,))]
        contact = self.conn.execute("SELECT status FROM outreach WHERE job_id=?", (identifier,)).fetchone()
        result["outreach_status"] = contact[0] if contact else None
        result.pop("list_hash")
        result.pop("detail_list_hash")
        return result

    def fresh(self, job, hours):
        return (job["detail_state"] == "complete" and not job["detail_stale"]
                and time.time() - (job["detail_seen"] or 0) < hours * 3600)

    def summaries(self, limit=10, batch=None, unreviewed=False, user_hash=None, cache_hours=72):
        reviewed = {item["job_id"] for item in self.reviews() if self.review_complete(item)} if unreviewed else set()
        query = "SELECT id FROM jobs"
        params = []
        if batch:
            query += " WHERE id IN (SELECT job_id FROM observations WHERE batch=?)"
            params.append(batch)
        query += " ORDER BY last_seen DESC,id"
        if not unreviewed:
            query += " LIMIT ?"
            params.append(limit)
        result = []
        for row in self.conn.execute(query, params):
            if row[0] in reviewed:
                continue
            job = self.job(row[0])
            if unreviewed:
                contact = self.conn.execute("SELECT status FROM outreach WHERE job_id=?", (row[0],)).fetchone()
                if (job["activity_hold"] and job["activity_hold"]["state"] in ACTIVITY_WAITING
                        or contact and contact[0] in OUTREACH_ATTEMPTED
                        or job["detail_state"] in ("failed", "partial", "unavailable")):
                    continue
            result.append({k: job[k] for k in ("id", "facts", "detail_state", "detail_stale", "last_seen", "facts_hash", "url", "recruiter_activity", "activity_hold", "needs_detail_refresh", "outreach_status")})
            if len(result) >= limit:
                break
        return result

    def review(self, data, user_hash=None, cache_hours=72, activity_policy=DEFAULT_ACTIVITY):
        data = dict(data)
        job = self.job(data["job_id"])
        if job["activity_hold"] and job["activity_hold"]["state"] in ACTIVITY_WAITING:
            raise Stopped("saved_hold_requires_explicit_reset")
        if data.get("decision") not in ("shortlist", "hold", "reject"):
            raise Stopped("invalid_review_decision")
        if not isinstance(data.get("reasons"), list) or not data["reasons"]:
            raise Stopped("review_requires_reasons")
        draft = data.get("greeting_draft")
        if draft:
            if not isinstance(draft, str) or data["decision"] != "shortlist" or not self.fresh(job, cache_hours):
                raise Stopped("draft_requires_fresh_shortlisted_detail")
            if ("继续沟通" in ((job["detail"] or {}).get("chat_button") or "")
                    or job["outreach_status"] in OUTREACH_ATTEMPTED):
                raise Stopped("already_contacted")
            if not data.get("evidence"):
                raise Stopped("draft_requires_resume_and_job_evidence")
        for key in ("user_hash", "facts_hash", "draft_hash", "_packet_id", "_submission_hash"):
            data.pop(key, None)
        data.setdefault("stage", "detail" if self.fresh(job, cache_hours) else "list")
        if data["stage"] not in ("list", "detail"):
            raise Stopped("invalid_review_stage")
        if draft and data["stage"] != "detail":
            raise Stopped("draft_requires_detail_review")
        if data["stage"] == "detail" and not self.fresh(job, cache_hours):
            raise Stopped("detail_review_requires_fresh_detail")
        if data.get("next_action") in {*ACTIVITY_WAITING, "matched_activity_unknown"}:
            if data["stage"] != "detail" or data["decision"] != "shortlist":
                raise Stopped("activity_hold_requires_matched_detail_review")
            data.pop("next_action")  # The observed label determines which matched hold applies.
        activity_state = None
        if data["decision"] == "shortlist" and data["stage"] == "detail":
            activity = activity_gate(job["recruiter_activity"], activity_policy)
            if activity["state"] == "deferred":
                activity_state = "matched_inactive"
            if activity_state:
                data["next_action"] = activity_state
        with self.conn:
            cursor = self.conn.execute("INSERT INTO reviews(job_id,created_at,user_hash,facts_hash,payload) VALUES(?,?,?,?,?)",
                              (job["id"], now(), user_hash or self.ws.user_hash(), job["facts_hash"], dumps(data)))
            if activity_state:
                self.suspend_match(job["id"], cursor.lastrowid, activity_state)
            elif data["stage"] == "detail" or data["decision"] == "reject":
                self.conn.execute("UPDATE activity_holds SET state='cleared',review_id=? WHERE job_id=?",
                                  (cursor.lastrowid, job["id"]))
        return {"saved": True, "job_id": job["id"], "review_id": cursor.lastrowid,
                "decision": data["decision"], "greeting_saved": bool(draft)}

    def reviews(self, user_hash=None, cache_hours=72):
        result = []
        for row in self.conn.execute("SELECT * FROM reviews WHERE id IN (SELECT MAX(id) FROM reviews GROUP BY job_id) ORDER BY id DESC"):
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    @staticmethod
    def review_complete(review):
        data = review["payload"]
        return data.get("decision") == "reject" or (data.get("stage") == "detail" and
                data.get("next_action") not in ("fetch_detail", "read_failed"))

    def reset_reviews(self, ids, reason):
        if not reason.strip():
            raise Stopped("review_reset_reason_required")
        jobs = [self.job(i) for i in dict.fromkeys(ids)]
        results = []
        for job in jobs:
            if job["outreach_status"] in OUTREACH_ATTEMPTED:
                results.append({"job_id": job["id"], "reset": False, "reason": "contact_history_preserved"})
                continue
            with self.conn:
                self.conn.execute("UPDATE activity_holds SET state='cleared' WHERE job_id=?", (job["id"],))
                self.conn.execute("UPDATE jobs SET detail_state='pending',detail_seen=NULL WHERE id=?", (job["id"],))
                self.conn.execute("DELETE FROM outreach WHERE job_id=? AND status NOT IN ('sent','sending','uncertain','already_contacted','contacted_external')", (job["id"],))
                self.review({"job_id": job["id"], "decision": "hold", "stage": "list",
                             "reasons": [reason.strip()], "next_action": "fetch_detail"})
            results.append({"job_id": job["id"], "reset": True})
        return {"items": results, "external_actions": 0}

    def stats(self):
        return {"jobs": self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0],
                "details": dict(self.conn.execute("SELECT detail_state,count(*) FROM jobs GROUP BY detail_state")),
                "outreach": dict(self.conn.execute("SELECT status,count(*) FROM outreach GROUP BY status")),
                "reviews": self.conn.execute("SELECT count(*) FROM reviews").fetchone()[0]}


@contextmanager
def database(root):
    store = Store(Path(root) / "jobs.sqlite")
    try:
        yield store
    finally:
        store.conn.close()
