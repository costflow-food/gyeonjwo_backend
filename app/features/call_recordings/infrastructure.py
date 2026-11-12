"""통화 녹음 DB(RPC)·Storage 어댑터. 파일 바이트는 서버를 거치지 않는다(서명 업로드 URL)."""
from datetime import datetime, timezone
import logging

from app.core.errors import ServiceError
from app.features.call_recordings.domain import BUCKET, CONTRACT_VERSION, MAX_BYTES, MAX_PER_CASE

logger = logging.getLogger('gyeonjwo.call_recordings')


def unavailable():
    return ServiceError('CALL_RECORDING_UNAVAILABLE', '통화 녹음 기능을 사용할 수 없습니다.', 503,
                        retryable=False, next_action='CONTACT_SERVER_ADMIN')


class CallRecordingRepository:
    def __init__(self, client):
        self.client = client

    def rpc(self, name, **params):
        try:
            return self.client.rpc(name, params).execute().data
        except Exception as error:
            if getattr(error, 'code', None) == 'PGRST202':
                raise unavailable() from None
            logger.warning('call_recording rpc_failed name=%s', name)
            raise ServiceError('PROCESSING_UNAVAILABLE', '녹음 정보를 저장소에서 처리하지 못했습니다.', 503) from None

    def health(self):
        return self.rpc('call_recording_health')

    def ready(self):
        """DB 계약(버킷·한도)이 이 코드와 같은지 확인한다."""
        result = self.health()
        return (isinstance(result, dict) and result.get('contract_version') in (CONTRACT_VERSION, CONTRACT_VERSION - 1)
                and result.get('ready') is True and result.get('bucket') == BUCKET
                and result.get('max_bytes') == MAX_BYTES and result.get('max_per_case') == MAX_PER_CASE)

    def create(self, owner, case_id, recording_id, supplier_id, file_name, extension, size_bytes, key, body_hash,
               request_id):
        """p_supplier 는 필수 인자다. DB 가 같은 트랜잭션에서 이 업무로 견적 요청(RFQ)을 보낸(SENT) 거래처인지 확인한다."""
        return self.rpc('create_call_recording', p_owner=str(owner), p_case=str(case_id), p_recording=str(recording_id),
                        p_supplier=str(supplier_id), p_file_name=file_name, p_extension=extension,
                        p_size_bytes=size_bytes, p_key=key, p_hash=body_hash, p_request_id=request_id)

    def find(self, owner, case_id, recording_id):
        """완료 처리 전 Storage 경로·상태를 읽는다. 없으면 None."""
        try:
            rows = (self.client.table('call_recordings').select('storage_path,status')
                    .eq('owner_user_id', str(owner)).eq('work_case_id', str(case_id)).eq('id', str(recording_id))
                    .limit(1).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '녹음 정보를 조회할 수 없습니다.', 503) from None
        return rows[0] if rows else None

    def complete(self, owner, case_id, recording_id, object_size):
        return self.rpc('complete_call_recording_upload', p_owner=str(owner), p_case=str(case_id),
                        p_recording=str(recording_id), p_object_size=object_size)

    def list(self, owner, case_id):
        return self.rpc('list_call_recordings', p_owner=str(owner), p_case=str(case_id))

    def expire_uploads(self, limit=100):
        return self.rpc('expire_call_recording_uploads', p_limit=limit)

    def claim_deletions(self, worker, limit=20, lease_seconds=120):
        return self.rpc('claim_call_recording_file_deletions', p_worker=worker, p_limit=limit,
                        p_lease_seconds=lease_seconds)

    def finish_deletion(self, deletion_id, worker, ok, error=None):
        return self.rpc('finish_call_recording_file_deletion', p_id=str(deletion_id), p_worker=worker,
                        p_ok=ok, p_error=error)

    def require_transcription_schema(self):
        if self.claim_transcription(None, 300, 'readiness') != [] or self.finish_transcription(None, None, None) is not False:
            raise RuntimeError('Unexpected transcription DB contract')

    def transcription_candidates(self, limit):
        now = datetime.now(timezone.utc).isoformat()
        rows = (self.client.table('call_recordings').select('id')
                .or_(f'stt_status.eq.rec-pending,and(stt_status.eq.rec-processing,stt_lease_until.lte.{now})')
                .order('stt_lease_until').order('id').limit(limit).execute().data)
        return [row['id'] for row in rows]

    def claim_transcription(self, recording_id, lease_seconds, worker):
        return self.rpc('claim_call_recording_stt', p_recording_id=recording_id, p_lease_seconds=lease_seconds, p_worker=worker)

    def finish_transcription(self, recording_id, lease_token, transcript, *, language=None, model=None, error=None):
        return self.rpc('finish_call_recording_stt', p_recording_id=recording_id, p_lease_token=lease_token,
                        p_transcript=transcript, p_language=language, p_model=model, p_error=error)


def _storage_failure():
    return ServiceError('CALL_RECORDING_STORAGE_UNAVAILABLE', '녹음 저장소에 연결할 수 없습니다.', 503, retryable=True)


def _is_not_found(error):
    status = str(getattr(error, 'status', '') or getattr(error, 'statusCode', '') or '')
    text = f'{getattr(error, "message", "")} {error}'.casefold()
    return status in {'400', '404'} and 'not found' in text or status == '404'


class CallRecordingStorage:
    def __init__(self, client):
        self.bucket = client.storage.from_(BUCKET)

    def signed_upload(self, path):
        """덮어쓰기 없는(upsert=false) 서명 업로드 URL. Storage 기본 유효시간은 2시간이다."""
        try:
            signed = self.bucket.create_signed_upload_url(path)
        except Exception:
            logger.warning('call_recording sign_failed')
            raise _storage_failure() from None
        if not signed.get('signed_url') or not signed.get('token') or signed.get('path') != path:
            raise _storage_failure()
        return signed

    def object_size(self, path):
        """올라간 파일의 실제 크기. 파일이 없으면 None."""
        try:
            info = self.bucket.info(path)
        except Exception as error:
            if _is_not_found(error):
                return None
            logger.warning('call_recording info_failed')
            raise _storage_failure() from None
        size = info.get('size') if isinstance(info, dict) else None
        if size is None and isinstance(info, dict):
            size = (info.get('metadata') or {}).get('size')
        if type(size) is not int or size < 0:
            raise _storage_failure()
        return size

    def download(self, path):
        return self.bucket.download(path)

    def remove(self, paths):
        removed = self.bucket.remove(list(paths))
        if not isinstance(removed, list):
            return []
        return [item['name'] for item in removed if isinstance(item, dict) and isinstance(item.get('name'), str)]
