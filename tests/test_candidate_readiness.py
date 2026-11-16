"""API-01/02 candidate readiness hints (SELECT_CANDIDATES).

Deployed main built a new CandidateRepository for every API-02 call, so its
readiness cache was always empty and the detail answered "확인 중" on every
request while the list allowed selection (queue label flipped between
"업체 선택 대기" and "견적 요청 준비 중").
"""
from threading import Event, Lock
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from app.features.work_cases.api import candidates as candidates_api
from app.features.work_cases.api import router as case_router
from app.features.work_cases.infrastructure import candidate_repository
from app.features.work_cases.infrastructure.candidate_repository import (
    CHECKING_MESSAGE, CandidateRepository)
from app.shared.infrastructure.db.health_probe import HealthProbe

HEALTHY = {'ready': True}
CLOCK = 'app.shared.infrastructure.db.health_probe.monotonic'


def graph_ready(result):
    return isinstance(result, dict) and result.get('ready') is True


def sequenced_probe(check, healthy):
    """Each refresh takes the next side effect; a callable one runs (and may block)."""
    def refresh():
        value = check()
        return value() if callable(value) else value
    return HealthProbe(refresh, healthy)


def candidates_row(case_id):
    return {'id': str(case_id), 'initial_mail_id': str(uuid4()), 'status': 'Running',
            'processing_step': 'CANDIDATES', 'input_revision': 1, 'item_name': '냉동 딸기',
            'specification': None, 'quantity': '800', 'unit': 'kg', 'decision': None,
            'archived_at': None, 'current_batch_id': str(uuid4())}


class CaseDetailUsesSharedCandidateServiceTests(TestCase):
    def call_detail(self, case_id, request):
        repo = Mock()
        repo.related_case_ids.return_value = []
        principal = SimpleNamespace(user_id=uuid4())
        with patch.object(case_router, 'load_action_case', side_effect=lambda *_: candidates_row(case_id)), \
                patch.object(case_router, 'case_actions', return_value=(['VIEW_ORIGINAL'], [])), \
                patch.object(case_router, 'extraction_readiness', return_value=None), \
                patch.object(case_router, 'resume_readiness', return_value=None), \
                patch.object(case_router, 'case_changes_ready', return_value=True), \
                patch.object(case_router, 'case_hold', return_value=None), \
                patch.object(case_router, 'api_json', side_effect=lambda body, **_: body):
            return case_router.get_case(case_id, request, principal, repo)

    def test_detail_reads_readiness_from_the_shared_service(self):
        case_id = uuid4()
        shared = Mock()
        shared.repo.blocks.return_value = {}
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        fresh = Mock(side_effect=AssertionError('API-02 must not build a per-request CandidateRepository'))
        with patch.object(candidates_api, 'get_candidate_service', return_value=shared) as service, \
                patch.object(candidate_repository, 'CandidateRepository', fresh):
            first = self.call_detail(case_id, request)
            second = self.call_detail(case_id, request)
        self.assertEqual(service.call_count, 2)
        self.assertEqual(shared.repo.blocks.call_count, 2)
        for body in (first, second):
            self.assertIn('SELECT_CANDIDATES', body['allowed_actions'])
            self.assertNotIn('SELECT_CANDIDATES', [d['action'] for d in body['disabled_actions']])

    def test_candidate_service_is_built_once_per_app(self):
        state = SimpleNamespace(service_lock=Lock(), settings=object(),
                                case_runtime_repository=SimpleNamespace(read_health=Mock()))
        with patch.object(candidates_api, 'make_server_client', return_value=Mock()) as client:
            first = candidates_api.get_case_candidate_service(state)
            second = candidates_api.get_case_candidate_service(state)
        self.assertIs(first, second)
        client.assert_called_once()


class ListingReadinessTests(TestCase):
    def setUp(self):
        self.gate = Event()
        self.addCleanup(self.gate.set)

    def slow(self, value):
        def check():
            if not self.gate.wait(5):
                raise RuntimeError('Test did not release the probe')
            return value
        return check

    def test_slow_refresh_keeps_last_healthy_hint_but_selection_waits(self):
        check = Mock(side_effect=[HEALTHY, self.slow({'ready': False})])
        probe = sequenced_probe(check, graph_ready)
        repo = CandidateRepository(None, graph_health=probe.read)
        with patch(CLOCK, return_value=100) as clock:
            self.assertTrue(repo.ready().available)            # cached until 160
            clock.return_value = 170                            # expired, refresh is slow
            for _ in range(5):
                self.assertTrue(repo.listing_readiness().available)
            self.gate.set()
            self.assertFalse(repo.ready().available)            # strict path sees the fresh failure
            self.assertFalse(repo.listing_readiness().available)  # failures are never served stale
        self.assertEqual(check.call_count, 2)

    def test_unknown_readiness_says_checking_not_disconnected(self):
        probe = HealthProbe(self.slow(HEALTHY), graph_ready)
        readiness = CandidateRepository(None, graph_health=probe.read).listing_readiness()
        self.assertFalse(readiness.available)
        self.assertEqual(readiness.message, CHECKING_MESSAGE)

    def test_stale_hint_expires(self):
        check = Mock(side_effect=[HEALTHY, self.slow(HEALTHY)])
        probe = sequenced_probe(check, graph_ready)
        repo = CandidateRepository(None, graph_health=probe.read)
        with patch(CLOCK, return_value=100) as clock:
            self.assertTrue(repo.ready().available)
            clock.return_value = 100 + 60 + candidate_repository.LISTING_STALE_SECONDS + 1
            self.assertEqual(repo.listing_readiness().message, CHECKING_MESSAGE)

    def test_default_read_is_unchanged(self):
        check = Mock(side_effect=[True, self.slow(True)])
        probe = sequenced_probe(check, bool)
        with patch(CLOCK, return_value=100) as clock:
            self.assertIs(probe.read(timeout=1), True)
            clock.return_value = 170
            self.assertIsNone(probe.read(timeout=0.01))
