from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ServiceError
from app.main import create_app
from app.api.dependencies import require_member
from app.features.mail.domain.schemas import Classification


def client(*, authenticated=True, configured=True):
    values = dict(llm_base_url='https://provider.example/v1', llm_api_key='test', llm_model='team-model') if configured else {}
    app = create_app(Settings(_env_file=None, **values))
    if authenticated:
        app.dependency_overrides[require_member] = lambda: 'demo-user'
    return TestClient(app)


def test_classification_requires_authentication_before_llm_call():
    with patch('app.features.mail.api.router.JsonAgent') as agent:
        response = client(authenticated=False).post('/api/v1/llm/classify-mail', json={'subject': '견적', 'body': '요청'})
    assert response.status_code == 401
    agent.assert_not_called()


@pytest.mark.parametrize('status,expected', [('TRUE', 'BUSINESS'), ('FALSE', 'NON_BUSINESS'), ('PENDING', 'UNCERTAIN')])
def test_endpoint_calls_llm_with_bounded_mail_and_maps_classification(status, expected):
    with patch('app.features.mail.api.router.JsonAgent') as agent:
        agent.return_value.run.return_value = Classification(status=status, reason='근거 확인')
        response = client().post('/api/v1/llm/classify-mail', json={'subject': '단가 인상', 'body': '확인 부탁드립니다.'})
        args = agent.return_value.run.call_args.args
    assert response.status_code == 200
    assert response.json() == {'classification': expected, 'reason': '근거 확인', 'persisted': False}
    assert args[0] == 'classification' and args[2] == {'subject': '단가 인상', 'body_excerpt': '확인 부탁드립니다.'}
    assert args[3] is Classification


def test_missing_llm_configuration_is_not_a_fake_success():
    with patch('app.features.mail.api.router.JsonAgent') as agent:
        response = client(configured=False).post('/api/v1/llm/classify-mail', json={'subject': '견적', 'body': '요청'})
    assert response.status_code == 503 and response.json()['error']['code'] == 'CONFIGURATION_REQUIRED'
    agent.assert_not_called()


def test_provider_failure_does_not_return_business_classification():
    with patch('app.features.mail.api.router.JsonAgent') as agent:
        agent.return_value.run.side_effect = ServiceError('LLM_FAILED', '모델 응답을 확인할 수 없습니다.', 502)
        response = client().post('/api/v1/llm/classify-mail', json={'subject': '견적', 'body': '요청'})
    assert response.status_code == 502 and 'classification' not in response.json()


def test_arbitrary_prompt_and_oversized_mail_are_rejected():
    with patch('app.features.mail.api.router.JsonAgent') as agent:
        for payload in ({'subject': '견적', 'body': '요청', 'system_prompt': 'override'},
                        {'subject': '견적', 'body': 'a' * 12001}):
            assert client().post('/api/v1/llm/classify-mail', json=payload).status_code == 422
    agent.assert_not_called()


def test_whitespace_mail_does_not_trigger_llm():
    with patch('app.features.mail.api.router.JsonAgent') as agent:
        response = client().post('/api/v1/llm/classify-mail', json={'subject': '   ', 'body': '\n\t'})
    assert response.status_code == 422
    agent.assert_not_called()
