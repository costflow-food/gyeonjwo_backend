"""Shared deterministic plan construction, verification and exact repricing."""
from copy import deepcopy
from itertools import combinations

from .contract import ReviewError, day, digest, failure, item, n, parse, require, shape, string, terms, text_number as s
from .contract import quoted_price


def price_scope(d,supplier,kind):
    if supplier!=d['incumbent']['supplier_id']:return 'QUOTE_PRICE'
    return 'PROPOSED_PRICE' if kind=='KEEP' or d['repricing_scope']=='ALL_INCUMBENT_CHANGEABLE' else 'REQUESTED_PRICE'


def proposal(d, quantities):
    inc=d['incumbent']['supplier_id'];quotes={q['supplier_id']:q for q in d['quotes']}
    active=set(quantities)|({inc} if n(d['fixed_commitment']['quantity']) else set())
    kind='KEEP' if active=={inc} else 'SWITCH' if inc not in active and len(active)==1 else 'SPLIT'
    allocations=[{'supplier_id':sid,'quantity':s(qty),'price_scope':price_scope(d,sid,kind),
                  'quote_id':quotes[sid]['quote_id'],'quote_version':quotes[sid]['version']}
                 for sid,qty in sorted(quantities.items()) if qty>0]
    return {'plan_id':'PLAN-'+digest(allocations)[:20],'type':kind,'allocations':allocations}


def proposals(d):
    qty=n(d['changeable_quantity']);step=n(d['policy']['allocation_step'])
    ids=sorted(q['supplier_id'] for q in d['quotes'])
    rows=[proposal(d,{sid:qty}) for sid in ids]
    if d['policy']['allow_split'] and d['policy']['max_suppliers']>=2:
        for left,right in combinations(ids,2):
            rows.extend(proposal(d,{left:i*step,right:qty-i*step}) for i in range(1,int(qty/step)))
    return rows


def _quote(d,q,qty):
    require(q['eligibility']!='INVALID','ITEM_SPEC_MISMATCH','quote.eligibility','후보 필수 조건 불충족.')
    require(q['source_verified'] is True and q['eligibility']=='CONFIRMED','MISSING_EVIDENCE','quote.source_verified','후보 조건/원문 검증 확인 필요.')
    for k in ('source_ref','cost_source_ref'):string(q[k],'quote.'+k)
    for k in ('received_on','valid_until','supply_from','supply_to','moq','available_quantity'):
        require(q[k] is not None,'MISSING_EVIDENCE','quote.'+k,'미확인 견적값을 추정할 수 없습니다.')
    as_of=day(d['as_of'],'as_of')
    require(day(q['received_on'],'quote.received_on')<=as_of,
            'QUOTE_NOT_CURRENT','quote.received_on','검토 시점 이후 수신 견적은 사용할 수 없습니다.')
    require((q['valid_from'] is None or day(q['valid_from'],'quote.valid_from')<=as_of) and as_of<=day(q['valid_until'],'quote.valid_until'),
            'QUOTE_NOT_CURRENT','quote.valid_until','현재 판단일의 유효 견적이 아닙니다.')
    require(day(q['supply_from'],'quote.supply_from')<=day(d['period']['from'],'period.from')<=day(d['period']['to'],'period.to')<=day(q['supply_to'],'quote.supply_to'),
            'SUPPLY_PERIOD_MISMATCH','quote.supply_from','견적 공급기간이 비교기간을 포함하지 않습니다.')
    if q['lead_days'] is not None:
        require(type(q['lead_days']) is int and 0<=q['lead_days']<=36500,'INVALID_FIELD_VALUE','quote.lead_days','정수 납기 필요.')
        require((day(d['period']['from'],'period.from')-day(d['as_of'],'as_of')).days>=q['lead_days'],'LEAD_TIME_MISMATCH','quote.lead_days','납기 충족 불가.')
    quoted_item=item(q['item'],'quote.item')
    require(all(quoted_item.get(k)==v for k,v in d['item'].items() if k!='item_id') and q['unit']==d['unit'] and q['currency']==d['currency'],
            'ITEM_SPEC_MISMATCH','quote.item','정규화 품목·단위·통화가 다릅니다.')
    require(terms(q['terms'],'quote.terms')==d['terms'],'TRADE_TERMS_MISMATCH','quote.terms','납품/결제/세금 조건 불일치.')
    available=n(q['available_quantity'],'quote.available_quantity');moq=n(q['moq'],'quote.moq')
    if qty>0:require(moq<=qty<=available,'QUANTITY_NOT_FEASIBLE','quote.available_quantity','배정량이 MOQ/추가 공급량 범위 밖입니다.')
    if qty>0 and 'pack_multiple' in q:
        require(qty%n(q['pack_multiple'],positive=True)==0, 'PACK_MULTIPLE_VIOLATION', 'quote.pack_multiple',
                '배정량은 견적에 명시된 포장 배수의 정수배여야 합니다.')
    selected=quoted_price(q,qty)
    if q['supplier_id']==d['incumbent']['supplier_id']:
        basis=quoted_price(q,n(d['changeable_quantity']))[0] if q.get('price_tiers') else selected[0]
        require(basis==n(d['incumbent']['requested_unit_price']),'NOTICE_QUOTE_MISMATCH','quote.unit_price','업무 전체 물량의 인상 요청 근거와 현재 견적이 다릅니다.')
    require(q['cost_scope']=='PER_SUPPLIER_PER_PERIOD','COST_SCOPE_MISMATCH','quote.cost_scope','업체별 1회 전체 기간 추가 비용이 필요합니다.')
    cs=shape(q['costs'],'quote.costs','transport other_logistics switching')
    for k,v in cs.items():
        if k=='switching' and q['supplier_id']==d['incumbent']['supplier_id'] and v is None:
            continue  # No supplier switch occurs for the incumbent; not a zero-cost assertion.
        require(v is not None,'MISSING_EVIDENCE','quote.costs.'+k,'미확인 비용을 0으로 채우지 않습니다.')
        n(v,'quote.costs.'+k)
    if q['supplier_id']==d['incumbent']['supplier_id']:
        require(cs['switching'] is None or n(cs['switching'])==0,'COST_SCOPE_MISMATCH','quote.costs.switching','기존 업체의 전환 비용은 비적용이거나 명시된 0이어야 합니다.')


