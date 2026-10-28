from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.core.errors import ReviewRequired
from app.features.review.domain.schemas import CalculationProposal, CardText, CardTexts, ComparisonVerdict, ReferenceDecision, ExtractedField, Extraction, FIELD_NAMES, FormulaOption, FormulaTerm, ReviewSummary, Verdict
from app.features.mail.domain.schemas import Classification
from app.features.review.application.pipeline import AnalysisPipeline


def fixture_data():
    supplier = {'supplier_id': 'SUP-A', 'supplier_name': 'A사', 'certifications': None, 'experience_comment': None, 'source_ref': 'supplier.txt'}
    other = {**supplier, 'supplier_id': 'SUP-B', 'supplier_name': 'B사'}
    contracts = [dict(contract_id='C1', contract_no='CON', supplier_id='SUP-A', item_id='I1', valid_from='2026-11-01', valid_to='2026-11-10',
                      price_rule='FIXED', unit_price=D('5000'), adjustment_clause='400kg 고정단가', source_ref='contract.txt'),
                 dict(contract_id='C2', contract_no='CON', supplier_id='SUP-A', item_id='I1', valid_from='2026-11-11', valid_to='2026-11-30',
                      price_rule='CHANGEABLE', unit_price=D('5000'), adjustment_clause='600kg 변경 가능', source_ref='contract.txt')]
    plans = [dict(plan_id='P1', contract_id='C1', delivery_date='2026-11-05', quantity=D('400'), source_ref='plan1.txt'),
             dict(plan_id='P2', contract_id='C2', delivery_date='2026-11-20', quantity=D('600'), source_ref='plan2.txt')]
    quote = dict(reference_id='Q1', reference_type='QUOTE', contract_id=None, supplier_id='SUP-B', item_id='I1',
                 reference_date='2020-01-01', valid_until='2099-12-31', unit_price=D('5200'), quantity=None,
                 moq=D('500'), unit='kg', currency='원', lead_days=7, delivery_terms='도착도', trade_stage='도매', source_ref='quote.txt')
    costs = [dict(cost_term_id=i, contract_no='CON', reference_id=None, cost_type=kind, amount=amount, basis='MONTHLY_FIXED',
                  payer='BUYER', included_in_price='FALSE', coverage_from='2026-11-01', coverage_to='2026-11-30',
                  coverage_quantity=D('1000'), source_ref='cost.txt')
             for i, (kind, amount) in enumerate([('TRANSPORT', D('100000')), ('OTHER_LOGISTICS', D('20000'))])]
    return {'suppliers': [supplier, other], 'contracts': contracts, 'purchase_plans': plans, 'price_references': [quote], 'cost_terms': costs}


class References:
    def __init__(self, data):
        self.data = data
    def match_supplier(self, name):
        return 'SUP-A'
    def match_item(self, **kwargs):
        return 'I1'
    def read(self, table, **equals):
        return [row for row in deepcopy(self.data.get(table, [])) if all(row.get(key) == value for key, value in equals.items())]


def extracted():
    values = {'supplier_name': 'A사', 'item_name': '딸기', 'origin': '국산', 'grade': 'A등급', 'processing': '냉동',
              'old_unit_price': '5,000원/kg', 'new_unit_price': '5,500원/kg', 'unit': 'kg', 'currency': '원',
              'effective_date': '2026년 11월 1일', 'reason': '원가 인상', 'cost_basis': '기존 계약과 동일',
              'transport_amount': '100,000원', 'other_logistics_amount': '20,000원', 'cost_payer': '구매사 부담',
              'cost_included_in_price': '단가 별도'}
    return Extraction(item_count=1, fields=[ExtractedField(field_name=name, raw_value=values.get(name), source_location='메일 본문 1문단') for name in FIELD_NAMES])


class Agent:
    def __init__(self, extraction=None, reject=None, corrupt_round=False):
        self.extraction, self.reject, self.corrupt_round = extraction or extracted(), reject, corrupt_round
        self.calls = []
    def run(self, role, instruction, data, output, deadline=None):
        self.calls.append((role, deepcopy(data)))
        if role == 'extraction':
            return self.extraction
        if output is Verdict:
            return Verdict(passed=role != self.reject, reason='원문 근거 확인' if role != self.reject else '원문 불일치', sources=['contract.txt'])
        if output is ComparisonVerdict:
            return ComparisonVerdict(decisions=[ReferenceDecision(reference_id=ref['reference_id'], comparable=role != self.reject,
                                      reason='비교 가능' if role != self.reject else '배송 조건 불일치', unconfirmed_conditions=[],
                                      sources=[ref['source_ref']]) for ref in data['references']])
        if output is ReviewSummary:
            return ReviewSummary(summary='계약과 후보 비교', sources=['contract.txt'])
        if output is CalculationProposal:
            options = [FormulaOption(option_id=key, goods=[FormulaTerm(value_key=p, quantity_key=q) for p, q in spec['goods']],
                                     transport_keys=spec['transport_keys'], other_logistics_keys=spec['other_logistics_keys'])
                       for key, spec in data['option_requirements'].items()]
            if self.corrupt_round:
                options[0].goods.pop()
            return CalculationProposal(options=options)
        if output is CardTexts:
            return CardTexts(cards=[CardText(option_id=c['option_id'], title='검토 방안', explanation='근거를 확인해 주세요.') for c in data['cards']])
        raise AssertionError(role)


