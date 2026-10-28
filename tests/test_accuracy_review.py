from copy import deepcopy
from decimal import Decimal as D
from random import Random
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.features.review.domain.calculations import evaluate
from app.core.errors import ReviewRequired
from app.features.review.domain.schemas import CalculationProposal, FormulaOption, FormulaTerm
from app.features.review.domain.responses import AnalysisResponse
from app.features.review.domain.validation import decimal_value, normalize_extraction
from test_pipeline import extracted, pipeline_fixture


def normalize(values):
    extraction = extracted()
    for field in extraction.fields:
        if field.field_name in values:
            field.raw_value = values[field.field_name]
    document = '\n'.join(field.raw_value for field in extraction.fields if field.raw_value)
    return normalize_extraction(extraction, document)


@pytest.mark.parametrize('raw', ['5,00,0원', '1,00kg', ',100원', '100,원', '1,,000원'])
def test_bad_thousands_grouping_is_not_silently_corrected(raw):
    with pytest.raises(ReviewRequired):
        decimal_value(raw, 'number')


@pytest.mark.parametrize('raw', ['5,000원', '5000원', '5,000.25원'])
def test_valid_number_grouping(raw):
    assert decimal_value(raw, 'number') == D(raw.replace(',', '').replace('원', ''))


@pytest.mark.parametrize('quantity,expected', [('100kg', '100'), ('100박스', '1000'), ('100', '1000')])
def test_box_price_does_not_multiply_kg_quantity_again(quantity, expected):
    result = normalize({'unit': '박스', 'order_unit': '박스', 'net_kg_per_order_unit': '10kg',
                        'old_unit_price': '50,000원/박스', 'new_unit_price': '55,000원/박스',
                        'requested_quantity': quantity})
    assert result['requested_quantity'] == D(expected)
    assert result['new_unit_price'] == D('5500')


def test_incompatible_quantity_unit_is_rejected():
    with pytest.raises(ReviewRequired):
        normalize({'requested_quantity': '100개'})


def test_box_is_not_treated_as_kg_conversion():
    with pytest.raises(ReviewRequired):
        normalize({'unit': '박스', 'net_kg_per_order_unit': '10박스'})


def successful_result():
    pipeline, source, _ = pipeline_fixture()
    return pipeline.run('W-20261026-001', source, lambda _: None, MagicMock())


@pytest.mark.parametrize('problem', ['negative_quantity', 'missing_quantity', 'zero_quantity', 'unknown_after',
                                    'wrong_status', 'negative_price', 'reverse_period', 'unconfirmed_baseline', 'bad_rounds'])
def test_inconsistent_success_never_crosses_response_boundary(problem):
    result = deepcopy(successful_result())
    if problem == 'negative_quantity':
        result['quantities'] = {'fixed': D('-1'), 'changeable': D('1001'), 'total': D('1000')}
    elif problem == 'missing_quantity':
        result['quantities'] = {'total': D('0')}
    elif problem == 'zero_quantity':
        result['quantities'] = dict.fromkeys(['fixed', 'changeable', 'total'], D('0'))
    elif problem in ('unknown_after', 'unconfirmed_baseline'):
        target = result['cards'][0] if problem == 'unknown_after' else result['baseline']
        for name in ('transport_amount', 'other_logistics_amount', 'total_cost', 'difference_from_before'):
            target[name] = None
        if problem == 'unknown_after':
            target['calculation_status'] = 'COST_UNCONFIRMED'
    elif problem == 'wrong_status':
        result['cards'][1]['calculation_status'] = 'VERIFIED'
    elif problem == 'negative_price':
        result['cards'][0]['changeable_unit_price'] = D('-1')
    elif problem == 'reverse_period':
        result['period'] = {'from': '2026-11-30', 'to': '2026-11-01'}
    elif problem == 'bad_rounds':
        result['calculation_verification']['rounds'] = '3'
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(result)


def test_thousand_seeded_financial_cases_match_integer_oracle():
    # Integer cents x integer milli-kg, rounded once after summing all goods.
    random = Random(20261026)
    expected = {name: {'goods': [('p', 'q'), ('p2', 'q2')], 'transport_keys': ['t'],
                       'other_logistics_keys': ['o'], 'cost_confirmed': True} for name in ('BEFORE', 'AFTER')}
    proposal = CalculationProposal(options=[FormulaOption(option_id=name,
        goods=[FormulaTerm(value_key=p, quantity_key=q) for p, q in spec['goods']],
        transport_keys=['t'], other_logistics_keys=['o']) for name, spec in expected.items()])
    for _ in range(1000):
        p, p2 = random.randrange(10**13), random.randrange(10**13)
        q, q2 = random.randrange(10**14), random.randrange(10**14)
        t, o = random.randrange(10**10), random.randrange(10**10)
        registry = {'p': D(p) / 100, 'p2': D(p2) / 100, 'q': D(q) / 1000,
                    'q2': D(q2) / 1000, 't': D(t) / 100, 'o': D(o) / 100}
        result = evaluate(proposal, registry, expected)
        goods_cents = (p * q + p2 * q2 + 500) // 1000
        assert result['AFTER']['goods_amount'] == D(goods_cents) / 100
        assert result['AFTER']['total_cost'] == D(goods_cents + t + o) / 100
        assert result['AFTER']['difference_from_before'] == 0
