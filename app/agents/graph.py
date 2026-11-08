"""실행 조정기 (07 2절 흐름도·5절 실행 흐름). LangGraph StateGraph로 노드를 잇고 next_action으로 분기한다.

    START ─┐                         ┌─ (START) ② ─ SEND_RFQ ─────────────── wait_reply
    REPLY ─┴─ preprocess ─ ① ─ STORED┤
                                     └─ (REPLY) ③ ─ REQUEST_CLARIFICATION ─ wait_reply
    PLAN ──────────────────────────────────────┐   └ BUILD_PLANS ─ ④ ─ PLANS_PROPOSED ─ plan_validation
                                               └─────────────────┘          FIX → ④ · NEED_INFO → ③ · PASS ↓
                         ⑤ ─ COUNTER_OFFER_PROPOSED ─┬ price_validation ─ PASS ─ review_card (COMPLETE)
                           └ COUNTER_OFFER_SKIPPED ──┘                 FIX → ⑤ · NEED_INFO → ③
    COUNTER_OFFER_SKIPPED: 역제안 없이 인상 수용 추천(요청 인상률이 허용 인상률 이하이거나 요청 단가로도 대안보다 쌈).
    가격 검증이 수용 추천 조건을 다시 확인하고, 카드에 사유를 남긴다.
    ③④⑤ RECHECK_SOURCE → ①(근거를 인용한 메일을 다시 읽는다) · ④⑤ NEEDS_EVIDENCE → ③ · ①~④ HOLD → hold (ON_HOLD)
    ⑤ HOLD(정책 없음 등) → review_card: 구매안은 검증을 통과했으므로 카드에 구매안·역제안 보류 사유·참고 계산을 보인다

회신·승인 대기에서는 그래프를 끝낸다(WAITING_REPLY). 백엔드가 발송하고, 회신이 오면 start_from=REPLY로 다시 실행한다.
역할 호출이 MAX_ROLE_CALLS_PER_RUN을 넘거나, 에이전트·노드 오류·형식 위반이면 FAILED,
실행 중 input_revision이 바뀌면 STALE로 끝낸다. revision은 에이전트 호출마다, 그리고 검토 카드 저장 직전에 확인한다.
역할 호출마다 next_action·reason을 steps에 쌓아 백엔드가 실행 기록으로 남긴다. HOLD 사유는 unresolved에도 넣는다.
③④⑤는 호출 직전에 업무의 업체별 최신 견적 버전 ID 전체로 quote_version_ids를 채운다. PLAN 시작(① 없음)이나
일부 업체만 회신한 경우에도 비교 대상이 빠지지 않게 하고, 뒤따르는 구매안·가격 검증도 같은 집합을 쓴다.
"""
import logging
import operator
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.nodes import Nodes
from app.agents.schemas import (AGENT_IO, MAX_ROLE_CALLS_PER_RUN, AgentInput, AgentNextAction as N, AgentOutput,
                                AgentRole, AgentStep, Candidate, CandidateIn, CounterOfferIn, CounterOfferTarget,
                                ExtractionIn, Plan, PlanCheck, PlanIn, PriceCheck, PriorityIn,
                                ProcessingStep,
                                Question, RunCaseInput, RunCaseOutput, RunStatus, StartFrom)

logger = logging.getLogger('gyeonjwo')

RunAgent = Callable[[AgentRole, AgentInput], AgentOutput]
CurrentRevision = Callable[[str], int]
LatestQuotes = Callable[[str], list[str]]


class CaseState(TypedDict, total=False):
    """한 번 실행하는 동안 쓰는 공용 메모장. 실제 저장은 도구가 DB에 하고, 여기에는 결과 ID·요약만 둔다."""
    # 입력
    case_id: str
    start_from: StartFrom
    input_revision: int
    mail_ids: list[str]
    # 에이전트 결과
    quote_version_ids: list[str]                      # ①
    candidates: list[Candidate]                       # ②
    draft_ids: Annotated[list[str], operator.add]     # ②③⑤ 누적
    questions: list[Question]                         # ③
    pending_questions: list[str]                      # ④⑤가 ③에 넘긴 미확인
    recheck_fields: list[str]                         # ③④⑤가 ①에 넘긴 재확인 항목
    recheck_mail_ids: list[str]                       # 재확인 근거가 적힌 메일 (①이 이 메일을 다시 읽는다)
    plans: list[Plan]                                 # ④
    counter_offer: CounterOfferTarget | None          # ⑤
    counter_offer_skip_reason: str | None             # ⑤ COUNTER_OFFER_SKIPPED 사유
    counter_offer_result: dict | None                 # ⑤ next_action·reason·reference·draft_ids (검토 카드용)
    # 분기 기준
    last_action: N
    plan_check: PlanCheck
    plan_check_reason: str | None
    price_check: PriceCheck
    price_check_reason: str | None
    # 실행 관리
    role_calls: int
    processing_step: ProcessingStep
    status: RunStatus
    card_id: str | None
    unresolved: Annotated[list[str], operator.add]    # 미확인·보류·실패 사유 누적
    steps: Annotated[list[AgentStep], operator.add]   # 역할 호출 기록 누적


