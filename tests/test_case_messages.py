"""KAN-175 API-10: owner-scoped mail timeline with reply_summary and keyset cursor."""
import pytest

from app.core.errors import ServiceError
from app.features.work_cases.application.cursor import CursorCodec
from app.features.work_cases.domain.replies import reply_summary
from app.features.work_cases.infrastructure.messages_repository import list_case_messages

OWNER = '11111111-1111-1111-1111-111111111111'
CASE = '22222222-2222-2222-2222-222222222222'
SUPPLIER = '33333333-3333-3333-3333-333333333333'


def mail(n, direction, at):
    return {'id': f'00000000-0000-0000-0000-00000000000{n}', 'direction': direction, 'subject': f's{n}',
            'sender': 'a@example.com', 'recipients_jsonb': ['b@example.com'], 'sent_or_received_at': at,
            'attachments_jsonb': [{'filename': 'q.pdf'}], 'supplier_id': SUPPLIER,
            'mail_classification': 'BUSINESS', 'body_text': 'secret body'}


class Query:
    def __init__(self, client, table):
        self.client, self.table, self.filters, self.limit_n, self.cols = client, table, [], None, None

    def select(self, cols, **_):
        self.cols = cols
        return self

    def eq(self, key, value):
        self.filters.append(('eq', key, value))
        return self

    def lte(self, key, value):
        self.filters.append(('lte', key, value))
        return self

    def or_(self, expr):
        self.filters.append(('or', expr))
        return self

    def order(self, *_, **__):
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def execute(self):
        self.client.calls.append((self.table, self.cols, self.filters))
        if self.table == 'work_cases':
            return type('R', (), {'data': self.client.cases})()
        rows = self.client.mails
        if any(f[0] == 'or' for f in self.filters):
            rows = rows[2:]
        return type('R', (), {'data': rows[:self.limit_n]})()


class Client:
    def __init__(self, cases, mails):
        self.cases, self.mails, self.calls = cases, mails, []

    def table(self, name):
        return Query(self, name)


MAILS = [mail(3, 'IN', '2026-11-09T03:00:00Z'), mail(2, 'OUT', '2026-11-09T02:00:00Z'),
         mail(1, 'IN', '2026-11-09T01:00:00Z')]


def test_lists_owned_case_mail_without_body_and_pages():
    client, codec = Client([{'id': CASE}], MAILS), CursorCodec('k')
    summaries = lambda owner, ids: {CASE: reply_summary([SUPPLIER], [SUPPLIER])}
    page = list_case_messages(client, codec, OWNER, CASE, limit=2, reply_summaries=summaries)
    assert [m['direction'] for m in page['items']] == ['IN', 'OUT']
    assert page['items'][0]['attachment_count'] == 1 and 'body_text' not in page['items'][0]
    assert page['reply_summary'] == {'requested_supplier_count': 1, 'responded_supplier_count': 1, 'reply_rate': '1'}
    assert page['next_cursor']
    assert ('eq', 'owner_user_id', OWNER) in client.calls[1][2]
    assert 'body_text' not in client.calls[1][1]
    second = list_case_messages(client, codec, OWNER, CASE, cursor=page['next_cursor'], limit=2)
    assert [m['mail_id'][-1] for m in second['items']] == ['1'] and second['next_cursor'] is None


def test_foreign_or_missing_case_is_404():
    with pytest.raises(ServiceError) as error:
        list_case_messages(Client([], MAILS), CursorCodec('k'), OWNER, CASE)
    assert error.value.code == 'RESOURCE_NOT_FOUND'


def test_unavailable_reply_facts_are_null_not_zero():
    def unavailable(owner, ids):
        raise ServiceError('PROCESSING_UNAVAILABLE', 'x', 503)
    page = list_case_messages(Client([{'id': CASE}], MAILS), CursorCodec('k'), OWNER, CASE, reply_summaries=unavailable)
    assert page['reply_summary'] is None and len(page['items']) == 3


def test_cursor_from_another_case_is_rejected():
    page = list_case_messages(Client([{'id': CASE}], MAILS), CursorCodec('k'), OWNER, CASE, limit=1)
    with pytest.raises(ServiceError) as error:
        list_case_messages(Client([{'id': SUPPLIER}], MAILS), CursorCodec('k'), OWNER, SUPPLIER,
                           cursor=page['next_cursor'])
    assert error.value.code == 'INVALID_CURSOR'
