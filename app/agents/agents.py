"""다섯 에이전트 (07 3절). 담당 팀원은 자기 역할의 PROMPTS 지시문과 tools.py 도구를 채운다.

각 에이전트는 LangChain create_agent로 만든 도구 호출 루프다. 허용 도구만 받고(tools.ROLE_TOOLS),
호출당 도구 MAX_TOOL_CALLS_PER_AGENT_CALL회를 넘으면 실패하며, 최종 답은 schemas의 역할별 *Out 모델로 검증된다.
입력은 schemas의 역할별 *In 모델(ID만)이고, 원문·정책은 도구로 조회한다.

시스템 프롬프트 = 공통 POLICY(shared/infrastructure/llm.py) + 역할 이름 + PROMPTS[역할].
PROMPTS에는 역할 목표, 어떤 도구를 언제 쓰는지, 어떤 경우 어떤 next_action을 고르는지를 적는다.
출력 형식(JSON 필드)은 *Out 모델이 스키마로 강제하므로 프롬프트에 다시 적지 않아도 된다.

혼자 실행해 보기 (.env에 LLM 설정 필요):

    from uuid import uuid4
    from app.core.config import get_settings
    from app.agents.agents import AgentRunner, chat_model
    from app.agents.schemas import AgentRole, ExtractionIn

    runner = AgentRunner(chat_model(get_settings()))
    print(runner(AgentRole.INFO_EXTRACTOR, ExtractionIn(case_id=uuid4(), input_revision=0, mail_ids=['m1'])))
"""
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.agents.schemas import AGENT_IO, MAX_TOOL_CALLS_PER_AGENT_CALL, AgentInput, AgentOutput, AgentRole
from app.agents.tools import tools_for
from app.shared.infrastructure.llm import POLICY

# 07 7절. 역할 지시문은 담당 팀원이 작성한다. 주석은 넣어야 할 내용과 고를 수 있는 next_action이다.
PROMPTS = {
    # ① 문서 선택 조회·필드 근거·품목 비고 판단·store_structured_mail 저장, 비고로 후보 자동 배제 금지
    #    STORED: 저장 완료 / HOLD: 읽을 수 있는 견적이 없음
    AgentRole.INFO_EXTRACTOR: '',
    # ② 취급 근거·필수 인증/규격(필수는 가중치로 상쇄 불가)·선호 가중치 체크리스트·업체별 RFQ 초안
    #    SEND_RFQ: 후보별 초안 생성 / HOLD: 후보 없음 (조회 범위·부족한 근거를 reason에)
    AgentRole.CANDIDATE: '',
    # ③ 필수 누락 우선, 원문에 이미 있는 답은 재문의 금지, calculate_break_even으로 역전 가능성 확인, 질문 묶기
    #    REQUEST_CLARIFICATION: 보완 초안 생성 / BUILD_PLANS: 검토 가능 / RECHECK_SOURCE: ① 재추출 필요 / HOLD
    AgentRole.INFO_PRIORITY: '',
    # ④ 유지·전환·최대 2곳 분산안, validate_purchase_plan 실패 사유로 수정, 탐색 범위·조건부 명시
    #    PLANS_PROPOSED: 안 구성 / NEEDS_EVIDENCE: ③ 확인 필요(unknown_fields에) / RECHECK_SOURCE / HOLD
    AgentRole.RESPONSE_PLAN: '',
    # ⑤ 기존 업체 유지 조건, 검증된 대안·회사 정책 기준, 정책 없으면 목표 가격 생성 금지
    #    COUNTER_OFFER_PROPOSED: 목표 조건·초안 / COUNTER_OFFER_SKIPPED: 정책상 역제안 불필요 (target·초안 없이, reason에 근거)
    #    NEEDS_EVIDENCE / RECHECK_SOURCE / HOLD: 정책·근거 없음
    #    가격 검증 FIX로 다시 호출되면 입력의 price_check_reason을 보고 목표 조건을 고친다
    AgentRole.COUNTER_OFFER: '',
}


def chat_model(settings) -> BaseChatModel:
    settings.require_llm()
    return ChatOpenAI(model=settings.llm_model, base_url=str(settings.llm_base_url),
                      api_key=settings.llm_api_key.get_secret_value(), timeout=settings.llm_timeout_seconds)


def build_agent(role: AgentRole, model: BaseChatModel):
    _, output = AGENT_IO[role]
    return create_agent(
        model, tools_for(role), system_prompt=POLICY + '\nRole: ' + role + '\n' + PROMPTS[role],
        response_format=ToolStrategy(output),
        middleware=[ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS_PER_AGENT_CALL, exit_behavior='error')],
        name=role.lower(),
    )


class AgentRunner:
    """graph.build_graph에 넘기는 run_agent(role, input) 구현. 역할별 에이전트는 처음 호출할 때 만든다."""

    def __init__(self, model: BaseChatModel):
        self.model, self.agents = model, {}

    def __call__(self, role: AgentRole, data: AgentInput) -> AgentOutput:
        expected_in, _ = AGENT_IO[role]
        if not isinstance(data, expected_in):
            raise TypeError(f'{role} expects {expected_in.__name__}')
        if role not in self.agents:
            self.agents[role] = build_agent(role, self.model)
        result = self.agents[role].invoke({'messages': [('user', data.model_dump_json())]})
        return result['structured_response']
