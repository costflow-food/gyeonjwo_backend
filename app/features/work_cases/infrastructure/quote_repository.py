"""API-13 read model: owner-scoped quote versions with supplier names. Never writes."""
from app.core.errors import ServiceError

QUOTE_COLUMNS = ('id,version_no,supplier_id,source_mail_id,extraction_status,unit_price,unit,currency,'
                 'available_quantity,moq_quantity,moq_unit,effective_from,valid_until,'
                 'checks_jsonb,cost_terms_jsonb,item_notes_jsonb,evidence_jsonb')
MAX_QUOTE_ROWS = 500


class QuoteRepository:
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
            raise ServiceError('PROCESSING_UNAVAILABLE', '견적 정보를 조회할 수 없습니다.', 503) from None

    def case(self, owner, case_id):
        """소유자의 업무 한 건. 없거나 다른 사용자의 업무면 404."""
        rows = self.rows(self.client.table('work_cases').select('id,initial_mail_id')
                         .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]

    def quotes(self, owner, case_id):
        """업무의 견적 버전 전체. 상한을 넘으면 잘린 목록 대신 503."""
        rows = self.rows(self.client.table('quote_versions').select(QUOTE_COLUMNS)
                         .eq('owner_user_id', str(owner)).eq('work_case_id', str(case_id))
                         .order('supplier_id').order('version_no', desc=True).limit(MAX_QUOTE_ROWS + 1))
        if len(rows) > MAX_QUOTE_ROWS:
            raise ServiceError('PROCESSING_UNAVAILABLE', '견적 조회 범위를 좁혀야 합니다.', 503)
        return rows

    def suppliers(self, owner, supplier_ids):
        """업체 ID → 업체 이름."""
        if not supplier_ids:
            return {}
        rows = self.rows(self.client.table('suppliers').select('id,name')
                         .eq('owner_user_id', str(owner)).in_('id', sorted(supplier_ids)))
        return {str(row['id']): row.get('name') for row in rows}

    def initial_supplier(self, owner, case):
        """첫 메일(인상 통보)을 보낸 업체 ID. 아직 업체가 연결되지 않았으면 None."""
        if not case.get('initial_mail_id'):
            return None
        rows = self.rows(self.client.table('mail_messages').select('supplier_id')
                         .eq('owner_user_id', str(owner)).eq('id', str(case['initial_mail_id'])).limit(1))
        return str(rows[0]['supplier_id']) if rows and rows[0].get('supplier_id') else None
