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


def normalized_recipient(raw) -> str | None:
    """받는 주소의 비교용 값(mailbox_address와 같은 정규화, 앞뒤 공백은 뗀다). 형식이 틀리면 None."""
    try:
        return mailbox_address(raw.strip() if isinstance(raw, str) else raw)
    except ValueError:
        return None


def check_mail_content(subject, body) -> None:
    """발송할 수 있는 제목·본문인지. 제목은 한 줄이고 비어 있지 않아야 하며, 본문은 비어 있지 않아야 한다.
    발송 단계(from_claim)와 초안 수정 저장(API-27)이 같은 규칙을 쓴다."""
    if (not isinstance(subject, str) or not subject.strip() or any(c in subject for c in '\r\n\x00')
            or not isinstance(body, str) or not body.strip() or '\x00' in body):
        raise ValueError('Invalid send content')


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
        check_mail_content(subject, body)
        version = (result.get('settings') or {}).get('settings_version')
        if version is not None and not isinstance(version, str):
            raise ValueError('Invalid settings snapshot')
        return cls(str(UUID(str(draft_id))), recipient, subject, body, version)

    @property
    def rfc_message_id(self):
        return '<' + self.draft_id + '@gyeonjwo.local>'
