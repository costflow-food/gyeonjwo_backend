"""KAN-109 C contacts are not suppliers or RFQ targets."""
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.shared.domain.schemas import StrictModel


class ContactCandidate(StrictModel):
    candidate_id: UUID
    name: str = Field(strict=True, min_length=1)
    phone: str | None = Field(strict=True)
    items: list[dict]
    evidence: dict
    contact_email: None
    supplier_id: None
    selectable: Literal[False]
    rfq_eligible: Literal[False]
    reason_code: Literal['CONTACT_REQUIRED']
    reason: Literal['전화로 업무 이메일 확인 필요', '연락처 미확보']

    @model_validator(mode='before')
    @classmethod
    def strict_exclusion(cls, value):
        if not isinstance(value, dict) or any(value.get(key) is not False for key in ('selectable', 'rfq_eligible')):
            raise ValueError('Invalid contact candidate exclusion')
        return value

    @model_validator(mode='after')
    def contact_reason(self):
        expected = '전화로 업무 이메일 확인 필요' if self.phone and self.phone.strip() else '연락처 미확보'
        if self.reason != expected:
            raise ValueError('Invalid contact reason')
        return self


class ContactCandidatesResponse(StrictModel):
    case_id: UUID
    revision: int = Field(strict=True, ge=1)
    items: list[ContactCandidate]

    @model_validator(mode='after')
    def unique_contacts(self):
        if len({item.candidate_id for item in self.items}) != len(self.items):
            raise ValueError('Duplicate contact candidates')
        return self
