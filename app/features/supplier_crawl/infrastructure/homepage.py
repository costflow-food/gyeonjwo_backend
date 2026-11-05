"""Collect explicitly supplied public URLs and their same-site contact pages."""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from app.features.supplier_crawl.domain.names import normalize_name
from app.features.supplier_crawl.infrastructure.http import FetchError, Fetcher, is_public_web_url, now

CONTACT_HINTS = ('contact', 'about', 'company', 'cs', 'customer', 'inquiry', 'qna', 'location', 'map',
                 '연락', '문의', '고객', '회사소개', '기업소개', '오시는', '찾아오시는', '회사정보', '인사말')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(0\d{1,2}[-.)\s]\d{3,4}[-.\s]\d{4}|1[5-9]\d{2}-\d{4})(?!\d)')
IGNORED_EMAIL_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
PLACEHOLDER_DOMAINS = ('example.com', 'domain.com', 'email.com', 'yourdomain', 'sentry.io')
@dataclass
class SiteSeed:
    url: str
    name: str | None = None


@dataclass
class PageEvidence:
    url: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    keyword_snippet: str | None = None
    fetched_at: str = ''


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
        if tag == 'img' and not self._skip:
            alt = dict(attrs).get('alt')
            if alt:
                self._text.append(alt)
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

    def _robots(self, root: str) -> RobotFileParser | None:
        """4xx는 제한 없음, 5xx·접속 실패는 수집하지 않음(보수적)."""
        parser = RobotFileParser()
        try:
            response = self.fetcher.get(urljoin(root, '/robots.txt'),
                                        redirect_allowed=lambda target: _same_site(target, root))
        except FetchError:
            return None
        if response.status_code >= 500 or response.status_code in (401, 403):
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
                                keyword_snippet=keyword_snippet(text, keyword) if keyword else None,
                                fetched_at=now().isoformat())
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
        while queue and len(seen) < self.settings.crawl_max_pages_per_site:
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
