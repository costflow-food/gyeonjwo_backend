"""API-side contracts with a simulated atomic DB boundary; no remote deletion."""
import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import ServiceError
from app.features.work_cases.application.actions import WorkCaseActions
from app.features.work_cases.application.cursor import CursorCodec
from app.features.work_cases.application.list_service import WorkCaseListService
from app.features.work_cases.api.router import CaseActionRequest, start_case, accept_increase
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository


class FakeAtomicRPC:
    def available(self):
        return True

    def __init__(self):
        self.case_id, self.owner = str(uuid4()), str(uuid4())
        self.row = {'status': 'Waiting', 'revision': 1}
        self.replays, self.receipts, self.jobs = {}, [], []

    def action(self, owner, case_id, revision, action, key, body_hash, request_id):
        identity = (owner, case_id, action, key)
        if identity in self.replays:
            old_hash, old = self.replays[identity]
            if old_hash != body_hash:
                raise ServiceError('IDEMPOTENCY_KEY_REUSED', '다른 요청', 409)
            return dict(old, kind='REPLAY')
        if owner != self.owner or case_id != self.case_id or self.row is None:
            raise ServiceError('RESOURCE_NOT_FOUND', '없음', 404)
        if revision != self.row['revision']:
            raise ServiceError('REVISION_CONFLICT', '변경됨', 409)
        if self.row['status'] != 'Waiting':
            raise ServiceError('WORK_ALREADY_STARTED', '이미 실행', 409)
        data = {'case_id': case_id}
        status = 200
        if action == 'ACCEPT_INCREASE':
            self.receipts.append({'outcome': 'ACCEPTED', 'mail_id': None})
            self.row = None
            data['deleted'] = True
        else:
            self.row.update(status='Running', processing_step='EXTRACTING')
            batch = str(uuid4())
            self.jobs.append(batch)
            status = 202
            data.update(input_revision=revision, batch_id=batch,
                        progress_url=f'/api/v1/work-cases/{case_id}/progress')
        result = {'kind': 'ACCEPTED', 'http_status': status,
                  'response': {'data': data, 'meta': {'request_id': request_id}}}
        self.replays[identity] = (body_hash, copy.deepcopy(result))
        return result


def test_accept_replays_original_response_after_case_is_gone():
    repo = FakeAtomicRPC(); service = WorkCaseActions(repo)
    first = service.submit(repo.owner, repo.case_id, 1, 'ACCEPT_INCREASE', 'accept-1')
    again = service.submit(repo.owner, repo.case_id, 1, 'ACCEPT_INCREASE', 'accept-1')
    assert first == again and first[0] == 200
    assert first[1]['data']['deleted'] is True and repo.row is None
    assert repo.receipts == [{'outcome': 'ACCEPTED', 'mail_id': None}] and repo.jobs == []


def test_reused_key_with_changed_body_is_conflict_even_after_delete():
    repo = FakeAtomicRPC(); service = WorkCaseActions(repo)
    service.submit(repo.owner, repo.case_id, 1, 'ACCEPT_INCREASE', 'same')
    with pytest.raises(ServiceError) as error:
        service.submit(repo.owner, repo.case_id, 2, 'ACCEPT_INCREASE', 'same')
    assert error.value.code == 'IDEMPOTENCY_KEY_REUSED'


def test_start_is_202_and_only_enqueues_one_job_on_replay():
    repo=FakeAtomicRPC();service=WorkCaseActions(repo,extraction_enabled=True)
    first=service.submit(repo.owner,repo.case_id,1,'START','start-1')
    assert service.submit(repo.owner,repo.case_id,1,'START','start-1')==first
    assert first[0]==202 and repo.row['processing_step']=='EXTRACTING' and len(repo.jobs)==1


def test_running_accept_does_not_delete_anything():
    repo=FakeAtomicRPC();repo.row['status']='Running'
    with pytest.raises(ServiceError) as error:
        WorkCaseActions(repo).submit(repo.owner,repo.case_id,1,'ACCEPT_INCREASE','accept')
    assert error.value.code=='WORK_ALREADY_STARTED' and repo.row and repo.receipts==[]


@pytest.mark.parametrize('owner,revision,code', [('other',1,'RESOURCE_NOT_FOUND'),(None,2,'REVISION_CONFLICT')])
def test_owner_and_revision_guards(owner,revision,code):
    repo=FakeAtomicRPC()
    with pytest.raises(ServiceError) as error:
        WorkCaseActions(repo).submit(owner or repo.owner,repo.case_id,revision,'ACCEPT_INCREASE','accept')
    assert error.value.code==code and repo.row is not None


def test_start_disabled_before_card_five_is_ready():
    repo=FakeAtomicRPC()
    with pytest.raises(ServiceError):
        WorkCaseActions(repo).submit(repo.owner,repo.case_id,1,'START','start')
    assert repo.jobs==[]


@pytest.mark.parametrize('key',[None,'',' leading','x'*201,'line\nbreak'])
def test_bad_idempotency_keys_do_not_touch_db(key):
    repo=FakeAtomicRPC()
    with pytest.raises(ServiceError) as error:
        WorkCaseActions(repo).submit(repo.owner,repo.case_id,1,'ACCEPT_INCREASE',key)
    assert error.value.status==400 and repo.row is not None


def test_http_handlers_return_immediately_without_calling_an_agent():
    repo=FakeAtomicRPC();signals=[]
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(agent_extraction_enabled=True,agent_stop_after_extraction=True),
        extraction_worker=SimpleNamespace(wake=SimpleNamespace(set=lambda:signals.append('wake'))))))
    response=start_case(repo.case_id,CaseActionRequest(expected_revision=1),request,
                        SimpleNamespace(user_id=repo.owner),repo,'start')
    assert response.status_code==202 and signals==['wake'] and len(repo.jobs)==1


def test_repository_calls_named_atomic_delete_function():
    calls=[]
    class Client:
        def rpc(self,name,args):
            calls.append((name,args));return SimpleNamespace(execute=lambda:SimpleNamespace(data=True))
    CaseRuntimeRepository(Client()).action('owner','case',1,'ACCEPT_INCREASE','key','hash','request')
    assert calls[0][0]=='accept_and_delete_work_case'
    assert calls[0][1]['p_owner']=='owner'


@pytest.mark.parametrize('enabled',[False,True])
def test_list_start_visibility_and_company_display(enabled):
    case_id,mail_id=str(uuid4()),str(uuid4())
    case={'id':case_id,'initial_mail_id':mail_id,'created_at':'2026-10-29T00:00:00Z',
          'status':'Waiting','processing_step':'PENDING_START','input_revision':1,
          'archived_at':None,'decision':None,'item_name':None}
    mail={'id':mail_id,'subject':'요청 제목','sender':'sales@example.invalid',
          'sent_or_received_at':'2026-10-29T00:00:00Z','attachments_jsonb':[]}
    repo=SimpleNamespace(rows=lambda *args:[(case,mail)],intake_pending=lambda owner:0)
    service=WorkCaseListService(repo,CursorCodec('synthetic-key'),extraction_enabled=enabled)
    row=service.list('owner').items[0]
    assert ('START' in row.allowed_actions)==enabled and row.title==mail['sender']
    case.update(status='Running',processing_step='SELECTING_CANDIDATES',requester_name='추출 회사',
                registered_requester_name='등록 거래처',item_name='원문 품목')
    row=service.list('owner').items[0]
    assert row.title=='등록 거래처' and row.requester_name=='추출 회사' and row.item_name=='원문 품목'
