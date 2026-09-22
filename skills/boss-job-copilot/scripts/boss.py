"""BOSS collection, assistant review, local greeting previews and authorized messaging."""
from __future__ import annotations
import argparse
import asyncio
import importlib.metadata
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from bosslib.local import FileLock, RUNTIME_VERSION, Stopped, Workspace, database, dumps, initialize, read_json, write_json
from bosslib.filters import PARAMETERS, compile_plan, load_catalog, resolve_field, values
from bosslib.browser_process import process_alive


def request(ws, action, **values):
    with FileLock(ws.runtime / "client.lock"):
        state = ws.status()
        if not state.get("alive"):
            raise Stopped("browser_not_running_open_it_first")
        if (ws.runtime / "close").exists() or state.get("mode") == "closing":
            raise Stopped("browser_closing_wait_for_close_then_open")
        if state.get("mode") != "idle" or any((ws.runtime / "requests").glob("*.json")):
            raise Stopped("browser_busy")
        if state.get("runtime_version", 1) < RUNTIME_VERSION:
            raise Stopped("browser_runtime_outdated_reopen_with_updated_skill")
        if action in ("collect", "details", "outreach_send"):
            values["search_plan_sha256"] = ws.require_search_plan()["plan_sha256"]
        identifier = uuid.uuid4().hex
        values.update(id=identifier, action=action, created=time.time(), session=state["session"])
        write_json(ws.runtime / "requests" / (identifier + ".json"), values)
    return {"state": "submitted", "request_id": identifier, "result_file": str(ws.runtime / "results" / (identifier + ".json"))}


def worker_running(ws):
    try:
        with FileLock(ws.runtime / "worker.lock"):
            return False
    except Stopped:
        return True


