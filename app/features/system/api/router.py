from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.api.dependencies import get_service
from scripts.check_connection import check

router = APIRouter(tags=['system'])

@router.get('/health')
def health():
    return {'status': 'ok', 'schema_contract': 'notion-06-v2.2'}


@router.get('/ready')
def ready(request: Request):
    if request.app.state.settings.google_mailbox_worker_enabled:
        try:
            settings = request.app.state.settings
            settings.require_google_connect()
            settings.require_llm()
            service = request.app.state.mailbox_service
            worker = request.app.state.connected_mailbox_worker
            if not service or not worker or not worker.running:
                raise RuntimeError('Connected mailbox worker not running')
            if service.repo.call('connected_google_mailbox_health') != 'google-mailbox-v1':
                raise RuntimeError('Connected mailbox schema mismatch')
        except Exception:
            return JSONResponse({'status': 'not_ready'}, status_code=503)
        return {'status': 'ready', 'checks': {'database': 'google-mailbox-v1', 'llm': 'configured', 'gmail_worker': 'running'}}
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

