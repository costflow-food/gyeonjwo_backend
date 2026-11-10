"""API-11/12 public DTOs matched to frontend CandidateSet/Candidate mapping."""
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.shared.domain.schemas import StrictModel

POLICY_MODE = 'PRESERVE_MAILBOX_LIMIT'
STAGED_PRODUCER = 'graph-staged-rfq-v1'


class CandidateCheck(StrictModel):
    label: str = Field(min_length=1, max_length=300)
    status: Literal['MET', 'UNMET', 'UNKNOWN']
    required: bool = Field(strict=True)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class CandidateCaution(StrictModel):
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=100)


class CandidateItem(StrictModel):
    supplier_id: UUID
    name: str
    contact_email: str | None
    handling_evidence: str
    reason: str = Field(min_length=1, max_length=300)
    checks: list[CandidateCheck] = Field(max_length=100)
    preference_score: int | None = Field(default=None, strict=True, ge=0)
    preference_score_exact: str | None = None
    is_web_candidate: bool = Field(strict=True)
    evidence_refs: list[str] = Field(max_length=100)
    unknown_fields: list[str] = Field(default_factory=list, max_length=100)
    caution: CandidateCaution | None = None

    @model_validator(mode='after')
    def score(self):
        if self.contact_email is not None:
            from app.features.work_cases.domain.rfq import mailbox_address
            self.contact_email = mailbox_address(self.contact_email)
        if self.preference_score_exact is not None:
            try:
                score = Decimal(self.preference_score_exact)
            except InvalidOperation:
                raise ValueError('Invalid preference score') from None
            if not score.is_finite() or score < 0:
                raise ValueError('Invalid preference score')
            if self.preference_score is not None and Decimal(self.preference_score) != score:
                raise ValueError('Rounded preference score')
        return self

    @property
    def selectable(self):
        return bool(self.contact_email) and not any(c.required and c.status == 'UNMET' for c in self.checks)


class CandidateSnapshot(StrictModel):
    contract_version: Literal[1] = 1
    case_id: UUID
    candidate_set_id: UUID
    revision: int = Field(strict=True, ge=1)
    review_batch_id: UUID
    items: list[CandidateItem] = Field(max_length=100)
    unknown_fields: list[str] = Field(default_factory=list, max_length=100)
    web_observations: list[dict] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def unique_suppliers(self):
        if len({item.supplier_id for item in self.items}) != len(self.items):
            raise ValueError('Duplicate candidates')
        return self


class CandidatesResponse(CandidateSnapshot):
    selected_supplier_ids: list[UUID] = Field(default_factory=list, max_length=100)
    is_stale: bool
    selection_revision: int = Field(default=0, strict=True, ge=0)
    selection_policy: Literal['PRESERVE_MAILBOX_LIMIT'] = POLICY_MODE
    allowed_actions: list[str] = Field(default_factory=list)
    disabled_actions: list[dict] = Field(default_factory=list)


class CandidateSelectionRequest(StrictModel):
    expected_revision: int = Field(strict=True, ge=1)
    candidate_set_id: UUID
    selected_supplier_ids: list[UUID] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_selection(self):
        if len(set(self.selected_supplier_ids)) != len(self.selected_supplier_ids):
            raise ValueError('Duplicate selected supplier')
        return self


class SelectedDraft(StrictModel):
    draft_id: UUID
    supplier_id: UUID
    dispatch_state: Literal['WAITING_APPROVAL', 'AUTO_QUEUED', 'CAUTION_REQUIRED', 'BLOCKED_POLICY']


class CandidateSelectionResponse(StrictModel):
    case_id: UUID
    candidate_set_id: UUID
    revision: int = Field(strict=True, ge=1)
    selection_revision: int = Field(strict=True, ge=1)
    selected_supplier_ids: list[UUID] = Field(min_length=1, max_length=100)
    drafts: list[SelectedDraft] = Field(min_length=1, max_length=100)
    selection_policy: Literal['PRESERVE_MAILBOX_LIMIT'] = POLICY_MODE

    @model_validator(mode='after')
    def exact_selected_drafts(self):
        selected = set(self.selected_supplier_ids)
        if (len(selected) != len(self.selected_supplier_ids) or len(self.drafts) != len(selected)
                or {draft.supplier_id for draft in self.drafts} != selected
                or len({draft.draft_id for draft in self.drafts}) != len(selected)):
            raise ValueError('Selection/draft scope mismatch')
        return self


def selection_staged(draft):
    style = draft.get('style_snapshot_jsonb')
    if style is None:
        style = {}
    if not isinstance(style, dict):
        return True  # Corrupt selection metadata must never become public/sendable.
    if style.get('producer') in (STAGED_PRODUCER, 'graph-staged-review-v1'):
        return True
    if style.get('candidate_selection_required') is True:
        selection = style.get('candidate_selection')
        return not isinstance(selection, dict) or selection.get('state') != 'SELECTED'
    return False


def selected_for_dispatch(draft, case):
    style = draft.get('style_snapshot_jsonb')
    if style is None:
        style = {}
    if selection_staged(draft):
        return False
    if style.get('candidate_selection_required') is not True:
        return True  # Existing legacy flow is unchanged.
    if case.get('archived_at') is not None or case.get('decision') in {'ACCEPT_INCREASE', 'CONFIRM_REVIEW'}:
        return False
    selection = style.get('candidate_selection') or {}
    try:
        UUID(selection['candidate_set_id'])
        return (selection['state'] == 'SELECTED' and selection['case_id'] == draft['work_case_id'] == case['id']
                and selection['supplier_id'] == draft['supplier_id']
                and type(selection['revision']) is int and selection['revision'] > 0
                and selection['revision'] == draft['input_revision'] == case['input_revision']
                and type(selection['selection_revision']) is int and selection['selection_revision'] > 0)
    except (KeyError, ValueError, TypeError):
        return False
