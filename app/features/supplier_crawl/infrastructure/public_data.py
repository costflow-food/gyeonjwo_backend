"""식품안전나라 Open API: 품목제조보고(I1250)로 품목을 만드는 업소를 찾고, 식품제조가공업(I1220)으로 연락처를 보강한다.

요청 형식: /api/{인증키}/{서비스ID}/json/{시작}/{끝}/{검색인자}={값}
필드 이름은 공개 명세 기준이며 첫 시험 수집에서 실제 응답과 대조한다(어긋나면 리포트 issues에 남음).
"""
from urllib.parse import quote

from app.features.supplier_crawl.infrastructure.http import FetchError, Fetcher

BASE_URL = 'https://openapi.foodsafetykorea.go.kr/api'
ITEM_REPORT_SERVICE = 'I1250'   # 식품(첨가물)품목제조보고
BUSINESS_SERVICE = 'I1220'      # 식품제조가공업 인허가
MAX_ROWS_PER_CALL = 1000
OK_CODES = {'INFO-000'}
EMPTY_CODES = {'INFO-200'}

# 업체 단위 항목만 읽는다. 대표자명 등 개인 이름 필드는 읽지 않는다.
ITEM_FIELDS = {'license_no': 'LCNS_NO', 'name': 'BSSH_NM', 'report_no': 'PRDLST_REPORT_NO',
               'item_name': 'PRDLST_NM', 'item_type': 'PRDLST_DCNM', 'reported_on': 'PRMS_DT',
               'production_ended': 'PRODUCTION'}
# I1220 실제 응답(공개 샘플 2026-11-02 확인): 소재지는 LOCP_ADDR, 업종은 INDUTY_NM. TELNO는 빈 값이 많다.
BUSINESS_FIELDS = {'license_no': 'LCNS_NO', 'name': 'BSSH_NM', 'phone': 'TELNO', 'address': 'LOCP_ADDR',
                   'business_type': 'INDUTY_NM'}


class PublicDataClient:
    def __init__(self, fetcher: Fetcher, api_key: str):
        self.fetcher, self.api_key = fetcher, api_key

    def _url(self, service: str, start: int, end: int, param: str, value: str) -> str:
        return f'{BASE_URL}/{quote(self.api_key, safe="")}/{service}/json/{start}/{end}/{param}={quote(value, safe="")}'

    def _rows(self, service: str, url: str) -> tuple[list[dict], list[str]]:
        payload = self.fetcher.get_json(url)
        body = payload.get(service) if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            result = payload.get('RESULT', {}) if isinstance(payload, dict) else {}
            raise FetchError(result.get('CODE') or 'UNEXPECTED_SHAPE', result.get('MSG') or '응답 형식이 명세와 다릅니다.')
        code = (body.get('RESULT') or {}).get('CODE')
        if code in EMPTY_CODES:
            return [], []
        if code not in OK_CODES:
            raise FetchError(code or 'UNKNOWN_RESULT', (body.get('RESULT') or {}).get('MSG') or '결과 코드를 확인할 수 없습니다.')
        rows = [row for row in body.get('row') or [] if isinstance(row, dict)]
        return rows, sorted({key for row in rows for key in row})

    def item_reports(self, keyword: str, limit: int) -> tuple[list[dict], str, list[str]]:
        """품목명에 keyword가 든 품목제조보고. (매핑한 행, 마스킹한 출처 URL, 응답 필드 이름)"""
        url = self._url(ITEM_REPORT_SERVICE, 1, min(limit, MAX_ROWS_PER_CALL), 'PRDLST_NM', keyword)
        rows, fields = self._rows(ITEM_REPORT_SERVICE, url)
        return [_pick(row, ITEM_FIELDS) for row in rows], self.fetcher.mask(url), fields

    def business(self, license_no: str) -> tuple[dict | None, str, list[str]]:
        url = self._url(BUSINESS_SERVICE, 1, 5, 'LCNS_NO', license_no)
        rows, fields = self._rows(BUSINESS_SERVICE, url)
        exact = [row for row in rows if str(row.get('LCNS_NO', '')).strip() == license_no]
        return (_pick(exact[0], BUSINESS_FIELDS) if exact else None), self.fetcher.mask(url), fields


def _pick(row: dict, mapping: dict) -> dict:
    picked = {}
    for name, field in mapping.items():
        value = row.get(field)
        picked[name] = str(value).strip() if value not in (None, '') else None
    return picked


def missing_fields(seen: list[str], mapping: dict) -> list[str]:
    """명세상 필드 중 실제 응답에 없는 이름. 첫 시험에서 필드명 확인용."""
    return [field for field in mapping.values() if seen and field not in seen]
