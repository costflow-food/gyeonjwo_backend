"""Read-only supplier catalogue and separate reference/transaction histories."""
from datetime import datetime, timezone
from decimal import Decimal

from app.core.errors import ServiceError
from app.features.work_cases.application.cursor import iso_z

SOURCE_TYPES = ('REGISTERED', 'VERIFIED_WEB')
CERTIFICATION_STATES = {'CONFIRMED', 'UNCONFIRMED', 'NOT_MET'}
SUPPLIER_COLUMNS = ('id,name,source_type,contact_email,location,contact_jsonb,'
                    'business_registration_no,evidence_jsonb')
ITEM_COLUMNS = ('id,supplier_id,item_name,specification,unit,origin,certifications_jsonb,notes,'
                'notes_evidence_jsonb,units_per_box::text,weight_per_box::text')
PAGE_SIZE = 200
MAX_SCAN = 10000


def evidence(value):
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in ('source_label', 'checked_at', 'memo', 'source_mail_id')
            if isinstance(value.get(key), str)}


def decimal_text(value):
    """DB numeric을 소수 끝의 0 없이 문자열로 바꾼다('5200.000000' → '5200', '1234.50' → '1234.5')."""
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Invalid price or quantity')
    text = format(number.normalize(), 'f')
    return '0' if text == '-0' else text


def certifications(values):
    result = []
    for value in values or []:
        if isinstance(value, str):
            result.append({'name': value, 'status': 'UNCONFIRMED', 'valid_until': None})
        elif isinstance(value, dict) and isinstance(value.get('name'), str):
            status = value.get('status')
            result.append({'name': value['name'],
                           'status': status if isinstance(status, str) and status in CERTIFICATION_STATES else 'UNCONFIRMED',
                           'valid_until': value.get('valid_until')})
    return result


def supplier_summary(row, item_names):
    return {'supplier_id': row['id'], 'name': row['name'], 'source_type': row['source_type'],
            'contact_email': row.get('contact_email'), 'location': row.get('location'),
            'item_names': sorted(set(item_names))}


def price_record(row, date_key):
    reference = date_key == 'effective_from'
    result = {'unit_price': decimal_text(row['unit_price']), 'unit': row['unit'],
              date_key: row.get(date_key), 'quantity': decimal_text(row.get('quantity'))}
    if reference:
        result['source_type'] = 'MAIL_QUOTE'
        result['source_mail_id'] = row['source_mail_id']
        conditions = row.get('conditions_jsonb')
        if isinstance(conditions, dict) and isinstance(conditions.get('summary'), str):
            result['conditions'] = conditions['summary']
    else:
        result['source_label'] = '실제 거래'
    return result