ROLE_STEPS = {
    AgentRole.INFO_EXTRACTOR: ProcessingStep.EXTRACTING,
    AgentRole.CANDIDATE: ProcessingStep.SELECTING_CANDIDATES,
    AgentRole.INFO_PRIORITY: ProcessingStep.GENERATING_PLANS,
    AgentRole.RESPONSE_PLAN: ProcessingStep.GENERATING_PLANS,
    AgentRole.COUNTER_OFFER: ProcessingStep.GENERATING_PLANS,
}


def _ids(state):
    return {'case_id': state['case_id'], 'input_revision': state['input_revision']}


INPUTS = {
    AgentRole.INFO_EXTRACTOR: lambda s: ExtractionIn(**_ids(s), recheck_fields=s.get('recheck_fields', []),
                                                     mail_ids=s.get('recheck_mail_ids') or s.get('mail_ids', [])),
    AgentRole.CANDIDATE: lambda s: CandidateIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', [])),
    AgentRole.INFO_PRIORITY: lambda s: PriorityIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', []),
                                                  pending_questions=s.get('pending_questions', [])),
    AgentRole.RESPONSE_PLAN: lambda s: PlanIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', []),
                                              questions=s.get('questions', []),
                                              plan_check_reason=s.get('plan_check_reason')),
    AgentRole.COUNTER_OFFER: lambda s: CounterOfferIn(**_ids(s), quote_version_ids=s.get('quote_version_ids', []),
                                                      validated_plans=s.get('plans', []),
                                                      price_check_reason=s.get('price_check_reason')),
}

OUTPUTS = {
    AgentRole.INFO_EXTRACTOR: lambda o: {'quote_version_ids': o.quote_version_ids, 'recheck_fields': [],
                                         'recheck_mail_ids': []},
    AgentRole.CANDIDATE: lambda o: {'candidates': o.candidates, 'draft_ids': o.draft_ids},
    AgentRole.INFO_PRIORITY: lambda o: {'questions': o.questions, 'draft_ids': o.draft_ids, 'pending_questions': []},
    AgentRole.RESPONSE_PLAN: lambda o: {'plans': o.plans},
    AgentRole.COUNTER_OFFER: lambda o: {
        'counter_offer': o.target, 'draft_ids': o.draft_ids,
        'counter_offer_result': {'next_action': o.next_action, 'reason': o.reason, 'draft_ids': o.draft_ids,
                                 'reference': o.reference and o.reference.model_dump(mode='json')},
        'counter_offer_skip_reason': o.reason if o.next_action == N.COUNTER_OFFER_SKIPPED else None},
}


def _failed(reason, step=ProcessingStep.FAILED):
    return {'status': RunStatus.FAILED, 'processing_step': step, 'unresolved': [reason]}


STALE = {'status': RunStatus.STALE, 'unresolved': ['INPUT_REVISION_CHANGED']}

LATEST_QUOTE_ROLES = (AgentRole.INFO_PRIORITY, AgentRole.RESPONSE_PLAN, AgentRole.COUNTER_OFFER)


