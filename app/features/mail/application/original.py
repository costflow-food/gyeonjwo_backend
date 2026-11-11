from app.core.errors import ServiceError
from app.features.mail.domain.attachment_ids import attachment_id, part_reference
from app.features.mail.domain.original import OriginalAttachment, OriginalMail


class OriginalMailService:
    def __init__(self, repository):
        self.repo = repository

    def get(self, owner_id, mail_id):
        row = self.repo.get(owner_id, mail_id)
        try:
            metadata = row.get('attachments_jsonb')
            if metadata is None:
                metadata = []
            if not isinstance(metadata, list) or any(not isinstance(a, dict) for a in metadata):
                raise ValueError('Invalid attachment metadata')
            downloadable_provider = str(row.get('provider') or '').upper() == 'GMAIL'
            attachments = [OriginalAttachment(
                attachment_id=attachment_id(row['id'], a, index), filename=a['filename'],
                mime_type=a['mime_type'], size_bytes=a['size_bytes'],
                can_download=downloadable_provider and isinstance(part_reference(a), str))
                for index, a in enumerate(metadata)]
            if len({a.attachment_id for a in attachments}) != len(attachments):
                raise ValueError('Ambiguous attachment references')
            return OriginalMail(mail_id=row['id'], subject=row['subject'], sender=row['sender'],
                recipients=row['recipients_jsonb'], received_at=row['sent_or_received_at'],
                body_text=row['body_text'] if row['body_text'] is not None else '',
                attachments=attachments, mail_classification=row['mail_classification'],
                source_type=row['provider'])
        except (ValueError, KeyError, TypeError, AttributeError):
            # Provider/DB exception data can include the original body. Never echo it.
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 메일 원문 형식을 확인할 수 없습니다.', 503) from None
