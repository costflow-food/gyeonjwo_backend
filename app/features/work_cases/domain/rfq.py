"""Server-owned RFQ drafts and authorization, never LLM send permissions."""
from dataclasses import dataclass
from email.utils import parseaddr
from uuid import UUID


def mailbox_address(raw):
    if not isinstance(raw, str) or any(c in raw for c in '\r\n\x00'):
        raise ValueError('Invalid recipient')
    display, address = parseaddr(raw)
    if display or address != raw or address.count('@') != 1 or any(c.isspace() for c in address):
        raise ValueError('Invalid recipient')
    return address.lower()


@dataclass(frozen=True, repr=False)
class AuthorizedMail:
    draft_id: str
    recipient: str
    subject: str
    body_text: str
    settings_version: str | None = None

    @classmethod
    def from_claim(cls, draft_id, result):
        if (not isinstance(result, dict) or result.get('authorized') is not True
                or str(UUID(result['draft_id'])) != str(UUID(str(draft_id)))):
            raise ValueError('Invalid send claim')
        recipient = mailbox_address(result['recipient'])
        subject, body = result['subject'], result['body_text']
        if (not isinstance(subject, str) or not subject.strip() or any(c in subject for c in '\r\n\x00')
                or not isinstance(body, str) or not body.strip() or '\x00' in body):
            raise ValueError('Invalid send content')
        version = (result.get('settings') or {}).get('settings_version')
        if version is not None and not isinstance(version, str):
            raise ValueError('Invalid settings snapshot')
        return cls(str(UUID(str(draft_id))), recipient, subject, body, version)

    @property
    def rfc_message_id(self):
        return '<' + self.draft_id + '@gyeonjwo.local>'
