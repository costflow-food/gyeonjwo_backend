"""업체 홈페이지 수집: 홈페이지 주소 찾기(네이버 지역 검색·시드 파일) → robots.txt 확인 → 같은 사이트의 연락처 페이지만 소량 조회.

수집 원칙
- robots.txt가 허용한 경로만, 같은 호스트 안에서, 사이트당 crawl_max_pages_per_site쪽까지 읽는다.
- 요청 사이에 crawl_delay_seconds 간격을 두고 수집기 이름을 User-Agent로 밝힌다.
- 업체 대표 연락처(이메일·전화)와 품목 언급 문장만 근거와 함께 남기고, 페이지 원문 전체는 저장하지 않는다.
"""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from app.features.supplier_crawl.domain.names import normalize_name
from app.features.supplier_crawl.infrastructure.http import FetchError, Fetcher, is_public_web_url

# NAVER API HUB(네이버 클라우드) 지역 검색. 개발자센터 신규 등록이 끝나 HUB 키 기준으로 부른다.
# 예전 개발자센터 키를 쓰면 NAVER_LOCAL_URL을 https://openapi.naver.com/v1/search/local.json 으로 바꾼다.
CONTACT_HINTS = ('contact', 'about', 'company', 'cs', 'customer', 'inquiry', 'qna', 'location', 'map',
                 '연락', '문의', '고객', '회사소개', '기업소개', '오시는', '찾아오시는', '회사정보', '인사말')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(0\d{1,2}[-.)\s]\d{3,4}[-.\s]\d{4}|1[5-9]\d{2}-\d{4})(?!\d)')
IGNORED_EMAIL_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
PLACEHOLDER_DOMAINS = ('example.com', 'domain.com', 'email.com', 'yourdomain', 'sentry.io')
TAG = re.compile(r'<[^>]+>')
# 지역 검색 링크가 업체 홈페이지가 아니라 플랫폼 계정인 경우(2026-11-02 시험: 유튜브 채널에서 유튜브 지원 메일을 수집함).
# 이런 링크는 업체 연락처 근거가 아니므로 크롤링하지 않고 건너뛴 수만 남긴다.
PLATFORM_DOMAINS = ('youtube.com', 'youtu.be', 'smartstore.naver.com', 'blog.naver.com', 'cafe.naver.com',
                    'm.blog.naver.com', 'instagram.com', 'facebook.com', 'band.us', 'pf.kakao.com', 'linktr.ee',
                    'coupang.com', '11st.co.kr', 'gmarket.co.kr')


# 업체 공식 사이트가 아닌 채용·기업정보·목록·백과 사이트. 웹문서 검색 결과에서 홈페이지로 채택하지 않는다.
DIRECTORY_DOMAINS = ('jobploy.kr', 'saramin.co.kr', 'jobkorea.co.kr', 'wanted.co.kr', 'catch.co.kr', 'teamblind.com',
                     'albamon.com', 'jobplanet.co.kr', 'incruit.com', 'thevc.kr', 'nextunicorn.kr', 'ifactoryhub.com',
                     'tridge.com', 'namu.wiki', 'wikipedia.org', 'kisline.com', 'bizno.net', 'kreport.co.kr',
                     'foodsafetykorea.go.kr', 'data.go.kr', 'naver.com', 'daum.net', 'kakao.com', 'google.com',
                     'tistory.com', 'brunch.co.kr', 'alibaba.com', 'amazon.com', 'auction.co.kr', 'ssg.com',
                     'kurly.com', 'oasis.co.kr', 'tmon.co.kr', 'wemakeprice.com', 'findcompany.kr', 'moneypin.biz',
                     'marketbz.com', 'grandculture.net', 'verb.run', 'catchapp.co.kr', 'saramin.kr')
# 정부·공공기관·학교 도메인은 업체 사이트가 아니다(2026-11-02 측정에서 군청·시청 사이트가 잘못 채택됨).
PUBLIC_SECTOR_SUFFIXES = ('.go.kr', '.or.kr', '.re.kr', '.ac.kr', '.mil.kr', '.kr.gov')


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith('.' + d) for d in domains)


def is_directory_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    return _matches(host, DIRECTORY_DOMAINS) or host.endswith(PUBLIC_SECTOR_SUFFIXES)


def is_platform_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    return any(host == domain or host.endswith('.' + domain) for domain in PLATFORM_DOMAINS)


