"""⑤ cap over every verified plan, independent checking, existing draft path reuse."""
from copy import deepcopy
from fractions import Fraction
from decimal import Decimal

from app.agents.kan60.counter_offer.contract import money, number
from app.agents.kan60.counter_offer.pricing import select_price
from app.agents.kan60.counter_offer.draft_contract import MAX_PRICE_MEANING, _assemble_verified_packet
from app.agents.kan60.counter_offer.drafting import _generate_validated_draft
from .contract import ReviewError, digest, failure, n, parse, require, shape, text_number as s
from .contract import calculated_number as cn
from .tools import _assess, verify_handoff


def _context(data,bundle):
    d=parse(data);b=verify_handoff(d,bundle)
    valid=[p for p in b['plans'] if p['status']=='VALID']
    keep=next((p for p in valid if p['type']=='KEEP'),None)
    alternatives=[p for p in valid if p['type']!='KEEP']
    unresolved=[p for p in b['plans'] if p['status']=='NEEDS_INFO']
    require(keep is not None,'KEEP_NOT_VERIFIED','plans','유지안의 계약/비용/물량 검증이 필요합니다.','HOLD')
    require(not unresolved or d['policy']['allow_partial_comparison'],'PLAN_INFORMATION_REQUIRED','plans',
            '미확인 후보 정보가 있어 비교를 중단합니다.','HOLD')
    require(bool(alternatives),'NO_VERIFIED_ALTERNATIVE','plans','비교할 검증 대안이 없습니다. 추가 확인이 필요합니다.','HOLD')
    return d,keep,alternatives


def _bounds(d,keep,alternatives):
    p=d['policy'];a=cn(keep['cost_function']['coefficient']);b=cn(keep['cost_function']['constant'])
    bounds={'requested_price':n(d['incumbent']['requested_unit_price']),'policy_unit_price':n(p['max_unit_price']),
            'policy_total_cost':(n(p['max_total_cost'])-b)/a}
    if p.get('max_increase_rate') is not None:
        bounds['increase_rate']=n(d['incumbent']['old_unit_price'])*(1+n(p['max_increase_rate']))
    for plan in alternatives:
        denom=a-cn(plan['cost_function']['coefficient'])
        require(denom>0,'UNSUPPORTED_COST_MODEL','plans','유지안보다 적은 재가격 계수의 대안이 필요합니다.','HOLD')
        bounds[plan['plan_id']]=(cn(plan['cost_function']['constant'])-b)/denom
    return bounds


def _independent_quote(q, quantity):
    """Separate Decimal boundary walk; never uses the operating tier selector."""
    if not q.get('price_tiers'):
        return n(q['unit_price']), None
    quantity=Decimal(s(quantity));matches=[]
    for i,t in enumerate(q['price_tiers']):
        low=t['min_quantity'];high=t['max_quantity']
        if low is not None:
            low=Decimal(low)
            if quantity<low or quantity==low and t['min_inclusive'] is not True:continue
        if high is not None:
            high=Decimal(high)
            if quantity>high or quantity==high and t['max_inclusive'] is not True:continue
        matches.append((Fraction(Decimal(t['unit_price'])),i))
    require(len(matches)==1,'INDEPENDENT_TIER_COVERAGE','price_tiers','독립 경계 검사에서 적용 구간이 하나가 아닙니다.')
    return matches[0]


