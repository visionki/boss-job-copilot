"""Browser recovery and database independence; Chrome tests use intercepted HTML only."""
import asyncio
import base64
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boss import close_browser, open_browser, parser, request, run, wait_browser_closed
from bosslib.local import RUNTIME_VERSION, Stopped, Workspace, database, initialize, normalize, read_json, write_json
from bosslib.page import page_problem
from bosslib.runtime import BrowserSession, serve
from bosslib.browser_process import chrome_processes, kill_owned_chrome, process_alive


class LifecycleCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        initialize(self.temp.name)
        self.ws = Workspace(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def live_state(self, **extra):
        state = dict(heartbeat=time.time(), mode="idle", session="existing", runtime_version=RUNTIME_VERSION)
        state.update(extra)
        write_json(self.ws.runtime / "state.json", state)
        return {**state, "alive": True}

    def test_home_on_reused_window_submits_real_navigation(self):
        self.live_state()
        with patch("boss.subprocess.Popen") as launch:
            result = open_browser(self.ws, boss_home=True)
        self.assertTrue(result["reused"])
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(read_json(result["result_file"].replace("results", "requests"))["action"], "open_home")
        launch.assert_not_called()

    def test_open_waits_for_pending_close_instead_of_reusing_old_worker(self):
        live = self.live_state()
        (self.ws.runtime / "close").touch()
        closed = {**live, "mode": "closed", "alive": False}
        new = {**live, "session": "new"}
        with patch.object(self.ws, "status", side_effect=[live, live, closed, new]), \
                patch("boss.time.sleep"), patch("boss.subprocess.Popen") as launch:
            result = open_browser(self.ws)
        self.assertFalse(result["reused"])
        self.assertEqual(result["session"], "new")
        launch.assert_called_once()

    def test_pending_close_rejects_requests_and_does_not_start_a_second_owner(self):
        self.live_state()
        (self.ws.runtime / "close").touch()
        with self.assertRaisesRegex(Stopped, "browser_closing"):
            request(self.ws, "check")
        with self.assertRaisesRegex(Stopped, "browser_close_pending"):
            wait_browser_closed(self.ws, timeout=0)
        with patch("boss.wait_browser_closed", side_effect=Stopped("browser_close_pending")), \
                patch("boss.subprocess.Popen") as launch:
            with self.assertRaisesRegex(Stopped, "browser_close_pending"):
                open_browser(self.ws)
            launch.assert_not_called()

    def test_restart_runs_close_before_open_and_keeps_workspace(self):
        events = []
        with patch("boss.close_browser", side_effect=lambda ws: events.append(("close", ws.root))), \
                patch("boss.open_browser", side_effect=lambda ws, home: events.append(("open", ws.root, home))):
            run(parser().parse_args(["--workspace", str(self.ws.root), "browser", "restart", "--boss-home"]))
        self.assertEqual(events, [("close", self.ws.root), ("open", self.ws.root, True)])

    def test_restart_defaults_to_home_but_open_remains_window_only(self):
        self.assertTrue(parser().parse_args(['browser', 'restart']).boss_home)
        self.assertFalse(parser().parse_args(['browser', 'restart', '--blank']).boss_home)
        self.assertFalse(parser().parse_args(['browser', 'open']).boss_home)

    def test_unconfirmed_close_prevents_a_new_owner(self):
        write_json(self.ws.runtime / 'state.json', {'mode': 'closed', 'close_warning': 'browser_close_unconfirmed'})
        (self.ws.runtime / 'close').touch()
        with patch('boss.subprocess.Popen') as launch:
            with self.assertRaisesRegex(Stopped, 'close_unconfirmed'):
                open_browser(self.ws)
            launch.assert_not_called()

    def test_cleanup_only_terminates_same_profile_and_same_process_instance(self):
        owned = [{'pid': 101, 'created': 'original'}, {'pid': 102, 'created': 'child'}]
        with patch('bosslib.browser_process.chrome_processes', return_value=[
                {'pid': 101, 'created': 'recycled'}, {'pid': 102, 'created': 'child'},
                {'pid': 103, 'created': 'new_browser'}]), patch('bosslib.browser_process.powershell') as terminate:
            self.assertEqual(kill_owned_chrome(self.ws.profile, owned), 1)
        self.assertEqual(json.loads(terminate.call_args.kwargs['BOSS_COPILOT_OWNED_CHROME']), [owned[1]])

    @unittest.skipUnless(os.name == 'nt', 'Windows process enumeration')
    def test_process_profile_match_is_exact_not_a_directory_prefix(self):
        rows = [{'ProcessId': 101, 'Created': 'a', 'CommandLine': 'chrome.exe --user-data-dir="' + str(self.ws.profile) + '" --flag'},
                {'ProcessId': 102, 'Created': 'b', 'CommandLine': 'chrome.exe --user-data-dir="' + str(self.ws.profile) + '-other" --flag'}]
        with patch('bosslib.browser_process.powershell', return_value=rows):
            self.assertEqual(chrome_processes(self.ws.profile), [{'pid': 101, 'created': 'a'}])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        initialize(self.temp.name)
        self.ws = Workspace(self.temp.name)
        self.session = BrowserSession(self.ws)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_graceful_close_waits_before_releasing_driver(self):
        process = Mock(pid=101)
        process.poll.return_value = None
        process.wait.side_effect = lambda **kw: setattr(process.poll, 'return_value', 0)
        async def release():
            self.assertEqual(process.poll(), 0)
        self.session.browser = SimpleNamespace(_process=process, connection=SimpleNamespace(send=AsyncMock()),
                                               stop=AsyncMock(side_effect=release))
        self.session.driver = SimpleNamespace(cdp=SimpleNamespace(browser=SimpleNamespace(close=lambda: 'close')))
        with patch('bosslib.runtime.chrome_processes', side_effect=[[{'pid': 101, 'created': 'owned'}], []]), \
                patch('bosslib.runtime.kill_owned_chrome') as force:
            result = await self.session.close()
        self.assertEqual(result, {'method': 'graceful', 'verified': True})
        force.assert_not_called()
        process.kill.assert_not_called()

    async def test_stuck_browser_falls_back_to_its_owned_processes_and_verifies_exit(self):
        process = Mock(pid=101)
        process.poll.side_effect = [None, 0]
        process.wait.side_effect = [subprocess.TimeoutExpired('fixture chrome', 8), 0]
        self.session.browser = SimpleNamespace(_process=process, connection=SimpleNamespace(send=AsyncMock()), stop=AsyncMock())
        self.session.driver = SimpleNamespace(cdp=SimpleNamespace(browser=SimpleNamespace(close=lambda: 'close')))
        owned = [{'pid': 101, 'created': 'root'}, {'pid': 102, 'created': 'child'}]
        with patch('bosslib.runtime.chrome_processes', side_effect=[owned, []]), \
                patch('bosslib.runtime.kill_owned_chrome', return_value=2) as force:
            result = await self.session.close()
        self.assertEqual(result['method'], 'forced')
        force.assert_called_once_with(self.ws.profile, owned)
        process.kill.assert_not_called()

    async def test_blank_check_is_not_a_login_failure_and_clears_only_legacy_page_block(self):
        self.session.read = AsyncMock(return_value={"url": "about:blank", "text": "", "title": "", "logged_in": False})
        write_json(self.session.pace.path, {"blocked": "blank_or_redirected", "actions": [123]})
        result = await self.session.check()
        self.assertEqual(result["login"], "not_checked")
        self.assertFalse(result["needs_manual_action"])
        self.assertEqual(read_json(self.session.pace.path), {"actions": [123]})
        self.session.pace.block("verification_required")
        result = await self.session.check()
        self.assertEqual(result["login"], "not_checked")
        self.assertTrue(result["needs_manual_action"])
        self.assertEqual(read_json(self.session.pace.path)["blocked"], "verification_required")

    async def test_reused_home_recovers_old_blank_block_but_not_platform_restrictions(self):
        self.session.prepare_tab = AsyncMock()
        self.session.tab = SimpleNamespace(get=AsyncMock())
        self.session.settle = AsyncMock()
        write_json(self.session.pace.path, {"blocked": "blank_or_redirected"})
        await self.session.open_home()
        self.session.tab.get.assert_awaited_once_with("https://www.zhipin.com/")
        for reason in ("verification_required", "login_required", "platform_contact_limit"):
            with self.subTest(reason=reason):
                write_json(self.session.pace.path, {"blocked": reason})
                with self.assertRaisesRegex(Stopped, "manual_check_required"):
                    await self.session.open_home()
                self.assertEqual(read_json(self.session.pace.path)["blocked"], reason)
        self.assertEqual(self.session.tab.get.await_count, 1)

    async def test_home_waits_for_render_and_retains_login_challenge_note(self):
        self.session.prepare_tab = AsyncMock()
        self.session.action = AsyncMock()
        self.session.settle = AsyncMock(side_effect=Stopped('login_required'))
        result = await self.session.open_home()
        self.session.settle.assert_awaited_once_with(timeout=15)
        self.assertEqual(result['navigation_note'], 'login_required')
        self.assertEqual(result['login'], 'not_checked')

    async def test_request_rebinds_live_boss_tab_and_ignores_stale_cached_target(self):
        stale = SimpleNamespace(target=SimpleNamespace(target_id="old"))
        live = SimpleNamespace(target=SimpleNamespace(target_id="new"))
        self.session.tab = stale
        self.session.driver = SimpleNamespace(cdp=SimpleNamespace(target=SimpleNamespace(get_targets=lambda: None)))
        self.session.browser = SimpleNamespace(tabs=[stale, live], connection=SimpleNamespace(send=AsyncMock(return_value=[
            SimpleNamespace(target_id="new", type_="page", url="https://www.zhipin.com/")
        ])))
        await self.session.prepare_tab()
        self.assertIs(self.session.tab, live)
        self.session.browser.connection.send.return_value = []
        with self.assertRaisesRegex(Stopped, "browser_tab_missing"):
            await self.session.prepare_tab()

    async def test_read_timeout_stops_batch_without_marking_any_jobs_failed(self):
        self.session.tab = SimpleNamespace(evaluate=AsyncMock(side_effect=asyncio.TimeoutError))
        self.session.action = AsyncMock()
        with database(self.ws.root) as store:
            self.session.store = store
            store.save_jobs([normalize({"encryptJobId": i}) for i in ("job_a", "job_b")], "batch", "query")
            before = list(store.conn.iterdump())
            with self.assertRaisesRegex(Stopped, "browser_read_timeout"):
                await self.session.details({"ids": ["job_a", "job_b"]})
            self.assertEqual(list(store.conn.iterdump()), before)
        self.session.action.assert_not_awaited()

    async def test_closed_connection_is_not_recorded_as_a_job_failure(self):
        self.session.tab = SimpleNamespace(evaluate=AsyncMock(side_effect=ConnectionResetError))
        with self.assertRaisesRegex(Stopped, "browser_connection_lost"):
            await self.session.read()
        self.assertFalse(self.session.pace.path.exists())

    async def test_close_cancels_request_but_preserves_entire_job_database_and_profile(self):
        with database(self.ws.root) as store:
            store.save_jobs([normalize({"encryptJobId": i}) for i in ("pending_job", "reviewed_job", "external_job")], "batch", "query")
            store.review({"job_id": "reviewed_job", "stage": "list", "decision": "reject", "reasons": ["明确的示例硬条件冲突"],
                          "user_hash": self.ws.user_hash(), "facts_hash": store.job("reviewed_job")["facts_hash"]}, self.ws.user_hash(), 72)
            store.observe_existing_contact("external_job")
            before = list(store.conn.iterdump())
        profile_marker = self.ws.profile / "fixture-login-marker"
        profile_marker.parent.mkdir(parents=True, exist_ok=True)
        profile_marker.write_text("keep fixture", encoding="utf-8")
        started = asyncio.Event()

        async def start():
            await asyncio.sleep(0)
            state = read_json(self.ws.runtime / "state.json")
            write_json(self.ws.runtime / "requests/work.json", {"id": "work", "session": state["session"],
                       "created": time.time(), "action": "details"})

        async def execute(req):
            started.set()
            await asyncio.Event().wait()

        async def close():
            await started.wait()
            return await asyncio.to_thread(close_browser, self.ws)

        self.session.start = AsyncMock(side_effect=start)
        self.session.execute = AsyncMock(side_effect=execute)
        self.session.browser = SimpleNamespace(stop=AsyncMock())
        with patch("bosslib.runtime.BrowserSession", return_value=self.session), patch('boss.process_alive', return_value=False):
            _, result = await asyncio.wait_for(asyncio.gather(serve(self.ws.root), close()), 5)
        self.assertEqual(result["state"], "closed")
        self.session.browser.stop.assert_awaited_once()
        self.assertEqual(read_json(self.ws.runtime / "results/work.json")["reason"], "browser_closed")
        with database(self.ws.root) as store:
            self.assertEqual(list(store.conn.iterdump()), before)
        self.assertEqual(profile_marker.read_text(encoding="utf-8"), "keep fixture")

    def test_short_explicit_login_page_is_detected_but_empty_page_is_not(self):
        self.assertEqual(page_problem({"url": "https://www.zhipin.com/", "text": "扫码登录"}), "login_required")
        self.assertEqual(page_problem({"url": "about:blank", "text": ""}), "blank_or_redirected")


@unittest.skipUnless(os.environ.get("BOSS_BROWSER_FIXTURE") == "1", "set BOSS_BROWSER_FIXTURE=1 for offline Chrome")
class RealBrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_close_and_restart_leave_no_owned_chrome_and_preserve_data(self):
        with tempfile.TemporaryDirectory() as directory:
            initialize(directory)
            ws = Workspace(directory)
            with database(ws.root) as store:
                store.save_jobs([normalize({'encryptJobId': 'fixture_job'})], 'batch', 'query')
                before = list(store.conn.iterdump())
            ws.profile.mkdir(parents=True, exist_ok=True)
            marker = ws.profile / 'fixture-user-marker'
            marker.write_text('preserved', encoding='utf-8')
            try:
                for cycle in range(2):
                    opened = await asyncio.to_thread(open_browser, ws)
                    self.assertEqual(opened['mode'], 'idle', str(opened) + (ws.runtime / 'worker.log').read_text(errors='replace'))
                    processes = await asyncio.to_thread(chrome_processes, ws.profile)
                    if os.name == 'nt':
                        self.assertTrue(processes)
                    closed = await asyncio.to_thread(close_browser, ws)
                    self.assertEqual(closed['browser_exit'], {'method': 'graceful', 'verified': True})
                    self.assertFalse(process_alive(opened['pid']))
                    self.assertEqual(await asyncio.to_thread(chrome_processes, ws.profile), [])
                    prefs = read_json(ws.profile / 'Default/Preferences', {})
                    self.assertEqual(prefs.get('profile', {}).get('exit_type'), 'Normal')
                    self.assertEqual(marker.read_text(encoding='utf-8'), 'preserved')
                    with database(ws.root) as store:
                        self.assertEqual(list(store.conn.iterdump()), before)
            finally:
                if ws.status().get('alive'):
                    await asyncio.to_thread(close_browser, ws)

    async def test_blank_home_and_manually_closed_tab_with_intercepted_pages(self):
        import zendriver
        html = '<html><body><div class="nav-figure">示例账户</div><p>' + '仅用于离线浏览器恢复验证。' * 12 + '</p></body></html>'
        with tempfile.TemporaryDirectory() as directory:
            initialize(directory)
            ws = Workspace(directory)
            browser = await zendriver.start(user_data_dir=str(ws.profile), headless=True)
            session = BrowserSession(ws)
            session.driver, session.browser, session.tab = zendriver, browser, browser.main_tab
            try:
                blank = await session.check()
                self.assertEqual(blank["url"], "about:blank")
                self.assertEqual(blank["login"], "not_checked")
                tab = session.tab

                async def intercept(event):
                    await tab.send(zendriver.cdp.fetch.fulfill_request(event.request_id, 200,
                        response_headers=[zendriver.cdp.fetch.HeaderEntry('Content-Type', 'text/html; charset=utf-8')],
                        body=base64.b64encode(html.encode()).decode()))

                tab.add_handler(zendriver.cdp.fetch.RequestPaused, intercept)
                await tab.send(zendriver.cdp.fetch.enable(patterns=[zendriver.cdp.fetch.RequestPattern(url_pattern='*')]))
                write_json(session.pace.path, {"blocked": "blank_or_redirected"})
                await session.execute({"id": "home", "action": "open_home"})
                await session.settle(timeout=5)
                self.assertEqual((await session.check())["login"], "confirmed")
                extra = await browser.get("about:blank", new_tab=True)
                session.tab = extra
                await extra.close()  # Simulate the user's manual tab close.
                result = await session.execute({"id": "check", "action": "check"})
                self.assertEqual(result["login"], "confirmed")
                self.assertEqual(session.tab.target.target_id, tab.target.target_id)
            finally:
                await browser.stop()


if __name__ == "__main__":
    unittest.main()
