"""Read public filter dictionaries already loaded by the search page."""
from copy import deepcopy
from .local import Stopped, now

# Read-only: never invokes a component method or makes a website request.
READ_CATALOG = r"""(() => {
 const out = {url:location.href, conditions:{}, rules:{}, cities:[], positions:[], industries:[], region:null};
 const seen = new Set();
 const tree = rows => (rows || []).map(r => ({code:String(r.code), name:r.name,
   children:tree(r.subLevelModelList || [])}));
 const fields = {'job-rec-jobType':'job_type','job-rec-salary':'salary','job-rec-exp':'experience',
   'job-rec-degree':'degree','job-rec-scale':'company_size','job-rec-stage':'stage',
   'job-rec-payType':'pay_type','job-rec-partTime':'part_time'};
 const visit = vm => {
   if (!vm || seen.has(vm)) return;
   seen.add(vm);
   const p=vm.$props || {}, name=vm.$options.name || vm.$vnode?.tag || '';
   if (fields[p.kaPrefix] && Array.isArray(p.list)) {
     const field=fields[p.kaPrefix];
     out.conditions[field]=p.list.map(r => ({code:String(r.code),name:r.name,
       ...(r.lowSalary!==undefined ? {low:r.lowSalary,high:r.highSalary} : {})}));
     out.rules[field]={multiple:!!p.multiple,max_select:p.maxSelectNum || 0,label:p.placeholder};
   }
   if (p.placeholder==='职位类型' && Array.isArray(vm.positionList)) {
     out.positions=tree(vm.positionList);
     out.rules.position={multiple:!!p.multiple,max_select:p.maxSelectNum || 0,label:p.placeholder};
   }
   if (p.placeholder==='公司行业' && Array.isArray(vm.industryList)) {
     out.industries=tree(vm.industryList);
     out.rules.industry={multiple:!!p.multiple,max_select:p.maxSelectNum || 0,label:p.placeholder};
   }
   if (/FilterCondition/.test(name) && Array.isArray(vm.cityGroup)) {
     out.cities=vm.cityGroup.flatMap(g => (g.cityList || []).map(r => ({code:String(r.code),name:r.name})));
     out.rules.city={multiple:!!p.config?.city?.multiple,max_select:1,label:'城市'};
     out.rules.area={multiple:true,max_select:vm.maxBusinessSelectNum,label:'工作区域/商圈'};
     out.rules.subway={multiple:true,max_select:vm.maxLineSelectNum,label:'地铁线路/站点'};
   }
   if (name==='CityAreaSelect') {
     out.region={city:String(p.city || ''),areas:tree(vm.businessDistrict?.subLevelModelList),subways:tree(vm.subwayList)};
   }
   for (const child of vm.$children || []) visit(child);
 };
 for (const el of document.querySelectorAll('*')) if (el.__vue__) visit(el.__vue__);
 return JSON.stringify(out);
})()"""

PARAMETERS = {'city':'city','salary':'salary','experience':'experience','degree':'degree',
              'company_size':'scale','stage':'stage','job_type':'jobType','part_time':'partTime',
              'pay_type':'payType','industry':'industry','position':'position',
              'area':'multiBusinessDistrict','subway':'multiSubway'}

READ_LIST_STATE = r"""(() => {
 const seen=new Set();
 const visit=vm=>{
   if (!vm || seen.has(vm)) return null;
   seen.add(vm);
   if (vm.$data?.pageVo && Array.isArray(vm.$data?.jobList) && vm.$data?.formData) {
     const params={};
     for (const k of ['query','city','salary','experience','degree','scale','stage','jobType','payType','partTime','industry','position','multiBusinessDistrict','multiSubway']) {
       const v=vm.$data.formData[k];
       params[k]=Array.isArray(v)?v.join(','):(v??'');
     }
     const page=vm.$data.pageVo.page, size=vm.$data.pageVo.pageSize;
     return {url:location.href,params,page,has_more:vm.$data.hasMore,
       page_ids:vm.$data.jobList.slice((page-1)*size,page*size).map(j=>j.encryptJobId)};
   }
   for (const c of vm.$children || []) { const v=visit(c); if(v)return v; }
   return null;
 };
 for (const el of document.querySelectorAll('*')) if (el.__vue__) { const s=visit(el.__vue__); if(s)return JSON.stringify(s); }
 return JSON.stringify(null);
})()"""


def clean_tree(rows):
    if not isinstance(rows, list):
        raise Stopped('filter_dictionary_schema_changed')
    result = []
    for row in rows:
        code, name = str(row.get('code', '')), row.get('name')
        if not code.isdigit() or not isinstance(name, str) or not name:
            raise Stopped('filter_dictionary_schema_changed')
        item = {'code':code, 'name':name}
        children = row.get('children', row.get('subLevelModelList')) or []
        if children:
            item['children'] = clean_tree(children)
        for key in ('low', 'high'):
            if isinstance(row.get(key), (int, float)):
                item[key] = row[key]
        result.append(item)
    return result


def leaves(rows, parents=()):
    result = []
    for row in rows:
        path = (*parents, row['name'])
        if row.get('children'):
            result.extend(leaves(row['children'], path))
        else:
            result.append({**row, 'path':' > '.join(path)})
    return result


def merge_view(base, view):
    """Keep only public labels/codes/rules. Never persist form values or profile data."""
    result = deepcopy(base)
    result.setdefault('filters', {})
    if not view.get('cities') or not view.get('positions') or not view.get('industries'):
        raise Stopped('filter_dictionary_incomplete')
    choices = dict(view['conditions'])
    choices.update(city=view['cities'], position=leaves(clean_tree(view['positions'])),
                   industry=leaves(clean_tree(view['industries'])))
    for key, rules in view['rules'].items():
        if key not in PARAMETERS:
            continue
        previous = result['filters'].get(key, {})
        options = choices.get(key, previous.get('options', []))
        cleaned = clean_tree(options)
        for src, dst in zip(options, cleaned):
            if src.get('path'):
                dst['path'] = src['path']
        result['filters'][key] = {**previous, **rules, 'parameter':PARAMETERS[key], 'options':cleaned}
    result.update(schema=1, observed_at=now(), scope='BOSS web search; public menu dictionaries')
    result['filters']['part_time']['requires'] = {'job_type':'1903'}
    result['filters']['pay_type']['requires'] = {'job_type':'1903'}
    result['filters']['salary']['excludes'] = {'job_type':'1903'}
    result['filters']['stage']['excludes'] = {'job_type':'1903'}
    return result


def clean_region(region):
    if not region or not str(region.get('city','')).isdigit():
        raise Stopped('region_dictionary_incomplete')
    return {'city':str(region['city']), 'observed_at':now(),
            'areas':clean_tree(region.get('areas', [])), 'subways':clean_tree(region.get('subways', []))}
