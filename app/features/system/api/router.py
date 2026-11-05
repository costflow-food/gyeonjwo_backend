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
    checks['review_execution_mode'] = 'GRAPH_START'
    checks['review_claim'] = 'awaiting_db_contract'
    from app.features.work_cases.api.router import get_runtime_repository
    try:
        worker = request.app.state.extraction_worker
        if not worker or not worker.running:
            raise RuntimeError('Graph worker unavailable')
        if not get_runtime_repository(request).available():
            raise RuntimeError('Graph contract unavailable')
        checks['extraction'] = 'running'
    except Exception:
        checks['extraction'] = 'not_ready'
    return {'status':'ready','mode':'google-mailbox','checks':checks,
            'degraded_features': ['review'] if checks['extraction'] == 'not_ready' else []}
