"""Google search -> public pages -> published email and phone. No business inference."""
from datetime import datetime, timezone
from shutil import which
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import httpcore
from bs4 import BeautifulSoup

from app.features.supplier_crawl.infrastructure.homepage_contacts import collect_homepage_contacts, public_url, read_body
from app.features.supplier_crawl.infrastructure.network import PublicHTTPTransport

SEARCH_URL = 'https://www.google.com/search'
GOOGLE_HOSTS = {'www.google.com', 'google.com'}


class SearchBlocked(Exception):
    pass


def _external_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password
            or parts.hostname in GOOGLE_HOSTS or parts.hostname.endswith('.google.com')):
        return None
    return url


def parse_results(html, limit):
    soup = BeautifulSoup(html, 'html.parser')
    results, seen = [], set()
    for heading in soup.find_all('h3'):
        anchor = heading.find_parent('a', href=True)
        title = heading.get_text(' ', strip=True)
        if not anchor or not title:
            continue
        try:
            url = urljoin(SEARCH_URL, anchor['href'])
            parts = urlsplit(url)
            if parts.hostname in GOOGLE_HOSTS and parts.path in {'/url', '/goto'}:
                values = parse_qs(parts.query)
                target = (values.get('q') or values.get('url') or [''])[0]
                url = _external_url(target) or url
            elif not _external_url(url):
                continue
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        block = anchor.find_parent(class_=['MjjYud', 'g', 'Gx5Zad'])
        snippet = block.select_one('.VwiC3b, .yXK7lf, .IsZvec, [data-sncf]') if block else None
        results.append({'search_title': title, 'search_result_url': url,
                        'snippet': snippet.get_text(' ', strip=True)[:1000] if snippet else None})
        if len(results) == limit:
            break
    return results


def _search_page(url):
    from scrapling.fetchers import StealthyFetcher

    executable = which('chromium') or which('chromium-browser')
    page = StealthyFetcher.fetch(url, real_chrome=True, headless=True, timeout=30000,
                                **({'executable_path': executable} if executable else {}))
    if page.status in {403, 429}:
        raise SearchBlocked
    if page.status >= 400:
        raise RuntimeError(f'Google HTTP {page.status}')
    return page.body, page.url


def _resolve_result_url(client, url):
    if _external_url(url):
        return url
    # Google sometimes wraps destination URLs in opaque /goto links.
    with client.stream('GET', public_url(url)) as response:
        if response.is_redirect:
            return _external_url(urljoin(url, response.headers.get('location', '')))
        response.raise_for_status()
        soup = BeautifulSoup(read_body(response), 'html.parser')
    targets = set()
    for anchor in soup.select('a[href]'):
        target = _external_url(urljoin(url, anchor['href']))
        if target:
            targets.add(target)
    return targets.pop() if len(targets) == 1 else None


def search_suppliers(query, limit=10, *, transport=None):
    source_url = SEARCH_URL + '?' + urlencode({'q': query, 'num': limit, 'hl': 'ko'})
    base = {'query': query, 'search_url': source_url, 'suppliers': []}
    try:
        html, current_url = _search_page(source_url)
        results = parse_results(html, limit)
        soup = BeautifulSoup(html, 'html.parser')
        if urlsplit(current_url).path.startswith('/sorry') or (not results and soup.select_one('.g-recaptcha, form[action*="/sorry/"]')):
            return {**base, 'status': 'ERROR', 'code': 'SEARCH_BLOCKED'}
        if not results:
            text = soup.get_text(' ', strip=True)
            empty = any(marker in text for marker in ('일치하는 검색결과가 없습니다', '검색결과가 없습니다', 'did not match any documents'))
            return {**base, 'status': 'EMPTY'} if empty else {**base, 'status': 'ERROR', 'code': 'SEARCH_FORMAT_CHANGED'}
    except SearchBlocked:
        return {**base, 'status': 'ERROR', 'code': 'SEARCH_BLOCKED'}
    except Exception as error:
        return {**base, 'status': 'ERROR', 'code': 'SEARCH_BROWSER_ERROR', 'error_type': type(error).__name__}

    suppliers, seen = [], set()
    with httpx.Client(transport=transport if transport is not None else PublicHTTPTransport(), timeout=15,
                      follow_redirects=False, trust_env=False) as client:
        for result in results:
            try:
                destination = _resolve_result_url(client, result['search_result_url'])
                if not destination:
                    raise ValueError('URL_UNRESOLVED')
                parts = urlsplit(destination)
                homepage = urlunsplit((parts.scheme, parts.netloc, '/', '', ''))
                if homepage in seen:
                    continue
                seen.add(homepage)
                contacts = collect_homepage_contacts(client, destination)
                if contacts['contact_status'] in {'NOT_FOUND', 'FETCH_FAILED'} and destination != homepage:
                    homepage_contacts = collect_homepage_contacts(client, homepage)
                    if homepage_contacts['contact_status'] == 'FOUND' or contacts['contact_status'] == 'FETCH_FAILED':
                        contacts = homepage_contacts
                suppliers.append({**result, 'search_result_url': destination, 'homepage_url': homepage, **contacts})
            except (httpx.HTTPError, httpcore.NetworkError, httpcore.TimeoutException, ValueError, OSError):
                suppliers.append({**result, 'homepage_url': None, 'emails': [], 'phones': [],
                                  'contact_source_url': None, 'contact_status': 'FETCH_FAILED', 'contact_error': 'URL_UNRESOLVED'})
    return {**base, 'status': 'OK', 'suppliers': suppliers, 'fetched_at': datetime.now(timezone.utc).isoformat()}
