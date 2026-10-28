"""Future work_cases guards, exercised without inventing a deployed DB contract."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import ServiceError
from app.core.idempotency import request_identity, require_same_body, request_in_progress
from app.features.work_cases.domain.contracts import (
    ActionAvailability, CaseSnapshot, PlanSnapshot, RevisionRequest,
    require_first_start, require_plan_selection, require_revision,
)


def case(**changes):
    return CaseSnapshot.model_validate(dict(case_id=uuid4(), status='Waiting', processing_step='PENDING_START',
        input_revision=5, review_revision=None, decision=None, archived=False) | changes)


def test_uuid_and_exact_status_are_required():
    for changes in ({'case_id': 'W-20261026-001'}, {'status': 'WAITING'}):
        with pytest.raises(ValidationError):
            case(**changes)


@pytest.mark.parametrize('revision', [True, '5', 1.5, -1])
def test_revision_is_a_nonnegative_json_integer(revision):
    with pytest.raises(ValidationError):
        RevisionRequest(expected_revision=revision)


def test_revision_conflict_returns_current_revision():
    with pytest.raises(ServiceError) as caught:
        require_revision(4, 5)
    assert (caught.value.code, caught.value.status, caught.value.current_revision) == ('REVISION_CONFLICT', 409, 5)


def test_first_start_does_not_mutate_snapshot():
    snapshot = case()
    before = snapshot.model_dump()
    require_first_start(snapshot, 5)
    assert snapshot.model_dump() == before


@pytest.mark.parametrize('changes,code', [
    ({'archived': True}, 'ACTION_NOT_ALLOWED'),
    ({'decision': 'ACCEPT_INCREASE'}, 'ACTION_NOT_ALLOWED'),
    ({'status': 'Running', 'processing_step': 'EXTRACTING'}, 'WORK_ALREADY_STARTED'),
])
def test_start_rejects_archive_acceptance_and_repeat(changes, code):
    with pytest.raises(ServiceError, match=code):
        require_first_start(case(**changes), 5)


@pytest.mark.parametrize('step', ['FAILED', 'WAITING_REPLY'])
def test_failure_and_reply_wait_cannot_be_complete(step):
    assert case(status='Running', processing_step=step).status == 'Running'
    with pytest.raises(ValidationError):
        case(status='Complete', processing_step=step)


def test_hold_before_and_after_card_remain_distinct():
    assert case(status='Running', processing_step='ON_HOLD', decision='HOLD').status == 'Running'
    assert case(status='Complete', processing_step='REVIEW_READY', review_revision=2, decision='HOLD').status == 'Complete'


def test_ready_card_requires_stored_version():
    with pytest.raises(ValidationError):
        case(status='Complete', processing_step='REVIEW_READY')


def selection():
    snapshot = case(status='Complete', processing_step='REVIEW_READY', review_revision=2)
    plan = PlanSnapshot(case_id=snapshot.case_id, plan_id=uuid4(), input_revision=5,
                        review_revision=2, validation_status='VALID')
    return snapshot, plan


def test_card_version_and_input_revision_are_not_the_same_counter():
    snapshot, plan = selection()
    require_plan_selection(snapshot, plan, 5)


@pytest.mark.parametrize('changes', [
    {'case_id': uuid4()}, {'input_revision': 4}, {'review_revision': 1},
    {'validation_status': 'CONDITIONAL'}, {'validation_status': 'INVALID'}, {'validation_status': 'NEEDS_INFO'},
])
def test_foreign_stale_and_nonvalid_plans_cannot_be_selected(changes):
    snapshot, plan = selection()
    changed = PlanSnapshot.model_validate(plan.model_dump() | changes)
    with pytest.raises(ServiceError, match='INVALID_PLAN_SELECTION'):
        require_plan_selection(snapshot, changed, 5)


def test_archived_plan_cannot_be_selected():
    snapshot, plan = selection()
    snapshot = CaseSnapshot.model_validate(snapshot.model_dump() | {'archived': True})
    with pytest.raises(ServiceError, match='ACTION_NOT_ALLOWED'):
        require_plan_selection(snapshot, plan, 5)


def test_action_arrays_cannot_contradict_each_other():
    with pytest.raises(ValidationError):
        ActionAvailability(allowed_actions=['START'], disabled_actions=[
            {'action': 'START', 'reason_code': 'ARCHIVED', 'message': '보관된 업무'}])


def identity(body, **changes):
    return request_identity(**(dict(user_id='member-a', method='POST', path='/api/v1/work-cases/id/start',
                                   key='intent-a', body=body) | changes))


def test_idempotency_hash_ignores_object_order_but_preserves_values_and_types():
    first = identity({'expected_revision': 5, 'note': '견줘'})
    same = identity({'note': '견줘', 'expected_revision': 5})
    assert first == same
    require_same_body(same, first.body_hash)
    for body in ({'expected_revision': '5', 'note': '견줘'}, {'expected_revision': 6, 'note': '견줘'}):
        with pytest.raises(ServiceError, match='IDEMPOTENCY_KEY_REUSED'):
            require_same_body(identity(body), first.body_hash)


def test_idempotency_scope_includes_user_method_path_and_key():
    original = identity({})
    for changes in ({'user_id': 'member-b'}, {'method': 'PATCH'}, {'path': '/api/v1/other'}, {'key': 'intent-b'}):
        assert identity({}, **changes) != original


def test_idempotency_hash_preserves_array_order():
    assert identity({'ids': [1, 2]}).body_hash != identity({'ids': [2, 1]}).body_hash


@pytest.mark.parametrize('key', [None, '', '   '])
def test_missing_idempotency_key_rejected(key):
    with pytest.raises(ServiceError, match='INVALID_REQUEST'):
        identity({}, key=key)


def test_processing_request_cannot_be_reexecuted():
    error = request_in_progress()
    assert (error.code, error.status, error.retryable) == ('REQUEST_IN_PROGRESS', 409, False)
