from datetime import timezone
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_serializer
from app.shared.domain.schemas import StrictModel


class OriginalAttachment(StrictModel):
    attachment_id: UUID
    filename: str
    mime_type: str
    size_bytes: int = Field(strict=True, ge=0)


class OriginalMail(StrictModel):
    mail_id: UUID
    subject: str
    sender: str
    recipients: list[str]
    received_at: AwareDatetime
    body_text: str
    attachments: list[OriginalAttachment]
    mail_classification: Literal['BUSINESS', 'NON_BUSINESS', 'UNCERTAIN'] | None
    source_type: str

    @field_serializer('received_at', when_used='json')
    def utc_received(self, value):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
