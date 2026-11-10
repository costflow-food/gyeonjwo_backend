"""API-14 read model: the stored card on work_cases plus quote/supplier facts to fill names and prices. Never writes."""
from app.core.errors import ServiceError

CASE_COLUMNS = ('id,status,processing_step,input_revision,review_revision,latest_review_jsonb,decision,'
                'selected_plan_id,selected_plan_jsonb,selected_at,archived_at,accepted_at')
MAX_LOOKUP = 200


class ReviewCardRepository:
    def __init__(self, client):
        self.client = client

    def rows(self, query):
        """조회 결과 행 목록. 형식이 다르거나 조회에 실패하면 503."""
        try:
            rows = query.execute().data
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('Invalid rows')
            return rows
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '검토 카드를 조회할 수 없습니다.', 503) from None

    def case(self, owner, case_id):
        """소유자의 업무와 저장된 카드. 없거나 다른 사용자의 업무면 404."""
        rows = self.rows(self.client.table('work_cases').select(CASE_COLUMNS)
                         .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]

    def quotes(self, owner, case_id, quote_ids):
        """카드가 가리키는 견적 ID → {supplier_id, unit, unit_price}. 같은 업무의 견적만."""
        if not quote_ids:
            return {}
        if len(quote_ids) > MAX_LOOKUP:
            raise ServiceError('PROCESSING_UNAVAILABLE', '카드의 견적 참조 범위를 확인할 수 없습니다.', 503)
        rows = self.rows(self.client.table('quote_versions').select('id,supplier_id,unit,unit_price')
                         .eq('owner_user_id', str(owner)).eq('work_case_id', str(case_id))
                         .in_('id', sorted(quote_ids)))
        return {str(row['id']): row for row in rows}

    def suppliers(self, owner, supplier_ids):
        """업체 ID → 업체 이름."""
        if not supplier_ids:
            return {}
        if len(supplier_ids) > MAX_LOOKUP:
            raise ServiceError('PROCESSING_UNAVAILABLE', '카드의 업체 참조 범위를 확인할 수 없습니다.', 503)
        rows = self.rows(self.client.table('suppliers').select('id,name')
                         .eq('owner_user_id', str(owner)).in_('id', sorted(supplier_ids)))
        return {str(row['id']): row.get('name') for row in rows}
