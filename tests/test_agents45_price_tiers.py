"""Team-stored tiers -> registered role tools, with model/storage doubles only."""
from fractions import Fraction
from uuid import UUID

import pytest

from test_agents45_team_data import team_storage, run_plan, CASE
from test_agents45_product_registration import ToolDrivenModel
from app.agents import tools
from app.agents.agents import AgentRunner
from app.agents.schemas import AgentRole as R, CounterOfferIn
from app.agents.kan60.team_bridge.team_data import MissingTeamData

ALL_UNITS = '주문 총수량에 해당하는 단가를 전량에 적용합니다.'
GRADUATED = '각 구간에 속한 물량에 각 구간 단가를 적용합니다.'


@pytest.mark.parametrize('fault', ['min', 'max', 'price'])
def test_review_tier_source_numeric_substring_is_rejected(team_storage, fault):
    row = stored_tiers(team_storage, [(None,'500kg 미만','5600'),('500kg 이상',None,'5300')])
    mail = team_storage[0].mail_messages[row.source_mail_id]
    old, new = {'min': ('500kg 이상', '1500kg 이상'), 'max': ('500kg 미만', '1500kg 미만'),
                'price': ('5300원/kg', '15300원/kg')}[fault]
    mail['body_text'] = mail['body_text'].replace(old, new)
    result = run_plan(team_storage)
    assert result.next_action == 'RECHECK_SOURCE' and result.unknown_fields == ['price_tiers']
    assert not team_storage[3].records


def test_review_tier_source_does_not_borrow_another_price_in_same_row(team_storage):
    row=stored_tiers(team_storage, [(None,'500kg 미만','5600'),('500kg 이상',None,'5300')])
    mail=team_storage[0].mail_messages[row.source_mail_id]
    mail['body_text']=mail['body_text'].replace('5300원/kg','15300원/kg; 참고단가 5300원/kg')
    result=run_plan(team_storage)
    assert result.next_action=='RECHECK_SOURCE' and result.unknown_fields==['price_tiers']


def test_review_tiers_and_pack_multiple_reprice_only_feasible_allocations(team_storage):
    from test_agents45_team_data import set_pack
    stored_tiers(team_storage, [(None,'500kg 미만','5600'),('500kg 이상',None,'5300')],capacity='600')
    set_pack(team_storage)
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    rows=[p for p in checked_rows(team_storage) if p['type']=='SPLIT']
    assert {supplier_line(p)['quantity'] for p in rows}=={'300','600'}
    assert {(supplier_line(p)['quantity'],supplier_line(p)['unit_price']) for p in rows}=={('300','5600'),('600','5300')}
    run_counter(team_storage,result)
    offer=next(r['payload']['result'] for r in team_storage[3].records.values() if r['kind']=='OFFER_CALCULATION')
    assert {a['quantity'] for p in offer['comparison']['plans_at_proposed_price'] for a in p['allocations'] if a['supplier_id']=='supplier-b'}=={'300','600'}