def pipeline_fixture(data=None, agent=None):
    agent = agent or Agent()
    data = data or fixture_data()
    body = '\n'.join(field.raw_value for field in agent.extraction.fields if field.raw_value)
    client, storage = MagicMock(), MagicMock()
    client.storage.from_.return_value.download.return_value = '계약 고정 400kg 변경 가능 600kg 월정액 120000원'.encode()
    settings = Settings(_env_file=None, calculation_rounds=3)
    pipeline = AnalysisPipeline(settings, agent, References(data), storage, client)
    return pipeline, {'mail_body': body, 'attachments': []}, agent


def test_full_pipeline_sample_and_three_separate_calculations():
    pipeline, source, agent = pipeline_fixture()
    on_item, lease = MagicMock(), MagicMock()
    result = pipeline.run('W-20261026-001', source, on_item, lease)
    assert result['outcome'] == 'SUCCESS'
    assert result['quantities'] == {'total': D('1000'), 'fixed': D('400'), 'changeable': D('600')}
    assert result['baseline']['total_cost'] == D('5120000.00')
    after, candidate = result['cards']
    assert after['total_cost'] == D('5420000.00')
    assert after['difference_from_before'] == D('300000.00')
    assert candidate['goods_amount'] == D('5120000.00') and candidate['total_cost'] is None
    assert candidate['difference_from_before'] is None
    rounds = [data for role, data in agent.calls if role == 'calculation']
    assert len(rounds) == 3 and rounds[0] == rounds[1] == rounds[2]
    assert all('tool_results' not in data for data in rounds)
    on_item.assert_called_once_with('I1')
    assert result['result_persisted'] is False


@pytest.mark.parametrize('problem', ['missing_date', 'multi_item', 'invented_value'])
def test_invalid_requests_stop_before_reference_and_calculation(problem):
    extraction = extracted()
    if problem == 'multi_item':
        extraction.item_count = 2
    elif problem == 'missing_date':
        next(f for f in extraction.fields if f.field_name == 'effective_date').raw_value = None
    pipeline, source, agent = pipeline_fixture(agent=Agent(extraction))
    if problem == 'invented_value':
        source['mail_body'] = source['mail_body'].replace('5,500원/kg', '')
    with pytest.raises(ReviewRequired):
        pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert 'calculation' not in [role for role, _ in agent.calls]


def test_missing_baseline_cost_is_not_zero():
    data = fixture_data()
    data['cost_terms'][0]['amount'] = None
    pipeline, source, agent = pipeline_fixture(data)
    with pytest.raises(ReviewRequired, match='COST_UNCONFIRMED'):
        pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert 'calculation' not in [role for role, _ in agent.calls]


def test_no_candidate_is_valid_and_does_not_create_fake_cards():
    data = fixture_data()
    data['price_references'] = []
    pipeline, source, _ = pipeline_fixture(data)
    result = pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert len(result['cards']) == 1 and result['candidate_search']['status'] == 'NONE'


def test_moq_uses_changeable_not_total_quantity():
    data = fixture_data()
    data['price_references'][0]['moq'] = D('700')
    pipeline, source, _ = pipeline_fixture(data)
    result = pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert result['candidate_search']['status'] == 'NONE'
    assert 'MOQ' in result['candidate_search']['excluded'][0]['reason']


@pytest.mark.parametrize('role', ['request_validation', 'contract_validation', 'calculation_validation'])
def test_failed_independent_verification_never_composes_cards(role):
    pipeline, source, agent = pipeline_fixture(agent=Agent(reject=role))
    with pytest.raises(ReviewRequired):
        pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert 'card_composition' not in [r for r, _ in agent.calls]


def test_formula_missing_quantity_never_retries_until_agreement():
    pipeline, source, agent = pipeline_fixture(agent=Agent(corrupt_round=True))
    with pytest.raises(ReviewRequired):
        pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert len([r for r, _ in agent.calls if r == 'calculation']) == 1


def test_comparison_excludes_candidate_without_stopping_valid_after_card():
    pipeline, source, agent = pipeline_fixture(agent=Agent(reject='comparison_validation'))
    result = pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
    assert result['outcome'] == 'SUCCESS'
    assert [card['type'] for card in result['cards']] == ['AFTER']
    assert result['candidate_search']['status'] == 'NONE'
    assert result['candidate_search']['excluded'][0]['reason'] == '배송 조건 불일치'


def test_price_basis_mismatch_is_not_silently_normalized():
    extraction = extracted()
    next(field for field in extraction.fields if field.field_name == 'new_unit_price').raw_value = '5,500원/박스'
    pipeline, source, _ = pipeline_fixture(agent=Agent(extraction))
    with pytest.raises(ReviewRequired, match='PRICE_UNIT_MISMATCH'):
        pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())


def test_claimed_attachment_location_cannot_borrow_values_from_mail_body():
    extraction = extracted()
    next(field for field in extraction.fields if field.field_name == 'new_unit_price').source_location = 'missing.pdf 1페이지'
    pipeline, source, _ = pipeline_fixture(agent=Agent(extraction))
    with pytest.raises(ReviewRequired, match='SOURCE_LOCATION_INVALID'):
        pipeline.run('W-20261026-001', source, MagicMock(), MagicMock())
