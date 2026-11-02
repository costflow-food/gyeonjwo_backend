"""KAN-98 크롤링 시험: 세 출처에서 소량 수집 → 잠정 후보로 병합 → 출처별 결과·필드 채움률·오류 리포트.

후보 병합 기준: 인허가번호가 있으면 인허가번호, 없으면 정규화한 업체명.
각 값은 출처 근거를 함께 가지며, 출처가 달라도 값을 덮어쓰지 않고 나란히 둔다.
"""
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.features.supplier_crawl.domain.schemas import (
    Certification, Contact, CrawledSupplierCandidate, HandledItem, SourceIssue, SourceRef, SourceStats, TrialReport,
)
from app.features.supplier_crawl.domain.names import normalize_item_type, normalize_name, region_of
from app.features.supplier_crawl.infrastructure import public_data
from app.features.supplier_crawl.infrastructure.haccp import HaccpClient
from app.features.supplier_crawl.infrastructure.homepage import HomepageCrawler, SiteSeed
from app.features.supplier_crawl.infrastructure.http import FetchError, Fetcher, now
from app.features.supplier_crawl.infrastructure.public_data import PublicDataClient

@dataclass
class TrialOptions:
    keyword: str
    sources: tuple[str, ...] = ('public', 'haccp', 'homepage')
    item_limit: int = 30
    business_limit: int = 10
    haccp_pages: int = 3
    haccp_rows: int = 100  # 서비스 한도 100
    haccp_item_type: str | None = None
    site_limit: int = 5
    naver_query: str | None = None
    seeds: tuple[SiteSeed, ...] = ()
    item_types: tuple[str, ...] = ()        # 품목유형 필터 (예: 과·채가공품). 비우면 거르지 않음
    include_ended: bool = False             # 생산 종료 품목 포함 여부


class _Candidates:
    def __init__(self):
        self.by_key: dict[str, CrawledSupplierCandidate] = {}
        self.key_by_name: dict[str, str] = {}

    def get(self, name: str, license_no: str | None = None) -> CrawledSupplierCandidate:
        key = ('license:' + license_no) if license_no else self.key_by_name.get(normalize_name(name))
        key = key or ('name:' + normalize_name(name))
        candidate = self.by_key.get(key)
        if candidate is None:
            candidate = CrawledSupplierCandidate(candidate_key=key, name=name, license_no=license_no)
            self.by_key[key] = candidate
        self.key_by_name.setdefault(normalize_name(name), key)
        return candidate

    def find(self, name: str | None, license_no: str | None) -> CrawledSupplierCandidate | None:
        if license_no and 'license:' + license_no in self.by_key:
            return self.by_key['license:' + license_no]
        key = self.key_by_name.get(normalize_name(name or '')) if name else None
        return self.by_key.get(key) if key else None


def _add_source(candidate, source_type):
    if source_type not in candidate.source_types:
        candidate.source_types.append(source_type)


def _same_domain(email: str, host: str) -> bool:
    domain = email.rsplit('@', 1)[-1].lower()
    host = host.lower().removeprefix('www.')
    return bool(host) and (domain == host or host.endswith('.' + domain) or domain.endswith('.' + host))


def _add_contact(candidate, contact_type, value, evidence):
    if value and not any(c.contact_type == contact_type and c.value == value for c in candidate.contacts):
        candidate.contacts.append(Contact(contact_type=contact_type, value=value, evidence=evidence))


