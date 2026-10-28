"""Local calculation inspector: python -m uvicorn scripts.calculation_viewer:app --host 127.0.0.1 --port 8150."""
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict

from app.features.review.domain.calculations import evaluate
from app.features.review.domain.schemas import CalculationProposal, FormulaOption, FormulaTerm

app = FastAPI(title='견줘 계산 검증 화면', docs_url=None, redoc_url=None)


class Inputs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fixed: Decimal = Field(default=Decimal('400'), ge=0, le=1000000000, decimal_places=3)
    changeable: Decimal = Field(default=Decimal('600'), ge=0, le=1000000000, decimal_places=3)
    old_price: Decimal = Field(default=Decimal('5000'), ge=0, le=1000000000, decimal_places=2)
    new_price: Decimal = Field(default=Decimal('5500'), ge=0, le=1000000000, decimal_places=2)
    transport: Decimal = Field(default=Decimal('100000'), ge=0, le=1000000000, decimal_places=2)
    other: Decimal = Field(default=Decimal('20000'), ge=0, le=1000000000, decimal_places=2)


@app.get('/')
def index():
    return FileResponse(Path(__file__).with_name('calculation_viewer.html'))


@app.post('/calculate')
def calculate(values: Inputs):
    registry = values.model_dump()
    expected = {}
    for name, change_price in [('BEFORE', 'old_price'), ('AFTER', 'new_price')]:
        expected[name] = {'goods': [('old_price', 'fixed'), (change_price, 'changeable')],
                          'transport_keys': ['transport'], 'other_logistics_keys': ['other'], 'cost_confirmed': True}
    proposal = CalculationProposal(options=[FormulaOption(option_id=name,
        goods=[FormulaTerm(value_key=p, quantity_key=q) for p, q in spec['goods']],
        transport_keys=spec['transport_keys'], other_logistics_keys=spec['other_logistics_keys'])
        for name, spec in expected.items()])
    result = evaluate(proposal, registry, expected)
    return {'inputs': {k: str(v) for k, v in registry.items()},
            'steps': {name: [{'price': str(registry[p]), 'quantity': str(registry[q]),
                              'product': str(registry[p] * registry[q])} for p, q in spec['goods']]
                      for name, spec in expected.items()},
            'result': {name: {k: str(v) if v is not None else None for k, v in row.items()}
                       for name, row in result.items()}}