def stored_tiers(storage, intervals, *, qid='qv-b', method=ALL_UNITS, quantity=None,
                 capacity='1000', moq='0', split=True):
    common, _, supplement, _, _ = storage
    row = common.quote_versions[qid];mail = common.mail_messages[row.source_mail_id]
    if quantity is not None:
        common.work_cases[CASE]['quantity'] = str(quantity)
        common.work_cases[CASE]['policy']['allocation_step'] = '1'
    common.work_cases[CASE]['policy']['allow_split'] = split
    previous_capacity=row.available_quantity
    row.available_quantity=capacity
    row.evidence_jsonb['available_quantity'].update(value=capacity, raw_value=capacity)
    mail['body_text']=mail['body_text'].replace('available_quantity: '+previous_capacity, 'available_quantity: '+capacity)
    row.pricing_terms_jsonb['moq']=moq
    row.evidence_jsonb['moq'].update(value=moq, raw_value=moq)
    mail['body_text']=mail['body_text'].replace('moq: 0', 'moq: '+moq)
    row.unit_price=None
    row.evidence_jsonb.pop('unit_price', None)
    row.mail_normalized[row.source_mail_id]['fields'].pop('unit_price', None)
    mail['body_text']='\n'.join(line for line in mail['body_text'].splitlines() if not line.startswith('unit_price:'))
    mail['body_text'] += '\n'+method+'\n'+'\n'.join(
        '| '+' ~ '.join(v for v in (lo, hi) if v is not None)+': '+price+'원/kg |' for lo, hi, price in intervals)
    # Use the actual, unchanged team normalizer to obtain its real stored shape.
    rejected, adjusted = [], []
    row.pricing_terms_jsonb['price_tiers'] = tools._tiers(tools._SourceCheck(common, CASE),
        [tools.PriceTier(min_quantity=lo, max_quantity=hi, unit_price=price+'원',
                         evidence=tools.EvidenceRef(mail_id=row.source_mail_id)) for lo, hi, price in intervals],
        rejected, adjusted)
    assert not rejected, rejected
    supplement.refresh_binding()
    return row


def checked_rows(storage):
    return [r['payload']['row'] for r in storage[3].records.values() if r['kind']=='PLAN_CALCULATION']


def supplier_line(row, supplier='supplier-b'):
    return next(line for line in row['lines'] if line['supplier_id']==supplier)


def run_counter(storage, plan):
    common, scope, _, _, _ = storage
    return AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER,
        CounterOfferIn(case_id=UUID(CASE), input_revision=1, quote_version_ids=list(scope.quote_ids), validated_plans=plan.plans))


@pytest.mark.parametrize('closed_lower,quantity,unit,total', [
    (True,499,'5600','2804400'), (True,500,'5300','2660000'), (True,501,'5300','2665300'),
    (False,499,'5600','2804400'), (False,500,'5600','2810000'), (False,501,'5300','2665300'),
])
def test_actual_registry_respects_open_closed_endpoints(team_storage, closed_lower, quantity, unit, total):
    intervals=[(None, '500kg 미만' if closed_lower else '500kg 이하', '5600'),
               ('500kg 이상' if closed_lower else '500kg 초과', None, '5300')]
    row=stored_tiers(team_storage, intervals, quantity=quantity, split=False)
    assert row.pricing_terms_jsonb['price_tiers'][0]['max_quantity']=='500'
    assert row.pricing_terms_jsonb['price_tiers'][0]['max_quantity_raw']==intervals[0][1]
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    switch=next(r for r in checked_rows(team_storage) if r['type']=='SWITCH')
    line=supplier_line(switch)
    assert (line['quantity'],line['unit_price'],switch['total_cost'])==(str(quantity),unit,total)
    assert line['quoted_unit_price']==unit and line['tier_quantity']==str(quantity)


def test_split_crossing_tier_and_counter_reassessment_keep_quantity_price_evidence(team_storage):
    stored_tiers(team_storage, [(None,'500kg 미만','5600'),('500kg 이상',None,'5300')])
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    rows=checked_rows(team_storage)
    assert {p.plan_type for p in result.plans}=={'KEEP','SWITCH','SPLIT'}
    expected={'400':('5600','5550000'),'500':('5300','5410000'),'600':('5300','5390000')}
    for qty,(price,total) in expected.items():
        row=next(r for r in rows if r['type']=='SPLIT' and supplier_line(r)['quantity']==qty)
        assert (supplier_line(row)['unit_price'],row['total_cost'])==(price,total)
    outcome=run_counter(team_storage,result)
    assert outcome.next_action=='HOLD' and outcome.unknown_fields==['create_mail_draft']
    offer=next(r['payload']['result'] for r in team_storage[3].records.values() if r['kind']=='OFFER_CALCULATION')
    assert offer['offer']['target_unit_price']=='5310'
    for row in offer['comparison']['plans_at_proposed_price']:
        for line in row['lines']:
            if line['supplier_id']=='supplier-b':
                assert line['tier_quantity']==line['quantity']
                assert line['unit_price']==('5600' if int(line['quantity'])<500 else '5300')
                assert line['unit_price_basis']=='QUOTE_TIER' and line['tier_source_ref']