def _independent_details(d,plan,price):
    quotes={q['supplier_id']:q for q in d['quotes']};inc=d['incumbent']['supplier_id']
    fixed=n(d['fixed_commitment']['quantity']);total=fixed*n(d['fixed_commitment']['unit_price']) if fixed else 0
    lines=[];active={inc} if fixed else set()
    if fixed:lines.append({'supplier_id':inc,'quantity':s(fixed),'unit_price':s(n(d['fixed_commitment']['unit_price'])),'goods_amount':s(total)})
    for allocation in plan['allocations']:
        sid=allocation['supplier_id'];active.add(sid);qty=n(allocation['quantity']);q=quotes[sid]
        if 'pack_multiple' in q:
            require(q.get('pack_multiple_unit')==q['unit'], 'INDEPENDENT_PACK_MULTIPLE_UNIT', 'pack_multiple',
                    '독립 검사에서 포장 배수 단위가 다릅니다.')
            pack=Decimal(q['pack_multiple'])
            require(pack>0 and Decimal(allocation['quantity'])%pack==0,
                    'INDEPENDENT_PACK_MULTIPLE_VIOLATION', 'pack_multiple', '독립 검사에서 견적 포장 배수를 위반했습니다.')
        quoted,index=_independent_quote(q,qty)
        override=price is not None and sid==inc and (plan['type']=='KEEP' or d['repricing_scope']=='ALL_INCUMBENT_CHANGEABLE')
        unit=price if override else quoted
        total+=qty*unit
        line={'supplier_id':sid,'quantity':s(qty),'unit_price':s(unit),'goods_amount':s(qty*unit)}
        if index is not None:
            line.update(quoted_unit_price=s(quoted),tier_index=index,tier_quantity=s(qty),
                        tier_source_ref=q['price_tiers'][index]['source_ref'],
                        unit_price_basis='PROPOSED_PRICE' if override else 'QUOTE_TIER')
        lines.append(line)
    for sid in active:
        total+=sum(n(v) for k,v in quotes[sid]['costs'].items() if not (sid==inc and k=='switching' and v is None))
    return total,lines


def verify_plan_cost(d,row,proposed_price=None):
    """Re-read source intervals and quantities; operating prices/totals are claims."""
    total,expected=_independent_details(d,row,proposed_price)
    require(cn(row['total_cost'])==total,'INDEPENDENT_PLAN_COST_MISMATCH','plans','원본 견적과 배분으로 재계산한 총액이 다릅니다.')
    keys={'supplier_id','quantity','unit_price','goods_amount','quoted_unit_price','tier_index',
          'tier_quantity','tier_source_ref','unit_price_basis'}
    actual=[{k:v for k,v in line.items() if k in keys} for line in row['lines']]
    require(sorted(map(digest,actual))==sorted(map(digest,expected)),
            'INDEPENDENT_PLAN_LINE_MISMATCH','plans','구간 단가/적용 수량/재가격 근거가 다릅니다.')
    return True


def _direct_totals(d,plans,price):
    return {plan['plan_id']:_independent_details(d,plan,price)[0] for plan in plans}


def _feasible(d,keep,alts,price):
    p=d['policy'];totals=_direct_totals(d,[keep,*alts],price);total=totals[keep['plan_id']]
    return (price>0 and price>=n(p['min_unit_price']) and price<=n(p['max_unit_price'])
            and price<=n(d['incumbent']['requested_unit_price']) and price%n(p['price_increment'])==0
            and total<=n(p['max_total_cost']) and all(total<=totals[a['plan_id']] for a in alts)
            and (p.get('max_increase_rate') is None or price<=n(d['incumbent']['old_unit_price'])*(1+n(p['max_increase_rate']))))


def _description(d,bundle,keep,alts,price,decision):
    rows=[_assess(d,{k:p[k] for k in ('plan_id','type','allocations')},price) for p in [keep,*alts]]
    evaluated=rows[0];alternatives=rows[1:];best=min(cn(p['total_cost']) for p in alternatives)
    comparison={'price_used':s(price),'repricing_scope':d['repricing_scope'],
                'scope':'VERIFIED_PROVIDED_PLAN_SET_AT_PROPOSED_PRICE',
                'keep_total_cost':evaluated['total_cost'],'best_alternative_total_cost':s(best),
                'lowest_alternative_ids':[p['plan_id'] for p in alternatives if cn(p['total_cost'])==best],
                'plans_at_proposed_price':rows,
                'excluded_plans':[{'plan_id':p['plan_id'],'status':p['status'],'issues':p['issues']} for p in bundle['plans'] if p['status']!='VALID']}
    offer=None
    if decision=='READY':
        requested=_direct_totals(d,[keep],n(d['incumbent']['requested_unit_price']))[keep['plan_id']]
        before=_direct_totals(d,[keep],n(d['incumbent']['old_unit_price']))[keep['plan_id']]
        offer={'target_unit_price':s(price),'total_cost':evaluated['total_cost'],'goods_amount':evaluated['goods_amount'],
               'costs':evaluated['costs'],'currency':d['currency'],'unit':d['unit'],
               'requested_total_cost':s(requested),'baseline_total_cost':s(before),
               'alternative_total_cost':s(best),'savings_vs_request':s(requested-cn(evaluated['total_cost'])),
               'verification_refs':sorted(set(r for p in rows for r in p['evidence_refs']))}
    return {'schema_version':'kan.plan-offer.v0','outcome':'SUCCESS','decision':decision,'offer':offer,
            'comparison':comparison,'issues':[],'input_digest':digest(d),'plan_validation_ref':bundle['validation_ref']}


