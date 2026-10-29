from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.schemas import (AGENT_IO, AgentNextAction, AgentRole, Allocation, CounterOfferOut, CounterOfferTarget,
                                ExtractionIn, ExtractionOut, Plan, PlanOut, RunCaseInput)
from app.agents.tools import KNOWN_TOOLS, ROLE_TOOLS


def allowed(role):
    return set(get_args(AGENT_IO[role][1].model_fields['next_action'].annotation))


def test_every_role_has_io_tools_and_can_hold():
    assert set(AGENT_IO) == set(ROLE_TOOLS) == set(AgentRole)
    assert all(AgentNextAction.HOLD in allowed(role) for role in AgentRole)
    assert set().union(*(allowed(role) for role in AgentRole)) == set(AgentNextAction)


def test_role_cannot_return_another_roles_next_action():
    with pytest.raises(ValidationError):
        ExtractionOut(next_action='SEND_RFQ', reason='근거')
    with pytest.raises(ValidationError):
        PlanOut(next_action='COUNTER_OFFER_PROPOSED', reason='근거')


def test_unknown_fields_and_loose_revision_are_rejected():
    with pytest.raises(ValidationError):
        ExtractionIn(case_id=uuid4(), input_revision=1, mail_ids=[], extra='x')
    with pytest.raises(ValidationError):
        RunCaseInput(case_id=uuid4(), start_from='START', input_revision='1')
    with pytest.raises(ValidationError):
        RunCaseInput(case_id=uuid4(), start_from='RESUME', input_revision=1)


def test_role_tools_exclude_node_only_and_unsafe_tools():
    assert not KNOWN_TOOLS & {'classify_mail_input', 'preprocess_mail_documents', 'dispatch_mail'}
    assert 'store_structured_mail' in ROLE_TOOLS[AgentRole.INFO_EXTRACTOR]
    assert 'store_structured_mail' not in ROLE_TOOLS[AgentRole.CANDIDATE]


def test_skipped_counter_offer_has_no_target_or_draft():
    target = CounterOfferTarget(supplier_id='A', target_unit_price='5400', basis_quote_version_ids=['A-v1'])
    CounterOfferOut(next_action='COUNTER_OFFER_SKIPPED', reason='정책상 유지 불필요')
    for extra in ({'target': target}, {'draft_ids': ['d1']}):
        with pytest.raises(ValidationError):
            CounterOfferOut(next_action='COUNTER_OFFER_SKIPPED', reason='근거', **extra)


def allocation(supplier, quantity='600'):
    return Allocation(supplier_id=supplier, quote_version_id=f'{supplier}-v1', quantity=quantity)


def test_plan_allocations_match_plan_type():
    Plan(plan_type='SPLIT', allocations=[allocation('B'), allocation('A', '400')], total_amount='5450000')
    Plan(plan_type='KEEP', allocations=[allocation('A', '1000')])
    for plan_type, suppliers in [('SPLIT', ['A']), ('SPLIT', ['A', 'A']), ('KEEP', ['A', 'B'])]:
        with pytest.raises(ValidationError):
            Plan(plan_type=plan_type, allocations=[allocation(s) for s in suppliers])


@pytest.mark.parametrize('amount', ['5,450,000', '5450000원', '1e6', ''])
def test_amounts_are_plain_decimal_strings(amount):
    with pytest.raises(ValidationError):
        CounterOfferTarget(supplier_id='A', target_unit_price=amount, basis_quote_version_ids=['A-v1'])
