"""③ 보완 요청 초안: DB가 한 업체만 거절해도 역할 전체가 실패하지 않는다 (2026-11-16 블루베리 업무 4f771fe2).

산타베리·오션푸드의 11-11 회신은 연락처를 +주소로 바꾸기 전 주소에서 왔다. stage_case_graph_review_draft가 그
업체를 REVIEW_SUPPLIER_NOT_AUTHORIZED로 거절했고, 저장소가 이를 PROCESSING_UNAVAILABLE로 바꿔 올려
③이 AGENT_FAILED로 끝났다. 이제 그 업체만 rejected(NOT_AUTHORIZED)로 돌려주고 나머지 초안은 저장한다.
"""
import unittest
from unittest import mock

from app.agents import tools
from app.agents.schemas import RunContext
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository


class _RpcError(Exception):
    """postgrest APIError처럼 message·code를 가진 예외."""

    def __init__(self, message, code):
        super().__init__(message)
        self.message, self.code = message, code


class _Client:
    def __init__(self, error):
        self.error = error

    def rpc(self, name, params):
        raise self.error


class RepositoryErrorTests(unittest.TestCase):
    def test_supplier_rejection_keeps_its_code(self):
        repo = CaseRuntimeRepository(_Client(_RpcError('REVIEW_SUPPLIER_NOT_AUTHORIZED', '23514')))
        with self.assertRaises(ServiceError) as caught:
            repo.call('stage_case_graph_review_draft', p_job='j')
        self.assertEqual((caught.exception.code, caught.exception.status), ('REVIEW_SUPPLIER_NOT_AUTHORIZED', 409))

    def test_other_rpc_errors_stay_unavailable(self):
        repo = CaseRuntimeRepository(_Client(_RpcError('FOREIGN_GRAPH_EVIDENCE', '23514')))
        with self.assertRaises(ServiceError) as caught:
            repo.call('stage_case_graph_review_draft', p_job='j')
        self.assertEqual(caught.exception.code, 'PROCESSING_UNAVAILABLE')


def _supplier(sid):
    return {'supplier_id': sid, 'quote_version_id': f'q-{sid}', 'missing_required': ['tax_terms'],
            'unknown_costs': [], 'requirements': []}


class _Store:
    def __init__(self, error=None):
        self.error, self.saved = error, []

    def get_work_case(self, case_id):
        return {'item_name': '수입 블루베리', 'input_revision': 4}

    def current_send_settings(self, case_id):
        return {}

    def get_supplier(self, case_id, supplier_id):
        return {'name': supplier_id, 'contact_email': f'{supplier_id}@example.invalid'}

    def insert_mail_draft(self, record, revision):
        if record.supplier_id == 'BLOCKED':
            raise self.error
        self.saved.append(record.supplier_id)
        return f'd-{record.supplier_id}', record


class ClarificationDraftTests(unittest.TestCase):
    def run_drafts(self, store):
        compared = {'status': 'OK', 'suppliers': [_supplier('OK'), _supplier('BLOCKED')]}
        requests = [tools.ClarificationRequest(supplier_id=sid, fields=['tax_terms']) for sid in ('OK', 'BLOCKED')]
        with mock.patch.object(tools, 'compare_quotes', return_value=compared):
            return tools.save_clarification_drafts(store, RunContext(case_id='c', input_revision=4), requests)

    def test_rejected_supplier_is_skipped(self):
        store = _Store(ServiceError('REVIEW_SUPPLIER_NOT_AUTHORIZED', '거절', 409))
        result = self.run_drafts(store)
        self.assertEqual(result['status'], 'DRAFTED')
        self.assertEqual(store.saved, ['OK'])
        self.assertEqual(result['rejected'], [{'supplier_id': 'BLOCKED', 'code': 'NOT_AUTHORIZED'}])
        self.assertIn('NOT_AUTHORIZED', result['hints'])

    def test_other_errors_still_fail_the_role(self):
        with self.assertRaises(ServiceError):
            self.run_drafts(_Store(ServiceError('PROCESSING_UNAVAILABLE', '장애', 503)))


if __name__ == '__main__':
    unittest.main()
