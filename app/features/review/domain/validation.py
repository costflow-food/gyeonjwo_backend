import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.errors import ReviewRequired


def decimal_value(raw, field):
    if raw is None:
        raise ReviewRequired('MISSING_FIELD', f'{field} 값이 없습니다.', field)
    text = raw.strip()
    match = re.fullmatch(r'((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*(?:원(?:/kg|/KG|/㎏|/박스|/box)?|KRW|kg|Kg|KG|㎏|개|박스|box)?', text)
    if not match:
        raise ReviewRequired('INVALID_NUMBER', f'{field}의 숫자와 단위를 확인할 수 없습니다.', field)
    try:
        value = Decimal(match[1].replace(',', ''))
        if not value.is_finite() or value > Decimal('999999999999'):
            raise InvalidOperation
        return value
    except InvalidOperation:
        raise ReviewRequired('INVALID_NUMBER', f'{field}의 숫자 범위를 확인해 주세요.', field)


def explicit_date(raw, field):
    if raw is None:
        raise ReviewRequired('MISSING_FIELD', f'{field} 값이 없습니다.', field)
    match = re.fullmatch(r'\s*([0-9]{4})(?:-|년\s*)([0-9]{1,2})(?:-|월\s*)([0-9]{1,2})일?\s*', raw)
    try:
        if not match:
            raise ValueError()
        return date(*map(int, match.groups()))
    except ValueError:
        raise ReviewRequired('INVALID_DATE', f'{field}에 연도를 포함한 날짜가 필요합니다.', field)


def unit_name(raw):
    return {'㎏': 'kg', 'KG': 'kg', 'Kg': 'kg', '킬로그램': 'kg', '박스': 'box', 'BOX': 'box'}.get(raw, raw)


def normalize_extraction(extraction, document, sections=None):
    if extraction.item_count != 1:
        raise ReviewRequired('MULTIPLE_ITEMS', '메일 한 건에서 품목 하나를 특정할 수 없습니다.')
    fields = {field.field_name: field.raw_value for field in extraction.fields}
    required = ('supplier_name', 'item_name', 'processing', 'old_unit_price', 'new_unit_price',
                'unit', 'currency', 'effective_date', 'reason')
    for name in required:
        if fields[name] is None or not fields[name].strip():
            raise ReviewRequired('MISSING_FIELD', f'{name} 필수값이 없습니다.', name)
    # Raw values must be evidence, not generated normalized replacements.
    evidence = re.sub(r'\s+', '', document)
    for field in extraction.fields:
        field_evidence = evidence
        if sections is not None:
            location = re.sub(r'\s+', '', field.source_location)
            matched = [text for name, text in sections.items() if location.startswith(re.sub(r'\s+', '', name))]
            if len(matched) != 1:
                raise ReviewRequired('SOURCE_LOCATION_INVALID', '추출 근거가 허용된 원문 파일에 연결되지 않았습니다.', field.source_location)
            field_evidence = re.sub(r'\s+', '', matched[0])
        if field.raw_value and re.sub(r'\s+', '', field.raw_value) not in field_evidence:
            raise ReviewRequired('SOURCE_MISMATCH', '추출값을 원문에서 확인할 수 없습니다.', field.source_location)
    normalized = dict(fields)
    normalized['currency'] = 'KRW' if fields['currency'] in ('KRW', '원', '원(KRW)', '원 (KRW)', '₩') else fields['currency']
    if normalized['currency'] != 'KRW':
        raise ReviewRequired('UNSUPPORTED_CURRENCY', '원화로 확인되지 않은 통화는 비교하지 않습니다.')
    normalized['processing'] = ('FROZEN' if re.fullmatch(r'냉동(?:\(IQF\))?', fields['processing']) else
                                'FRESH' if fields['processing'] in ('생물', '생딸기', '신선') else fields['processing'])
    if fields['origin'] and (re.fullmatch(r'국산(?:\([^)]*\))?', fields['origin']) or fields['origin'] in ('대한민국', '한국')):
        normalized['origin'] = 'KR'
    if fields['grade']:
        grade = re.fullmatch(r'([A-Za-z0-9]+)등급(?:\([^)]*\))?', fields['grade'])
        normalized['grade'] = grade[1] if grade else fields['grade']
    normalized['effective_date'] = explicit_date(fields['effective_date'], 'effective_date')
    conversion = Decimal('1')
    price_unit = unit_name(fields['unit'])
    if price_unit != 'kg':
        net_raw = fields['net_kg_per_order_unit']
        if net_raw and re.search(r'(?:박스|box|개|원|KRW)\s*$', net_raw):
            raise ReviewRequired('INVALID_CONVERSION', '환산 중량의 단위는 kg이어야 합니다.')
        conversion = decimal_value(net_raw, 'net_kg_per_order_unit')
        if conversion <= 0:
            raise ReviewRequired('INVALID_CONVERSION', '주문 단위당 kg이 확인되지 않았습니다.')
    for field in ('old_unit_price', 'new_unit_price'):
        suffix = re.search(r'/\s*([^\s]+)\s*$', fields[field])
        if suffix and unit_name(suffix[1]) != price_unit:
            raise ReviewRequired('PRICE_UNIT_MISMATCH', '단가에 적힌 단위와 요청 단위가 다릅니다.', field)
        normalized[field] = decimal_value(fields[field], field) / conversion
    if fields['requested_quantity'] is not None:
        quantity_raw = fields['requested_quantity']
        suffix = re.search(r'([A-Za-z㎏]+|박스|개)\s*$', quantity_raw)
        quantity_unit = unit_name(suffix[1]) if suffix else unit_name(fields['order_unit'] or fields['unit'])
        if quantity_unit not in ('kg', price_unit):
            raise ReviewRequired('QUANTITY_UNIT_MISMATCH', '요청 수량의 단위를 환산할 수 없습니다.', 'requested_quantity')
        normalized['requested_quantity'] = decimal_value(quantity_raw, 'requested_quantity') * (Decimal('1') if quantity_unit == 'kg' else conversion)
        if normalized['requested_quantity'] <= 0:
            raise ReviewRequired('INVALID_QUANTITY', '요청 수량은 0보다 커야 합니다.')
    if fields['requested_delivery_date']:
        normalized['requested_delivery_date'] = explicit_date(fields['requested_delivery_date'], 'requested_delivery_date')
    normalized['unit'] = 'kg'
    return normalized
