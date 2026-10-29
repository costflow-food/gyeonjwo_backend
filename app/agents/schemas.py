"""견줘 에이전트 계약 (07 에이전트 구성도 기준). 오케스트레이션 담당이 정하고 팀원은 이 형식에 맞춰 구현한다.

1. 실행 한도
2. 백엔드 ↔ 그래프 계약 (run_case 입출력·재개 지점·진행 단계)
3. 코드 노드 결과
4. 에이전트 입력 — 원문·정책은 넘기지 않고 ID만 준다. 내용은 도구로 조회한다.
5. 에이전트 결과 — LLM 최종 응답 스키마. next_action으로 조정기가 다음 단계를 정한다.

업무 상태(Waiting/Running/Complete)·revision 검사는 app.features.work_cases.domain.contracts를 따른다.
API 오류 응답의 next_action(app.core.errors)과는 다른 값이다.
"""
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.shared.domain.schemas import StrictModel

# ─────────────────────────────────────────────
# 1. 실행 한도 (07 7절 초기 제안값, 인수 실험 후 조정)
# ─────────────────────────────────────────────
MAX_ROLE_CALLS_PER_RUN = 6
MAX_TOOL_CALLS_PER_AGENT_CALL = 8
READ_RETRY_LIMIT = 1


# ─────────────────────────────────────────────
# 2. 백엔드 ↔ 그래프 계약
# ─────────────────────────────────────────────

class StartFrom(StrEnum):
    """재개 지점 (07 5.1). 항상 다섯 역할을 처음부터 돌리지 않는다."""
    START = 'START'  # 대안 찾기 최초 실행 → ① → ②
    REPLY = 'REPLY'  # 활성 업무에 확실히 연결된 회신 → ① → ③
    PLAN = 'PLAN'    # 단순 배분 오류 재실행 → ④


class ProcessingStep(StrEnum):
    """업무 processing_step 값 (07 상태 계약). work_cases.CaseSnapshot.processing_step과 같은 값을 쓴다."""
    # Waiting — 백엔드
    PENDING_START = 'PENDING_START'
    NEEDS_CLASSIFICATION_REVIEW = 'NEEDS_CLASSIFICATION_REVIEW'
    # Running
    EXTRACTING = 'EXTRACTING'                          # 그래프
    SELECTING_CANDIDATES = 'SELECTING_CANDIDATES'      # 그래프
    AWAITING_APPROVAL = 'AWAITING_APPROVAL'            # 백엔드 (승인 ON)
    WAITING_REPLY = 'WAITING_REPLY'                    # 백엔드 (발송 후)
    NEEDS_INFO = 'NEEDS_INFO'                          # 그래프
    BUILDING_PLANS = 'BUILDING_PLANS'                  # 그래프
    BUILDING_COUNTER_OFFER = 'BUILDING_COUNTER_OFFER'  # 그래프
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


# ─────────────────────────────────────────────
# 3. 코드 노드 결과
# ─────────────────────────────────────────────

class PlanCheck(StrEnum):
    PASS = 'PASS'            # → ⑤
    FIX = 'FIX'              # 배분 수정 → ④
    NEED_INFO = 'NEED_INFO'  # 추가 근거 → ③


class PriceCheck(StrEnum):
    PASS = 'PASS'            # → 검토 카드
    NEED_INFO = 'NEED_INFO'  # → ③


# ─────────────────────────────────────────────
# 4. 에이전트 입력
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


class CandidateIn(AgentInput):
    quote_version_ids: list[str]         # 인상 요청 견적
    required_conditions: list[str] = []


class PriorityIn(AgentInput):
    quote_version_ids: list[str]         # 회신 포함 최신 견적
    pending_questions: list[str] = []    # ④⑤가 넘긴 미확인


class PlanIn(AgentInput):
    quote_version_ids: list[str]
    priority_result: dict = {}
    plan_check_reason: str | None = None  # 검증 실패로 재진입 시 사유


class CounterOfferIn(AgentInput):
    incumbent_quote_id: str | None = None  # 기존 업체 견적
    validated_plans: list[dict] = []       # ④의 검증된 대안


# ─────────────────────────────────────────────
# 5. 에이전트 결과 — next_action은 역할마다 허용 값만 Literal로 둔다
# ─────────────────────────────────────────────

class AgentNextAction(StrEnum):
    STORED = 'STORED'                                  # ① 검증 저장 완료 → START면 ②, 그 외 ③
    SEND_RFQ = 'SEND_RFQ'                              # ② 업체별 RFQ 초안 → 멈춤(발송)
    REQUEST_CLARIFICATION = 'REQUEST_CLARIFICATION'    # ③ 보완 요청 초안 → 멈춤(발송)
    BUILD_PLANS = 'BUILD_PLANS'                        # ③ 검토 가능 → ④
    PLANS_PROPOSED = 'PLANS_PROPOSED'                  # ④ 구매안 구성 → 구매안 검증 노드
    COUNTER_OFFER_PROPOSED = 'COUNTER_OFFER_PROPOSED'  # ⑤ 역제안 → 가격 검증 노드
    NEEDS_EVIDENCE = 'NEEDS_EVIDENCE'                  # ④⑤ 추가 근거 필요 → ③
    RECHECK_SOURCE = 'RECHECK_SOURCE'                  # ③④⑤ 문서 재확인 필요 → ①
    HOLD = 'HOLD'                                      # 후보·정책·근거 부족으로 보류


N = AgentNextAction


class AgentOutput(StrictModel):
    unknown_fields: list[str] = []        # 원문에 없어 null로 둔 항목
    reason: str = Field(min_length=1, max_length=500)


class ExtractionOut(AgentOutput):
    next_action: Literal[N.STORED, N.HOLD]
    quote_version_ids: list[str] = []


class CandidateOut(AgentOutput):
    next_action: Literal[N.SEND_RFQ, N.HOLD]
    candidates: list[dict] = []
    draft_ids: list[str] = []


class PriorityOut(AgentOutput):
    next_action: Literal[N.REQUEST_CLARIFICATION, N.BUILD_PLANS, N.RECHECK_SOURCE, N.HOLD]
    questions: list[dict] = []
    draft_ids: list[str] = []


class PlanOut(AgentOutput):
    next_action: Literal[N.PLANS_PROPOSED, N.NEEDS_EVIDENCE, N.RECHECK_SOURCE, N.HOLD]
    plans: list[dict] = []                # KEEP / SWITCH / SPLIT


class CounterOfferOut(AgentOutput):
    next_action: Literal[N.COUNTER_OFFER_PROPOSED, N.NEEDS_EVIDENCE, N.RECHECK_SOURCE, N.HOLD]
    target: dict | None = None
    draft_ids: list[str] = []


AGENT_IO = {
    AgentRole.INFO_EXTRACTOR: (ExtractionIn, ExtractionOut),
    AgentRole.CANDIDATE: (CandidateIn, CandidateOut),
    AgentRole.INFO_PRIORITY: (PriorityIn, PriorityOut),
    AgentRole.RESPONSE_PLAN: (PlanIn, PlanOut),
    AgentRole.COUNTER_OFFER: (CounterOfferIn, CounterOfferOut),
}
