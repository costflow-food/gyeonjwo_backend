"""선택 워커(RFQ 기록·발송, 그래프)의 기동과 재시도.

이 워커들은 DB 계약(헬스 함수)이 맞아야 뜬다. 부팅 순간 계약이 어긋나 있으면 워커 없이 API만 뜨고,
계약이 고쳐진 뒤에는 재배포 없이 올라오도록 주기적으로 다시 시도한다.
"""
import asyncio
import logging

from anyio import to_thread

from app.features.mailboxes.api.router import make_mailbox_service
from app.shared.infrastructure.db.client import make_server_client

logger = logging.getLogger('gyeonjwo')
# 부팅 때 못 뜬 워커를 다시 시도하는 간격(초).
WORKER_RETRY_SECONDS = 60


def start_metadata_worker(settings, mailbox_service):
    """보낸 RFQ의 메일 헤더를 기록하는 워커를 만든다. 스키마가 없으면 경고만 남기고 None을 돌려준다."""
    from app.features.work_cases.infrastructure.rfq_repository import RfqRepository
    from app.features.work_cases.application.rfq_metadata_worker import RfqMetadataWorker
    repo = RfqRepository(make_server_client(settings))
    try:
        repo.require_schema()
    except Exception:
        logger.warning('rfq metadata worker unavailable code=SCHEMA_REQUIRED; API remains available')
        return None
    return RfqMetadataWorker(repo, mailbox_service, repo.metadata_owners)


def start_workflow_worker(settings, mailbox_service):
    """RFQ 준비·발송 워커를 만든다. 발송 여부는 사용자별 발송 설정이 정한다. 스키마가 없으면 경고만 남기고 None."""
    from app.features.work_cases.infrastructure.rfq_delivery_repository import RfqDeliveryRepository
    from app.features.work_cases.application.rfq_preparation import RfqPreparation
    from app.features.work_cases.application.rfq_delivery import RfqDelivery
    from app.features.work_cases.application.rfq_workflow_worker import RfqWorkflowWorker
    from app.shared.infrastructure.llm import JsonAgent
    repo = RfqDeliveryRepository(make_server_client(settings))
    try:
        repo.require_schema()
        repo.require_http_schema()
    except Exception:
        logger.warning('rfq workflow worker unavailable code=SCHEMA_REQUIRED; API remains available')
        return None
    return RfqWorkflowWorker(repo, RfqPreparation(repo, JsonAgent(settings), settings.llm_model),
                             RfqDelivery(repo, mailbox_service), repo.metadata_owners)


def create_missing_rfq_workers(state, settings):
    """아직 없는 RFQ 워커를 만들어 state에 건다. 새로 만든 워커(아직 시작 전)만 돌려준다.

    메일 수신 워커가 없으면 RFQ 워커도 만들지 않는다. 부팅 때와 재시도 때 같이 쓴다.
    """
    created = []
    if state.connected_mailbox_worker is None:
        return created
    for name, factory in (('rfq_metadata_worker', start_metadata_worker),
                          ('rfq_workflow_worker', start_workflow_worker)):
        if getattr(state, name) is None:
            worker = factory(settings, state.mailbox_service)
            setattr(state, name, worker)
            if worker is not None:
                created.append(worker)
    return created


def start_graph_worker(state, settings):
    """그래프 워커를 만들고 시작해 state에 건다. 준비가 안 됐으면 None으로 두고 사유 코드를 남긴다."""
    # Graph readiness is optional for receiving mail and serving APIs.
    # No feature toggle: attempt the existing START contract each boot and on every retry.
    from app.features.work_cases.infrastructure.graph_repository import runtime_repository
    from app.features.work_cases.application.graph_worker import GraphWorker
    from app.features.work_cases.application.start_readiness import configuration_readiness, agent_readiness
    worker = None
    try:
        configuration_readiness(settings).require()
        agent_readiness().require()
        repo = runtime_repository(make_server_client(settings), settings)
        if not repo.available():
            raise RuntimeError('Graph START contract unavailable')
        if state.mailbox_service is None:
            state.mailbox_service = make_mailbox_service(settings)
        worker = GraphWorker(settings, repo, state.mailbox_service)
        worker.start()
        state.extraction_worker = worker
        state.graph_startup_code = None
    except Exception:
        if worker is not None:
            try:
                worker.close()
            except Exception:
                logger.warning('graph cleanup failed code=GRAPH_CLEANUP_FAILED')
        state.extraction_worker = None
        state.graph_startup_code = 'GRAPH_RUNTIME_UNAVAILABLE'
        logger.warning('graph unavailable code=GRAPH_RUNTIME_UNAVAILABLE; mail/API remain available')
    return state.extraction_worker


