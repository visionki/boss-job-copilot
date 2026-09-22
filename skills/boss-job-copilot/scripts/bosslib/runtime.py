from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .local import ACTIVITY_WAITING, FileLock, RUNTIME_VERSION, Stopped, Workspace, database, digest, now, page_interval, parse_response, read_json, search_url, write_json
from .page import READ_PAGE, check_profile_available, detail_state, make_driver_config, page_problem, separate_company_text
from .catalog import READ_CATALOG, READ_LIST_STATE, clean_region, merge_view
from .filters import compile_plan, finish_region, load_catalog, request_matches, request_params
from .browser_process import chrome_processes, kill_owned_chrome


def browser_problem(reason):
    return reason.startswith(("browser_tab_", "browser_connection_", "browser_read_", "browser_navigation_"))


def connection_error(exc):
    # ProtocolException also represents JS errors, so do not classify all CDP
    # exceptions as disconnections (e.g. a missing send button is different).
    return (isinstance(exc, (ConnectionError, OSError)) or "websockets" in type(exc).__module__
            or any(text in str(exc).lower() for text in (
                "target closed", "target was closed", "session closed", "session with given id not found",
                "no target with given id", "connection closed", "not connected to devtools")))


def manual_check_required(reason):
    return reason.startswith(("manual_check_required", "login_required", "verification_",
                              "api_rejected", "list_http_401", "list_http_403", "list_http_429",
                              "platform_contact_limit"))


def session_blocking(reason):
    return (manual_check_required(reason) or browser_problem(reason)
            or reason.startswith(("login_unconfirmed", "blank_or_redirected")))


def recoverable_collection(reason):
    return (not session_blocking(reason) and reason.startswith((
        "invalid_job_id", "api_field_type_changed", "api_schema_changed", "response_read_failed",
        "no_matching_list_response", "list_", "region_", "page_load_timeout", "browser_operation_timeout")))


class Pace:
    """Serial page-action spacing and a manual-check latch; no volume quotas."""
    def __init__(self, profile, settings):
        self.path = profile / ".boss-skill-rate.json"
        self.interval = page_interval(settings.get("interval_seconds", 5))

    def delay(self, clock=None):
        clock = time.time() if clock is None else clock
        data = read_json(self.path, {})
        if data.get("blocked"):
            raise Stopped("manual_check_required:" + data["blocked"])
        times = [t for t in data.get("actions", []) if clock - t < 86400]
        return max(0, self.interval - (clock - max(times))) if times else 0

    async def before(self):
        delay = self.delay()
        if delay:
            await asyncio.sleep(delay)
        # Recheck after waiting; record attempts before touching the website.
        self.delay()
        data = read_json(self.path, {})
        clock = time.time()
        data["actions"] = [t for t in data.get("actions", []) if clock - t < 86400] + [clock]
        write_json(self.path, data)

    def block(self, reason):
        # A missing/blank tab is not evidence of a login or security challenge.
        if not manual_check_required(reason):
            return
        data = read_json(self.path, {})
        # Keep the original verification reason until an explicit healthy check.
        if data.get("blocked") and (data["blocked"] == "verification_required" or reason != "verification_required"):
            return
        data.update(blocked=reason, blocked_at=time.time())
        write_json(self.path, data)

    def clear(self):
        data = read_json(self.path, {})
        data.pop("blocked", None)
        data.pop("blocked_at", None)
        write_json(self.path, data)

    def clear_legacy_page_block(self):
        if read_json(self.path, {}).get("blocked") in ("blank_or_redirected", "login_unconfirmed", "job_unavailable"):
            self.clear()


FILTER_VIEW = r"""(() => {
    const visible = e => e.getClientRects().length > 0;
    const groups = Array.from(document.querySelectorAll('[class*="filter"], [class*="condition"]'))
        .filter(visible).filter(e => e.innerText.trim() && e.innerText.length < 2500)
        .map(e => ({tag: e.tagName, class_name: e.className, text: e.innerText.trim()}));
    return JSON.stringify({url: location.href, groups: groups.slice(0, 80)});
})()"""


