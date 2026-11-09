"""KAN-155 S1-14: reply headers without a verified link must not become NEW_MESSAGE."""
from app.features.mailboxes.application.ingestion import determine_link

CASE = '33333333-3333-3333-3333-333333333333'


def test_plain_new_mail_without_candidates_is_new_message():
    assert determine_link({'in_reply_to': None, 'references': None}, []) == {'kind': 'NEW_MESSAGE', 'case_id': None}


def test_reply_headers_without_candidates_go_to_intake():
    message = {'in_reply_to': '<a@example.com>', 'references': None}
    assert determine_link(message, [])['kind'] == 'AMBIGUOUS'
    message = {'in_reply_to': None, 'references': '<a@example.com> <b@example.com>'}
    assert determine_link(message, [])['kind'] == 'AMBIGUOUS'


def test_single_verified_candidate_links_existing_case():
    message = {'in_reply_to': '<rfq-1@example.com>', 'references': None}
    rows = [{'case_id': CASE, 'same_participants': True, 'matched_reference': '<rfq-1@example.com>'}]
    assert determine_link(message, rows) == {
        'kind': 'EXISTING_CASE', 'case_id': CASE, 'matched_reference': '<rfq-1@example.com>'}


def test_unverified_candidate_is_ambiguous():
    message = {'in_reply_to': '<rfq-1@example.com>', 'references': None}
    rows = [{'case_id': CASE, 'same_participants': False, 'matched_reference': '<rfq-1@example.com>'}]
    assert determine_link(message, rows)['kind'] == 'AMBIGUOUS'


def test_sync_failure_cause_is_code_or_class_only():
    from app.core.errors import ServiceError
    from app.features.mailboxes.application.worker import failure_cause
    assert failure_cause(ServiceError('CREDENTIALS_UNAVAILABLE', '저장된 연결 정보를 확인할 수 없습니다.')) == 'CREDENTIALS_UNAVAILABLE'
    assert failure_cause(KeyError('refresh_token=secret')) == 'KeyError'
    assert failure_cause(ServiceError('bad code with spaces', 'x')) == 'UNKNOWN'
