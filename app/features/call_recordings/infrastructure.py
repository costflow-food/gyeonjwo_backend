"""통화 녹음 DB(RPC)·Storage 어댑터. 파일 바이트는 서버를 거치지 않는다(서명 업로드 URL)."""
import logging
from datetime import datetime, timezone

import httpx

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
        return (isinstance(result, dict) and result.get('contract_version') == CONTRACT_VERSION
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

    def remove(self, paths):
        # 없는 파일 삭제도 성공으로 돌아온다(멱등).
        self.bucket.remove(list(paths))

    def download_to(self, path, target, *, transport=None):
        """비공개 파일을 짧은 서명 URL로 받아 target 파일에 스트리밍으로 쓴다(메모리에 통째로 올리지 않는다)."""
        try:
            signed = self.bucket.create_signed_url(path, 300)
        except Exception:
            logger.warning('call_recording download_sign_failed')
            raise _storage_failure() from None
        url = signed.get('signedURL') or signed.get('signedUrl') or signed.get('signed_url') if isinstance(signed, dict) else None
        if not url:
            raise _storage_failure()
        try:
            with httpx.Client(timeout=120, transport=transport) as client, client.stream('GET', url) as response:
                if response.status_code == 404:
                    raise ServiceError('CALL_RECORDING_NOT_FOUND', '녹음 파일이 저장소에 없습니다.', 404)
                response.raise_for_status()
                with open(target, 'wb') as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
        except ServiceError:
            raise
        except Exception:
            logger.warning('call_recording download_failed')
            raise _storage_failure() from None


# 통화 회신 연결(BE 담당 DB 변경)
CALL_REPLY_RPC = {'register': 'register_call_reply', 'release_audio': 'release_extracted_call_audio'}
_RECORDING_COLUMNS = 'id,owner_user_id,work_case_id,supplier_id,storage_path,file_name,extension,size_bytes,status'


def _expired(value, now):
    """임대 만료 시각(ISO 문자열)이 지났는지."""
    try:
        when = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return False
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)) <= now


