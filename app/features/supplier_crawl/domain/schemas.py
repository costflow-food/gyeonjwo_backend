"""KAN-109 registration-only extensions of the PR #33 collection models."""
from typing import Literal
from pydantic import Field
from app.agents.tools import (
    CrawlCandidate, CrawlCertification, CrawlHandledItem, CrawlReport,
    CrawlSourceRef as SourceRef, CrawlContact as Contact,
    CrawlSourceIssue as SourceIssue, CrawlSourceStats as SourceStats,
)


class HandledItem(CrawlHandledItem):
    report_no: str | None = None  # Raw PRDLST_REPORT_NO; never derived from item name.


class Certification(CrawlCertification):
    license_no: str | None = None  # Observed HACCP licenseno, never copied from a candidate.


class CrawledSupplierCandidate(CrawlCandidate):
    evidence: dict[Literal['name', 'license_no', 'location'], SourceRef] = Field(default_factory=dict)
    handled_items: list[HandledItem] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)


class TrialReport(CrawlReport):
    candidates: list[CrawledSupplierCandidate]
