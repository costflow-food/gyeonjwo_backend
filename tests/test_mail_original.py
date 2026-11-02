"""API-25 synthetic fixtures. No real mail or credentials in test files."""
import base64
import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import httpx
from pydantic import SecretStr
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.responses import service_error_response
from app.core.errors import ServiceError
from app.features.mail.api.router import router, get_original_mail_service
from app.features.mailboxes.api.router import require_google_user
from app.features.mail.application.original import OriginalMailService
from app.features.mail.infrastructure.original import OriginalMailRepository
from app.features.mail.application.attachments import fetch_gmail_attachment
from app.features.mail.domain.attachment_ids import attachment_id


@pytest.fixture
def data():
    mail_id=str(uuid4())
    return {'id':mail_id,'owner_user_id':'owner','subject':'합성 요청서','sender':'sales@example.invalid',
            'recipients_jsonb':['buyer@example.invalid'],'sent_or_received_at':'2026-10-30T09:00:00+09:00',
            'body_text':'단가 변경을 검토해 주세요.','mail_classification':'BUSINESS','provider':'GMAIL',
            'mailbox_ref':'box','provider_message_id':'message',
            'attachments_jsonb':[{'filename':'견적서.pdf','mime_type':'application/pdf','size_bytes':7,
                                 'provider_part_ref':'0.1','provider_attachment_ref':'old-gmail-id'}]}


class SelectOnlyClient:
    def __init__(self, row):
        self.row=row;self.calls=[];self.filters={}
    def table(self,name):
        assert name=='mail_messages'
        self.calls.append(('table',name));return self
    def select(self,columns):
        self.calls.append(('select',columns));return self
    def eq(self,name,value):
        self.filters[name]=value;return self
    def limit(self,count):
        assert count==1;return self
    def execute(self):
        matches=self.filters=={'owner_user_id':self.row['owner_user_id'],'id':self.row['id']}
        return SimpleNamespace(data=[dict(self.row)] if matches else [])


def service(row):
    return OriginalMailService(OriginalMailRepository(SelectOnlyClient(row)))


def test_plain_text_metadata_and_utc_z(data):
    before=copy.deepcopy(data)
    result=service(data).get('owner',data['id']).model_dump(mode='json')
    assert result['received_at']=='2026-10-30T00:00:00Z'
    assert result['body_text']==data['body_text'] and result['recipients']==data['recipients_jsonb']
    assert result['source_type']=='GMAIL' and result['mail_classification']=='BUSINESS'
    assert set(result)=={'mail_id','subject','sender','recipients','received_at','body_text',
                         'attachments','mail_classification','source_type'}
    a=result['attachments'][0]
    assert set(a)=={'attachment_id','filename','mime_type','size_bytes'}
    assert a['filename']=='견적서.pdf' and a['size_bytes']==7
    assert a['attachment_id']!='old-gmail-id' and UUID(a['attachment_id'])
    assert data==before


@pytest.mark.parametrize('owner,mail_id',[('other',None),('owner','00000000-0000-0000-0000-000000000000')])
def test_other_owner_and_missing_mail_are_both_404(data,owner,mail_id):
    with pytest.raises(ServiceError) as error:
        service(data).get(owner,mail_id or data['id'])
    assert (error.value.status,error.value.code)==(404,'RESOURCE_NOT_FOUND')


def test_repository_selects_only_owner_scoped_original_fields(data):
    client=SelectOnlyClient(data)
    OriginalMailRepository(client).get('owner',data['id'])
    assert client.filters['owner_user_id']=='owner'
    columns=client.calls[1][1]
    assert 'body_text' in columns and 'normalized_jsonb' not in columns and 'html' not in columns
    assert 'provider_message_id' not in columns


def test_no_attachment_and_empty_plain_text_are_valid(data):
    data.update(attachments_jsonb=[],body_text=None)
    result=service(data).get('owner',data['id'])
    assert result.attachments==[] and result.body_text==''


def test_server_attachment_id_survives_reordering_and_gmail_id_rotation(data):
    first=data['attachments_jsonb'][0]
    old=attachment_id(data['id'],first,0)
    first['provider_attachment_ref']='rotated'
    assert attachment_id(data['id'],first,4)==old
    assert attachment_id(str(uuid4()),first,0)!=old


def test_null_new_part_ref_uses_valid_legacy_ref_and_preserves_root_id(data):
    from app.features.mail.domain.attachment_ids import part_reference
    assert part_reference({'provider_part_ref':None,'part_id':'0.1'})=='0.1'
    assert part_reference({'provider_part_ref':'','part_id':'legacy'})==''
    first=data['attachments_jsonb'][0]
    current=attachment_id(data['id'],first,0)
    first.update(provider_part_ref=None,part_id='0.1')
    assert attachment_id(data['id'],first,5)==current


