"""Offline names -> validated normal search URLs; no browser or model dependency."""
from copy import deepcopy
from itertools import product
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from .catalog import PARAMETERS
from .local import Stopped, digest, page_interval, read_json, search_url

CATALOG_PATH = Path(__file__).with_name('filter-catalog.json')


def load_catalog(ws=None):
    local = ws.root/'filters/catalog.json' if ws else None
    result = read_json(local) if local and local.exists() else read_json(CATALOG_PATH)
    if not result or result.get('schema') != 1:
        raise Stopped('invalid_filter_catalog')
    return result


def values(value):
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, list):
        raise Stopped('invalid_filter_value')
    result = []
    for part in value:
        if not isinstance(part, (str, int)):
            raise Stopped('invalid_filter_value')
        for text in str(part).replace('，', ',').split(','):
            text = text.strip()
            if text and text not in result:
                result.append(text)
    return result


def normalized(value):
    return str(value).strip().casefold().replace('–','-').replace('—','-').replace(' ＞ ',' > ')


def resolve(options, value, field):
    wanted = normalized(value)
    matches = {}
    for item in options:
        names = [item['code'], item['name'], item.get('path','')]
        if field == 'city':
            names.append(item['name'] + '市')
        if field == 'company_size':
            names += [item['name'].replace('人',''), item['name'].replace('人以上','+')]
        if wanted in map(normalized, names):
            # The same position can appear under multiple category paths with one code.
            matches.setdefault(item['code'],item)
    if len(matches) != 1:
        raise Stopped(('unknown_' if not matches else 'ambiguous_') + f'filter_option:{field}:{value}; use filters show')
    return next(iter(matches.values()))


def resolve_field(catalog, key, raw):
    spec = catalog['filters'][key]
    tokens = values(raw)
    if key != 'city' and any(v in ('不限','0') for v in tokens):
        if len(tokens)>1:
            raise Stopped('invalid_unlimited_combination:' + key)
        return []
    selected = {item['code']:item for value in tokens
                for item in [resolve(spec['options'], value, key)]}
    if '0' in selected:
        if len(selected)>1:
            raise Stopped('invalid_unlimited_combination:' + key)
        return []
    limit = spec.get('max_select', 0)
    # Single-select fields are expanded into separate searches below.
    if spec['multiple'] and limit and len(selected)>limit:
        raise Stopped(f'invalid_selection_count:{key}:maximum_{limit}')
    return list(selected.values())


def region_value(rows, selections, limit, kind):
    groups = {}
    for selection in values(selections):
        candidates = []
        for parent in rows:
            if normalized(selection) in (normalized(parent['name']),parent['code']):
                candidates.append((parent,None))
            for child in parent.get('children',[]):
                aliases = (child['name'],child['code'],parent['name']+' > '+child['name'])
                if normalized(selection) in map(normalized, aliases):
                    candidates.append((parent,child))
        if len(candidates)!=1:
            raise Stopped(('unknown_' if not candidates else 'ambiguous_') + f'filter_option:{kind}:{selection}')
        parent,child=candidates[0]
        code=parent['code']
        if child is None:
            groups[code]=None
        elif code not in groups:
            groups[code]=[child['code']]
        elif groups[code] is not None and child['code'] not in groups[code]:
            groups[code].append(child['code'])
    count=sum(len(v) if v else 1 for v in groups.values())
    if count>limit:
        raise Stopped(f'invalid_selection_count:{kind}:maximum_{limit}')
    return ','.join(p+(':'+ '_'.join(c) if c else '') for p,c in groups.items())


def finish_region(item, catalog, region):
    result=deepcopy(item)
    if not result.get('region_filters'):
        return result
    if not region or region.get('city')!=item['params']['city']:
        return result
    for key, raw in result['region_filters'].items():
        spec=catalog['filters'][key]
        encoded=region_value(region['areas' if key=='area' else 'subways'],raw,spec['max_select'],key)
        if encoded:
            result['params'][spec['parameter']]=encoded
    result['needs_region_dictionary']=False
    result['url']=search_url('https://www.zhipin.com/web/geek/jobs?'+urlencode(result['params']))
    return result


def positive_int(value, name):
    if isinstance(value,bool) or not isinstance(value,int) or value<1:
        raise Stopped('invalid_' + name)
    return value


def choose_salary(target, catalog):
    """Use the upper target for boundary/range ambiguity, not a second search."""
    import re
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[kK]?\s*(?:[-–—~至]\s*(\d+(?:\.\d+)?)\s*[kK]?)?\s*', target)
    if not match:
        raise Stopped('invalid_monthly_salary_target')
    low = float(match[1]); high = float(match[2] or match[1])
    if low <= 0 or high < low:
        raise Stopped('invalid_monthly_salary_target')
    options = [o for o in catalog['filters']['salary']['options'] if o['code'] != '0'
               and o['low'] <= high and (not o['high'] or high < o['high'])]
    if not options:
        raise Stopped('salary_target_not_in_catalog')
    option = max(options, key=lambda o:o['low'])
    return {'salary': option['name'], 'code': option['code'], 'target_upper_k': high,
            'single_search': True, 'note': 'Website band is coarse; review actual job salary ranges locally.'}


