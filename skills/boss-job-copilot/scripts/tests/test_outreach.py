import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boss import parser, run
from bosslib.local import Stopped, Store, Workspace, activity_gate, initialize, normalize
from bosslib.page import READ_PAGE
from bosslib.outreach import DOM, DELIVERY_TIMEOUT, ChatTimeout, Outreach, delivered, send, verify, wait_chat, visible_identity_matches
from bosslib.filters import choose_salary, load_catalog
from bosslib.runtime import session_blocking, recoverable_collection


class OutreachTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        initialize(self.temp.name)
        self.ws = Workspace(self.temp.name)
        self.ws.initial_review_count(1)  # These send fixtures explicitly use a one-job plan.
        (self.ws.root / 'user/PROFILE.md').write_text('示例真实项目证据', encoding='utf-8')
        (self.ws.root / 'user/SEARCH_PLAN.md').write_text('示例范围：应用研发，匹配后沟通', encoding='utf-8')
        self.ws.confirm_search_plan('同意示例搜索方案')
        self.store = Store(self.ws.root / 'jobs.sqlite')
        self.ledger = Outreach(self.ws, self.store)
        self.page = {'url': 'https://www.zhipin.com/job_detail/job_a.html', 'job_detail': '应用研发职责' * 50,
                     'company_info': '示例企业', 'work_address': '示例地址', 'chat_button': '立即沟通',
                     'logged_in': True, 'text': '页面内容' * 40, 'recruiter_active_label': '今日活跃'}
        self.seed('job_a')
        self.session = SimpleNamespace(ws=self.ws, store=self.store, healthy_page=AsyncMock(),
                                       action=AsyncMock(), settle=AsyncMock(return_value=self.page),
                                       browser=SimpleNamespace(tabs=[]))

    async def asyncTearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def seed(self, identifier, decision='shortlist', draft=True):
        self.store.save_jobs([normalize({'encryptJobId': identifier, 'jobName': '示例应用研发'})], 'batch', 'query')
        detail = {k: self.page.get(k) for k in ('url', 'job_detail', 'company_info', 'work_address', 'chat_button', 'recruiter_active_label')}
        detail.update(url=f'https://www.zhipin.com/job_detail/{identifier}.html', problem=None)
        self.store.save_detail(identifier, detail, 'complete')
        review = {'job_id': identifier, 'user_hash': self.ws.user_hash(), 'facts_hash': self.store.job(identifier)['facts_hash'],
                  'decision': decision, 'reasons': ['真实证据支持的判断'], 'evidence': [{'source': 'user/PROFILE.md'}]}
        if draft and decision == 'shortlist':
            review['greeting_draft'] = '你好，我做过相关业务应用项目，希望交流岗位。'
        self.store.review(review, self.ws.user_hash(), 72)

    def preview(self, ids=None):
        return self.ledger.preview(ids or ['job_a'])

    def approve(self, scope='queue'):
        preview = self.preview()
        message = '发送本批这条' if scope == 'batch' else '发送这条并按相同标准处理本轮余下队列'
        auth = self.ledger.authorize(preview['id'], message, scope=scope)
        return {'job_id': 'job_a', 'review_id': self.ledger.ready('job_a')['id'], 'authorization_id': auth['authorization_id']}

    def view(self, job_id='job_a', message=None):
        return {'job_id': job_id, 'recipient': 'recruiter_a', 'contact_job': job_id,
                'editor': True, 'messages': [{'text': message}] if message else []}

    def defer_match(self, label='本月活跃', draft=False):
        self.store.save_detail('job_a', {**self.store.job('job_a')['detail'], 'recruiter_active_label': label}, 'complete')
        data = {'job_id': 'job_a', 'user_hash': self.ws.user_hash(), 'facts_hash': self.store.job('job_a')['facts_hash'],
                'decision': 'shortlist', 'stage': 'detail', 'reasons': ['完整职责符合目标，相关经历支持匹配'],
                'evidence': [{'source': 'user/PROFILE.md'}]}
        if draft:
            data['greeting_draft'] = '历史草稿不能在唤醒后直接发送'
        return self.store.review(data, self.ws.user_hash(), 72)

    def recollect(self, online=True, observed_at=None, **fields):
        item = {'encryptJobId': 'job_a', 'jobName': '示例应用研发', **fields}
        if online is not None:
            item['bossOnline'] = online
        self.store.save_jobs([normalize(item)], 'later_batch', 'query', observed_at=observed_at)

    async def test_preview_has_real_trace_and_no_browser_submission(self):
        for i in range(3):
            self.seed('rejected_' + str(i), decision='reject', draft=False)
        self.seed('job_a')
        ids = ['rejected_0', 'rejected_1', 'rejected_2', 'job_a']
        with patch('boss.request') as request:
            result = run(parser().parse_args(['--workspace', str(self.ws.root), 'outreach', 'preview', '--ids', *ids]))
        request.assert_not_called()
        self.assertEqual([r['decision'] for r in result['items']], ['reject'] * 3 + ['shortlist'])
        self.assertEqual(result['external_actions'], 0)
        self.assertEqual(self.ledger.policy()['mode'], 'preview')

    async def test_send_without_permission_stops_before_browser(self):
        review = self.ledger.ready('job_a')
        with self.assertRaisesRegex(Stopped, 'authorization_required'):
            await send(self.session, {'job_id': 'job_a', 'review_id': review['id'], 'authorization_id': 'made-up'})
        self.session.action.assert_not_awaited()

    async def test_queue_scope_is_explicit_and_saved_permission_survives_document_edits(self):
        self.seed('job_b')
        request = self.approve()
        self.ledger.require_authorization('job_b')
        self.seed('new_job')
        with self.assertRaisesRegex(Stopped, 'outside_authorized'):
            self.ledger.require_authorization('new_job')
        for name in ('USER', 'PROFILE', 'OUTREACH'):
            (self.ws.root / 'user' / (name + '.md')).write_text('用户更新了材料或表达', encoding='utf-8')
        self.assertEqual(self.ledger.policy()['mode'], 'live')
        self.ledger.require_authorization('job_a', request['review_id'])

    async def test_success_verified_and_not_resent_after_recollection(self):
        request = self.approve(scope='batch')
        message = self.ledger.ready('job_a')['payload']['greeting_draft']
        with patch('bosslib.outreach.dom', new=AsyncMock(side_effect=[self.view(), {'clicked': True}, {'filled': True}, {'clicked': True}])) as dom:
            with patch('bosslib.outreach.wait_chat', new=AsyncMock(side_effect=[self.view(), self.view(message=message)])):
                self.assertEqual((await send(self.session, request))['status'], 'sent')
            self.store.save_jobs([self.store.job('job_a')['facts']], 'tomorrow', 'second_query')
            self.assertTrue((await send(self.session, request))['skipped'])
            self.assertEqual(dom.await_count, 4)
        self.assertEqual(self.ledger.queue()['counts']['contacted'], 1)

    async def test_timeout_journal_and_read_only_recovery(self):
        request = self.approve()
        with patch('bosslib.outreach.dom', new=AsyncMock(side_effect=[self.view(), {'clicked': True}])):
            with patch('bosslib.outreach.wait_chat', new=AsyncMock(side_effect=asyncio.TimeoutError)):
                self.assertEqual((await send(self.session, request))['status'], 'uncertain')
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            self.assertTrue((await send(self.session, request))['skipped'])
            dom.assert_not_awaited()
        message = self.ledger.record('job_a')['message']
        with patch('bosslib.outreach.dom', new=AsyncMock(return_value=self.view('wrong', message))):
            with self.assertRaisesRegex(Stopped, 'recorded_chat'):
                await verify(self.session, {'job_id': 'job_a'})
        with patch('bosslib.outreach.dom', new=AsyncMock(return_value=self.view(message=message))) as dom:
            self.assertEqual((await verify(self.session, {'job_id': 'job_a'}))['status'], 'sent')
            dom.assert_awaited_once_with(self.session)

    async def test_changed_live_jd_stops_before_contact(self):
        request = self.approve()
        self.session.settle.return_value = {**self.page, 'job_detail': '改变后的研究职责' * 50}
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            with self.assertRaisesRegex(Stopped, 'job_changed_before_contact'):
                await send(self.session, request)
            dom.assert_not_awaited()
        self.assertEqual(self.ledger.record('job_a')['status'], 'read_failed')

    async def test_delivery_timeout_keeps_click_phase_and_matching_pending_evidence(self):
        request = self.approve()
        message = self.ledger.ready('job_a')['payload']['greeting_draft']
        pending = self.view(message=message)
        pending['messages'][0].update(id='message_1', pending=True, source='startchat_modal', status='发送中')
        with patch('bosslib.outreach.dom', new=AsyncMock(side_effect=[self.view(), {'clicked': True}, {'filled': True}, {'clicked': True}])):
            with patch('bosslib.outreach.wait_chat', new=AsyncMock(side_effect=[self.view(), ChatTimeout(pending)])):
                result = await send(self.session, request)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['reason'], 'outreach_delivery_confirmation_timeout')
        self.assertEqual(result['phase'], 'waiting_delivery')
        self.assertTrue(result['send_attempted'])
        self.assertTrue(result['send_click_acknowledged'])
        self.assertTrue(result['observation']['matching_messages'][0]['pending'])
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            self.assertTrue((await send(self.session, request))['skipped'])
            dom.assert_not_awaited()

    async def test_click_timeout_is_not_reported_as_acknowledged_click(self):
        request = self.approve()
        with patch('bosslib.outreach.dom', new=AsyncMock(side_effect=[self.view(), {'clicked': True}, {'filled': True}, asyncio.TimeoutError()])):
            with patch('bosslib.outreach.wait_chat', new=AsyncMock(return_value=self.view())):
                result = await send(self.session, request)
        self.assertEqual(result['phase'], 'send_attempted')
        self.assertTrue(result['send_attempted'])
        self.assertFalse(result['send_click_acknowledged'])
        self.assertEqual(self.ledger.record('job_a')['status'], 'uncertain')

    async def test_parameterless_chat_verification_requires_visible_identity_and_full_message(self):
        self.store.save_jobs([normalize({'encryptJobId': 'job_a', 'jobName': '示例应用研发',
            'brandName': '示例公司', 'bossName': '示例招聘方'})], 'later', 'query')
        # Re-review changed list facts before creating the real-attempt fixture.
        data = self.store.reviews(self.ws.user_hash())[0]['payload']
        self.store.save_detail('job_a', self.store.job('job_a')['detail'], 'complete')
        data['facts_hash'] = self.store.job('job_a')['facts_hash']
        self.store.review(data, self.ws.user_hash(), 72)
        self.approve()
        review = self.ledger.ready('job_a')
        self.ledger.reserve('job_a', review, 'recruiter_a')
        self.ledger.finish('job_a', 'uncertain', {'phase': 'waiting_delivery'})
        identity = {'company': '示例公司', 'recruiter': '示例招聘方', 'title': '示例应用研发',
                    'selected_company': '示例公司', 'selected_recruiter': '示例招聘方'}
        view = {'url': 'https://www.zhipin.com/web/geek/chat', 'job_id': '', 'recipient': '',
                'identity': identity, 'messages': [{'text': review['payload']['greeting_draft'], 'id': 'message_1'}]}
        for bad in ({**view, 'identity': {**identity, 'recruiter': '别人'}},
                    {**view, 'identity': {**identity, 'selected_company': '另一家'}},
                    {**view, 'recipient': 'conflicting_id'}):
            with patch('bosslib.outreach.dom', new=AsyncMock(return_value=bad)):
                with self.assertRaisesRegex(Stopped, 'recorded_chat'):
                    await verify(self.session, {'job_id': 'job_a'})
        with patch('bosslib.outreach.dom', new=AsyncMock(return_value={**view, 'messages': []})):
            self.assertEqual((await verify(self.session, {'job_id': 'job_a'}))['status'], 'uncertain')
        with patch('bosslib.outreach.dom', new=AsyncMock(return_value=view)) as dom:
            result = await verify(self.session, {'job_id': 'job_a'})
            self.assertEqual(dom.await_count, 1)
            self.assertEqual(len(dom.await_args.args), 1)  # Default read operation, no click/fill/send.
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['previous_attempt']['phase'], 'waiting_delivery')
        self.assertEqual(result['identity_evidence'], 'visible_company_recruiter_and_job')
        self.session.action.assert_not_awaited()

    async def test_continue_button_records_external_contact_without_clicks(self):
        request = self.approve()
        self.session.settle.return_value = {**self.page, 'chat_button': '继续沟通'}
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            result = await send(self.session, request)
            dom.assert_not_awaited()
        self.assertEqual(result['status'], 'contacted_external')
        self.assertEqual(result['external_actions'], 0)
        self.assertEqual(self.ledger.queue()['queues']['contacted'], ['job_a'])
        self.assertEqual(self.store.stats()['outreach']['contacted_external'], 1)
        self.assertTrue(run(parser().parse_args(['--workspace', str(self.ws.root), 'outreach', 'send', '--id', 'job_a']))['skipped'])

    async def test_detail_discovery_persists_external_contact_without_review(self):
        self.store.save_jobs([normalize({'encryptJobId': 'history_job', 'jobName': '历史职位'})], 'b', 'q')
        detail = {**self.page, 'chat_button': '继续沟通'}
        self.store.save_detail('history_job', detail, 'complete')
        record = self.ledger.record('history_job')
        self.assertEqual(record['status'], 'contacted_external')
        self.assertIsNone(record['review_id'])
        self.assertEqual(record['message'], '')
        self.store.save_jobs([normalize({'encryptJobId': 'history_job', 'jobName': '岗位更新'})], 'tomorrow', 'q')
        self.assertNotIn('history_job', [r['id'] for r in self.store.summaries(unreviewed=True, user_hash=self.ws.user_hash())])
        self.assertIn('history_job', self.ledger.queue()['queues']['contacted'])
        # Opening an older database backfills the same cached evidence, without browser work.
        with self.store.conn:
            self.store.conn.execute('DELETE FROM outreach WHERE job_id=?', ('history_job',))
        upgraded = Store(self.ws.root / 'jobs.sqlite')
        try:
            self.assertEqual(upgraded.job('history_job')['outreach_status'], 'contacted_external')
        finally:
            upgraded.conn.close()

    async def test_continue_observation_never_relabels_skill_attempt_or_verified_send(self):
        self.approve()
        review = self.ledger.ready('job_a')
        self.ledger.reserve('job_a', review, 'recruiter_a')
        for status in ('sending', 'uncertain', 'sent'):
            self.ledger.finish('job_a', status, {'proof': 'preserve'})
            original = self.ledger.record('job_a')
            self.store.save_detail('job_a', {**self.page, 'chat_button': '继续沟通'}, 'complete')
            self.assertEqual(self.ledger.record('job_a'), original)
            with self.assertRaisesRegex(Stopped, 'already_attempted'):
                self.ledger.save('job_a', review, 'previewed')

    async def test_missing_button_or_text_elsewhere_does_not_mark_existing_contact(self):
        detail = {**self.page, 'job_detail': '示例职责（含继续沟通字样）' * 50, 'chat_button': None}
        self.store.save_detail('job_a', detail, 'complete')
        self.assertIsNone(self.ledger.record('job_a'))
        reopened = Store(self.ws.root / 'jobs.sqlite')
        try:
            self.assertIsNone(reopened.job('job_a')['outreach_status'])
        finally:
            reopened.conn.close()

    async def test_failed_first_batch_read_does_not_block_later_jobs_or_loop(self):
        self.seed('job_b')
        request = self.approve()
        self.session.settle.return_value = {**self.page, 'job_detail': 'too short'}
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            with self.assertRaisesRegex(Stopped, 'detail_incomplete'):
                await send(self.session, request)
            dom.assert_not_awaited()
        self.assertEqual(self.ledger.queue()['queues']['read_failed'], ['job_a'])
        self.ledger.require_authorization('job_b')
        self.store.reset_reviews(['job_a'], '用户要求重试读取失败岗位')
        self.seed('job_a')
        self.assertIn('job_a', self.ledger.queue()['queues']['draft_ready'])

    async def test_recipient_dedup_is_independent_of_job_id(self):
        self.seed('job_b')
        self.approve()
        first = self.ledger.ready('job_a')
        self.ledger.reserve('job_a', first, 'same_recruiter')
        self.ledger.finish('job_a', 'sent', {'verified': True})
        with self.assertRaisesRegex(Stopped, 'recipient_already_contacted'):
            self.ledger.reserve('job_b', self.ledger.ready('job_b'), 'same_recruiter')
        self.assertIsNone(self.ledger.record('job_b'))

    async def test_later_queue_read_failure_is_recorded_without_a_preview_row(self):
        self.seed('job_b')
        approval = self.approve()
        self.assertIsNone(self.ledger.record('job_b'))
        review = self.ledger.ready('job_b')
        self.session.settle.return_value = {**self.page, 'job_detail': 'too short'}
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            with self.assertRaisesRegex(Stopped, 'detail_incomplete'):
                await send(self.session, {'job_id': 'job_b', 'review_id': review['id'],
                                          'authorization_id': approval['authorization_id']})
            dom.assert_not_awaited()
        self.assertEqual(self.ledger.record('job_b')['status'], 'read_failed')
        self.assertNotIn('job_b', self.ledger.queue()['queues']['draft_ready'])
        self.assertEqual(self.ledger.ready('job_b')['id'], review['id'])

    async def test_cancel_after_contact_preserves_last_durable_attempt(self):
        request = self.approve()
        interrupted = None
        async def cancel_at_contact(session, op='read', **data):
            nonlocal interrupted
            if op == 'read':
                return self.view()
            interrupted = self.ledger.record('job_a')
            raise asyncio.CancelledError
        with patch('bosslib.outreach.dom', new=AsyncMock(side_effect=cancel_at_contact)):
            with self.assertRaises(asyncio.CancelledError):
                await send(self.session, request)
        self.assertEqual(self.ledger.record('job_a'), interrupted)
        self.assertEqual(interrupted['status'], 'sending')
        with patch('bosslib.outreach.dom', new=AsyncMock()) as browser:
            self.assertTrue((await send(self.session, request))['skipped'])
            browser.assert_not_awaited()

    async def test_session_failure_before_contact_does_not_change_job_records(self):
        request = self.approve()
        before = list(self.store.conn.iterdump())
        self.session.healthy_page.side_effect = Stopped('browser_read_timeout')
        with self.assertRaisesRegex(Stopped, 'browser_read_timeout'):
            await send(self.session, request)
        self.assertEqual(list(self.store.conn.iterdump()), before)

    async def test_queue_keeps_old_details_and_simulated_drafts(self):
        self.store.save_jobs([normalize({'encryptJobId': 'old_job'})], 'old_batch', 'old_query')
        self.store.review({'job_id': 'old_job', 'user_hash': self.ws.user_hash(),
                           'facts_hash': self.store.job('old_job')['facts_hash'], 'decision': 'hold',
                           'reasons': ['需补详情'], 'next_action': 'fetch_detail'}, self.ws.user_hash(), 72)
        self.preview()
        queue = self.ledger.queue()
        self.assertEqual(queue['queues']['unreviewed'], ['old_job'])
        self.assertEqual(queue['queues']['draft_ready'], ['job_a'])

    async def test_send_authorization_revoked_locally(self):
        self.approve()
        with patch('boss.request') as request:
            result = run(parser().parse_args(['--workspace', str(self.ws.root), 'outreach', 'simulate']))
        request.assert_not_called()
        self.assertEqual(result['mode'], 'preview')
        self.assertIsNotNone(self.ledger.record('job_a'))

    async def test_salary_omission_is_caught_before_live_collection(self):
        with patch('boss.request') as submit:
            with self.assertRaisesRegex(Stopped, 'salary_filter_required'):
                run(parser().parse_args(['--workspace', str(self.ws.root), 'collect', '--keyword', '示例', '--city', '深圳']))
            submit.assert_not_called()

    async def test_partial_detail_does_not_stop_later_jobs(self):
        from bosslib.runtime import BrowserSession
        self.seed('job_b')
        self.store.save_detail('job_a', {}, 'pending')
        self.store.save_detail('job_b', {}, 'pending')
        s=BrowserSession(self.ws);s.store=self.store
        s.healthy_page=AsyncMock();s.action=AsyncMock()
        s.settle=AsyncMock(side_effect=[{**self.page,'job_detail':'too short'},
                                      {**self.page,'url':'https://www.zhipin.com/job_detail/job_b.html'}])
        result=await s.details({'ids':['job_a','job_b']})
        self.assertEqual([r['state'] for r in result['jobs']],['partial','complete'])
        self.assertEqual(s.action.await_count,2)

    async def test_activity_observation_does_not_rewrite_saved_review(self):
        before = self.store.reviews()
        detail = {**self.store.job('job_a')['detail'], 'recruiter_active_label': '3月内活跃'}
        self.store.save_detail('job_a', detail, 'complete')
        self.assertEqual(self.preview()['items'][0]['result'], 'matched_inactive')
        self.assertEqual(self.store.reviews(), before)
        self.assertIsNone(self.store.activity_hold('job_a'))  # Reporting is read-only.
        self.store.save_detail('job_a', {**detail, 'recruiter_active_label': '在线'}, 'complete')
        self.assertEqual(self.preview()['items'][0]['result'], 'send')

    async def test_live_activity_recheck_defers_without_contact_or_consuming_send_state(self):
        self.seed('job_b')
        request = self.approve()
        self.session.settle.return_value = {**self.page, 'recruiter_active_label': '3月内活跃'}
        with patch('bosslib.outreach.dom', new=AsyncMock()) as dom:
            result = await send(self.session, request)
            dom.assert_not_awaited()
        self.assertEqual(result['status'], 'matched_inactive')
        self.assertEqual(result['external_actions'], 0)
        self.assertEqual(self.ledger.record('job_a')['status'], 'matched_inactive')
        self.ledger.require_authorization('job_b')  # Continue this round after the first became inactive.

    async def test_fresh_unknown_can_contact_but_stale_observation_requires_read(self):
        self.store.conn.execute('UPDATE recruiter_activity SET observed_at=?', (time.time() - 86400,))
        self.store.conn.commit()
        self.assertEqual(self.ledger.queue()['queues']['draft_ready'], ['job_a'])
        self.assertFalse(self.preview()['ready_for_confirmation'])
        self.store.save_detail('job_a', {**self.store.job('job_a')['detail'], 'recruiter_active_label': None}, 'complete')
        self.assertEqual(self.ledger.queue()['queues']['draft_ready'], ['job_a'])
        report = self.preview()
        self.assertTrue(report['ready_for_confirmation'])
        self.assertEqual(report['items'][0]['result'], 'send')
        self.assertEqual(report['items'][0]['recruiter_activity']['state'], 'unknown')
        self.assertIsNone(self.store.activity_hold('job_a'))

    async def test_explicit_activity_policy_is_local_and_preserves_previous_scope(self):
        self.approve()
        with patch('boss.request') as submit:
            read = run(parser().parse_args(['--workspace', str(self.ws.root), 'outreach', 'policy']))
            changed = run(parser().parse_args(['--workspace', str(self.ws.root), 'outreach', 'policy', '--activity', 'any']))
            submit.assert_not_called()
        self.assertEqual(read['recruiter_activity'], 'week')
        self.assertTrue(read['allow_unknown_activity'])
        self.assertEqual(changed['external_actions'], 0)
        self.assertEqual(Workspace(self.ws.root).outreach_activity(), 'any')
        self.assertEqual(self.ledger.policy()['mode'], 'live')

    async def test_refresh_reads_activity_even_with_cached_full_jd(self):
        from bosslib.runtime import BrowserSession
        s = BrowserSession(self.ws); s.store = self.store
        s.healthy_page = AsyncMock(); s.action = AsyncMock()
        s.settle = AsyncMock(return_value={**self.page, 'recruiter_active_label': '刚刚活跃'})
        self.assertEqual((await s.details({'ids': ['job_a']}))['jobs'][0]['state'], 'cached')
        s.action.assert_not_awaited()
        result = await s.details({'ids': ['job_a'], 'refresh': True})
        self.assertEqual(result['jobs'][0]['recruiter_activity']['label'], '刚刚活跃')
        s.action.assert_awaited_once()

    async def test_new_online_collection_preserves_review_and_requires_explicit_reset(self):
        self.defer_match(draft=True)
        before = self.store.reviews()
        self.recollect()
        self.assertEqual(self.store.reviews(), before)
        self.assertEqual(self.store.activity_hold('job_a')['state'], 'matched_inactive')
        self.assertEqual(self.store.summaries(unreviewed=True), [])
        with self.assertRaisesRegex(Stopped, 'explicit_reset'):
            self.ledger.ready('job_a')
        self.store.reset_reviews(['job_a'], '用户要求重审这个岗位')
        self.assertEqual([j['id'] for j in self.store.summaries(unreviewed=True)], ['job_a'])
        self.assertEqual(self.store.job('job_a')['detail_state'], 'pending')
        self.seed('job_a')
        self.assertEqual(self.ledger.queue()['queues']['draft_ready'], ['job_a'])

    async def test_inactive_hold_survives_days_offline_missing_and_list_changes(self):
        self.defer_match()
        with patch('bosslib.local.time.time', return_value=time.time() + 3 * 86400):
            self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])
        self.recollect(False, jobName='已改变的列表标题')
        self.recollect(None, jobName='已改变的列表标题')
        self.store.conn.close()
        self.store = Store(self.ws.root / 'jobs.sqlite')
        self.ledger = Outreach(self.ws, self.store)
        self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])
        self.assertEqual(self.store.summaries(unreviewed=True, user_hash=self.ws.user_hash()), [])
        self.assertEqual(self.store.job('job_a')['facts']['title'], '已改变的列表标题')

    async def test_old_online_observation_cannot_wake_or_manually_refresh_hold(self):
        from bosslib.runtime import BrowserSession
        self.recollect()  # The first online evidence predates the inactive detail review.
        self.defer_match()
        held_at = self.store.activity_hold('job_a')['suspended_at']
        self.recollect(observed_at=held_at - 1)
        self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])
        s = BrowserSession(self.ws); s.store = self.store
        s.healthy_page = AsyncMock(); s.action = AsyncMock()
        result = await s.details({'ids': ['job_a'], 'refresh': True})
        s.action.assert_not_awaited()
        self.assertEqual(result['jobs'][0]['reason'], 'saved_hold_requires_explicit_reset')

    async def test_matching_is_decided_before_activity_and_unknown_is_separate(self):
        self.defer_match(None)
        self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])
        self.assertIsNone(self.store.activity_hold('job_a'))
        self.recollect()
        self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])
        self.page['recruiter_active_label'] = '3月内活跃'
        self.seed('rejected_job', decision='reject', draft=False)
        self.store.save_jobs([normalize({'encryptJobId': 'rejected_job', 'jobName': '示例应用研发', 'bossOnline': True})], 'b2', 'q')
        self.assertIn('rejected_job', self.ledger.queue()['queues']['reviewed'])
        self.assertIsNone(self.store.activity_hold('rejected_job'))

    async def test_unknown_activity_can_follow_authorized_send_flow(self):
        self.page['recruiter_active_label'] = None
        self.seed('job_a')
        request = self.approve()
        message = self.ledger.ready('job_a')['payload']['greeting_draft']
        with patch('bosslib.outreach.dom', new=AsyncMock(side_effect=[self.view(), {'clicked': True}, {'filled': True}, {'clicked': True}])):
            with patch('bosslib.outreach.wait_chat', new=AsyncMock(side_effect=[self.view(), self.view(message=message)])):
                self.assertEqual((await send(self.session, request))['status'], 'sent')
        self.assertEqual(self.ledger.activity('job_a')['state'], 'unknown')
        self.assertIsNone(self.store.activity_hold('job_a'))

    async def test_upgrade_preserves_legacy_reviews_holds_and_send_history(self):
        self.seed('sent_unknown')
        review = next(r for r in self.store.reviews() if r['job_id'] == 'sent_unknown')
        self.ledger.save('sent_unknown', review, 'sent', {'fixture': True})
        with self.store.conn:
            self.store.conn.execute("INSERT INTO activity_holds(job_id,state,review_id,suspended_at) VALUES('job_a','matched_activity_unknown',1,1)")
        before = self.store.reviews()
        self.store.conn.close()
        self.store = Store(self.ws.root / 'jobs.sqlite')
        self.ledger = Outreach(self.ws, self.store)
        self.assertEqual(self.store.reviews(), before)
        self.assertEqual(self.store.activity_hold('job_a')['state'], 'matched_activity_unknown')
        self.assertEqual(self.store.summaries(unreviewed=True), [])
        self.assertEqual(self.ledger.record('sent_unknown')['status'], 'sent')

    async def test_changing_activity_preference_does_not_reopen_historical_holds(self):
        self.defer_match()
        before = self.store.reviews()
        self.ws.outreach_activity('any')
        self.recollect()
        self.assertEqual(self.store.activity_hold('job_a')['state'], 'matched_inactive')
        self.assertEqual(self.store.reviews(), before)
        self.assertEqual(self.store.summaries(unreviewed=True), [])

    async def test_legacy_authorization_metadata_does_not_invalidate_real_permission(self):
        self.approve()
        path = self.ledger.root / 'authorization.json'
        auth = json.loads(path.read_text(encoding='utf-8'))
        auth.update(activity_policy_version=-1, authorization_hash='old-version', user_hash='old-profile')
        path.write_text(json.dumps(auth), encoding='utf-8')
        self.assertEqual(self.ledger.policy()['mode'], 'live')
        self.ledger.require_authorization('job_a')

    async def test_new_full_detail_can_reject_a_reactivated_job(self):
        self.defer_match()
        self.store.reset_reviews(['job_a'], '用户要求重新审查')
        detail = {**self.store.job('job_a')['detail'], 'job_detail': '职责已变成不合适的方向' * 50,
                  'recruiter_active_label': '今日活跃'}
        self.store.save_detail('job_a', detail, 'complete')
        self.store.review({'job_id': 'job_a', 'user_hash': self.ws.user_hash(),
                           'facts_hash': self.store.job('job_a')['facts_hash'], 'decision': 'reject',
                           'stage': 'detail', 'reasons': ['更新后的核心职责与求职方向不符']}, self.ws.user_hash(), 72)
        self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])
        self.recollect()
        self.assertEqual(self.ledger.queue()['queues']['reviewed'], ['job_a'])

    async def test_contact_history_wins_over_activity_and_list_changes(self):
        self.defer_match(draft=True)
        review = self.store.reviews(self.ws.user_hash())[0]
        self.ledger.save('job_a', review, 'sent', {'evidence': 'fixture'})
        self.recollect(jobName='列表字段改变')
        self.assertEqual(self.ledger.queue()['queues']['contacted'], ['job_a'])
        self.assertEqual(self.store.summaries(unreviewed=True, user_hash=self.ws.user_hash()), [])
        self.assertEqual(self.store.activity_hold('job_a')['generation'], 0)

    async def test_offline_list_is_not_evidence_of_inactivity(self):
        self.store.save_jobs([normalize({'encryptJobId': 'job_a', 'jobName': '示例应用研发', 'bossOnline': False}),
                              normalize({'encryptJobId': 'new_offline', 'bossOnline': False})], 'batch2', 'query')
        self.assertEqual(self.store.job('job_a')['recruiter_activity']['label'], '今日活跃')
        self.assertTrue(self.ledger.activity('job_a')['eligible'])
        self.assertIsNone(self.store.job('new_offline')['recruiter_activity'])
        self.assertEqual(self.ledger.activity('new_offline')['state'], 'unchecked')


