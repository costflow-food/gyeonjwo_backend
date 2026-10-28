import logging
import mimetypes
import threading

from app.features.mail.application.classification import classify
from app.shared.infrastructure.db.contracts import RpcRepository, WORK_COLUMNS, WorkNotFound, OwnershipLost
from app.shared.infrastructure.db.references import ReferenceRepository
from app.shared.infrastructure.db.storage import SourceStorage, mail_path
from app.features.review.infrastructure.documents import parse_mail, source_file
from app.core.errors import ReviewRequired, ServiceError
from app.features.mail.infrastructure.gmail import GmailClient
from app.core.leases import LeaseHeartbeat
from app.shared.infrastructure.llm import JsonAgent
from app.features.review.application.pipeline import AnalysisPipeline
from app.features.review.infrastructure.reference_files import download_reference, reference_id, reference_path

logger = logging.getLogger('gyeonjwo')
PUBLIC_WORK_COLUMNS = ('work_id', 'received_at', 'sender', 'subject', 'classification_reason', 'notes',
                       'queue_status', 'item_id', 'created_at', 'updated_at')


class Services:
    def __init__(self, settings, client, agent=None, gmail=None):
        self.settings, self.client = settings, client
        self.rpc = RpcRepository(client)
        self.references, self.storage = ReferenceRepository(client), SourceStorage(client)
        self.agent, self.gmail = agent or JsonAgent(settings), gmail or GmailClient(settings)
        self.analysis_lock = threading.Lock()
        self.pipeline = AnalysisPipeline(settings, self.agent, self.references, self.storage, client)

    def work(self, work_id):
        rows = self.client.table('work_items').select(WORK_COLUMNS).eq('work_id', work_id).limit(1).execute().data
        if not rows:
            raise ServiceError('WORK_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]

    def source(self, work_id):
        self.work(work_id)
        result = self.rpc.call('get_work_item_source', p_work_id=work_id)
        if not isinstance(result, dict):
            raise ServiceError('SOURCE_READ_FAILED', '원문 정보를 찾을 수 없습니다.', 502)
        return result

    def raw(self, work_id, source):
        try:
            return self.storage.download_verified(mail_path(work_id), source['raw_size_bytes'], source['raw_sha256'])
        except Exception:
            raise ServiceError('SOURCE_READ_FAILED', '원문 파일 검증에 실패했습니다.', 502) from None

    def detail(self, work_id):
        work = self.work(work_id)
        source = self.source(work_id)
        try:
            headers = parse_mail(self.raw(work_id, source))['headers']
        except ValueError:
            raise ServiceError('SOURCE_READ_FAILED', '원문 메일 헤더를 읽을 수 없습니다.', 502) from None
        return {'work': {key: work[key] for key in PUBLIC_WORK_COLUMNS}, 'mail_header': headers}

    def files(self, work_id):
        source = self.source(work_id)
        names = ['mail.eml'] + [row['storage_path'].rsplit('/', 1)[-1] for row in source.get('attachments', [])]
        return {'work_id': work_id, 'files': [
            {'filename': name, **{key: value for key, value in source_file(source, work_id, name).items()
                                  if key in ('original_name', 'content_type', 'size_bytes')}} for name in names]}

    def file(self, work_id, filename):
        source = self.source(work_id)
        try:
            meta = source_file(source, work_id, filename)
        except ValueError:
            raise ServiceError('INVALID_FILENAME', '허용하지 않은 파일 경로입니다.', 422) from None
        try:
            content = self.storage.download_verified(meta['storage_path'], meta['size_bytes'], meta['sha256'])
        except Exception:
            raise ServiceError('SOURCE_READ_FAILED', '원문 파일을 읽거나 검증할 수 없습니다.', 502) from None
        return meta, content

    def reclassify(self, work_id):
        work = self.work(work_id)
        if work['queue_status'] == 'RUNNING':
            raise ServiceError('WORK_RUNNING', '이미 분석 중인 업무입니다.', 409)
        if work['queue_status'] == 'COMPLETE':
            raise ServiceError('INVALID_WORK_STATE', '종료된 업무는 재분류할 수 없습니다. 다시 분석할 수 있습니다.', 409)
        token = self.rpc.call('restart_classification', p_work_id=work_id)
        source = self.source(work_id)
        self.verify_source(work_id, source)
        result = classify(self.agent, work['subject'], source['mail_body'])
        self.rpc.call('finish_classification', p_work_id=work_id, p_classification_token=token,
                      p_status=result.status, p_reason=result.reason)
        return {'work_id': work_id, 'queue_status': result.status, 'classification_reason': result.reason}

    def skip(self, work_id):
        work = self.work(work_id)
        if work['queue_status'] not in ('PENDING', 'TRUE', 'FALSE'):
            raise ServiceError('INVALID_WORK_STATE', '분석 전 업무만 실행 안 함으로 표시할 수 있습니다.', 409)
        self.rpc.call('exclude_work_item', p_work_id=work_id)
        return {'work_id': work_id, 'queue_status': 'FALSE'}

    def notes(self, work_id, notes):
        self.rpc.call('update_work_item_notes', p_work_id=work_id, p_notes=notes)
        return {'work_id': work_id, 'notes': self.work(work_id)['notes']}

    def verify_source(self, work_id, source):
        try:
            body = parse_mail(self.raw(work_id, source))['body']
            if body != source['mail_body']:
                raise ValueError('Stored mail text differs from immutable original')
        except (KeyError, ValueError):
            raise ServiceError('SOURCE_MISMATCH', '저장된 본문과 원문 메일이 일치하지 않습니다.', 502) from None

    def reference_files(self, work_id):
        item_id = self.work(work_id).get('item_id')
        if item_id is None:
            return {'work_id': work_id, 'files': []}
        refs = set()
        contracts = self.references.read('contracts', item_id=item_id)
        quotes = self.references.read('price_references', item_id=item_id)
        for row in contracts + quotes:
            refs.add(row['source_ref'])
        for contract in contracts:
            refs.update(row['source_ref'] for row in self.references.read('purchase_plans', contract_id=contract['contract_id']))
        for contract_no in {row['contract_no'] for row in contracts}:
            refs.update(row['source_ref'] for row in self.references.read('cost_terms', contract_no=contract_no))
        for quote in quotes:
            refs.update(row['source_ref'] for row in self.references.read('cost_terms', reference_id=quote['reference_id']))
        return {'work_id': work_id, 'files': [
            {'file_id': reference_id(ref), 'source_ref': ref, 'filename': reference_path(ref).rsplit('/', 1)[-1]}
            for ref in sorted(refs)]}

    def reference_file(self, work_id, file_id):
        # Rebuild the allow-list from DB references for this work's item. No arbitrary URL/path input.
        matches = [row for row in self.reference_files(work_id)['files'] if row['file_id'] == file_id]
        if len(matches) != 1:
            raise ServiceError('SOURCE_FILE_NOT_FOUND', '이 업무에 연결된 기준자료 원문을 찾을 수 없습니다.', 404)
        row = matches[0]
        return {'original_name': row['filename'], 'content_type': mimetypes.guess_type(row['filename'])[0] or 'application/octet-stream'}, download_reference(self.client, row['source_ref'])

    def analyze(self, work_id):
        self.settings.require_llm()
        if self.work(work_id)['queue_status'] == 'RUNNING':
            raise ServiceError('WORK_RUNNING', '이미 분석 중인 업무입니다.', 409)
        if not self.analysis_lock.acquire(blocking=False):
            raise ServiceError('ANALYSIS_BUSY', '다른 업무의 분석이 진행 중입니다.', 409)
        found_item, status = None, 200
        try:
            try:
                run = self.rpc.call('start_analysis', p_work_id=work_id)
                run_id = run['run_id']
            except Exception:
                # Never retry a start after an uncertain response; the DB lease will recover it.
                raise ServiceError('QUEUE_UPDATE_FAILED', '분석 시작 상태를 확인할 수 없습니다. 큐를 다시 조회해 주세요.', 503) from None
            with LeaseHeartbeat(lambda: self.rpc.call('heartbeat_analysis', p_work_id=work_id, p_run_id=run_id)) as heartbeat:
                def on_item(item):
                    nonlocal found_item
                    heartbeat.check()
                    self.rpc.call('set_analysis_item', p_work_id=work_id, p_run_id=run_id, p_item_id=item)
                    found_item = item
                try:
                    source = self.source(work_id)
                    self.verify_source(work_id, source)
                    result = self.pipeline.run(work_id, source, on_item, heartbeat)
                except ReviewRequired as error:
                    result = {'outcome': 'REVIEW_REQUIRED', 'cards': [], 'issues': [error.issue], 'result_persisted': False, 'item_id': found_item}
                except ServiceError as error:
                    status = error.status if error.status in (502, 504) else 502
                    result = {'outcome': 'FAILED', 'cards': [], 'issues': [], 'result_persisted': False,
                              'error': {'code': error.code, 'message': error.message, 'details': {}}, 'item_id': found_item}
                except Exception:
                    status = 502
                    result = {'outcome': 'FAILED', 'cards': [], 'issues': [], 'result_persisted': False,
                              'error': {'code': 'ANALYSIS_FAILED', 'message': '분석 처리에 실패했습니다.', 'details': {}}, 'item_id': found_item}
                try:
                    heartbeat.check()
                    self.rpc.call('complete_analysis', p_work_id=work_id, p_run_id=run_id)
                except Exception:
                    raise ServiceError('QUEUE_UPDATE_FAILED', '분석 종료 상태를 기록하지 못했습니다.', 503) from None
            logger.info('analysis work_id=%s outcome=%s', work_id, result['outcome'])
            return status, {'work_id': work_id, 'queue_status': 'COMPLETE', **result}
        finally:
            self.analysis_lock.release()