def compile_plan(raw, catalog, regions=None):
    allowed=set(PARAMETERS)|{'keyword','pages','interval','max_jobs'}
    unknown=set(raw)-allowed
    if unknown:
        raise Stopped('unknown_search_config_keys:' + ','.join(sorted(unknown)))
    keywords=values(raw.get('keyword'))
    if not keywords or not values(raw.get('city')):
        raise Stopped('keyword_and_city_required')
    pages=positive_int(raw.get('pages',10),'pages')
    interval=page_interval(raw.get('interval',5))
    max_jobs=raw.get('max_jobs')
    if max_jobs is not None:
        positive_int(max_jobs,'max_jobs')
    selected={key:resolve_field(catalog,key,raw[key]) for key in PARAMETERS if key in raw and key not in ('area','subway')}
    if len(selected.get('salary', [])) > 1:
        raise Stopped('select_one_salary_band:choose_the_band_covering_the_target_upper_end')
    axes=[]; shared={}; labels={}
    for key, opts in selected.items():
        labels[key]=[o['name'] for o in opts]
        codes=[o['code'] for o in opts]
        if not codes:
            continue
        if catalog['filters'][key]['multiple']:
            shared[PARAMETERS[key]]=','.join(codes)
        else:
            axes.append((PARAMETERS[key],codes))
    if 'city' not in dict(axes):
        raise Stopped('city_required')
    regional={key:values(raw[key]) for key in ('area','subway') if values(raw.get(key))}
    if regional and len(dict(axes)['city'])>1:
        raise Stopped('invalid_region_scope:use_one_city_with_area_or_subway')
    count=len(keywords)
    for _, options in axes:
        count*=len(options)
    if count>100:
        raise Stopped('invalid_search_count:maximum_100_combinations')
    searches=[]
    for keyword in keywords:
        for codes in product(*(options for _,options in axes)):
            params={'query':keyword,**shared,**dict(zip((key for key,_ in axes),codes))}
            part_time=params.get('jobType')=='1903'
            if part_time and 'salary' in params:
                raise Stopped('invalid_filter_combination:salary_unavailable_for_part_time')
            if part_time and 'stage' in params:
                raise Stopped('invalid_filter_combination:stage_unavailable_for_part_time')
            if not part_time and ('payType' in params or 'partTime' in params):
                raise Stopped('invalid_filter_combination:pay_type_and_part_time_require_job_type_兼职')
            item={'params':params,'url':search_url('https://www.zhipin.com/web/geek/jobs?'+urlencode(params)),
                  'region_filters':regional,'needs_region_dictionary':bool(regional)}
            item=finish_region(item,catalog,(regions or {}).get(params['city']))
            item['key']=digest({'params':params,'region':regional})[:16]
            searches.append(item)
    return {'schema':2,'catalog_version':catalog['observed_at'],'spec':raw,'labels':labels,
            'pages_per_search':pages,'interval_seconds':interval,'max_jobs_per_search':max_jobs,
            'searches':searches,'search_count':len(searches),'max_list_pages':len(searches)*pages,
            'planned_page_actions':len(searches)*pages+sum(i['needs_region_dictionary'] for i in searches)}


def request_matches(params, expected):
    """Use only public filter fields; never persist headers/signature/security tokens."""
    for key in set(PARAMETERS.values())|{'query'}:
        actual=params.get(key,'')
        if isinstance(actual,list):
            if len(actual)!=1: return False
            actual=actual[0]
        wanted=expected.get(key,'')
        if str(actual or '') in ('0','') and not wanted:
            continue
        if set(str(actual or '').split(',')) != set(str(wanted or '').split(',')):
            return False
    return True


def request_params(url, post_data=None):
    params=parse_qs(urlsplit(url).query,keep_blank_values=True)
    if post_data:
        import json
        try:
            more=json.loads(post_data)
            if isinstance(more,dict): params.update(more)
        except (ValueError,TypeError):
            params.update(parse_qs(post_data,keep_blank_values=True))
    return params


def plan_from_snapshot(ws, identifier, options):
    import re
    if not re.fullmatch('[a-f0-9]{16}',identifier):
        raise Stopped('invalid_filter_id')
    snapshot=read_json(ws.root/'filters'/(identifier+'.json'))
    if not snapshot:
        raise Stopped('filter_not_observed')
    url=search_url(snapshot['url'])
    params={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()}
    if 'areaBusiness' in params:
        params['multiBusinessDistrict']=params.pop('areaBusiness')
    if not params.get('query') or not params.get('city'):
        raise Stopped('keyword_and_city_required')
    # Validate scalar budgets through the same planner.
    plan=compile_plan({'keyword':params['query'],'city':params['city'],**options},load_catalog(ws))
    plan['searches']=[{'key':digest(params)[:16],'params':params,
                       'url':search_url('https://www.zhipin.com/web/geek/jobs?'+urlencode(params)),
                       'region_filters':{},'needs_region_dictionary':False}]
    plan['snapshot_id']=identifier
    return plan
