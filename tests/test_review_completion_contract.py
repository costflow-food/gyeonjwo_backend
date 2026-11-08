from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents import agents, graph, nodes, schemas as s
from app.features.work_cases.application.mail_drafts import MailDraftService
from app.features.work_cases.domain.mail_drafts import requires_manual_approval
from scripts.check_market_scenario import CASE


@pytest.mark.parametrize('style,configured,setting,expected', [
    ({'auto_policy_eligible': True}, True, False, False),
    ({'auto_policy_eligible': True, 'candidate_caution': {'reason': '확인 필요'}}, True, False, True),
    ({'auto_policy_eligible': False}, True, False, True),
    ({}, True, False, True),
    ({'auto_policy_eligible': True}, True, True, True),
    ({}, False, None, None),
])
def test_effective_approval_matches_dispatch_guard(style, configured, setting, expected):
    policy = {'configured': configured, 'approval_required': setting, 'settings_version': 'base:case'}
    repo = SimpleNamespace(send_policy=Mock(return_value=policy))
    draft = {'id': 'draft', 'work_case_id': CASE, 'supplier_id': 'supplier', 'intent_id': 'intent',
             'purpose': 'CLARIFICATION', 'recipient': 'supplier@example.invalid', 'subject': '확인',
             'body_text': '운송비를 알려 주세요.', 'status': 'DRAFT', 'input_revision': 1,
             'style_snapshot_jsonb': style}
    result = MailDraftService(repo, None).present('owner', draft, {'input_revision': 1})
    assert result['effective_send_policy']['approval_required'] is expected
    assert policy['approval_required'] is setting  # The user's saved setting is unchanged.
    if configured and requires_manual_approval(draft):
        assert result['effective_send_policy']['approval_required'] is True
    assert result['effective_send_policy']['settings_version'] == 'base:case'


@pytest.mark.parametrize('last_action', ['COUNTER_OFFER_PROPOSED', 'COUNTER_OFFER_SKIPPED', 'HOLD'])
def test_counter_retry_completion_only_returns_final_card_drafts(last_action):
    attempts, cards = [], []
    def run(role, data):
        if role == s.AgentRole.RESPONSE_PLAN:
            return s.PlanOut(next_action='PLANS_PROPOSED', reason='graph contract fixture')
        attempts.append(role)
        first = len(attempts) == 1
        action = 'COUNTER_OFFER_PROPOSED' if first else last_action
        return s.CounterOfferOut(next_action=action, reason='graph contract fixture',
            draft_ids=['old-draft' if first else 'final-draft'] if action == 'COUNTER_OFFER_PROPOSED' else [])
    def save(state):
        cards.append(nodes.build_review_card(state))
        return {'card_id': 'card'}
    app = graph.build_graph(run, lambda _: 1, nodes=nodes.Nodes(
        plan_validation=lambda _: {'plan_check': s.PlanCheck.PASS},
        price_validation=lambda _: {'price_check': s.PriceCheck.FIX if len(attempts) == 1 else s.PriceCheck.PASS},
        review_card=save))
    result = graph.run_case(app, s.RunCaseInput(case_id=CASE, input_revision=1, start_from='PLAN'))
    assert result.status == 'COMPLETE'
    assert result.draft_ids == cards[0].counter_offer.draft_ids
    assert 'old-draft' not in result.draft_ids


def test_extraction_budget_handles_multiple_reply_bodies_and_attachments():
    limits = [agents.tool_call_limit(s.AgentRole.INFO_EXTRACTOR,
              s.ExtractionIn(case_id=CASE, input_revision=1, mail_ids=[f'mail-{i}' for i in range(count)]))
              for count in (1, 2, 3, 20)]
    assert limits == [8, 12, 16, 24]
