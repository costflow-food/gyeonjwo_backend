"""견줘 에이전트 계약 (07 에이전트 구성도 기준). 오케스트레이션 담당이 정하고 팀원은 이 형식에 맞춰 구현한다.

1. 실행 한도
2. 백엔드 ↔ 그래프 계약 (run_case 입출력·재개 지점·진행 단계)
3. 코드 노드 결과
4. 역할 결과 항목 — ②후보·③질문·④구매안·⑤역제안. 역할 사이에 그대로 전달되므로 형식을 고정한다 (07 6절).
5. 에이전트 입력 — 원문·정책은 넘기지 않고 ID만 준다. 내용은 도구로 조회한다.
6. 에이전트 결과 — LLM 최종 응답 스키마. next_action으로 조정기가 다음 단계를 정한다.

금액·수량은 소수 문자열(DecimalStr, 예: "5450000", "12.5")이다. 계산은 도구가 하고 LLM은 결과를 옮겨 적는다.

업무 상태(Waiting/Running/Complete)·revision 검사는 app.features.work_cases.domain.contracts를 따른다.
API 오류 응답의 next_action(app.core.errors)과는 다른 값이다.
"""
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from app.shared.domain.schemas import StrictModel
from app.features.work_cases.domain.processing import ProcessingStep

# ─────────────────────────────────────────────
# 1. 실행 한도 (07 7절 초기 제안값, 인수 실험 후 조정)
# ─────────────────────────────────────────────
MAX_ROLE_CALLS_PER_RUN = 6
MAX_TOOL_CALLS_PER_AGENT_CALL = 8
READ_RETRY_LIMIT = 1

DecimalStr = Annotated[str, Field(pattern=r'^-?\d+(\.\d+)?$')]


# ─────────────────────────────────────────────
# 2. 백엔드 ↔ 그래프 계약
# ─────────────────────────────────────────────

class StartFrom(StrEnum):
    """재개 지점 (07 5.1). 항상 다섯 역할을 처음부터 돌리지 않는다."""
    START = 'START'  # 대안 찾기 최초 실행 → ① → ②
    REPLY = 'REPLY'  # 활성 업무에 확실히 연결된 회신 → ① → ③
    PLAN = 'PLAN'    # 단순 배분 오류 재실행 → ④


class RunStatus(StrEnum):
    EXTRACTION_READY = 'EXTRACTION_READY'  # ① 종료, 업무는 Running 유지
    NEEDS_INFO = 'NEEDS_INFO'

    """그래프 한 번 실행의 결과. 회신·승인 대기 중에는 그래프를 끝내고 백엔드가 다시 실행한다."""
    WAITING_REPLY = 'WAITING_REPLY'  # 초안까지 만들고 멈춤 → 백엔드가 승인 설정을 보고 발송
    COMPLETE = 'COMPLETE'            # 검토 카드 생성
    ON_HOLD = 'ON_HOLD'              # 진행 근거 없음 (후보 없음, 정책 없음 등)
    FAILED = 'FAILED'                # 호출 한도 초과, 모델·도구 오류, 형식 위반
    STALE = 'STALE'                  # 실행 중 새 회신·정책으로 input_revision이 바뀜. 결과를 쓰지 않는다


class RunCaseInput(StrictModel):
    case_id: UUID
    start_from: StartFrom
    input_revision: int = Field(strict=True, ge=0)  # 새 회신·정책 변경 시 백엔드가 +1
    mail_ids: list[str] = []                        # START: 최초 메일 / REPLY: 새 회신


class RunCaseOutput(StrictModel):
    case_id: UUID                                   # 입력값 그대로
    status: RunStatus
    processing_step: ProcessingStep                 # 멈춘 마지막 단계
    input_revision: int = Field(strict=True, ge=0)  # 이 결과가 본 데이터 시점
    draft_ids: list[str] = []                       # RFQ·보완 질문·역제안 초안 → 백엔드 발송
    card_id: str | None = None                      # COMPLETE일 때만
    unresolved: list[str] = []                      # 미확인 항목, 보류·실패 사유
    steps: list['AgentStep'] = []                   # 역할 호출 기록 → 백엔드가 실행 기록으로 저장 (07 9절)


# ─────────────────────────────────────────────
# 3. 코드 노드 결과
# ─────────────────────────────────────────────

class PlanCheck(StrEnum):
    PASS = 'PASS'            # → ⑤
    FIX = 'FIX'              # 배분 수정 → ④
    NEED_INFO = 'NEED_INFO'  # 추가 근거 → ③


class PriceCheck(StrEnum):
    PASS = 'PASS'            # → 검토 카드
    FIX = 'FIX'              # 정책 범위 밖 목표 조건·허용되지 않는 생략 → ⑤
    NEED_INFO = 'NEED_INFO'  # → ③


