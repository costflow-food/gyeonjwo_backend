"""Local checks for retrying optional workers after a failed boot-time DB contract (KAN-195)."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.features.system.application import workers


def state(**overrides):
    values = {'connected_mailbox_worker': MagicMock(), 'mailbox_service': MagicMock(),
              'rfq_metadata_worker': None, 'rfq_workflow_worker': None,
              'extraction_worker': None, 'graph_startup_code': 'GRAPH_RUNTIME_UNAVAILABLE'}
    values.update(overrides)
    return SimpleNamespace(**values)


class CreateMissingRfqWorkersTests(unittest.TestCase):
    def test_keeps_none_while_schema_is_unavailable(self):
        current = state()
        with patch.object(workers, 'start_metadata_worker', return_value=None), \
                patch.object(workers, 'start_workflow_worker', return_value=None):
            self.assertEqual(workers.create_missing_rfq_workers(current, object()), [])
        self.assertIsNone(current.rfq_metadata_worker)
        self.assertIsNone(current.rfq_workflow_worker)

    def test_creates_only_missing_workers(self):
        existing, created = MagicMock(), MagicMock()
        current = state(rfq_metadata_worker=existing)
        with patch.object(workers, 'start_metadata_worker') as metadata, \
                patch.object(workers, 'start_workflow_worker', return_value=created) as workflow:
            result = workers.create_missing_rfq_workers(current, 'settings')
        metadata.assert_not_called()
        workflow.assert_called_once_with('settings', current.mailbox_service)
        self.assertEqual(result, [created])
        self.assertIs(current.rfq_metadata_worker, existing)
        self.assertIs(current.rfq_workflow_worker, created)

    def test_skips_rfq_workers_without_mailbox_worker(self):
        current = state(connected_mailbox_worker=None)
        with patch.object(workers, 'start_metadata_worker') as metadata, \
                patch.object(workers, 'start_workflow_worker') as workflow:
            self.assertEqual(workers.create_missing_rfq_workers(current, object()), [])
        metadata.assert_not_called()
        workflow.assert_not_called()


class ReviveWorkersTests(unittest.TestCase):
    def test_starts_recovered_workers_and_leaves_running_ones(self):
        running_graph, metadata, workflow = MagicMock(), MagicMock(), MagicMock()
        current = state(extraction_worker=running_graph)
        with patch.object(workers, 'start_metadata_worker', return_value=metadata), \
                patch.object(workers, 'start_workflow_worker', return_value=workflow), \
                patch.object(workers, 'start_graph_worker') as graph:
            workers.revive_workers(current, object())
        metadata.start.assert_called_once_with()
        workflow.start.assert_called_once_with()
        graph.assert_not_called()
        running_graph.start.assert_not_called()

    def test_retries_graph_worker_when_missing(self):
        current = state(rfq_metadata_worker=MagicMock(), rfq_workflow_worker=MagicMock())
        with patch.object(workers, 'start_graph_worker', return_value=None) as graph:
            workers.revive_workers(current, 'settings')
        graph.assert_called_once_with(current, 'settings')
        current.rfq_metadata_worker.start.assert_not_called()


class StartGraphWorkerTests(unittest.TestCase):
    def test_unavailable_contract_leaves_worker_off_with_reason(self):
        current = state(graph_startup_code=None)
        repo = MagicMock()
        repo.available.return_value = False
        with patch('app.features.work_cases.application.start_readiness.configuration_readiness'), \
                patch('app.features.work_cases.application.start_readiness.agent_readiness'), \
                patch('app.features.work_cases.infrastructure.graph_repository.runtime_repository',
                      return_value=repo), \
                patch.object(workers, 'make_server_client'):
            self.assertIsNone(workers.start_graph_worker(current, MagicMock()))
        self.assertIsNone(current.extraction_worker)
        self.assertEqual(current.graph_startup_code, 'GRAPH_RUNTIME_UNAVAILABLE')

    def test_available_contract_starts_worker_and_clears_reason(self):
        current = state()
        repo, worker = MagicMock(), MagicMock()
        repo.available.return_value = True
        with patch('app.features.work_cases.application.start_readiness.configuration_readiness'), \
                patch('app.features.work_cases.application.start_readiness.agent_readiness'), \
                patch('app.features.work_cases.infrastructure.graph_repository.runtime_repository',
                      return_value=repo), \
                patch('app.features.work_cases.application.graph_worker.GraphWorker', return_value=worker), \
                patch.object(workers, 'make_server_client'):
            self.assertIs(workers.start_graph_worker(current, MagicMock()), worker)
        worker.start.assert_called_once_with()
        self.assertIs(current.extraction_worker, worker)
        self.assertIsNone(current.graph_startup_code)


class RetryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, current, ticks=3):
        with patch.object(workers, 'revive_workers') as revive:
            task = asyncio.create_task(workers.retry_unavailable_workers(current, 'settings', interval=0.01))
            await asyncio.sleep(0.01 * ticks + 0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        return revive

    async def test_retries_while_a_worker_is_missing(self):
        current = state()
        revive = await self.run_loop(current)
        self.assertGreaterEqual(revive.call_count, 1)
        revive.assert_called_with(current, 'settings')

    async def test_idle_when_every_worker_is_up(self):
        current = state(rfq_metadata_worker=MagicMock(), rfq_workflow_worker=MagicMock(),
                        extraction_worker=MagicMock())
        revive = await self.run_loop(current)
        revive.assert_not_called()

    async def test_keeps_retrying_after_a_failed_attempt(self):
        current = state()
        with patch.object(workers, 'revive_workers', side_effect=RuntimeError('db down')) as revive:
            task = asyncio.create_task(workers.retry_unavailable_workers(current, 'settings', interval=0.01))
            await asyncio.sleep(0.12)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(revive.call_count, 2)


if __name__ == '__main__':
    unittest.main()
