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
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from app.shared.domain.schemas import StrictModel

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


class ProcessingStep(StrEnum):
    """업무 processing_step 값. DB work_cases_processing_step_check가 허용하는 값과 같다.
    Running은 EXTRACTING·SELECTING_CANDIDATES·CANDIDATES·GENERATING_PLANS·WAITING_REPLY만, Pending은
    WAITING_APPROVAL·NEEDS_INFO·ON_HOLD·FAILED·SEND_UNKNOWN만 쓸 수 있다(work_cases_check4·5)."""
    # Waiting·Confirm — 백엔드
    PENDING_START = 'PENDING_START'
    ACKNOWLEDGED = 'ACKNOWLEDGED'
    # Running
    EXTRACTING = 'EXTRACTING'                          # 그래프: 정보 추출
    SELECTING_CANDIDATES = 'SELECTING_CANDIDATES'      # 그래프: 후보 선정 에이전트 실행 중
    CANDIDATES = 'CANDIDATES'                          # 백엔드: 그래프가 끝나고 사용자 후보 선택 대기
    GENERATING_PLANS = 'GENERATING_PLANS'              # 그래프: 정보·우선순위, 구매안, 역제안
    WAITING_REPLY = 'WAITING_REPLY'                    # 백엔드 (발송 후)
    # Pending
    WAITING_APPROVAL = 'WAITING_APPROVAL'              # 백엔드 (승인 ON)
    NEEDS_INFO = 'NEEDS_INFO'                          # 백엔드
    SEND_UNKNOWN = 'SEND_UNKNOWN'                      # 백엔드 (발송 결과 미확인)
    ON_HOLD = 'ON_HOLD'                                # 그래프
    FAILED = 'FAILED'                                  # 그래프
    # Complete
    REVIEW_READY = 'REVIEW_READY'                      # 그래프


class RunStatus(StrEnum):
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


class CandidateCaution(StrictModel):
    """② 후보 업체의 등록 비고를 읽고 담당자 확인이 필요하다고 본 이유. 후보에서 빼지 않는다(BR-15)."""
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1)


class Candidate(StrictModel):
    """② 문의 대상 후보."""
    supplier_id: str
    reason: str = Field(min_length=1, max_length=300)
    checklist: list[ConditionCheck] = []
    preference_score: DecimalStr | None = None  # 선호 조건 가중 합 (정렬용)
    provisional: StrictBool = False             # 웹 탐색 등 잠정 근거
    evidence_refs: list[str] = []
    caution: CandidateCaution | None = None     # 있으면 승인 OFF에서도 자동 발송하지 않고 담당자가 확인한다


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
    break_even_amount: DecimalStr | None = None  # 최종 검사가 calculate_break_even 결과로 채운다
    formula: str | None = None                 # 역전 기준 산식 (화면 표시용, 최종 검사가 채운다)


class RecheckEvidence(StrictModel):
    """③이 ①에 재추출을 넘길 근거: 견적에 빠진 항목의 답이 적힌 원문 문장. 최종 검사가 읽은 원문과 대조한다."""
    supplier_id: str
    field: str = Field(min_length=1)           # check_comparability의 missing_required·unknown_costs field
    mail_id: str
    source_text: str = Field(min_length=1)     # 답이 적힌 원문 구절 그대로 (예: "월 1,000kg 공급 가능")


class PlanType(StrEnum):
    KEEP = 'KEEP'      # 기존 업체 유지
    SWITCH = 'SWITCH'  # 전량 전환
    SPLIT = 'SPLIT'    # 최대 2개 업체 분산


class Allocation(StrictModel):
    supplier_id: str
    quote_version_id: str
    quantity: DecimalStr


class Plan(StrictModel):
    """④ 구매안. 배정량·총액·검증 결과는 최종 검사가 validate_purchase_plan 결과로 채운다."""
    plan_type: PlanType
    allocations: list[Allocation] = Field(min_length=1, max_length=2)
    total_amount: DecimalStr | None = None     # validate_purchase_plan 결과
    validation_status: Literal['VALID', 'CONDITIONAL'] = 'VALID'   # CONDITIONAL: 기존 업체의 미확인이 남은 유지안 등
    conditions: list[str] = []                 # 성립 조건 (예: 기존 업체 운송비 미확인)
    adjustments: list[str] = []                # 배정량 조정 내역 (예: 제안 A 610·B 390 → A 600·B 400, B 발주 배수 50)
    reason: str | None = Field(default=None, max_length=300)   # 이 안을 보여 주는 이유 (담당자가 읽는 한 문장)

    @model_validator(mode='after')
    def allocation_count(self):
        suppliers = {a.supplier_id for a in self.allocations}
        expected = 2 if self.plan_type == PlanType.SPLIT else 1
        if len(suppliers) != len(self.allocations) or len(suppliers) != expected:
            raise ValueError('KEEP/SWITCH need one supplier, SPLIT needs two distinct suppliers')
        return self