@dataclass
class SiteSeed:
    url: str
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    found_by: str = 'SEED'


@dataclass
class PageEvidence:
    url: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    keyword_snippet: str | None = None


@dataclass
class SiteResult:
    seed: SiteSeed
    title: str | None = None
    pages: list[PageEvidence] = field(default_factory=list)
    blocked_by_robots: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    name_verified: bool = False  # 첫 화면 제목·본문에 업체명이 있는지(업체 사이트 확인)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.mailtos: list[str] = []
        self.title = ''
        self._text: list[str] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self._skip += 1
        elif tag == 'title':
            self._in_title = True
        elif tag == 'a':
            href = dict(attrs).get('href') or ''
            if href.lower().startswith('mailto:'):
                self.mailtos.append(href[7:].split('?', 1)[0])
            self._href, self._link_text = href, []

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self._skip:
            self._skip -= 1
        elif tag == 'title':
            self._in_title = False
        elif tag == 'a' and self._href is not None:
            self.links.append((self._href, ' '.join(self._link_text).strip()))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        text = data.strip()
        if text:
            self._text.append(text)
            if self._href is not None:
                self._link_text.append(text)

    @property
    def text(self) -> str:
        return ' '.join(self._text)


def clean_emails(values: list[str]) -> list[str]:
    found = []
    for value in values:
        email = value.strip().strip('.').lower()
        if not EMAIL.fullmatch(email) or email.endswith(IGNORED_EMAIL_SUFFIXES):
            continue
        if any(domain in email.split('@', 1)[1] for domain in PLACEHOLDER_DOMAINS):
            continue
        if email not in found:
            found.append(email)
    return found


def normalize_phone(value: str) -> str:
    return re.sub(r'[.)\s]', '-', value.strip())


def keyword_snippet(text: str, keyword: str, width: int = 60) -> str | None:
    index = text.find(keyword)
    if index < 0:
        return None
    start, end = max(0, index - width), index + len(keyword) + width
    return re.sub(r'\s+', ' ', text[start:end]).strip()


def _site_root(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, '/', '', ''))


def _same_site(url: str, root: str) -> bool:
    return urlsplit(url).netloc.lower().removeprefix('www.') == urlsplit(root).netloc.lower().removeprefix('www.')


