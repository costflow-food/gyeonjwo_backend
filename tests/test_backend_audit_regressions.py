"""Regression cases found by cross-feature review, not live DB mutations."""
import copy
import json
import threading
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.list_repository import WorkCaseListRepository
from app.features.work_cases.application.list_service import WorkCaseListService
from app.features.work_cases.application.cursor import CursorCodec
from app.features.work_cases.application.actions import WorkCaseActions
from app.features.work_cases.api.router import get_list_service, get_case, case_progress
from app.features.review.domain.calculations import evaluate


def sample_rows():
    case_id,mail_id=str(uuid4()),str(uuid4())
    case={'id':case_id,'initial_mail_id':mail_id,'created_at':'2026-01-01T00:00:00Z',
          'status':'Running','processing_step':'SELECTING_CANDIDATES','input_revision':1,
          'archived_at':None,'decision':None,'item_name':'원문 품목','requester_name':'보존 회사'}
    mail={'id':mail_id,'subject':'제목','sender':'sender@example.invalid',
          'sent_or_received_at':'2026-01-01T00:00:00Z','attachments_jsonb':[],'supplier_id':None}
    return case,mail


class MissingColumn(Exception):
    code='42703'
    message='column work_cases.requester_name does not exist'


class ListClient:
    def __init__(self,missing=False,error=None):
        self.case,self.mail=sample_rows();self.missing=missing;self.error=error;self.queries=[]
    def table(self,name):
        client=self
        class Query:
            def __init__(self):self.columns='';self.filters={}
            def select(self,columns,**kwargs):self.columns=columns;return self
            def eq(self,key,value):self.filters[key]=value;return self
            def lte(self,*args):return self
            def order(self,*args):return self
            def limit(self,*args):return self
            def is_(self,*args):return self
            def in_(self,*args):return self
            def execute(self):
                assert self.filters['owner_user_id']=='owner'
                client.queries.append((name,self.columns))
                if name=='work_cases':
                    if client.error:raise client.error
                    if client.missing and 'requester_name' in self.columns:raise MissingColumn()
                    row=dict(client.case)
                    if 'requester_name' not in self.columns:row.pop('requester_name',None)
                    return SimpleNamespace(data=[row])
                return SimpleNamespace(data=[dict(client.mail)])
        return Query()


def test_stored_company_name_remains_visible_when_extraction_is_disabled():
    repo=WorkCaseListRepository(ListClient())
    # Existing display data must not depend on a worker/LLM enable flag.
    rows=repo.rows('owner','2026-10-30T00:00:00Z',False)
    service=WorkCaseListService(SimpleNamespace(rows=lambda *args:rows,intake_pending=lambda _:0),CursorCodec('test'))
    result=service.list('owner')
    assert result.items[0].title=='보존 회사' and 'START' not in result.items[0].allowed_actions


def test_old_schema_falls_back_once_and_keeps_owner_filter():
    client=ListClient(missing=True);repo=WorkCaseListRepository(client)
    rows=repo.rows('owner','2026-10-30T00:00:00Z',False)
    assert len(rows)==1 and repo.extraction_fields is False
    assert len([q for q in client.queries if q[0]=='work_cases'])==2
    repo.rows('owner','2026-10-30T00:00:00Z',False)
    assert len([q for q in client.queries if q[0]=='work_cases'])==3


def test_unrelated_storage_failure_is_not_mistaken_for_missing_optional_column():
    error=MissingColumn();error.code='OTHER_ERROR'
    repo=WorkCaseListRepository(ListClient(error=error))
    with pytest.raises(ServiceError) as caught:
        repo.rows('owner','2026-10-30T00:00:00Z',False)
    assert caught.value.status==503 and repo.extraction_fields is True


def test_transient_readiness_failure_recovers_after_cache_ttl(monkeypatch):
    import app.features.work_cases.api.router as api
    clock=[0];checks=[]
    monkeypatch.setattr(api.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(api,'make_server_client',lambda _:object())
    def available(self):
        checks.append(1)
        if len(checks)==1:raise ServiceError('PROCESSING_UNAVAILABLE','一時',503)
        return True
    monkeypatch.setattr(api.CaseRuntimeRepository,'available',available)
    settings=SimpleNamespace(agent_extraction_enabled=True,agent_stop_after_extraction=True,
                             google_token_encryption_key=SecretStr('synthetic'))
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings,service_lock=threading.Lock())))
    service=get_list_service(request)
    assert service.extraction_enabled is False and service.repo.extraction_fields is True
    assert get_list_service(request) is service and len(checks)==1
    clock[0]=6
    assert get_list_service(request).extraction_enabled is True and len(checks)==2