class BrowserSession:
    def __init__(self, ws):
        self.ws = ws
        self.browser = self.tab = None
        self.pace = Pace(ws.profile, ws.rate)
        self.actions = 0
        self.accepting = False
        self.requests = {}
        self.tasks = set()
        self.error = None
        self.responses = 0
        self.seen = set()
        self.handlers = []
        self.collection_progress = None
        self.query_token = None

    async def start(self):
        import zendriver
        self.driver = zendriver
        check_profile_available(self.ws.profile)
        config_args = dict(user_data_dir=str(self.ws.profile), headless=False, sandbox=True, expert=False,
                           browser_args=["--no-first-run", "--no-default-browser-check", "--window-size=1400,950",
                                         "--remote-debugging-address=127.0.0.1"])
        if self.ws.settings.get("chrome"):
            config_args["browser_executable_path"] = self.ws.settings["chrome"]
        self.browser = await asyncio.wait_for(zendriver.start(config=make_driver_config(zendriver, **config_args)), 45)
        self.tab = self.browser.main_tab
        if self.tab is None:
            self.tab = await self.browser.get("about:blank")
        # Do not inspect a page or install network handlers in open-only mode.

    async def prepare_tab(self, create=False):
        """Rebind between requests only; never replay an action on a replacement tab."""
        try:
            targets = await asyncio.wait_for(self.browser.connection.send(self.driver.cdp.target.get_targets()), 5)
            live = {t.target_id: t for t in targets if t.type_ == "page"}
            tabs = {t.target.target_id: t for t in self.browser.tabs if t.target.target_id in live}
            if live.keys() - tabs.keys():
                await asyncio.wait_for(self.browser.update_targets(), 5)
                tabs = {t.target.target_id: t for t in self.browser.tabs if t.target.target_id in live}
            current = getattr(getattr(self.tab, "target", None), "target_id", None)
            boss_tabs = [key for key in tabs if urlsplit(live[key].url).hostname in ("www.zhipin.com", "zhipin.com")]
            chosen = current if current in boss_tabs else next(iter(boss_tabs), None)
            chosen = chosen or (current if current in tabs else next(iter(tabs), None))
            if chosen is not None:
                self.tab = tabs[chosen]
            elif create:
                self.tab = await asyncio.wait_for(self.browser.get("about:blank", new_tab=True), 15)
            else:
                raise Stopped("browser_tab_missing_open_boss_home_or_restart")
        except Stopped:
            raise
        except Exception as exc:
            raise Stopped("browser_connection_unavailable:" + type(exc).__name__) from exc

    async def close(self):
        """Let Chrome finish normally before Zendriver's terminate/kill fallback."""
        if not self.browser:
            return {"method": "not_started", "verified": True}
        process = getattr(self.browser, "_process", None)
        try:
            owned = await asyncio.to_thread(chrome_processes, self.ws.profile)
        except Exception:
            owned = []  # Still request a normal close; never force unverified child processes.
        # Only this worker's original browser process authorizes child cleanup.
        may_kill_children = process is not None and any(p["pid"] == process.pid for p in owned)
        forced = False
        try:
            if process is not None and process.poll() is None:
                try:
                    await asyncio.wait_for(self.browser.connection.send(self.driver.cdp.browser.close()), 3)
                except Exception:
                    pass  # Chrome may disconnect before acknowledging Browser.close.
                try:
                    await asyncio.to_thread(process.wait, timeout=8)
                except subprocess.TimeoutExpired:
                    forced = True
                    if may_kill_children:
                        await asyncio.to_thread(kill_owned_chrome, self.ws.profile, owned)
                    else:
                        process.kill()  # Popen handle owned by this session, never a process-name kill.
                    await asyncio.to_thread(process.wait, timeout=5)
            # The root has exited; stop now only releases Zendriver connections/resources.
            await asyncio.wait_for(self.browser.stop(), 5)
            remaining = await asyncio.to_thread(chrome_processes, self.ws.profile)
            for _ in range(3):
                if not remaining:
                    break
                await asyncio.sleep(.3)
                remaining = await asyncio.to_thread(chrome_processes, self.ws.profile)
            if remaining and may_kill_children:
                forced = bool(await asyncio.to_thread(kill_owned_chrome, self.ws.profile, owned)) or forced
                remaining = await asyncio.to_thread(chrome_processes, self.ws.profile)
            if remaining or process is not None and process.poll() is None:
                raise Stopped("browser_close_unconfirmed")
            return {"method": "forced" if forced else "graceful", "verified": True}
        except Exception as exc:
            raise Stopped("browser_close_unconfirmed") from exc

    async def read(self):
        try:
            raw = await asyncio.wait_for(self.tab.evaluate(READ_PAGE, return_by_value=True), 5)
        except asyncio.TimeoutError as exc:
            raise Stopped("browser_read_timeout") from exc
        except Exception as exc:
            raise Stopped("browser_connection_lost:" + type(exc).__name__) from exc
        return separate_company_text(json.loads(raw))

    async def check(self):
        page = await self.read()
        reason = page_problem(page)
        self.pace.clear_legacy_page_block()
        if reason in ("blank_or_redirected", "login_unconfirmed"):
            blocked = read_json(self.pace.path, {}).get("blocked")
            return {"login": "not_checked", "reason": reason, "url": page.get("url"), "title": page.get("title"),
                    "needs_manual_action": bool(blocked), "blocked_reason": blocked,
                    "next_action": "handle_previous_block_then_check" if blocked else "open_boss_home_then_check"}
        if reason:
            self.pace.block(reason)
        else:
            self.pace.clear()
        return {"login": "confirmed" if not reason else reason,
                "url": page.get("url"), "title": page.get("title"), "needs_manual_action": bool(reason)}

    async def open_home(self):
        await self.prepare_tab(create=True)
        self.pace.clear_legacy_page_block()
        await self.action("https://www.zhipin.com/")
        result = {"opened": "https://www.zhipin.com/", "login": "not_checked", "next_action": "browser_check"}
        try:
            await self.settle(timeout=15)  # get() can return before the homepage has rendered.
        except Stopped as exc:
            result["navigation_note"] = str(exc)  # Leave login/challenge handling to explicit check.
        return result

    async def action(self, url=None):
        await self.pace.before()
        if self.error:
            raise Stopped(self.error)
        self.actions += 1
        try:
            if url:
                await asyncio.wait_for(self.tab.get(url), 35)
            else:
                for event in ("keyDown", "keyUp"):
                    await asyncio.wait_for(self.tab.send(self.driver.cdp.input_.dispatch_key_event(
                        type_=event, key="End", code="End", windows_virtual_key_code=35)), 10)
        except asyncio.TimeoutError as exc:
            raise Stopped("browser_navigation_timeout") from exc
        except Exception as exc:
            raise Stopped("browser_connection_lost:" + type(exc).__name__) from exc

    async def healthy_page(self):
        page = await self.read()
        problem = page_problem(page)
        if problem and problem != "job_unavailable":
            raise Stopped(problem)
        return page

    async def settle(self, after_page=None, detail_id=None, timeout=20, expected_path=None, previous_document=None):
        """Read local page state until the expected data is ready, without a fixed sleep."""
        page = None

        async def ready():
            nonlocal page
            while True:
                if self.error:
                    raise Stopped(self.error)
                page = await self.read()
                problem = page_problem(page)
                if problem == "job_unavailable" and detail_id:
                    return page
                if problem not in (None, "blank_or_redirected", "login_unconfirmed"):
                    raise Stopped(problem)
                navigation_ready = ((expected_path is None or urlsplit(page.get("url", "")).path.rstrip("/") == expected_path)
                                    and (previous_document is None or page.get("document_id") != previous_document))
                if not problem and navigation_ready:
                    if detail_id and detail_state(page, detail_id) == "complete":
                        return page
                    if not detail_id and (after_page is None or self.last_page > after_page):
                        return page
                # DOM reads only: no reload, navigation, scrolling, or HTTP replay.
                await asyncio.sleep(0.2)

        try:
            return await asyncio.wait_for(ready(), timeout)
        except asyncio.TimeoutError:
            if self.error:
                raise Stopped(self.error)
            if detail_id and page is not None:
                return page  # Preserve partial content; details() will stop this request.
            if page is not None and page_problem(page):
                raise Stopped(page_problem(page))
            raise Stopped("no_matching_list_response" if after_page == 0 else
                          "list_pagination_stalled" if after_page is not None else "page_load_timeout")

    def list_url(self, url):
        parts = urlsplit(url)
        return parts.hostname == "www.zhipin.com" and parts.path == "/wapi/zpgeek/search/joblist.json"

    async def on_request(self, event):
        if self.accepting and event.request.method in ("GET", "POST") and self.list_url(event.request.url):
            params = request_params(event.request.url, getattr(event.request, "post_data", None))
            if not request_matches(params, self.expected_params):
                return
            number = params.get("page", [])
            number = number[0] if isinstance(number, list) and len(number) == 1 else number
            try:
                number = int(number)
            except (TypeError, ValueError):
                self.error = "list_page_number_missing"
                return
            if number < 1:
                self.error = "list_page_number_invalid"
                return
            self.requests[event.request_id] = {"page":number,"token":self.query_token,"source":self.source}

    async def on_response(self, event):
        if self.accepting and event.request_id in self.requests and getattr(event.type_, "value", event.type_) != "Preflight":
            if event.response.status != 200:
                self.error = "list_http_" + str(int(event.response.status))
                self.requests.pop(event.request_id, None)

    async def on_failed(self, event):
        if event.request_id in self.requests:
            self.requests.pop(event.request_id, None)
            self.error = "list_network_failed"

    async def on_finished(self, event):
        if self.accepting and event.request_id in self.requests:
            meta = self.requests.pop(event.request_id)
            task = asyncio.create_task(self.response_body(event.request_id, meta))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def response_body(self, request_id, meta):
        try:
            body, encoded = await asyncio.wait_for(self.tab.send(self.driver.cdp.network.get_response_body(request_id)), 15)
            if not self.accepting or meta["token"] != self.query_token:
                return
            payload = json.loads(base64.b64decode(body).decode() if encoded else body)
            issues = []
            rows = parse_response(payload, issues=issues)
            if issues:
                self.query_progress.setdefault("skipped_rows", []).extend(
                    {"page": meta["page"], **issue} for issue in issues)
                self.save_progress()
            if meta["page"] > self.last_page + 1:
                raise Stopped("list_page_gap")
            if meta["page"] < self.last_page:
                return  # A late duplicate response must not roll back the browser cursor.
            selected = []
            for row in rows:
                if row["id"] in self.seen or self.max_jobs is None or len(self.seen) < self.max_jobs:
                    selected.append(row)
                    self.seen.add(row["id"])
            self.store.save_jobs(selected, self.batch, meta["source"])
            self.responses += 1
            self.last_page = meta["page"]
            more = payload["zpData"].get("hasMore")
            if more is not None and not isinstance(more, bool):
                raise Stopped("list_has_more_schema_changed")
            self.has_more = more
            self.query_progress["pages"][str(meta["page"])] = digest([r["id"] for r in rows])
            self.query_progress.update(last_page=self.last_page,has_more=more,job_ids=sorted(self.seen))
            self.save_progress()
        except Stopped as exc:
            if self.accepting and meta["token"] == self.query_token:
                self.error = str(exc)
        except Exception as exc:
            if self.accepting and meta["token"] == self.query_token:
                self.error = "response_read_failed:" + type(exc).__name__

    async def listen(self):
        network = self.driver.cdp.network
        self.handlers = [(network.RequestWillBeSent, self.on_request), (network.ResponseReceived, self.on_response),
                         (network.LoadingFinished, self.on_finished), (network.LoadingFailed, self.on_failed)]
        for event, handler in self.handlers:
            self.tab.add_handler(event, handler)
        await self.tab.send(network.enable())
        self.accepting = True

    async def unlisten(self):
        self.accepting = False
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.tasks.clear()
        self.requests.clear()
        try:
            for event, handler in self.handlers:
                self.tab.remove_handlers(event, handler)
            if self.handlers:
                await asyncio.wait_for(self.tab.send(self.driver.cdp.network.disable()), 3)
        except Exception:
            pass  # A closed tab must not mask the original request result.
        finally:
            self.handlers = []

    async def filters(self):
        await self.healthy_page()
        view = json.loads(await self.tab.evaluate(FILTER_VIEW, return_by_value=True))
        view["url"] = search_url(view["url"])
        view["observed_at"] = now()
        view["id"] = digest(view)[:16]
        view["scope"] = "observed_url_and_visible_labels_only; hidden options are unverified"
        write_json(self.ws.root / "filters" / (view["id"] + ".json"), view)
        return view

    async def catalog(self, reload=False, city=None):
        page = await self.settle(expected_path="/web/geek/jobs" if city else None)
        if urlsplit(page["url"]).path.rstrip("/") != "/web/geek/jobs":
            raise Stopped("invalid_catalog_page_open_search_first")
        if reload:
            await self.pace.before()
            self.actions += 1
            await self.tab.send(self.driver.cdp.page.reload())
            await self.settle(previous_document=page.get("document_id"), expected_path="/web/geek/jobs")
        last_error = "catalog_load_timeout"

        async def ready():
            nonlocal last_error
            while True:
                await self.healthy_page()
                view = json.loads(await self.tab.evaluate(READ_CATALOG, return_by_value=True))
                try:
                    catalog = merge_view(load_catalog(self.ws), view)
                    region = clean_region(view["region"])
                    if city and region["city"] != city:
                        raise Stopped("region_city_not_ready")
                    return catalog, region
                except Stopped as exc:
                    last_error = str(exc)
                await asyncio.sleep(0.2)

        try:
            catalog, region = await asyncio.wait_for(ready(), 20)
        except asyncio.TimeoutError:
            raise Stopped(last_error)
        write_json(self.ws.root/"filters/catalog.json", catalog)
        write_json(self.ws.root/"filters/regions"/(region["city"]+".json"), region)
        return {"catalog_version":catalog["observed_at"],"city":region["city"],
                "counts":{k:len(v["options"]) for k,v in catalog["filters"].items()},"region":region}

    def save_progress(self):
        if self.collection_progress is not None:
            self.collection_progress["updated_at"] = now()
            write_json(self.ws.runtime/"collections"/(self.batch+".json"),self.collection_progress)

    def collection_summary(self):
        if self.collection_progress is None:
            return None
        return {"run_id":self.batch,"batch":self.batch,"state":self.collection_progress["state"],
                "pages_per_search":self.collection_progress["plan"]["pages_per_search"],
                "searches":[{k:v for k,v in q.items() if k not in ("job_ids","pages")} |
                            {"pages_collected":len(q["pages"]),"jobs":len(q["job_ids"])}
                            for q in self.collection_progress["queries"]],"browser_kept_open":True}

    async def collect(self, request):
        await self.healthy_page()  # No navigation at all if the current session needs login.
        plan = request.get("plan")
        if not plan or plan.get("schema") != 2:
            raise Stopped("invalid_collection_plan")
        self.pace = Pace(self.ws.profile,{"interval_seconds":plan["interval_seconds"]})
        self.max_jobs = plan["max_jobs_per_search"]
        if request.get("resume"):
            self.batch = uuid.UUID(hex=request["resume"]).hex
            self.collection_progress = read_json(self.ws.runtime/"collections"/(self.batch+".json"))
            if not self.collection_progress or self.collection_progress["plan"] != plan:
                raise Stopped("invalid_resume_plan")
        else:
            self.collection_progress = {"run_id":self.batch,"plan":plan,"queries":[
                {"key":item["key"],"url":item["url"],"state":"pending","pages":{},"job_ids":[],"last_page":0,"has_more":None}
                for item in plan["searches"]]}
        self.collection_progress["state"] = "running"
        self.save_progress()
        for item, progress in zip(plan["searches"], self.collection_progress["queries"]):
            if progress["state"] == "completed":
                continue
            self.query_progress = progress
            self.error = None
            self.responses, self.seen, self.last_page, self.has_more = 0, set(progress["job_ids"]), 0, None
            self.query_token = uuid.uuid4().hex
            try:
                if item.get("needs_region_dictionary"):
                    catalog = load_catalog(self.ws)
                    cached = read_json(self.ws.root/"filters/regions"/(item["params"]["city"]+".json"))
                    if not cached:
                        # Preparing a city dictionary is not a collected page; no list listener is active.
                        await self.action(item["url"])
                        cached = (await self.catalog(city=item["params"]["city"]))["region"]
                    item = finish_region(item, catalog, cached)
                    if item["needs_region_dictionary"]:
                        raise Stopped("region_dictionary_incomplete")
                self.source, self.expected_params = item["url"], item["params"]
                progress.update(url=self.source,state="running")
                await self.healthy_page()
                # A same-window resume may continue at its verified current page. Otherwise replay the
                # unfinished query from page 1, with ordinary paced scrolling and DB deduplication.
                current = json.loads(await self.tab.evaluate(READ_LIST_STATE,return_by_value=True)) if progress["last_page"] else None
                continuing = bool(current and urlsplit(current["url"]).path.rstrip("/")=="/web/geek/jobs"
                                  and current["page"] == progress["last_page"]
                                  and request_matches(current["params"],self.expected_params)
                                  and digest(current.get("page_ids")) == progress["pages"].get(str(current["page"])))
                if continuing:
                    self.last_page, self.has_more = current["page"], current["has_more"]
                progress["resume_mode"] = "continue_current_page" if continuing else "normal_page_load"
                self.save_progress()
                await self.listen()
                if not continuing:
                    await self.action(self.source)
                    await self.settle(after_page=0)
                    if self.responses == 0:
                        raise Stopped("no_matching_list_response")
                while self.last_page < plan["pages_per_search"]:
                    if self.has_more is False:
                        break
                    if self.max_jobs is not None and len(self.seen) >= self.max_jobs:
                        break
                    before = self.last_page
                    await self.action()
                    await self.settle(after_page=before)
                    if self.last_page <= before:
                        raise Stopped("list_pagination_stalled")
                progress.update(state="completed",stop_reason=("no_more_results" if self.has_more is False else
                                "max_jobs" if self.max_jobs is not None and len(self.seen)>=self.max_jobs else "page_limit"))
                self.save_progress()
            except (Stopped, asyncio.TimeoutError) as exc:
                reason = "browser_operation_timeout" if isinstance(exc, asyncio.TimeoutError) else str(exc)
                if not recoverable_collection(reason):
                    raise
                progress.update(state="skipped", stop_reason=reason)
                self.save_progress()
            finally:
                await self.unlisten()
        self.error = None
        self.collection_progress["state"] = ("completed_with_gaps" if any(
            q["state"] != "completed" or q.get("skipped_rows") for q in self.collection_progress["queries"])
            else "completed")
        self.save_progress()
        return self.collection_summary()

    async def details(self, request):
        ids = list(dict.fromkeys(request.get("ids", [])))
        if not ids:
            raise Stopped("select_at_least_one_job_id")
        jobs = [self.store.job(identifier) for identifier in ids]  # Validate entire selection before browsing.
        results = []
        for job in jobs:
            if job["activity_hold"] and job["activity_hold"]["state"] in ACTIVITY_WAITING:
                results.append({"id": job["id"], "state": job["activity_hold"]["state"],
                                "reason": "saved_hold_requires_explicit_reset"})
                continue
            if not request.get("refresh") and self.store.fresh(job, self.ws.settings["detail_cache_hours"]):
                if ((job["detail"] or {}).get("chat_button") or "").strip() == "继续沟通":
                    self.store.observe_existing_contact(job["id"])
                results.append({"id": job["id"], "state": "cached", "recruiter_activity": job["recruiter_activity"],
                                "outreach_status": self.store.job(job["id"])["outreach_status"]})
                continue
            try:
                await self.healthy_page()
                await self.action(job["url"])
                page = await self.settle(detail_id=job["id"])
                problem = page_problem(page)
                if problem and session_blocking(problem):
                    raise Stopped(problem)
                state = "unavailable" if problem == "job_unavailable" else detail_state(page, job["id"])
                detail = {k: page.get(k) for k in ("url", "job_detail", "company_info", "work_address", "chat_button", "recruiter_active_label")}
                detail["problem"] = problem
                self.store.save_detail(job["id"], detail, state)
                results.append({"id": job["id"], "state": state, "recruiter_activity": self.store.job(job["id"])["recruiter_activity"],
                                "outreach_status": self.store.job(job["id"])["outreach_status"], "reason": problem or
                                ("detail_incomplete" if state == "partial" else None)})
            except (Stopped, asyncio.TimeoutError) as exc:
                reason = "browser_operation_timeout" if isinstance(exc, asyncio.TimeoutError) else str(exc)
                if session_blocking(reason) or reason == "user_paused":
                    raise
                self.store.save_detail(job["id"], {"problem": reason}, "failed")
                results.append({"id": job["id"], "state": "failed", "reason": reason})
        return {"jobs": results, "actions": self.actions, "browser_kept_open": True}

    async def execute(self, request):
        self.actions, self.error = 0, None
        self.collection_progress = None
        self.pace = Pace(self.ws.profile,self.ws.rate)
        self.batch = request["id"]
        action = request["action"]
        if action == "open_home":
            return await self.open_home()
        if action in ("check", "filters", "catalog", "outreach_verify"):
            await self.prepare_tab()
        if action == "check":
            return await self.check()
        if action == "filters":
            return await self.filters()
        if action == "catalog":
            return await self.catalog(request.get("reload",False))
        if action == "outreach_verify":
            from .outreach import verify
            with database(self.ws.root) as self.store:
                return await verify(self, request)
        if action not in ("collect", "details", "outreach_send"):
            raise Stopped("unsupported_action")
        confirmed = self.ws.require_search_plan()
        if request.get("search_plan_sha256") != confirmed["plan_sha256"]:
            raise Stopped("search_plan_request_version_changed")
        await self.prepare_tab()
        with database(self.ws.root) as self.store:
            if action == "outreach_send":
                from .outreach import send
                return await send(self, request)
            return await (self.collect(request) if action == "collect" else self.details(request))


