"""kan.plan.v0 / prototype-1. Explicit fixed contracts and price application scopes."""
from copy import deepcopy
from fractions import Fraction
import re

from app.agents.kan60.counter_offer.contract import ReviewError, day, digest, money, number, require, shape, string, terms


def item(value, path):
    """Team name/specification plus only explicitly supplied matching conditions.

    A calculator's opaque item_id is not a supplier fact. Optional business
    attributes remain constraints when specified; absence does not assert a value.
    """
    result = shape(value, path, 'name specification', 'item_id origin grade processing')
    for key, val in result.items():
        string(val, path + '.' + key)
    return result


def n(value,path='number',positive=False):
    """External input: keep the existing 15 integer / 6 fractional digit limit."""
    return Fraction(number(value,path,positive=positive))


def calculated_number(value,path='calculated_number'):
    """Exact finite decimal from checked tool output; never apply input precision caps.

    These values are still compared with recomputed results before being trusted.
    The size bound is a resource limit, not a rounding/precision operation.
    """
    require(type(value) is str and len(value)<=256 and re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',value) is not None,
            'INVALID_FIELD_VALUE',path,'계산값은 유한 십진수 문자열이어야 합니다.')
    return Fraction(value)


def text_number(value):
    f=Fraction(value)
    denominator=f.denominator;twos=fives=0
    while denominator%2==0:denominator//=2;twos+=1
    while denominator%5==0:denominator//=5;fives+=1
    require(denominator==1,'NON_TERMINATING_DECIMAL','calculation','순환 소수를 임의 반올림해 기록할 수 없습니다.')
    scale=max(twos,fives)
    numerator=abs(f.numerator)*2**(scale-twos)*5**(scale-fives)
    digits=str(numerator).zfill(scale+1)
    result=(digits[:-scale]+'.'+digits[-scale:]).rstrip('0').rstrip('.') if scale else digits
    return ('-' if f<0 else '')+result


def validate_price_tiers(tiers):
    """Canonical all-units intervals, retaining open/closed endpoints."""
    require(isinstance(tiers, list) and 1 <= len(tiers) <= 100, 'TIER_TABLE_INVALID', 'price_tiers', '1~100개 구간이 필요합니다.')
    previous = None
    for t in tiers:
        shape(t, 'price_tiers', 'min_quantity max_quantity min_inclusive max_inclusive unit_price source_ref')
        require(type(t['min_inclusive']) is bool and type(t['max_inclusive']) is bool,
                'TIER_BOUNDARY_AMBIGUOUS', 'price_tiers', '경계 포함 여부가 필요합니다.')
        lo = n(t['min_quantity']) if t['min_quantity'] is not None else None
        hi = n(t['max_quantity']) if t['max_quantity'] is not None else None
        require(lo is not None or hi is not None, 'TIER_BOUNDARY_AMBIGUOUS', 'price_tiers', '경계 없는 구간은 해석하지 않습니다.')
        require(lo is None or hi is None or lo < hi or lo == hi and t['min_inclusive'] and t['max_inclusive'],
                'TIER_INTERVAL_EMPTY', 'price_tiers', '빈 구간/역전된 경계입니다.')
        n(t['unit_price'], positive=True);string(t['source_ref'], 'price_tiers.source_ref')
        if previous is not None:
            end = n(previous['max_quantity']) if previous['max_quantity'] is not None else None
            require(end is not None and lo is not None and (end < lo or end == lo and
                    not (previous['max_inclusive'] and t['min_inclusive'])),
                    'TIER_OVERLAP', 'price_tiers', '정렬된 단가 구간이 겹칩니다(경계 포함 여부 포함).')
        previous = t


def validate_tier_coverage(tiers, lower, upper):
    """No unknown prices anywhere in the feasible comparison quantity range.

    Zero is not an order. Out-of-MOQ/capacity/comparison quantities are not
    extrapolated and need not be covered by this invocation.
    """
    if lower > upper or upper <= 0:
        return
    cursor, included = lower, lower == 0
    for t in tiers:
        lo = n(t['min_quantity']) if t['min_quantity'] is not None else None
        hi = n(t['max_quantity']) if t['max_quantity'] is not None else None
        if hi is not None and (hi < cursor or hi == cursor and not t['max_inclusive']):
            continue
        require(lo is None or lo < cursor or lo == cursor and (included or t['min_inclusive']),
                'TIER_GAP', 'price_tiers', '가능 주문 수량에 단가가 없는 구간/경계가 있습니다.')
        if hi is None or hi > upper or hi == upper and t['max_inclusive']:
            return
        if hi > cursor:
            cursor, included = hi, t['max_inclusive']
        elif hi == cursor:
            included = included or t['max_inclusive']
    require(False, 'TIER_GAP', 'price_tiers', '가능 주문 수량의 끝까지 단가가 제시되지 않았습니다.')


