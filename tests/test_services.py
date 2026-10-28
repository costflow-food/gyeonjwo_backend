from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.shared.infrastructure.db.contracts import OwnershipLost
from app.core.errors import ReviewRequired, ServiceError
from app.features.review.application.services import Services

WORK = 'W-20261026-001'


def service_for_test():
    settings = Settings(_env_file=None, llm_base_url='https://llm.example/v1', llm_api_key='test', llm_model='configured-model')
    service = Services(settings, MagicMock())
    service.work = MagicMock(return_value={'queue_status': 'TRUE'})
    service.source = MagicMock(return_value={})
    service.raw = MagicMock(return_value=b'mail')
    service.verify_source = MagicMock()
    service.rpc = MagicMock()
    service.rpc.call.return_value = {'run_id': 'current-run'}
    service.pipeline = MagicMock()
    return service


@pytest.mark.parametrize('outcome,exception,status', [
    ('SUCCESS', None, 200), ('REVIEW_REQUIRED', ReviewRequired('MISSING_FIELD', '적용일 없음'), 200),
    ('FAILED', ServiceError('LLM_TIMEOUT', '모델 시간 초과', 502), 502),
])
def test_analysis_finishes_queue_for_each_outcome(outcome, exception, status):
    service = service_for_test()
    if exception:
        service.pipeline.run.side_effect = exception
    else:
        service.pipeline.run.return_value = {'outcome': 'SUCCESS', 'cards': [], 'result_persisted': False}
    response_status, result = service.analyze(WORK)
    assert response_status == status
    assert result['outcome'] == outcome and result['queue_status'] == 'COMPLETE'
    service.rpc.call.assert_any_call('complete_analysis', p_work_id=WORK, p_run_id='current-run')
    assert not service.analysis_lock.locked()


def test_completion_failure_hides_result_and_does_not_claim_complete():
    service = service_for_test()
    service.pipeline.run.return_value = {'outcome': 'SUCCESS', 'cards': ['would-be-card']}
    def rpc(name, **params):
        if name == 'complete_analysis':
            raise OwnershipLost('expired')
        return {'run_id': 'current-run'}
    service.rpc.call.side_effect = rpc
    with pytest.raises(ServiceError) as error:
        service.analyze(WORK)
    assert error.value.code == 'QUEUE_UPDATE_FAILED'
    assert not service.analysis_lock.locked()


def test_same_work_already_running_never_starts_another_analysis():
    service = service_for_test()
    service.work.return_value['queue_status'] = 'RUNNING'
    with pytest.raises(ServiceError) as error:
        service.analyze(WORK)
    assert error.value.status == 409
    service.rpc.call.assert_not_called()


def test_other_work_does_not_overlap_in_single_process():
    service = service_for_test()
    service.analysis_lock.acquire()
    with pytest.raises(ServiceError) as error:
        service.analyze(WORK)
    assert error.value.code == 'ANALYSIS_BUSY'
    service.rpc.call.assert_not_called()
    service.analysis_lock.release()


@pytest.mark.parametrize('state', ['RUNNING', 'COMPLETE'])
def test_skip_never_cancels_active_or_finished_analysis(state):
    service = service_for_test()
    service.work.return_value['queue_status'] = state
    with pytest.raises(ServiceError):
        service.skip(WORK)
    service.rpc.call.assert_not_called()
