"""Payload projection for applied KAN-109 SQL, not a supplier/item normalizer.

No IDs, report numbers, item types or standardized keys are synthesized here.
Optional SQL fields are omitted, never represented by JSON null or empty certs.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from app.features.supplier_crawl.domain.schemas import CrawledSupplierCandidate, SourceRef
from app.agents.tools import is_public_web_url, mask_url


class RegistrationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # Never include source values or provider messages.


@dataclass(frozen=True)
class RegistrationPayload:
    supplier: dict
    items: list[dict]


EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")


class PayloadBuilder:
    def __init__(self, secrets=(), clock=lambda: datetime.now(timezone.utc)):
        self.secrets, self.clock = tuple(s for s in secrets if s), clock

    def _clean(self, value):
        masked = mask_url(value, list(self.secrets))
        decoded = masked
        for _ in range(4):
            if any(secret in decoded for secret in self.secrets):
                raise RegistrationError('UNSAFE_SOURCE_VALUE')
            decoded = unquote(decoded)
        return masked

    def source(self, ref: SourceRef) -> dict:
        observed = ref.fetched_at
        if observed.utcoffset() is None or observed > self.clock() + timedelta(minutes=5):
            raise RegistrationError('INVALID_SOURCE_TIME')
        # Query and fragment are removed, rather than replaced by made-up values.
        try:
            parts = urlsplit(ref.source_url)
            if parts.username is not None or parts.password is not None:
                raise RegistrationError('INVALID_SOURCE_URL')
            path = parts.path
            if '/api/' in path:
                # Redact a known credential/masking marker only. Do not relabel
                # an arbitrary API resource as a credential to satisfy SQL.
                def redact(match):
                    token = match.group(1)
                    if token not in {'***', 'REDACTED'} and unquote(token) not in self.secrets:
                        raise RegistrationError('INVALID_SOURCE_URL')
                    return '/api/REDACTED'
                path = re.sub(r'/api/([^/]+)', redact, path)
            url = urlunsplit((parts.scheme, parts.netloc, path, '', ''))
            if not is_public_web_url(url) or re.search(r'\s', url):
                raise RegistrationError('INVALID_SOURCE_URL')
        except ValueError:
            raise RegistrationError('INVALID_SOURCE_URL') from None
        url = self._clean(url)
        if '?' in url or '#' in url or ('/api/' in url and '/api/REDACTED/' not in url):
            raise RegistrationError('INVALID_SOURCE_URL')
        result = {'source_type': ref.source_type, 'source_url': url,
                  'fetched_at': observed.isoformat()}
        for field in ('record_ref', 'snippet'):
            value = getattr(ref, field)
            if value is not None:
                result[field] = self._clean(value)
        return result

    def _field(self, candidate, field):
        ref = candidate.evidence.get(field)
        if ref is None:
            raise RegistrationError('MISSING_FIELD_EVIDENCE')
        return self.source(ref)

    @staticmethod
    def _date(value):
        if not isinstance(value, str):
            raise RegistrationError('INCOMPLETE_HACCP_EVIDENCE')
        if re.fullmatch(r'[0-9]{8}', value):
            value = value[:4] + '-' + value[4:6] + '-' + value[6:]
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise RegistrationError('INVALID_HACCP_PERIOD')
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise RegistrationError('INVALID_HACCP_PERIOD') from None

    def build(self, candidate: CrawledSupplierCandidate) -> RegistrationPayload:
        license_no = candidate.license_no
        if not license_no or not re.fullmatch(r'[0-9]{11}', license_no) or not candidate.name.strip():
            raise RegistrationError('INVALID_WEB_SUPPLIER')
        evidence = {field: self._field(candidate, field) for field in ('name', 'license_no')}
        if (evidence['license_no']['source_type'] != 'PUBLIC_DATA'
                or evidence['license_no'].get('record_ref') != license_no):
            raise RegistrationError('INVALID_WEB_PROVENANCE')
        supplier = {'name': candidate.name, 'license_no': license_no, 'evidence': evidence}
        if candidate.location:
            supplier['location'] = candidate.location
            evidence['location'] = self._field(candidate, 'location')
        for kind, field in (('EMAIL', 'contact_email'), ('PHONE', 'phone'), ('HOMEPAGE', 'homepage_url')):
            contacts = [c for c in candidate.contacts if c.contact_type == kind and c.value.strip()]
            values = {c.value.strip().lower() if kind == 'EMAIL' else c.value.strip() for c in contacts}
            if not values:
                continue
            # Never let an LLM select a new address or silently pick one of several emails.
            if kind == 'EMAIL' and len(values) > 1:
                raise RegistrationError('CONTACT_SELECTION_REQUIRED')
            contact = contacts[0]
            value = contact.value.strip().lower() if kind == 'EMAIL' else contact.value.strip()
            ref = self.source(contact.evidence)
            if kind in ('EMAIL', 'HOMEPAGE') and ref['source_type'] != 'HOMEPAGE':
                raise RegistrationError('INVALID_WEB_PROVENANCE')
            if kind == 'EMAIL' and (len(value) > 254 or not EMAIL.fullmatch(value)):
                raise RegistrationError('INVALID_CONTACT_EMAIL')
            if kind == 'HOMEPAGE':
                if value != ref['source_url']:
                    # A source URL may identify a contact page; homepage value is
                    # independently checked and never replaced by that page URL.
                    parts = urlsplit(value)
                    if (parts.query or parts.fragment or parts.username is not None
                            or parts.password is not None or not is_public_web_url(value)):
                        raise RegistrationError('INVALID_HOMEPAGE')
                value = self._clean(value)
            supplier[field], evidence[field] = value, ref

        items, reports = [], set()
        for observed in candidate.handled_items:
            if observed.evidence.source_type != 'PUBLIC_DATA':
                continue  # Homepage text is not a manufacturing report.
            report = observed.report_no
            if not report or not re.fullmatch(r'[0-9]{1,40}', report):
                raise RegistrationError('REPORT_MAPPING_REQUIRED')
            if report in reports:
                raise RegistrationError('DUPLICATE_WEB_REPORT')
            reports.add(report)
            ref = self.source(observed.evidence)
            if ref.get('record_ref') != report:
                raise RegistrationError('INVALID_WEB_PROVENANCE')
            if not observed.item_name.strip() or not observed.item_type or not observed.item_type.strip():
                raise RegistrationError('INCOMPLETE_WEB_ITEM')
            # v3 resolves/preserves the actual key in the DB, including C items.
            # Omit the field entirely: neither null nor a local/generated key.
            item = {'report_no': report, 'item_name': observed.item_name, 'item_type': observed.item_type,
                    'evidence': ref}
            certificates = []
            for cert in candidate.certifications:
                if cert.item_type != observed.item_type:
                    continue  # No unproven cross-product association.
                if (cert.license_no != license_no or not cert.certification_no
                        or cert.evidence.source_type != 'HACCP'):
                    raise RegistrationError('INCOMPLETE_HACCP_EVIDENCE')
                start, end = self._date(cert.valid_from), self._date(cert.valid_until)
                if start > end:
                    raise RegistrationError('INVALID_HACCP_PERIOD')
                certificates.append({'type': 'HACCP', 'certification_no': cert.certification_no,
                    'license_no': cert.license_no, 'item_type': cert.item_type, 'valid_from': start,
                    'valid_until': end, 'evidence': self.source(cert.evidence)})
            if certificates:
                item['certifications'] = certificates  # Objects, never ['HACCP'] or [].
            items.append(item)
        if not items:
            raise RegistrationError('REPORT_MAPPING_REQUIRED')
        if len(items) > 100:
            raise RegistrationError('WEB_INPUT_LIMIT')
        # Guard non-evidence values as well; SQL errors/logs must never receive a secret.
        import json
        serialized = json.dumps([supplier, items], ensure_ascii=False)
        decoded = serialized
        for _ in range(5):
            if any(secret in decoded for secret in self.secrets):
                raise RegistrationError('UNSAFE_SOURCE_VALUE')
            decoded = unquote(decoded)
        if len(json.dumps(supplier, ensure_ascii=False).encode()) + len(json.dumps(items, ensure_ascii=False).encode()) > 1_048_576:
            raise RegistrationError('WEB_INPUT_LIMIT')
        return RegistrationPayload(supplier, items)