def start_call_recording_worker(state, settings):
    """통화 녹음 정리 워커(KAN-248)를 만들고 시작한다. DB 계약(버킷·한도)이 다르면 None으로 둔다."""
    from app.features.call_recordings.infrastructure import CallRecordingRepository, CallRecordingStorage
    from app.features.call_recordings.worker import CallRecordingCleanupWorker
    try:
        client = make_server_client(settings)
        repo = CallRecordingRepository(client)
        if not repo.ready():
            raise RuntimeError('call recording contract unavailable')
        worker = CallRecordingCleanupWorker(repo, CallRecordingStorage(client))
        worker.start()
        state.call_recording_worker = worker
    except Exception:
        state.call_recording_worker = None
        logger.warning('call recording worker unavailable code=SCHEMA_REQUIRED; API remains available')
    return state.call_recording_worker


def transcription_configured(settings):
    return bool(getattr(settings, 'stt_url', None) and getattr(settings, 'stt_model', '').strip())


def start_transcription_worker(state, settings):
    if not transcription_configured(settings):
        return None

    from app.features.call_recordings.infrastructure import CallRecordingRepository, CallRecordingStorage
    from app.features.call_recordings.transcription import HttpTranscriber, TranscriptionWorker

    try:
        client = make_server_client(settings)
        repo = CallRecordingRepository(client)
        repo.require_transcription_schema()
        key = settings.stt_api_key.get_secret_value() if settings.stt_api_key else None
        transcriber = HttpTranscriber(str(settings.stt_url), settings.stt_model.strip(), key)
        worker = TranscriptionWorker(repo, CallRecordingStorage(client), transcriber)
        worker.start()
        state.transcription_worker = worker
    except Exception:
        logger.warning('call recording transcription worker unavailable; API remains available')
    return getattr(state, 'transcription_worker', None)


def revive_workers(state, settings):
    """부팅 때 못 뜬 RFQ·그래프 워커를 다시 띄운다. 이미 떠 있는 워커는 건드리지 않는다."""
    for worker in create_missing_rfq_workers(state, settings):
        worker.start()
        logger.info('worker recovered name=%s', type(worker).__name__)
    if state.extraction_worker is None and start_graph_worker(state, settings) is not None:
        logger.info('worker recovered name=GraphWorker')
    if getattr(state, 'call_recording_worker', None) is None and start_call_recording_worker(state, settings) is not None:
        logger.info('worker recovered name=CallRecordingCleanupWorker')
    if getattr(state, 'transcription_worker', None) is None and start_transcription_worker(state, settings):
        logger.info('worker recovered name=TranscriptionWorker')


async def retry_unavailable_workers(state, settings, interval=WORKER_RETRY_SECONDS):
    """DB 계약이 고쳐지면 재배포 없이 워커가 올라오도록, 빠진 워커가 있는 동안 주기적으로 다시 시도한다."""
    while True:
        await asyncio.sleep(interval)
        missing = state.extraction_worker is None or getattr(state, 'call_recording_worker', None) is None or (
            state.connected_mailbox_worker is not None and (
            state.rfq_metadata_worker is None or state.rfq_workflow_worker is None))
        missing = missing or (transcription_configured(settings) and getattr(state, 'transcription_worker', None) is None)
        if not missing:
            continue
        try:
            await to_thread.run_sync(revive_workers, state, settings)
        except Exception:
            logger.warning('worker retry failed code=WORKER_RETRY_FAILED')
