"""Draft HTTP snapshots use byte-length framing shared with the SQL wrapper."""
import hashlib
from uuid import UUID
from pydantic import Field
from app.shared.domain.schemas import StrictModel

SNAPSHOT_FIELDS = ('id', 'work_case_id', 'supplier_id', 'intent_id', 'purpose',
                   'recipient', 'subject', 'body_text', 'input_revision')


def requires_manual_approval(draft):
    style = draft.get('style_snapshot_jsonb') or {}
    return bool(style.get('candidate_caution')) or style.get('auto_policy_eligible') is not True


def snapshot_hash(draft):
    framed = bytearray()
    for field in SNAPSHOT_FIELDS:
        raw = str(draft[field]).encode('utf-8')
        framed += str(len(raw)).encode('ascii') + b':' + raw
    return hashlib.sha256(framed).hexdigest()


class ApproveDraftRequest(StrictModel):
    expected_revision: int = Field(strict=True, ge=1)
    snapshot_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_settings_version: str = Field(min_length=9, max_length=73,
        description='Opaque settings_version returned by draft GET; do not use a local counter.')


class DraftAccepted(StrictModel):
    draft_id: UUID
    accepted: bool = Field(strict=True)
    draft_url: str
