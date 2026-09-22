import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from boss import parser, run, request
from bosslib.filters import compile_plan, finish_region, load_catalog, region_value, request_matches, request_params
from bosslib.local import Stopped, Workspace, database, initialize, read_json, write_json
from bosslib.runtime import BrowserSession


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.catalog=load_catalog()

    def plan(self,**kwargs):
        return compile_plan({'keyword':'测试','city':'深圳',**kwargs},self.catalog)

    def test_default_ten_pages_for_each_search_combination(self):
        p=self.plan(keyword=['测试','示例'],city='深圳,杭州',salary='20-50K')
        self.assertEqual((p['search_count'],p['pages_per_search'],p['max_list_pages']),(4,10,40))
        self.assertEqual(p['interval_seconds'],5)
        self.assertIsNone(p['max_jobs_per_search'])

    def test_pages_applies_to_each_city_and_each_single_select_salary(self):
        p=self.plan(city='深圳,杭州',salary='20-50K',company_size='100-499,500-999,1000-9999,10000+',pages=4)
        self.assertEqual((p['search_count'],p['pages_per_search'],p['max_list_pages']),(2,4,8))
        self.assertIsNone(p['max_jobs_per_search'])
        self.assertEqual({s['params']['city'] for s in p['searches']},{'101280600','101210100'})
        self.assertEqual({s['params']['salary'] for s in p['searches']},{'406'})
        self.assertTrue(all(s['params']['scale']=='303,304,305,306' for s in p['searches']))

    def test_multiple_salary_bands_are_rejected_before_search_expansion(self):
        with self.assertRaisesRegex(Stopped, 'select_one_salary_band'):
            self.plan(salary=['20-50K', '50K以上'])

    def test_degree_multiselect_and_unlimited_are_different(self):
        self.assertEqual(self.plan(degree='中专/中技,本科')['searches'][0]['params']['degree'],'208,203')
        self.assertNotIn('degree',self.plan(degree='不限')['searches'][0]['params'])
        with self.assertRaisesRegex(Stopped,'unlimited'):
            self.plan(degree='不限,本科')

    def test_same_position_under_multiple_categories_is_one_selection(self):
        item=self.plan(position='软件项目经理,100608,项目管理 > 项目管理 > 软件项目经理')['searches'][0]
        self.assertEqual(item['params']['position'],'100608')

    def test_unknown_labels_and_wrong_size_boundaries_are_rejected(self):
        for field,value in [('city','不存在的城市'),('degree','研究员'),('company_size','100-500')]:
            with self.assertRaisesRegex(Stopped,'unknown_filter_option'):
                self.plan(**{field:value})

    def test_industry_limit_and_conditional_part_time(self):
        industries=self.catalog['filters']['industry']['options'][:4]
        with self.assertRaisesRegex(Stopped,'maximum_3'):
            self.plan(industry=[v['code'] for v in industries])
        with self.assertRaisesRegex(Stopped,'salary_unavailable'):
            self.plan(job_type='兼职',salary='20-50K')
        with self.assertRaisesRegex(Stopped,'stage_unavailable'):
            self.plan(job_type='兼职',stage='已上市')
        with self.assertRaisesRegex(Stopped,'require_job_type'):
            self.plan(pay_type='日结')
        item=self.plan(job_type='兼职',pay_type='日结,周结',part_time='周末/节假日')['searches'][0]
        self.assertEqual(item['params']['payType'],'2501,2502')
        self.assertEqual(item['params']['partTime'],'2701')

    def test_region_resolution_is_scoped_and_counts_actual_selections(self):
        region={'city':'101280600','areas':[{'code':'440305','name':'南山区','children':[
            {'code':'11','name':'科技园'},{'code':'12','name':'后海'}]}],'subways':[]}
        item=self.plan(area='南山区 > 科技园,后海')['searches'][0]
        self.assertTrue(item['needs_region_dictionary'])
        actual=finish_region(item,self.catalog,region)
        self.assertEqual(actual['params']['multiBusinessDistrict'],'440305:11_12')
        self.assertFalse(actual['needs_region_dictionary'])
        self.assertNotIn('multiBusinessDistrict',item['params'])
        with self.assertRaisesRegex(Stopped,'region_scope'):
            self.plan(city='深圳,杭州',area='南山区')
        with self.assertRaisesRegex(Stopped,'maximum_1'):
            region_value(region['areas'],['科技园','后海'],1,'area')

    def test_request_matching_ignores_tokens_but_requires_all_filters(self):
        wanted=self.plan(degree='本科,硕士')['searches'][0]['params']
        got={**wanted,'degree':'204,203','page':'2','token':'private','experience':''}
        self.assertTrue(request_matches(got,wanted))
        self.assertFalse(request_matches({**got,'city':'101210100'},wanted))
        self.assertFalse(request_matches({**got,'salary':'406'},wanted))
        self.assertFalse(request_matches({**got,'degree':'203'},wanted))
        p=request_params('https://www.zhipin.com/wapi/zpgeek/search/joblist.json?page=2',json.dumps(got))
        self.assertTrue(request_matches(p,wanted))

    def test_all_input_budgets_validated_before_browsing(self):
        for raw in ({'pages':0},{'pages':True},{'interval':1},{'interval':4.9},{'interval':True},
                    {'interval':float('nan')},{'interval':float('inf')},{'max_jobs':0},{'made_up':1}):
            with self.assertRaises(Stopped): self.plan(**raw)

    def test_dry_run_and_dictionary_listing_do_not_use_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            initialize(directory)
            config=Path(directory)/'search.json'
            for configured,override,expected in [(None,None,10),(3,None,3),(3,2,2),(None,1,1)]:
                with self.subTest(configured=configured,override=override):
                    raw={'keyword':'测试','city':['深圳','杭州']}
                    if configured is not None:raw['pages']=configured
                    write_json(config,raw)
                    argv=['--workspace',directory,'collect','--config',str(config),'--dry-run']
                    if override is not None:argv+=['--pages',str(override)]
                    with patch('boss.request') as submit:
                        result=run(parser().parse_args(argv))
                        submit.assert_not_called()
                    self.assertEqual(result['plan']['pages_per_search'],expected)
                    self.assertEqual(result['plan']['max_list_pages'],2*expected)
                    self.assertEqual(result['rate_limits'],{'interval_seconds':5})
            result=run(parser().parse_args(['--workspace',directory,'filters','show','city','--contains','杭州']))
            self.assertEqual(result['options'][0]['code'],'101210100')

    def test_resume_preview_keeps_the_saved_page_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            initialize(directory)
            ws=Workspace(directory);rid=uuid.uuid4().hex
            saved_plan=self.plan(pages=2)
            write_json(ws.runtime/'collections'/(rid+'.json'),{'state':'stopped','plan':saved_plan})
            with patch('boss.request') as submit:
                result=run(parser().parse_args(['--workspace',directory,'collect','--resume',rid,'--dry-run']))
                submit.assert_not_called()
            self.assertEqual(result['plan'],saved_plan)

    def test_old_worker_rejected_instead_of_silently_ignoring_filters(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            initialize(directory);ws=Workspace(directory)
            write_json(ws.runtime/'state.json',{'mode':'idle','heartbeat':time.time(),'session':'old'})
            with self.assertRaisesRegex(Stopped,'runtime_outdated'):
                request(ws,'collect',plan=self.plan())
            self.assertEqual(list((ws.runtime/'requests').glob('*.json')),[])


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();initialize(self.temp.name);self.ws=Workspace(self.temp.name)
        (self.ws.root/'user/PROFILE.md').write_text('示例用户的项目证据已整理',encoding='utf-8')
        (self.ws.root/'user/SEARCH_PLAN.md').write_text('分页测试使用的明确搜索范围',encoding='utf-8')
        self.confirmation=self.ws.confirm_search_plan('同意该测试方案')
        self.s=BrowserSession(self.ws)
        self.s.prepare_tab=AsyncMock()
        self.s.driver=SimpleNamespace(cdp=SimpleNamespace(network=SimpleNamespace(get_response_body=lambda rid:None)))
        self.s.tab=SimpleNamespace(evaluate=AsyncMock(return_value='null'),send=AsyncMock())
        self.s.healthy_page=AsyncMock(return_value={'url':'https://www.zhipin.com/web/geek/jobs'})
        async def listen():self.s.accepting=True
        async def unlisten():self.s.accepting=False;self.s.requests.clear()
        async def settle(**kwargs):
            if self.s.error:raise Stopped(self.s.error)
        self.s.listen=listen;self.s.unlisten=unlisten;self.s.settle=settle
        self.actions=[];self.page=0;self.stop_after=None;self.has_more=True;self.stall=False
        async def action(url=None):
            if self.stop_after is not None and len(self.actions)>=self.stop_after:
                raise Stopped('user_paused')
            self.actions.append(url);self.s.actions+=1
            if url:self.page=1
            elif not self.stall:self.page+=1
            params={**self.s.expected_params,'page':str(self.page)}
            rid=str(len(self.actions))
            event=SimpleNamespace(request_id=rid,request=SimpleNamespace(method='GET',url='https://www.zhipin.com/wapi/zpgeek/search/joblist.json?'+urlencode(params),post_data=None))
            await self.s.on_request(event)
            meta=self.s.requests.pop(rid)
            rows=[{'encryptJobId':'common','jobName':'跨检索重复岗位'},
                  {'encryptJobId':params['city']+'_'+str(self.page),'jobName':'测试岗位'}]
            rows.extend(getattr(self, 'extra_rows', []))
            payload={'code':0,'zpData':{'jobList':rows,'hasMore':self.has_more}}
            self.s.tab.send.return_value=(json.dumps(payload),False)
            await self.s.response_body(rid,meta)
        self.s.action=action

    async def asyncTearDown(self):self.temp.cleanup()

    def plan(self,**kwargs):
        return compile_plan({'keyword':'测试','city':'深圳,杭州','pages':2,**kwargs},load_catalog())

    async def execute(self,request):
        return await self.s.execute({**request,'search_plan_sha256':self.confirmation['plan_sha256']})

    async def test_two_pages_per_city_and_global_database_deduplication(self):
        result=await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':self.plan()})
        self.assertEqual(len(self.actions),4)
        self.assertEqual([s['pages_collected'] for s in result['searches']],[2,2])
        with database(self.ws.root) as store:
            self.assertEqual(store.stats()['jobs'],5)
            self.assertEqual(len(store.job('common')['sources']),2)

    async def test_default_stops_each_search_at_ten_pages_even_with_more_results(self):
        plan=compile_plan({'keyword':'测试','city':'深圳,杭州'},load_catalog())
        result=await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':plan})
        self.assertEqual(len(self.actions),20)
        self.assertEqual([s['pages_collected'] for s in result['searches']],[10,10])
        self.assertTrue(all(s['stop_reason']=='page_limit' for s in result['searches']))
        with database(self.ws.root) as store:
            self.assertEqual(store.stats()['jobs'],21)

    async def test_exhausted_results_stop_without_spurious_scrolls(self):
        self.has_more=False
        result=await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':self.plan(pages=100)})
        self.assertEqual(len(self.actions),2)
        self.assertTrue(all(s['stop_reason']=='no_more_results' for s in result['searches']))

    async def test_no_implicit_five_page_or_thirty_job_limit(self):
        result=await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':self.plan(city='深圳',pages=35)})
        self.assertEqual(len(self.actions),35,result)
        self.assertEqual(result['searches'][0]['jobs'],36)

    async def test_stalled_pagination_is_not_success_and_preserves_first_page(self):
        self.stall=True;rid=uuid.uuid4().hex
        result=await self.execute({'id':rid,'action':'collect','plan':self.plan()})
        saved=read_json(self.ws.runtime/'collections'/(rid+'.json'))
        self.assertEqual(len(saved['queries'][0]['pages']),1)
        self.assertEqual(saved['queries'][1]['state'],'skipped')
        self.assertEqual(len(saved['queries'][1]['pages']),1)
        self.assertEqual(result['state'],'completed_with_gaps')
        self.assertNotIn('next_action', result)

    async def test_bad_row_does_not_discard_valid_records_or_stop_next_search(self):
        self.extra_rows=[{'encryptJobId':'bad/path'}, {'encryptJobId':'padded~'}]
        result=await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':self.plan()})
        self.assertEqual(result['state'],'completed_with_gaps')
        self.assertEqual(len(self.actions),4)
        self.assertTrue(all(s['skipped_rows'] for s in result['searches']))
        with database(self.ws.root) as store:
            self.assertEqual(store.stats()['jobs'],6)
            self.assertTrue(store.job('padded~')['url'].endswith('padded~.html'))

    async def test_verification_stops_without_trying_next_group(self):
        self.s.settle=AsyncMock(side_effect=Stopped('verification_required'))
        with self.assertRaisesRegex(Stopped,'verification_required'):
            await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':self.plan()})
        self.assertEqual(len(self.actions),1)

    async def test_resume_skips_completed_city_and_continues_verified_current_page(self):
        self.stop_after=3;rid=uuid.uuid4().hex;plan=self.plan()
        with self.assertRaisesRegex(Stopped,'user_paused'):
            await self.execute({'id':rid,'action':'collect','plan':plan})
        saved=read_json(self.ws.runtime/'collections'/(rid+'.json'))
        self.assertEqual(saved['queries'][0]['state'],'completed')
        self.assertEqual(saved['queries'][1]['last_page'],1)
        self.s.tab.evaluate.return_value=json.dumps({'url':plan['searches'][1]['url'],'page':1,'has_more':True,
            'params':plan['searches'][1]['params'],'page_ids':['common','101210100_1']})
        self.stop_after=None
        result=await self.execute({'id':uuid.uuid4().hex,'action':'collect','plan':plan,'resume':rid})
        self.assertEqual(len(self.actions),4)
        self.assertIsNone(self.actions[-1])
        self.assertEqual(result['batch'],rid)
        self.assertEqual(result['searches'][1]['resume_mode'],'continue_current_page')

    async def test_wrong_filter_and_late_previous_query_never_enter_database(self):
        self.s.expected_params=self.plan()['searches'][0]['params'];self.s.accepting=True
        event=SimpleNamespace(request_id='wrong',request=SimpleNamespace(method='GET',url='https://www.zhipin.com/wapi/zpgeek/search/joblist.json?query=测试&city=101210100&page=1',post_data=None))
        await self.s.on_request(event)
        self.assertEqual(self.s.requests,{})
        self.s.query_token='new';self.s.responses=0
        self.s.tab.send.return_value=(json.dumps({'code':0,'zpData':{'jobList':[{'encryptJobId':'late'}]}}),False)
        with database(self.ws.root) as store:
            self.s.store=store
            await self.s.response_body('late',{'page':1,'token':'old','source':'previous'})
            self.assertEqual(store.stats()['jobs'],0)
            self.assertEqual(self.s.responses,0)
            self.assertIsNone(self.s.error)


if __name__=='__main__':unittest.main()