class CounterOfferReference(StrictModel):
    """⑤ 참고 계산. 정책이 없어 목표를 만들지 않을 때도 검토 카드에 보인다. 값은 최종 검사가 도구 결과로 채운다."""
    status: str                                # PROPOSABLE·NO_POLICY·NOT_NEEDED·WITHIN_POLICY·NOT_COMPUTABLE 등
    unit: str | None = None
    requested_unit_price: DecimalStr | None = None   # 기존 업체가 요청한 단가
    previous_unit_price: DecimalStr | None = None    # 인상 전 단가 (인상 통보 메일 근거)
    ceiling_unit_price: DecimalStr | None = None     # 이 단가 이하면 비교한 대안보다 싸거나 같다
    alternative_total: DecimalStr | None = None      # 비교한 대안의 총액
    allowed_range: list[DecimalStr] = []             # 정책·상한을 만족하는 목표 단가 [최소, 최대]


class CounterOfferTarget(StrictModel):
    """⑤ 기존 업체 역제안 조건. 근거·대안·상한은 최종 검사가 도구 결과로 채운다."""
    supplier_id: str
    target_unit_price: DecimalStr
    conditions: list[str] = []
    basis_quote_version_ids: list[str] = []          # 요청 단가 견적 (최종 검사가 채운다)
    alternative: list[Allocation] = []               # 비교한 대안의 배분


class CounterOfferSummary(StrictModel):
    """검토 카드의 ⑤ 결과. 생략·보류에도 사유와 참고 계산을 보인다."""
    status: Literal['PROPOSED', 'SKIPPED', 'HOLD']
    target: CounterOfferTarget | None = None
    reference: CounterOfferReference | None = None
    draft_ids: list[str] = []
    reason: str | None = None


class ReviewCard(StrictModel):
    """검토 카드 (06 work_cases.latest_review_jsonb). 담당자가 보는 구매안·역제안·미확인과 그 근거 견적.
    숫자는 검증 노드를 통과한 값을 그대로 옮긴다(카드에 보인 숫자는 계산 코드가 바뀌어도 카드에 남는다)."""
    case_id: str
    input_revision: int
    quote_version_ids: list[str] = []
    plans: list[Plan] = []
    counter_offer: CounterOfferSummary | None = None
    questions: list[Question] = []         # ③이 남긴 확인 항목 (묻지 않은 LOW_VALUE 근거 포함)
    unresolved: list[str] = []             # 미확인·보류 사유
    steps: list['AgentStep'] = []          # 역할 호출 기록 (next_action·reason)


# ─────────────────────────────────────────────
# 5. 에이전트 입력
# ─────────────────────────────────────────────

class AgentRole(StrEnum):
    INFO_EXTRACTOR = 'INFO_EXTRACTOR'
    CANDIDATE = 'CANDIDATE'
    INFO_PRIORITY = 'INFO_PRIORITY'
    RESPONSE_PLAN = 'RESPONSE_PLAN'
    COUNTER_OFFER = 'COUNTER_OFFER'


# 역할별 도구 호출 한도. 없는 역할은 MAX_TOOL_CALLS_PER_AGENT_CALL을 쓴다.
# 확인 우선순위는 비용 누락도 원문을 다시 읽고, 대응안은 평균 5.5~6.2회를 써서 8회를 가끔 넘었다.
TOOL_CALL_LIMITS = {AgentRole.INFO_PRIORITY: 10, AgentRole.RESPONSE_PLAN: 10}


class AgentInput(StrictModel):
    case_id: UUID
    input_revision: int = Field(strict=True, ge=0)


@dataclass(frozen=True)
class RunContext:
    """도구에 넘기는 실행 정보. LLM 인자가 아니라 AgentRunner가 넣고, 도구는 runtime.context로 읽는다."""
    case_id: str
    input_revision: int
    agent_run_id: str | None = None      # 06 agent_runs.id. 실행 기록 저장소가 없으면 None
    review_batch_id: str | None = None   # 06 agent_runs.review_batch_id


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
    recheck: list[RecheckEvidence] = []    # RECHECK_SOURCE의 근거 (원문에 답이 있는데 견적에 빠진 항목)


class PlanOut(AgentOutput):
    next_action: Literal[N.PLANS_PROPOSED, N.NEEDS_EVIDENCE, N.RECHECK_SOURCE, N.HOLD]
    plans: list[Plan] = Field(default=[], max_length=5)
    recheck: list[RecheckEvidence] = []    # RECHECK_SOURCE의 근거 (③과 같은 규칙)


class CounterOfferOut(AgentOutput):
    next_action: Literal[N.COUNTER_OFFER_PROPOSED, N.COUNTER_OFFER_SKIPPED, N.NEEDS_EVIDENCE, N.RECHECK_SOURCE,
                         N.HOLD]
    target: CounterOfferTarget | None = None  # PROPOSED가 아니면 None
    draft_ids: list[str] = []
    reference: CounterOfferReference | None = None   # 참고 계산 (정책 없음·생략·보류에도 남긴다)
    recheck: list[RecheckEvidence] = []           # RECHECK_SOURCE의 근거 (③과 같은 규칙)

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
ReviewCard.model_rebuild()
