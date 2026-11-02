"""수집 공통 HTTP. 인증키가 든 주소는 마스킹한 형태로만 기록하고, 응답 크기·시간을 제한한다."""
import ipaddress
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

MAX_RESPONSE_BYTES = 2_000_000
_KEY_PARAMS = re.compile(r'(?i)((?:service_?key|apikey|api_key|key)=)[^&#]+')


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
            masked = masked.replace(secret, '***').replace(quote(secret, safe=''), '***')
    return masked


def is_public_web_url(url: str) -> bool:
    """http(s)이고 로컬·사설 주소가 아닌 경우만 허용한다."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host == 'localhost' or host.endswith('.localhost') or host.endswith('.local'):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host
    return address.is_global


class Fetcher:
    """요청 수·실패 수를 세고, 같은 호스트에는 crawl_delay_seconds 간격을 지킨다."""

    def __init__(self, settings, transport=None, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.secrets = settings.secrets()
        self.requests = 0
        self.failed = 0
        self._sleep, self._clock = sleep, clock
        self._last_by_host: dict[str, float] = {}
        self._client = httpx.Client(timeout=settings.crawl_timeout_seconds, transport=transport,
                                    follow_redirects=True, max_redirects=3,
                                    headers={'User-Agent': settings.crawl_user_agent,
                                             # 일부 공공 API가 압축 헤더와 실제 본문이 달라 해제 오류가 난다. 압축 없이 받는다.
                                             'Accept-Encoding': 'identity'})

    def close(self):
        self._client.close()

    def mask(self, url: str) -> str:
        return mask_url(url, self.secrets)

    def preview(self, text: str, limit: int = 160) -> str:
        """오류 확인용 본문 앞부분. 인증키는 가린다."""
        return mask_url(re.sub(r'\s+', ' ', text or '')[:limit], self.secrets)

    def _wait(self, host: str):
        last = self._last_by_host.get(host)
        if last is not None:
            remaining = self.settings.crawl_delay_seconds - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_by_host[host] = self._clock()

    def get(self, url: str, *, headers: dict | None = None, params: dict | None = None,
            timeout: float | None = None) -> httpx.Response:
        self._wait(urlsplit(url).hostname or '')
        self.requests += 1
        try:
            options = {'timeout': timeout} if timeout else {}
            with self._client.stream('GET', url, headers=headers, params=params, **options) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise FetchError('RESPONSE_TOO_LARGE', '응답이 수집 한도보다 큽니다.')
                    chunks.append(chunk)
                return httpx.Response(response.status_code, headers=response.headers, content=b''.join(chunks),
                                      request=response.request)
        except FetchError:
            self.failed += 1
            raise
        except httpx.TimeoutException:
            self.failed += 1
            raise FetchError('TIMEOUT', '응답 시간이 초과됐습니다.') from None
        except httpx.DecodingError:
            self.failed += 1
            raise FetchError('DECODING_ERROR', '응답 본문 압축을 풀 수 없습니다.') from None
        except httpx.HTTPError as error:
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
            code, message = _xml_error(response.text)
            if code == 'NOT_JSON':
                message += ' 본문 앞부분: ' + self.preview(response.text)
            raise FetchError(code, message) from None


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