def build_graph(run_agent: RunAgent, current_revision: CurrentRevision, nodes: Nodes = Nodes(),
                latest_quotes: LatestQuotes | None = None):
    """run_agent: agents.AgentRunner 또는 테스트용 가짜. current_revision: 업무의 현재 input_revision 조회(백엔드).
    latest_quotes: 업무의 업체별 최신 견적 버전 ID 조회(백엔드, work_cases의 latest_quote_version_ids와 같은 값).
    실제 실행에서는 꼭 넘긴다. None이면 상태에 들어 있는 quote_version_ids를 그대로 쓰며, 테스트용이다."""

    def changed(state):
        return current_revision(state['case_id']) != state['input_revision']

    def latest_quote_ids(role, state):
        """③④⑤ 호출 전에 최신 견적 ID 전체를 읽어 상태에 넣을 값을 돌려준다. 다른 역할이면 바꾸지 않는다.
        같은 실행 안에서도 ①이 새 견적을 저장할 수 있으므로 호출할 때마다 다시 읽는다."""
        if latest_quotes is None or role not in LATEST_QUOTE_ROLES:
            return {}
        return {'quote_version_ids': list(latest_quotes(state['case_id']))}

    def agent_node(role):
        def node(state):
            if state.get('role_calls', 0) >= MAX_ROLE_CALLS_PER_RUN:
                return _failed('ROLE_CALL_LIMIT')
            try:
                quote_ids = latest_quote_ids(role, state)
            except Exception:
                logger.warning('latest quote lookup failed role=%s', role)
                return _failed(f'{role}: QUOTE_LOOKUP_FAILED')
            try:
                output = run_agent(role, INPUTS[role](state | quote_ids))
            except Exception as error:
                # 실패 원인(예외 종류와 짧은 메시지)을 서버 로그에 남긴다. DB에는 AGENT_FAILED만 저장된다
                logger.warning('agent failed role=%s error=%s: %.300s', role, type(error).__name__, error)
                return _failed(f'{role}: AGENT_FAILED') | {'role_calls': state.get('role_calls', 0) + 1}
            update = {'role_calls': state.get('role_calls', 0) + 1, 'processing_step': ROLE_STEPS[role]} | quote_ids
            if not isinstance(output, AGENT_IO[role][1]):
                return update | _failed(f'{role}: INVALID_OUTPUT')
            if changed(state):
                return update | STALE
            step = AgentStep(role=role, next_action=output.next_action, reason=output.reason,
                             unknown_fields=output.unknown_fields)
            update |= OUTPUTS[role](output) | {'last_action': output.next_action, 'steps': [step],
                                               'unresolved': output.unknown_fields}
            if output.next_action == N.HOLD:
                update['unresolved'] = output.unknown_fields + [f'{role} HOLD: {output.reason}']
            elif output.next_action == N.NEEDS_EVIDENCE:
                update['pending_questions'] = output.unknown_fields
            elif output.next_action == N.RECHECK_SOURCE:
                update['recheck_fields'] = output.unknown_fields
                # 이번 실행의 새 메일이 아니라 근거를 인용한 메일을 다시 읽어야 한다(예: 기존 업체의 인상 통보 메일)
                update['recheck_mail_ids'] = list(dict.fromkeys(item.mail_id for item in getattr(output, 'recheck', [])))
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
    def review_card(state):
        if changed(state):
            return STALE
        card = nodes.review_card(state)
        if not card.get('card_id'):
            return STALE
        return card | {'status': RunStatus.COMPLETE, 'processing_step': ProcessingStep.REVIEW_READY}

    graph.add_node('review_card', code_node('review_card', review_card))
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
        'counter_offer': {N.COUNTER_OFFER_PROPOSED: 'price_validation', N.COUNTER_OFFER_SKIPPED: 'price_validation',
                          N.NEEDS_EVIDENCE: 'priority', N.RECHECK_SOURCE: 'extraction',
                          N.HOLD: 'review_card'},     # 역제안만 보류: 검증된 구매안과 참고 계산을 카드로 보인다
    }
    for name, table in routes.items():
        def route(state, table=table):
            target = {N.HOLD: 'hold', **table}[state['last_action']]
            return target(state) if callable(target) else target
        graph.add_conditional_edges(name, stopped(route))
    graph.add_conditional_edges('plan_validation', stopped(lambda s: {
        PlanCheck.PASS: 'counter_offer', PlanCheck.FIX: 'plan', PlanCheck.NEED_INFO: 'priority'}[s['plan_check']]))
    graph.add_conditional_edges('price_validation', stopped(lambda s: {
        PriceCheck.PASS: 'review_card', PriceCheck.FIX: 'counter_offer',
        PriceCheck.NEED_INFO: 'priority'}[s['price_check']]))
    for name in ('review_card', 'wait_reply', 'hold'):
        graph.add_edge(name, END)
    return graph.compile()


def run_case(graph, data: RunCaseInput) -> RunCaseOutput:
    state = graph.invoke({'case_id': str(data.case_id), 'start_from': data.start_from,
                          'input_revision': data.input_revision, 'mail_ids': data.mail_ids,
                          'role_calls': 0, 'draft_ids': [], 'unresolved': [], 'steps': []},
                         {'recursion_limit': 4 * MAX_ROLE_CALLS_PER_RUN + 4})
    # Completion commits exactly the drafts referenced by the final card; retries may have created older drafts.
    drafts = ((state.get('counter_offer_result') or {}).get('draft_ids', [])
              if state['status'] == RunStatus.COMPLETE else state.get('draft_ids', []))
    return RunCaseOutput(case_id=data.case_id, status=state['status'],
                         processing_step=state.get('processing_step', ProcessingStep.PENDING_START),
                         input_revision=data.input_revision, draft_ids=drafts,
                         card_id=state.get('card_id'), unresolved=state.get('unresolved', []),
                         steps=state.get('steps', []))
