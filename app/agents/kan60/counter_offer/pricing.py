"""Shared exact price-grid selection; no plan selection or provider dependency."""
from decimal import Decimal, localcontext
from fractions import Fraction
from .contract import require


def select_price(ceiling, increment, minimum=Decimal(0), explicit=None):
    cap, step = Fraction(ceiling), Fraction(increment)
    chosen = Fraction(explicit) if explicit is not None else (cap // step) * step
    require(chosen > 0, 'NO_FEASIBLE_PRICE', 'policy', '확인된 비용·정책으로 양수 목표 단가를 만들 수 없습니다.', 'HOLD')
    require(chosen >= Fraction(minimum), 'NO_FEASIBLE_PRICE', 'policy.min_unit_price',
            '비교/정책 상한을 만족하는 목표가가 정책 하한보다 낮습니다.', 'HOLD')
    require(chosen <= cap and chosen % step == 0, 'POLICY_VIOLATION', 'policy.explicit_target_price',
            '명시된 목표가 상한·요청가·가격 증분 조건을 위반합니다. 임의로 고치지 않고 보류합니다.', 'HOLD')
    with localcontext() as ctx:
        ctx.prec = 80
        return Decimal(chosen.numerator) / Decimal(chosen.denominator)
