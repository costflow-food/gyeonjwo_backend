"""Only registry-backed multiply and sum; no eval, arbitrary expressions or credentials."""
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP

from app.core.errors import ReviewRequired

ZERO = Decimal('0')
MONEY = Decimal('0.01')


def money(value):
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def evaluate(proposal, registry, expected):
    if len(proposal.options) != len(expected) or {x.option_id for x in proposal.options} != set(expected):
        raise ReviewRequired('INVALID_FORMULA', '계산 대상 방안이 일치하지 않습니다.')
    result = {}
    for option in proposal.options:
        canonical = expected[option.option_id]
        terms = [(term.value_key, term.quantity_key) for term in option.goods]
        if (Counter(terms) != Counter(canonical['goods'])
                or Counter(option.transport_keys) != Counter(canonical['transport_keys'])
                or Counter(option.other_logistics_keys) != Counter(canonical['other_logistics_keys'])):
            raise ReviewRequired('INVALID_FORMULA', '계산 입력 누락·중복 또는 적용 물량이 일치하지 않습니다.')
        goods = money(sum((registry[price] * registry[quantity] for price, quantity in terms), ZERO))
        transport = money(sum((registry[key] for key in option.transport_keys), ZERO))
        other = money(sum((registry[key] for key in option.other_logistics_keys), ZERO))
        total = goods + transport + other if canonical['cost_confirmed'] else None
        result[option.option_id] = {'goods_amount': goods, 'transport_amount': transport if canonical['cost_confirmed'] else None,
                                    'other_logistics_amount': other if canonical['cost_confirmed'] else None,
                                    'total_cost': total}
    baseline = result['BEFORE']['total_cost']
    for value in result.values():
        value['difference_from_before'] = None if value['total_cost'] is None else value['total_cost'] - baseline
    return result


def all_rounds_match(rounds, count):
    if len(rounds) != count or count < 2 or any(result != rounds[0] for result in rounds[1:]):
        raise ReviewRequired('CALCULATION_MISMATCH', '독립 계산 회차의 항목별 금액이 일치하지 않습니다.')
    return rounds[0]
