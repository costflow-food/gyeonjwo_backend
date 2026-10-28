from decimal import Decimal

from app.shared.infrastructure.db.contracts import REFERENCE_COLUMNS

DECIMAL_COLUMNS = {'unit_price', 'quantity', 'moq', 'amount', 'coverage_quantity'}


class ReferenceRepository:
    """Read-only access. Pagination avoids the Data API default row limit."""
    def __init__(self, client):
        self.client = client

    def read(self, table: str, **equals) -> list[dict]:
        if table not in REFERENCE_COLUMNS:
            raise ValueError('Unknown reference table')
        columns = REFERENCE_COLUMNS[table]
        if equals.keys() - set(columns.split(',')):
            raise ValueError('Unknown filter column')
        rows = []
        offset = 0
        while True:
            query = self.client.table(table).select(columns)
            for key, value in equals.items():
                query = query.is_(key, 'null') if value is None else query.eq(key, value)
            # Stable primary-key order and small pages work with standard Supabase limits.
            page = query.order(columns.split(',')[0]).range(offset, offset + 99).execute().data
            for row in page:
                rows.append({key: Decimal(str(value)) if key in DECIMAL_COLUMNS and value is not None else value
                             for key, value in row.items()})
            if len(page) < 100:
                return rows
            offset += 100

    def match_supplier(self, supplier_name: str) -> str:
        rows = self.read('suppliers', supplier_name=supplier_name)
        if len(rows) != 1:
            raise ValueError('Supplier match must return exactly one row')
        return rows[0]['supplier_id']

    def match_item(self, *, item_name: str, origin: str | None, grade: str | None, processing: str) -> str:
        rows = self.read('items', item_name=item_name, origin=origin, grade=grade, processing=processing)
        if len(rows) != 1:
            raise ValueError('Item match must return exactly one row')
        return rows[0]['item_id']
