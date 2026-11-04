"""수집 공통 HTTP. 인증키가 든 주소는 마스킹한 형태로만 기록하고, 응답 크기·시간을 제한한다."""
import ipaddress
import json
import re
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote, quote_plus, urlsplit

import httpcore
import httpx

from app.features.supplier_crawl.infrastructure.network import (
    PublicHTTPTransport, UnsafeAddress, public_address, resolve_public,
)

MAX_RESPONSE_BYTES = 2_000_000
_KEY_PARAMS = re.compile(r'(?i)((?:service_?key|apikey|api_key|key)=)[^&#]+')
_CREDENTIAL_HEADERS = {'authorization', 'proxy-authorization', 'cookie',
    'x-ncp-apigw-api-key', 'x-ncp-apigw-api-key-id',
    'x-naver-client-secret', 'x-naver-client-id'}


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port if url.port is not None else (443 if url.scheme == 'https' else 80)
    return url.scheme, url.host, port


class FetchError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(code)


def now() -> datetime:
    return datetime.now(timezone.utc)


def mask_url(url: str, secrets: list[str]) -> str:
    masked = _KEY_PARAMS.sub(r'\1***', url)
    for secret in secrets:
        if secret:
            forms = {secret, quote(secret, safe=''), quote_plus(secret, safe='')}
            forms |= {json.dumps(form, ensure_ascii=ascii_only)[1:-1]
                      for form in tuple(forms) for ascii_only in (False, True)}
            for form in sorted(forms, key=len, reverse=True):
                # Percent escapes are case-insensitive; secret text is not.
                pattern = ''.join('%' + ''.join(f'[{c.lower()}{c.upper()}]' if c.isalpha() else c
                                                for c in token[1:])
                                  if re.fullmatch(r'%[0-9A-Fa-f]{2}', token) else re.escape(token)
                                  for token in re.findall(r'%[0-9A-Fa-f]{2}|.', form, re.DOTALL))
                masked = re.sub(pattern, lambda _: '***', masked)
    return masked


def is_public_web_url(url: str) -> bool:
    """URL 형식·리터럴 주소 검사. DNS와 연결 IP 검사는 Fetcher와 TCP backend에서 한다."""
    try:
        parts = urlsplit(url)
        parts.port  # Reject malformed/out-of-range ports without raising to callers.
    except ValueError:
        return False
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith('.localhost') or host.endswith('.local'):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host and '%' not in host
    return public_address(str(address))


