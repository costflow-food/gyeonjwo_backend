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