class PolicyTests(unittest.TestCase):
    def test_activity_labels_and_china_calendar_boundary(self):
        from datetime import datetime
        before_midnight = datetime.fromisoformat('2026-09-21T23:59:00+08:00').timestamp()
        after_midnight = datetime.fromisoformat('2026-09-22T00:01:00+08:00').timestamp()
        for label in ('在线', '刚刚活跃', '今日活跃'):
            observation = {'label': label, 'observed_at': before_midnight}
            self.assertTrue(activity_gate(observation, at=before_midnight + 1)['eligible'])
            self.assertEqual(activity_gate(observation, at=after_midnight)['state'], 'stale')
        for label in ('3日内活跃', '本周活跃', '本月活跃', '3月内活跃', '近半年活跃', '半年前活跃'):
            self.assertEqual(activity_gate({'label': label, 'observed_at': before_midnight}, 'today', at=before_midnight)['state'], 'deferred')
        self.assertEqual(activity_gate(None, at=before_midnight)['state'], 'unchecked')
        self.assertEqual(activity_gate({'label': '招聘中', 'observed_at': before_midnight}, at=before_midnight)['state'], 'unknown')
        self.assertTrue(activity_gate(None, 'any', at=before_midnight)['eligible'])
        self.assertTrue(activity_gate({'label': '3日内活跃', 'observed_at': before_midnight}, '3d', at=before_midnight)['eligible'])
        for label in (None, '', '招聘中', '本周活跃', '7天内活跃', '3日内活跃'):
            self.assertTrue(activity_gate({'label': label, 'observed_at': before_midnight}, at=before_midnight)['eligible'])
        for label in ('本月活跃', '3月内活跃', '近半年活跃'):
            self.assertFalse(activity_gate({'label': label, 'observed_at': before_midnight}, at=before_midnight)['eligible'])
        self.assertFalse(activity_gate(None, at=before_midnight)['eligible'])

    def test_choose_one_band_and_round_up_at_boundaries(self):
        for value, expected in [('35-40', '406'), ('50', '407'), ('45–60K', '407'), ('8', '404')]:
            self.assertEqual(choose_salary(value, load_catalog())['code'], expected)

    def test_session_errors_do_not_include_bad_rows_or_single_page_errors(self):
        for reason in ('invalid_job_id', 'api_schema_changed', 'list_pagination_stalled', 'list_http_500'):
            self.assertTrue(recoverable_collection(reason))
            self.assertFalse(session_blocking(reason))
        for reason in ('verification_required', 'login_required', 'list_http_429', 'list_http_403', 'api_rejected'):
            self.assertFalse(recoverable_collection(reason))
            self.assertTrue(session_blocking(reason))


