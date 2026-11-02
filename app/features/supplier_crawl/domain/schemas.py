"""잠정 웹 후보(06 CRAWLED_SUPPLIERS / 07 crawl_supplier_candidates, Should) 시험 수집 형식.

웹·공공데이터에서 확인한 값은 출처·조회 시각과 함께 잠정 근거로만 보관한다.
현재 공급 가능 여부·가격·인증 적합성은 RFQ 회신과 체크리스트로 확인하며,
기존 거래처로 확정하기 전 verified_supplier_id는 항상 null이다.
업체 단위 정보(업체명·업체 연락처·소재지·취급 품목·인증)만 수집하고 대표자 등 개인 이름은 담지 않는다.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal['PUBLIC_DATA', 'HACCP', 'HOMEPAGE']
ContactType = Literal['PHONE', 'EMAIL', 'HOMEPAGE']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SourceRef(StrictModel):
    """값 하나의 출처. source_url에는 인증키가 남지 않도록 마스킹한 주소만 담는다."""
    source_type: SourceType
    source_url: str
    fetched_at: datetime
    record_ref: str | None = None  # 인허가번호·품목제조보고번호·페이지 경로 등 원문 대조용 키
    snippet: str | None = Field(default=None, max_length=300)


class Contact(StrictModel):
    contact_type: ContactType
    value: str
    evidence: SourceRef


class HandledItem(StrictModel):
    item_name: str
    item_type: str | None = None
    evidence: SourceRef


class Certification(StrictModel):
    """웹·공공데이터의 인증 표시. 필수 인증 충족 판단은 체크리스트 회신으로 한다."""
    certification_type: Literal['HACCP']
    certification_no: str | None = None
    item_type: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    evidence: SourceRef


class CrawledSupplierCandidate(StrictModel):
    candidate_key: str
    name: str
    license_no: str | None = None
    location: str | None = None
    contacts: list[Contact] = Field(default_factory=list)
    handled_items: list[HandledItem] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    source_types: list[SourceType] = Field(default_factory=list)
    checklist_status: Literal['UNCONFIRMED'] = 'UNCONFIRMED'
    verified_supplier_id: None = None


class SourceIssue(StrictModel):
    source_type: SourceType
    code: str
    message: str
    target: str | None = None


class SourceStats(StrictModel):
    source_type: SourceType
    enabled: bool
    skipped_reason: str | None = None
    requests: int = 0
    failed_requests: int = 0
    records: int = 0
    candidates: int = 0
    elapsed_seconds: float = 0.0


class TrialReport(StrictModel):
    keyword: str
    started_at: datetime
    finished_at: datetime
    sources: list[SourceStats]
    candidates: list[CrawledSupplierCandidate]
    fill_rates: dict[str, str]
    issues: list[SourceIssue]
