"""Local regression checks for graph extraction rejection and hold normalization."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.errors import ServiceError
from app.features.work_cases.domain.hold import from_agent_output
from app.features.work_cases.infrastructure.graph_store import GraphStore


class GraphHoldTests(unittest.TestCase):
    def test_structured_reason_precedes_text(self):
        output = {'hold': {'code': 'SUPPLIER_MISMATCH'}, 'reason': '등록 거래처를 찾지 못함'}
        self.assertEqual(from_agent_output(output).code, 'SUPPLIER_MISMATCH')

    def test_legacy_text_and_unknown(self):
        self.assertEqual(from_agent_output({'reason': '등록 거래처를 찾지 못함'}).code,
                         'SUPPLIER_NOT_MATCHED')
        self.assertEqual(from_agent_output({'reason': '원인 불명'}).code, 'HOLD_REASON_UNAVAILABLE')

    def test_conflicting_verified_evidence(self):
        output = {'hold': {'code': 'SUPPLIER_MISMATCH'}}
        codes = {'SUPPLIER_NOT_MATCHED', 'SUPPLIER_MISMATCH'}
        self.assertEqual(from_agent_output(output, rejection_codes=codes).code, 'HOLD_REASON_UNAVAILABLE')

    def test_recorded_evidence_precedes_structured_reason(self):
        scope = SimpleNamespace(case_id='case', batch_id='batch', revision=4,
                                mail_ids=('mail',), repo=Mock(), check=Mock())
        scope.repo.call.return_value = True
        store = GraphStore(scope, {'job_id': 'job', 'claim_token': 'token'})
        store.active_extraction_run = 'run'
        store.role_runs['run'] = 'INFO_EXTRACTOR'
        issue = {'code': 'SUPPLIER_NOT_MATCHED', 'field': 'supplier_name'}
        with self.assertRaises(ServiceError):
            store.record_extraction_issue('case', ['foreign'], issue, '업체명')
        scope.repo.call.assert_not_called()
        store.record_extraction_issue('case', ['mail'], issue, '업체명')
        scope.repo.call.assert_called_once_with(
            'record_case_graph_extraction_issue', p_job='job', p_token='token', p_revision=4,
            p_run='run', p_issue={'case_id': 'case', 'batch_id': 'batch', 'input_revision': 4,
                                  'mail_id': 'mail', **issue, 'supplier_name': '업체명'})
        store.finish_agent_run('run', 'COMPLETED', {
            'next_action': 'HOLD', 'hold': {'code': 'SUPPLIER_MISMATCH'}, 'reason': 'private reason'}, None)
        output = scope.repo.call.call_args.kwargs['p_output']
        self.assertEqual(output['hold'], {
            'code': 'SUPPLIER_NOT_MATCHED', 'message': '등록된 거래처와 연결되지 않아 견적을 저장하지 못했어요.'})
        self.assertEqual(output['reason'], 'private reason')
        self.assertIsNone(store.active_extraction_run)


if __name__ == '__main__':
    unittest.main()
