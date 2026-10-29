"""실행 조정기 (07 2절 흐름도·5절 실행 흐름). LangGraph StateGraph로 노드를 잇고 next_action으로 분기한다.

    START ─┐                         ┌─ (START) ② ─ SEND_RFQ ─────────────── wait_reply
    REPLY ─┴─ preprocess ─ ① ─ STORED┤
                                     └─ (REPLY) ③ ─ REQUEST_CLARIFICATION ─ wait_reply
    PLAN ──────────────────────────────────────┐   └ BUILD_PLANS ─ ④ ─ PLANS_PROPOSED ─ plan_validation
                                               └─────────────────┘          FIX → ④ · NEED_INFO → ③ · PASS ↓
                         ⑤ ─ COUNTER_OFFER_PROPOSED ─ price_validation ─ PASS ─ review_card (COMPLETE)
                                                                      └ NEED_INFO → ③
    ③④⑤ RECHECK_SOURCE → ① · ④⑤ NEEDS_EVIDENCE → ③ · 모든 역할 HOLD → hold (ON_HOLD)

회신·승인 대기에서는 그래프를 끝낸다(WAITING_REPLY). 백엔드가 발송하고, 회신이 오면 start_from=REPLY로 다시 실행한다.
역할 호출이 MAX_ROLE_CALLS_PER_RUN을 넘거나, 에이전트·노드 오류·형식 위반이면 FAILED,
실행 중 input_revision이 바뀌면 STALE로 끝낸다.
"""
import logging
import operator
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.nodes import Nodes
from app.agents.schemas import (AGENT_IO, MAX_ROLE_CALLS_PER_RUN, AgentInput, AgentNextAction as N, AgentOutput,
                                AgentRole, CandidateIn, CounterOfferIn, ExtractionIn, PlanCheck, PlanIn,
                                PriceCheck, PriorityIn, ProcessingStep, RunCaseInput, RunCaseOutput, RunStatus,
                                StartFrom)

logger = logging.getLogger('gyeonjwo')

RunAgent = Callable[[AgentRole, AgentInput], AgentOutput]
CurrentRevision = Callable[[str], int]


class CaseState(TypedDict, total=False):
    """한 번 실행하는 동안 쓰는 공용 메모장. 실제 저장은 도구가 DB에 하고, 여기에는 결과 ID·요약만 둔다."""
    # 입력
    case_id: str
    start_from: StartFrom
    input_revision: int
    mail_ids: list[str]
    # 에이전트 결과
    quote_version_ids: list[str]                      # ①
    candidates: list[dict]                            # ②
    draft_ids: Annotated[list[str], operator.add]     # ②③⑤ 누적
    priority: dict                                    # ③
    pending_questions: list[str]                      # ④⑤가 ③에 넘긴 미확인
    plans: list[dict]                                 # ④
    counter_offer: dict | None                        # ⑤
    # 분기 기준
    last_action: N
    plan_check: PlanCheck
    plan_check_reason: str | None
    price_check: PriceCheck
    # 실행 관리
    role_calls: int
    processing_step: ProcessingStep
    status: RunStatus
    card_id: str | None
    unresolved: Annotated[list[str], operator.add]    # 미확인·보류·실패 사유 누적


ROLE_STEPS = {
    AgentRole.INFO_EXTRACTOR: ProcessingStep.EXTRACTING,
    AgentRole.CANDIDATE: ProcessingStep.SELECTING_CANDIDATES,
    AgentRole.INFO_PRIORITY: ProcessingStep.NEEDS_INFO,
    AgentRole.RESPONSE_PLAN: ProcessingStep.BUILDING_PLANS,
    AgentRole.COUNTER_OFFER: ProcessingStep.BUILDING_COUNTER_OFFER,
}


def _ids(state):
    return {'case_id': state['case_id'], 'input_revision': state['input_revision']}


INPUTS = {
    AgentRole.INFO_EXTRACTOR: lambda s: ExtractionIn(**_ids(s), mail_ids=s.get('mail_ids', [])),
    AgentRole.CANDIDATE: lambda s: CandidateIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', [])),
    AgentRole.INFO_PRIORITY: lambda s: PriorityIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', []),
                                                  pending_questions=s.get('pending_questions', [])),
    AgentRole.RESPONSE_PLAN: lambda s: PlanIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', []),
                                              priority_result=s.get('priority', {}),
                                              plan_check_reason=s.get('plan_check_reason')),
    AgentRole.COUNTER_OFFER: lambda s: CounterOfferIn(**_ids(s), validated_plans=s.get('plans', [])),
}

OUTPUTS = {
    AgentRole.INFO_EXTRACTOR: lambda o: {'quote_version_ids': o.quote_version_ids},
    AgentRole.CANDIDATE: lambda o: {'candidates': o.candidates, 'draft_ids': o.draft_ids},
    AgentRole.INFO_PRIORITY: lambda o: {'priority': {'questions': o.questions}, 'draft_ids': o.draft_ids,
                                        'pending_questions': []},
    AgentRole.RESPONSE_PLAN: lambda o: {'plans': o.plans},
    AgentRole.COUNTER_OFFER: lambda o: {'counter_offer': o.target, 'draft_ids': o.draft_ids},
}