# ─────────────────────────────────────────────
# 4. 역할 결과 항목
# ─────────────────────────────────────────────

class CheckState(StrEnum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    UNKNOWN = 'UNKNOWN'  # 근거 없음 → RFQ 체크리스트로 확인


class ConditionCheck(StrictModel):
    """② 후보의 조건 체크리스트 한 줄."""
    condition: str = Field(min_length=1)       # 예: HACCP, 품종, 규격
    required: StrictBool                       # True: 필수 (FAIL이면 후보 제외, 가중치로 상쇄 불가) / False: 선호
    state: CheckState
    evidence_refs: list[str] = []


class Candidate(StrictModel):
    """② 문의 대상 후보."""
    supplier_id: str
    reason: str = Field(min_length=1, max_length=300)
    checklist: list[ConditionCheck] = []
    preference_score: DecimalStr | None = None  # 선호 조건 가중 합 (정렬용)
    provisional: StrictBool = False             # 웹 탐색 등 잠정 근거
    evidence_refs: list[str] = []


class QuestionPriority(StrEnum):
    MUST_CONFIRM = 'MUST_CONFIRM'            # 필수 조건 누락
    CAN_CHANGE_CHOICE = 'CAN_CHANGE_CHOICE'  # 답에 따라 업체 순위·분산 조합이 바뀔 수 있음
    LOW_VALUE = 'LOW_VALUE'                  # 추가 조사 실익 낮음 (입증된 경우만)


class Question(StrictModel):
    """③ 확인 항목. 보완 요청 초안의 질문이자 ④에 넘기는 미확인 목록."""
    supplier_id: str
    field: str = Field(min_length=1)           # 예: shipping_cost
    priority: QuestionPriority
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = []
    calculation_ref: str | None = None         # calculate_break_even 결과 ID
    break_even_amount: DecimalStr | None = None


class PlanType(StrEnum):
    KEEP = 'KEEP'      # 기존 업체 유지
    SWITCH = 'SWITCH'  # 전량 전환
    SPLIT = 'SPLIT'    # 최대 2개 업체 분산


class Allocation(StrictModel):
    supplier_id: str
    quote_version_id: str
    quantity: DecimalStr


class Plan(StrictModel):
    """④ 구매안. 총액·검증은 도구 결과를 옮겨 적는다."""
    plan_type: PlanType
    allocations: list[Allocation] = Field(min_length=1, max_length=2)
    total_amount: DecimalStr | None = None     # validate_purchase_plan 결과
    calculation_ref: str | None = None         # 도구 결과 ID
    conditions: list[str] = []                 # 성립 조건 (예: B 공급량 600개 확인 시)

    @model_validator(mode='after')
    def allocation_count(self):
        suppliers = {a.supplier_id for a in self.allocations}
        expected = 2 if self.plan_type == PlanType.SPLIT else 1
        if len(suppliers) != len(self.allocations) or len(suppliers) != expected:
            raise ValueError('KEEP/SWITCH need one supplier, SPLIT needs two distinct suppliers')
        return self


class CounterOfferTarget(StrictModel):
    """⑤ 기존 업체 역제안 조건."""
    supplier_id: str
    target_unit_price: DecimalStr
    conditions: list[str] = []
    basis_quote_version_ids: list[str] = Field(min_length=1)  # 인상 전·요청 가격 근거
    alternative_plan_ref: str | None = None    # ④ 대안 비용 참조
    policy_ref: str | None = None              # 회사 가격 정책
    validation_ref: str | None = None          # validate_counter_offer 결과 ID


# ─────────────────────────────────────────────
# 5. 에이전트 입력
# ─────────────────────────────────────────────

class AgentRole(StrEnum):
    INFO_EXTRACTOR = 'INFO_EXTRACTOR'
    CANDIDATE = 'CANDIDATE'
    INFO_PRIORITY = 'INFO_PRIORITY'
    RESPONSE_PLAN = 'RESPONSE_PLAN'
    COUNTER_OFFER = 'COUNTER_OFFER'


class AgentInput(StrictModel):
    case_id: UUID
    input_revision: int = Field(strict=True, ge=0)


class ExtractionIn(AgentInput):
    mail_ids: list[str]                  # 최초 메일 또는 새 회신
    recheck_fields: list[str] = []       # ③④⑤의 RECHECK_SOURCE로 다시 볼 항목


class CandidateIn(AgentInput):
    quote_version_ids: list[str]         # 인상 요청 견적. 요청 조건·필수 인증은 get_case_context로 조회


class PriorityIn(AgentInput):
    quote_version_ids: list[str]         # 회신 포함 최신 견적
    pending_questions: list[str] = []    # ④⑤·가격 검증이 넘긴 미확인


class PlanIn(AgentInput):
    quote_version_ids: list[str]
    questions: list[Question] = []       # ③의 확인 항목 (남은 미확인 표시용)
    plan_check_reason: str | None = None  # 구매안 검증 FIX로 재진입 시 사유


class CounterOfferIn(AgentInput):
    quote_version_ids: list[str]         # 기존 업체 포함 최신 견적
    validated_plans: list[Plan] = []     # 구매안 검증을 통과한 ④의 대안
    price_check_reason: str | None = None  # 가격 검증 FIX로 재진입 시 사유


# ─────────────────────────────────────────────
# 6. 에이전트 결과 — next_action은 역할마다 허용 값만 Literal로 둔다
# ─────────────────────────────────────────────

class AgentNextAction(StrEnum):
    STORED = 'STORED'                                  # ① 검증 저장 완료 → START면 ②, 그 외 ③
    SEND_RFQ = 'SEND_RFQ'                              # ② 업체별 RFQ 초안 → 멈춤(발송)
    REQUEST_CLARIFICATION = 'REQUEST_CLARIFICATION'    # ③ 보완 요청 초안 → 멈춤(발송)
    BUILD_PLANS = 'BUILD_PLANS'                        # ③ 검토 가능 → ④
    PLANS_PROPOSED = 'PLANS_PROPOSED'                  # ④ 구매안 구성 → 구매안 검증 노드
    COUNTER_OFFER_PROPOSED = 'COUNTER_OFFER_PROPOSED'  # ⑤ 역제안 → 가격 검증 노드
    COUNTER_OFFER_SKIPPED = 'COUNTER_OFFER_SKIPPED'    # ⑤ 정책상 역제안 불필요 → 가격 검증 노드가 생략 허용 확인
    NEEDS_EVIDENCE = 'NEEDS_EVIDENCE'                  # ④⑤ 추가 근거 필요 → ③
    RECHECK_SOURCE = 'RECHECK_SOURCE'                  # ③④⑤ 문서 재확인 필요 → ①
    HOLD = 'HOLD'                                      # 후보·정책·근거 부족으로 보류


N = AgentNextAction


class AgentOutput(StrictModel):
    unknown_fields: list[str] = []        # 원문에 없어 null로 둔 항목. NEEDS_EVIDENCE·RECHECK_SOURCE면 다음 역할에 전달
    reason: str = Field(min_length=1, max_length=500)


class ExtractionOut(AgentOutput):
    next_action: Literal[N.STORED, N.HOLD]
    quote_version_ids: list[str] = []


class CandidateOut(AgentOutput):
    next_action: Literal[N.SEND_RFQ, N.HOLD]
    candidates: list[Candidate] = []
    draft_ids: list[str] = []


class PriorityOut(AgentOutput):
    next_action: Literal[N.REQUEST_CLARIFICATION, N.BUILD_PLANS, N.RECHECK_SOURCE, N.HOLD]
    questions: list[Question] = []
    draft_ids: list[str] = []


class PlanOut(AgentOutput):
    next_action: Literal[N.PLANS_PROPOSED, N.NEEDS_EVIDENCE, N.RECHECK_SOURCE, N.HOLD]
    plans: list[Plan] = []


class CounterOfferOut(AgentOutput):
    next_action: Literal[N.COUNTER_OFFER_PROPOSED, N.COUNTER_OFFER_SKIPPED, N.NEEDS_EVIDENCE, N.RECHECK_SOURCE,
                         N.HOLD]
    target: CounterOfferTarget | None = None  # PROPOSED가 아니면 None
    draft_ids: list[str] = []

    @model_validator(mode='after')
    def skipped_has_no_offer(self):
        if self.next_action == N.COUNTER_OFFER_SKIPPED and (self.target or self.draft_ids):
            raise ValueError('COUNTER_OFFER_SKIPPED must not include target or draft_ids')
        return self


class AgentStep(StrictModel):
    """역할 호출 한 번의 기록. 그래프가 만들어 RunCaseOutput.steps에 쌓는다."""
    role: AgentRole
    next_action: AgentNextAction
    reason: str
    unknown_fields: list[str] = []


AGENT_IO = {
    AgentRole.INFO_EXTRACTOR: (ExtractionIn, ExtractionOut),
    AgentRole.CANDIDATE: (CandidateIn, CandidateOut),
    AgentRole.INFO_PRIORITY: (PriorityIn, PriorityOut),
    AgentRole.RESPONSE_PLAN: (PlanIn, PlanOut),
    AgentRole.COUNTER_OFFER: (CounterOfferIn, CounterOfferOut),
}

RunCaseOutput.model_rebuild()
