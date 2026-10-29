# 에이전트 작업 안내

Notion [07 에이전트 구성도](https://app.notion.com/p/b30311ce9342825286b1815d97c03672)의 다섯 역할을 LangGraph로 연결한 뼈대다.
그래프 흐름, 입출력 계약, 한도와 오류 처리는 이미 구현되어 있고 테스트도 통과한다.
아직 비어 있는 부분은 **역할별 프롬프트, 도구, 코드 노드**다.

## 브랜치

```bash
git fetch origin
git switch -c feature/agents-<역할> origin/feature/agents   # 예: feature/agents-extractor
```

- PR은 `feature/agents`로 올린다(`main`이 아니다). 오케스트레이션 담당이 리뷰하고 합친 뒤 `main`에 올린다.
- 자기 역할의 구역만 고친다. `schemas.py`와 `graph.py`는 수정하지 않는다(아래 "계약 변경" 참고).

## 파일과 담당

| 파일 | 담당 | 고칠 곳 |
|---|---|---|
| `schemas.py` | 오케스트레이션 | 실행 한도, 백엔드↔그래프 계약, 에이전트별 `…In`/`…Out` |
| `graph.py` | 오케스트레이션 | 노드 연결, `next_action` 분기, 한도·STALE·실패 처리 |
| `agents.py` | 역할 담당 | `PROMPTS[역할]`의 지시문 |
| `tools.py` | 역할 담당 | 파일 맨 아래 역할별 구역에 `@register` 도구 추가 |
| `nodes.py` | 정해야 함 | 코드 노드 4개: 전처리, 구매안 검증, 가격 검증, 검토 카드 |

## 흐름

```
START ─ preprocess ─ ① ─ STORED ─ ② ─ SEND_RFQ ─ 종료 (WAITING_REPLY, 백엔드가 발송)
REPLY ─ preprocess ─ ① ─ STORED ─ ③ ─ REQUEST_CLARIFICATION ─ 종료 (WAITING_REPLY)
                                    └ BUILD_PLANS ─ ④ ─ PLANS_PROPOSED ─ 구매안 검증
PLAN ───────────────────────────────────────────┘     FIX → ④ / NEED_INFO → ③ / PASS ↓
                    ⑤ ─ COUNTER_OFFER_PROPOSED ─ 가격 검증 ─ PASS ─ 검토 카드 (COMPLETE)
                                                          └ NEED_INFO → ③
③④⑤ RECHECK_SOURCE → ①   ④⑤ NEEDS_EVIDENCE → ③   모든 역할 HOLD → 보류 (ON_HOLD)
```

- 회신이나 승인을 기다리는 동안 그래프는 **종료된다.** 회신이 오면 백엔드가 `start_from=REPLY`로 다시 실행한다.
- 다음 단계는 **에이전트가 돌려준 `next_action`으로** 정해진다. 역할별로 고를 수 있는 값은 `…Out.next_action`의 `Literal`에 정해져 있고, 그 밖의 값은 검증에서 거부된다.

## 역할 담당이 할 일

### 1. 입출력 확인 — `schemas.py`

- **입력 `…In`:** ID만 들어온다(`case_id`, `input_revision`, 메일·견적 ID 등). 원문, 정책, 가격은 도구로 조회한다.
- **출력 `…Out`:** LLM의 최종 응답 형식이다. 아래 필드를 채운다.
  - `next_action`
  - `reason`: 1~500자의 짧은 근거
  - `unknown_fields`: 원문에 없어서 null로 둔 항목. ④⑤가 `NEEDS_EVIDENCE`를 고르면 이 목록이 그대로 ③에 전달된다.
  - 역할별 결과 필드

| 역할 | 입력 | 출력 | next_action |
|---|---|---|---|
| ① INFO_EXTRACTOR | `ExtractionIn` | `ExtractionOut` | STORED, HOLD |
| ② CANDIDATE | `CandidateIn` | `CandidateOut` | SEND_RFQ, HOLD |
| ③ INFO_PRIORITY | `PriorityIn` | `PriorityOut` | REQUEST_CLARIFICATION, BUILD_PLANS, RECHECK_SOURCE, HOLD |
| ④ RESPONSE_PLAN | `PlanIn` | `PlanOut` | PLANS_PROPOSED, NEEDS_EVIDENCE, RECHECK_SOURCE, HOLD |
| ⑤ COUNTER_OFFER | `CounterOfferIn` | `CounterOfferOut` | COUNTER_OFFER_PROPOSED, NEEDS_EVIDENCE, RECHECK_SOURCE, HOLD |

> `list[dict]`/`dict` 필드(`candidates`, `questions`, `plans`, `target`, `priority_result`, `validated_plans`)는 아직 내용이 정해지지 않았다. 역할 담당이 필요한 필드를 제안하면 오케스트레이션 담당이 모델로 확정한다.

### 2. 프롬프트 — `agents.py`의 `PROMPTS`

- 시스템 프롬프트는 `공통 POLICY + 역할 이름 + PROMPTS[역할]`로 만들어진다.
- 역할 목표, 도구를 언제 쓰는지, 어떤 경우에 어떤 `next_action`을 고르는지를 적는다. 각 항목 주석에 07 7절 요약과 선택 기준이 있다.
- 출력 JSON 형식은 스키마가 강제하므로 프롬프트에 다시 적을 필요가 없다.

### 3. 도구 — `tools.py`

- 도구 이름은 `ROLE_TOOLS`에 있는 것만 쓸 수 있다. 목록에 없는 이름은 `register`가 거부한다.
- 도구별 입출력은 `tools.py` 맨 위 표(07 4절)를 따른다. 작성 규칙도 그 docstring에 있다.
  - LLM이 넘긴 인자를 믿지 말고 업무 범위와 revision을 도구 안에서 검사한다.
  - 금액 계산은 도구가 Decimal로 한다.
  - 결과는 작게 돌려준다.
- 공통 도구 3개(`get_case_context`, `read_mail_evidence`, `save_agent_result`)는 한 사람이 먼저 만든다. 누가 만들지 정하고 시작한다.
- `create_mail_draft`는 ②③⑤가 같이 쓰므로 한 사람이 만든다.

### 4. 테스트

```bash
.venv/Scripts/python -m pytest tests/test_agent_*.py -q   # 에이전트 테스트
.venv/Scripts/python -m pytest -q                         # 전체 (PR 전)
```

- **LLM 없이 확인:** `tests/test_agent_runner.py`의 `FakeModel`처럼 정해진 tool call과 최종 응답을 순서대로 돌려주는 가짜 모델로 테스트한다.
- **실제 LLM으로 실행:** `.env`에 LLM 설정을 넣고 `agents.py` docstring의 예시를 실행한다.
- 실제 메일 원문이나 API 키는 커밋하지 않는다. 테스트 데이터는 가짜나 마스킹한 데이터만 쓴다.

## 이미 정해진 한도와 처리

| 상황 | 결과 |
|---|---|
| 한 실행에서 역할 호출 6회 초과 | FAILED `ROLE_CALL_LIMIT` |
| 한 에이전트 호출에서 도구 8회 초과 | FAILED `{역할}: AGENT_FAILED` |
| 모델·도구 예외 | FAILED `{역할}: AGENT_FAILED` |
| 최종 응답이 `…Out` 형식이 아님 | FAILED `{역할}: INVALID_OUTPUT` |
| 코드 노드 예외 | FAILED `{노드}: NODE_FAILED` |
| 실행 중 `input_revision`이 바뀜 | STALE: 결과와 초안을 쓰지 않고, 백엔드가 최신 revision으로 다시 실행 |

## 계약 변경

`schemas.py`의 필드, `next_action`, 도구 목록을 바꿔야 하면 직접 고치지 않는다. 오케스트레이션 담당에게 요청하거나 따로 PR을 올린다. 이 계약이 바뀌면 그래프와 다른 역할이 같이 깨지기 때문이다.

## 아직 정하지 않은 것

- `list[dict]` 필드의 구체 모델
- `nodes.py` 코드 노드 담당과 구현
- `ProcessingStep`을 `work_cases`로 옮길지 여부(PR #3과 맞춤)
- 백엔드에서 `run_case`를 호출하는 위치(업무 실행 API·워커)
