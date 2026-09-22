import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boss import parser, run
from bosslib.local import Store, Workspace, Stopped, digest, initialize, normalize, now, write_json
from bosslib.outreach import Outreach


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        initialize(self.temp.name)
        self.ws = Workspace(self.temp.name)
        (self.ws.root / 'user/PROFILE.md').write_text('本人有设计与作品交付经验。', encoding='utf-8')
        (self.ws.root / 'user/SEARCH_PLAN.md').write_text('设计岗位，首批十个校准，后续每组二十个。', encoding='utf-8')
        self.ws.confirm_search_plan('认可这份搜索方案')
        self.store = Store(self.ws.root / 'jobs.sqlite')
        self.ledger = Outreach(self.ws, self.store)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def cli(self, *args):
        return run(parser().parse_args(['--workspace', str(self.ws.root), *args]))

    def seed(self, ids, detail=True):
        self.store.save_jobs([normalize({'encryptJobId': i, 'jobName': '设计师，职责待Agent判断'}) for i in ids], 'sample', 'query')
        if detail:
            for i in ids:
                self.store.save_detail(i, {'job_detail': '完整设计职责与作品交付要求。' * 30,
                                          'chat_button': '立即沟通', 'recruiter_active_label': '今日活跃'}, 'complete')

    def decision(self, job_id, match=False):
        data = {'job_id': job_id, 'decision': 'shortlist' if match else 'reject', 'stage': 'detail',
                'reasons': ['本人交付经历与岗位设计职责相关' if match else '已读职责与确认方向不符'],
                'evidence': [{'source': 'user/PROFILE.md', 'claim': '作品交付'}, {'source': 'job.detail', 'claim': '设计职责'}]}
        if match:
            data['greeting_draft'] = '你好，我负责过设计交付，与贵司设计职责相关，欢迎看看我的作品。'
        return data

    def submit(self, items):
        path = self.ws.root / 'outputs/reviews.json'
        write_json(path, items)
        return self.cli('review', '--file', str(path))

    def test_default_twenty_partial_save_and_new_session_need_no_batch_state(self):
        self.seed([f'j{i:02}' for i in range(45)])
        first = self.cli('jobs', '--unreviewed')['jobs']
        self.assertEqual(len(first), 20)
        self.assertEqual(len(self.cli('jobs', '--unreviewed', '--limit', '10')['jobs']), 10)
        self.assertEqual(len(self.cli('jobs', '--unreviewed', '--limit', '35')['jobs']), 35)
        rows = [self.decision(i['id']) for i in first[:6]] + [{'job_id': first[6]['id'], 'decision': 'bad'}]
        result = self.submit(rows)
        self.assertEqual((result['saved'], result['failed']), (6, 1))
        self.assertNotIn('workflow', result)
        self.store.conn.close()
        self.store = Store(self.ws.root / 'jobs.sqlite')
        next_group = self.cli('jobs', '--unreviewed')['jobs']
        self.assertEqual(len(next_group), 20)
        self.assertTrue(set(i['id'] for i in first[:6]).isdisjoint(i['id'] for i in next_group))
        self.assertIn(first[6]['id'], [i['id'] for i in next_group])
        self.assertEqual(list((self.ws.runtime / 'review-packets').glob('*')), [])
        self.assertFalse((self.ws.runtime / 'outreach/current-batch.json').exists())

    def test_review_view_keeps_location_and_full_text_without_semantic_selection_or_writes(self):
        self.store.save_jobs([normalize({'encryptJobId': 'design', 'jobName': '设计师', 'skills': [],
                                        'cityName': '工作城市甲', 'areaDistrict': '工作区域',
                                        'salaryDesc': '18-26K', 'brandScaleName': '100-499人',
                                        'jobExperience': '3-5年', 'jobDegree': '不限'}),
                              normalize({'encryptJobId': 'unknown', 'jobName': '专员'})], 'sample', 'query')
        full_text = '主要负责方案设计与作品交付。\n' * 1000 + '最后一项职责不能被裁掉。'
        detail = {'job_detail': full_text, 'work_address': '工作城市乙 | 实际办公地址',
                  'company_info': '总部位于城市丙，与岗位工作地分别记录。',
                  'chat_button': '立即沟通', 'recruiter_active_label': '在线'}
        self.store.save_detail('design', detail, 'complete')
        before = self.store.stats()
        selected = self.cli('jobs', '--unreviewed')['jobs']
        rows = self.cli('jobs', '--unreviewed', '--view', 'review')['jobs']
        self.assertEqual([r['id'] for r in rows], [r['id'] for r in selected])
        row = next(r for r in rows if r['id'] == 'design')
        original = self.store.job('design')
        self.assertEqual(row['facts'], original['facts'])
        self.assertEqual(row['facts']['city'], '工作城市甲')
        self.assertEqual(row['detail'], {k: v for k, v in detail.items() if k != 'recruiter_active_label'})
        self.assertEqual(row['detail']['job_detail'], full_text)
        self.assertEqual(row['recruiter_activity'], original['recruiter_activity'])
        self.assertEqual(row['recruiter_activity']['label'], '在线')
        self.assertNotIn('sources', row)
        self.assertIsNone(next(r for r in rows if r['id'] == 'unknown')['detail'])
        self.assertEqual(self.cli('jobs', '--id', 'design', '--view', 'review')['jobs'][0], row)
        self.assertIn('sources', self.cli('jobs', '--id', 'design', '--view', 'raw')['jobs'][0])
        out = self.ws.root / 'tmp/read.jsonl'
        self.cli('jobs', '--unreviewed', '--view', 'review', '--out', str(out))
        self.assertEqual([json.loads(line) for line in out.read_text(encoding='utf-8').splitlines()], rows)
        self.assertEqual(self.store.stats(), before)
        self.assertEqual(self.store.reviews(), [])

    def test_preview_exposes_work_location_separately_from_company_location(self):
        self.store.save_jobs([normalize({'encryptJobId': 'location', 'jobName': '设计师',
                                        'cityName': '工作城市甲', 'areaDistrict': '区域甲',
                                        'brandName': '示例公司', 'brandScaleName': '100-499人',
                                        'salaryDesc': '18-26K'})], 'sample', 'query')
        self.store.save_detail('location', {'job_detail': '设计交付职责。' * 30,
                                           'work_address': '工作城市乙 | 某办公楼',
                                           'company_info': '总部城市丙', 'chat_button': '立即沟通',
                                           'recruiter_active_label': '今日活跃'}, 'complete')
        self.submit([{'job_id': 'location', 'stage': 'detail', 'decision': 'hold',
                      'reasons': ['列表与详情工作地不一致，待核实'],
                      'unknowns': ['实际常驻地点']}])
        out = self.ws.root / 'outputs/location.md'
        preview = self.ledger.preview(['location'], out=str(out))
        self.assertEqual(preview['items'][0]['facts']['city'], '工作城市甲')
        self.assertEqual(preview['items'][0]['work_address'], '工作城市乙 | 某办公楼')
        report = out.read_text(encoding='utf-8')
        self.assertIn('工作城市甲 / 区域甲', report)
        self.assertIn('工作城市乙 &#124; 某办公楼', report)
        self.assertIn('100-499人', report)
        self.assertNotIn('总部城市丙', report)
        self.assertEqual(preview['counts']['held'], 1)

    def test_cli_partial_failure_is_nonzero_and_only_failed_items_need_resubmission(self):
        self.seed(['good', 'missing_decision', 'bad_stage'])
        path = self.ws.root / 'tmp/reviews.json'
        write_json(path, [self.decision('good'),
                          {'job_id': 'missing_decision', 'stage': 'hold', 'reasons': ['等待核实']},
                          {**self.decision('bad_stage'), 'stage': 'hold'}])
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'boss.py'),
                   '--workspace', str(self.ws.root), 'review', '--file', str(path)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload['state'], payload['saved'], payload['failed']), ('partial', 1, 2))
        self.assertEqual(payload['failed_ids'], ['missing_decision', 'bad_stage'])
        self.assertTrue(all('stage must be list/detail' in i['hint'] for i in payload['items'][1:]))
        self.assertEqual({r['id'] for r in self.cli('jobs', '--unreviewed')['jobs']}, set(payload['failed_ids']))
        good_review = self.store.reviews()[0]
        write_json(path, [self.decision(i) for i in payload['failed_ids']])
        retried = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(retried.stdout)['state'], 'saved')
        self.assertEqual(self.store.stats()['reviews'], 3)
        self.assertIn(good_review, self.store.reviews())

    def test_calibration_adds_ten_until_one_contactable_and_shows_all_results(self):
        ids = [f'j{i:02}' for i in range(30)]
        self.seed(ids)
        self.assertEqual(self.ws.initial_review_count(), 10)
        for offset in (0, 10, 20):
            self.submit([self.decision(i, match=i == 'j24') for i in ids[offset:offset + 10]])
            report = self.ledger.preview(ids[:offset + 10])
            self.assertEqual(report['counts']['detail_reviewed'], offset + 10)
            self.assertEqual(report['ready_for_confirmation'], offset == 20)
            self.assertEqual(report['target'], offset + 20 if offset < 20 else 30)
            self.assertEqual(len(report['items']), offset + 10)
        self.assertNotIn('report_file', report)
        self.assertEqual(list((self.ws.root / 'outputs').glob('*.md')), [])
        self.assertEqual(report['items'][24]['greeting_draft'], self.decision('j24', True)['greeting_draft'])
        self.assertEqual(sum(i['result'] == 'reject' for i in report['items']), 29)
        saved = json.loads((self.ledger.root / 'previews' / (report['id'] + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(saved, report)
        self.ledger.authorize(report['id'], '发送本批并继续本轮', 'queue')

    def test_preview_exports_markdown_only_when_requested_and_preserves_authorization(self):
        self.ws.initial_review_count(1)
        self.seed(['one'])
        self.submit([self.decision('one', True)])
        before = self.store.stats()
        chat = self.cli('outreach', 'preview', '--ids', 'one')
        self.assertNotIn('report_file', chat)
        self.assertEqual(list((self.ws.root / 'outputs').glob('*.md')), [])
        out = self.ws.root / 'outputs' / 'requested' / 'preview.md'
        exported = self.cli('outreach', 'preview', '--ids', 'one', '--out', str(out))
        self.assertEqual(Path(exported['report_file']), out)
        self.assertIn(self.decision('one', True)['greeting_draft'], out.read_text(encoding='utf-8'))
        self.assertEqual(exported['items'], chat['items'])
        self.assertEqual(exported['signature'], chat['signature'])
        self.assertEqual(self.store.stats(), before)
        self.ledger.authorize(exported['id'], '发送这个已展示的招呼', 'batch')
        self.ledger.require_authorization('one')

    def test_list_rejections_stay_without_details_while_new_candidates_fill_calibration(self):
        rejected = ['low1', 'low2', 'low3']
        candidates = [f'j{i:02}' for i in range(10)]
        self.seed(rejected, detail=False)
        self.seed(candidates)
        self.submit([{'job_id': i, 'stage': 'list', 'decision': 'reject',
                      'reasons': ['列表同口径薪资上限低于用户明确底线']} for i in rejected])
        self.submit([self.decision(i, match=i == 'j00') for i in candidates[:7]])
        first = self.ledger.preview(rejected + candidates[:7])
        self.assertEqual(first['counts']['additional_reviews'], 3)
        self.assertEqual({i['id'] for i in self.cli('jobs', '--unreviewed', '--limit', '3')['jobs']}, set(candidates[7:]))
        self.submit([self.decision(i) for i in candidates[7:]])
        report = self.ledger.preview(rejected + candidates)
        self.assertTrue(report['ready_for_confirmation'])
        self.assertEqual((report['counts']['selected'], report['counts']['detail_reviewed'], report['counts']['list_rejected']), (13, 10, 3))
        self.assertTrue(all(self.store.job(i)['detail'] is None for i in rejected))
        self.ledger.authorize(report['id'], '发送已展示的合适岗位', 'batch')

    def test_early_calibration_preserves_open_questions_and_user_feedback_updates_only_those_jobs(self):
        ids = [f'j{i:02}' for i in range(10)]
        self.seed(ids)
        question = '主要做应用但兼顾内部系统维护，用户是否接受这种混合职责？'
        holds = [{'job_id': i, 'stage': 'detail', 'decision': 'hold',
                  'reasons': ['主要职责与经历相关，待用户校准工作边界'], 'unknowns': [question]} for i in ids[:2]]
        self.submit(holds)
        early = self.cli('outreach', 'preview', '--ids', *ids[:2])
        self.assertEqual(early['counts']['held'], 2)
        self.assertFalse(early['ready_for_confirmation'])
        self.assertEqual(early['items'][0]['unknowns'], [question])
        self.assertEqual({i['result'] for i in early['items']}, {'reserve'})
        self.assertFalse(any(i['greeting_draft'] for i in early['items']))
        with self.assertRaisesRegex(Stopped, 'calibration_incomplete'):
            self.ledger.authorize(early['id'], '先看这些结果', 'batch')
        self.submit([self.decision(i) for i in ids[2:]])
        before = {r['job_id']: r for r in self.store.reviews()}
        approved = self.decision(ids[0], True)
        approved['reasons'].append('用户确认接受这个岗位中的内部系统维护职责')
        self.submit([approved])
        after = {r['job_id']: r for r in self.store.reviews()}
        self.assertTrue(all(before[i] == after[i] for i in ids[1:]))
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM reviews WHERE job_id=?', (ids[0],)).fetchone()[0], 2)
        preview = self.ledger.preview(ids)
        self.assertTrue(preview['ready_for_confirmation'])
        self.assertEqual(preview['counts']['held'], 1)
        authorization = self.ledger.authorize(preview['id'], '只发送这个已确认岗位，其余待定', 'batch')
        self.assertEqual(authorization['initial_jobs'], [ids[0]])
        with patch('boss.request') as request:
            with self.assertRaisesRegex(Stopped, 'shortlisted_draft'):
                self.cli('outreach', 'send', '--id', ids[1])
            request.assert_not_called()

    def test_at_least_one_in_first_ten_is_enough_and_following_group_can_send(self):
        ids = [f'j{i:02}' for i in range(20)]
        self.seed(ids)
        self.submit([self.decision(i, match=i == 'j00') for i in ids[:10]])
        report = self.ledger.preview(ids[:10])
        self.assertTrue(report['ready_for_confirmation'])
        self.assertEqual(report['target'], 10)
        self.ledger.authorize(report['id'], '发送并按同样要求继续本轮岗位', 'queue')
        self.submit([self.decision(i, match=i == 'j10') for i in ids[10:]])
        self.ledger.require_authorization('j10')  # No persisted first-batch gate.
        self.assertEqual({r['id'] for r in self.cli('jobs', '--unsent')['jobs']}, {'j00', 'j10'})

    def test_fetched_details_are_not_counted_as_reviews_and_failure_is_not_rejection(self):
        ids = [f'j{i:02}' for i in range(10)]
        self.seed(ids)
        self.assertEqual(self.ledger.preview(ids)['counts']['detail_reviewed'], 0)
        self.store.save_detail('j09', {'problem': 'timeout'}, 'failed')
        self.submit([self.decision(i, match=i == 'j00') for i in ids[:9]])
        report = self.ledger.preview(ids)
        self.assertFalse(report['ready_for_confirmation'])
        self.assertEqual(report['counts']['read_failed'], 1)
        self.assertEqual(self.cli('jobs', '--unreviewed')['jobs'], [])
        partial = self.ledger.preview(ids, '当前队列只剩一条读取失败，已无其他候选')
        self.assertTrue(partial['ready_for_confirmation'])
        empty = self.ledger.preview(['j09'], '读取失败')
        self.assertFalse(empty['ready_for_confirmation'])

    def test_historical_reviews_survive_two_hundred_days_edits_and_recollection(self):
        self.seed(['done', 'pending'])
        self.submit([self.decision('done')])
        before = self.store.reviews()
        for name in ('PROFILE', 'OUTREACH', 'SEARCH_PLAN', 'USER'):
            (self.ws.root / 'user' / (name + '.md')).write_text('用户已更新此文档。', encoding='utf-8')
        self.store.save_jobs([normalize({'encryptJobId': 'done', 'jobName': '新的标题', 'bossOnline': True})], 'later', 'q')
        with patch('bosslib.local.time.time', return_value=time.time() + 200 * 86400):
            self.assertEqual(self.store.reviews(), before)
            self.assertEqual([j['id'] for j in self.cli('jobs', '--unreviewed')['jobs']], ['pending'])
        self.assertNotIn('stale', self.store.reviews()[0])

    def test_explicit_reset_preserves_history_and_does_not_reopen_sent_or_uncertain(self):
        self.seed(['done', 'failed', 'sent', 'uncertain'])
        self.submit([self.decision('done'), self.decision('sent', True), self.decision('uncertain', True)])
        self.store.save_detail('failed', {'problem': 'timeout'}, 'failed')
        reviews = {r['job_id']: r for r in self.store.reviews()}
        for name in ('sent', 'uncertain'):
            self.ledger.save(name, reviews[name], name)
        before = self.store.stats()['reviews']
        result = self.cli('review-reset', '--ids', 'done', 'failed', 'sent', 'uncertain', '--reason', '用户要求重审这些岗位')
        self.assertEqual(sum(i['reset'] for i in result['items']), 2)
        self.assertEqual(self.store.stats()['reviews'], before + 2)
        self.assertEqual({j['id'] for j in self.cli('jobs', '--unreviewed')['jobs']}, {'done', 'failed'})
        self.assertEqual(self.ledger.record('sent')['status'], 'sent')
        self.assertEqual(self.ledger.record('uncertain')['status'], 'uncertain')

    def test_batch_authorization_binds_messages_queue_does_not_include_new_jobs(self):
        self.ws.initial_review_count(1)
        self.seed(['one'])
        self.submit([self.decision('one', True)])
        report = self.ledger.preview(['one'])
        self.ledger.authorize(report['id'], '只发送这一条', 'batch')
        edit = self.decision('one', True)
        edit['greeting_draft'] += '新加内容。'
        self.submit([edit])
        with self.assertRaisesRegex(Stopped, 'preview_changed'):
            self.ledger.require_authorization('one')
        updated = self.ledger.preview(['one'])
        self.ledger.authorize(updated['id'], '发送更新稿并继续当前库', 'queue')
        self.seed(['outside'])
        self.submit([self.decision('outside', True)])
        with patch('boss.request') as request:
            with self.assertRaisesRegex(Stopped, 'outside_authorized'):
                self.cli('outreach', 'send', '--id', 'outside')
            request.assert_not_called()

    def test_batch_full_records_and_jsonl_submission_do_not_generate_documents(self):
        self.seed(['a', 'b'])
        rows = self.cli('jobs', '--ids', 'a', 'b')['jobs']
        self.assertTrue(all(r['detail']['job_detail'] for r in rows))
        path = self.ws.root / 'outputs/reviews.jsonl'
        path.write_text('\n'.join(json.dumps(self.decision(i)) for i in ('a', 'b')), encoding='utf-8')
        self.assertEqual(self.cli('review', '--file', str(path))['saved'], 2)
        self.assertEqual(list((self.ws.root / 'outputs').glob('*.md')), [])
        out = self.ws.root / 'outputs/summary.md'
        self.assertEqual(self.cli('report', '--out', str(out))['reviewed'], 2)
        self.assertTrue(out.is_file())

    def test_previously_reactivated_review_remains_usable_after_upgrade(self):
        self.ws.initial_review_count(1)
        self.seed(['legacy'])
        job = self.store.job('legacy')
        old_hash = digest([job['facts'], job['detail'], job['detail_state'], {'activity_recheck_generation': 2}])
        with self.store.conn:
            self.store.conn.execute("INSERT INTO activity_holds(job_id,state,generation) VALUES('legacy','cleared',2)")
            self.store.conn.execute('INSERT INTO reviews(job_id,created_at,user_hash,facts_hash,payload) VALUES(?,?,?,?,?)',
                                    ('legacy', now(), 'old_profile_version', old_hash, json.dumps(self.decision('legacy', True))))
        before = self.store.reviews()
        self.store.conn.close()
        self.store = Store(self.ws.root / 'jobs.sqlite')
        self.ledger = Outreach(self.ws, self.store)
        self.assertEqual(self.store.reviews(), before)
        self.assertEqual(self.cli('jobs', '--unreviewed')['jobs'], [])
        self.assertTrue(self.ledger.preview(['legacy'])['ready_for_confirmation'])

    def test_on_demand_report_includes_collection_gaps_without_routing_or_writes(self):
        self.seed(['pending'])
        write_json(self.ws.runtime / 'collections' / 'sample.json', {
            'run_id': 'sample', 'state': 'completed_with_gaps', 'queries': [
                {'key': 'one', 'state': 'failed', 'pages': {'1': {}}, 'skipped_rows': 2, 'reason': 'pagination_timeout'},
                {'key': 'two', 'state': 'completed', 'pages': {'1': {}, '2': {}}}]})
        before = self.store.stats()
        report = self.cli('report')
        self.assertEqual(report['queue_counts']['unreviewed'], 1)
        self.assertEqual(report['latest_collection']['state'], 'completed_with_gaps')
        self.assertEqual([r['pages_collected'] for r in report['latest_collection']['searches']], [1, 2])
        self.assertNotIn('next', report)
        self.assertEqual(self.store.stats(), before)
        self.assertEqual(list((self.ws.root / 'outputs').glob('*.md')), [])


if __name__ == '__main__':
    unittest.main()