def wait_browser_closed(ws, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        state = ws.status()
        if not state.get("alive") and not worker_running(ws) and not process_alive(state.get("pid")):
            if state.get("close_warning"):
                raise Stopped("browser_close_unconfirmed_check_owned_processes")
            return state
        if time.monotonic() >= deadline:
            raise Stopped("browser_close_pending_check_status_do_not_start_another_profile")
        time.sleep(0.2)


def close_browser(ws):
    # Process control only. Never open or update the jobs database here.
    with FileLock(ws.runtime / "client.lock"):
        state = ws.status()
        running = bool(state.get("alive") or worker_running(ws))
        if running:
            (ws.runtime / "close").touch()
    if running:
        state = wait_browser_closed(ws, timeout=55)
    from bosslib.page import check_profile_available
    check_profile_available(ws.profile)
    return {"requested": "close", "was_alive": running, "state": "closed", "browser_kept_open": False,
            "browser_exit": state.get("browser_exit")}


def open_browser(ws, boss_home=False):
    with FileLock(ws.runtime / "client.lock"):
        state = ws.status()
        if (ws.runtime / "close").exists() or state.get("mode") == "closing":
            state = wait_browser_closed(ws)
        if state.get("alive"):
            result = {"reused": True, **state}
        else:
            return start_browser(ws, state, boss_home)
    # request() takes client.lock itself. An explicit home open must also work
    # for a reused window, not silently return its old about:blank state.
    if boss_home:
        result.update(request(ws, "open_home"))
    return result


def start_browser(ws, state, boss_home):
    # Caller holds client.lock, including during pending-close recovery.
    if worker_running(ws):
        raise Stopped("browser_worker_unresponsive_close_or_restart_first")
    with FileLock(ws.runtime / "startup.lock"):
        previous_session = state.get("session")
        for name in ("close", "pause"):
            (ws.runtime / name).unlink(missing_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--workspace", str(ws.root), "_serve"]
        if boss_home:
            command.append("--boss-home")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with (ws.runtime / "worker.log").open("ab") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        deadline = time.time() + 55
        while time.time() < deadline:
            state = ws.status()
            # Windows venv python.exe may be a launcher with a different PID.
            if state.get("session") != previous_session and state.get("mode") in ("idle", "failed"):
                return {"reused": False, **state}
            if child.poll() is not None:
                raise Stopped("browser_worker_failed_see_runtime_worker_log")
            time.sleep(0.5)
        raise Stopped("browser_start_pending_check_status_do_not_retry")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", default=".boss-workspace",
                   help="Private data directory (default: ./.boss-workspace in the current working directory)")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--chrome")
    init.add_argument("--profile", help="Existing dedicated profile to reuse without copying it")
    sub.add_parser("doctor")
    plan = sub.add_parser("plan", help="Inspect or record confirmation of the local search plan")
    plans = plan.add_subparsers(dest="plan_command", required=True)
    plans.add_parser("status")
    confirm = plans.add_parser("confirm", help="Only after the user approves the specific plan; never infer consent")
    confirm.add_argument("--user-message", required=True, help="The user's actual confirmation of this plan")
    browser = sub.add_parser("browser")
    bs = browser.add_subparsers(dest="browser_command", required=True)
    op = bs.add_parser("open")
    op.add_argument("--boss-home", action="store_true", help="Open BOSS home once; default leaves about:blank")
    restart = bs.add_parser("restart", help="Close and reopen the same dedicated profile; does not change job records")
    restart_page = restart.add_mutually_exclusive_group()
    restart_page.add_argument("--boss-home", dest="boss_home", action="store_true",
                              help="Open BOSS home after restarting (default)")
    restart_page.add_argument("--blank", dest="boss_home", action="store_false", help="Explicitly leave a blank window")
    restart.set_defaults(boss_home=True)
    for action in ("status", "check", "pause", "close"):
        bs.add_parser(action)
    filters = sub.add_parser("filters")
    fs = filters.add_subparsers(dest="filter_command")
    fs.add_parser("list")
    show = fs.add_parser("show")
    show.add_argument("field", choices=tuple(PARAMETERS))
    show.add_argument("--contains", default="")
    show.add_argument("--city")
    show.add_argument("--limit", type=int, default=50)
    show.add_argument("--offset", type=int, default=0)
    fs.add_parser("snapshot")
    salary = fs.add_parser("salary", help="Choose ONE salary band containing the target upper end")
    salary.add_argument("--target", required=True, help="Monthly K amount or range, e.g. 30 or 28-32")
    refresh = fs.add_parser("refresh")
    refresh.add_argument("--reload", action="store_true", help="One paced reload of the current search page")
    collect = sub.add_parser("collect")
    collect.add_argument("--keyword", action="append")
    for field in PARAMETERS:
        collect.add_argument("--" + field.replace("_", "-"), action="append", help="Observed Chinese labels or codes; comma-separated")
    collect.add_argument("--filter-id", help="Reuse an observed filter snapshot instead of keyword/city")
    collect.add_argument("--config", help="JSON search parameters; CLI arguments override matching keys")
    collect.add_argument("--pages", "--page", type=int, help="Maximum list pages PER search combination, including page 1; default 10 unless configured; resume keeps the saved limit")
    collect.add_argument("--interval", type=float, help="Seconds between page actions, default/minimum 5; no hourly or daily quota")
    collect.add_argument("--max-jobs", type=int, help="Optional job limit PER search; no hidden 30-job cap")
    collect.add_argument("--dry-run", action="store_true", help="Resolve dictionaries/URLs locally without browser access")
    collect.add_argument("--resume", help="Resume a stopped collection run ID")
    details = sub.add_parser("details")
    details.add_argument("--ids", nargs="+", required=True)
    details.add_argument("--refresh", action="store_true", help="Read the page again, including the latest recruiter label; bypass JD cache")
    result = sub.add_parser("result")
    result.add_argument("--id", required=True)
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--id")
    jobs.add_argument("--ids", nargs="+", help="Read the full records for a selected group")
    jobs.add_argument("--batch")
    jobs.add_argument("--limit", type=int, default=20, help="Number to read, default 20; use 10 for initial calibration")
    jobs.add_argument("--view", choices=("raw", "review"), default="raw",
                      help="Review view keeps all list facts, full cached JD and work address without collection history")
    jobs.add_argument("--out", help="Write local JSONL; reuse the same private output file if desired")
    jobs.add_argument("--unreviewed", action="store_true", help="Jobs without a completed review; saved history never expires")
    jobs.add_argument("--unsent", action="store_true", help="Reviewed matches with drafts and no recorded contact attempt")
    review = sub.add_parser("review")
    review.add_argument("--file", required=True, help="Assistant-written review JSON")
    reset = sub.add_parser("review-reset", help="Reopen specified jobs only when the user requests it; preserve history")
    reset.add_argument("--ids", nargs="+", required=True)
    reset.add_argument("--reason", required=True, help="User's actual re-review request")
    report = sub.add_parser("report", help="Current database progress and collection gaps")
    report.add_argument("--out", help="Optionally write a local Markdown progress report")
    reviews = sub.add_parser("reviews")
    reviews.add_argument("--ids", nargs="+")
    sub.add_parser("queue", help="Local work queues including old pending details and unsent drafts")
    outreach = sub.add_parser("outreach")
    outs = outreach.add_subparsers(dest="outreach_command", required=True)
    preview = outs.add_parser("preview", help="Query selected reviews and print all results and greetings")
    preview.add_argument("--ids", nargs="+", required=True)
    preview.add_argument("--partial-reason", help="Actual queue shortfall or browser interruption; report remains partial")
    preview.add_argument("--out", help="Optionally export Markdown; default returns results for chat without a document")
    for operation in ("send", "verify"):
        command = outs.add_parser(operation)
        command.add_argument("--id", required=True, help="An existing job ID")
    authorize = outs.add_parser("authorize", help="Record the user's actual sending scope; defaults to approved batch drafts only")
    authorize.add_argument("--preview", required=True)
    authorize.add_argument("--user-message", required=True)
    authorize.add_argument("--scope", choices=("batch", "queue"), default="batch",
                           help="Use queue only if the user also approved continued sending to this round's queue")
    outs.add_parser("status")
    policy = outs.add_parser("policy", help="Read or set local outreach activity policy; does not alter website search filters")
    policy.add_argument("--activity", choices=("today", "3d", "week", "month", "any"))
    policy.add_argument("--initial-review-count", type=int, help="First calibration step, default 10; extend by this many until at least one can be contacted")
    outs.add_parser("simulate", help="Return to preview mode and revoke live authorization locally")
    sub.add_parser("stats")
    worker = sub.add_parser("_serve", help=argparse.SUPPRESS)
    worker.add_argument("--boss-home", action="store_true")
    return p


def run(args):
    if args.command == "init":
        return initialize(args.workspace, args.chrome, args.profile)
    ws = Workspace(args.workspace)
    if args.command == "filters" and args.filter_command in (None, "list", "show", "salary"):
        catalog = load_catalog(ws)
        if args.filter_command == "salary":
            from bosslib.filters import choose_salary
            return choose_salary(args.target, catalog)
        if args.filter_command in (None, "list"):
            return {"catalog_version": catalog["observed_at"], "filters": {
                k:{**{p:v for p,v in spec.items() if p != "options"}, "options_count":len(spec["options"]),
                   "unique_codes":len({o["code"] for o in spec["options"]})}
                for k,spec in catalog["filters"].items()}}
        spec = catalog["filters"][args.field]
        options = spec["options"]
        if args.field in ("area", "subway"):
            cities = resolve_field(catalog,"city",args.city)
            if len(cities)!=1:
                raise Stopped("select_one_city_for_region_options")
            region = read_json(ws.root/"filters/regions"/(cities[0]["code"]+".json"))
            if not region:
                return {"state":"needs_region_dictionary", "instruction":"Collect for this city once, or filters refresh on its search page; no custom script needed."}
            options = region["areas" if args.field=="area" else "subways"]
        options = [o for o in options if args.contains.casefold() in dumps(o).casefold()]
        if args.offset<0 or not 1<=args.limit<=5000:
            raise Stopped("invalid_filter_listing_range")
        return {"field":args.field,"catalog_version":catalog["observed_at"],
                **{k:v for k,v in spec.items() if k!="options"}, "total":len(options),
                "options":options[args.offset:args.offset+args.limit]}
    ws.require()
    if args.command == "plan":
        return ws.search_plan_status() if args.plan_command == "status" else ws.confirm_search_plan(args.user_message)
    if args.command == "_serve":
        from bosslib.runtime import serve
        asyncio.run(serve(ws.root, "https://www.zhipin.com/" if args.boss_home else None))
        return {"worker": "closed"}
    if args.command == "doctor":
        try:
            installed = importlib.metadata.version("zendriver")
        except importlib.metadata.PackageNotFoundError:
            installed = None
        return {"python": sys.version.split()[0], "zendriver": installed, "tested_zendriver": "0.16.0",
                "workspace": str(ws.root), "profile": str(ws.profile),
                "browser": ws.status(), "rate": ws.rate, "search_plan": ws.search_plan_status()}
    if args.command == "browser":
        action = args.browser_command
        if action == "open":
            return open_browser(ws, args.boss_home)
        if action == "restart":
            close_browser(ws)
            return open_browser(ws, args.boss_home)
        if action == "status":
            return ws.status()
        if action == "close":
            return close_browser(ws)
        if action == "pause":
            state = ws.status()
            if state.get("alive") and state.get("mode") == "busy":
                (ws.runtime / action).touch()
            return {"requested": action, "was_alive": state.get("alive", False), "browser_kept_open": action == "pause"}
        return request(ws, action)
    if args.command == "filters":
        if args.filter_command == "snapshot":
            return request(ws,"filters")
        return request(ws, "catalog", reload=args.reload)
    if args.command == "collect":
        if not args.dry_run:
            ws.require_search_plan()
        raw = read_json(args.config) if args.config else {}
        if not isinstance(raw,dict):
            raise Stopped("invalid_search_config")
        raw.update({key:getattr(args,key) for key in (*PARAMETERS,"keyword","pages","interval","max_jobs")
                    if getattr(args,key) is not None})
        if args.resume:
            if raw or args.filter_id:
                raise Stopped("invalid_resume_combination")
            identifier=uuid.UUID(hex=args.resume).hex
            progress=read_json(ws.runtime/"collections"/(identifier+".json"))
            if not progress:
                raise Stopped("unknown_collection_run")
            if progress.get("state")=="completed":
                return {"state":"already_completed","run_id":identifier}
            plan=progress["plan"]
        elif args.filter_id:
            if set(raw)-{"pages","interval","max_jobs"}:
                raise Stopped("invalid_snapshot_combination")
            from bosslib.filters import plan_from_snapshot
            plan=plan_from_snapshot(ws,args.filter_id,raw)
        else:
            raw.setdefault("interval",ws.rate["interval_seconds"])
            regions={p.stem:read_json(p) for p in (ws.root/"filters/regions").glob("*.json")}
            plan=compile_plan(raw,load_catalog(ws),regions)
        if args.dry_run:
            return {"state":"preview","plan":plan,"rate_limits":{"interval_seconds":plan["interval_seconds"]},
                    "warnings": [] if values(plan['spec'].get('salary')) or all(s['params'].get('jobType')=='1903' for s in plan['searches'])
                                else ['salary_not_specified:choose_one_band_or_explicit_不限'],
                    "note":"pages is an upper bound per search; gaps do not block database review; serial actions, no hourly/daily quota"}
        if (not args.resume and not values(plan['spec'].get('salary'))
                and any(s['params'].get('jobType') != '1903' for s in plan['searches'])):
            raise Stopped('salary_filter_required:choose_one_band_or_explicit_不限')
        return request(ws,"collect",plan=plan,resume=args.resume)
    if args.command == "details":
        ws.require_search_plan()
        return request(ws, "details", ids=args.ids, refresh=args.refresh)
    if args.command == "result":
        try:
            identifier = uuid.UUID(hex=args.id).hex
        except ValueError:
            raise Stopped("invalid_request_id")
        value = read_json(ws.runtime / "results" / (identifier + ".json"))
        if value is not None:
            return value
        return {"state": "pending_or_interrupted", "request_id": identifier, "browser": ws.status(),
                "instruction": "Inspect status and local database; do not automatically replay a lost request."}
    with database(ws.root) as store:
        if args.command in ("report", "stats"):
            from bosslib.outreach import Outreach
            from bosslib.reports import progress, render_progress
            result = progress(Outreach(ws, store))
            if args.command == "report" and args.out:
                out = Path(args.out).resolve()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_progress(result), encoding="utf-8")
                result["report_file"] = str(out)
            return result
        if args.command == "queue":
            from bosslib.outreach import Outreach
            return Outreach(ws, store).queue()
        if args.command == "outreach":
            from bosslib.outreach import Outreach
            ledger = Outreach(ws, store)
            operation = args.outreach_command
            if operation == "policy":
                return {"recruiter_activity": ws.outreach_activity(args.activity), "allow_unknown_activity": True,
                        "initial_review_count": ws.initial_review_count(args.initial_review_count),
                        "scope": "local_outreach_filter", "external_actions": 0}
            if operation == "preview":
                return ledger.preview(args.ids, args.partial_reason, args.out)
            if operation == "authorize":
                return ledger.authorize(args.preview, args.user_message, args.scope)
            if operation == "status":
                return {**ledger.policy(),
                        "records": [dict(r) for r in store.conn.execute("SELECT * FROM outreach ORDER BY updated_at DESC")]}
            if operation == "simulate":
                (ledger.root / "authorization.json").unlink(missing_ok=True)
                return {"mode": "preview", "external_actions": 0}
            if operation == "verify":
                return request(ws, "outreach_verify", job_id=args.id)
            from bosslib.outreach import TERMINAL
            record = ledger.record(args.id)
            if record and record["status"] in TERMINAL:
                return {"job_id": args.id, "status": record["status"], "skipped": True, "external_actions": 0}
            review = ledger.ready(args.id)
            auth = ledger.require_authorization(args.id, review["id"])
            return request(ws, "outreach_send", job_id=args.id, review_id=review["id"], authorization_id=auth["id"])
        if args.command == "jobs":
            from bosslib.outreach import Outreach
            if sum(bool(v) for v in (args.id, args.ids, args.unreviewed, args.unsent)) > 1:
                raise Stopped("select_one_job_query")
            if args.limit < 1:
                raise Stopped("job_limit_must_be_positive")
            limit = args.limit
            if args.id or args.ids:
                rows = [store.job(i) for i in dict.fromkeys(args.ids or [args.id])]
            elif args.unsent:
                rows = [store.job(i) for i in Outreach(ws, store).queue()["queues"]["draft_ready"][:limit]]
            else:
                rows = store.summaries(limit, args.batch, args.unreviewed)
            if args.view == "review":
                from bosslib.reports import review_view
                rows = [review_view(store.job(row["id"])) for row in rows]
            if args.out:
                out = Path(args.out).resolve()
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.open("w", encoding="utf-8") as stream:
                    for row in rows:
                        stream.write(dumps(row) + "\n")
                return {"rows": len(rows), "out": str(out)}
            return {"jobs": rows}
        if args.command == "review":
            from bosslib.reviews import submit_reviews
            return submit_reviews(ws, store, args.file)
        if args.command == "review-reset":
            return store.reset_reviews(args.ids, args.reason)
        if args.command == "reviews":
            return {"reviews": [r for r in store.reviews() if not args.ids or r["job_id"] in args.ids]}
    raise Stopped("unsupported_command")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args()
        result = run(args)
        print(dumps(result), flush=True)
        if args.command == "review" and result["failed"]:
            sys.exit(1)
    except (Stopped, ValueError, KeyError, TypeError, OSError) as exc:
        print(dumps({"state": "error", "reason": str(exc)}), flush=True)
        sys.exit(2)