def calculate_offer(data,bundle):
    try:
        d,keep,alts=_context(data,bundle);bounds=_bounds(d,keep,alts)
        p=d['policy'];request=n(d['incumbent']['requested_unit_price'])
        price=Fraction(select_price(min(bounds.values()),number(p['price_increment'],'policy.price_increment'),number(p['min_unit_price'],'policy.min_unit_price')))
        decision='NO_COUNTER_NEEDED' if p['skip_if_request_not_more_than_alternative'] and price==request else 'READY'
        return _description(d,bundle,keep,alts,price,decision)
    except ReviewError as exc:return {**failure(exc),'offer':None,'comparison':None}


def validate_offer(data,bundle,result):
    """Never calls calculate_offer/_bounds/select_price. Direct sums + next-grid check."""
    try:
        d,keep,alts=_context(data,bundle)
        shape(result,'calculation','schema_version outcome decision offer comparison issues input_digest plan_validation_ref')
        require(result['outcome']=='SUCCESS' and result['decision'] in ('READY','NO_COUNTER_NEEDED'),'CALCULATION_NOT_READY','calculation','정상 계산이 필요합니다.')
        decision=result['decision'];request=n(d['incumbent']['requested_unit_price'])
        if decision=='READY':
            require(type(result['offer']) is dict,'CALCULATION_NOT_READY','offer','제안 결과가 필요합니다.')
            price=n(result['offer'].get('target_unit_price'),positive=True)
        else:price=request
        require(_feasible(d,keep,alts,price),'POLICY_OR_COMPARISON_VIOLATION','offer','배정별 재계산/정책 부등식 위반입니다.','HOLD')
        skip=d['policy']['skip_if_request_not_more_than_alternative'] and _feasible(d,keep,alts,request)
        require((decision=='NO_COUNTER_NEEDED')==skip,'DECISION_MISMATCH','decision','역제안 생략 정책과 결과가 다릅니다.')
        require(not _feasible(d,keep,alts,price+n(d['policy']['price_increment'])),'TARGET_RULE_MISMATCH','offer','다음 가격 증분도 가능하므로 상한이 아닙니다.')
        expected=_description(d,bundle,keep,alts,price,decision)
        for row in result['comparison']['plans_at_proposed_price']:
            verify_plan_cost(d,row,price)
        direct=_direct_totals(d,[keep,*alts],price)
        require(all(cn(p['total_cost'])==direct[p['plan_id']] for p in expected['comparison']['plans_at_proposed_price']),
                'CALCULATION_MISMATCH','plans','공통 도구와 독립 배정 재계산이 다릅니다.')
        require(digest(result)==digest(expected),'CALCULATION_MISMATCH','calculation','총액/배분/가격 범위/근거/입력 연결이 다릅니다.')
        return {'valid':True,'issues':[],'validation_ref':'PLAN-OFFER-'+digest(expected)}
    except ReviewError as exc:return {'valid':False,'issues':[exc.issue],'validation_ref':None}


