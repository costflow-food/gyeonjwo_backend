"""Schema inspection is read-only and must not claim real execution success."""
import json
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.features.system.application.extraction_diagnostics import check_extraction_schema


CONTRACT = {'rpc': {'submit_work_case_start': {'args': {'p_owner': 'uuid', 'p_case': 'uuid'}}}}


def settings():
    return SimpleNamespace(supabase_url='https://synthetic.example.invalid',
                           supabase_secret_key=SecretStr('synthetic-not-a-real-key'))


def test_reports_missing_column_and_function_without_running_rpc():
    calls=[]
    def transport(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={'paths': {}, 'definitions': {}})
    result=check_extraction_schema(settings(),transport=httpx.MockTransport(transport),contract=CONTRACT)
    assert result['missing']==['work_cases.requester_name','rpc.submit_work_case_start']
    assert calls==[('GET','/rest/v1/')]
    assert result['integration_verified'] is False and result['remote_mutations'] is False


@pytest.mark.parametrize('extra_required',[False,True])
def test_reports_signature_mismatch(extra_required):
    args={'p_owner': {'type':'string'}}
    required=[]
    if extra_required:
        args['p_case']={'type':'string'}
        required=['p_unexpected']
    schema={'definitions':{'work_cases':{'properties':{'requester_name':{}}}},
            'paths':{'/rpc/submit_work_case_start':{'post':{'parameters':[
                {'name':'args','in':'body','schema':{'properties':args,'required':required}}]}}}}
    result=check_extraction_schema(settings(),transport=httpx.MockTransport(lambda _:httpx.Response(200,json=schema)),contract=CONTRACT)
    assert result['status']=='schema_not_ready' and result['mismatched']


def test_schema_ready_is_not_integration_verified():
    schema={'definitions':{'work_cases':{'properties':{'requester_name':{}}}},
            'paths':{'/rpc/submit_work_case_start':{'post':{'parameters':[
                {'name':'args','in':'body','schema':{'properties':{'p_owner':{},'p_case':{}}}}]}}}}
    result=check_extraction_schema(settings(),transport=httpx.MockTransport(lambda _:httpx.Response(200,json=schema)),contract=CONTRACT)
    assert result['status']=='schema_ready' and result['integration_verified'] is False


@pytest.mark.parametrize('status',[401,404,500])
def test_failure_never_returns_credentials_or_raw_provider_text(status):
    secret=settings().supabase_secret_key.get_secret_value()
    result=check_extraction_schema(settings(),transport=httpx.MockTransport(lambda _:httpx.Response(status,text=secret)),contract=CONTRACT)
    assert result['error_code']=='SCHEMA_DISCOVERY_UNAVAILABLE'
    assert secret not in json.dumps(result)


def test_missing_credentials_do_not_make_network_request():
    def transport(_):
        raise AssertionError('Network not allowed')
    result=check_extraction_schema(SimpleNamespace(supabase_url=None,supabase_secret_key=None),
        transport=httpx.MockTransport(transport),contract=CONTRACT)
    assert result['missing']==['SUPABASE_URL','SUPABASE_SECRET_KEY']
