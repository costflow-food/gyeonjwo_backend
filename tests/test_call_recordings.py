"""KAN-248 통화 녹음 업로드 API: 서비스·라우터·정리 워커 (DB·Storage 는 가짜로 대체)."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.responses import service_error_response
from app.core.errors import ServiceError
from app.features.call_recordings.application import CallRecordingService
from app.features.call_recordings.domain import (BUCKET, CONTRACT_VERSION, ERRORS, MAX_BYTES, MAX_PER_CASE,
                                                 CreateRecordingRequest)
from app.features.call_recordings.infrastructure import CallRecordingRepository
from app.features.call_recordings.router import get_call_recording_service, router
from app.features.call_recordings.worker import CallRecordingCleanupWorker
from app.features.mailboxes.api.router import require_google_user

OWNER = 'f412a4ec-faf9-4797-bc88-6be7bc840a38'
CASE = UUID('faf2bb00-0b41-442f-86e5-f896516a0141')
SUPPLIER = UUID('5d0c2f3a-6f0e-4c58-9d39-2f1b7a8f4c11')        # 이 업무에서 견적 요청을 보낸 거래처
OTHER_SUPPLIER = UUID('8a41d7b2-1c3e-4f6a-b0d5-93e2c7a6f540')  # 견적 요청을 보낸 또 다른 거래처
UNSENT_SUPPLIER = UUID('c7e9a1d4-52b8-4e0f-8a63-0b9d4f2e6a17')  # 견적 요청을 보내지 않은 거래처
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)


def row(recording_id, status='UPLOADING', expires=NOW + timedelta(hours=2), ext='m4a', size=1234, supplier=SUPPLIER):
    return {'recording_id': str(recording_id), 'case_id': str(CASE), 'supplier_id': str(supplier),
            'file_name': f'통화.{ext}', 'extension': ext,
            'content_type': 'audio/mp4', 'size_bytes': size, 'status': status, 'failure_reason': None,
            'created_at': NOW.isoformat(), 'uploaded_at': None,
            'storage_path': f'{OWNER}/{CASE}/{recording_id}.{ext}', 'upload_expires_at': expires.isoformat()}


class FakeRepo:
    """DB 함수의 응답 모양만 흉내 낸다. 규칙 자체는 SQL 드라이런에서 검증한다."""

    def __init__(self):
        self.created, self.by_key, self.rows, self.completed, self.suppliers = [], {}, {}, [], []
        self.next_error = None
        self.sent_suppliers = {str(SUPPLIER), str(OTHER_SUPPLIER)}
        self.without_supplier = False  # DB 가 계약 2 이전일 때의 응답(거래처 없음)을 흉내 낸다

    def create(self, owner, case_id, recording_id, supplier_id, file_name, extension, size_bytes, key, body_hash,
               request_id):
        self.created.append((owner, case_id, recording_id, file_name, extension, size_bytes, key, body_hash))
        self.suppliers.append(supplier_id)
        if self.next_error:
            return {'kind': 'ERROR', 'code': self.next_error}
        if key not in self.by_key and str(supplier_id) not in self.sent_suppliers:
            return {'kind': 'ERROR', 'code': 'CALL_RECORDING_SUPPLIER_NOT_ALLOWED'}
        if key in self.by_key:
            first_hash, first_id = self.by_key[key]
            if first_hash != body_hash:
                return {'kind': 'ERROR', 'code': 'IDEMPOTENCY_KEY_REUSED'}
            return {'kind': 'REPLAY', 'http_status': 201, 'response': {'data': {}}, 'recording': self.rows[first_id]}
        self.by_key[key] = (body_hash, recording_id)
        self.rows[recording_id] = row(recording_id, ext=extension, size=size_bytes, supplier=supplier_id)
        if self.without_supplier:
            del self.rows[recording_id]['supplier_id']
        return {'kind': 'CREATED', 'http_status': 201, 'response': {'data': {}}, 'recording': self.rows[recording_id]}

    def find(self, owner, case_id, recording_id):
        current = self.rows.get(recording_id)
        return current and {'storage_path': current['storage_path'], 'status': current['status']}

    def complete(self, owner, case_id, recording_id, object_size):
        self.completed.append(object_size)
        current = self.rows.get(recording_id)
        if current is None:
            return {'kind': 'ERROR', 'code': 'CALL_RECORDING_NOT_FOUND'}
        if current['status'] == 'UPLOADED':
            return {'kind': 'OK', 'recording': current}
        if object_size is None:
            return {'kind': 'ERROR', 'code': 'CALL_RECORDING_UPLOAD_INCOMPLETE'}
        if object_size != current['size_bytes']:
            current['status'], current['failure_reason'] = 'FAILED', 'SIZE_MISMATCH'
            return {'kind': 'ERROR', 'code': 'CALL_RECORDING_SIZE_MISMATCH'}
        current['status'], current['uploaded_at'] = 'UPLOADED', NOW.isoformat()
        return {'kind': 'OK', 'recording': current}

    def list(self, owner, case_id):
        if str(case_id) != str(CASE):
            return {'kind': 'ERROR', 'code': 'CALL_RECORDING_CASE_NOT_FOUND'}
        items = [{k: v for k, v in r.items() if k not in ('storage_path', 'upload_expires_at')} for r in self.rows.values()]
        return {'kind': 'OK', 'items': items, 'limits': {'max_per_case': 10, 'max_bytes': 52428800}}


class FakeStorage:
    def __init__(self):
        self.objects, self.signed, self.removed, self.fail_remove, self.fail_sign = {}, [], [], False, False

    def signed_upload(self, path):
        if path in self.objects or self.fail_sign:
            # 실제 Storage 처럼: 이미 있는 경로는 서명을 거절한다(덮어쓰기 불가).
            raise ServiceError('CALL_RECORDING_STORAGE_UNAVAILABLE', '녹음 저장소에 연결할 수 없습니다.', 503,
                               retryable=True)
        self.signed.append(path)
        return {'signed_url': f'https://storage.example/upload/sign/call-recordings/{path}?token=t{len(self.signed)}',
                'token': f't{len(self.signed)}', 'path': path}

    def object_size(self, path):
        return self.objects.get(path)

    def remove(self, paths):
        if self.fail_remove:
            raise RuntimeError('storage down')
        self.removed.extend(paths)


def request(**overrides):
    body = {'supplier_id': str(SUPPLIER), 'file_name': '통화_2026-11-11.m4a', 'extension': 'm4a', 'size_bytes': 1234,
            **overrides}
    return CreateRecordingRequest(**body)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.storage = FakeRepo(), FakeStorage()
        self.clock = [NOW]
        self.service = CallRecordingService(self.repo, self.storage, clock=lambda: self.clock[0])

    def code(self, call):
        with self.assertRaises(ServiceError) as raised:
            call()
        return raised.exception.code, raised.exception.status

    def test_create_returns_upload_target_without_storing_url(self):
        status, data = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.assertEqual(status, 201)
        self.assertEqual(data['status'], 'UPLOADING')
        self.assertNotIn('storage_path', data)
        self.assertEqual(data['upload']['method'], 'PUT')
        self.assertEqual(data['upload']['content_type'], 'audio/mp4')
        self.assertTrue(data['upload']['path'].startswith(f'{OWNER}/{CASE}/'))

    def test_same_key_replays_same_recording_with_fresh_url(self):
        _, first = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        status, second = self.service.create(OWNER, CASE, request(), 'key-1', 'req2')
        self.assertEqual((status, second['recording_id']), (201, first['recording_id']))
        self.assertEqual(len(self.repo.rows), 1)
        self.assertNotEqual(first['upload']['token'], second['upload']['token'])

    def test_same_key_after_file_uploaded_returns_no_upload(self):
        # 파일은 올라갔지만 완료 요청 전에 끊긴 뒤 재시도: 503 대신 업로드 자리 없이 돌려줘 완료로 넘어가게 한다.
        _, first = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.storage.objects[first['upload']['path']] = 1234
        status, second = self.service.create(OWNER, CASE, request(), 'key-1', 'req2')
        self.assertEqual((status, second['recording_id'], second['status'], second['upload']),
                         (201, first['recording_id'], 'UPLOADING', None))
        done = self.service.complete(OWNER, CASE, UUID(first['recording_id']))
        self.assertEqual(done['status'], 'UPLOADED')

    def test_sign_failure_without_file_stays_storage_unavailable(self):
        self.storage.fail_sign = True
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(), 'key-1', 'req')),
                         ('CALL_RECORDING_STORAGE_UNAVAILABLE', 503))

    def test_size_bytes_must_be_integer(self):
        for bad in ('12', 12.0, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                request(size_bytes=bad)

    def test_same_key_other_body_rejected(self):
        self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(size_bytes=99), 'key-1', 'r')),
                         ('IDEMPOTENCY_KEY_REUSED', 409))

    def test_extension_normalized_lowercase(self):
        self.service.create(OWNER, CASE, request(extension='.M4A'), 'key-1', 'req')
        self.assertEqual(self.repo.created[0][4], 'm4a')

    def test_validation_errors_use_call_recording_codes(self):
        cases = [(request(extension='exe'), ('CALL_RECORDING_UNSUPPORTED_TYPE', 415)),
                 (request(size_bytes=52428801), ('CALL_RECORDING_TOO_LARGE', 413)),
                 (request(size_bytes=0), ('CALL_RECORDING_INVALID_REQUEST', 422)),
                 (request(file_name='a/b.m4a'), ('CALL_RECORDING_INVALID_REQUEST', 422)),
                 (request(file_name=' '), ('CALL_RECORDING_INVALID_REQUEST', 422))]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, payload, 'k', 'r')), expected)
        self.assertEqual(self.repo.created, [])

    def test_exact_limit_is_allowed(self):
        status, _ = self.service.create(OWNER, CASE, request(size_bytes=52428800), 'k', 'r')
        self.assertEqual(status, 201)

    def test_db_errors_map_to_http(self):
        for code, status in (('CALL_RECORDING_CASE_NOT_FOUND', 404), ('CALL_RECORDING_LIMIT_EXCEEDED', 409),
                             ('REQUEST_IN_PROGRESS', 409)):
            self.repo.next_error = code
            self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(), 'k', 'r')), (code, status))

    def test_unknown_db_code_is_not_leaked(self):
        self.repo.next_error = 'SOMETHING_ELSE'
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(), 'k', 'r'))[0],
                         'CALL_RECORDING_UNAVAILABLE')

    def test_missing_idempotency_key(self):
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(), None, 'r')), ('INVALID_REQUEST', 422))

    def test_no_url_after_upload_window(self):
        _, first = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.clock[0] = NOW + timedelta(hours=3)
        _, replay = self.service.create(OWNER, CASE, request(), 'key-1', 'req2')
        self.assertIsNone(replay['upload'])
        self.assertEqual(len(self.storage.signed), 1)

    def test_complete_flow(self):
        _, data = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        rid = UUID(data['recording_id'])
        self.assertEqual(self.code(lambda: self.service.complete(OWNER, CASE, rid)), ('CALL_RECORDING_UPLOAD_INCOMPLETE', 409))
        self.storage.objects[data['upload']['path']] = 1234
        done = self.service.complete(OWNER, CASE, rid)
        self.assertEqual(done['status'], 'UPLOADED')
        again = self.service.complete(OWNER, CASE, rid)
        self.assertEqual(again, done)
        # 완료된 녹음은 Storage 를 다시 보지 않는다
        self.assertEqual(self.repo.completed, [None, 1234, None])

    def test_complete_size_mismatch(self):
        _, data = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.storage.objects[data['upload']['path']] = 99
        self.assertEqual(self.code(lambda: self.service.complete(OWNER, CASE, UUID(data['recording_id']))),
                         ('CALL_RECORDING_SIZE_MISMATCH', 409))

    def test_complete_unknown(self):
        self.assertEqual(self.code(lambda: self.service.complete(OWNER, CASE, uuid4())), ('CALL_RECORDING_NOT_FOUND', 404))

    def test_list(self):
        self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        data = self.service.list(OWNER, CASE)
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(data['limits']['max_per_case'], 10)
        self.assertEqual(self.code(lambda: self.service.list(OWNER, uuid4())), ('CALL_RECORDING_CASE_NOT_FOUND', 404))

    def test_create_sends_supplier_and_returns_it(self):
        _, data = self.service.create(OWNER, CASE, request(supplier_id=str(OTHER_SUPPLIER)), 'key-1', 'req')
        self.assertEqual(self.repo.suppliers, [OTHER_SUPPLIER])
        self.assertEqual(data['supplier_id'], str(OTHER_SUPPLIER))

    def test_supplier_without_sent_rfq_rejected(self):
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(supplier_id=str(UNSENT_SUPPLIER)),
                                                               'key-1', 'req')),
                         ('CALL_RECORDING_SUPPLIER_NOT_ALLOWED', 422))
        self.assertEqual(self.repo.rows, {})
        self.assertEqual(self.storage.signed, [])

    def test_same_key_other_supplier_rejected(self):
        self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(supplier_id=str(OTHER_SUPPLIER)),
                                                               'key-1', 'r')),
                         ('IDEMPOTENCY_KEY_REUSED', 409))

    def test_list_and_complete_carry_supplier(self):
        _, data = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        self.storage.objects[data['upload']['path']] = 1234
        self.assertEqual(self.service.complete(OWNER, CASE, UUID(data['recording_id']))['supplier_id'], str(SUPPLIER))
        self.assertEqual(self.service.list(OWNER, CASE)['items'][0]['supplier_id'], str(SUPPLIER))

    def test_db_before_supplier_contract_is_unavailable(self):
        """DB 가 아직 거래처를 돌려주지 않으면(계약 2 이전) 거래처 없는 녹음을 내보내지 않고 닫는다."""
        self.repo.without_supplier = True
        unavailable = ('CALL_RECORDING_UNAVAILABLE', 503)
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(), 'key-1', 'req')), unavailable)
        self.assertEqual(self.storage.signed, [])
        recording_id = next(iter(self.repo.rows))
        self.storage.objects[self.repo.rows[recording_id]['storage_path']] = 1234
        self.assertEqual(self.code(lambda: self.service.complete(OWNER, CASE, recording_id)), unavailable)
        self.assertEqual(self.code(lambda: self.service.list(OWNER, CASE)), unavailable)

    def test_replay_after_case_deleted_returns_stored_response(self):
        """업무가 수용(삭제)된 뒤 같은 키 재요청: 녹음 행은 없고 저장된 첫 응답만 온다."""
        _, first = self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        stored = {k: v for k, v in first.items() if k != 'upload'}
        self.repo.create = lambda *args: {'kind': 'REPLAY', 'http_status': 201, 'response': {'data': stored},
                                          'recording': None}
        status, data = self.service.create(OWNER, CASE, request(), 'key-1', 'req2')
        self.assertEqual((status, data['supplier_id'], data['upload']), (201, str(SUPPLIER), None))
        self.assertEqual(len(self.storage.signed), 1)
        # 계약 2 이전에 저장된 응답(거래처 없음)은 내보내지 않는다
        del stored['supplier_id']
        self.assertEqual(self.code(lambda: self.service.create(OWNER, CASE, request(), 'key-1', 'req3')),
                         ('CALL_RECORDING_UNAVAILABLE', 503))

    def test_null_supplier_is_not_returned(self):
        """거래처가 null 인 행(계약 2 이전에 만들어진 녹음)도 거래처 없는 녹음으로 본다."""
        self.service.create(OWNER, CASE, request(), 'key-1', 'req')
        next(iter(self.repo.rows.values()))['supplier_id'] = None
        self.assertEqual(self.code(lambda: self.service.list(OWNER, CASE)), ('CALL_RECORDING_UNAVAILABLE', 503))

    def test_error_table_statuses(self):
        self.assertEqual(ERRORS['CALL_RECORDING_UPLOAD_EXPIRED'][0], 410)
        self.assertTrue(all(code.startswith('CALL_RECORDING_') for code in ERRORS
                            if code not in {'IDEMPOTENCY_KEY_REUSED', 'REQUEST_IN_PROGRESS'}))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.storage = FakeRepo(), FakeStorage()
        service = CallRecordingService(self.repo, self.storage, clock=lambda: NOW)
        app = FastAPI()
        app.include_router(router)
        app.add_exception_handler(ServiceError, lambda request, error: service_error_response(error))
        app.dependency_overrides[require_google_user] = lambda: SimpleNamespace(user_id=OWNER)
        app.dependency_overrides[get_call_recording_service] = lambda: service
        self.client = TestClient(app)
        self.base = f'/api/v1/work-cases/{CASE}/call-recordings'

    def test_create_complete_list(self):
        body = {'supplier_id': str(SUPPLIER), 'file_name': '통화.m4a', 'extension': 'm4a', 'size_bytes': 1234}
        created = self.client.post(self.base, json=body, headers={'Idempotency-Key': 'k-1'})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers['cache-control'], 'private, no-store')
        data = created.json()['data']
        self.assertEqual(data['supplier_id'], str(SUPPLIER))
        self.assertEqual(data['upload']['method'], 'PUT')
        incomplete = self.client.post(f"{self.base}/{data['recording_id']}/complete")
        self.assertEqual(incomplete.status_code, 409)
        self.assertEqual(incomplete.json()['error']['code'], 'CALL_RECORDING_UPLOAD_INCOMPLETE')
        self.assertEqual(incomplete.json()['error']['next_action'], 'UPLOAD_FILE')
        self.storage.objects[data['upload']['path']] = 1234
        done = self.client.post(f"{self.base}/{data['recording_id']}/complete")
        self.assertEqual((done.status_code, done.json()['data']['status']), (200, 'UPLOADED'))
        listed = self.client.get(self.base)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()['data']['items'][0]['status'], 'UPLOADED')
        self.assertEqual(listed.json()['data']['items'][0]['supplier_id'], str(SUPPLIER))
        self.assertNotIn('storage_path', listed.json()['data']['items'][0])

    def test_unsupported_type_415(self):
        response = self.client.post(self.base, json={'supplier_id': str(SUPPLIER), 'file_name': 'a.exe',
                                                     'extension': 'exe', 'size_bytes': 1},
                                    headers={'Idempotency-Key': 'k-2'})
        self.assertEqual((response.status_code, response.json()['error']['code']), (415, 'CALL_RECORDING_UNSUPPORTED_TYPE'))

    def test_too_large_413(self):
        response = self.client.post(self.base, json={'supplier_id': str(SUPPLIER), 'file_name': 'a.wav',
                                                     'extension': 'wav', 'size_bytes': 52428801},
                                    headers={'Idempotency-Key': 'k-3'})
        self.assertEqual((response.status_code, response.json()['error']['code']), (413, 'CALL_RECORDING_TOO_LARGE'))

    def test_supplier_is_required(self):
        for body in ({'file_name': '통화.m4a', 'extension': 'm4a', 'size_bytes': 1234},
                     {'supplier_id': 'not-a-uuid', 'file_name': '통화.m4a', 'extension': 'm4a', 'size_bytes': 1234}):
            with self.subTest(body=body):
                response = self.client.post(self.base, json=body, headers={'Idempotency-Key': 'k-4'})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.repo.created, [])

    def test_supplier_without_sent_rfq_422(self):
        response = self.client.post(self.base, json={'supplier_id': str(UNSENT_SUPPLIER), 'file_name': '통화.m4a',
                                                     'extension': 'm4a', 'size_bytes': 1234},
                                    headers={'Idempotency-Key': 'k-5'})
        error = response.json()['error']
        self.assertEqual((response.status_code, error['code']), (422, 'CALL_RECORDING_SUPPLIER_NOT_ALLOWED'))
        # 다시 보내도 같은 결과다: 앱은 재시도하지 않고 업무(견적 요청 대상)를 새로 읽는다
        self.assertEqual((error['retryable'], error['next_action']), (False, 'REFRESH_CASE'))


class RepositoryTests(unittest.TestCase):
    def repo(self, data):
        client = MagicMock()
        client.rpc.return_value.execute.return_value.data = data
        return CallRecordingRepository(client), client

    def test_create_passes_supplier_to_db(self):
        repo, client = self.repo({'kind': 'CREATED'})
        recording_id = uuid4()
        repo.create(OWNER, CASE, recording_id, SUPPLIER, '통화.m4a', 'm4a', 1234, 'key-1', 'hash', 'req')
        client.rpc.assert_called_once_with('create_call_recording', {
            'p_owner': OWNER, 'p_case': str(CASE), 'p_recording': str(recording_id), 'p_supplier': str(SUPPLIER),
            'p_file_name': '통화.m4a', 'p_extension': 'm4a', 'p_size_bytes': 1234,
            'p_key': 'key-1', 'p_hash': 'hash', 'p_request_id': 'req'})

    def test_db_without_supplier_argument_is_unavailable(self):
        """DB 함수가 아직 p_supplier 를 받지 않으면(계약 1) PostgREST 가 함수를 찾지 못한다: 기능 미준비로 닫는다."""
        client = MagicMock()
        client.rpc.return_value.execute.side_effect = type('ApiError', (Exception,), {'code': 'PGRST202'})()
        with self.assertRaises(ServiceError) as raised:
            CallRecordingRepository(client).create(OWNER, CASE, uuid4(), SUPPLIER, '통화.m4a', 'm4a', 1234, 'k', 'h', 'r')
        self.assertEqual((raised.exception.code, raised.exception.status, raised.exception.retryable),
                         ('CALL_RECORDING_UNAVAILABLE', 503, False))

    def test_ready_needs_supplier_contract(self):
        health = {'contract_version': CONTRACT_VERSION, 'ready': True, 'bucket': BUCKET, 'max_bytes': MAX_BYTES,
                  'max_per_case': MAX_PER_CASE}
        self.assertEqual(CONTRACT_VERSION, 3)
        self.assertTrue(self.repo(health)[0].ready())
        self.assertTrue(self.repo({**health, 'contract_version': 2})[0].ready())
        # 거래처 추가분이 적용되기 전의 DB(계약 1)에서는 기능을 켜지 않는다
        self.assertFalse(self.repo({**health, 'contract_version': 1})[0].ready())


class WorkerRepo:
    def __init__(self, jobs):
        self.jobs, self.finished = jobs, []

    def expire_uploads(self, limit):
        return 1

    def claim_deletions(self, worker, limit, lease):
        jobs, self.jobs = self.jobs, []
        return jobs

    def finish_deletion(self, deletion_id, worker, ok, error=None):
        self.finished.append((deletion_id, ok, error))


class WorkerTests(unittest.TestCase):
    def test_deletes_claimed_files(self):
        repo, storage = WorkerRepo([{'id': 'a', 'storage_path': 'o/c/1.m4a'}, {'id': 'b', 'storage_path': 'o/c/2.m4a'}]), FakeStorage()
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 1, 'deleted': 2})
        self.assertEqual(storage.removed, ['o/c/1.m4a', 'o/c/2.m4a'])
        self.assertEqual([f[1] for f in repo.finished], [True, True])

    def test_storage_failure_reported_for_retry(self):
        repo, storage = WorkerRepo([{'id': 'a', 'storage_path': 'o/c/1.m4a'}]), FakeStorage()
        storage.fail_remove = True
        CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(repo.finished, [('a', False, 'RuntimeError')])

    def test_nothing_to_do(self):
        repo = WorkerRepo([])
        self.assertEqual(CallRecordingCleanupWorker(repo, FakeStorage()).run_once(), {'expired': 1, 'deleted': 0})


if __name__ == '__main__':
    unittest.main()
