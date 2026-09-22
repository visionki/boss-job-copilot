import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boss import open_browser, parser, request, run
from bosslib.local import FileLock, RUNTIME_VERSION, Stopped, Workspace, database, initialize, normalize, parse_response, read_json, search_url, write_json
from bosslib.page import detail_state, page_problem
from bosslib.runtime import BrowserSession, Pace, serve
from package_skill import FILES, package


def facts(identifier="job_a", title="示例研发岗位"):
    return normalize({"encryptJobId": identifier, "jobName": title, "securityId": "do-not-export"})


class WorkspaceCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.script = Path(__file__).resolve().parents[1] / "boss.py"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, cwd, *args, expected_code=0):
        result = subprocess.run([sys.executable, "-B", str(self.script), *args], cwd=cwd,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_default_workspace_tracks_cwd_and_isolates_directions(self):
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        first_ws, second_ws = first / ".boss-workspace", second / ".boss-workspace"
        self.assertEqual(self.cli(first, "init")["workspace"], str(first_ws))
        (first_ws / "user/USER.md").write_text("目标：方向 A，仅属于本目录", encoding="utf-8")
        with database(first_ws) as store:
            store.save_jobs([facts()], "first_batch", "first_source")

        self.assertEqual(self.cli(second, "init")["workspace"], str(second_ws))
        self.assertEqual(self.cli(first, "stats")["jobs"], 1)
        self.assertEqual(self.cli(second, "stats")["jobs"], 0)
        self.assertNotIn("方向 A", (second_ws / "user/USER.md").read_text(encoding="utf-8"))
        self.assertEqual(Workspace(first_ws).profile, first_ws / "browser-profile")
        self.assertEqual(Workspace(second_ws).profile, second_ws / "browser-profile")
        self.assertEqual(self.cli(second, "doctor")["workspace"], str(second_ws))
        self.assertFalse((self.root / ".boss-workspace").exists())

    def test_missing_local_workspace_does_not_fall_back_to_parent_or_other_project(self):
        initialize(self.root / ".boss-workspace")
        initialize(self.root / "old-project/.boss-workspace")
        fresh = self.root / "fresh-project"
        fresh.mkdir()
        result = self.cli(fresh, "stats", expected_code=2)
        self.assertEqual(result["reason"], "workspace_not_initialized")
        self.assertEqual(list(fresh.iterdir()), [])

    def test_explicit_workspace_is_local_to_cwd_and_remains_selectable_from_elsewhere(self):
        chosen = self.root / "directions/backend/.boss-workspace"
        result = self.cli(self.root, "--workspace", "directions/backend/.boss-workspace", "init")
        self.assertEqual(result["workspace"], str(chosen))
        (chosen / "user/USER.md").write_text("本方向已确认的要求", encoding="utf-8")
        with database(chosen) as store:
            store.save_jobs([facts()], "chosen_batch", "chosen_source")
        another = self.root / "elsewhere"
        another.mkdir()
        self.assertEqual(self.cli(another, "--workspace", str(chosen), "stats")["jobs"], 1)
        self.assertTrue(self.cli(another, "--workspace", str(chosen), "init")["existing"])
        self.assertEqual((chosen / "user/USER.md").read_text(encoding="utf-8"), "本方向已确认的要求")
        self.assertFalse((self.root / ".boss-workspace").exists())
        self.assertEqual(list(another.iterdir()), [])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        initialize(self.temp.name)
        self.ws = Workspace(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare_documents(self):
        (self.ws.root / "user/PROFILE.md").write_text("示例项目证据：本人负责业务系统研发，部署经验待核实。", encoding="utf-8")
        (self.ws.root / "user/SEARCH_PLAN.md").write_text("示例方案：业务应用研发；地区已明确；学历可协商；首批一页。", encoding="utf-8")

    def command(self, *args):
        return run(parser().parse_args(["--workspace", str(self.ws.root), *args]))

    def test_initialization_creates_only_unconfirmed_documents_and_preserves_edits(self):
        for name in ("USER", "PROFILE", "SEARCH_PLAN"):
            self.assertTrue((self.ws.root / "user" / (name + ".md")).is_file())
        self.assertEqual(self.command("plan", "status")["state"], "unconfirmed")
        self.assertFalse((self.ws.root / "user/search-plan-confirmation.json").exists())
        self.prepare_documents()
        before = (self.ws.root / "user/SEARCH_PLAN.md").read_bytes()
        initialize(self.ws.root)
        self.assertEqual((self.ws.root / "user/SEARCH_PLAN.md").read_bytes(), before)
        self.assertEqual(self.ws.search_plan_status()["state"], "unconfirmed")

    def test_confirm_requires_prepared_documents_and_user_message(self):
        with self.assertRaisesRegex(Stopped, "profile_analysis_required"):
            self.command("plan", "confirm", "--user-message", "按这份方案开始")
        (self.ws.root / "user/PROFILE.md").write_text("已整理的示例项目证据", encoding="utf-8")
        with self.assertRaisesRegex(Stopped, "search_plan_document_required"):
            self.command("plan", "confirm", "--user-message", "按这份方案开始")
        self.prepare_documents()
        with self.assertRaisesRegex(Stopped, "user_confirmation_message_required"):
            self.command("plan", "confirm", "--user-message", " ")

    def test_collect_resume_and_details_are_blocked_before_submitting_requests(self):
        self.prepare_documents()
        for command in (("collect", "--keyword", "测试", "--city", "上海"),
                        ("collect", "--resume", "0" * 32), ("details", "--ids", "job_a")):
            with self.subTest(command=command), patch("boss.request") as submit:
                with self.assertRaisesRegex(Stopped, "search_plan_confirmation_required"):
                    self.command(*command)
                submit.assert_not_called()
        with patch("boss.request") as submit:
            self.assertEqual(self.command("collect", "--keyword", "测试", "--city", "上海", "--dry-run")["state"], "preview")
            submit.assert_not_called()
        self.assertFalse((self.ws.root / "user/search-plan-confirmation.json").exists())

    def test_confirmation_is_versioned_persistent_and_unaffected_by_handoff(self):
        self.prepare_documents()
        approved = self.command("plan", "confirm", "--user-message", "方向与宽严准确，按这份方案开始")
        self.assertEqual(approved["state"], "confirmed")
        saved = (self.ws.root / "user/search-plan-confirmation.json").read_bytes()
        (self.ws.root / "user/USER.md").write_text("交接：完成本地准备，待首批列表。", encoding="utf-8")
        self.assertEqual(Workspace(self.ws.root).require_search_plan(), approved)
        self.command("plan", "confirm", "--user-message", "继续同一方案")
        self.assertEqual((self.ws.root / "user/search-plan-confirmation.json").read_bytes(), saved)
        with patch("boss.request", return_value={"state": "submitted"}) as submit:
            self.assertEqual(self.command("collect", "--keyword", "测试", "--city", "上海", "--salary", "不限")["state"], "submitted")
            self.command("details", "--ids", "job_a")
            self.assertEqual(submit.call_count, 2)
        (self.ws.root / "user/SEARCH_PLAN.md").write_text("方向及条件已改变，尚未确认。", encoding="utf-8")
        with patch("boss.request") as submit:
            with self.assertRaisesRegex(Stopped, "search_plan_changed_requires_confirmation"):
                self.command("details", "--ids", "job_a")
            submit.assert_not_called()

    def test_copying_documents_and_confirmation_does_not_authorize_another_workspace(self):
        self.prepare_documents()
        self.ws.confirm_search_plan("同意本方向的具体方案")
        other = self.ws.root / "another_direction"
        initialize(other)
        for name in ("PROFILE.md", "SEARCH_PLAN.md", "search-plan-confirmation.json"):
            (other / "user" / name).write_bytes((self.ws.root / "user" / name).read_bytes())
        with self.assertRaisesRegex(Stopped, "search_plan_changed_requires_confirmation"):
            Workspace(other).require_search_plan()

    def test_requests_capture_the_confirmed_version_and_reject_old_workers(self):
        self.prepare_documents()
        approved = self.ws.confirm_search_plan("按这份方案开始")
        write_json(self.ws.runtime / "state.json", {"heartbeat": time.time(), "mode": "idle", "session": "a", "runtime_version": RUNTIME_VERSION})
        result = request(self.ws, "details", ids=["job_a"])
        pending = self.ws.runtime / "requests" / (result["request_id"] + ".json")
        self.assertEqual(read_json(pending)["search_plan_sha256"], approved["plan_sha256"])
        pending.unlink()
        write_json(self.ws.runtime / "state.json", {"heartbeat": time.time(), "mode": "idle", "session": "a", "runtime_version": 3})
        with self.assertRaisesRegex(Stopped, "runtime_outdated"):
            request(self.ws, "details", ids=["job_a"])

    def test_open_only_remains_independent_of_intake_and_confirmation(self):
        with patch("boss.open_browser", return_value={"reused": True}) as open_window:
            self.assertTrue(self.command("browser", "open")["reused"])
            open_window.assert_called_once()
        self.assertEqual(self.ws.search_plan_status()["state"], "unconfirmed")


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root / "alice")
        self.ws = Workspace(self.root / "alice")

    def tearDown(self):
        self.temp.cleanup()

    def test_init_idempotent_and_two_users_isolated(self):
        initialize(self.root / "bob")
        (self.ws.root / "user/USER.md").write_text("目标：测试方向A；偏好短句", encoding="utf-8")
        (self.ws.root / "user/PROFILE.md").write_text("目标：测试方向A", encoding="utf-8")
        initialize(self.ws.root)
        bob = Workspace(self.root / "bob")
        self.assertNotEqual(self.ws.profile, bob.profile)
        self.assertNotEqual(self.ws.user_hash(), bob.user_hash())
        self.assertIn("方向A", (self.ws.root / "user/USER.md").read_text(encoding="utf-8"))
        with database(self.ws.root) as db:
            db.save_jobs([facts()], "batch_a", "source")
        with database(bob.root) as db:
            self.assertEqual(db.stats()["jobs"], 0)

    def test_cannot_put_user_workspace_inside_skill(self):
        with self.assertRaises(Stopped):
            initialize(Path(__file__).resolve().parents[2] / "private")

    def test_source_resume_change_does_not_requeue_completed_review(self):
        with database(self.ws.root) as db:
            db.save_jobs([facts()], 'b', 'q')
            db.review({'job_id': 'job_a', 'decision': 'reject', 'reasons': ['不符合当时方向']})
            before = db.reviews()
            (self.ws.root / 'user/materials/resume.txt').write_text('new source', encoding='utf-8')
            self.assertEqual(db.reviews(), before)
            self.assertEqual(db.summaries(unreviewed=True), [])

    def test_no_secret_fields_or_personal_default_filters(self):
        item = facts()
        self.assertIsNone(item["salary"])
        self.assertNotIn("securityId", json.dumps(item))
        self.assertNotIn("target_salary", item)
        self.assertNotIn("city", self.ws.settings)

    def test_schema_errors_are_not_empty_results(self):
        for value in ({"code": 1}, {"code": 0, "zpData": {}}, {"code": 0, "zpData": {"jobList": "bad"}}):
            with self.assertRaises(Stopped):
                parse_response(value)
        self.assertEqual(parse_response({"code": 0, "zpData": {"jobList": []}}), [])

    def test_job_dedup_provenance_and_changed_detail(self):
        with database(self.ws.root) as db:
            db.save_jobs([facts()], "first", "query1")
            db.save_detail("job_a", {"job_detail": "真实职责" * 50}, "complete")
            db.save_jobs([facts()], "second", "query2")
            self.assertTrue(db.fresh(db.job("job_a"), 72))
            self.assertEqual(db.stats()["jobs"], 1)
            self.assertEqual(len(db.job("job_a")["sources"]), 2)
            db.save_jobs([facts(title="更新职责")], "third", "query2")
            self.assertFalse(db.fresh(db.job("job_a"), 72))

    def test_daily_recollection_preserves_unchanged_review_and_updates_seen_dates(self):
        user_hash = self.ws.user_hash()
        item = {**facts(), "company": "示例公司", "salary": "20-30K"}
        with database(self.ws.root) as db:
            with patch("bosslib.local.now", return_value="2026-09-19T10:00:00"):
                db.save_jobs([item], "day_1", "query_1")
            db.save_detail("job_a", {"job_detail": "真实职责" * 50}, "complete")
            original = db.job("job_a")
            review = {"job_id": "job_a", "user_hash": user_hash, "facts_hash": original["facts_hash"],
                      "decision": "hold", "reasons": ["待核实工作制"], "next_action": "clarify"}
            db.review(review, user_hash, 72)
            with patch("bosslib.local.now", return_value="2026-09-20T10:00:00"):
                db.save_jobs([item, item], "day_2", "query_1")
                db.save_jobs([item], "day_2", "query_2")
            updated = db.job("job_a")
            self.assertEqual(db.stats()["jobs"], 1)
            self.assertEqual(updated["first_seen"], original["first_seen"])
            self.assertEqual(updated["last_seen"], "2026-09-20T10:00:00")
            self.assertEqual(updated["facts_hash"], original["facts_hash"])
            self.assertEqual(updated["detail"], original["detail"])
            self.assertEqual(len(updated["sources"]), 3)
            self.assertEqual(db.stats()["reviews"], 1)
            self.assertEqual(db.reviews(user_hash)[0]["payload"], {**{k: v for k, v in review.items() if k not in ("user_hash", "facts_hash")}, "stage": "detail"})
            self.assertNotIn("stale", db.reviews(user_hash)[0])
            self.assertEqual(db.summaries(batch="day_2", unreviewed=True, user_hash=user_hash), [])
            # Identical company/title does not mean two different BOSS IDs are the same posting.
            db.save_jobs([{**item, "id": "job_b"}], "day_2", "query_1")
            self.assertEqual(db.stats()["jobs"], 2)
            self.assertEqual([j["id"] for j in db.summaries(batch="day_2", unreviewed=True, user_hash=user_hash)], ["job_b"])

    def test_changed_job_can_finish_list_review_without_refreshing_old_detail(self):
        user_hash = self.ws.user_hash()
        with database(self.ws.root) as db:
            db.save_jobs([facts()], "day_1", "query")
            db.save_detail("job_a", {"job_detail": "旧职责" * 50}, "complete")
            db.review({"job_id": "job_a", "user_hash": user_hash, "facts_hash": db.job("job_a")["facts_hash"],
                       "decision": "hold", "reasons": ["待核实"], "next_action": "clarify"}, user_hash, 72)
            db.save_jobs([facts(title="新的不相关职责")], "day_2", "query")
            changed = db.job("job_a")
            self.assertTrue(changed["detail_stale"])
            self.assertNotIn("stale", db.reviews(user_hash)[0])
            self.assertEqual(db.summaries(unreviewed=True), [])  # Recollection preserves the previous completed decision.
            db.review({"job_id": "job_a", "user_hash": user_hash, "facts_hash": changed["facts_hash"],
                       "decision": "reject", "reasons": ["更新后的列表职责明显不在目标方向内"]}, user_hash, 72)
            self.assertEqual(db.stats()["reviews"], 2)
            self.assertEqual(db.job("job_a")["detail"], changed["detail"])
            self.assertNotIn("stale", db.reviews(user_hash)[0])
            self.assertEqual(db.summaries(unreviewed=True, user_hash=user_hash), [])
            with self.assertRaisesRegex(Stopped, "fresh_shortlisted_detail"):
                db.review({"job_id": "job_a", "user_hash": user_hash, "facts_hash": changed["facts_hash"],
                           "decision": "shortlist", "reasons": ["测试"], "evidence": ["测试材料"],
                           "greeting_draft": "测试草稿"}, user_hash, 72)

    def test_drafts_require_read_detail_and_contact_history_is_preserved(self):
        with database(self.ws.root) as db:
            db.save_jobs([facts()], "b", "q")
            data = {"job_id": "job_a", "decision": "shortlist", "reasons": ["理由"],
                    "evidence": [{"source": "resume"}], "greeting_draft": "你好"}
            with self.assertRaises(Stopped):
                db.review(data)
            db.save_detail("job_a", {"job_detail": "职责" * 80, "recruiter_active_label": "今日活跃"}, "complete")
            self.assertTrue(db.review(data)["greeting_saved"])
            before = db.reviews()
            with db.conn:
                db.conn.execute("UPDATE jobs SET detail_seen=? WHERE id='job_a'", (time.time() - 200 * 86400,))
            self.assertEqual(db.reviews("changed-user-hash"), before)
            self.assertEqual(db.summaries(unreviewed=True), [])
            db.save_detail("job_a", {"job_detail": "职责" * 80, "chat_button": "继续沟通"}, "complete")
            with self.assertRaisesRegex(Stopped, "already_contacted"):
                db.review(data)

    def test_unreviewed_query_skips_completed_decisions_but_keeps_list_candidates(self):
        with database(self.ws.root) as db:
            db.save_jobs([facts("job_a"), facts("job_b"), facts("job_c")], "batch_a", "source")
            db.review({"job_id": "job_a", "decision": "reject", "reasons": ["已判断不合适"]})
            db.review({"job_id": "job_b", "decision": "hold", "stage": "list",
                       "reasons": ["需要读详情"], "next_action": "fetch_detail"})
            self.assertEqual([j["id"] for j in db.summaries(limit=1, unreviewed=True)], ["job_b"])
            db.save_jobs([facts("job_a", title="职责已变")], "batch_a", "source")
            self.assertEqual({j["id"] for j in db.summaries(unreviewed=True, user_hash="changed")},
                             {"job_b", "job_c"})

    def test_all_unreviewed_batches_are_drained_without_repeating_stable_jobs(self):
        user_hash = self.ws.user_hash()
        items = [facts(f"job_{i:03d}") for i in range(65)]
        with database(self.ws.root) as db:
            db.save_jobs(items, "day_1", "query")
            reviewed = []
            for expected_size in (10, 10, 10, 10, 10, 10, 5, 0):
                pending = db.summaries(unreviewed=True)
                self.assertEqual(len(pending), expected_size)
                for item in pending:
                    db.review({"job_id": item["id"], "user_hash": user_hash, "facts_hash": item["facts_hash"],
                               "decision": "reject", "reasons": ["该测试岗位的职责不在目标方向内"]}, user_hash, 72)
                    reviewed.append(item["id"])
            self.assertEqual(len(reviewed), len(set(reviewed)))
            self.assertEqual(set(reviewed), {item["id"] for item in items})
            db.save_jobs(items, "day_2", "query")
            self.assertEqual(db.stats()["jobs"], 65)
            self.assertEqual(db.summaries(batch="day_2", unreviewed=True, user_hash=user_hash), [])

    def test_rate_ledger_spaces_actions_without_hourly_or_daily_quota(self):
        self.ws.profile.mkdir()
        settings = {"interval_seconds": 5, "hourly_actions": 1, "daily_actions": 1}
        pace = Pace(self.ws.profile, settings)
        write_json(pace.path, {"actions": [1000] * 200})
        self.assertEqual(Pace(self.ws.profile, settings).delay(1002), 3)
        self.assertEqual(pace.delay(1005), 0)
        self.assertEqual(pace.delay(1010), 0)

    def test_challenge_latch_survives_restart_and_explicit_clear_has_no_cooldown(self):
        self.ws.profile.mkdir()
        pace = Pace(self.ws.profile, self.ws.rate)
        pace.block("verification_required")
        with self.assertRaisesRegex(Stopped, "manual_check_required"):
            Pace(self.ws.profile, self.ws.rate).delay()
        pace.clear()
        self.assertEqual(pace.delay(), 0)

    def test_effective_rate_ignores_legacy_quota_fields(self):
        self.ws.settings["rate"].update(hourly_actions=1, daily_actions=1)
        self.ws.settings.update(max_batch_actions=1, max_batch_seconds=0.001)
        write_json(self.ws.root / "settings.json", self.ws.settings)
        self.assertEqual(Workspace(self.ws.root).rate, {"interval_seconds": 5})
        args = parser().parse_args(["--workspace", str(self.ws.root), "collect", "--keyword", "测试",
                                   "--city", "深圳", "--interval", "8", "--dry-run"])
        self.assertEqual(run(args)["rate_limits"], {"interval_seconds": 8})

    def test_profile_lock_excludes_second_process_owner(self):
        with FileLock(self.ws.profile / ".collector.lock"):
            with self.assertRaises(Stopped):
                FileLock(self.ws.profile / ".collector.lock")

    def test_existing_browser_open_does_nothing(self):
        write_json(self.ws.runtime / "state.json", {"heartbeat": time.time(), "mode": "idle", "pid": 123, "session": "a"})
        with patch("subprocess.Popen") as launch:
            self.assertTrue(open_browser(self.ws)["reused"])
            launch.assert_not_called()

    def test_windows_launcher_pid_may_differ_from_worker(self):
        state = {"heartbeat": time.time(), "mode": "idle", "pid": 999, "session": "new-session"}
        with patch.object(self.ws, "status", side_effect=[{"alive": False}, state]), patch("subprocess.Popen") as launch:
            launch.return_value.pid = 111
            result = open_browser(self.ws)
        self.assertFalse(result["reused"])
        self.assertEqual(result["pid"], 999)

    def test_requests_are_serial_and_bound_to_session(self):
        write_json(self.ws.runtime / "state.json", {"heartbeat": time.time(), "mode": "idle", "session": "a", "runtime_version": RUNTIME_VERSION})
        submitted = request(self.ws, "check")
        record = read_json(self.ws.runtime / "requests" / (submitted["request_id"] + ".json"))
        self.assertEqual(record["session"], "a")
        with self.assertRaisesRegex(Stopped, "busy"):
            request(self.ws, "collect")

    def test_filter_url_rejects_unobserved_token_fields_and_hosts(self):
        self.assertIn("city=123", search_url("https://www.zhipin.com/web/geek/jobs?city=123&query=example"))
        for url in ("https://elsewhere.test/web/geek/jobs", "https://www.zhipin.com/web/geek/jobs?token=secret",
                    "https://www.zhipin.com/web/geek/jobs?city=1&city=2"):
            with self.assertRaises(Stopped):
                search_url(url)

    def test_share_package_does_not_include_stray_private_files(self):
        fake = self.root / "share"
        for name in FILES:
            path = fake / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public", encoding="utf-8")
        (fake / "resume.pdf").write_text("PRIVATE", encoding="utf-8")
        (fake / "scripts/private-key.txt").write_text("PRIVATE", encoding="utf-8")
        output = self.root / "share.zip"
        package(fake, output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(len(archive.namelist()), len(FILES) + 1)
            self.assertFalse(any(b"PRIVATE" in archive.read(name) for name in archive.namelist()))


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        initialize(self.temp.name)
        self.ws = Workspace(self.temp.name)
        self.session = BrowserSession(self.ws)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_logged_out_stops_before_navigating(self):
        self.session.read = AsyncMock(return_value={"url": "https://www.zhipin.com/", "text": "扫码登录" * 30, "logged_in": False})
        self.session.action = AsyncMock()
        with self.assertRaisesRegex(Stopped, "login_required"):
            await self.session.collect({"keyword": "example", "city": "123"})
        self.session.action.assert_not_awaited()

    async def test_cached_detail_and_invalid_selection_do_not_browse(self):
        with database(self.ws.root) as store:
            self.session.store = store
            store.save_jobs([facts()], "b", "q")
            store.save_detail("job_a", {"job_detail": "职责" * 80}, "complete")
            self.session.action, self.session.read = AsyncMock(), AsyncMock()
            result = await self.session.details({"ids": ["job_a"]})
            self.assertEqual(result["jobs"][0]["state"], "cached")
            with self.assertRaises(Stopped):
                await self.session.details({"ids": ["job_a", "missing"]})
            self.session.action.assert_not_awaited()
            self.session.read.assert_not_awaited()

    async def test_detail_company_only_and_challenge_not_complete(self):
        page = {"url": "https://www.zhipin.com/job_detail/job_a.html", "logged_in": True,
                "text": "公司简介" * 100, "job_detail": "公司简介" * 100, "company_info": "公司简介" * 100}
        self.assertEqual(detail_state(page, "job_a"), "partial")
        page["text"] += "请完成安全验证"
        self.assertEqual(page_problem(page), "verification_required")

    async def test_page_actions_have_no_batch_count_limit(self):
        self.session.actions = 100
        self.session.pace.before = AsyncMock()
        self.session.tab = Mock(get=AsyncMock())
        await self.session.action("https://www.zhipin.com/")
        self.session.pace.before.assert_awaited_once()
        self.session.tab.get.assert_awaited_once_with("https://www.zhipin.com/")
        self.assertEqual(self.session.actions, 101)

    async def test_page_actions_keep_five_seconds_across_pace_instances(self):
        clock = [1000.0]
        async def advance(delay):
            clock[0] += delay
        with patch("bosslib.runtime.time.time", side_effect=lambda: clock[0]), \
                patch("bosslib.runtime.asyncio.sleep", side_effect=advance) as sleep:
            await self.session.pace.before()
            await Pace(self.ws.profile, self.ws.rate).before()
            await self.session.pace.before()
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [5, 5])
        self.assertEqual(read_json(self.session.pace.path)["actions"], [1000.0, 1005.0, 1010.0])

    def ready_page(self, identifier="job_a"):
        return {"url": f"https://www.zhipin.com/job_detail/{identifier}.html", "logged_in": True,
                "text": "真实职责" * 50, "job_detail": "真实职责" * 50, "chat_button": "立即沟通"}

    async def test_ready_list_and_detail_return_without_fixed_loading_sleep(self):
        page = self.ready_page()
        self.session.read = AsyncMock(return_value=page)
        self.session.last_page = 1
        with patch("bosslib.runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await self.session.settle(after_page=0), page)
            self.assertEqual(await self.session.settle(detail_id="job_a"), page)
        sleep.assert_not_awaited()

    async def test_loading_detail_waits_for_complete_content_without_navigating_again(self):
        complete = self.ready_page()
        self.session.read = AsyncMock(side_effect=[{**complete, "text": "", "job_detail": ""},
                                                  {**complete, "job_detail": "加载中"}, complete])
        self.session.action = AsyncMock()
        with patch("bosslib.runtime.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await self.session.settle(detail_id="job_a"), complete)
        self.assertEqual(self.session.read.await_count, 3)
        self.session.action.assert_not_awaited()

    async def test_loading_list_waits_for_the_next_json_page_before_continuing(self):
        self.session.last_page = 0
        self.session.read = AsyncMock(return_value=self.ready_page())
        self.session.action = AsyncMock()
        async def response_arrives(delay):
            self.session.last_page = 1
        with patch("bosslib.runtime.asyncio.sleep", side_effect=response_arrives):
            await self.session.settle(after_page=0)
        self.assertEqual(self.session.read.await_count, 2)
        self.session.action.assert_not_awaited()

    async def test_catalog_waits_for_search_navigation_and_loaded_dictionaries(self):
        search_page = {**self.ready_page(), "url": "https://www.zhipin.com/web/geek/jobs"}
        self.session.read = AsyncMock(side_effect=[self.ready_page(), search_page, search_page, search_page])
        empty = {"cities": [], "positions": [], "industries": [], "conditions": {}, "rules": {}, "region": None}
        complete = {**empty, "cities": [{"code": "101280600", "name": "深圳"}],
                    "positions": [{"code": "100101", "name": "研发"}], "industries": [{"code": "100001", "name": "示例行业"}],
                    "region": {"city": "101280600", "areas": [{"code": "440305", "name": "南山区"}], "subways": []}}
        self.session.tab = Mock(evaluate=AsyncMock(side_effect=[json.dumps(empty), json.dumps(complete)]))
        with patch("bosslib.runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await self.session.catalog(city="101280600")
        self.assertEqual(result["region"]["areas"][0]["name"], "南山区")
        self.assertEqual(self.session.tab.evaluate.await_count, 2)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(self.session.actions, 0)

    async def test_reload_readiness_waits_for_a_new_document_instead_of_old_content(self):
        old_page = {**self.ready_page(), "document_id": 1000}
        new_page = {**self.ready_page(), "document_id": 2000}
        self.session.read = AsyncMock(side_effect=[old_page, new_page])
        with patch("bosslib.runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await self.session.settle(previous_document=1000), new_page)
        self.assertEqual(sleep.await_count, 1)

    async def test_missing_list_response_times_out_without_refresh_or_extra_scroll(self):
        self.session.read = AsyncMock(return_value=self.ready_page())
        self.session.action = AsyncMock()
        for previous, reason in ((0, "no_matching_list_response"), (1, "list_pagination_stalled")):
            self.session.last_page = previous
            with self.assertRaisesRegex(Stopped, reason):
                await self.session.settle(after_page=previous, timeout=0.01)
        self.session.action.assert_not_awaited()

    async def test_more_than_five_details_are_serially_processed_and_deduplicated(self):
        ids = [f"job_{i}" for i in range(8)]
        with database(self.ws.root) as store:
            self.session.store = store
            store.save_jobs([facts(identifier) for identifier in ids], "batch", "query")
            self.session.healthy_page = AsyncMock()
            self.session.action = AsyncMock()
            self.session.settle = AsyncMock(side_effect=lambda detail_id: self.ready_page(detail_id))
            result = await self.session.details({"ids": ids + [ids[0]]})
            self.assertEqual([j["id"] for j in result["jobs"]], ids)
            self.assertTrue(all(j["state"] == "complete" for j in result["jobs"]))
            self.assertEqual(self.session.action.await_count, 8)

    async def test_verification_stops_immediately_and_requires_a_healthy_manual_check(self):
        challenge = {**self.ready_page(), "text": "请完成安全验证" * 30}
        self.session.read = AsyncMock(return_value=challenge)
        with self.assertRaisesRegex(Stopped, "verification_required"):
            await self.session.settle(detail_id="job_a")
        self.assertEqual((await self.session.check())["login"], "verification_required")
        with self.assertRaisesRegex(Stopped, "manual_check_required"):
            self.session.pace.delay()
        self.session.read.return_value = self.ready_page()
        self.assertEqual((await self.session.check())["login"], "confirmed")
        self.assertEqual(self.session.pace.delay(), 0)

    async def test_worker_ignores_legacy_batch_deadline_and_saves_completed_result(self):
        self.ws.settings.update(max_batch_seconds=0.001, max_batch_actions=1)
        write_json(self.ws.root / "settings.json", self.ws.settings)
        async def start():
            await asyncio.sleep(0)
            state = read_json(self.ws.runtime / "state.json")
            write_json(self.ws.runtime / "requests/work.json", {
                "id": "work", "session": state["session"], "created": time.time(), "action": "details"})
        async def execute(request):
            await asyncio.sleep(0.02)
            (self.ws.runtime / "close").touch()
            return {"finished": True}
        self.session.start = AsyncMock(side_effect=start)
        self.session.execute = AsyncMock(side_effect=execute)
        with patch("bosslib.runtime.BrowserSession", return_value=self.session):
            await asyncio.wait_for(serve(self.ws.root), 3)
        result = read_json(self.ws.runtime / "results/work.json")
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["result"]["finished"])

    async def test_worker_without_deadline_still_honors_user_pause(self):
        started = asyncio.Event()
        async def start():
            await asyncio.sleep(0)
            state = read_json(self.ws.runtime / "state.json")
            write_json(self.ws.runtime / "requests/work.json", {
                "id": "work", "session": state["session"], "created": time.time(), "action": "details"})
        async def execute(request):
            started.set()
            await asyncio.Event().wait()
        async def pause_then_close():
            await started.wait()
            (self.ws.runtime / "pause").touch()
            while not (self.ws.runtime / "results/work.json").exists():
                await asyncio.sleep(0.01)
            (self.ws.runtime / "close").touch()
        self.session.start = AsyncMock(side_effect=start)
        self.session.execute = AsyncMock(side_effect=execute)
        with patch("bosslib.runtime.BrowserSession", return_value=self.session):
            await asyncio.wait_for(asyncio.gather(serve(self.ws.root), pause_then_close()), 3)
        result = read_json(self.ws.runtime / "results/work.json")
        self.assertEqual((result["state"], result["reason"]), ("stopped", "user_paused"))

    async def test_worker_rechecks_confirmation_before_collection_or_details(self):
        self.session.collect, self.session.details = AsyncMock(), AsyncMock()
        for action in ("collect", "details"):
            with self.assertRaisesRegex(Stopped, "search_plan_confirmation_required"):
                await self.session.execute({"id": "queued", "action": action})
        (self.ws.root / "user/PROFILE.md").write_text("已整理示例背景证据", encoding="utf-8")
        plan = self.ws.root / "user/SEARCH_PLAN.md"
        plan.write_text("首版已明确的搜索范围", encoding="utf-8")
        first = self.ws.confirm_search_plan("按首版方案开始")
        plan.write_text("更改后的搜索范围", encoding="utf-8")
        self.ws.confirm_search_plan("按修改后的方案开始")
        with self.assertRaisesRegex(Stopped, "search_plan_request_version_changed"):
            await self.session.execute({"id": "queued", "action": "collect", "search_plan_sha256": first["plan_sha256"]})
        self.session.collect.assert_not_awaited()
        self.session.details.assert_not_awaited()

    async def test_open_only_start_does_not_read_or_listen(self):
        browser = Mock()
        browser.main_tab = Mock()
        with patch("zendriver.start", new=AsyncMock(return_value=browser)), \
                patch("bosslib.runtime.check_profile_available"), patch("bosslib.runtime.make_driver_config"):
            await self.session.start()
        browser.main_tab.evaluate.assert_not_called()
        browser.main_tab.add_handler.assert_not_called()
        browser.main_tab.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
