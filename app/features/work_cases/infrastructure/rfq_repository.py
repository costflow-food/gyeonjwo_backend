"""SQL v3.6 RFQ boundaries. Never duplicate trigger-owned evidence inserts."""
from uuid import UUID
from app.core.errors import ServiceError


def require(value):
    if not value:
        raise ValueError('Invalid RFQ storage response')


class RfqRepository:
    def __init__(self, client):
        self.client = client

    def require_schema(self):
        if self.call('rfq_schema_health') != 'rfq-reply-facts-v1':
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ v3.6 DB 계약을 확인해야 합니다.', 503)

    def call(self, name, **params):
        try:
            return self.client.rpc(name, params).execute().data
        except Exception as error:
            code = getattr(error, 'code', None)
            if code == 'P0002':
                raise ServiceError('RESOURCE_NOT_FOUND', '업무 또는 발송 기록을 찾을 수 없습니다.', 404) from None
            if code == '40001':
                raise ServiceError('REVISION_CONFLICT', '업무 또는 작업 소유권이 변경됐습니다.', 409, retryable=False) from None
            if code == '55000':
                raise ServiceError('ACTION_NOT_ALLOWED', '현재 상태에서는 RFQ 작업을 진행할 수 없습니다.', 409, retryable=False) from None
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 저장소를 확인할 수 없습니다.', 503) from None

    def metadata_owners(self):
        """메일함을 연결한 사용자 ID 목록. 메타데이터 워커가 매 주기 사용자별로 작업을 가져갈 때 쓴다."""
        rows = self.client.table('mail_sync_states').select('owner_user_id').execute().data or []
        return sorted({str(row['owner_user_id']) for row in rows})

    def open_round(self, owner, case_id, revision, current_round, suppliers, key):
        """Internal candidate orchestrator entry, not a browser/LLM tool.

        The SQL owns revision, state, scope and durable idempotency checks.
        Returned intent IDs MUST be used by the existing draft creation path.
        """
        owner, case_id = str(UUID(str(owner))), str(UUID(str(case_id)))
        if type(revision) is not int or revision < 1:
            raise ValueError('Invalid revision')
        if (not isinstance(key, str) or not 1 <= len(key) <= 200 or key.strip() != key
                or any(ord(c) < 32 for c in key)):
            raise ValueError('Invalid idempotency key')
        if not isinstance(suppliers, list) or len(suppliers) > 100:
            raise ValueError('Invalid supplier scope')
        suppliers = list(dict.fromkeys(str(UUID(str(x))) for x in suppliers))
        result = self.call('open_rfq_round', p_owner=owner, p_case=case_id,
                           p_expected_revision=revision,
                           p_expected_current_round=str(UUID(str(current_round))) if current_round else None,
                           p_suppliers=suppliers, p_key=key)
        try:
            require(isinstance(result, dict) and str(UUID(result['case_id'])) == case_id)
            UUID(result['request_round_id'])
            require(type(result['round_no']) is int and result['round_no'] > 0)
            require(isinstance(result['targets'], list))
            seen, intents = set(), set()
            for target in result['targets']:
                supplier = str(UUID(target['supplier_id']))
                intent = str(UUID(target['intent_id']))
                UUID(target['target_id'])
                require(supplier in suppliers and supplier not in seen and intent not in intents)
                seen.add(supplier)
                intents.add(intent)
            require(seen == set(suppliers))
        except (KeyError, ValueError, TypeError, AttributeError, AssertionError):
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 라운드 접수 결과를 확인할 수 없습니다.', 503) from None
        return result

    def claim_metadata(self, owner, limit=20):
        result = self.call('claim_rfq_outbound_metadata', p_owner=owner, p_limit=limit)
        try:
            require(isinstance(result, list) and len(result) <= limit)
            seen = set()
            for job in result:
                sent = str(UUID(job['sent_message_id']))
                UUID(job['claim_token'])
                UUID(job['mailbox_ref'])
                require(sent not in seen)
                require(isinstance(job['provider_message_id'], str) and job['provider_message_id'])
                seen.add(sent)
        except (KeyError, ValueError, TypeError, AttributeError, AssertionError):
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 헤더 작업 접수를 확인할 수 없습니다.', 503) from None
        return result

    def complete_metadata(self, owner, job, message):
        result = self.call('complete_rfq_outbound_metadata', p_owner=owner,
                           p_sent=job['sent_message_id'], p_token=job['claim_token'], p_message=message)
        if (not isinstance(result, dict) or result.get('metadata_state') != 'READY'
                or result.get('sent_message_id') != job['sent_message_id']):
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 헤더 저장 결과를 확인할 수 없습니다.', 503)
        return result

    def fail_metadata(self, owner, job):
        return self.call('fail_rfq_outbound_metadata', p_owner=owner,
                         p_sent=job['sent_message_id'], p_token=job['claim_token'])

    def retry_metadata(self, owner, sent_id):
        """Explicit operator action only; never automatically revive UNAVAILABLE."""
        return self.call('retry_rfq_outbound_metadata', p_owner=str(UUID(str(owner))),
                         p_sent=str(UUID(str(sent_id))))
