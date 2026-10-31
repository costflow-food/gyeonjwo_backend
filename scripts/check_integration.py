"""python -m scripts.check_integration --db --base-url http://127.0.0.1:8000

Read-only checks. Field names/status codes only, never tokens/keys/response text.
"""
import argparse
import json

import httpx

from app.core.config import Settings
from app.features.system.application.diagnostics import configuration_report

REQUIRED_ROUTES = ('/api/v1/mailboxes/google/connect','/api/v1/mailboxes/google/connection')


def server_report(base_url,origin,*,client=None,check_readiness=True):
    checks={}
    try:
        if client is None:
            with httpx.Client(timeout=8,trust_env=False,follow_redirects=False) as connection:
                return server_report(base_url,origin,client=connection,check_readiness=check_readiness)
        base=base_url.rstrip('/')
        health=client.get(base+'/health')
        health_data=health.json()
        checks['health']='ready' if health.status_code==200 else 'not_ready'
        checks['build_revision']=health_data.get('build_revision','old_server_or_unknown')
        schema=client.get(base+'/openapi.json').json()
        checks['google_routes']='ready' if all(p in schema.get('paths',{}) for p in REQUIRED_ROUTES) else 'old_server_or_wrong_port'
        response=client.options(base+REQUIRED_ROUTES[0],headers={
            'Origin':origin,'Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'authorization,idempotency-key,content-type,cache-control'})
        checks['cors']='ready' if response.status_code==200 and response.headers.get('access-control-allow-origin')==origin else 'origin_or_headers_rejected'
        if check_readiness:
            response=client.get(base+'/ready')
            checks['application_ready']='ready' if response.status_code==200 else 'not_ready'
    except Exception:
        checks['transport']='unavailable_or_invalid_response'
    return checks


def database_report(settings,*,client=None):
    if configuration_report(settings)['status']!='ready': return {'status':'configuration_required'}
    try:
        if client is None:
            with httpx.Client(timeout=15,trust_env=False,follow_redirects=False) as connection:
                return database_report(settings,client=connection)
        key=settings.supabase_secret_key.get_secret_value()
        response=client.post(str(settings.supabase_url).rstrip('/')+'/rest/v1/rpc/connected_google_mailbox_health',
            headers={'apikey':key,'Authorization':'Bearer '+key},json={})
        if response.status_code==200 and response.json()=='google-mailbox-v1': return {'status':'ready'}
        data=response.json()
        code=data.get('code') if isinstance(data,dict) else None
        return {'status':'migration_required' if code=='PGRST202' else 'unavailable','http_status':response.status_code}
    except Exception:
        return {'status':'unavailable'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    parser.add_argument('--origin',default='http://localhost:3000')
    parser.add_argument('--local-only',action='store_true')
    parser.add_argument('--db',action='store_true')
    parser.add_argument('--skip-readiness',action='store_true',help='Do not call /ready (it probes the DB).')
    args=parser.parse_args()
    settings=Settings()
    result={'configuration':configuration_report(settings,receiver=settings.google_mailbox_worker_enabled)}
    if args.db: result['database']=database_report(settings)
    if not args.local_only:
        result['server']=server_report(args.base_url,args.origin,check_readiness=not args.skip_readiness)
    print(json.dumps(result,ensure_ascii=True,indent=2))
    failed=result['configuration']['status']!='ready'
    if args.db: failed |= result['database']['status']!='ready'
    if not args.local_only:
        failed |= any(v!='ready' for k,v in result['server'].items() if k!='build_revision')
    return int(failed)


if __name__=='__main__':
    raise SystemExit(main())