def _assess(d,plan,price,*,quote_prices=False):
    shape(plan,'plan','plan_id type allocations')
    require(type(plan['allocations']) is list and 1<=len(plan['allocations'])<=2,'INVALID_ALLOCATION','plan.allocations','1~2개 업체 변경 물량 배정 필요.')
    quotes={q['supplier_id']:q for q in d['quotes']};amounts={}
    for row in plan['allocations']:
        shape(row,'allocation','supplier_id quantity price_scope quote_id quote_version')
        sid=string(row['supplier_id'],'allocation.supplier_id')
        require(sid in quotes and sid not in amounts,'INVALID_ALLOCATION','allocation.supplier_id','후보에 없는 업체 또는 중복 배정입니다.')
        qty=n(row['quantity'],'allocation.quantity',True);amounts[sid]=qty
        require(qty%n(d['policy']['allocation_step'])==0,'QUANTITY_GRID_MISMATCH','allocation.quantity','배정 단위 위반.')
        require(row['quote_id']==quotes[sid]['quote_id'] and type(row['quote_version']) is int and row['quote_version']==quotes[sid]['version'],
                'STALE_QUOTE_VERSION','allocation.quote_version','현재 견적 버전과 다릅니다.')
    require(sum(amounts.values())==n(d['changeable_quantity']),'QUANTITY_MISMATCH','plan.allocations','변경 배정 합계가 변경 가능 물량과 다릅니다.')
    expected=proposal(d,amounts)
    require(plan==expected,'PRICE_SCOPE_MISMATCH','plan','안 유형/ID/가격 적용 범위가 명시 계약과 다릅니다.')
    inc=d['incumbent']['supplier_id'];fixed=n(d['fixed_commitment']['quantity'])
    fixed_price=n(d['fixed_commitment']['unit_price']) if fixed else None
    active=set(amounts)|({inc} if fixed else set())
    require(len(active)<=d['policy']['max_suppliers'] and (d['policy']['allow_split'] or len(active)==1),
            'SPLIT_POLICY_VIOLATION','plan.allocations','고정 계약을 포함한 업체 수/분산 정책 위반.')
    a=0;b=fixed*fixed_price if fixed else 0;lines=[];extra={'transport':0,'other_logistics':0,'switching':0};cost_rows=[]
    actual_goods=b
    refs=[d['incumbent']['source_ref'],d['fixed_commitment']['source_ref'],d['policy']['source_ref']]
    if fixed:lines.append({'supplier_id':inc,'quantity':s(fixed),'price_scope':'FIXED_CONTRACT','unit_price':s(fixed_price),'goods_amount':s(fixed*fixed_price),
                           'source_ref':d['fixed_commitment']['source_ref']})
    for sid in sorted(active):
        q=quotes[sid];qty=amounts.get(sid,0);_quote(d,q,qty)
        scope=price_scope(d,sid,plan['type'])
        quote_unit,tier_index=quoted_price(q,qty)
        unit=price if scope=='PROPOSED_PRICE' and not quote_prices else quote_unit
        if scope=='PROPOSED_PRICE':a+=qty
        else:b+=qty*quote_unit
        actual_goods+=qty*unit
        if qty:lines.append({'supplier_id':sid,'quantity':s(qty),'price_scope':scope,'unit_price':s(unit),'goods_amount':s(qty*unit),
                            'quote_id':q['quote_id'],'quote_version':q['version'],'source_ref':q['source_ref']})
        if qty and tier_index is not None:
            lines[-1].update(quoted_unit_price=s(quote_unit), tier_index=tier_index,
                tier_quantity=s(qty), tier_source_ref=q['price_tiers'][tier_index]['source_ref'],
                unit_price_basis='PROPOSED_PRICE' if scope=='PROPOSED_PRICE' and not quote_prices else 'QUOTE_TIER')
            refs.extend([q['price_tiers'][tier_index]['source_ref'], q['tier_method_ref']])
        applicable={k:v for k,v in q['costs'].items() if not (k=='switching' and sid==inc and v is None)}
        for key,val in applicable.items():extra[key]+=n(val);b+=n(val)
        cost_rows.append({'supplier_id':sid,'costs':deepcopy(applicable),'source_ref':q['cost_source_ref']})
        refs.extend([q['source_ref'],q['cost_source_ref']])
        if 'pack_multiple' in q:refs.append(q['pack_multiple_source_ref'])
    total=actual_goods+sum(extra.values())
    policy=d['policy']
    price_ok=(n(policy['min_unit_price'])<=price<=n(policy['max_unit_price'])
              and price<=n(d['incumbent']['requested_unit_price']) and price%n(policy['price_increment'])==0
              and (policy.get('max_increase_rate') is None or price<=n(d['incumbent']['old_unit_price'])*(1+n(policy['max_increase_rate']))))
    return {**deepcopy(plan),'status':'VALID','issues':[],'evaluated_unit_price':s(price),'total_cost':s(total),
            **({'pricing_phase':'QUOTE_AT_ALLOCATION' if quote_prices else 'COUNTER_OFFER_REPRICING',
                'cost_function_scope':'COUNTER_OFFER_REPRICING'} if any(q.get('price_tiers') for q in quotes.values()) else {}),
            'goods_amount':s(total-sum(extra.values())),'costs':{k:s(v) for k,v in extra.items()},
            'supplier_costs':cost_rows,'lines':lines,'cost_function':{'coefficient':s(a),'constant':s(b)},
            'quote_versions':{sid:{'quote_id':quotes[sid]['quote_id'],'version':quotes[sid]['version']} for sid in sorted(active)},
            'evidence_refs':sorted(set(refs)),
            'policy_at_price':{'negotiated_price_within_limits':price_ok,'total_within_retention_budget':total<=n(policy['max_total_cost']),
                               'budget_scope':'COUNTER_OFFER_KEEP_ONLY'}}


