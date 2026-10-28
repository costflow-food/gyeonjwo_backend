"""Additional verification of the existing staged common-contract implementation."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_api_contract import make_client, WORK


@pytest.mark.parametrize('path', ['/health', '/missing', '/api/v1/work-items/bad-id'])
def test_success_and_errors_are_private_and_not_cached(path):
    client, _ = make_client()
    response = client.get(path)
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'


def test_parallel_requests_keep_independent_request_ids():
    client, service = make_client()
    service.notes.return_value = {'work_id': WORK, 'notes': '검증'}
    def send(_):
        response = client.patch('/api/v1/work-items/' + WORK + '/notes', json={'notes': '검증'})
        assert response.status_code == 200
        assert response.json()['meta']['request_id'] == response.headers['x-request-id']
        return response.headers['x-request-id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(send, range(8)))
    assert len(set(ids)) == 8


def test_openapi_validation_error_matches_runtime_contract():
    client, _ = make_client()
    schema = client.get('/openapi.json').json()
    operation = schema['paths']['/api/v1/work-items/{work_id}/notes']['patch']
    assert operation['responses']['422']['content']['application/json']['schema'] == {
        '$ref': '#/components/schemas/ErrorEnvelope'}


def test_method_not_allowed_preserves_allow_header():
    client, _ = make_client()
    response = client.delete('/api/v1/work-items/' + WORK)
    assert response.status_code == 405
    assert response.json()['error']['code'] == 'METHOD_NOT_ALLOWED'
    assert 'GET' in response.headers['allow']
