"""KAN-109 provenance extension; collection/security implementations live in agents.tools.

This adapter adds only observed registration evidence. It does not register a tool,
write to the DB, synthesize source values or change the PR #33 CLI output contract.
"""
from app.agents.tools import SupplierCrawlTrial as CollectionTrial, TrialOptions, _Candidates, CrawlSourceRef
from app.features.supplier_crawl.domain.schemas import (
    Certification, CrawledSupplierCandidate, HandledItem, TrialReport,
)


def _observe_field(candidate, field, value, evidence):
    if value and getattr(candidate, field) == value and field not in candidate.evidence:
        candidate.evidence[field] = evidence


class _RegistrationCandidates(_Candidates):
    candidate_type = CrawledSupplierCandidate


class SupplierCrawlTrial(CollectionTrial):
    homepage_item_type = HandledItem
    candidate_collection = _RegistrationCandidates
    report_type = TrialReport

    def _item_exists(self, candidate, row):
        return any(i.report_no == row['report_no'] and i.item_name == row['item_name']
                   for i in candidate.handled_items)

    def _make_item(self, row, evidence):
        return HandledItem(item_name=row['item_name'], report_no=row['report_no'],
                           item_type=row['item_type'], evidence=evidence)

    def _make_certification(self, row, evidence):
        return Certification(certification_type='HACCP', certification_no=row['certification_no'],
                             license_no=row['license_no'], item_type=row['item_type'],
                             valid_from=row['valid_from'], valid_until=row['valid_until'], evidence=evidence)

    def _observe(self, kind, candidate, row, url, fetched_at):
        if kind == 'PUBLIC_ITEM':
            _observe_field(candidate, 'name', row['name'], CrawlSourceRef(
                source_type='PUBLIC_DATA', source_url=url, fetched_at=fetched_at,
                record_ref=row['report_no'], snippet=self.fetcher.preview(row['name'], 300)))
            if row['license_no']:
                _observe_field(candidate, 'license_no', row['license_no'], CrawlSourceRef(
                    source_type='PUBLIC_DATA', source_url=url, fetched_at=fetched_at,
                    record_ref=row['license_no'], snippet=row['license_no']))
        elif kind == 'PUBLIC_BUSINESS':
            for field, value in (('name', row['name']), ('license_no', row['license_no']), ('location', row['address'])):
                _observe_field(candidate, field, value, CrawlSourceRef(
                    source_type='PUBLIC_DATA', source_url=url, fetched_at=fetched_at,
                    record_ref=row['license_no'], snippet=self.fetcher.preview(value, 300)))
        elif kind == 'HACCP':
            location = ' '.join(v for v in (row['sido'], row['sgg']) if v) or None
            for field, value in (('name', row['name']), ('license_no', row['license_no']), ('location', location)):
                _observe_field(candidate, field, value, CrawlSourceRef(
                    source_type='HACCP', source_url=url, fetched_at=fetched_at,
                    record_ref=row['certification_no'], snippet=self.fetcher.preview(value, 300)))