def _failed(reason, step=ProcessingStep.FAILED):
    return {'status': RunStatus.FAILED, 'processing_step': step, 'unresolved': [reason]}


def build_graph(run_agent: RunAgent, current_revision: CurrentRevision, nodes: Nodes = Nodes()):
    """run_agent: agents.AgentRunner 또는 테스트용 가짜. current_revision: 업무의 현재 input_revision 조회(백엔드)."""

    def agent_node(role):
        def node(state):
            if state.get('role_calls', 0) >= MAX_ROLE_CALLS_PER_RUN:
                return _failed('ROLE_CALL_LIMIT')
            try:
                output = run_agent(role, INPUTS[role](state))
            except Exception:
                logger.warning('agent failed role=%s', role)
                return _failed(f'{role}: AGENT_FAILED') | {'role_calls': state.get('role_calls', 0) + 1}
            update = {'role_calls': state.get('role_calls', 0) + 1, 'processing_step': ROLE_STEPS[role]}
            if not isinstance(output, AGENT_IO[role][1]):
                return update | _failed(f'{role}: INVALID_OUTPUT')
            if current_revision(state['case_id']) != state['input_revision']:
                return update | {'status': RunStatus.STALE, 'unresolved': ['INPUT_REVISION_CHANGED']}
            update |= OUTPUTS[role](output) | {'last_action': output.next_action, 'unresolved': output.unknown_fields}
            if output.next_action == N.NEEDS_EVIDENCE:
                update['pending_questions'] = output.unknown_fields
            return update
        return node

    def code_node(name, func):
        def node(state):
            try:
                return func(state)
            except Exception:
                logger.warning('node failed node=%s', name)
                return _failed(f'{name}: NODE_FAILED')
        return node

    graph = StateGraph(CaseState)
    names = {AgentRole.INFO_EXTRACTOR: 'extraction', AgentRole.CANDIDATE: 'candidate',
             AgentRole.INFO_PRIORITY: 'priority', AgentRole.RESPONSE_PLAN: 'plan',
             AgentRole.COUNTER_OFFER: 'counter_offer'}
    for role, name in names.items():
        graph.add_node(name, agent_node(role))
    for name in ('preprocess', 'plan_validation', 'price_validation'):
        graph.add_node(name, code_node(name, getattr(nodes, name)))
    graph.add_node('review_card', code_node('review_card', lambda s: nodes.review_card(s) | {
        'status': RunStatus.COMPLETE, 'processing_step': ProcessingStep.REVIEW_READY}))
    graph.add_node('wait_reply', lambda s: {'status': RunStatus.WAITING_REPLY})
    graph.add_node('hold', lambda s: {'status': RunStatus.ON_HOLD, 'processing_step': ProcessingStep.ON_HOLD})

    def stopped(route):
        def guarded(state):
            return END if state.get('status') else route(state)
        return guarded

    graph.add_conditional_edges(START, lambda s: 'plan' if s['start_from'] == StartFrom.PLAN else 'preprocess')
    graph.add_conditional_edges('preprocess', stopped(lambda s: 'extraction'))
    routes = {
        'extraction': {N.STORED: lambda s: 'candidate' if s['start_from'] == StartFrom.START else 'priority'},
        'candidate': {N.SEND_RFQ: 'wait_reply'},
        'priority': {N.REQUEST_CLARIFICATION: 'wait_reply', N.BUILD_PLANS: 'plan', N.RECHECK_SOURCE: 'extraction'},
        'plan': {N.PLANS_PROPOSED: 'plan_validation', N.NEEDS_EVIDENCE: 'priority', N.RECHECK_SOURCE: 'extraction'},
        'counter_offer': {N.COUNTER_OFFER_PROPOSED: 'price_validation', N.NEEDS_EVIDENCE: 'priority',
                          N.RECHECK_SOURCE: 'extraction'},
    }
    for name, table in routes.items():
        def route(state, table=table):
            target = {N.HOLD: 'hold', **table}[state['last_action']]
            return target(state) if callable(target) else target
        graph.add_conditional_edges(name, stopped(route))
    graph.add_conditional_edges('plan_validation', stopped(lambda s: {
        PlanCheck.PASS: 'counter_offer', PlanCheck.FIX: 'plan', PlanCheck.NEED_INFO: 'priority'}[s['plan_check']]))
    graph.add_conditional_edges('price_validation', stopped(lambda s: {
        PriceCheck.PASS: 'review_card', PriceCheck.NEED_INFO: 'priority'}[s['price_check']]))
    for name in ('review_card', 'wait_reply', 'hold'):
        graph.add_edge(name, END)
    return graph.compile()


def run_case(graph, data: RunCaseInput) -> RunCaseOutput:
    state = graph.invoke({'case_id': str(data.case_id), 'start_from': data.start_from,
                          'input_revision': data.input_revision, 'mail_ids': data.mail_ids,
                          'role_calls': 0, 'draft_ids': [], 'unresolved': []},
                         {'recursion_limit': 4 * MAX_ROLE_CALLS_PER_RUN + 4})
    return RunCaseOutput(case_id=data.case_id, status=state['status'],
                         processing_step=state.get('processing_step', ProcessingStep.PENDING_START),
                         input_revision=data.input_revision, draft_ids=state.get('draft_ids', []),
                         card_id=state.get('card_id'), unresolved=state.get('unresolved', []))