def build_draft_packet(data,bundle,result):
    checked=validate_offer(data,bundle,result)
    require(checked['valid'] and result.get('decision')=='READY','DRAFT_INPUT_NOT_VERIFIED','calculation','검증된 초안 대상이 필요합니다.')
    d=parse(data);o=result['offer'];comp=result['comparison'];fixed=d['fixed_commitment']
    diff=cn(o['total_cost'])-cn(o['alternative_total_cost'])
    plan_facts=[]
    for p in comp['plans_at_proposed_price']:
        lines=[{**line,'quote_id':line.get('quote_id'),'quote_version':str(line['quote_version']) if 'quote_version' in line else None} for line in p['lines']]
        plan_facts.append({'plan_id':p['plan_id'],'type':p['type'],'total_cost':p['total_cost'],'lines':lines,
                           'supplier_costs':deepcopy(p['supplier_costs']),
                           'quote_versions':[{'supplier_id':sid,'quote_id':q['quote_id'],'version':str(q['version'])} for sid,q in p['quote_versions'].items()]})
    facts={'supplier_id':d['incumbent']['supplier_id'],'item':deepcopy(d['item']),
           'currency':d['currency'],'unit':d['unit'],'tax_basis':d['terms']['tax_basis'],
           'proposal':{'unit_price':o['target_unit_price'],'price_meaning':MAX_PRICE_MEANING,
                       'quantity_scope':'CHANGEABLE_ONLY','quantity':d['changeable_quantity'],
                       'application_period':deepcopy(d['period']),'price_valid_until':None,
                       'confirmed_delivery_date':None,'agreement_status':'PROPOSED_NOT_ACCEPTED'},
           'fixed_commitment':{'quantity_scope':'FIXED_ONLY','quantity':fixed['quantity'],'unit_price':fixed.get('unit_price')},
           'comparison_period':deepcopy(d['period']),'total_quantity':s(n(fixed['quantity'])+n(d['changeable_quantity'])),
           'costs':{'scope':'FULL_PERIOD_FIXED_PLUS_CHANGEABLE','cost_model':'PRICE_INDEPENDENT',
                    'goods_amount':o['goods_amount'],'buyer_extra_costs':deepcopy(o['costs']),'total_cost':o['total_cost']},
           'terms':deepcopy(d['terms']),
           'comparison_to_requested':{'baseline':'REQUESTED_PRICE_ACCEPTANCE','scope':'FULL_PERIOD_FIXED_PLUS_CHANGEABLE',
                                      'baseline_total_cost':o['requested_total_cost'],'reduction_amount':o['savings_vs_request']},
           'comparison_to_alternative':{'baseline':'VERIFIED_PLAN_SET_REPRICED_AT_PROPOSAL','scope':comp['scope'],
                                       'baseline_total_cost':o['alternative_total_cost'],'counter_minus_alternative':s(diff),
                                       'relationship':'EQUAL' if diff==0 else 'LOWER' if diff<0 else 'HIGHER'},
           'plan_comparison':{'repricing_scope':d['repricing_scope'],'input_revision':str(d['revision']),
                              'validation_ref':bundle['validation_ref'],
                              'plans':plan_facts,
                              'excluded_plans':comp['excluded_plans']}}
    packet=_assemble_verified_packet(d,o,checked,facts,MAX_PRICE_MEANING)
    packet['approved_wording']['explanations']['PRICE_MEANING']=(
        '제안 단가는 전달된 검증 완료 구매안 모두를 명시 가격 적용 범위로 재계산하여 비교한 최대 허용 단가입니다. '
        '고정 계약 단가는 바뀌지 않습니다. 미확인·무효 안은 제외되며 전체 후보나 시장의 최저가를 뜻하지 않습니다.')
    packet.pop('packet_id');packet['packet_id']='KAN60-DRAFT-'+digest(packet)
    return packet


def run_offer_with_model(data,bundle,model=None):
    d=deepcopy(data);b=deepcopy(bundle);result=calculate_offer(d,b)
    result.update(draft=None,validation=None,draft_validation=None,draft_status='NOT_ATTEMPTED',failure_stage=None,
                  send_authorized=False,draft_input_summary=None,
                  execution={'mode':'VERIFIED_PLAN_SET_DRAFT_PIPELINE','llm_used':False,'mail_sent':False,
                             'model_call_attempted':False,'model_mode':None,'model_id':None})
    if result['outcome']!='SUCCESS':result['failure_stage']='INPUT_OR_CALCULATION';return result
    calc={k:deepcopy(result[k]) for k in ('schema_version','outcome','decision','offer','comparison','issues','input_digest','plan_validation_ref')}
    checked=validate_offer(d,b,calc);result['validation']=checked
    if not checked['valid']:
        result.update(outcome='REVIEW_REQUIRED',decision='HOLD',offer=None,issues=checked['issues'],failure_stage='CALCULATION_VALIDATION');return result
    if result['decision']=='NO_COUNTER_NEEDED':result['draft_status']='NOT_NEEDED';return result
    try:packet=build_draft_packet(d,b,calc)
    except ReviewError as exc:
        result.update(outcome='REVIEW_REQUIRED',decision='HOLD',offer=None,issues=[exc.issue],failure_stage='CALCULATION_VALIDATION');return result
    return _generate_validated_draft(result,packet,model)
