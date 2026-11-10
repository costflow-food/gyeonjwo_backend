"""Local API-28 repository checks using an isolated PostgREST test double."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

from app.core.errors import ServiceError
from app.features.workflow_logs.repository import list_workflow_logs, public_log
from app.features.work_cases.application.cursor import CursorCodec

CASE = str(UUID(int=5))
TIME = '2026-11-05T00:00:00Z'


def row(number):
    return {'id': str(UUID(int=number)), 'created_at': TIME, 'event_type': 'REVIEW_CREATED',
            'work_case_id': CASE, 'payload_jsonb': {'agent_role': 'INFO_EXTRACTOR',
                                                  'reason': 'private', 'body_text': 'private'}}


class WorkflowLogTests(unittest.TestCase):
    def setup_queries(self, rows):
        client = MagicMock()
        count, page, titles = MagicMock(), MagicMock(), MagicMock()
        count.select.return_value.eq.return_value.lte.return_value.eq.return_value.execute.return_value.count = 2
        query = page.select.return_value.eq.return_value.lte.return_value.eq.return_value
        query.order.return_value.order.return_value.limit.return_value.execute.return_value.data = rows
        query.or_.return_value = query
        titles.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
            {'id': CASE, 'item_name': '딸기'}]
        client.table.side_effect = [count, page, titles]
        return client, count, page, query, titles

    def test_case_filter_count_and_keyset_pages(self):
        codec = CursorCodec('test')
        filters = SimpleNamespace(case_id=UUID(CASE), cursor=None, limit=1)
        client, count, page, query, titles = self.setup_queries([row(2), row(1)])
        first = list_workflow_logs(client, codec, 'owner', filters)
        self.assertEqual(first['counts'], {'total': 2})
        self.assertEqual(first['items'][0]['case_title'], '딸기')
        self.assertNotIn('reason', first['items'][0])
        self.assertNotIn('body_text', first['items'][0])
        for table in (count, page, titles):
            table.select.return_value.eq.assert_called_once_with('owner_user_id', 'owner')
        for table in (count, page):
            table.select.return_value.eq.return_value.lte.return_value.eq.assert_called_once_with(
                'work_case_id', CASE)
        query.order.assert_called_once_with('created_at', desc=True)
        query.order.return_value.order.assert_called_once_with('id', desc=True)
        filters.cursor = first['next_cursor']
        client, _, _, query, _ = self.setup_queries([row(1)])
        second = list_workflow_logs(client, codec, 'owner', filters)
        self.assertEqual(second['items'][0]['id'], str(UUID(int=1)))
        self.assertEqual(second['counts'], first['counts'])
        self.assertIsNone(second['next_cursor'])
        self.assertIn(f'id.lt.{UUID(int=2)}', query.or_.call_args.args[0])
        for owner, case in [('other', UUID(CASE)), ('owner', UUID(int=6))]:
            filters.case_id = case
            with self.assertRaises(ServiceError) as error:
                list_workflow_logs(MagicMock(), codec, owner, filters)
            self.assertEqual(error.exception.code, 'INVALID_CURSOR')


class GraphLogActorTests(unittest.TestCase):
    """실DB의 그래프 실행 로그 모양(actor_type 컬럼, payload의 role·status)을 응답 계약으로 옮긴다."""

    def log(self, event_type, actor_type, payload):
        return public_log({'id': str(UUID(int=9)), 'created_at': TIME, 'event_type': event_type,
                           'work_case_id': CASE, 'actor_type': actor_type, 'payload_jsonb': payload}, {})

    def test_agent_role_log_shows_role_and_result(self):
        item = self.log('GRAPH_ROLE_FINISHED', 'AGENT',
                        {'role': 'INFO_EXTRACTOR', 'status': 'COMPLETED', 'hold': {'code': 'private'}})
        self.assertEqual(item['component_type'], 'AGENT')
        self.assertEqual(item['agent_role'], 'INFO_EXTRACTOR')
        self.assertEqual(item['run_status'], 'COMPLETED')
        self.assertEqual(item['outcome'], 'SUCCESS')
        self.assertNotIn('hold', item)
        self.assertNotIn('role', item)
        self.assertNotIn('status', item)

    def test_started_role_has_no_outcome_yet(self):
        item = self.log('GRAPH_ROLE_STARTED', 'AGENT', {'role': 'CANDIDATE', 'review_batch_id': 'private'})
        self.assertEqual(item['agent_role'], 'CANDIDATE')
        self.assertNotIn('outcome', item)
        self.assertNotIn('run_status', item)
        self.assertNotIn('review_batch_id', item)

    def test_hold_and_failure_map_to_waiting_and_failure(self):
        hold = self.log('GRAPH_ROLE_FINISHED', 'AGENT', {'role': 'CANDIDATE', 'status': 'HOLD'})
        failed = self.log('GRAPH_ROLE_FINISHED', 'AGENT', {'role': 'CANDIDATE', 'status': 'FAILED'})
        graph_failed = self.log('GRAPH_FAILED', 'SYSTEM', {'error_code': 'GRAPH_RUNTIME_ERROR'})
        self.assertEqual(hold['outcome'], 'WAITING')
        self.assertEqual(failed['outcome'], 'FAILURE')
        self.assertEqual(graph_failed['outcome'], 'FAILURE')
        self.assertEqual(graph_failed['component_type'], 'SYSTEM')

    def test_user_and_system_logs_show_the_actor_kind(self):
        user = self.log('REVIEW_REQUESTED', 'USER', {'previous_status': 'Waiting'})
        system = self.log('GRAPH_COMPLETED', 'SYSTEM', {'status': 'WAITING_REPLY', 'job_id': 'private'})
        self.assertEqual(user['component_type'], 'USER')
        self.assertNotIn('agent_role', user)
        self.assertEqual(system['component_type'], 'SYSTEM')
        self.assertEqual(system['run_status'], 'WAITING_REPLY')
        self.assertNotIn('outcome', system)

    def test_explicit_payload_fields_win_and_unknown_actor_is_dropped(self):
        item = self.log('AGENT_RUN', 'ROBOT', {'component_type': 'NODE', 'agent_role': 'CODE_VALIDATOR',
                                               'role': 'IGNORED', 'run_status': 'DONE', 'status': 'IGNORED',
                                               'outcome': 'SUCCESS'})
        self.assertEqual(item['component_type'], 'NODE')
        self.assertEqual(item['agent_role'], 'CODE_VALIDATOR')
        self.assertEqual(item['run_status'], 'DONE')
        self.assertEqual(item['outcome'], 'SUCCESS')
        bare = self.log('GROUP_LINKED', 'ROBOT', None)
        self.assertNotIn('component_type', bare)


if __name__ == '__main__':
    unittest.main()