class SupplierCrawlTrial:
    def __init__(self, settings, fetcher: Fetcher):
        self.settings, self.fetcher = settings, fetcher
        self.candidates = _Candidates()
        self.issues: list[SourceIssue] = []

    def _issue(self, source_type, code, message, target=None):
        self.issues.append(SourceIssue(source_type=source_type, code=code, message=message[:300],
                                       target=self.fetcher.mask(target) if target else None))

    def _stats(self, source_type, run, enabled, skipped):
        if not enabled:
            return SourceStats(source_type=source_type, enabled=False, skipped_reason=skipped)
        requests, failed = self.fetcher.requests, self.fetcher.failed
        started = time.monotonic()
        records = run()
        touched = [c for c in self.candidates.by_key.values() if source_type in c.source_types]
        return SourceStats(source_type=source_type, enabled=True, requests=self.fetcher.requests - requests,
                           failed_requests=self.fetcher.failed - failed, records=records,
                           candidates=len(touched), elapsed_seconds=round(time.monotonic() - started, 2))

    # 1) 공공데이터: 품목제조보고 → 업소별 묶음 → 식품제조가공업 인허가로 연락처·소재지 보강
    def _public(self, options: TrialOptions) -> int:
        client = PublicDataClient(self.fetcher, self.settings.food_safety_api_key.get_secret_value())
        try:
            rows, url, fields = client.item_reports(options.keyword, options.item_limit)
        except FetchError as error:
            self._issue('PUBLIC_DATA', error.code, error.message, 'I1250')
            return 0
        missing = public_data.missing_fields(fields, public_data.ITEM_FIELDS)
        if missing:
            self._issue('PUBLIC_DATA', 'FIELD_NAME_MISMATCH', 'I1250 응답에 없는 필드: ' + ', '.join(missing))
        fetched_at, licenses, skipped_type, skipped_ended = now(), [], 0, 0
        wanted_types = [normalize_item_type(t) for t in options.item_types if t.strip()]
        for row in rows:
            if not row['name']:
                continue
            if wanted_types and not any(t in normalize_item_type(row['item_type']) for t in wanted_types):
                skipped_type += 1
                continue
            if not options.include_ended and (row['production_ended'] or '').strip() in ('예', 'Y', 'y'):
                skipped_ended += 1
                continue
            candidate = self.candidates.get(row['name'], row['license_no'])
            _add_source(candidate, 'PUBLIC_DATA')
            if row['item_name']:
                ref = SourceRef(source_type='PUBLIC_DATA', source_url=url, fetched_at=fetched_at,
                                record_ref='품목제조보고 ' + (row['report_no'] or '-'),
                                snippet=' / '.join(v for v in (row['item_name'], row['item_type'], row['reported_on'],
                                                              '생산종료 ' + row['production_ended'] if row['production_ended'] else None) if v))
                if not any(i.item_name == row['item_name'] for i in candidate.handled_items):
                    candidate.handled_items.append(HandledItem(item_name=row['item_name'], item_type=row['item_type'], evidence=ref))
            if row['license_no'] and row['license_no'] not in licenses:
                licenses.append(row['license_no'])
        for license_no in licenses[:options.business_limit]:
            try:
                business, url, fields = client.business(license_no)
            except FetchError as error:
                self._issue('PUBLIC_DATA', error.code, error.message, 'I1220 ' + license_no)
                continue
            missing = public_data.missing_fields(fields, public_data.BUSINESS_FIELDS)
            if missing and not any(i.code == 'FIELD_NAME_MISMATCH' and 'I1220' in i.message for i in self.issues):
                self._issue('PUBLIC_DATA', 'FIELD_NAME_MISMATCH', 'I1220 응답에 없는 필드: ' + ', '.join(missing))
            if business is None:
                self._issue('PUBLIC_DATA', 'BUSINESS_NOT_FOUND', '식품제조가공업 인허가에 같은 인허가번호가 없습니다.', license_no)
                continue
            candidate = self.candidates.get(business['name'] or license_no, license_no)
            ref = SourceRef(source_type='PUBLIC_DATA', source_url=url, fetched_at=now(), record_ref='인허가 ' + license_no,
                            snippet=business['business_type'])
            _add_contact(candidate, 'PHONE', business['phone'], ref)
            candidate.location = candidate.location or business['address']
        if skipped_type or skipped_ended:
            self._issue('PUBLIC_DATA', 'ITEM_FILTERED',
                        f'품목유형이 맞지 않는 {skipped_type}건, 생산 종료 {skipped_ended}건을 제외했습니다.')
        if len(licenses) > options.business_limit:
            self._issue('PUBLIC_DATA', 'BUSINESS_LIMIT', f'업소 {len(licenses)}곳 중 {options.business_limit}곳만 연락처를 보강했습니다.')
        return len(rows)

    # 2) HACCP: 인허가번호로 기존 후보에 인증 근거를 붙이고, 품목 유형 필터가 있으면 새 후보도 만든다.
    def _haccp(self, options: TrialOptions) -> int:
        client = HaccpClient(self.fetcher, self.settings.data_go_kr_service_key.get_secret_value(), self.settings.haccp_api_url)
        count, item_type, page_rows = 0, (options.haccp_item_type or '').strip(), min(options.haccp_rows, 100)
        for page_no in range(1, options.haccp_pages + 1):
            try:
                rows, total, url, _ = client.page(page_no, page_rows)
            except FetchError as error:
                self._issue('HACCP', error.code, error.message, f'page {page_no} rows {page_rows}')
                if page_no == 1 and page_rows > 100:
                    # 한 쪽 결과 수 한도를 넘으면 오류가 나는 서비스가 있어 100건으로 한 번 다시 시도한다.
                    page_rows = 100
                    try:
                        rows, total, url, _ = client.page(page_no, page_rows)
                    except FetchError as retry_error:
                        self._issue('HACCP', retry_error.code, retry_error.message, f'page {page_no} rows {page_rows}')
                        break
                else:
                    break
            count += len(rows)
            fetched_at = now()
            for row in rows:
                candidate = self.candidates.find(row['name'], row['license_no'])
                if candidate is None and item_type and row['name'] and item_type in (row['item_type'] or ''):
                    candidate = self.candidates.get(row['name'], row['license_no'])
                    candidate.location = ' '.join(v for v in (row['sido'], row['sgg']) if v) or None
                if candidate is None:
                    continue
                _add_source(candidate, 'HACCP')
                ref = SourceRef(source_type='HACCP', source_url=url, fetched_at=fetched_at,
                                record_ref='HACCP ' + (row['certification_no'] or '-'), snippet=row['item_type'])
                if not any(c.certification_no == row['certification_no'] for c in candidate.certifications):
                    candidate.certifications.append(Certification(
                        certification_type='HACCP', certification_no=row['certification_no'], item_type=row['item_type'],
                        valid_from=row['valid_from'], valid_until=row['valid_until'], evidence=ref))
            if not rows or (total is not None and page_no * page_rows >= total):
                break
        else:
            self._issue('HACCP', 'PAGE_LIMIT', f'{options.haccp_pages}쪽까지만 읽었습니다. 전체 대조는 다음 단계에서 정합니다.')
        return count

    # 3) 홈페이지: 네이버 지역 검색·시드 주소 → robots 허용 범위의 연락처 페이지
    def _homepage(self, options: TrialOptions) -> int:
        crawler = HomepageCrawler(self.fetcher, self.settings)
        seeds = list(options.seeds)
        has_naver = bool(self.settings.naver_client_id and self.settings.naver_client_secret)
        # 공공데이터로 찾은 업체는 '업체명 + 시군구' 웹문서 검색으로 공식 홈페이지를 찾는다.
        public_candidates = [c for c in self.candidates.by_key.values() if 'PUBLIC_DATA' in c.source_types]
        if has_naver and public_candidates:
            found = 0
            for candidate in public_candidates[:options.site_limit]:
                query = ' '.join(v for v in (candidate.name, region_of(candidate.location)) if v)
                try:
                    url = crawler.find_homepage(candidate.name, query)
                except FetchError as error:
                    self._issue('HOMEPAGE', error.code, '네이버 웹문서 검색: ' + error.message, query)
                    continue
                if url:
                    found += 1
                    seeds.append(SiteSeed(url=url, name=candidate.name, found_by='NAVER_WEB'))
            stats = crawler.web_stats
            self._issue('HOMEPAGE', 'NAVER_WEB_RESULT',
                        f'업체 {min(len(public_candidates), options.site_limit)}곳 검색 → 홈페이지 {found}곳 확인. '
                        f'검색 결과 중 채용·기업정보·쇼핑 등 {stats["skipped"]}건, 업체명 불일치 {stats["unmatched"]}건 제외.')
        if has_naver and options.naver_query:
            query = options.naver_query
            try:
                seeds += crawler.discover_naver(query, options.site_limit)
                stats = crawler.naver_stats
                self._issue('HOMEPAGE', 'NAVER_LOCAL_RESULT',
                            f'지역 검색 {stats["results"]}건 중 홈페이지 링크 없음 {stats["no_link"]}건, '
                            f'플랫폼 계정(유튜브·스마트스토어 등) {stats["platform"]}건은 건너뛰었습니다.', query)
            except FetchError as error:
                self._issue('HOMEPAGE', error.code, '네이버 지역 검색: ' + error.message)
        if not seeds:
            self._issue('HOMEPAGE', 'NO_SEEDS', '찾은 홈페이지가 없습니다(네이버 키·공공데이터 후보·--seeds 확인).')
        unique, roots = [], set()
        for seed in seeds:
            root = urlsplit(seed.url).netloc.lower().removeprefix('www.')
            if root and root not in roots:
                roots.add(root)
                unique.append(seed)
        pages = 0
        for seed in unique:
            site = crawler.crawl(seed, options.keyword)
            for code, target in site.errors:
                self._issue('HOMEPAGE', code, '홈페이지 수집 오류', target)
            if site.blocked_by_robots:
                self._issue('HOMEPAGE', 'ROBOTS_DISALLOWED', f'robots.txt로 {len(site.blocked_by_robots)}쪽을 건너뛰었습니다.',
                            site.blocked_by_robots[0])
            if not site.pages:
                continue
            pages += len(site.pages)
            if seed.found_by == 'NAVER_WEB' and not site.name_verified:
                # 검색 결과가 업체를 '언급한' 페이지였을 뿐, 사이트 자체에는 업체명이 없다 → 업체 사이트로 보지 않는다.
                self._issue('HOMEPAGE', 'SITE_NOT_VERIFIED', f'{seed.name}: 사이트 첫 화면에 업체명이 없어 제외했습니다.', seed.url)
                continue
            name = seed.name or site.title or seed.url
            candidate = self.candidates.find(name, None) or self.candidates.get(name)
            _add_source(candidate, 'HOMEPAGE')
            fetched_at = now()
            if seed.found_by == 'NAVER_LOCAL':
                ref = SourceRef(source_type='HOMEPAGE', source_url='naver-local:' + (seed.name or ''), fetched_at=fetched_at,
                                record_ref='네이버 지역 검색', snippet=seed.address)
                _add_contact(candidate, 'PHONE', seed.phone, ref)
                candidate.location = candidate.location or seed.address
            _add_contact(candidate, 'HOMEPAGE', site.pages[0].url,
                         SourceRef(source_type='HOMEPAGE', source_url=site.pages[0].url, fetched_at=fetched_at,
                                   snippet=site.title))
            site_domain = urlsplit(site.pages[0].url).hostname or ''
            for page in site.pages:
                ref = SourceRef(source_type='HOMEPAGE', source_url=page.url, fetched_at=fetched_at)
                for email in page.emails:
                    same = _same_domain(email, site_domain)
                    _add_contact(candidate, 'EMAIL', email, ref.model_copy(update={
                        'snippet': '업체 도메인 메일' if same else '외부 메일 주소(담당자 확인 필요)'}))
                for phone in page.phones[:3]:
                    _add_contact(candidate, 'PHONE', phone, ref)
                if page.keyword_snippet and not any(i.evidence.source_type == 'HOMEPAGE' for i in candidate.handled_items):
                    candidate.handled_items.append(HandledItem(
                        item_name=options.keyword, evidence=SourceRef(source_type='HOMEPAGE', source_url=page.url,
                                                                      fetched_at=fetched_at, snippet=page.keyword_snippet)))
        return pages

    def run(self, options: TrialOptions) -> TrialReport:
        started = now()
        public_on = 'public' in options.sources and bool(self.settings.food_safety_api_key)
        haccp_on = 'haccp' in options.sources and bool(self.settings.data_go_kr_service_key)
        homepage_on = 'homepage' in options.sources
        excluded = '이번 시험 대상에서 제외'
        stats = [
            self._stats('PUBLIC_DATA', lambda: self._public(options), public_on,
                        excluded if 'public' not in options.sources else '.env에 FOOD_SAFETY_API_KEY 필요'),
            self._stats('HACCP', lambda: self._haccp(options), haccp_on,
                        excluded if 'haccp' not in options.sources else '.env에 DATA_GO_KR_SERVICE_KEY 필요'),
            self._stats('HOMEPAGE', lambda: self._homepage(options), homepage_on, excluded),
        ]
        candidates = list(self.candidates.by_key.values())
        return TrialReport(keyword=options.keyword, started_at=started, finished_at=now(), sources=stats,
                           candidates=candidates, fill_rates=fill_rates(candidates), issues=self.issues)


def fill_rates(candidates: list[CrawledSupplierCandidate]) -> dict[str, str]:
    total = len(candidates)
    checks = {
        '업체명': lambda c: bool(c.name),
        '인허가번호': lambda c: bool(c.license_no),
        '소재지': lambda c: bool(c.location),
        '전화': lambda c: any(x.contact_type == 'PHONE' for x in c.contacts),
        '이메일': lambda c: any(x.contact_type == 'EMAIL' for x in c.contacts),
        '업체 도메인 메일': lambda c: any(x.contact_type == 'EMAIL' and x.evidence.snippet == '업체 도메인 메일'
                                     for x in c.contacts),
        '홈페이지': lambda c: any(x.contact_type == 'HOMEPAGE' for x in c.contacts),
        '취급 품목': lambda c: bool(c.handled_items),
        'HACCP 표시': lambda c: bool(c.certifications),
    }
    return {name: (f'{sum(map(check, candidates))}/{total} ({round(100 * sum(map(check, candidates)) / total)}%)'
                   if total else '0/0') for name, check in checks.items()}
