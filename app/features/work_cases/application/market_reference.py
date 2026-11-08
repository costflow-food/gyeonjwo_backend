"""Public survey reference; never a supplier quote or a counter-offer ceiling."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from urllib.parse import unquote

import httpx

URL = 'https://apis.data.go.kr/B552845/recent/price'
SOURCE_URL = 'https://www.data.go.kr/data/15156063/openapi.do'
KST = timezone(timedelta(hours=9))
SERIES = {
    'garlic_daeseo': ('258', '03', '02', '04'),
    'frozen_blueberry_imported': ('429', '02', '01', '04'),
}
NOTE = '공공 조사 참고가격입니다. 납품가에는 물류·포장·거래 조건이 달라 가격 차이만으로 인상 적정성을 판단하지 않습니다.'


def series_for(item_name, specification):
    """Only explicit commodity/origin/variety/grade matches; no fuzzy substitution."""
    name = str(item_name or '').replace(' ', '')
    spec = str(specification or '').replace(' ', '')
    text = name + ' ' + spec
    if any(word in text for word in ('다진', '분말', '건조', '절임', '퓨레', '주스', '잼')):
        return None
    if '깐마늘' in name and all(word in text for word in ('국산', '대서', '상품')):
        if not any(word in text for word in ('수입', '남도', '중품', '햇깐마늘', '냉동')):
            return 'garlic_daeseo'
    if '블루베리' in name and all(word in text for word in ('냉동', '수입', '상품')):
        if not any(word in text for word in ('국산', '생과', '중품', '건조')):
            return 'frozen_blueberry_imported'
    return None


def number(value):
    try:
        result = Decimal(str(value).replace(',', ''))
        return result if result.is_finite() and Decimal('0.000001') <= result <= Decimal('1e12') else None
    except (InvalidOperation, ValueError):
        return None


def text_number(value):
    return format(value.quantize(Decimal('0.01')), 'f')


def reference_from_rows(rows, key, now):
    base = {'item_key': key, 'source': 'aT / 공공데이터포털', 'source_url': SOURCE_URL,
            'note': NOTE, 'retrieved_at': now.isoformat(), 'comparisons': []}
    matches = [row for row in rows if tuple(str(row.get(k)) for k in
               ('item_cd', 'vrty_cd', 'se_cd', 'grd_cd')) == SERIES[key]]
    if not matches:
        return base | {'status': 'NOT_FOUND'}
    if len(matches) != 1:
        return base | {'status': 'AMBIGUOUS'}
    row = matches[0]
    price, size = number(row.get('exmn_dd_prc')), number(row.get('unit_sz'))
    factor = {'kg': Decimal(1), 'g': Decimal('0.001')}.get(str(row.get('unit')))
    try:
        observed = datetime.strptime(str(row['exmn_ymd']), '%Y%m%d').date()
    except (KeyError, ValueError):
        return base | {'status': 'INVALID_DATA'}
    age = (now.astimezone(KST).date() - observed).days
    if price is None or size is None or factor is None or age < 0:
        return base | {'status': 'INVALID_DATA'}
    per_kg = price / (size * factor)
    if per_kg < Decimal('0.01') or per_kg > Decimal('1e12'):
        return base | {'status': 'INVALID_DATA'}
    # shortcut: seven calendar days is a visible MVP freshness rule; replace with the survey calendar if needed.
    result = base | {'status': 'OK' if age <= 7 else 'STALE', 'observed_date': observed.isoformat(),
        'age_days': age, 'freshness_limit_days': 7, 'item_name': row.get('item_nm'),
        'variety': row.get('vrty_nm'), 'market_type': row.get('se_nm'), 'grade': row.get('grd_nm'),
        'price_won': str(price), 'unit_size': str(size), 'unit': row['unit'],
        'price_won_per_kg': text_number(per_kg), 'previous_prices': {}}
    for label, field in [('day', 'dd1_bfr_prc'), ('week', 'ww1_bfr_prc'), ('month', 'mm1_bfr_prc')]:
        before = number(row.get(field))
        if before is not None:
            result['previous_prices'][label] = {
                'price_won_per_kg': text_number(before / (size * factor)),
                'change_pct': text_number((price / before - 1) * 100)}
    return result


class MarketReferenceService:
    def __init__(self, key=None, *, transport=None):
        self.key, self.transport = key, transport
        self._lock = Lock()
        self._cache = None

    def _load(self):
        with self._lock:
            if self._cache and monotonic() < self._cache[0]:
                return self._cache[1], self._cache[2]
            if not self.key:
                return None, 'NOT_CONFIGURED'
            rows, total = [], None
            try:
                deadline = monotonic() + 12
                # Use the transport directly: Client logs its complete URL at INFO,
                # including this provider's query-string credential. No redirects.
                with self.transport or httpx.HTTPTransport(retries=0) as transport:
                    for page in range(1, 21):
                        if monotonic() >= deadline:
                            raise ValueError('deadline')
                        timeout = min(3, deadline - monotonic())
                        request = httpx.Request('GET', URL, params={'serviceKey': unquote(self.key.strip()), 'returnType': 'JSON',
                            'pageNo': page, 'numOfRows': 100}, headers={'User-Agent': 'Gyeonjwo-price-reference/1.0',
                            'Accept': 'application/json'}, extensions={'timeout': dict.fromkeys(
                                ('connect', 'read', 'write', 'pool'), timeout)})
                        response = transport.handle_request(request)
                        try:
                            response.request = request
                            response.read()
                            response.raise_for_status()
                            payload = response.json()['response']
                        finally:
                            response.close()
                        if str(payload['header']['resultCode']) not in {'0', '00', '000'}:
                            raise ValueError('provider error')
                        body = payload['body']
                        count = int(body['totalCount'])
                        if count < 0 or count > 2000 or (total is not None and total != count):
                            raise ValueError('changed page total')
                        total = count
                        items = (body.get('items') or {}).get('item') or []
                        if isinstance(items, dict):
                            items = [items]
                        if not isinstance(items, list) or any(not isinstance(r, dict) for r in items):
                            raise ValueError('invalid rows')
                        rows.extend(items)
                        if len(rows) == total:
                            break
                        if not items or len(rows) > total:
                            raise ValueError('incomplete pages')
                    if len(rows) != total:
                        raise ValueError('incomplete pages')
                fetched = datetime.now(KST)
                self._cache = (monotonic() + 300, rows, fetched)
                return rows, fetched
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                # Provider exceptions can contain serviceKey; return a fixed code without logging them.
                self._cache = (monotonic() + 30, None, 'SOURCE_UNAVAILABLE')
                return None, 'SOURCE_UNAVAILABLE'

    def get(self, case, quotes=None):
        key = series_for(case.get('item_name'), case.get('specification'))
        if key is None:
            return {'status': 'UNSUPPORTED', 'note': '원산지·품종·등급이 맞는 공공 가격을 확인하지 못했습니다.'}
        rows, fetched = self._load()
        if rows is None:
            return {'status': fetched, 'item_key': key, 'source_url': SOURCE_URL, 'note': NOTE}
        result = reference_from_rows(rows, key, datetime.now(KST))
        result['retrieved_at'] = fetched.isoformat()
        if result['status'] != 'OK':
            return result
        benchmark = Decimal(result['price_won_per_kg'])
        for quote_id, quote in (quotes or {}).items():
            evidence, checks = quote.get('evidence_jsonb') or {}, quote.get('checks_jsonb') or {}
            if not isinstance(evidence, dict) or not isinstance(checks, dict):
                continue
            name, spec = evidence.get('item_name'), checks.get('specification')
            if not isinstance(name, dict) or not isinstance(spec, dict):
                continue
            name, spec = name.get('value'), spec.get('value')
            if series_for(name, spec) != key:
                continue
            price = number(quote.get('unit_price'))
            factor = {'kg': Decimal(1), 'g': Decimal('0.001')}.get(quote.get('unit'))
            if price is None or factor is None or quote.get('currency') != 'KRW':
                continue
            per_kg = price / factor
            result['comparisons'].append({'quote_id': quote_id, 'price_won_per_kg': text_number(per_kg),
                'difference_pct': text_number((per_kg / benchmark - 1) * 100)})
        return result
