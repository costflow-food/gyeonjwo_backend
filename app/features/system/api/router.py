from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.api.dependencies import get_service
from scripts.check_connection import check
from app.features.mailboxes.api.router import get_mailbox_service
from app.features.system.application.diagnostics import configuration_report

router = APIRouter(tags=['system'])

@router.get('/health')
def health(request: Request):
    return {'status': 'ok', 'schema_contract': 'notion-06-v2.2',
            'api_contract':'google-mailbox-v1', 'build_revision':request.app.state.settings.app_build_revision}


@router.get('/ready')
def ready(request: Request):
    if not request.app.state.settings.worker_enabled:
        settings=request.app.state.settings
        checks={'configuration':'not_ready','database':'not_checked',
                'gmail_worker':'disabled' if not settings.google_mailbox_worker_enabled else 'not_running'}
        report=configuration_report(settings,receiver=settings.google_mailbox_worker_enabled)
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
        if settings.google_mailbox_worker_enabled:
            worker=request.app.state.connected_mailbox_worker
            if not worker or not worker.running:
                return JSONResponse({'status':'not_ready','mode':'google-mailbox','checks':checks},status_code=503)
            checks['gmail_worker']='running'
        checks['extraction'] = 'disabled'
        if settings.agent_extraction_enabled:
            from app.features.work_cases.api.router import get_runtime_repository
            try:
                if not settings.agent_stop_after_extraction or not get_runtime_repository(request).available():
                    raise RuntimeError('Extraction contract unavailable')
                checks['extraction'] = 'ready'
                if settings.agent_extraction_worker_enabled:
                    worker = request.app.state.extraction_worker
                    if not worker or not worker.running:
                        raise RuntimeError('Extraction worker unavailable')
                    checks['extraction'] = 'running'
            except Exception:
                checks['extraction'] = 'not_ready'
                return JSONResponse({'status':'not_ready','mode':'google-mailbox','checks':checks},status_code=503)
        return {'status':'ready','mode':'google-mailbox','checks':checks}
    settings = request.app.state.settings
    try:
        settings.require_server()
        settings.require_llm()
        settings.require_gmail()
        if not settings.supabase_publishable_key or not settings.supabase_publishable_key.get_secret_value():
            raise RuntimeError('Authentication key missing')
        check(get_service(request).client)
        if not request.app.state.worker or not request.app.state.worker.running:
            raise RuntimeError('Worker not running')
    except Exception:
        return JSONResponse({'status': 'not_ready'}, status_code=503)
    return {'status': 'ready', 'checks': {'database': 'read_probe_passed', 'llm': 'configured', 'gmail_worker': 'running'}}

