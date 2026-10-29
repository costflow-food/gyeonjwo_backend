"""Owner-scoped attachment metadata lookup; file bodies remain in Gmail."""
from app.core.errors import ServiceError


class AttachmentRepository:
    def __init__(self, client):
        self.client = client

    def message(self, owner_id, mail_id):
        try:
            rows = (self.client.table('mail_messages')
                    .select('id,mailbox_ref,provider,provider_message_id,attachments_jsonb')
                    .eq('owner_user_id', owner_id).eq('id', str(mail_id)).limit(1).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 저장소를 확인할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 또는 첨부를 찾을 수 없습니다.', 404)
        return rows[0]