def quoted_price(q, quantity):
    """Operating selector. The independent checker does not call this function."""
    if not q.get('price_tiers'):
        return n(q['unit_price'], 'quote.unit_price', True), None
    found = []
    for i, t in enumerate(q['price_tiers']):
        lo = n(t['min_quantity']) if t['min_quantity'] is not None else None
        hi = n(t['max_quantity']) if t['max_quantity'] is not None else None
        if (lo is None or quantity > lo or quantity == lo and t['min_inclusive']) and (
                hi is None or quantity < hi or quantity == hi and t['max_inclusive']):
            found.append((n(t['unit_price'], positive=True), i))
    require(len(found) == 1, 'TIER_GAP' if not found else 'TIER_OVERLAP', 'price_tiers',
            '배분 수량에 적용할 구간이 정확히 하나여야 합니다.')
    return found[0]


def parse(data):
    d=shape(data,'input','schema_version contract_revision data_kind request_id revision as_of period item unit currency terms incumbent fixed_commitment changeable_quantity repricing_scope quotes policy')
    require(d['schema_version']=='kan.plan.v0' and d['contract_revision']=='prototype-1','UNSUPPORTED_SCHEMA','input','임시 구매안 계약 버전이 다릅니다.')
    require(d['data_kind'] in ('SYNTHETIC','LIVE'),'INVALID_FIELD_VALUE','data_kind','자료 종류가 필요합니다.')
    string(d['request_id'],'request_id')
    require(type(d['revision']) is int and d['revision']>=0,'INVALID_FIELD_VALUE','revision','0 이상의 정수 revision이 필요합니다.')
    asof=day(d['as_of'],'as_of');period=shape(d['period'],'period','from to')
    start,end=day(period['from'],'period.from'),day(period['to'],'period.to')
    require(asof<=start<=end,'INVALID_COMPARISON_DATE','period','판단일 이후 비교기간이 필요합니다.')
    item(d['item'],'item');terms(d['terms'],'terms');string(d['unit'],'unit')
    require(d['currency']=='KRW','CURRENCY_NOT_SUPPORTED','currency','KRW만 지원합니다.')
    inc=shape(d['incumbent'],'incumbent','supplier_id old_unit_price requested_unit_price effective_date source_ref verified')
    for k in ('supplier_id','source_ref'):string(inc[k],'incumbent.'+k)
    require(inc['verified'] is True,'MISSING_EVIDENCE','incumbent.verified','인상 원문 검증 근거가 필요합니다.')
    require(n(inc['requested_unit_price'],positive=True)>n(inc['old_unit_price'],positive=True),'NOT_PRICE_INCREASE','incumbent','인상 요청만 지원합니다.')
    require(day(inc['effective_date'],'incumbent.effective_date')<=start,'EFFECTIVE_DATE_MISMATCH','period','인상 적용일 이후 기간이 필요합니다.')
    fixed=shape(d['fixed_commitment'],'fixed_commitment','supplier_id quantity source_ref verified','unit_price period')
    require(fixed['supplier_id']==inc['supplier_id'],'SUPPLIER_MISMATCH','fixed_commitment','기존 거래처 고정 계약만 지원합니다.')
    fixed_qty=n(fixed['quantity'],'fixed_commitment.quantity')
    require(fixed['verified'] is True,'FIXED_CONTRACT_UNVERIFIED','fixed_commitment','고정 물량 유무의 명시 근거가 필요합니다.')
    if fixed_qty:
        n(fixed.get('unit_price'),'fixed_commitment.unit_price',True)
        require(fixed.get('period')==d['period'],'FIXED_CONTRACT_UNVERIFIED','fixed_commitment','양수 고정 계약의 단가/기간 근거가 필요합니다.')
    string(fixed['source_ref'],'fixed_commitment.source_ref')
    change=n(d['changeable_quantity'],'changeable_quantity',True)
    p=shape(d['policy'],'policy','policy_id source_ref approved valid_from valid_to allow_split max_suppliers allocation_step min_unit_price max_unit_price max_total_cost price_increment skip_if_request_not_more_than_alternative allow_partial_comparison','max_increase_rate')
    require(d['repricing_scope'] in ('ALL_INCUMBENT_CHANGEABLE','KEEP_ONLY') or
            d['repricing_scope'] is None and (p['allow_split'] is False or p['max_suppliers']==1),
            'PRICE_SCOPE_UNCONFIRMED','repricing_scope','분산안의 기존 업체 물량에 적용할 가격 범위를 명시하세요.')
    string(p['policy_id'],'policy.policy_id');string(p['source_ref'],'policy.source_ref')
    require(p['approved'] is True,'POLICY_UNAPPROVED','policy.approved','명시 승인 정책이 필요합니다.','HOLD')
    require(day(p['valid_from'],'policy.valid_from')<=asof and end<=day(p['valid_to'],'policy.valid_to'),'POLICY_NOT_CURRENT','policy','정책 유효기간 확인 필요.','HOLD')
    for k in ('allow_split','skip_if_request_not_more_than_alternative','allow_partial_comparison'):
        require(type(p[k]) is bool,'INVALID_FIELD_VALUE','policy.'+k,'명시 bool 정책이 필요합니다.')
    require(type(p['max_suppliers']) is int and 1<=p['max_suppliers']<=2,'UNSUPPORTED_POLICY','policy.max_suppliers','최대 2개 업체 지원.')
    step=n(p['allocation_step'],'policy.allocation_step',True)
    require(change%step==0,'QUANTITY_GRID_MISMATCH','changeable_quantity','변경 물량이 배정 단위의 정수배여야 합니다.')
    require(change/step<=1000,'SEARCH_LIMIT','policy.allocation_step','1000개 배정 단위 초과; 일부 탐색을 최적이라고 보고하지 않습니다.','HOLD')
    require(n(p['min_unit_price'])<=n(p['max_unit_price'],positive=True),'POLICY_VIOLATION','policy','하한이 상한을 초과합니다.','HOLD')
    n(p['price_increment'],positive=True);n(p['max_total_cost'],positive=True)
    if p.get('max_increase_rate') is not None:n(p['max_increase_rate'])
    require(type(d['quotes']) is list and 1<=len(d['quotes'])<=8,'INVALID_FIELD_VALUE','quotes','현재 견적 1~8개가 필요합니다.')
    suppliers=set();ids=set()
    for i,q in enumerate(d['quotes']):
        path=f'quotes[{i}]'
        shape(q,path,'supplier_id quote_id version received_on valid_from valid_until supply_from supply_to item unit currency terms unit_price moq available_quantity lead_days costs cost_scope cost_source_ref source_ref source_verified eligibility',
              'price_tiers pricing_method tier_method_ref pack_multiple pack_multiple_unit pack_multiple_source_ref')
        if 'pack_multiple' in q:
            n(q['pack_multiple'], path+'.pack_multiple', True)
            require(q.get('pack_multiple_unit') == q['unit'], 'PACK_MULTIPLE_UNIT_UNSUPPORTED', path, '포장 배수 단위 환산은 지원하지 않습니다.')
            string(q.get('pack_multiple_source_ref'), path+'.pack_multiple_source_ref')
        if q.get('price_tiers'):
            require(not fixed_qty, 'TIER_FIXED_QUANTITY_BASIS_UNSUPPORTED', path,
                    '양수 고정 물량의 구간 판정 합산 범위가 필요합니다.')
            require(q.get('pricing_method') == 'ALL_UNITS', 'TIER_METHOD_UNSUPPORTED', path, '명시된 전량 단가 방식만 지원합니다.')
            validate_price_tiers(q['price_tiers'])
            string(q.get('tier_method_ref'), path+'.tier_method_ref')
            require(q['unit_price'] is None, 'TIER_FLAT_PRICE_CONFLICT', path, '구간/단일 단가를 동시에 임의 선택하지 않습니다.')
        for k in ('supplier_id','quote_id'): string(q[k],path+'.'+k)
        require(q['supplier_id'] not in suppliers and q['quote_id'] not in ids,'DUPLICATE_CURRENT_QUOTE',path,'업체별 현재 버전 하나만 전달하세요.')
        suppliers.add(q['supplier_id']);ids.add(q['quote_id'])
        require(type(q['version']) is int and q['version']>=1,'INVALID_FIELD_VALUE',path+'.version','양의 견적 버전이 필요합니다.')
        require(q['eligibility'] in ('CONFIRMED','NEEDS_INFO','INVALID'),'INVALID_FIELD_VALUE',path+'.eligibility','조건 확인 상태가 필요합니다.')
        require(q['source_verified'] in (True,False,None) and type(q['source_verified']) in (bool,type(None)), 'INVALID_FIELD_VALUE',path+'.source_verified','검증 여부가 필요합니다.')
    require(inc['supplier_id'] in suppliers,'MISSING_EVIDENCE','quotes','유지안 비용/공급량을 확인할 기존 거래처 견적이 필요합니다.')
    return deepcopy(d)


def failure(exc):
    return {'outcome':'REVIEW_REQUIRED','decision':exc.decision,'issues':[exc.issue]}
