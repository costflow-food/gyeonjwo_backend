"""API-21..24 DTOs matched to frontend dev ae66460 (SCR-008/009)."""
import hashlib
import json
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.shared.domain.schemas import StrictModel

Purpose = Literal['RFQ', 'CLARIFICATION', 'COUNTER_OFFER']
PURPOSES = ('RFQ', 'CLARIFICATION', 'COUNTER_OFFER')
MAX_VERSION = 2**52 - 1


def snapshot_version(snapshot):
    """Safe JSON integer conflict token; no fabricated DB revision column.

    Zero means no stored settings. Mutations ALSO compare the complete snapshot
    under the DB lock, so the numeric token is not an authorization credential.
    """
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], 'big') % MAX_VERSION + 1


MAIL_SNAPSHOT_FIELDS = ('id', 'work_case_id', 'mail_classification', 'processing_status', 'link_status',
                        'classification_reason', 'normalized_jsonb')


def mail_snapshot(row):
    return {name: row.get(name) for name in MAIL_SNAPSHOT_FIELDS}


class IntakeCandidate(StrictModel):
    case_id: UUID
    title: str
    status: Literal['Waiting', 'Confirm', 'Pending', 'Running', 'Complete']
    item_name: str | None = None
    conditions: str | None = None
    difference: str | None = None
    reply_evidence: str | None = None


class IntakeItem(StrictModel):
    mail_id: UUID
    subject: str
    sender: str
    received_at: str
    body_text: str
    mail_classification: Literal['UNCERTAIN', 'AMBIGUOUS']
    reason_detail: str
    attachments: list[str]
    candidates: list[IntakeCandidate]
    revision: int = Field(strict=True, ge=1, le=MAX_VERSION)


class IntakePage(StrictModel):
    items: list[IntakeItem]
    next_cursor: str | None
    total_pending: int = Field(strict=True, ge=0)


class ResolveRequest(StrictModel):
    resolution: Literal['NON_BUSINESS', 'CREATE_CASE', 'LINK_CASE']
    target_case_id: UUID | None = None
    expected_revision: int | None = Field(default=None, strict=True, ge=1, le=MAX_VERSION)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def target(self):
        if (self.resolution == 'LINK_CASE') != (self.target_case_id is not None):
            raise ValueError('target_case_id is required only for LINK_CASE')
        return self


class ResolvedMail(StrictModel):
    mail_id: UUID
    resolution: Literal['NON_BUSINESS', 'CREATE_CASE', 'LINK_CASE']
    case_id: UUID | None
    case_status: Literal['Waiting', 'Confirm', 'Pending', 'Running', 'Complete'] | None
    total_pending: int = Field(strict=True, ge=0)

    @model_validator(mode='after')
    def linked_case(self):
        if self.resolution == 'NON_BUSINESS':
            if self.case_id is not None or self.case_status is not None:
                raise ValueError('Non-business cannot create a case')
        elif self.case_id is None or self.case_status is None:
            raise ValueError('Missing resolved case')
        if self.resolution == 'CREATE_CASE' and self.case_status != 'Waiting':
            raise ValueError('New case must wait for explicit review')
        return self


class ApprovalUpdate(StrictModel):
    RFQ: bool | None = Field(default=None, strict=True)
    CLARIFICATION: bool | None = Field(default=None, strict=True)
    COUNTER_OFFER: bool | None = Field(default=None, strict=True)


class MailStyle(StrictModel):
    tone: Literal['FORMAL', 'FRIENDLY'] = 'FORMAL'
    signature: str = Field(default='', max_length=4000)


class SettingsUpdate(StrictModel):
    mailbox_ref: str = Field(min_length=1, max_length=300)
    case_id: UUID | None = None
    expected_settings_version: int = Field(strict=True, ge=0, le=MAX_VERSION)
    approval_required_by_purpose: ApprovalUpdate
    allowed_supplier_ids: list[UUID] = Field(max_length=100)
    style: MailStyle
    apply_to: Literal['FUTURE_DRAFTS', 'SPECIFIED_DRAFTS']
    draft_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def scope(self):
        if len(set(self.allowed_supplier_ids)) != len(self.allowed_supplier_ids) or len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError('Duplicate targets')
        return self


class EffectiveApproval(StrictModel):
    approval_required: bool | None = Field(strict=True)
    effective_approval_required: bool | None = Field(strict=True)
    source: Literal['CASE', 'MAILBOX'] | None
    source_version: int | None = Field(default=None, strict=True, ge=1)
    source_settings_id: UUID | None = None

    @model_validator(mode='after')
    def source_matches_value(self):
        if self.effective_approval_required is None:
            if self.source is not None or self.source_version is not None or self.source_settings_id is not None:
                raise ValueError('Unset approval cannot have a source')
        elif self.source is None or self.source_version is None or self.source_settings_id is None:
            raise ValueError('Effective approval requires its immutable source')
        return self


class AllowedSupplier(StrictModel):
    supplier_id: UUID
    name: str
    email: str | None
    allowed: bool


class PendingDraft(StrictModel):
    draft_id: UUID
    purpose: Purpose
    supplier_name: str
    recipient: str
    status: Literal['DRAFT', 'SENDING', 'SENT', 'FAILED', 'UNKNOWN']


class SendSettings(StrictModel):
    mailbox_ref: str
    case_id: UUID | None
    configured: bool = Field(strict=True)
    settings_version: int = Field(strict=True, ge=0, le=MAX_VERSION)
    missing_items: list[str]
    effective_settings: dict[Purpose, EffectiveApproval]
    allowed_suppliers: list[AllowedSupplier]
    style: MailStyle
    pending_drafts: list[PendingDraft]

    @model_validator(mode='after')
    def all_purposes(self):
        if set(self.effective_settings) != set(PURPOSES):
            raise ValueError('All purposes are required')
        if self.configured:
            if self.missing_items or any(value.effective_approval_required is None for value in self.effective_settings.values()):
                raise ValueError('Incomplete settings cannot be configured')
            if any(value.effective_approval_required is False for value in self.effective_settings.values()) and not any(value.allowed for value in self.allowed_suppliers):
                raise ValueError('Automatic sending requires allowed suppliers')
        return self
