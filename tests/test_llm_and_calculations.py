import json
from decimal import Decimal as D

import httpx
import pytest

from app.features.review.domain.calculations import all_rounds_match, evaluate
from app.core.config import Settings
from app.core.errors import ReviewRequired, ServiceError
from app.shared.infrastructure.llm import JsonAgent
from app.features.review.domain.schemas import CalculationProposal
from app.features.mail.domain.schemas import Classification
from app.features.review.application.pipeline import paired_costs, request_costs


def test_llm_adapter_uses_configured_model_validates_json_and_does_not_store():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['model'] == 'team-model' and body['store'] is False
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"status":"TRUE","reason":"구매 요청"}'}, 'finish_reason': 'stop'}]})
    settings = Settings(_env_file=None, llm_base_url='https://provider.example/v1', llm_api_key='secret', llm_model='team-model')
    agent = JsonAgent(settings, httpx.MockTransport(handler))
    assert agent.run('classification', 'classify', {'body': '구매'}, Classification).status == 'TRUE'
    assert len(calls[0]['messages']) == 2


@pytest.mark.parametrize('choice', [
    {'message': {'content': '{}'}, 'finish_reason': 'stop'},
    {'message': {'content': '{}', 'refusal': 'refused'}, 'finish_reason': 'stop'},
    {'message': {'content': '{}'}, 'finish_reason': 'length'},
])
def test_incomplete_or_invalid_model_response_never_becomes_success(choice):
    settings = Settings(_env_file=None, llm_base_url='https://provider.example/v1', llm_api_key='secret', llm_model='team-model')
    agent = JsonAgent(settings, httpx.MockTransport(lambda r: httpx.Response(200, json={'choices': [choice]})))
    with pytest.raises(ServiceError):
        agent.run('classification', 'classify', {}, Classification)


def test_round_mismatch_is_not_majority_vote():
    with pytest.raises(ReviewRequired):
        all_rounds_match([{'total': D('1')}, {'total': D('1')}, {'total': D('2')}], 3)


@pytest.mark.parametrize('bad', ['2026년 11월 월정액 100kg', '2026년 11월 월정액', 'TOTAL'])
def test_monthly_cost_never_uses_substring_quantity_or_unconfirmed_basis(bad):
    fields = {'cost_basis': bad, 'cost_payer': 'BUYER', 'cost_included_in_price': 'FALSE',
              'transport_amount': '100원', 'other_logistics_amount': '20원'}
    from datetime import date
    with pytest.raises(ReviewRequired):
        request_costs(fields, {}, D('1000'), date(2026, 11, 1), date(2026, 11, 30))
