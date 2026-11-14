from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.features.mailboxes.api.router import get_mailbox_service
from app.features.system.application.diagnostics import configuration_report
from app.shared.infrastructure.db.health_probe import HealthProbe

router = APIRouter(tags=['system'])

@router.get('/health')
def health(request: Request):
    return {'status': 'ok', 'schema_contract': 'notion-06-v2.2',
            'api_contract':'google-mailbox-v1', 'build_revision':request.app.state.settings.app_build_revision}


def _not_ready(checks, revision):
    return JSONResponse({'status': 'not_ready', 'mode': 'google-mailbox', 'checks': checks,
                         'build_revision': revision}, status_code=503)


def _readiness_probes(request):
    state = request.app.state
    with state.service_lock:
        probes = getattr(state, 'readiness_probes', None)
        if probes is None:
            from app.features.work_cases.api.router import get_case_runtime
            from app.features.work_cases.api.decisions import case_changes_ready

            def mailbox():
                return get_mailbox_service(request).repo.call('connected_google_mailbox_health')

            def decisions():
                return case_changes_ready(get_case_runtime(state))

            probes = (
                HealthProbe(mailbox, lambda result: result == 'google-mailbox-v1'),
                HealthProbe(decisions, bool),
            )
            state.readiness_probes = probes
        return probes


@router.get('/ready')
def ready(request: Request):
    settings=request.app.state.settings
    # 배포 스크립트가 이 응답이 새 컨테이너에서 왔는지 확인한다(200·503 모두).
    revision = settings.app_build_revision
    checks={'configuration':'not_ready','database':'not_checked','gmail_worker':'not_running'}
    report=configuration_report(settings)
    if report['status']!='ready':
        return _not_ready(checks, revision)
    checks['configuration']='ready'
    mailbox_probe, decision_probe = _readiness_probes(request)
    mailbox_status, _ = mailbox_probe.readiness()
    if mailbox_status in {'checking', 'failed'}:
        checks['database'] = ('checking' if mailbox_status == 'checking' else 'rpc_missing_or_unavailable')
        return _not_ready(checks, revision)
    checks['database'] = 'refreshing' if mailbox_status == 'refreshing' else 'ready'
    worker=request.app.state.connected_mailbox_worker
    if not worker or not worker.running:
        return _not_ready(checks, revision)
    checks['gmail_worker']='running'
    metadata_worker = getattr(request.app.state, 'rfq_metadata_worker', None)
    checks['rfq_metadata_worker'] = 'running' if metadata_worker and metadata_worker.running else 'not_running'
    # RFQ preparation/sending worker. Per-user send settings still gate actual sending.
    dispatch_worker = getattr(request.app.state, 'rfq_workflow_worker', None)
    checks['rfq_dispatch'] = 'running' if dispatch_worker and dispatch_worker.running else 'not_running'
    checks['review_execution_mode'] = 'GRAPH_START'
    checks['review_claim'] = 'awaiting_db_contract'
    checks['final_decision'] = 'ready' if decision_probe.read(timeout=0.05) else 'not_ready'
    degraded = []
    try:
        worker = request.app.state.extraction_worker
        if not worker or not worker.running:
            raise RuntimeError('Graph worker unavailable')
        if not worker.repo.health_ready(worker.repo.read_health(timeout=0.05)):
            raise RuntimeError('Graph contract unavailable')
        checks['extraction'] = 'running'
        if getattr(worker.repo, 'review_ready', False):
            checks['review_claim'] = 'ready'
    except Exception:
        checks['extraction'] = 'not_ready'
    if checks['extraction'] == 'not_ready':
        degraded.append('review')
    if checks['review_claim'] != 'ready':
        degraded.append('review_reply')
    if checks['final_decision'] != 'ready':
        degraded.append('final_decision')
    if checks['rfq_metadata_worker'] != 'running':
        degraded.append('rfq_metadata')
    if checks['rfq_dispatch'] != 'running':
        degraded.append('rfq_dispatch')
    return {'status':'ready','mode':'google-mailbox','checks':checks,'degraded_features':degraded,
            'build_revision': revision}