async def serve(workspace, url=None):
    ws = Workspace(workspace).require()
    session = BrowserSession(ws)
    state = {"pid": os.getpid(), "session": uuid.uuid4().hex, "mode": "starting", "started_at": now(),"runtime_version":RUNTIME_VERSION}
    current = None

    def persist():
        state["heartbeat"] = time.time()
        write_json(ws.runtime / "state.json", state)

    async def heartbeat():
        while True:
            persist()  # Disk only: never evaluates a page or refreshes a tab.
            await asyncio.sleep(2)

    with FileLock(ws.runtime / "worker.lock"), FileLock(ws.profile / ".collector.lock"):
        beat = asyncio.create_task(heartbeat())
        try:
            await session.start()
            if url:
                # Opening BOSS once is explicit; leave its content entirely to the user.
                if url != "https://www.zhipin.com/":
                    raise Stopped("open_url_must_be_boss_home")
                try:
                    await session.open_home()
                except Stopped as exc:
                    state["open_note"] = str(exc) + "; window left open for manual use"
            state.update(mode="idle", request=None)
            persist()
            while not (ws.runtime / "close").exists():
                request_files = sorted((ws.runtime / "requests").glob("*.json"))
                if not request_files:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    with FileLock(ws.runtime / "client.lock"):
                        file = request_files[0]
                        request = read_json(file)
                        file.unlink()
                        state.update(mode="busy", request=request["id"])
                        persist()
                except Stopped:
                    await asyncio.sleep(0.5)
                    continue
                result_path = ws.runtime / "results" / (request["id"] + ".json")
                if request.get("session") != state["session"] or time.time() - request.get("created", 0) > 120:
                    write_json(result_path, {"state": "stopped", "reason": "expired_request_not_replayed"})
                    state.update(mode="idle", request=None)
                    persist()
                    continue
                state.update(mode="busy", request=request["id"])
                persist()
                try:
                    current = asyncio.create_task(session.execute(request))
                    cancelled = False
                    while not current.done():
                        if not cancelled and ((ws.runtime / "pause").exists() or (ws.runtime / "close").exists()):
                            current.cancel()
                            cancelled = True
                        await asyncio.sleep(0.5)
                    result = {"state": "completed", "result": await current}
                except asyncio.CancelledError:
                    result = {"state": "stopped", "reason": "browser_closed" if (ws.runtime / "close").exists() else "user_paused"}
                except Exception as exc:
                    reason = "browser_operation_timeout" if isinstance(exc, asyncio.TimeoutError) else str(exc) if isinstance(exc, Stopped) else type(exc).__name__
                    blocked = manual_check_required(reason)
                    if blocked:
                        session.pace.block(reason)
                    result = {"state": "stopped", "reason": reason, "needs_manual_check": blocked,
                              "next_action": "recover_browser_then_check" if browser_problem(reason) or reason == "blank_or_redirected" else "review_local_database"}
                if session.collection_progress is not None:
                    if result["state"] != "completed":
                        session.collection_progress.update(state="stopped",reason=result.get("reason"))
                        session.save_progress()
                    result["collection"] = session.collection_summary()
                result.update(id=request["id"], finished_at=now(), actions=session.actions)
                write_json(result_path, result)
                (ws.runtime / "pause").unlink(missing_ok=True)
                state.update(mode="idle", request=None, last_result=request["id"])
                persist()
        except Exception as exc:
            state.update(mode="failed", error=str(exc) if isinstance(exc, Stopped) else type(exc).__name__)
        finally:
            if state["mode"] != "failed":
                state["mode"] = "closing"
                persist()
            if current and not current.done():
                current.cancel()
                await asyncio.gather(current, return_exceptions=True)
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            if session.browser:
                try:
                    state["browser_exit"] = await session.close()
                except Exception:
                    state["close_warning"] = "browser_close_unconfirmed"
            if state["mode"] != "failed":
                state["mode"] = "closed"
            persist()
