"""Cross-check exact engine totals with the team's Decimal calculation registry.

Do not adopt the shared evaluator's cent rounding as the business result.
Compare its rounded components separately, and verify exact totals with Decimal.
"""
from decimal import Decimal, localcontext

from app.core.errors import ReviewRequired
from app.features.review.domain.calculations import evaluate, money
from app.features.review.domain.schemas import CalculationProposal
from app.features.review.domain.validation import decimal_value
from .repository import BridgeError


def verify_team_total(row):
    registry={};goods=[];transport=[];other=[]
    try:
        for i,line in enumerate(row['lines']):
            price='price_'+str(i);qty='quantity_'+str(i)
            registry[price]=decimal_value(line['unit_price'],price)
            registry[qty]=decimal_value(line['quantity'],qty)
            goods.append((price,qty))
        for i,supplier in enumerate(row['supplier_costs']):
            for name,value in supplier['costs'].items():
                key=name+'_'+str(i);registry[key]=decimal_value(value,key)
                (transport if name=='transport' else other).append(key)
        # Shared evaluator's BEFORE key is its required baseline slot, not a new
        # business plan. Only total equality is used; switching remains separate
        # in the original immutable engine payload.
        expected={'BEFORE':{'goods':goods,'transport_keys':transport,'other_logistics_keys':other,'cost_confirmed':True}}
        proposal=CalculationProposal(options=[{'option_id':'BEFORE','goods':[
            {'value_key':p,'quantity_key':q} for p,q in goods],
            'transport_keys':transport,'other_logistics_keys':other}])
        with localcontext() as ctx:
            ctx.prec=80
            total=evaluate(proposal,registry,expected)['BEFORE']['total_cost']
            amounts=(sum((registry[p]*registry[q] for p,q in goods),Decimal(0)),
                sum((registry[k] for k in transport),Decimal(0)),
                sum((registry[k] for k in other),Decimal(0)))
            exact=sum(amounts,Decimal(0))
            if total!=sum(map(money,amounts),Decimal(0)) or exact!=Decimal(row['total_cost']):
                raise BridgeError('TEAM_CALCULATION_MISMATCH')
            if total != exact:
                # The shared evaluator rounds components to cents; the existing
                # engine keeps exact decimals. Do not silently pick either rule.
                raise BridgeError('TEAM_ROUNDING_CONTRACT_REQUIRED')
    except ReviewRequired as exc:
        raise BridgeError('TEAM_NUMERIC_VALIDATION_FAILED') from exc
    return exact