@pytest.mark.parametrize('scope,target,split_price', [('ALL_INCUMBENT_CHANGEABLE','5416','5416'),
                                                   ('KEEP_ONLY','5460','5700')])
def test_incumbent_tier_and_explicit_repricing_scope_are_distinct(team_storage, scope, target, split_price):
    stored_tiers(team_storage, [(None,'500kg 미만','5700'),('500kg 이상',None,'5500')], qid='qv-a')
    team_storage[2].facts['repricing_scope']['value']=scope
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    initial=next(r for r in checked_rows(team_storage) if r['type']=='SPLIT' and supplier_line(r,'supplier-a')['quantity']=='400')
    assert supplier_line(initial,'supplier-a')['unit_price']=='5700' and initial['total_cost']=='5530000'
    assert initial['pricing_phase']=='QUOTE_AT_ALLOCATION' and initial['cost_function_scope']=='COUNTER_OFFER_REPRICING'
    run_counter(team_storage,result)
    offer=next(r['payload']['result'] for r in team_storage[3].records.values() if r['kind']=='OFFER_CALCULATION')
    assert offer['offer']['target_unit_price']==target
    split=next(r for r in offer['comparison']['plans_at_proposed_price'] if r['type']=='SPLIT' and supplier_line(r,'supplier-a')['quantity']=='400')
    line=supplier_line(split,'supplier-a')
    assert split['pricing_phase']=='COUNTER_OFFER_REPRICING'
    assert line['unit_price']==split_price and line['quoted_unit_price']=='5700'
    assert line['unit_price_basis']==('PROPOSED_PRICE' if scope=='ALL_INCUMBENT_CHANGEABLE' else 'QUOTE_TIER')


@pytest.mark.parametrize('intervals,method,action,reason', [
    ([(None,'500kg 이하','5600'),('500kg 이상',None,'5300')],ALL_UNITS,'NEEDS_EVIDENCE','TIER_OVERLAP'),
    ([(None,'500kg 미만','5600'),('500kg 초과',None,'5300')],ALL_UNITS,'NEEDS_EVIDENCE','TIER_GAP'),
    ([(None,'400kg 이하','5600'),('500kg 이상',None,'5300')],ALL_UNITS,'NEEDS_EVIDENCE','TIER_GAP'),
    ([(None,'500kg 미만','5600'),('500kg 이상',None,'5300')],'','NEEDS_EVIDENCE','TIER_METHOD_AMBIGUOUS'),
    ([(None,'500kg 미만','5600'),('500kg 이상',None,'5300')],GRADUATED,'HOLD','TIER_GRADUATED_UNSUPPORTED'),
    ([(None,'500kg 미만','5600'),('500kg 이상',None,'5300')],ALL_UNITS+'\n'+GRADUATED,'NEEDS_EVIDENCE','TIER_METHOD_AMBIGUOUS'),
    ([(None,'500kg','5600'),('500kg 이상',None,'5300')],ALL_UNITS,'NEEDS_EVIDENCE','TIER_BOUNDARY_AMBIGUOUS'),
])
def test_ambiguous_or_unpriced_tables_return_reason_without_calculation(team_storage, intervals, method, action, reason):
    stored_tiers(team_storage,intervals,method=method)
    with pytest.raises(MissingTeamData) as err:team_storage[4].snapshot(team_storage[1])
    assert err.value.code==reason
    result=run_plan(team_storage)
    assert result.next_action==action
    assert result.unknown_fields==([reason] if action=='HOLD' else ['price_tiers'])
    assert not team_storage[3].records


