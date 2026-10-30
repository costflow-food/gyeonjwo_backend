"""Synthetic-only extraction runtime tests. No Gmail, Supabase or model network."""
import copy
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agents.agents import AgentRunner
from app.agents.graph import build_graph, run_case
from app.agents.nodes import Nodes
from app.agents.schemas import ExtractionOut, RunCaseInput
from app.core.errors import ServiceError
from app.features.work_cases.application.extraction_worker import ExtractionWorker
from app.features.work_cases.application.extraction_tools import CURRENT_SCOPE, ExtractionScope, stage
from app.features.work_cases.application.list_service import WorkCaseListService
from app.features.work_cases.application.cursor import CursorCodec


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name, args, number):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f't{number}'}])


class FakeRepository:
    """Models atomic RPC outcomes, not proof that the remote SQL implements them."""
    def __init__(self, body='회사: 햇살식품\n품목: 냉동 딸기\n수량 100 kg', attachments=None):
        self.owner, self.case_id, self.mail_id, self.batch = map(str, (uuid4(), uuid4(), uuid4(), uuid4()))
        self.row = {'id': self.case_id, 'input_revision': 1, 'current_batch_id': self.batch,
                    'status': 'Running', 'processing_step': 'EXTRACTING', 'item_name': None,
                    'requester_name': None, 'archived_at': None}
        self.message = {'id': self.mail_id, 'subject': '단가 변경', 'body_text': body,
                        'attachments_jsonb': attachments or [], 'sender': 'sales@example.invalid'}
        self.job = {'job_id': str(uuid4()), 'claim_token': str(uuid4()), 'owner_user_id': self.owner,
                    'case_id': self.case_id, 'input_revision': 1, 'batch_id': self.batch, 'mail_ids': [self.mail_id]}
        self.done, self.failures, self.saved, self.suppliers = False, [], [], []
        self.lock = threading.Lock()

    def claim(self):
        if self.done:
            return None
        self.done = True
        return self.job

    def heartbeat(self, job):
        return True

    def revision(self, owner, case_id):
        assert owner == self.owner and case_id == self.case_id
        return self.row['input_revision']

    def context(self, owner, case_id, revision, batch):
        if owner != self.owner or case_id != self.case_id:
            raise ServiceError('RESOURCE_NOT_FOUND', '없음', 404)
        if revision != self.row['input_revision'] or batch != self.batch:
            raise ServiceError('REVISION_CONFLICT', '변경됨', 409)
        return dict(self.row)

    def mail(self, owner, case_id, mail_id):
        assert owner == self.owner and case_id == self.case_id and mail_id == self.mail_id
        return self.message

    def fail(self, job, code):
        self.failures.append(code)
        if job['input_revision'] == self.row['input_revision']:
            self.row.update(status='Running', processing_step='FAILED', error_jsonb={'code': code})
        return True

    def finish(self, job, output, result, model_id):
        with self.lock:
            if self.row['input_revision'] != job['input_revision']:
                return {'kind': 'STALE'}
            if result:
                self.row.update(result.stored_fields())
            self.row.update(status='Running', processing_step=output.processing_step)
            self.saved.append(output)
            return {'kind': 'STORED', 'quote_version_ids': []}


def result_fields(repo, *, item='냉동 딸기', company='햇살식품', part=None):
    evidence_id = f'{repo.mail_id}:part:{part}' if part else f'{repo.mail_id}:body'
    text = '회사: 햇살식품\n품목: 냉동 딸기\n수량 100 kg' if part else repo.message['body_text']
    values = {'item_name': item, 'requester_name': company}
    return {'fields': {key: {'value': value, 'evidence': [{'evidence_id': evidence_id, 'excerpt': text}] if value else []}
                       for key, value in values.items()}, 'unknown_fields': []}


def worker(repo, fields=None, *, runner_factory=None):
    args = {'case_id': repo.case_id, 'expected_revision': 1}
    messages = [tool_call('read_mail_evidence', {**args, 'mail_id': repo.mail_id}, 1),
                tool_call('save_agent_result', {**args, 'result': fields or result_fields(repo)}, 2),
                tool_call('ExtractionOut', {'next_action': 'STORED', 'reason': '근거 확인',
                                            'unknown_fields': [], 'quote_version_ids': []}, 3)]
    factory = runner_factory or (lambda: AgentRunner(FakeModel(messages=iter(messages))))
    settings = SimpleNamespace(max_document_characters=120000, llm_model='FakeModel')
    return ExtractionWorker(settings, repo, object(), runner_factory=factory, attachment_repo=object())


def test_fake_model_populates_company_item_and_stops_without_candidate():
    repo = FakeRepository()
    assert worker(repo).process_once()
    assert repo.row['status'] == 'Running'
    assert repo.row['processing_step'] == 'SELECTING_CANDIDATES'
    assert repo.row['requester_name'] == '햇살식품' and repo.row['item_name'] == '냉동 딸기'
    assert repo.row['standardized_item_key'] == '냉동 딸기'
    assert [s.role for s in repo.saved[0].steps] == ['INFO_EXTRACTOR']
    assert repo.suppliers == [] and repo.failures == []
    with pytest.raises(LookupError):
        CURRENT_SCOPE.get()


