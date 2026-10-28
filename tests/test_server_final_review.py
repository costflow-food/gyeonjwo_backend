from unittest.mock import patch
from fastapi.testclient import TestClient
from app.features.auth.application.auth import AuthUnavailable
from app.core.config import Settings
from app.main import create_app


def test_authentication_outage_is_not_reported_as_missing_configuration():
    app = create_app(Settings(_env_file=None))
    with patch('app.api.dependencies.verify_member', side_effect=AuthUnavailable('private upstream detail')):
        response = TestClient(app).post('/api/v1/llm/classify-mail',
            headers={'Authorization': 'Bearer test'}, json={'subject': 'request', 'body': 'quote'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'AUTH_UNAVAILABLE'
    assert 'private upstream' not in response.text