def test_wrong_stored_boundary_and_original_row_are_reextracted(team_storage):
    row=stored_tiers(team_storage,[(None,'500kg 미만','5600'),('500kg 이상',None,'5300')])
    row.pricing_terms_jsonb['price_tiers'][0]['max_quantity']='499'
    team_storage[2].refresh_binding()
    result=run_plan(team_storage)
    assert result.next_action=='RECHECK_SOURCE' and result.unknown_fields==['price_tiers']
    assert not team_storage[3].records


def test_moq_capacity_and_tier_boundary_are_checked_together(team_storage):
    stored_tiers(team_storage,[('500kg 이상','600kg 이하','5300')],moq='500',capacity='600')
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    amounts={a.quantity for p in result.plans for a in p.allocations if a.supplier_id=='supplier-b'}
    assert amounts=={'500','600'}
    stored=next(r['payload']['bundle'] for r in team_storage[3].records.values() if r['kind']=='PLAN_SET')
    assert any(i['code']=='QUANTITY_NOT_FEASIBLE' for p in stored['plans'] if p['status']=='INVALID' for i in p['issues'])


def test_tier_coverage_does_not_extrapolate_to_unpriced_feasible_moq(team_storage):
    stored_tiers(team_storage,[('500kg 이상','600kg 이하','5300')],moq='400',capacity='600')
    result=run_plan(team_storage)
    assert result.next_action=='NEEDS_EVIDENCE' and result.unknown_fields==['price_tiers']
    assert not team_storage[3].records


def test_off_grid_boundaries_are_disclosed_not_silently_added_or_claimed_optimal(team_storage):
    stored_tiers(team_storage,[(None,'450kg 미만','5600'),('450kg 이상',None,'5300')])
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    bundle=next(r['payload']['bundle'] for r in team_storage[3].records.values() if r['kind']=='PLAN_SET')
    search=bundle['tier_search']
    assert search['allocation_step']=='100' and not search['global_optimum_claimed']
    assert {'quote_id':'qv-b','quantity':'450'} in search['off_grid_boundaries']
    assert not any(a.quantity=='450' for p in result.plans for a in p.allocations)
    assert all(any('off-grid' in condition for condition in p.conditions) for p in result.plans)


def test_independent_checker_rejects_wrong_operating_selector(team_storage,monkeypatch):
    from app.agents.kan60.purchase_plans import tools as operating
    stored_tiers(team_storage,[(None,'500kg 미만','5600'),('500kg 이상',None,'5300')])
    original=operating.quoted_price
    def wrong(q,quantity):
        price,index=original(q,quantity)
        return (price+Fraction(1),index) if q.get('price_tiers') else (price,index)
    monkeypatch.setattr(operating,'quoted_price',wrong)
    result=run_plan(team_storage)
    assert result.next_action=='HOLD' and result.unknown_fields==['INDEPENDENT_PLAN_COST_MISMATCH']
    assert not team_storage[3].records


def test_incumbent_cannot_supply_basis_quantity_is_held_without_invented_price(team_storage):
    stored_tiers(team_storage,[('500kg 이상','600kg 이하','5500')],qid='qv-a',moq='500',capacity='600')
    result=run_plan(team_storage)
    assert result.next_action=='HOLD' and result.unknown_fields==['KEEP_QUANTITY_NOT_FEASIBLE']
    assert not team_storage[3].records


def test_tier_price_must_still_be_in_its_original_row(team_storage):
    row=stored_tiers(team_storage,[(None,'500kg 미만','5600'),('500kg 이상',None,'5300')])
    team_storage[0].mail_messages[row.source_mail_id]['body_text']=team_storage[0].mail_messages[row.source_mail_id]['body_text'].replace('5300원/kg','5301원/kg')
    result=run_plan(team_storage)
    assert result.next_action=='RECHECK_SOURCE' and result.unknown_fields==['price_tiers']
    assert not team_storage[3].records
