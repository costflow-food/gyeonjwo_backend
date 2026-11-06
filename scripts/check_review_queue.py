"""Exercise KAN-150 queue dispatch without making Supabase or Gmail calls."""
import unittest
from unittest.mock import Mock, call

from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.graph_repository import GraphRuntimeRepository

START_CAPABILITIES = {
    'START', 'SCOPED_LEASES', 'DOCUMENTS', 'CANDIDATES', 'DRAFTS', 'RUNS', 'RESULTS',
    'LEGACY_RFQ_GUARD', 'CAUTION_GUARD', 'CANDIDATE_SETS', 'USER_SELECTION_GATE',
    'EXTRACTED_CONTINUATIONS',
}


class ReviewQueueTests(unittest.TestCase):
    def repository(self, *, ready, responses=None):
        repo = GraphRuntimeRepository(None)
        capabilities = START_CAPABILITIES | (repo.REVIEW_CAPABILITIES if ready else set())
        values = {'case_graph_health': {'contract_version': 1, 'ready': True,
                                       'legacy_queue_drained': True, 'capabilities': list(capabilities)},
                  'recover_case_graph_runs': 0, 'schedule_case_graph_continuations': 0,
                  'schedule_case_graph_replies': 1, 'finalize_case_graph_reviews': 0}
        values.update(responses or {})
        repo.call = Mock(side_effect=lambda name, **kwargs: values.get(name))
        return repo

    def test_start_only_when_review_capabilities_are_absent(self):
        repo = self.repository(ready=False)
        repo.recover()
        repo.call.reset_mock()
        self.assertIsNone(repo.claim())
        repo.call.assert_called_once_with('claim_case_graph_run', p_start_from='START')

    def test_review_recovery_and_fair_claim(self):
        reply, start = {'start_from': 'REPLY'}, {'start_from': 'START'}
        repo = self.repository(ready=True, responses={'claim_case_graph_review_run': reply,
                                                       'claim_case_graph_run': start})
        repo.recover()
        repo.call.assert_has_calls([call('schedule_case_graph_replies', p_limit=25),
                                    call('finalize_case_graph_reviews', p_limit=25)])
        self.assertEqual(repo.claim(), reply)
        self.assertEqual(repo.claim(), start)

    def test_invalid_scheduler_acknowledgement_preserves_start(self):
        repo = self.repository(ready=True, responses={'schedule_case_graph_replies': True})
        repo.recover()
        self.assertFalse(repo.review_ready)
        self.assertIsNone(repo.claim())

    def test_lost_review_claim_acknowledgement_does_not_claim_start_in_same_iteration(self):
        repo = self.repository(ready=True)
        repo.recover()
        repo.call.reset_mock(side_effect=True)
        repo.call.side_effect = ServiceError('PROCESSING_UNAVAILABLE', 'lost response', 503)
        with self.assertRaises(ServiceError):
            repo.claim()
        repo.call.assert_called_once_with('claim_case_graph_review_run')
        self.assertFalse(repo.review_ready)


if __name__ == '__main__':
    unittest.main()