class Fetcher:
    """요청 수·실패 수를 세고, 같은 호스트에는 crawl_delay_seconds 간격을 지킨다."""

    def __init__(self, settings, transport=None, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.secrets = settings.secrets()
        self.requests = 0
        self.failed = 0
        self._sleep, self._clock = sleep, clock
        self._last_by_host: dict[str, float] = {}
        self._client = httpx.Client(timeout=settings.crawl_timeout_seconds,
                                    transport=transport if transport is not None else PublicHTTPTransport(),
                                    follow_redirects=False, trust_env=False,
                                    headers={'User-Agent': settings.crawl_user_agent,
                                             # 일부 공공 API가 압축 헤더와 실제 본문이 달라 해제 오류가 난다. 압축 없이 받는다.
                                             'Accept-Encoding': 'identity'})

    def close(self):
        self._client.close()

    def mask(self, url: str) -> str:
        return mask_url(url, self.secrets)

    def _has_credentials(self, request: httpx.Request) -> bool:
        return bool(_CREDENTIAL_HEADERS.intersection(request.headers)
                    or any(self.mask(value) != value for value in request.headers.values())
                    or self.mask(str(request.url)) != str(request.url))

    def preview(self, text: str, limit: int = 160) -> str:
        """오류 확인용 본문 앞부분. 인증키는 가린다."""
        return re.sub(r'\s+', ' ', self.mask(text or ''))[:limit]

    def _wait(self, host: str):
        last = self._last_by_host.get(host)
        if last is not None:
            remaining = self.settings.crawl_delay_seconds - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_by_host[host] = self._clock()

    def get(self, url: str, *, headers: dict | None = None, params: dict | None = None,
            timeout: float | None = None,
            redirect_allowed: Callable[[str], bool] | None = None) -> httpx.Response:
        try:
            options = {'timeout': timeout} if timeout else {}
            request = self._client.build_request('GET', url, headers=headers, params=params, **options)
            initial_origin = _origin(request.url)
            initial_authenticated = self._has_credentials(request)
            for hop in range(4):
                if not is_public_web_url(str(request.url)):
                    raise FetchError('URL_NOT_ALLOWED', '공개 웹 주소만 허용합니다.')
                # This guards every hop, including injected offline transports.
                # The default backend independently validates and pins at connect.
                resolve_public(request.url.host, request.url.port or (443 if request.url.scheme == 'https' else 80))
                self._wait(request.url.host)
                self.requests += 1
                with closing(self._client.send(request, stream=True)) as response:
                    if response.has_redirect_location:
                        if hop == 3:
                            raise FetchError('REDIRECT_LIMIT', '리다이렉트 한도를 초과했습니다.')
                        target = response.next_request
                        if target is None:
                            raise FetchError('INVALID_REDIRECT', '리다이렉트 주소가 없습니다.')
                        # Callers such as HomepageCrawler impose a narrower site
                        # and robots boundary, before DNS or destination I/O.
                        if redirect_allowed is not None and not redirect_allowed(str(target.url)):
                            raise FetchError('REDIRECT_NOT_ALLOWED', '수집 범위를 벗어나는 이동을 차단했습니다.')
                        # Keep the initial credential boundary even if a same-origin
                        # hop rewrites, percent-encodes or removes the credentials.
                        leaves_authenticated_origin = (initial_authenticated
                                                       and _origin(target.url) != initial_origin)
                        # Also preserve checks for credentials introduced on later hops.
                        if (leaves_authenticated_origin
                                or (_origin(target.url) != _origin(request.url)
                                    and (self._has_credentials(request)
                                         or self.mask(str(target.url)) != str(target.url)))):
                            raise FetchError('AUTH_REDIRECT_BLOCKED', '인증 요청의 다른 origin 이동을 차단했습니다.')
                        request = target
                        continue
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise FetchError('RESPONSE_TOO_LARGE', '응답이 수집 한도보다 큽니다.')
                        chunks.append(chunk)
                    return httpx.Response(response.status_code, headers=response.headers, content=b''.join(chunks),
                                          request=response.request)
        except UnsafeAddress:
            self.failed += 1
            raise FetchError('URL_NOT_ALLOWED', '공개 인터넷 주소만 허용합니다.') from None
        except FetchError:
            self.failed += 1
            raise
        except (httpx.TimeoutException, httpcore.TimeoutException):
            self.failed += 1
            raise FetchError('TIMEOUT', '응답 시간이 초과됐습니다.') from None
        except httpx.DecodingError:
            self.failed += 1
            raise FetchError('DECODING_ERROR', '응답 본문 압축을 풀 수 없습니다.') from None
        except (httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError) as error:
            self.failed += 1
            raise FetchError('NETWORK_ERROR', type(error).__name__) from None

    def get_json(self, url: str, *, headers: dict | None = None, params: dict | None = None,
                 timeout: float | None = None):
        response = self.get(url, headers=headers, params=params, timeout=timeout)
        if response.status_code != 200:
            self.failed += 1
            raise FetchError(f'HTTP_{response.status_code}', '정상 응답이 아닙니다. 본문 앞부분: ' + self.preview(response.text))
        try:
            return response.json()
        except ValueError:
            self.failed += 1
            code, message = _xml_error(self.mask(response.text))
            if code == 'NOT_JSON':
                message += ' 본문 앞부분: ' + self.preview(response.text)
            raise FetchError(self.preview(code, 80), self.preview(message, 300)) from None


def _xml_error(text: str) -> tuple[str, str]:
    """공공데이터포털은 인증 오류를 JSON 요청에도 XML로 돌려준다. 코드·메시지만 꺼낸다."""
    def tag(name):
        found = re.search(rf'<{name}>([^<]{{1,200}})</{name}>', text or '')
        return found.group(1).strip() if found else None
    code = tag('returnReasonCode') or tag('resultCode')
    message = tag('returnAuthMsg') or tag('resultMsg') or tag('errMsg')
    if code or message:
        return 'API_' + (code or 'ERROR'), message or '오류 응답'
    return 'NOT_JSON', 'JSON 응답이 아닙니다. 인증키·주소·응답 형식을 확인하세요.'