@unittest.skipUnless(os.getenv('BOSS_BROWSER_FIXTURE') == '1', 'Optional offline Chrome DOM fixture')
class BrowserDOMTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_send_through_parameterless_full_chat_and_identity_changes(self):
        """Observed full-page layout, including recruiter title as the third sidebar span."""
        import zendriver
        html = '''<!doctype html><html><head><style>.text-content{white-space:pre-wrap}</style></head><body>
        <div class="nav-figure">离线示例账户</div><p>离线聊天兼容测试，不访问招聘方。</p>
        <a class="btn-startchat" redirect-url="/web/geek/chat?id=recruiter_a&jobId=job_a"
          onclick="window.contacts=(window.contacts||0)+1;history.pushState({},'', '/web/geek/chat');document.querySelector('#chat').style.display='block';this.remove();setTimeout(()=>{document.querySelector('.position-name').textContent='示例岗位'},200)">立即沟通</a>
        <div id="chat" style="display:none">
        <div class="friend-content selected"><span class="name-box"><span class="name-text">示例招聘方</span><span>示例公司</span><span>招聘专员</span></span></div>
        <div class="chat-conversation"><div class="base-info"><div><span class="name-text">示例招聘方</span></div><span>示例公司</span><span class="base-title">招聘专员</span></div>
        <div class="position-content"><span class="position-name"></span></div><ul class="message-list"></ul>
        <div id="chat-input" contenteditable="true" style="min-height:30px"></div>
        <button class="btn-send" onclick="window.sends=(window.sends||0)+1;const row=document.createElement('li');row.className='message-item item-myself';row.dataset.mid='fixture_sent';row.innerHTML='<div class=text><i class=message-status>发送中</i><p><span class=text-content></span></p></div>';row.querySelector('.text-content').textContent=document.querySelector('#chat-input').innerText;document.querySelector('.message-list').append(row);document.querySelector('#chat-input').innerText='';setTimeout(()=>row.querySelector('.message-status').textContent='送达',200)">发送</button>
        </div></div><div class="item-myself"><div class="text">页面其他区域不属于当前会话</div></div>
        </body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            initialize(directory)
            ws = Workspace(directory)
            ws.initial_review_count(1)
            (ws.root / 'user/PROFILE.md').write_text('示例项目经历', encoding='utf-8')
            (ws.root / 'user/SEARCH_PLAN.md').write_text('已确认的示例方案', encoding='utf-8')
            ws.confirm_search_plan('确认示例方案')
            store = Store(ws.root / 'jobs.sqlite')
            browser = await zendriver.start(user_data_dir=str(ws.profile), headless=True)
            try:
                tab = browser.main_tab
                async def intercept(event):
                    await tab.send(zendriver.cdp.fetch.fulfill_request(event.request_id, 200,
                        response_headers=[zendriver.cdp.fetch.HeaderEntry('Content-Type', 'text/html; charset=utf-8')],
                        body=base64.b64encode(html.encode()).decode()))
                tab.add_handler(zendriver.cdp.fetch.RequestPaused, intercept)
                await tab.send(zendriver.cdp.fetch.enable(patterns=[zendriver.cdp.fetch.RequestPattern(url_pattern='*')]))
                await tab.get('https://www.zhipin.com/job_detail/job_a.html')
                page = {'url': 'https://www.zhipin.com/job_detail/job_a.html', 'job_detail': '示例完整职责' * 50,
                        'company_info': '示例公司', 'work_address': '示例地址', 'chat_button': '立即沟通',
                        'recruiter_active_label': '今日活跃', 'problem': None}
                store.save_jobs([normalize({'encryptJobId': 'job_a', 'jobName': '示例岗位',
                                           'brandName': '示例公司', 'bossName': '示例招聘方'})], 'batch', 'query')
                store.save_detail('job_a', page, 'complete')
                message = '你好，我有相关项目经验，期待交流。\n这是离线测试消息。'
                store.review({'job_id': 'job_a', 'stage': 'detail', 'decision': 'shortlist',
                              'reasons': ['示例职责匹配'], 'greeting_draft': message,
                              'evidence': [{'source': 'user/PROFILE.md'}],
                              'facts_hash': store.job('job_a')['facts_hash'], 'user_hash': ws.user_hash()}, ws.user_hash(), 72)
                ledger = Outreach(ws, store)
                auth = ledger.authorize(ledger.preview(['job_a'])['id'], '发送这条示例招呼')
                async def read_page():
                    return json.loads(await tab.evaluate(READ_PAGE, return_by_value=True))
                session = SimpleNamespace(ws=ws, store=store, browser=browser, tab=tab, actions=0,
                    pace=SimpleNamespace(before=AsyncMock()), read=read_page, healthy_page=AsyncMock(),
                    action=AsyncMock(), settle=AsyncMock(return_value={**page, 'logged_in': True, 'text': '完整示例岗位内容' * 30}))
                request = {'job_id': 'job_a', 'review_id': ledger.ready('job_a')['id'], 'authorization_id': auth['authorization_id']}
                result = await send(session, request)
                self.assertEqual(result['status'], 'sent', result)
                self.assertEqual(result['messages'][0]['id'], 'fixture_sent')
                self.assertEqual(result['messages'][0]['text'], message)
                self.assertEqual(await tab.evaluate('window.contacts'), 1)
                self.assertEqual(await tab.evaluate('window.sends'), 1)
                self.assertTrue((await send(session, request))['skipped'])
                self.assertEqual(await tab.evaluate('window.sends'), 1)
                data = {'job_id': 'job_a', 'recipient': 'recruiter_a', 'message': message,
                        'expected_identity': {'company': '示例公司', 'recruiter': '示例招聘方', 'title': '示例岗位'}}
                async def call(op):
                    return json.loads(await tab.evaluate(DOM+'('+json.dumps({'op': op, **data}, ensure_ascii=False)+')', return_by_value=True))
                # Recheck the recipient on EACH write, including a user switching after fill.
                self.assertTrue((await call('fill'))['filled'])
                for selector in ('.base-info .name-text', '.base-info > span:not(.base-title)',
                                 '.position-name', '.friend-content .name-text', '.name-box > .name-text + span'):
                    with self.subTest(selector=selector):
                        old = await tab.evaluate('document.querySelector('+json.dumps(selector)+').textContent')
                        await tab.evaluate('document.querySelector('+json.dumps(selector)+').textContent="另一个对象"')
                        with self.assertRaises(Exception):
                            await call('fill')
                        with self.assertRaises(Exception):
                            await call('send')
                        await tab.evaluate('document.querySelector('+json.dumps(selector)+').textContent='+json.dumps(old))
                await tab.evaluate('history.pushState({},"","/web/geek/chat?id=wrong&jobId=job_a")')
                with self.assertRaises(Exception):
                    await call('send')
                await tab.evaluate('history.pushState({},"","/web/geek/chat");document.body.append(document.querySelector(".friend-content").cloneNode(true))')
                with self.assertRaises(Exception):
                    await call('send')
                self.assertEqual(await tab.evaluate('window.sends'), 1)
                view = await call('read')
                self.assertEqual(len(view['messages']), 1)  # Excludes unrelated visible messages outside this pane.
            finally:
                store.conn.close()
                await browser.stop()

    async def test_new_parameterless_tab_is_bound_to_contact_not_an_existing_chat(self):
        import zendriver
        html = '''<html><body><div class="nav-figure">示例账户</div><p>离线示例聊天页面。</p>
        <div class="friend-content selected"><span class="name-box"><span class="name-text">示例招聘方</span><span>示例公司</span><span>招聘</span></span></div>
        <div class="chat-conversation"><div class="base-info"><div><span class="name-text">示例招聘方</span></div><span>示例公司</span><span class="base-title">招聘</span></div>
        <div class="position-content"><span class="position-name">示例岗位</span></div><div id="chat-input" contenteditable="true" style="min-height:30px"></div></div></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            browser = await zendriver.start(user_data_dir=directory, headless=True)
            try:
                original = browser.main_tab
                async def new_chat():
                    tab = await browser.get('about:blank', new_tab=True)
                    async def intercept(event):
                        await tab.send(zendriver.cdp.fetch.fulfill_request(event.request_id, 200,
                            response_headers=[zendriver.cdp.fetch.HeaderEntry('Content-Type', 'text/html; charset=utf-8')],
                            body=base64.b64encode(html.encode()).decode()))
                    tab.add_handler(zendriver.cdp.fetch.RequestPaused, intercept)
                    await tab.send(zendriver.cdp.fetch.enable(patterns=[zendriver.cdp.fetch.RequestPattern(url_pattern='*')]))
                    await tab.get('https://www.zhipin.com/web/geek/chat')
                    return tab
                existing = await new_chat()
                old_tabs = {original.target.target_id, existing.target.target_id}
                session = SimpleNamespace(tab=original, browser=browser,
                                          read=AsyncMock(return_value={'url': 'about:blank', 'text': ''}))
                facts = {'company': '示例公司', 'recruiter': '示例招聘方', 'title': '示例岗位'}
                with self.assertRaises(ChatTimeout):
                    await wait_chat(session, 'job_a', 'recruiter_a', timeout=.8, old_tabs=old_tabs, expected_identity=facts)
                created = await new_chat()
                view = await wait_chat(session, 'job_a', 'recruiter_a', timeout=5, old_tabs=old_tabs, expected_identity=facts)
                self.assertEqual(session.tab.target.target_id, created.target.target_id)
                self.assertEqual(view['job_id'], '')
                session.tab = original
                await new_chat()
                with self.assertRaisesRegex(Stopped, 'multiple_matching_chats'):
                    await wait_chat(session, 'job_a', 'recruiter_a', timeout=5, old_tabs=old_tabs, expected_identity=facts)
            finally:
                await browser.stop()

    async def test_full_chat_status_is_not_message_text_and_identity_is_visible(self):
        import zendriver
        html = '''<!doctype html><html><body>
        <div class="friend-content selected"><span class="name-box"><span class="name-text">示例招聘方</span><span>示例公司</span></span></div>
        <div class="chat-conversation"><div class="base-info"><div><span class="name-text">示例招聘方</span></div><span>示例公司</span><span class="base-title">招聘</span></div>
        <div class="position-content"><span class="position-name">示例应用研发</span></div>
        <li class="message-item item-myself" data-mid="known_message"><div class="text"><i class="message-status status-delivery"> 送达 </i><p><span class="text-content">真实正文</span></p></div></li></div>
        </body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            browser = await zendriver.start(user_data_dir=directory, headless=True)
            try:
                tab = browser.main_tab
                async def intercept(event):
                    await tab.send(zendriver.cdp.fetch.fulfill_request(event.request_id, 200,
                        response_headers=[zendriver.cdp.fetch.HeaderEntry('Content-Type', 'text/html; charset=utf-8')],
                        body=base64.b64encode(html.encode()).decode()))
                tab.add_handler(zendriver.cdp.fetch.RequestPaused, intercept)
                await tab.send(zendriver.cdp.fetch.enable(patterns=[zendriver.cdp.fetch.RequestPattern(url_pattern='*')]))
                await tab.get('https://www.zhipin.com/web/geek/chat')
                view = json.loads(await tab.evaluate(DOM+'({op:"read"})', return_by_value=True))
                self.assertEqual(view['job_id'], '')
                self.assertEqual(view['recipient'], '')
                self.assertEqual(delivered(view, '真实正文')[0]['id'], 'known_message')
                self.assertEqual(delivered(view, '送达 真实正文'), [])
                self.assertTrue(visible_identity_matches(view, {'facts': {'company': '示例公司', 'recruiter': '示例招聘方', 'title': '示例应用研发'}}))
                await tab.evaluate('document.querySelector(".message-status").textContent="发送中"')
                pending = json.loads(await tab.evaluate(DOM+'({op:"read"})', return_by_value=True))
                self.assertEqual(delivered(pending, '真实正文'), [])
            finally:
                await browser.stop()

    async def test_real_modal_structure_and_delayed_delivery(self):
        import zendriver
        message = '示例真实结构回归消息，含换行\n第二行。'
        html = '''<!doctype html><html><body>
        <div class="nav-figure">示例登录用户</div><div>离线回归样本，用来验证弹窗消息及发送状态，不访问真实招聘方，不产生任何实际对外消息。</div>
        <a class="btn-startchat" redirect-url="/web/geek/chat?id=recruiter_a&jobId=job_a">继续沟通</a>
        <div class="startchat-dialog"><div class="message"><ul class="message-list">
          <li class="message-item" id="incoming"><p class="text">来自对方的消息</p></li>
          <li class="message-item" id="fixture_message"><span class="status">发送中</span><p class="text"></p></li>
        </ul></div><textarea class="input-area"></textarea></div>
        <div class="startchat-dialog" style="display:none"><ul class="message-list"><li class="message-item"><span class="status success">已发送</span><p class="text"></p></li></ul></div>
        <div class="message-item"><span class="status success">已发送</span><p class="text"></p></div>
        </body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            browser = await zendriver.start(user_data_dir=directory, headless=True)
            try:
                tab = browser.main_tab
                async def intercept(event):
                    await tab.send(zendriver.cdp.fetch.fulfill_request(event.request_id, 200,
                        response_headers=[zendriver.cdp.fetch.HeaderEntry('Content-Type', 'text/html; charset=utf-8')],
                        body=base64.b64encode(html.encode()).decode()))
                tab.add_handler(zendriver.cdp.fetch.RequestPaused, intercept)
                await tab.send(zendriver.cdp.fetch.enable(patterns=[zendriver.cdp.fetch.RequestPattern(url_pattern='*')]))
                await tab.get('https://www.zhipin.com/job_detail/job_a.html')
                await tab.evaluate('document.querySelectorAll("p.text").forEach(e=>e.innerText=' + json.dumps(message) + ')')
                async def read_dom():
                    return json.loads(await tab.evaluate(DOM+'({op:"read"})', return_by_value=True))
                view = await read_dom()
                self.assertEqual(view['surface'], 'startchat_modal')
                self.assertEqual(len(view['messages']), 1)  # No incoming, hidden, or unrelated text.
                self.assertEqual(delivered(view, message), [])
                await tab.evaluate('document.querySelector("#fixture_message .status").textContent="发送失败"')
                self.assertTrue((await read_dom())['messages'][0]['failed'])
                self.assertEqual(delivered(await read_dom(), message), [])
                await tab.evaluate('document.querySelector("#fixture_message .status").textContent="发送中"; setTimeout(()=>{const e=document.querySelector("#fixture_message .status"); e.className="status success";e.textContent="已发送"},16000)')
                async def read_page():
                    return json.loads(await tab.evaluate(READ_PAGE, return_by_value=True))
                session = SimpleNamespace(tab=tab, browser=browser, read=read_page)
                view = await wait_chat(session, 'job_a', 'recruiter_a', timeout=DELIVERY_TIMEOUT, message=message)
                self.assertEqual(delivered(view, message)[0]['id'], 'fixture_message')
                self.assertEqual(delivered(view, message + '不是原文'), [])
                self.assertEqual(view['editor_text'], '')
            finally:
                await browser.stop()

    async def test_online_badge_and_last_active_are_scoped_to_visible_recruiter_card(self):
        import zendriver
        fixtures = [
            ('online', '<span class="boss-online-tag">在线</span>', '在线'),
            ('online_whitespace', '<span class="boss-online-tag"> 在线\n</span>', '在线'),
            ('recent', '<span class="boss-active-time">刚刚活跃</span>', '刚刚活跃'),
            ('inactive', '<span class="boss-active-time">3月内活跃</span>', '3月内活跃'),
            ('online_priority', '<span class="boss-active-time">本周活跃</span><span class="boss-online-tag">在线</span>', '在线'),
            ('hidden_online', '<span class="boss-online-tag" style="display:none">在线</span><span class="boss-active-time">本月活跃</span>', '本月活跃'),
            ('hidden_time', '<span class="boss-active-time" style="display:none">本月活跃</span><span class="boss-online-tag">在线</span>', '在线'),
            ('missing', '<span>招聘者</span>', None),
            ('ordinary_text', '<span>负责在线业务研发招聘</span>', None),
            ('unknown_label', '<span class="boss-online-tag">招聘中</span>', '招聘中'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            browser = await zendriver.start(user_data_dir=directory, headless=True)
            try:
                tab = browser.main_tab
                await tab.get('about:blank')
                for name, card, expected in fixtures:
                    with self.subTest(name=name):
                        html = ('<div class="job-detail-description">JD 提到在线和今日活跃'
                                '<span class="boss-online-tag">在线</span></div>'
                                '<div class="job-boss-info" style="display:none"><span class="boss-online-tag">在线</span></div>'
                                '<div class="job-boss-info"><h2 class="name">示例招聘者' + card + '</h2></div>')
                        await tab.evaluate('document.body.innerHTML = ' + json.dumps(html))
                        page = json.loads(await tab.evaluate(READ_PAGE, return_by_value=True))
                        self.assertEqual(page['recruiter_active_label'], expected)
                        self.assertEqual(activity_gate({'label': page['recruiter_active_label'], 'observed_at': time.time()})['eligible'],
                                         expected in ('在线', '刚刚活跃', None, '招聘中'))
            finally:
                await browser.stop()

    async def test_dom_contact_fill_send_and_wrong_target_guard(self):
        import zendriver
        html = '''<!doctype html><html><body>
        <div class="job-detail-description">JD 中提到今日活跃不能作为招聘方状态</div>
        <div class="job-boss-info"><h2 class="name">示例招聘方<span class="boss-active-time">3月内活跃</span></h2></div>
        <a class="btn-startchat" redirect-url="/web/geek/chat?id=recruiter_a&jobId=job_a"
           onclick="history.pushState({},'',this.getAttribute('redirect-url'));document.getElementById('chat').style.display='block';this.remove()">立即沟通</a>
        <div id="chat" class="chat-conversation" style="display:none"><div id="chat-input" contenteditable="true" style="min-height:30px"></div>
        <button class="btn-send" onclick="const e=document.createElement('div');e.className='item-myself';const t=document.createElement('div');t.className='text';t.innerText=document.getElementById('chat-input').innerText;e.append(t);document.getElementById('chat').append(e);document.getElementById('chat-input').innerText=''">发送</button></div>
        </body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            browser = await zendriver.start(user_data_dir=directory, headless=True)
            try:
                tab = browser.main_tab
                async def intercept(event):
                    await tab.send(zendriver.cdp.fetch.fulfill_request(event.request_id, 200,
                        response_headers=[zendriver.cdp.fetch.HeaderEntry('Content-Type', 'text/html; charset=utf-8')],
                        body=base64.b64encode(html.encode()).decode()))
                tab.add_handler(zendriver.cdp.fetch.RequestPaused, intercept)
                await tab.send(zendriver.cdp.fetch.enable(patterns=[zendriver.cdp.fetch.RequestPattern(url_pattern='*')]))
                await tab.get('https://www.zhipin.com/job_detail/job_a.html')
                page = json.loads(await tab.evaluate(READ_PAGE, return_by_value=True))
                self.assertEqual(page['recruiter_active_label'], '3月内活跃')
                async def call(op, **kw):
                    return json.loads(await tab.evaluate(DOM+'('+json.dumps({'op':op,**kw},ensure_ascii=False)+')',return_by_value=True))
                data={'job_id':'job_a','recipient':'recruiter_a','message':'示例消息，含 "引号" 和换行\n第二行。'}
                self.assertEqual((await call('read'))['recipient'],'recruiter_a')
                with self.assertRaises(Exception):
                    await call('contact',**{**data,'recipient':'wrong'})
                await call('contact',**data)
                self.assertTrue((await call('fill',**data))['filled'])
                with self.assertRaises(Exception):
                    await call('send',**{**data,'message':'被替换的消息'})
                self.assertEqual((await call('read'))['messages'],[])
                await call('send',**data)
                self.assertEqual((await call('read'))['messages'][0]['text'],data['message'])
            finally:
                await browser.stop()


if __name__ == '__main__':
    unittest.main()
