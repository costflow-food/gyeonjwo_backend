"""Read public homepage HTML and extract published contact details only."""
import ipaddress
import re
import socket
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
import httpcore
from bs4 import BeautifulSoup

MAX_RESPONSE_BYTES = 2_000_000
EMAIL = re.compile(r'[A-Z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,}', re.I)
PHONE = re.compile(r'(?<![\w.+-])(?:\+\d{1,3}[ ().-]*\d[\d ().-]{5,}\d|0\d{1,3}[ .-]+\d{3,4}[ .-]+\d{4})(?![\w.-])')
PHONE_LABEL = re.compile(r'(?:전화(?:번호)?|대표번호|연락처|tel(?:ephone)?|phone|call)\s*[.:：]?\s*([+\d][\d .-]{6,}\d)', re.I)
FAX = re.compile(r'\b(?:fax|facsimile)\b|팩스', re.I)


def public_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname
            or parts.username or parts.password or parts.port not in {None, 80, 443}):
        raise ValueError('UNSAFE_URL')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError('UNSAFE_URL')
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))


def read_body(response):
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError('RESPONSE_TOO_LARGE')
        chunks.append(chunk)
    return b''.join(chunks)


def extract_contacts(html):
    soup = BeautifulSoup(html, 'html.parser')
    emails, phones = [], []
    seen_phones = set()

    def add_phone(value):
        value = value.strip()
        digits = re.sub(r'\D', '', value)
        if re.fullmatch(r'[+\d ().-]+', value) and 8 <= len(digits) <= 15 and digits not in seen_phones:
            seen_phones.add(digits)
            phones.append(value)

    for hidden in soup.select('script, style, noscript, template, [hidden], [aria-hidden="true"]'):
        hidden.decompose()
    for anchor in soup.select('a[href]'):
        href = unquote(anchor['href'])
        if href.lower().startswith('mailto:'):
            for value in href[7:].split('?', 1)[0].split(','):
                if EMAIL.fullmatch(value.strip()):
                    emails.append(value.strip().lower())
        elif href.lower().startswith('tel:'):
            if not FAX.search(anchor.get_text(' ', strip=True)):
                add_phone(href[4:].split(';', 1)[0].split('?', 1)[0])
    lines = soup.get_text('\n', strip=True).splitlines()
    emails.extend(match.group().lower() for match in EMAIL.finditer('\n'.join(lines)))
    previous = ''
    for line in lines:
        if not FAX.fullmatch(previous.strip(' :：')):
            for match in PHONE.finditer(FAX.split(line, maxsplit=1)[0]):
                add_phone(match.group())
            for match in PHONE_LABEL.finditer(FAX.split(previous + ' ' + line, maxsplit=1)[0]):
                add_phone(match.group(1))
        previous = line
    return {'emails': list(dict.fromkeys(emails)), 'phones': phones}


def collect_homepage_contacts(client, homepage_url):
    """Read one public page, following redirects but not links on the page."""
    url = homepage_url
    try:
        for _ in range(4):
            url = public_url(url)
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    location = response.headers.get('location')
                    if not location:
                        raise ValueError('INVALID_REDIRECT')
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                if 'html' not in response.headers.get('content-type', 'text/html').lower():
                    raise ValueError('NOT_HTML')
                html = read_body(response)
                soup = BeautifulSoup(html, 'html.parser')
                refresh = soup.find('meta', attrs={'http-equiv': re.compile('^refresh$', re.I)})
                target = re.match(r'\s*\d+\s*;\s*url\s*=\s*(.+)', refresh.get('content', ''), re.I) if refresh else None
                if target:
                    url = urljoin(url, target.group(1).strip(' \"\''))
                    continue
                contacts = extract_contacts(html)
            # shortcut: many contacts may belong to multiple businesses; require review until ownership can be verified.
            if len(contacts['phones']) > 5 or len(contacts['emails']) > 5:
                return {'emails': [], 'phones': [], 'contact_source_url': url, 'contact_status': 'NEEDS_REVIEW'}
            return {**contacts, 'contact_source_url': url,
                    'contact_status': 'FOUND' if any(contacts.values()) else 'NOT_FOUND'}
        raise ValueError('TOO_MANY_REDIRECTS')
    except (httpx.HTTPError, httpcore.NetworkError, httpcore.TimeoutException, ValueError, OSError) as error:
        return {'emails': [], 'phones': [], 'contact_source_url': None, 'contact_status': 'FETCH_FAILED',
                'contact_error': str(error) if isinstance(error, ValueError) else type(error).__name__}
