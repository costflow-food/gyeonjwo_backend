"""Read explicit URLs and extract observed contact evidence without discovery."""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.features.supplier_crawl.infrastructure.http import FetchError, Fetcher, now

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(0\d{1,2}[-.)\s]\d{3,4}[-.\s]\d{4}|1[5-9]\d{2}-\d{4})(?!\d)')
IGNORED_EMAIL_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')


def canonical_url(url: str) -> str:
    return str(httpx.URL(url).copy_with(fragment=None))


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, '/robots.txt', '', ''))


@dataclass
class PageEvidence:
    url: str
    fetched_at: str
    title: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    keyword_snippet: str | None = None


@dataclass
class PageResult:
    requested_url: str
    status: str = 'FAILED'
    page: PageEvidence | None = None
    error_code: str | None = None


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.mailtos: list[str] = []
        self.title = ''
        self._text: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self._skip += 1
        if self._skip:
            return
        if tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self._text.append(alt)
        elif tag == 'title':
            self._in_title = True
        elif tag == 'a':
            href = dict(attrs).get('href') or ''
            if href.lower().startswith('mailto:'):
                self.mailtos.append(href[7:].split('?', 1)[0])

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self._skip:
            self._skip -= 1
        elif tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        text = data.strip()
        if text:
            self._text.append(text)

    @property
    def text(self) -> str:
        return ' '.join(self._text)


def clean_emails(values: list[str]) -> list[str]:
    found = []
    for value in values:
        email = value.strip().strip('.').lower()
        if EMAIL.fullmatch(email) and not email.endswith(IGNORED_EMAIL_SUFFIXES) and email not in found:
            found.append(email)
    return found


def keyword_snippet(text: str, keyword: str, width: int = 60) -> str | None:
    index = text.find(keyword)
    if index < 0:
        return None
    return text[max(0, index - width):index + len(keyword) + width]


class HomepageCrawler:
    def __init__(self, fetcher: Fetcher, allowed_urls: list[str]):
        self.fetcher = fetcher
        self.allowed_urls = set(allowed_urls)
        self.allowed_robots = {robots_url(url) for url in allowed_urls}
        self._robots_cache = {}

    def _robots(self, url: str) -> RobotFileParser | None:
        target = robots_url(url)
        if target in self._robots_cache:
            return self._robots_cache[target]
        parser = RobotFileParser()
        try:
            response = self.fetcher.get(target,
                redirect_allowed=lambda value: canonical_url(value) in self.allowed_robots)
        except FetchError:
            parser = None
        else:
            if response.status_code >= 500 or response.status_code in (401, 403):
                parser = None
            elif response.status_code >= 400:
                parser.parse([])
            else:
                parser.parse(response.text.splitlines())
        self._robots_cache[target] = parser
        return parser

    def _allowed_redirect(self, target: str) -> bool:
        if canonical_url(target) not in self.allowed_urls:
            return False
        robots = self._robots(target)
        return robots is not None and robots.can_fetch(self.fetcher.settings.crawl_user_agent, target)

    def collect(self, url: str, keyword: str) -> PageResult:
        result = PageResult(requested_url=url)
        robots = self._robots(url)
        if robots is None:
            result.error_code = 'ROBOTS_UNAVAILABLE'
            return result
        if not robots.can_fetch(self.fetcher.settings.crawl_user_agent, url):
            result.status, result.error_code = 'BLOCKED', 'ROBOTS_DISALLOWED'
            return result
        try:
            response = self.fetcher.get(url, redirect_allowed=self._allowed_redirect)
            if response.status_code != 200:
                raise FetchError(f'HTTP_{response.status_code}', '페이지를 읽을 수 없습니다.')
            if 'html' not in response.headers.get('content-type', '').lower():
                raise FetchError('UNSUPPORTED_CONTENT_TYPE', 'HTML 페이지만 수집합니다.')
        except FetchError as error:
            result.error_code = error.code
            return result
        parser = _PageParser()
        parser.feed(response.text)
        result.page = PageEvidence(url=str(response.url), fetched_at=now().isoformat(),
            title=re.sub(r'\s+', ' ', parser.title).strip()[:100] or None,
            emails=clean_emails(parser.mailtos + EMAIL.findall(parser.text)),
            phones=list(dict.fromkeys(re.sub(r'[.)\s]', '-', value.strip()) for value in PHONE.findall(parser.text))),
            keyword_snippet=keyword_snippet(parser.text, keyword) if keyword else None)
        result.status = 'COLLECTED'
        return result