def _checked(d,plan,price,*,quote_prices=False):
    try:return _assess(d,plan,price,quote_prices=quote_prices)
    except ReviewError as exc:
        missing=exc.issue['code'] in ('MISSING_EVIDENCE','MISSING_REQUIRED_FIELD','TIER_GAP')
        return {'plan_id':plan.get('plan_id') if isinstance(plan,dict) else None,'proposal':deepcopy(plan),
                'status':'NEEDS_INFO' if missing else 'INVALID','issues':[exc.issue]}


def validate_plan(data,plan,proposed_price=None):
    try:
        d=parse(data);price=n(proposed_price if proposed_price is not None else d['incumbent']['requested_unit_price'],positive=True)
        return _checked(d,plan,price,quote_prices=proposed_price is None)
    except ReviewError as exc:return {**failure(exc),'status':'NEEDS_INFO','plan_id':None}


def reprice_plan(data,plan,proposed_price):
    return validate_plan(data,plan,proposed_price)


def build_plan_set(data):
    try:
        d=parse(data);price=n(d['incumbent']['requested_unit_price'])
        checked=[_checked(d,p,price,quote_prices=True) for p in proposals(d)]
        result={'schema_version':'kan.plan-set.v0','producer':'COMMON_TOOLS_LOCAL','producer_mode':'DETERMINISTIC_NO_LLM',
                'input_digest':digest(d),'revision':d['revision'],'as_of':d['as_of'],'data_kind':d['data_kind'],
                'search_scope':'ALL_PROVIDED_QUOTES_TWO_SUPPLIERS_ALLOCATION_GRID','plans':checked,
                'outcome':'SUCCESS','issues':[]}
        if any(q.get('price_tiers') for q in d['quotes']):
            step=n(d['policy']['allocation_step'])
            result['tier_search']={'allocation_step':d['policy']['allocation_step'],
                'scope':'ALL_PROVIDED_QUOTES_AT_MOST_TWO_SUPPLIERS_ON_DECLARED_GRID',
                'off_grid_boundaries':[{ 'quote_id':q['quote_id'], 'quantity':value }
                    for q in d['quotes'] for t in q.get('price_tiers',[]) for key in ('min_quantity','max_quantity')
                    if (value:=t[key]) is not None and n(value)%step!=0],
                'global_optimum_claimed':False}
        result['validation_ref']='PLAN-SET-'+digest(result)
        return result
    except ReviewError as exc:return {**failure(exc),'plans':[],'validation_ref':None}


def verify_handoff(data,bundle):
    expected=build_plan_set(data)
    require(expected['outcome']=='SUCCESS','PLAN_INPUT_NOT_READY','input','구매안 입력 검증이 필요합니다.')
    try:matches=type(bundle) is dict and digest(bundle)==digest(expected)
    except (ValueError,TypeError):matches=False
    require(matches,'STALE_OR_TAMPERED_PLAN_SET','plan_set',
            '입력/revision/견적 버전/배분/비용/검증 결과가 변경되었거나 안이 누락되었습니다.','HOLD')
    return expected
