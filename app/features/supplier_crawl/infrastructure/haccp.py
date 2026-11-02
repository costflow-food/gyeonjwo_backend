"""한국식품안전관리인증원 '식품 제조가공 HACCP 업체 정보 서비스'(공공데이터포털 15065475).

검색 인자가 없어 페이지 단위로 읽는다. 인허가번호(licenseno)로 품목제조보고 후보와 연결하고,
품목 유형(species) 필터로 HACCP 업체를 별도 후보로도 볼 수 있다.
"""
import json

from app.features.supplier_crawl.infrastructure.http import FetchError, Fetcher

FIELDS = {'license_no': 'licenseno', 'certification_no': 'appointno', 'name': 'company',
          'business_type': 'businessnm', 'item_type': 'species', 'sido': 'sido', 'sgg': 'sgg',
          'valid_from': 'issuedate', 'valid_until': 'issueenddate'}
OK_CODES = {'00', '0', '0000', 'OK', 'INFO-000'}  # 실제 성공 응답은 'OK'(2026-11-02 시험)
MAX_ROWS = 100  # 1000건 요청은 결과 코드 13으로 거절됐다(2026-11-02 시험).
TIMEOUT_SECONDS = 30  # 100건 응답이 10초를 넘긴 적이 있다(2026-11-02 시험). 시간 초과는 한 번 다시 시도한다.


class HaccpClient:
    def __init__(self, fetcher: Fetcher, service_key: str, url: str):
        self.fetcher, self.service_key, self.url = fetcher, service_key, url.rstrip('/')

    def page(self, page_no: int, rows: int) -> tuple[list[dict], int | None, str, list[str]]:
        # 활용가이드 표기는 retunType이지만 실제로는 returnType에만 JSON으로 답한다(2026-11-02 시험). 둘 다 보낸다.
        params = {'ServiceKey': self.service_key, 'returnType': 'json', 'retunType': 'json',
                  'pageNo': str(page_no), 'numOfRows': str(min(rows, MAX_ROWS))}
        try:
            payload = self.fetcher.get_json(self.url, params=params, timeout=TIMEOUT_SECONDS)
        except FetchError as error:
            if error.code != 'TIMEOUT':
                raise
            payload = self.fetcher.get_json(self.url, params=params, timeout=TIMEOUT_SECONDS)
        masked = self.fetcher.mask(self.url + '?pageNo=' + str(page_no) + '&numOfRows=' + str(rows))
        header, body = _header_body(payload)
        code = str(header.get('resultCode', '')).strip()
        if code and code not in OK_CODES:
            detail = str(header.get('resultMsg') or '') or self.fetcher.preview(json.dumps(payload, ensure_ascii=False))
            raise FetchError('HACCP_' + code, detail)
        rows_out = _items(body)
        total = body.get('totalCount')
        total = int(total) if str(total or '').isdigit() else None
        fields = sorted({key for row in rows_out for key in row})
        return [_pick(row) for row in rows_out], total, masked, fields


def _header_body(payload) -> tuple[dict, dict]:
    if not isinstance(payload, dict):
        raise FetchError('UNEXPECTED_SHAPE', '응답 형식이 명세와 다릅니다.')
    root = payload.get('response', payload)
    header = root.get('header') or {}
    body = root.get('body') or {}
    if not isinstance(body, dict):
        raise FetchError('UNEXPECTED_SHAPE', '응답 body를 찾을 수 없습니다.')
    return header, body


def _items(body: dict) -> list[dict]:
    items = body.get('items')
    if isinstance(items, dict):
        items = items.get('item', [])
    if isinstance(items, dict):
        items = [items]
    # 실제 응답은 items가 [{"item": {...}}, ...] 형태다(2026-11-02 시험). 가이드 예제 형태도 함께 받는다.
    rows = []
    for row in items or []:
        if isinstance(row, dict) and isinstance(row.get('item'), dict):
            rows.append(row['item'])
        elif isinstance(row, dict):
            rows.append(row)
    return rows


def _pick(row: dict) -> dict:
    return {name: (str(row[field]).strip() if row.get(field) not in (None, '') else None)
            for name, field in FIELDS.items()}
