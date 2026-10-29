from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.schemas import (AGENT_IO, AgentNextAction, AgentRole, ExtractionIn, ExtractionOut, PlanOut,
                                RunCaseInput)
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