class CallTranscriptionRepository(CallRecordingRepository):
    """전사 작업(KAN-250·254·255, call_recordings의 stt_* 컬럼)과 통화 회신 연결 RPC.

    계약: 녹음이 UPLOADED가 되면 stt_status가 rec-pending이 된다. claim_call_recording_stt(녹음 ID, 임대 초, 워커)가
    rec-processing으로 바꾸고 lease_token을 준다(시도 5번을 넘으면 rec-failed). finish_call_recording_stt(녹음 ID, lease_token,
    전사문, 언어, 모델, 실패 사유)가 저장한다. 전사문이 NULL이면 DB가 rec-pending으로 되돌려 다시 시도하게 하거나(5번까지)
    rec-failed로 끝낸다."""

    def capabilities(self):
        """call_recording_health의 기능 목록. 읽지 못하면 빈 집합."""
        try:
            result = self.health()
        except ServiceError:
            return set()
        values = result.get('capabilities') if isinstance(result, dict) else None
        return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()

    def transcription_ready(self):
        """DB가 KAN-254 전사 계약(워커 이름을 받는 임대·결과 저장)을 제공하는지. 없는 녹음으로 불러 확인한다(아무것도 바꾸지 않음)."""
        try:
            claimed = self.rpc('claim_call_recording_stt', p_recording_id=None, p_lease_seconds=60, p_worker='readiness-probe')
            finished = self.rpc('finish_call_recording_stt', p_recording_id=None, p_lease_token=None, p_transcript=None,
                                p_language=None, p_model=None, p_error=None)
        except ServiceError:
            return False
        return claimed == [] and finished is False

    def call_reply_ready(self):
        """DB가 통화 회신 연결(CALL_REPLY)을 제공하는지."""
        return 'CALL_REPLY' in self.capabilities()

    def pending_recordings(self, limit):
        """전사를 기다리는 녹음 ID: rec-pending이거나 임대가 끝난 rec-processing. 오래된 것부터."""
        try:
            rows = (self.client.table('call_recordings').select('id,stt_status,stt_lease_until')
                    .eq('status', 'UPLOADED').in_('stt_status', ['rec-pending', 'rec-processing'])
                    .order('uploaded_at').limit(max(limit * 5, 10)).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '전사 대기 녹음을 조회할 수 없습니다.', 503) from None
        now = datetime.now(timezone.utc)
        return [row['id'] for row in rows or []
                if row.get('stt_status') == 'rec-pending' or _expired(row.get('stt_lease_until'), now)][:limit]

    def claim_transcriptions(self, worker, limit=1, lease_seconds=1800):
        """대기 녹음을 하나씩 임대한다. 다른 워커가 먼저 가져간 녹음은 빈 결과라 건너뛴다."""
        from app.features.call_recordings.transcription import TranscriptionJob
        jobs = []
        for recording_id in self.pending_recordings(limit):
            claimed = self.rpc('claim_call_recording_stt', p_recording_id=str(recording_id),
                               p_lease_seconds=lease_seconds, p_worker=worker)
            if not isinstance(claimed, list) or not claimed:
                continue
            token = claimed[0].get('lease_token')
            row = self.recording(recording_id)
            if not token or row is None:
                continue
            jobs.append(TranscriptionJob(
                job_id=str(recording_id), recording_id=str(recording_id), owner_user_id=str(row['owner_user_id']),
                case_id=str(row['work_case_id']), supplier_id=str(row['supplier_id']), storage_path=row['storage_path'],
                file_name=row['file_name'], extension=row['extension'], size_bytes=int(row['size_bytes']),
                claim_token=str(token), hint_item_name=self.item_name(row), hint_supplier_name=self.supplier_name(row)))
        return jobs

    def recording(self, recording_id):
        """녹음 행. 없으면 None."""
        rows = (self.client.table('call_recordings').select(_RECORDING_COLUMNS)
                .eq('id', str(recording_id)).limit(1).execute().data)
        return rows[0] if rows else None

    def item_name(self, row):
        """STT 문맥 설명용 업무 품목명. 읽지 못하면 None."""
        try:
            rows = (self.client.table('work_cases').select('item_name').eq('owner_user_id', str(row['owner_user_id']))
                    .eq('id', str(row['work_case_id'])).limit(1).execute().data)
        except Exception:
            return None
        return (rows[0].get('item_name') if rows else None) or None

    def supplier_name(self, row):
        """STT 문맥 설명용 거래처 이름. 읽지 못하면 None."""
        try:
            rows = (self.client.table('suppliers').select('name').eq('owner_user_id', str(row['owner_user_id']))
                    .eq('id', str(row['supplier_id'])).limit(1).execute().data)
        except Exception:
            return None
        return (rows[0].get('name') if rows else None) or None

    def finish_transcription(self, job, worker, result):
        """전사문(근거 본문)과 모델을 저장한다. False면 임대를 잃었다(다른 워커가 다시 가져갔거나 임대가 끝났다)."""
        return self.rpc('finish_call_recording_stt', p_recording_id=job.recording_id, p_lease_token=job.claim_token,
                        p_transcript=result.body_text, p_language='ko', p_model=result.model, p_error=None) is True

    def fail_transcription(self, job, worker, code, retryable):
        """전사 실패를 실패 사유와 함께 저장한다. 다시 시도할지는 DB가 시도 횟수로 정한다(5번까지).
        음성 파일은 지우지 않는다."""
        logger.warning('call_recording transcription_failed recording=%s code=%s retryable=%s',
                       job.recording_id, code, retryable)
        return self.rpc('finish_call_recording_stt', p_recording_id=job.recording_id, p_lease_token=job.claim_token,
                        p_transcript=None, p_language=None, p_model=None, p_error=code) is True

    def register_call_reply(self, job, result):
        """전사문을 메일 회신과 같은 회신 자료(통화 행·회신 이벤트)로 넣는다. 같은 녹음은 한 번만 들어간다."""
        return self.rpc(CALL_REPLY_RPC['register'], p_owner=job.owner_user_id, p_recording=job.recording_id,
                        p_body=result.body_text,
                        p_call_started_at=result.call_started_at.isoformat() if result.call_started_at else None)

    def release_extracted_audio(self, limit=20):
        """견적 추출(①)이 끝난 통화의 음성 파일을 삭제 대기열에 넣는다. 넣은 수를 돌려준다."""
        return self.rpc(CALL_REPLY_RPC['release_audio'], p_limit=limit)
