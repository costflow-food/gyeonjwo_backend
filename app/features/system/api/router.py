from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.features.mailboxes.api.router import get_mailbox_service
from app.features.system.application.diagnostics import configuration_report

router = APIRouter(tags=['system'])

@router.get('/health')
def health(request: Request):
    return {'status': 'ok', 'schema_contract': 'notion-06-v2.2',
            'api_contract':'google-mailbox-v1', 'build_revision':request.app.state.settings.app_build_revision}


@router.get('/ready')
def ready(request: Request):
    settings=request.app.state.settings
    checks={'configuration':'not_ready','database':'not_checked','gmail_worker':'not_running'}
    report=configuration_report(settings)
    if report['status']!='ready':
        return JSONResponse({'status':'not_ready','mode':'google-mailbox','checks':checks},status_code=503)
    checks['configuration']='ready'
    try:
        service = get_mailbox_service(request)
        if service.repo.call('connected_google_mailbox_health') != 'google-mailbox-v1':
            raise RuntimeError('Connected mailbox schema mismatch')
        checks['database']='ready'
    except Exception:
        checks['database']='rpc_missing_or_unavailable'
        return JSONResponse({'status':'not_ready','mode':'google-mailbox','checks':checks},status_code=503)
    worker=request.app.state.connected_mailbox_worker
    if not worker or not worker.running:
        return JSONResponse({'status':'not_ready','mode':'google-mailbox','checks':checks},status_code=503)
    checks['gmail_worker']='running'
    metadata_worker = getattr(request.app.state, 'rfq_metadata_worker', None)
    checks['rfq_metadata_worker'] = 'running' if metadata_worker and metadata_worker.running else 'not_running'
    # KAN-161 S4-16: RFQ preparation/sending worker. Per-user send settings still gate actual sending.
    dispatch_worker = getattr(request.app.state, 'rfq_workflow_worker', None)
    checks['rfq_dispatch'] = 'running' if dispatch_worker and dispatch_worker.running else 'not_running'
    checks['review_execution_mode'] = 'GRAPH_START'
    checks['review_claim'] = 'awaiting_db_contract'
    from app.features.work_cases.api.router import get_runtime_repository
    from app.features.work_cases.api.decisions import case_changes_ready
    checks['final_decision'] = 'not_ready'
    degraded = []
    try:
        worker = request.app.state.extraction_worker
        repo = get_runtime_repository(request)
        if case_changes_ready(repo):
            checks['final_decision'] = 'ready'
        if not worker or not worker.running:
            raise RuntimeError('Graph worker unavailable')
        if not repo.available():
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
    return {'status':'ready','mode':'google-mailbox','checks':checks,'degraded_features':degraded}