def test_unknown_action_does_not_fall_through_to_start_rpc():
    def action(*args):raise AssertionError('RPC must not be called')
    with pytest.raises(ServiceError) as caught:
        WorkCaseActions(SimpleNamespace(action=action)).submit('owner',str(uuid4()),1,'UNKNOWN','key')
    assert caught.value.code=='ACTION_NOT_ALLOWED'


class DetailClient:
    def __init__(self,row):self.row=row;self.columns=''
    def table(self,name):assert name=='work_cases';return self
    def select(self,columns):self.columns=columns;return self
    def eq(self,*args):return self
    def limit(self,*args):return self
    def execute(self):return SimpleNamespace(data=[self.row])


def test_detail_is_non_mutating_and_requests_exact_quantity_text():
    row={'id':str(uuid4()),'quantity':'99999999999999.999999'}
    before=copy.deepcopy(row);client=DetailClient(row)
    response=get_case(row['id'],SimpleNamespace(user_id='owner'),SimpleNamespace(client=client))
    assert row==before and 'quantity::text' in client.columns
    assert json.loads(response.body)['data']['quantity']=='99999999999999.999999'


def test_progress_normalizes_utc_and_redacts_unknown_error_code():
    row={'id':str(uuid4()),'status':'Running','processing_step':'FAILED','input_revision':1,
         'updated_at':'2026-10-30T09:00:00+09:00','error_jsonb':{'code':'raw-sensitive-provider-response'}}
    response=case_progress(row['id'],SimpleNamespace(user_id='owner'),SimpleNamespace(client=DetailClient(row)))
    payload=json.loads(response.body)['data']
    assert payload['updated_at']=='2026-10-30T00:00:00.000000Z'
    assert payload['error']['code']=='EXTRACTION_FAILED' and 'raw-sensitive' not in response.body.decode()


def test_corrupt_error_object_is_safe_service_error():
    row={'id':str(uuid4()),'error_jsonb':['private body']}
    with pytest.raises(ServiceError) as caught:
        case_progress(row['id'],SimpleNamespace(user_id='owner'),SimpleNamespace(client=DetailClient(row)))
    assert caught.value.status==503 and 'private body' not in str(caught.value)


@pytest.mark.parametrize('baseline_known',[False,True])
def test_unknown_baseline_cost_does_not_crash_or_invent_a_difference(baseline_known):
    options=[];expected={};registry={'q':Decimal('2'),'old':Decimal('10'),'new':Decimal('12.50')}
    for name,price,known in [('BEFORE','old',baseline_known),('AFTER','new',True)]:
        options.append(SimpleNamespace(option_id=name,goods=[SimpleNamespace(value_key=price,quantity_key='q')],
                                       transport_keys=[],other_logistics_keys=[]))
        expected[name]={'goods':[(price,'q')],'transport_keys':[],'other_logistics_keys':[],'cost_confirmed':known}
    result=evaluate(SimpleNamespace(options=options),registry,expected)
    assert result['AFTER']['total_cost']==Decimal('25.00')
    assert result['AFTER']['difference_from_before']==(Decimal('5.00') if baseline_known else None)


@pytest.mark.parametrize('start,end',[('2026-10-01',None),(None,'2026-10-31')])
def test_half_known_cost_period_is_unconfirmed_not_free_or_unbounded(start,end):
    from app.features.review.application.pipeline import cost_value
    cost={'payer':'BUYER','included_in_price':'FALSE','amount':Decimal('10'),'basis':'PER_UNIT',
          'coverage_from':start,'coverage_to':end,'coverage_quantity':None}
    assert cost_value(cost,Decimal('100'),date(2026,10,1),date(2026,10,31)) is None


def test_reference_numeric_values_are_selected_as_text_before_decimal_conversion(monkeypatch):
    from app.shared.infrastructure.db.references import ReferenceRepository
    from app.shared.infrastructure.db import references
    monkeypatch.setitem(references.REFERENCE_COLUMNS,'synthetic','id,unit_price,quantity')
    class Query:
        def __init__(self):self.columns=''
        def table(self,name):assert name=='synthetic';return self
        def select(self,columns):self.columns=columns;return self
        def order(self,column):assert column=='id';return self
        def range(self,*args):return self
        def execute(self):return SimpleNamespace(data=[{'id':'test','unit_price':'999999999999.123456','quantity':'0.000001'}])
    client=Query();rows=ReferenceRepository(client).read('synthetic')
    assert client.columns=='id,unit_price::text,quantity::text'
    assert rows[0]['unit_price']==Decimal('999999999999.123456')
    assert rows[0]['quantity']==Decimal('0.000001')
