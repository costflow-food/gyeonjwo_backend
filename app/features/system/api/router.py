from fastapi import APIRouter, Request
from app.api.dependencies import get_service
from app.api.responses import api_json
from scripts.check_connection import check

router = APIRouter(tags=['system'])

@router.get('/health')
@router.get('/api/v1/health', deprecated=True, include_in_schema=False)
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
        return api_json({'status': 'not_ready'}, 503)
    return {'status': 'ready', 'checks': {'database': 'read_probe_passed', 'llm': 'configured', 'gmail_worker': 'running'}}