def test_original_id_works_with_shared_attachment_download(data):
    public_id=service(data).get('owner',data['id']).attachments[0].attachment_id
    class Repo:
        def message(self,owner,mail_id):
            assert owner=='owner' and mail_id==data['id'];return data
    calls=[]
    class Google:
        def message(self,grant,mail_id):
            calls.append('message')
            return {'payload':{'parts':[{'partId':'0.1','mimeType':'application/pdf','filename':'견적서.pdf',
                                        'body':{'attachmentId':'fresh-id'}}]}}
        def attachment(self,grant,mail_id,provider_id):
            assert provider_id=='fresh-id'
            return {'data':base64.urlsafe_b64encode(b'content').decode(),'size':7}
    mailbox=SimpleNamespace(google=Google(),authorized=lambda *_:({'mailbox_ref':'box'},object()))
    result=fetch_gmail_attachment(Repo(),mailbox,'owner',data['id'],str(public_id))
    assert result.content==b'content' and calls==['message']


def test_storage_failure_never_echoes_body(data):
    class BadClient:
        def table(self,_):
            raise RuntimeError('sensitive body synthetic-token')
    with pytest.raises(ServiceError) as error:
        OriginalMailRepository(BadClient()).get('owner',data['id'])
    assert error.value.status==503 and 'sensitive' not in str(error.value)


@pytest.mark.parametrize('rows',[None,{},'private body',[None]])
def test_corrupt_sdk_response_is_not_reported_as_missing_mail(data,rows):
    client=SelectOnlyClient(data)
    client.execute=lambda:SimpleNamespace(data=rows)
    with pytest.raises(ServiceError) as caught:
        OriginalMailRepository(client).get('owner',data['id'])
    assert caught.value.status==503 and 'private body' not in str(caught.value)


def test_attachment_ownership_is_checked_before_oauth_configuration():
    class Repo:
        def message(self,*args):raise ServiceError('RESOURCE_NOT_FOUND','없음',404)
    def factory():raise AssertionError('OAuth must not be initialized')
    with pytest.raises(ServiceError) as caught:
        fetch_gmail_attachment(Repo(),factory,'other',str(uuid4()),str(uuid4()))
    assert caught.value.status==404


def test_missing_attachment_is_checked_before_oauth_configuration(data):
    class Repo:
        def message(self,*args):return data
    def factory():raise AssertionError('OAuth must not be initialized')
    with pytest.raises(ServiceError) as caught:
        fetch_gmail_attachment(Repo(),factory,'owner',data['id'],str(uuid4()))
    assert caught.value.status==404


def test_invalid_attachment_metadata_fails_without_echoing_it(data):
    data['attachments_jsonb'][0]['size_bytes']=-1
    with pytest.raises(ServiceError) as error:
        service(data).get('owner',data['id'])
    assert error.value.status==503 and '견적서' not in str(error.value)


def test_swagger_contract_exists():
    app=FastAPI();app.include_router(router)
    path=app.openapi()['paths']['/api/v1/mail-messages/{mail_id}']['get']
    assert '404' in path['responses'] and '200' in path['responses']


def test_http_envelope_and_no_store(data):
    app=FastAPI();app.include_router(router)
    @app.exception_handler(ServiceError)
    async def errors(request,error):
        return service_error_response(error)
    async def principal():return SimpleNamespace(user_id='owner')
    async def original_service():return service(data)
    app.dependency_overrides[require_google_user]=principal
    app.dependency_overrides[get_original_mail_service]=original_service
    with TestClient(app) as client:
        response=client.get('/api/v1/mail-messages/'+data['id'])
    assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
    assert set(response.json())=={'data','meta'}
    assert response.json()['data']['mail_id']==data['id']


def test_http_verified_identity_boundary_and_401_404(data,monkeypatch):
    from app.features.mailboxes.application.auth import google_principal
    import app.features.mailboxes.api.router as auth_router
    owner,other=str(uuid4()),str(uuid4())
    data['owner_user_id']=owner
    def transport(request):
        token=request.headers.get('authorization')
        if token not in {'Bearer owner-token','Bearer other-token'}:
            return httpx.Response(401,json={'message':'invalid'})
        uid=owner if token=='Bearer owner-token' else other
        return httpx.Response(200,json={'id':uid,'identities':[{'provider':'google','identity_data':{
            'sub':'synthetic-sub','email':'user@example.invalid'}}]})
    monkeypatch.setattr(auth_router,'google_principal',lambda token,settings:
        google_principal(token,settings,transport=httpx.MockTransport(transport)))
    app=FastAPI();app.include_router(router)
    app.state.settings=SimpleNamespace(supabase_url='https://synthetic.example.invalid',
                                      supabase_publishable_key=SecretStr('synthetic-public-key'))
    @app.exception_handler(ServiceError)
    async def errors(request,error):return service_error_response(error)
    async def original_service():return service(data)
    app.dependency_overrides[get_original_mail_service]=original_service
    path='/api/v1/mail-messages/'+data['id']
    with TestClient(app,base_url='https://backend.example.invalid') as client:
        assert client.get(path).status_code==401
        assert client.get(path,headers={'Authorization':'Bearer invalid'}).status_code==401
        assert client.get(path,headers={'Authorization':'Bearer owner-token'}).status_code==200
        response=client.get(path,headers={'Authorization':'Bearer other-token'})
        assert response.status_code==404 and response.json()['error']['code']=='RESOURCE_NOT_FOUND'
        assert client.get('/api/v1/mail-messages/'+str(uuid4()),headers={'Authorization':'Bearer owner-token'}).status_code==404
