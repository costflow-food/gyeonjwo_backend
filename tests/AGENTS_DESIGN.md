# 견줘 에이전트 설계 (현재 구현 기준)

기준: `feature/agents` 브랜치, 2026-11-02 (PR #24 병합 시점, 커밋 `e403cab`).
이 문서는 **지금 코드에 구현된 것과 확정된 결정만** 적는다. 코드와 이 문서가 다르면 코드가 맞다.
상위 설계는 Notion 06(데이터 스키마)·07(에이전트 구성도)이다. 이 문서는 07을 실제 구현으로 옮긴 결과다.

코드 위치: `app/agents/`

| 파일 | 내용 |
|---|---|
| `schemas.py` | 역할별 입력(`*In`)·출력(`*Out`), 그래프 입출력(`RunCaseInput`/`RunCaseOutput`), 검토 카드(`ReviewCard`) |
| `tools.py` | 에이전트 도구, 공통 계산, 저장소 인터페이스(Protocol), 역할별 허용 도구(`ROLE_TOOLS`) |
| `agents.py` | 역할별 프롬프트(`PROMPTS`), 공통 규칙(`AGENT_POLICY`), 최종 검사(`FINAL_CHECKS`), 실행기(`AgentRunner`), 모델 생성(`chat_model`) |
| `nodes.py` | 코드 노드 4개(전처리·구매안 검증·가격 검증·검토 카드)와 그 저장소 인터페이스 |
| `graph.py` | LangGraph 그래프(`build_graph`, `run_case`) |

---

## 1. 핵심 원칙 (모든 역할 공통)

1. **LLM은 숫자를 계산하지 않는다.** 금액·수량·환산·총액·역전 기준·상한은 도구가 `Decimal`로 계산한다. LLM은 도구 결과를 보고 판단만 한다.
2. **최종 검사가 LLM 출력을 도구 결과로 다시 채운다.** LLM이 최종 응답에 옮겨 적은 ID·금액·배분은 믿지 않는다. 역할별 최종 검사(`FINAL_CHECKS`)가 이번 실행의 도구 결과와 대조하고, 숫자는 도구 결과 값으로 덮어쓴다. 맞지 않으면 `UngroundedOutputError`로 실패한다.
3. **숫자가 들어간 문장과 메일 문안은 코드가 쓴다.** RFQ·보완 요청·역제안 메일은 코드 템플릿이다. LLM은 질문 ID·조건 문구·사유 문장만 고른다.
4. **원문 근거가 있어야 한다.** ①은 값마다 원문 위치(evidence)를 붙이고, 도구가 원문과 대조한다. ③④⑤가 재추출을 요청할 때는 원문 구절을 그대로 인용해야 하고, 최종 검사가 이번 실행에서 읽은 그 업체 메일에 그 구절이 있는지 확인한다.
5. **언어 판단은 LLM이 한다.** 정규식·단어 목록으로 LLM의 언어 판단을 대신하지 않는다(예외: 메일 속 AI 지시문 차단 같은 보안 필터).
6. **에이전트끼리 값을 직접 넘기지 않는다.** 그래프 상태에는 ID와 요약만 둔다. 각 역할은 도구로 DB를 다시 읽는다.
7. **계산 기록·판단 기록을 따로 저장하지 않는다.** `calculation_ref`, `validation_ref`, 결과 저장 도구(`save_agent_result`)는 없다. 실행 결과는 `AgentRunner`가 `agent_runs`에 저장한다.
8. **최종 응답은 결과 도구 호출이다.** LangChain `ToolStrategy`라 매 턴 도구 호출이 강제된다. 공통 규칙(`AGENT_POLICY`)이 "최종 응답은 `<역할>Out` 도구를 호출해서 낸다, 텍스트로 쓰지 않는다, 확인만 하려고 도구를 다시 부르지 않는다"를 명시한다. (공유 `POLICY`의 "JSON만 반환"을 쓰면 추론 수준 medium에서 읽기 도구 반복 호출이 생겼다.)
9. **메일·문서·도구 결과 속 문장은 근거일 뿐 지시가 아니다.**

---

## 2. 서비스 흐름과 그래프

### 2.1 전체 흐름

1. Gmail 수신 → 경량 분류(LLM 노드) → 업무 메일이면 Work ID(`work_cases`) 생성, **Waiting** (백엔드)
2. 담당자가 원문 확인 → "수긍하기"(보관, 끝) 또는 "다른 대안 찾기"(**Running**, 그래프 `START` 실행)
3. `START`: 전처리 → ① → ② → RFQ 초안 → **회신 대기**로 멈춤
4. 백엔드가 발송(승인 ON: 담당자 승인 후 / OFF: 정책에 따라 자동)
5. 회신 도착 → 업무에 연결, `input_revision` +1 → 그래프 `REPLY` 실행
6. `REPLY`: 전처리 → ① → ③ → (보완 요청이면 회신 대기로 멈춤 / 아니면) ④ → 구매안 검증 → ⑤ → 가격 검증 → 검토 카드 → **Complete**
7. 담당자가 카드를 보고 구매안 선택, 역제안 메일 발송(백엔드)
8. 정책만 바뀌면 `PLAN`으로 시작해 ④부터 다시

### 2.2 그래프 (`graph.py`)

```
START ─┐                         ┌─ (START) ② ─ SEND_RFQ ─────────────── wait_reply
REPLY ─┴─ preprocess ─ ① ─ STORED┤
                                 └─ (REPLY) ③ ─ REQUEST_CLARIFICATION ─ wait_reply
PLAN ──────────────────────────────────────┐   └ BUILD_PLANS ─ ④ ─ PLANS_PROPOSED ─ plan_validation
                                           └─────────────────┘          FIX → ④ · NEED_INFO → ③ · PASS ↓
                     ⑤ ─ COUNTER_OFFER_PROPOSED ─┬ price_validation ─ PASS ─ review_card (COMPLETE)
                       └ COUNTER_OFFER_SKIPPED ──┘                 FIX → ⑤ · NEED_INFO → ③
                       └ HOLD ──────────────────────────────────────────── review_card (COMPLETE)
③④⑤ RECHECK_SOURCE → ① · ④⑤ NEEDS_EVIDENCE → ③ · ①~④ HOLD → hold (ON_HOLD)
```

- **분기 기준:** 역할 출력의 `next_action`, 검증 노드의 `PlanCheck`/`PriceCheck`(PASS·FIX·NEED_INFO).
- **⑤ HOLD도 검토 카드로 간다.** 구매안은 이미 검증을 통과했으므로 카드에 구매안·역제안 보류 사유·참고 계산을 보인다.
- **RECHECK_SOURCE → ①:** ①은 이번 실행의 새 메일이 아니라 **재추출 근거로 인용된 메일**(`recheck_mail_ids`)을 다시 읽는다. 다시 볼 항목은 `recheck_fields`.
- **③④⑤ 호출 직전**에 업무의 업체별 최신 견적 ID 전체로 `quote_version_ids`를 채운다(`latest_quotes`).
- **멈춤·종료 상태(`RunStatus`):** `WAITING_REPLY`(초안 생성, 백엔드 발송 대기), `COMPLETE`(카드 저장), `ON_HOLD`(①~④ 보류), `STALE`(실행 중 `input_revision` 변경. 에이전트 호출마다, 카드 저장 직전에 확인), `FAILED`(에이전트·노드 예외, 형식 위반, 역할 호출 한도 초과).
- **한도:** 한 실행의 역할 호출 `MAX_ROLE_CALLS_PER_RUN = 6`, 한 역할 호출 안의 도구 호출 `MAX_TOOL_CALLS_PER_AGENT_CALL = 8`(넘으면 그 호출 실패).
- **그래프 상태 주요 키:** `quote_version_ids`, `candidates`, `draft_ids`(누적), `questions`, `pending_questions`, `recheck_fields`, `recheck_mail_ids`, `plans`, `counter_offer`(목표), `counter_offer_result`(⑤의 next_action·reason·reference·draft_ids), `plan_check(_reason)`, `price_check(_reason)`, `unresolved`(누적), `steps`(누적).

### 2.3 실행기 (`AgentRunner`)

- 역할마다 `create_agent(model, 허용 도구, system_prompt, response_format=ToolStrategy(<역할>Out), ToolCallLimitMiddleware(8))`.
- 시스템 프롬프트 = `AGENT_POLICY` + `Role: <역할>` + `PROMPTS[역할]`.
- 실행 기록(`agent_runs`)을 시작·종료한다. 완료 시 최종 출력과 참고한 견적 ID(`source_quote_ids`)를 저장하고, 실패 시 그때까지의 도구 호출(`partial_tool_calls`)을 남긴다(`ToolCallLog`).
- 도구에는 `RunContext(case_id, input_revision, agent_run_id, review_batch_id)`가 `runtime.context`로 들어간다. **case_id·input_revision은 LLM 인자가 아니다.**

---

## 3. 역할별 사양

각 역할의 출력은 공통으로 `next_action`, `reason`(담당자가 읽는 한국어 문장, 영문 코드·필드 이름 금지), `unknown_fields`를 가진다. 모든 역할은 `get_case_context`, `read_mail_evidence`를 쓸 수 있다.

### ① 정보 추출 (`INFO_EXTRACTOR`)

- **입력:** `ExtractionIn(mail_ids, recheck_fields)`
- **도구:** `store_structured_mail(fields, price_tiers, cost_terms, item_notes)`
- **LLM이 하는 것:** 메일·첨부 쪽을 골라 읽고, 값마다 원문 그대로의 `raw_value`와 근거(`evidence: mail_id, part, page, cell`)를 보낸다. 비고(`item_notes`)를 고르고 분류(RECEIVING·QUALITY·PACKING·PAYMENT·SUPPLY·PRICE_NOTICE)한다. 세금 조건이면 `tax_basis`(INCLUDED·EXCLUDED·EXEMPT)를, 비용마다 `included_in_price`(true·false·null)를 정한다.
- **코드가 하는 것:** 원문 대조, 숫자·단위·날짜 정규화, 업체 확인, 이전 버전 값 이어받기, 새 견적 버전 저장. 거절되면 `rejected`(항목·코드)와 `hints`(고치는 법)를 돌려준다. 메일 속 AI 지시문 문장은 비고에서 뺀다(`INSTRUCTION_NOT_NOTE`).
- **저장:** `quote_versions`(단가·단위·공급량·`checks_jsonb`(규격·인증·납기·`tax_basis`)·`pricing_terms_jsonb`·`cost_terms_jsonb`·`item_notes_jsonb`), `price_history`, `mail_messages.normalized_jsonb`(인상 전 단가·인상 사유 등 견적이 아닌 값과 근거), `work_cases`의 비어 있는 품목명·규격.
- **next_action:** `STORED`(→ START면 ②, 아니면 ③), `HOLD`.
- **최종 검사(`check_extraction`):** `quote_version_ids`는 이번 실행에서 실제 저장된 ID만. `unknown_fields`는 필드 이름만 남긴다.

### ② 후보 선정 (`CANDIDATE`)

- **입력:** `CandidateIn(quote_version_ids)`
- **도구:** `search_supplier_items(keywords)` → `search_supplier_evidence(supplier_item_ids)` → `create_rfq_draft(supplier_item_ids, supplier_ids, questions)`. (`crawl_supplier_candidates`는 허용 목록에만 있고 **미구현**, Should)
- **LLM이 하는 것:**
  - 업무 품목명에서 검색어를 정하고(최대 3번 재검색), 검색 결과에서 **같은 품목**만 고른다(표기가 달라도 같으면 선택, "딸기잼" 같은 다른 품목 제외).
  - 업체마다 추가로 물을 선택 질문을 ID로 고른다(`MOQ`, `VALIDITY`, `TAX`, `PACKING`, `ORIGIN`, `DELIVERY_DAYS`). 고르지 않은 업체는 기본(`MOQ`, `VALIDITY`). 고정 질문(단가·단위, 공급 가능 여부, 납기 가능 여부, 운송비 부담·포함 여부)은 항상 들어간다.
  - **주의(caution):** 후보 **자신의** 등록 비고(`supplier_items.notes`)에 요청을 그대로 받기 어려운 내용(배송 요일·최소 주문 등)이 있으면 표시한다. 주의 업체도 후보에 남고, 승인 OFF에서도 자동 발송만 보류한다(백엔드 D3, 미구현).
  - 업체별 사유 한 문장.
- **코드가 하는 것:** 기존 업체(인상 요청 업체) 제외, 필수 조건 판정(PASS·FAIL·UNKNOWN, 규격은 정확히 같을 때만 PASS), 선호 점수(원산지 국내산 2점, 거래 이력 1점), 후보 순서, RFQ 문안(회사명·소개·서명은 `mail_send_settings.style_jsonb`). **기존 업체의 비고나 인상 사유는 후보 선정에 쓰지 않는다.**
- **next_action:** `SEND_RFQ`(→ 회신 대기), `HOLD`(후보 없음).
- **최종 검사(`check_candidates`):** 후보는 `search_supplier_evidence` 결과에 있는 업체만, 초안은 이번 실행의 DRAFTED만. 체크리스트·점수·근거·순서는 도구 값으로. 주의는 비고가 있는 업체만 남기고 근거를 그 비고 참조로 맞춘다. 전체 사유는 `create_rfq_draft`의 `summary` 문장.

### ③ 확인 우선순위 (`INFO_PRIORITY`)

- **입력:** `PriorityIn(quote_version_ids, pending_questions)`
- **도구:** `check_comparability()` → (필요 시) `calculate_break_even(supplier_id)` → `create_clarification_draft(requests)`
- **`check_comparability` 결과(③④⑤ 공통):** 업체별 업무 수량 기준 `known_total`, `blocked`(계산을 막는 사유), `unknown_costs`(field `cost:<type>`), `missing_required`(①의 필드 이름: specification, certifications, available_quantity, delivery_date, unit_price/unit/price_tiers/currency, tax_terms), `requirements`(MET 또는 LLM이 판단할 CHECK), `is_incumbent`, 그리고 `next_steps`(해야 할 일).
- **LLM이 하는 것:**
  - `missing_required`가 있으면 그 업체 메일 원문을 다시 읽는다. **원문에 답이 있으면** 업체에 묻지 않고 `recheck`(supplier_id, field, mail_id, source_text=원문 구절 그대로)로 `RECHECK_SOURCE`. 원문에도 없으면 `MUST_CONFIRM`으로 묻는다.
  - 미확인 비용은 `calculate_break_even`으로 확인한다. `can_change_choice`면 `CAN_CHANGE_CHOICE`로 묻고, `proven_low_value`면 묻지 않고 `LOW_VALUE`로 기록한다. 둘 다 아니면 묻지 않고 `unknown_fields`에 남긴다.
  - 물을 것이 있으면 `create_clarification_draft`를 한 번 부른다(질문 문장·문안은 코드).
- **next_action:** `RECHECK_SOURCE`(→ ①, 질문보다 먼저), `REQUEST_CLARIFICATION`(→ 회신 대기), `BUILD_PLANS`(→ ④), `HOLD`.
- **최종 검사(`check_priority`):** 질문의 업체·항목이 그 업체의 누락·미확인 비용인지, 우선순위가 역전 기준 결과로 입증되는지 확인. `break_even_amount`·`formula`는 도구 값으로 채운다. 재추출 근거는 인용 구절이 이번 실행에서 읽은 그 업체 메일에 있어야 한다(`_grounded_recheck`). 묻지 않은 필수 누락은 `unknown_fields`에 기록.

### ④ 대응안 (`RESPONSE_PLAN`)

- **입력:** `PlanIn(quote_version_ids, questions, plan_check_reason)`
- **도구:** `check_comparability()` → `build_plan_candidates(met_requirements)` → `validate_purchase_plan(plan_type, allocations, mode, met_requirements)`
- **LLM이 하는 것:**
  - `requirements` 중 CHECK 항목(예: "식품안전관리인증" = HACCP?)을 원문을 보고 충족 판단해 `met_requirements`로 넘긴다. 판단할 수 없으면 넣지 않는다(그 업체는 빠진다).
  - 보일 안을 고른다. 기본은 유지안·가장 싼 전환안·가장 싼 분산안. 비용 외 이유가 있으면 배정량을 정해 `mode=quantities`로 검증한다(도구가 MOQ·발주 배수·공급량에 맞게 가장 가까운 값으로 조정). 안은 최대 5개.
  - `plan_check_reason`(구매안 검증 FIX)이 있으면 그 위반을 고친다.
- **코드가 하는 것:** 업체 제외(필수 조건 미충족·불명확, 비기존 업체의 미확인 비용), 기존 업체만 미확인이 있으면 조건부(CONDITIONAL) 유지안, KEEP·SWITCH 상위 3·SPLIT 상위 3 후보, 한 업체 전량보다 싸지 않은 분산안 제외(`NOT_CHEAPER_THAN_SINGLE`), 정책(`allow_split`, `max_suppliers` 기본 2) 적용. 가능한 안이 없으면 `next`에 HOLD 안내.
- **next_action:** `PLANS_PROPOSED`(→ 구매안 검증), `NEEDS_EVIDENCE`(→ ③, `unknown_fields`에 `<supplier_id>.<field>`), `RECHECK_SOURCE`(→ ①), `HOLD`.
- **최종 검사(`check_plan`):** 안마다 이번 실행의 `validate_purchase_plan` 결과(VALID·CONDITIONAL)가 있어야 한다. 배정량·견적 버전·총액·검증 결과·성립 조건·조정 내역은 그 결과로 채운다. 같은 안은 하나만.

### ⑤ 역제안 (`COUNTER_OFFER`)

- **입력:** `CounterOfferIn(quote_version_ids, validated_plans, price_check_reason)`
- **도구:** `calculate_counter_offer(alternative)` → `validate_counter_offer(target_unit_price, alternative)` → `create_counter_offer_draft(target_unit_price, alternative, conditions)`
- **비교할 대안:** 기본은 `validated_plans` 중 가장 싼 비유지 안(SWITCH·SPLIT, VALID). 다른 안을 고르면 이유를 `reason`에. 기존 업체가 일부 포함된 분산안도 대안이 될 수 있다(기존 업체만 있는 유지안은 불가).
- **`calculate_counter_offer` 계산:**
  - 요청 단가(기존 업체 최신 견적), 인상 전 단가(①이 저장한 `normalized_jsonb`, `get_increase_terms`), 대안 총액(최신 견적으로 재계산).
  - 유지 상한 단가(`keep_price_ceiling`): `(대안 총액 − 기존 업체의 단가 외 확인 비용) ÷ 견적 단위 수량`. 이 단가 이하면 기존 업체가 대안보다 싸거나 같다.
  - 정책 범위(`work_cases.policy_jsonb.counter_offer`, 아래 6절)와 유지 상한의 교집합 = 허용 범위 `allowed_range`.
  - `counter_status`: `PROPOSABLE`(허용 범위 있음), `NO_POLICY`, `NOT_NEEDED`(요청 단가가 이미 상한 이하), `NOT_COMPETITIVE`(인상 전 단가로도 대안보다 비쌈), `NOT_COMPUTABLE`, `COSTS_UNKNOWN`(기존 업체 단가 외 비용 미확인), `PREVIOUS_UNKNOWN`(인상 전 단가 근거 없음). `skip_allowed`는 정책의 `skip_when`에 그 상태가 있을 때 true.
- **LLM이 하는 것:** `PROPOSABLE`이면 허용 범위 안에서 목표 단가와 조건(예: "12월 물량 전량 발주 시")을 정한다. **회사 정책이 없으면 목표를 만들지 않는다("합리적 인상률" 추정 금지).**
- **코드가 하는 것:** 범위 강제, 초안 도구 안에서 다시 검증(통과할 때만 초안), 메일 문안(목표 단가와 조건만. **다른 공급처 이야기·검토 중이라는 사실·대안 가격은 넣지 않는다**).
- **next_action과 조건:**

  | next_action | 언제 | 다음 |
  |---|---|---|
  | `COUNTER_OFFER_PROPOSED` | 검증된 목표와 이번 실행의 초안이 있음 | 가격 검증 |
  | `COUNTER_OFFER_SKIPPED` | `NOT_NEEDED`·`NOT_COMPETITIVE`이고 `skip_allowed` | 가격 검증 |
  | `NEEDS_EVIDENCE` | `COSTS_UNKNOWN` | ③ |
  | `RECHECK_SOURCE` | `PREVIOUS_UNKNOWN`인데 인상 전 단가가 원문에 있음(구절 인용) | ① |
  | `HOLD` | `NO_POLICY`, `NOT_COMPUTABLE`, 대안 없음, 원문에도 인상 전 단가 없음 | 검토 카드 |
- **최종 검사(`check_counter_offer`):** `reference`(참고 계산: 상태·요청 단가·인상 전 단가·유지 상한·대안 총액·허용 범위)는 마지막 `calculate_counter_offer` 결과로 채운다(보류·생략에도 남는다). PROPOSED는 같은 목표 단가의 이번 실행 초안이 있어야 하고, 목표의 업체·대안·근거 견적·조건은 그 초안 결과로 채운다. SKIPPED는 `skip_allowed`가 true여야 한다.
- **자동 다회차 협상은 하지 않는다.** 초안은 한 번.

---

## 4. 코드 노드 (`nodes.py`)

LLM을 쓰지 않는다. 그래프에 `Nodes(...)`로 주입한다.

| 노드 | 하는 일 | 반환 |
|---|---|---|
| `preprocess` | 이번 실행 메일의 아직 읽지 않은 첨부를 받아 쪽 단위 텍스트로 저장(`DocumentStore`). 텍스트 PDF는 쪽마다, txt·csv(UTF-8 → CP949), HTML, xlsx는 시트마다(셀 위치 `C2: 5500` 포함). **OCR은 하지 않는다**(글자 없는 PDF는 `NO_TEXT`). 20MB 초과 `TOO_LARGE`, 그 외 `UNSUPPORTED`·`FAILED`. 받아오지 못한 첨부는 저장하지 않아 다음 실행에서 재시도 | 읽지 못한 첨부 사유를 `unresolved`에 |
| `plan_validation` | 구매안마다 최신 견적으로 총액·공급량·MOQ·발주 배수·분산 정책·세금 기준을 다시 계산(`revalidate_plans`) | PASS / FIX(위반 → ④) / NEED_INFO(견적이 최신 아님 → ③) |
| `price_validation` | 역제안 목표(또는 생략)를 최신 견적과 정책으로 다시 확인(`revalidate_counter_offer`) | PASS / FIX(범위 밖·허용되지 않은 생략 → ⑤) / NEED_INFO(→ ③) |
| `review_card` | 상태에서 `ReviewCard`(구매안, ⑤ 결과 요약, ③ 질문, 미확인, 역할 기록)를 만들어 저장(`ReviewCardStore.save_review_card`) | `card_id`. 저장소가 revision 변경으로 거절하면 그래프가 STALE |

---

## 5. 공통 계산 (`tools.py` "공통 계산" 절)

③④⑤와 검증 노드가 같은 함수를 써서 같은 숫자가 나온다.

| 함수 | 내용 |
|---|---|
| `cost_sheet` | 견적 한 행을 업무 수량·단위 기준으로 계산: 단가(구간 단가 포함), 상품 금액, 확인된 비용, 미확인 비용, 계산 차단 사유, 경고(SUPPLY_SHORT·BELOW_MOQ·NOT_MULTIPLE) |
| `unit_ratio` | 단위 환산. 박스↔kg은 견적에 박스 중량이 적혀 있을 때만 |
| `tax_basis` | `checks_jsonb.tax_basis`(INCLUDED·EXCLUDED·EXEMPT). 세금 기준이 다르면 비교하지 않는다 |
| `order_limits` | MOQ·발주 배수·최대 공급량 |
| `tier_price` | 수량에 맞는 구간 단가 |
| `plan_total` | 구매안(업체별 배정량) 총액과 위반 |
| `adjust_split`, `cheapest_split` | 분산 배정량을 가장 가까운 가능한 값으로 조정 / 가장 싼 분산 |
| `break_even` | 미확인 비용 역전 기준 금액과 산식 |
| `keep_price_ceiling` | 유지 상한 단가 |

필수 비용 항목은 `SHIPPING`(운송비)이다. 적혀 있지 않으면 0원이 아니라 미확인이다("없음"·"무료"라고 적힌 경우만 0원).

---

## 6. 회사 정책 (`work_cases.policy_jsonb`)

| 키 | 쓰는 곳 | 뜻 |
|---|---|---|
| `allow_split` | ④ | false면 분산안 없음 |
| `max_suppliers` | ④ | 분산 업체 수 상한(기본 2) |
| `counter_offer.enabled` | ⑤ | true일 때만 정책 사용 |
| `counter_offer.target_range` | ⑤ | 지금은 `BETWEEN_PREVIOUS_AND_REQUESTED`만(인상 전 단가 이상, 요청 단가 미만) |
| `counter_offer.max_increase_rate` | ⑤ | 인상 전 단가 대비 최대 인상률(예: "0.03") |
| `counter_offer.skip_when` | ⑤ | 역제안 생략을 허용하는 상태 목록(예: `["NOT_COMPETITIVE"]`) |

`counter_offer`의 구조는 구현됐지만 **값은 팀 합의 전(C4)** 이다. 테스트는 합성 정책으로 한다.

---

## 7. 저장소 인터페이스 (DB 연결 대상, D4)

도구·노드는 DB를 직접 부르지 않고 Protocol을 거친다. 지금은 테스트용 메모리 저장소(`tests/agent_memory_store.py`)만 있고, **실제 Supabase 구현이 필요하다.** 함수 하나 = RPC 하나, 소유·업무 범위와 revision 확인은 구현이 한다.

| Protocol | 함수 | 06 테이블 |
|---|---|---|
| `CaseStore` | `current_input_revision`, `get_work_case`, `list_case_mails`(첨부에 `status` 포함), `read_mail_part` | work_cases, mail_messages |
| `QuoteStore` | `resolve_supplier`, `latest_quote_version`, `insert_quote_version`(quote_versions·price_history·normalized_jsonb·work_cases 조건·workflow_logs 한 트랜잭션) | suppliers, quote_versions 등 |
| `CandidateStore` | `get_candidate_conditions`, `incumbent_supplier_ids`, `search_supplier_items`(품목명 부분 일치, 상한), `get_supplier_items`, `get_supplier`, `list_purchase_history` | supplier_items, suppliers, purchase_history |
| `MailDraftStore` | `current_send_settings`(style_jsonb 등), `insert_mail_draft`(멱등 키) | mail_send_settings, mail_drafts |
| `ComparisonStore` | `get_quote_versions(case_id, ids)` | quote_versions |
| `CounterOfferStore` | `get_increase_terms(case_id, supplier_id)`(인상 통보 메일의 `normalized_jsonb.fields`에서 인상 전 단가·사유) | mail_messages |
| `AgentRunStore` | `start_agent_run`, `finish_agent_run` | agent_runs |
| `ReviewCardStore` | `save_review_card`(revision 확인 + `latest_review_jsonb` 저장 + Complete + `workflow_logs` REVIEW_CREATED, 한 트랜잭션) | work_cases, workflow_logs |
| `DocumentStore` | `list_unread_attachments`, `download_attachment`(Gmail 첨부), `save_document`(`normalized_jsonb.documents`) | mail_messages |

도구 쪽 저장소는 `tools.use_case_store`, `use_quote_store`, `use_candidate_store`, `use_draft_store`, `use_priority_store`, 노드 쪽은 `nodes.use_card_store`, `use_document_store`로 연결한다(앱 시작 시 연결은 아직 안 됨).

---

## 8. 설정

| 환경 변수 | 뜻 |
|---|---|
| `LLM_MODEL`, `LLM_MODEL_<ROLE>` | 기본 모델, 역할별 모델(비우면 기본) |
| `LLM_REASONING_EFFORT`, `LLM_REASONING_EFFORT_<ROLE>` | 추론 수준(비우면 보내지 않음). 값이 있으면 Responses API로 호출 |

- `<ROLE>`: `INFO_EXTRACTOR`, `CANDIDATE`, `INFO_PRIORITY`, `RESPONSE_PLAN`, `COUNTER_OFFER`.
- 응답은 OpenAI에 저장하지 않는다(`store=False`).
- 지금 검증한 조합: `gpt-5.4-mini`, 추론 `medium`. (gpt-5.4-mini는 Chat Completions에서 도구와 추론 수준을 함께 받지 않아 Responses API를 쓴다.)

---

## 9. 테스트

- **위치:** `tests/` (저장소에 올리지 않고 로컬에서 관리). 데이터는 반드시 `tests/data/*.json`에 두고 `tests/agent_test_data.py`로 읽는다. 테스트 코드 안에 데이터를 넣지 않는다.
- **가짜 LLM 테스트:** `GenericFakeChatModel`로 도구 호출 순서를 정해 두고 최종 검사·노드·그래프를 검증한다. `tests/test_agent_flow.py`는 한 업무를 세 번 실행(인상 통보 → RFQ / 회신 → 보완 요청 / 보완 회신 → 검토 카드)한다.
- **실제 LLM 시나리오:** `RUN_LIVE_LLM=1`일 때만 실행. 결과는 `tests/results/<agent>/live_<모델>_<시각>.json·md`(폴더별 README 목록). `LIVE_NOTE`로 회차 설명.
- **현재 결과:** 단위 457 통과. 실제 LLM(gpt-5.4-mini, medium): ① 23/23, ③ 6/6, ④ 6/6, ⑤ 4/4, 전체 흐름 3/3.
- 데이터는 모두 합성이다. 실제 메일 원문을 쓰지 않는다.

---

## 10. 하지 않는 것 (결정됨)

- 정규식·단어 목록으로 LLM의 언어 판단(비고 여부, 품목 동일성, 날짜 의미 등)을 대신하지 않는다.
- 계산 기록·판단 기록을 따로 저장하지 않는다. `*_ref` 칸을 만들지 않는다. 값은 값 칸으로 둔다.
- LLM이 금액·배정량을 계산하거나 지어내지 않는다. 미확인 비용을 0원으로 보지 않는다.
- ②에서 기존 업체의 비고·인상 사유로 후보를 정하지 않는다.
- ④에서 필수 조건 미충족·불명확 업체, 미확인 비용이 있는 비기존 업체를 안에 넣지 않는다. "최적"이라고 쓰지 않는다(탐색 범위 안의 결과).
- ⑤에서 정책 없이 목표 단가를 만들지 않는다. 메일에 다른 공급처 이야기를 넣지 않는다. 자동 다회차 협상을 하지 않는다.
- 과거 단가(`purchase_history`)를 현재 비교 단가로 쓰지 않는다.
- 첨부 OCR을 하지 않는다(텍스트 PDF만).
