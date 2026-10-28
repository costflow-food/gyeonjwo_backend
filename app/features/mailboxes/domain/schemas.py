from dataclasses import dataclass
from datetime import timezone
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, field_serializer

from app.shared.domain.schemas import StrictModel

READ_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'


@dataclass(frozen=True)
class GooglePrincipal:
    user_id: str
    google_sub: str
    email: str


class GoogleConnectRequest(StrictModel):
    provider_access_token: SecretStr = Field(min_length=1, max_length=16384)
    provider_refresh_token: SecretStr | None = Field(min_length=1, max_length=16384)


class Capabilities(StrictModel):
    read: bool
    send: bool


class WatchState(StrictModel):
    started: bool
    expiration: AwareDatetime | None

    @field_serializer('expiration', when_used='json')
    def utc_expiration(self, value):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if value else None


class GoogleConnection(StrictModel):
    mailbox_ref: UUID | None
    email: str | None
    status: Literal['CONNECTED', 'PARTIAL', 'NEEDS_RECONNECT', 'FAILED', 'NOT_CONNECTED']
    capabilities: Capabilities
    watch: WatchState