class SupplierRepository:
    def __init__(self, client, owner):
        self.client, self.owner = client, str(owner)

    def query(self, table, columns):
        return self.client.table(table).select(columns).eq('owner_user_id', self.owner)

    def suppliers(self):
        return self.query('suppliers', SUPPLIER_COLUMNS).in_('source_type', list(SOURCE_TYPES))

    def rows(self, query):
        try:
            rows = query.execute().data
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('Invalid supplier storage response')
            return rows
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '거래처·가격 정보를 조회할 수 없습니다.', 503) from None

    def scan(self, make_query):
        rows = []
        for offset in range(0, MAX_SCAN + 1, PAGE_SIZE):
            batch = self.rows(make_query().range(offset, offset + PAGE_SIZE - 1))
            rows.extend(batch)
            if len(rows) > MAX_SCAN:
                raise ServiceError('PROCESSING_UNAVAILABLE', '거래처 조회 범위를 줄여 주세요.', 503)
            if len(batch) < PAGE_SIZE:
                return rows

    def one(self, query):
        rows = self.rows(query.limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '거래처 또는 품목을 찾을 수 없습니다.', 404)
        return rows[0]

    def list(self, codec, q, cursor, limit):
        needle = q.strip().casefold()
        scope = codec.scope(self.owner, 'suppliers', None, needle)
        snapshot, after = (codec.decode(cursor, scope, normalize_position=str) if cursor is not None
                           else (iso_z(datetime.now(timezone.utc)), None))
        suppliers = self.scan(lambda: self.suppliers().lte('created_at', snapshot).order('id'))
        names = {}
        ids = [row['id'] for row in suppliers]
        for offset in range(0, len(ids), PAGE_SIZE):
            group = ids[offset:offset + PAGE_SIZE]
            items = self.scan(lambda: self.query('supplier_items', 'id,supplier_id,item_name')
                              .in_('supplier_id', group).order('id'))
            for item in items:
                names.setdefault(item['supplier_id'], []).append(item['item_name'])
        matches = []
        for row in suppliers:
            item_names = names.get(row['id'], [])
            contact = row.get('contact_jsonb')
            fields = [row.get(key) for key in ('name', 'business_registration_no', 'contact_email',
                                               'location')] + item_names
            if isinstance(contact, dict):
                fields.append(contact.get('name'))
            if not needle or any(needle in value.casefold() for value in fields if isinstance(value, str)):
                matches.append(supplier_summary(row, item_names))

        def position(item):
            return item['name'], item['supplier_id']

        matches.sort(key=position)
        remaining = [item for item in matches if after is None or position(item) > after]
        items = remaining[:limit]
        return {'items': items, 'counts': {'total': len(matches)},
                'next_cursor': codec.encode(scope, snapshot, position(items[-1])) if len(remaining) > limit else None}

    def prices(self, table, item, date_key, *, latest=False):
        reference = table == 'price_history'
        fields = 'conditions_jsonb,source_mail_id' if reference else 'quantity::text'
        columns = f'id,unit_price::text,unit,{date_key},{fields}'

        def query():
            scoped = self.query(table, columns).eq('supplier_item_id', item['id'])
            # Reference prices are linked to the supplier through the owned item.
            if not reference:
                scoped = scoped.eq('supplier_id', item['supplier_id'])
            return (scoped.not_.is_('unit_price', 'null').not_.is_('unit', 'null')
                    .order(date_key, desc=True, nullsfirst=False).order('id', desc=True))

        rows = self.rows(query().limit(1)) if latest else self.scan(query)
        return [price_record(row, date_key) for row in rows]

    def detail(self, supplier_id):
        row = self.one(self.suppliers().eq('id', supplier_id))
        items = self.scan(lambda: self.query('supplier_items', ITEM_COLUMNS)
                          .eq('supplier_id', supplier_id).order('item_name').order('id'))
        result = supplier_summary(row, [item['item_name'] for item in items])
        contact = row.get('contact_jsonb')
        if not isinstance(contact, dict):
            contact = {}
        result.update(contact={key: contact.get(key) if isinstance(contact.get(key), str) else None
                               for key in ('name', 'phone')},
                      business_registration_no=row.get('business_registration_no'),
                      evidence=evidence(row.get('evidence_jsonb')), items=[])
        for item in items:
            quotes = self.prices('price_history', item, 'effective_from', latest=True)
            purchases = self.prices('purchase_history', item, 'transaction_date', latest=True)
            result['items'].append({
                'supplier_item_id': item['id'],
                **{key: item.get(key) for key in ('item_name', 'specification', 'unit', 'origin', 'notes')},
                'units_per_box': decimal_text(item.get('units_per_box')),
                'weight_per_box': decimal_text(item.get('weight_per_box')),
                'certifications': certifications(item.get('certifications_jsonb')),
                'notes_evidence': evidence(item.get('notes_evidence_jsonb')),
                'latest_reference_quote': quotes[0] if quotes else None,
                'last_purchase': purchases[0] if purchases else None,
            })
        return result

    def history(self, item_id):
        item = self.one(self.query('supplier_items', 'id,supplier_id').eq('id', item_id))
        self.one(self.suppliers().eq('id', item['supplier_id']))
        return {'reference_quotes': self.prices('price_history', item, 'effective_from'),
                'actual_purchases': self.prices('purchase_history', item, 'transaction_date')}
