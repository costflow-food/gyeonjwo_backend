"""Local API-28 repository checks using an isolated PostgREST test double."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

from app.core.errors import ServiceError
from app.features.workflow_logs.repository import list_workflow_logs
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


if __name__ == '__main__':
    unittest.main()