class HomepageCrawler:
    def __init__(self, fetcher: Fetcher, settings):
        self.fetcher, self.settings = fetcher, settings

    def _naver_headers(self, url: str) -> dict:
        secret = self.settings.naver_client_secret.get_secret_value()
        if urlsplit(url).hostname == 'openapi.naver.com':
            return {'X-Naver-Client-Id': self.settings.naver_client_id, 'X-Naver-Client-Secret': secret}
        return {'X-NCP-APIGW-API-KEY-ID': self.settings.naver_client_id, 'X-NCP-APIGW-API-KEY': secret}

    def find_homepage(self, company: str, query: str) -> str | None:
        """웹문서 검색으로 업체 공식 홈페이지 첫 화면 주소를 찾는다.
        채택 조건: 공개 웹 주소이고, 플랫폼·채용·기업정보 사이트가 아니며, 제목·요약에 업체명이 들어 있는 결과."""
        if not hasattr(self, 'web_stats'):
            self.web_stats = {'skipped': 0, 'unmatched': 0}
        url = self.settings.naver_webkr_url
        payload = self.fetcher.get_json(url, headers=self._naver_headers(url), params={'query': query, 'display': '10'})
        wanted = normalize_name(company)
        for item in payload.get('items') or []:
            link = (item.get('link') or '').strip()
            if not link or not is_public_web_url(link) or is_platform_url(link) or is_directory_url(link):
                self.web_stats['skipped'] += 1
                continue
            text = normalize_name(TAG.sub('', (item.get('title') or '') + ' ' + (item.get('description') or '')))
            if wanted and len(wanted) >= 2 and wanted in text:
                return _site_root(link)
            self.web_stats['unmatched'] += 1
        return None

    def discover_naver(self, query: str, limit: int) -> list[SiteSeed]:
        """네이버 지역 검색 결과 중 홈페이지 링크가 있는 업체. 검색 결과 자체는 잠정 근거다."""
        url = self.settings.naver_local_url
        payload = self.fetcher.get_json(url, headers=self._naver_headers(url),
                                        params={'query': query, 'display': str(min(limit, 5)), 'sort': 'random'})
        seeds, self.naver_stats = [], {'results': 0, 'no_link': 0, 'platform': 0}
        for item in payload.get('items') or []:
            self.naver_stats['results'] += 1
            link = (item.get('link') or '').strip()
            if not link or not is_public_web_url(link):
                self.naver_stats['no_link'] += 1
                continue
            if is_platform_url(link):
                self.naver_stats['platform'] += 1
                continue
            seeds.append(SiteSeed(url=link, name=TAG.sub('', item.get('title') or '').strip() or None,
                                  phone=(item.get('telephone') or '').strip() or None,
                                  address=(item.get('roadAddress') or item.get('address') or '').strip() or None,
                                  found_by='NAVER_LOCAL'))
        return seeds

    def _robots(self, root: str) -> RobotFileParser | None:
        """4xx는 제한 없음, 5xx·접속 실패는 수집하지 않음(보수적)."""
        parser = RobotFileParser()
        try:
            response = self.fetcher.get(urljoin(root, '/robots.txt'),
                                        redirect_allowed=lambda target: _same_site(target, root))
        except FetchError:
            return None
        if response.status_code >= 500:
            return None
        if response.status_code >= 400:
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())
        return parser

    def _page(self, url: str, keyword: str, redirect_allowed=None) -> tuple[PageEvidence, _PageParser] | None:
        response = self.fetcher.get(url, redirect_allowed=redirect_allowed)
        if response.status_code != 200 or 'html' not in response.headers.get('content-type', 'text/html'):
            raise FetchError(f'HTTP_{response.status_code}', '홈페이지 응답을 읽을 수 없습니다.')
        parser = _PageParser()
        parser.feed(response.text)
        text = parser.text
        evidence = PageEvidence(url=str(response.url),
                                emails=clean_emails(parser.mailtos + EMAIL.findall(text)),
                                phones=list(dict.fromkeys(normalize_phone(p) for p in PHONE.findall(text))),
                                keyword_snippet=keyword_snippet(text, keyword))
        return evidence, parser

    def crawl(self, seed: SiteSeed, keyword: str) -> SiteResult:
        result = SiteResult(seed=seed)
        if not is_public_web_url(seed.url):
            result.errors.append(('URL_NOT_ALLOWED', seed.url))
            return result
        root = _site_root(seed.url)
        robots = self._robots(root)
        if robots is None:
            result.errors.append(('ROBOTS_UNAVAILABLE', root))
            return result
        agent = self.settings.crawl_user_agent
        redirect_allowed = lambda target: _same_site(target, root) and robots.can_fetch(agent, target)
        queue, seen = [seed.url], set()
        while queue and len(result.pages) < self.settings.crawl_max_pages_per_site:
            url = queue.pop(0).split('#', 1)[0]
            if url in seen:
                continue
            seen.add(url)
            if not robots.can_fetch(agent, url):
                result.blocked_by_robots.append(url)
                continue
            try:
                evidence, parser = self._page(url, keyword, redirect_allowed=redirect_allowed)
            except FetchError as error:
                result.errors.append((error.code, url))
                continue
            result.pages.append(evidence)
            if result.title is None and parser.title.strip():
                result.title = re.sub(r'\s+', ' ', parser.title).strip()[:100]
            wanted = normalize_name(seed.name)
            if wanted and len(wanted) >= 2 and wanted in normalize_name(parser.title + ' ' + parser.text):
                result.name_verified = True
            if len(result.pages) == 1:
                queue.extend(_contact_links(parser.links, url, root))
        return result


def _contact_links(links: list[tuple[str, str]], base: str, root: str) -> list[str]:
    picked = []
    for href, text in links:
        target = urljoin(base, href)
        label = (href + ' ' + text).lower()
        if (target.startswith(('http://', 'https://')) and _same_site(target, root)
                and any(hint in label for hint in CONTACT_HINTS) and target not in picked):
            picked.append(target)
    return picked[:8]


def read_seed_file(path) -> list[SiteSeed]:
    """한 줄에 '주소' 또는 '주소,업체명'. #으로 시작하는 줄은 무시한다."""
    seeds = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        url, _, name = line.partition(',')
        seeds.append(SiteSeed(url=url.strip(), name=name.strip() or None))
    return seeds