def test_person_only_signature_is_not_made_into_company():
    repo = FakeRepository(body='품목: 냉동 딸기\n홍길동 드림')
    worker(repo, result_fields(repo, company=None)).process_once()
    assert repo.row['requester_name'] is None
    assert repo.row['processing_step'] == 'SELECTING_CANDIDATES'


def test_missing_item_uses_server_unknown_fields_and_needs_info():
    repo = FakeRepository(body='회사: 햇살식품\n확인 부탁드립니다.')
    worker(repo, result_fields(repo, item=None)).process_once()
    assert repo.row['item_name'] is None and repo.row['processing_step'] == 'NEEDS_INFO'
    assert 'item_name' in repo.saved[0].steps[0].unknown_fields
    assert repo.row['status'] == 'Running'


def test_pdf_only_item_uses_shared_attachment_fetch(monkeypatch):
    import app.features.work_cases.application.extraction_tools as tools
    repo = FakeRepository(body='첨부 확인', attachments=[{'provider_part_ref': '0.1', 'mime_type': 'application/pdf'}])
    calls = []
    monkeypatch.setattr(tools, 'fetch_gmail_attachment', lambda *args: calls.append(args) or SimpleNamespace(content=b'synthetic-pdf'))
    monkeypatch.setattr(tools, 'read_pdf', lambda raw: '회사: 햇살식품\n품목: 냉동 딸기\n수량 100 kg')
    worker(repo, result_fields(repo, part='0.1')).process_once()
    assert calls[0][2:] == (repo.owner, repo.mail_id, '0.1')
    assert repo.row['item_name'] == '냉동 딸기' and repo.row['processing_step'] == 'SELECTING_CANDIDATES'


def test_unreadable_excel_does_not_break_body_extraction():
    repo = FakeRepository(attachments=[{'provider_part_ref': '0.1', 'mime_type': 'application/vnd.ms-excel'}])
    worker(repo).process_once()
    assert repo.row['item_name'] == '냉동 딸기'


def test_missing_llm_key_failure_keeps_running_and_never_completes(caplog):
    repo = FakeRepository()
    def broken():
        raise RuntimeError('synthetic-token synthetic-body')
    worker(repo, runner_factory=broken).process_once()
    assert repo.row['status'] == 'Running' and repo.row['processing_step'] == 'FAILED'
    assert repo.saved == [] and repo.failures == ['EXTRACTION_FAILED']
    assert 'synthetic-token' not in caplog.text and 'synthetic-body' not in caplog.text


def test_hold_keeps_running_on_hold():
    repo = FakeRepository()
    worker(repo, runner_factory=lambda: lambda role, data: ExtractionOut(next_action='HOLD', reason='근거 없음')).process_once()
    assert repo.row['status'] == 'Running' and repo.row['processing_step'] == 'ON_HOLD'


def test_false_stored_without_staging_is_failure():
    repo = FakeRepository()
    worker(repo, runner_factory=lambda: lambda role, data: ExtractionOut(next_action='STORED', reason='가짜')).process_once()
    assert repo.saved == [] and repo.row['processing_step'] == 'FAILED'


def test_revision_race_never_overwrites_newer_work():
    repo = FakeRepository()
    def runner(role, data):
        repo.row['input_revision'] = 2
        repo.row['processing_step'] = 'NEWER_STATE'
        return ExtractionOut(next_action='STORED', reason='과거 입력')
    worker(repo, runner_factory=lambda: runner).process_once()
    assert repo.row['processing_step'] == 'NEWER_STATE' and repo.saved == []


def test_scope_blocks_other_case_and_invented_evidence():
    repo = FakeRepository()
    scope = ExtractionScope(repo.owner, repo.case_id, 1, repo.batch, (repo.mail_id,), repo,
                            object(), object(), 120000, time.monotonic()+60)
    token = CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(ServiceError):
            scope.check(str(uuid4()), 1)
        with pytest.raises(ValueError):
            stage(repo.case_id, 1, result_fields(repo))
    finally:
        CURRENT_SCOPE.reset(token)


def test_graph_defaults_to_extraction_only():
    roles=[]
    def runner(role, data):
        roles.append(role)
        return ExtractionOut(next_action='STORED', reason='완료')
    graph=build_graph(runner, lambda _:1, Nodes(preprocess=lambda _:{}))
    result=run_case(graph, RunCaseInput(case_id=uuid4(), start_from='START', input_revision=1, mail_ids=[]))
    assert roles == ['INFO_EXTRACTOR'] and result.processing_step == 'SELECTING_CANDIDATES'


def test_wrong_entry_point_cannot_run_later_roles_in_default_graph():
    calls=[]
    graph=build_graph(lambda *args:calls.append(args),lambda _:1)
    result=run_case(graph,RunCaseInput(case_id=uuid4(),start_from='PLAN',input_revision=1))
    assert calls==[] and result.status=='FAILED'


def test_numeric_substrings_and_invented_month_day_are_rejected():
    from app.features.work_cases.domain.extraction import ExtractionResult
    for field,value,text in [('quantity','100','수량 1000 kg'),
                             ('effective_from','2026-12-31','2026년 11월 1일 적용')]:
        result=ExtractionResult(fields={field:{'value':value,'evidence':[{'evidence_id':'source','excerpt':text}]}})
        with pytest.raises(ValueError):
            result.validated({'source':text})
