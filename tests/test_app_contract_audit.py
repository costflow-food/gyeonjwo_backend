"""Assembled app/middleware contracts with synthetic auth, DB and secrets only."""
import importlib
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.features.mail.api.router import get_original_mail_service
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.api.router import get_runtime_repository, get_list_service
from test_agent_case_actions import FakeAtomicRPC


@pytest.fixture
def app(monkeypatch):
    import app.core.config as config
    settings=Settings(_env_file=None,supabase_url='https://synthetic.example.invalid',
        supabase_secret_key='sb_secret_synthetic',supabase_publishable_key='sb_publishable_synthetic',
        gmail_client_id='synthetic-client',gmail_client_secret='synthetic-secret',
        google_token_encryption_key=Fernet.generate_key().decode(),
        worker_enabled=False,google_mailbox_worker_enabled=False,
        agent_extraction_enabled=True,agent_extraction_worker_enabled=False)
    monkeypatch.setattr(config,'get_settings',lambda:settings)
    main=importlib.import_module('app.main')
    app=main.create_app(settings=settings)
    repo=FakeAtomicRPC()
    async def principal():return SimpleNamespace(user_id=repo.owner)
    async def repository():return repo
    app.dependency_overrides[require_google_user]=principal
    app.dependency_overrides[get_runtime_repository]=repository
    return app,repo


def test_idempotent_replay_keeps_first_envelope_and_request_header(app):
    app,repo=app
    with TestClient(app,base_url='https://backend.example.invalid') as client:
        path='/api/v1/work-cases/'+repo.case_id+'/start'
        headers={'Idempotency-Key':'start-once','Origin':'http://localhost:3000'}
        first=client.post(path,json={'expected_revision':1},headers=headers)
        replay=client.post(path,json={'expected_revision':1},headers=headers)
        assert first.status_code==replay.status_code==202 and first.json()==replay.json()
        assert first.headers['x-request-id']==replay.headers['x-request-id']==first.json()['meta']['request_id']
        assert first.headers['cache-control']=='private, no-store' and len(repo.jobs)==1
        conflict=client.post(path,json={'expected_revision':2},headers=headers)
        assert conflict.status_code==409 and conflict.json()['error']['code']=='IDEMPOTENCY_KEY_REUSED'
        assert conflict.headers['access-control-allow-origin']=='http://localhost:3000'


def test_cors_and_validation_errors_do_not_echo_input(app):
    app,repo=app
    with TestClient(app,base_url='https://backend.example.invalid') as client:
        path='/api/v1/work-cases/'+repo.case_id+'/start'
        preflight=client.options(path,headers={'Origin':'http://localhost:3000',
            'Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'authorization,idempotency-key,content-type,cache-control'})
        assert preflight.status_code==200
        bad=client.post(path,json={'expected_revision':'synthetic-private-input'},
                        headers={'Origin':'http://localhost:3000','Idempotency-Key':'invalid'})
        assert bad.status_code==422 and 'synthetic-private-input' not in bad.text
        assert bad.headers['cache-control']=='private, no-store'
        assert bad.headers['x-content-type-options']=='nosniff'
        assert bad.json()['meta']['request_id']==bad.headers['x-request-id']


def test_accept_replay_is_available_after_case_deleted_in_mock_db(app):
    app,repo=app
    with TestClient(app,base_url='https://backend.example.invalid') as client:
        path='/api/v1/work-cases/'+repo.case_id+'/accept-increase'
        headers={'Idempotency-Key':'accept-once'}
        first=client.post(path,json={'expected_revision':1},headers=headers)
        again=client.post(path,json={'expected_revision':1},headers=headers)
        assert first.status_code==again.status_code==200 and first.json()==again.json()
        assert repo.row is None and repo.jobs==[] and len(repo.receipts)==1
