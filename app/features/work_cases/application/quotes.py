"""API-13: latest quote version per supplier, converted from stored JSON to the frontend list shape."""
from functools import lru_cache
from app.core.errors import ServiceError
from app.features.work_cases.domain.quotes import QuoteItem, QuotesResponse, decimal_text, MAX_QUOTE_ITEMS

# Shown as the top-level tax_basis, so it is not repeated in checks.
HIDDEN_CHECKS = {'tax_basis'}


@lru_cache
def _field_order():
    """추출 필드 이름 → 순번. jsonb는 키를 길이순으로 저장하므로 화면 순서는 추출 필드 순서를 따른다.
    에이전트 모듈은 서버 시작 때 불러오지 않는다(에이전트가 없어도 API는 뜬다). 불러오지 못하면 이름순."""
    try:
        from app.agents.tools import QUOTE_FIELDS
    except Exception:
        return {}
    return {name: index for index, name in enumerate(QUOTE_FIELDS)}


def _field_key(field):
    """추출 필드 순서, 목록에 없는 필드는 뒤에 이름순."""
    order = _field_order()
    return order.get(field, len(order)), field


def _items(value):
    """{'items': [...]} 또는 목록으로 저장된 JSON 배열을 꺼낸다."""
    if isinstance(value, dict):
        value = value.get('items', [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('Invalid stored list')
    return value


def _text(value):
    """체크 값을 화면 문자열로 바꾼다. 목록은 쉼표로 잇는다."""
    if value is None:
        return None
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    return str(value)


def quote_item(row, supplier_names, incumbent_id):
    """quote_versions 한 행을 API-13 항목으로 바꾼다."""
    checks = row.get('checks_jsonb') or {}
    evidence = row.get('evidence_jsonb') or {}
    if not isinstance(checks, dict) or not isinstance(evidence, dict):
        raise ValueError('Invalid stored object')
    tax = checks.get('tax_basis')
    return QuoteItem(
        quote_id=row['id'], version_no=row['version_no'], supplier_id=row['supplier_id'],
        supplier_name=supplier_names.get(str(row['supplier_id'])) or '이름 미확인 업체',
        is_incumbent=incumbent_id is not None and str(row['supplier_id']) == incumbent_id,
        source_mail_id=row['source_mail_id'], extraction_status=row['extraction_status'],
        unit_price=decimal_text(row.get('unit_price')), unit=row.get('unit'), currency=row.get('currency') or 'KRW',
        tax_basis=_text(tax.get('value')) if isinstance(tax, dict) else None,
        available_quantity=decimal_text(row.get('available_quantity')),
        moq=({'quantity': decimal_text(row['moq_quantity']), 'unit': row.get('moq_unit')}
             if decimal_text(row.get('moq_quantity')) is not None else None),
        effective_from=row.get('effective_from'), valid_until=row.get('valid_until'),
        costs=[{'cost_type': cost['cost_type'], 'amount': decimal_text(cost.get('amount')),
                'included_in_price': cost.get('included_in_price'), 'source_text': cost.get('source_text')}
               for cost in _items(row.get('cost_terms_jsonb'))],
        checks=[{'field': field, 'state': entry.get('state') or 'STATED', 'value': _text(entry.get('value'))}
                for field, entry in sorted(checks.items(), key=lambda pair: _field_key(pair[0]))
                if field not in HIDDEN_CHECKS and isinstance(entry, dict)],
        item_notes=[note['text'] if isinstance(note, dict) else str(note)
                    for note in _items(row.get('item_notes_jsonb'))],
        evidence=[{'field': field, 'raw_value': _text(entry.get('raw_value')), 'mail_id': entry['mail_id'],
                   'location': entry.get('location')}
                  for field, entry in sorted(evidence.items(), key=lambda pair: _field_key(pair[0]))
                  if isinstance(entry, dict) and entry.get('mail_id')])


class QuoteService:
    def __init__(self, repo):
        self.repo = repo

    def get(self, owner, case_id):
        """업무의 업체별 최신 견적. 기존 업체를 먼저, 그다음 업체 이름순으로 둔다."""
        case = self.repo.case(owner, case_id)
        latest = {}
        for row in self.repo.quotes(owner, case_id):
            current = latest.get(str(row['supplier_id']))
            if current is None or row['version_no'] > current['version_no']:
                latest[str(row['supplier_id'])] = row
        if len(latest) > MAX_QUOTE_ITEMS:
            raise ServiceError('PROCESSING_UNAVAILABLE', '견적 업체 수가 조회 한도를 넘었습니다.', 503)
        names = self.repo.suppliers(owner, set(latest))
        incumbent = self.repo.initial_supplier(owner, case) or next(
            (supplier for supplier, row in latest.items()
             if case.get('initial_mail_id') and str(row['source_mail_id']) == str(case['initial_mail_id'])), None)
        try:
            items = [quote_item(row, names, incumbent) for row in latest.values()]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 견적 형식을 확인할 수 없습니다.', 503) from None
        items.sort(key=lambda item: (not item.is_incumbent, item.supplier_name, str(item.supplier_id)))
        return QuotesResponse(case_id=case['id'], items=items)
