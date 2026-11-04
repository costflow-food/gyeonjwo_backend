"""Read-only adapter for the deployed KAN-109 three-argument RPC."""
from app.core.errors import ServiceError


class ContactCandidateRepository:
    def __init__(self, client):
        self.client = client

    def case_revision(self, owner, case_id):
        try:
            rows = (self.client.table('work_cases').select('id,input_revision')
                    .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1).execute().data)
            if (not isinstance(rows, list) or len(rows) > 1
                    or any(not isinstance(row, dict) or str(row.get('id')) != str(case_id)
                           or type(row.get('input_revision')) is not int or row['input_revision'] < 1 for row in rows)):
                raise ValueError('Invalid case revision')
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 정보를 조회할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]['input_revision']

    def list(self, owner, case_id, revision):
        try:
            result = self.client.rpc('list_web_contact_candidates', {
                'p_owner': str(owner), 'p_case': str(case_id), 'p_expected_revision': revision}).execute().data
        except Exception as error:
            code, message = getattr(error, 'code', None), getattr(error, 'message', None)
            if code == 'P0002' and message == 'RESOURCE_NOT_FOUND':
                raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404) from None
            if code == '40001' and message == 'REVISION_CONFLICT':
                raise ServiceError('REVISION_CONFLICT', '업무 입력이 바뀌었습니다. 최신 내용을 확인해 주세요.',
                                   409, retryable=False) from None
            if code in {'42501', 'PGRST202'}:
                raise ServiceError('CONTACT_CANDIDATES_INTEGRATION_REQUIRED', '연락처 후보 조회 설정을 확인해야 합니다.',
                                   503, retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
            # Never forward provider message/details/hint or stored contact data.
            raise ServiceError('PROCESSING_UNAVAILABLE', '연락처 후보를 조회할 수 없습니다.', 503) from None
        if not isinstance(result, list):
            raise ServiceError('PROCESSING_UNAVAILABLE', '연락처 후보 조회 결과를 확인할 수 없습니다.', 503)
        return result
