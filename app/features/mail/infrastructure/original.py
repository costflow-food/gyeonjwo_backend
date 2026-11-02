"""API-25: SELECT only, with an explicit owner predicate despite service-role RLS bypass."""
from app.core.errors import ServiceError

ORIGINAL_COLUMNS = ('id,subject,sender,recipients_jsonb,sent_or_received_at,body_text,'
                    'attachments_jsonb,mail_classification,provider')


class OriginalMailRepository:
    def __init__(self, client):
        self.client = client

    def get(self, owner_id, mail_id):
        try:
            rows = (self.client.table('mail_messages').select(ORIGINAL_COLUMNS)
                    .eq('owner_user_id', str(owner_id)).eq('id', str(mail_id)).limit(1).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 원문을 조회할 수 없습니다.', 503) from None
        if not isinstance(rows, list) or len(rows) > 1 or (rows and not isinstance(rows[0], dict)):
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 원문 조회 결과를 확인할 수 없습니다.', 503)
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일을 찾을 수 없습니다.', 404)
        return rows[0]
