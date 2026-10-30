# ③·④·⑤ 공통 구조 이관 및 외부 검토 비용 명세 보완 — 2026-10-30

이 절이 아래 과거 기록보다 우선한다. 기존 checkout/feature/agents-4-5의 HEAD는
`85501db870d69da226a911992532b33145b2312e`다. 팀 기준 origin/feature/agents는
`1185b120e8e3dd57d5d86c073ea71a38f0b520d1`. 기존 미커밋 결과와 7파일 최종 검증을 공통 코드로 이관해 보존했다.
이번 단계 실제 모델·DB·메일 호출, commit/push/PR, 새 checkout/폴더/실행기/ZIP/보고서는 없다.
`graph.py`와 `AgentRunner` 본문은 기준과 동일하다(AST 확인). Runner 최종 등록은 담당자 영역이다.

## Draft PR 준비 — 대상 feature/agents-integration

원격 fetch 후 확인한 대상 SHA는 `1185b120e8e3dd57d5d86c073ea71a38f0b520d1`이며 현재 HEAD
`85501db870d69da226a911992532b33145b2312e`의 직접 기준/merge-base다. 대상에만 있는 추가 커밋은 없다.
비교 대상은 기존 HEAD 커밋과 보존된 미커밋 변경 전체다. 실제 commit/push/PR은 아직 수행하지 않았다.

예정 PR diff는 총 11개 경로(수정 4개/추가 7개/삭제 0개)다. 수정은 app/agents/{agents,nodes,schemas,tools}.py,
추가는 이 문서, tests/agent_memory_store.py, tests/fixtures/agents45_normal13.json,
tests/test_agents345_priority.py 및 tests/test_agents45_{price_tiers,product_registration,team_data}.py다.
fixture는 기존 정상 13 시나리오의 합성 데이터이며 test_agents45_product_registration.py에서 실제 읽는다.
HEAD 대비 삭제한 kan60 파일은 대상 브랜치에 애초에 없으므로 PR 삭제로 나타나지 않는다.
기존 ①·②/공통 도구 함수와 기존 스키마 정의를 보존했다. agents.py의 기존 build_agent/check_grounded 변경은
모듈 import에 맞춘 도구 참조 변경이다. AgentRunner 본문과 graph.py는 대상과 동일하다.
이 문서의 공통 담당자 연결 요청 표까지 최신 내용으로 PR에 포함하고,
AGENTS45_REVIEW.patch는 문서 정리 직전 HEAD 대비 스냅샷으로 보존하여 PR 대상에서 제외한다.

Draft PR 제목 초안: `feat(agents): implement shared info priority, response plan and counter offer`

PR 설명 초안:

- ③ INFO_PRIORITY의 공통 비교·역전 기준·질문 우선순위와 ④ RESPONSE_PLAN·⑤ COUNTER_OFFER의
  계산/독립 검증을 공통 agents.py/tools.py/schemas.py/nodes.py 구조에 연결한다.
  원문 가격·비용 포함/수량당 비용·단가 구간·포장 배수·배분 재가격·참조 무결성을 검사한다.
  최종 결과 검사는 읽기 전용이며 잘못된 금액/배분/참조/초안을 거부한다.
- 실행 범위는 실제 팀 AgentRunner·등록 도구·그래프/기본 검증 노드의 오프라인 경로다.
  모델·Store·초안 생성/조회는 명시적인 대역이며 실제 LLM·DB·메일 통합 결과가 아니다.
  전체 `pytest tests -k agent`를 기존 오프라인 검사기로 한 번 실행해
  **630 passed / 20 skipped / 4 xfailed / 0 failed / 282 deselected**를 확인했다(check-55).
  20 skip은 실제 모델 시험이고 4 strict xfail은 Runner 저장 전 검사 미등록 재현이다. 중복 실행 수는 합산하지 않는다.
- 공통 담당자 미연결: (1) AgentRunner.__call__의 check_grounded 후/COMPLETED 저장 전 check_final_output 등록,
  (2) 실제 역할 Store 어댑터와 source/revision/견적 범위의 원자적 조건부 저장,
  (3) 공통 create_mail_draft 정식 생성과 DraftLookupStore 조회,
  (4) 초기 PLAN 및 부분 회신 후 같은 업무/revision의 전체 최신 견적 집합 전달.
  구체적인 함수·모델·위치·연결 후 기존 검사는 이 문서의 대응표에 남긴다.
- LOW_VALUE는 계산 참조/판단 근거만 평가 기록에 보존하고 문의·초안에서 제외한다.
  LOW_VALUE만 남아도 현재 ④의 미확인 비용 조건부 구매안 계약이 없어 내부 제약 HOLD다.
  미확인 비용을 0원으로 확정하거나 업체를 제외해 BUILD_PLANS로 넘기지 않는다.
  공통 계약 연결과 실제 모델 시험은 후속이며 이 Draft에서 완료로 주장하지 않는다.

## 현재 변경 파일과 실행 경로

| 경로 | 구현 범위 |
|---|---|
| app/agents/agents.py | ③ 프롬프트, ④⑤ 공통 비교 지침, 읽기 전용 check_final_output/check_priority_output |
| app/agents/tools.py | 기존 계산·원문·단가 구간·포장·재가격·독립 검증 이관, ③ 정식 도구, 공통 비교/비용 규칙, 역할 Store Protocol |
| app/agents/schemas.py | 계산/사실/참조 typed 모델, BreakEvenRecord, AllocationWarning, ClarificationDraftBinding |
| app/agents/nodes.py | 기존 기본 구매/가격 검증 노드가 최종 검사 호출. PASS/FIX/NEED_INFO 또는 내부 의존성 HOLD |
| tests/agent_memory_store.py | 명시적인 메모리 Store 대역과 조건부 민감도 저장/조회 대역 |
| tests/test_agents45_product_registration.py | 정식 등록/typed 인자, 기존 위조·최종 검사 회귀 및 strict xfail 4건 보존 |
| tests/test_agents45_team_data.py | 원문/업무/revision/견적/참조, 공통 반올림 및 잘못된 총액 차단 |
| tests/test_agents45_price_tiers.py | 기존 구간·포장·배분 재가격·독립 단가 검사 보존 |
| tests/test_agents345_priority.py | ③ 저장/검증, 실제 Runner/그래프/노드 경로, 초기/부분 회신 누락 재현 |
| docs/AGENTS45_DELIVERY_ALIGNMENT.md | 이 기존 인계 문서 갱신 |

kan60/ 추적 파일 21개는 기능 이관·import/테스트 전환 후 제거했다. EvidenceRepository/TeamReviewRepository,
제품의 HumanMessage 입력 복원, 별도 임시 dict 입력 계약, 미사용 독립 LLM 호출기/전용 초안 경로는 제거했다.
공통 초안 내용 검증까지 구현했다는 뜻은 아니다. 실제 공통 계약·어댑터 연결은 아래 잔여다.

agents.py PROMPTS[INFO_PRIORITY] → 기존 build_agent/AgentRunner → tools.py ROLE_TOOLS/@register를 사용한다.
기존 graph.py INPUTS/OUTPUTS가 PriorityOut.questions→PlanIn.questions, PlanOut.plans→CounterOfferIn.validated_plans를 전달한다.
기존 routes의 BUILD_PLANS→④, ④⑤ NEEDS_EVIDENCE→③, RECHECK_SOURCE→①, REQUEST_CLARIFICATION→WAITING_REPLY를 유지한다.
보완 후 새 revision/REPLY로 재진입한다. 실제 기본 검증 노드는 check_final_output을 읽기 전용 호출한다.

## 이번 원문 표현 보완: 비용 포함 설명의 ‘단가’ 구분

기존 tests/test_agents345_priority.py에 동일 견적의 “배송비 상품가격에 포함”/“배송비 단가 포함”을
그대로 넣어 실제 등록 도구→④ 구매안 경로를 재현했다. check-53은 1 passed / 1 failed였고,
실패는 단가 포함 표현의 RECHECK_SOURCE, unknown_fields=[unit_price]였다. 테스트 표현을 대체하지 않았다.
앞선 단계에서 비용 표현을 바꿔 피했던 가격 검사기 결함을 이번에 tools.py에서 수정했다.

team_bridge_team_data_numeric_field_source는 배송비/물류비 등 비용 문맥에서 ‘단가’ 바로 뒤가
포함/미포함 설명인 앵커를 숫자 가격 제시로 세지 않는다. 그 설명 뒤의 숫자를 단가로 차용하지도 않는다.
실제 숫자 단가 앵커는 별도로 있어야 하며, 기존 단일 가격 구절·필드 귀속·숫자 경계·원문 값 일치 검사를 유지한다.
포함 설명만 있는 원문, 15500 안의 5500, 다른 필드/참고값 차용, 실제 6500과 추출 5500 불일치,
서로 다른 실제 단가 구절, 음수, 줄을 이어 만든 숫자는 계속 거부한다.

두 표현 모두 같은 견적 집합으로 PLANS_PROPOSED가 되고, ⑤ 재가격/검증 후 실제 초안 미연결 HOLD까지 확인했다.
추가 가산액은 0원이지만 원래 비용 amount=None은 보존한다. 제품 변경은 tools.py의 이 검사에 한정했고,
기존 구조 이관·원문 검증·포함 비용·LOW_VALUE 처리를 유지했다. Runner 본문·graph.py는 변경하지 않았다.
검사는 원문 숫자 및 포함 비용의 ④⑤ 영향 범위만 실행했다. 현재 결과는 아래 check-54이며,
기존 4 xfailed는 이번 선택 범위 밖이고 Runner 등록 전까지 미해결 그대로다.

## 직전 외부 재검사 보완: 포함 비용과 LOW_VALUE 문의 제외

이번 변경은 tools.py, agents.py, 기존 tests/test_agents345_priority.py 및 이 문서/기존 패치에 한정했다.
구조 이관과 기존 미커밋 변경을 보존했으며 Runner 본문·graph.py·Store 계약·스키마 필드는 변경하지 않았다.

- period_cost는 원문 included_in_price=True 대조와 제공된 금액/기준 검증 뒤, amount 누락 분기보다 먼저
  추가 가산액 0원을 반환한다. 별도 amount=None은 그대로 보존한다. 이는 미확인 비용의 0원 대체가 아니다.
  포함 여부 불명/별도 비용의 금액 누락은 미확인, 포함 여부 위조·원문 변경·제공 금액 위조는 RECHECK_SOURCE다.
  SHIPPING/HANDLING 모두 ④ 구매안과 ⑤ 재가격 경로로 확인했다.
- questions는 LOW_VALUE를 포함한 평가 기록 전체다. inquiry_question_groups/question_groups는
  MUST_CONFIRM/CAN_CHANGE_CHOICE만 실제 문의 대상으로 묶는다. LOW_VALUE의 실제 계산 참조와 판단 근거는 보존한다.
  ③ 프롬프트도 LOW_VALUE의 계산 저장 후 평가 기록 보존과 초안 제외를 명시한다.
- 최종 검사는 평가 기록 전체의 업체·필드·근거·계산 참조를 검증하고, 초안은 문의 대상 부분집합과 정확히 대조한다.
  LOW_VALUE가 섞인 초안, LOW_VALUE만으로 만든 보완 요청/초안, 평가 기록 누락, 임의 BUILD_PLANS를 거부한다.
  읽기 전용 검사를 반복해도 참조·초안·결과 저장은 늘지 않는다.
- LOW_VALUE만 남으면 next_action=HOLD, draft_required=False, draft_ids=[]이고 초안 도구/조회 의존성을 요구하지 않는다.
  unknown_fields는 LOW_VALUE_UNCONFIRMED_COST_PLAN_CONTRACT_REQUIRED다. 실제 팀 그래프는 ON_HOLD로 종료하며
  REQUEST_CLARIFICATION/WAITING_REPLY를 만들지 않는다. graph.py의 unresolved는 이전 ④ cost_terms와 HOLD 사유를
  누적하지만 최종 ③ AgentStep.unknown_fields는 위 내부 계약 사유만 가진다.
- **현재 ④ 계약 제약:** 현재 전체 견적 집합에서 미확인 비용이 남으면 확정 구매안 계산을 진행할 수 없다.
  LOW_VALUE는 비용의 확정이나 업체 제외 승인이 아니다. 비용 None과 견적 전체 집합을 보존한다.
  진행을 허용하려면 담당자가 조건부 비용/범위를 가진 구매안, 평가 완료된 미확인 항목의 전달·소비,
  해당 출력의 총액/참조 검증 계약을 합의해야 한다. 이번에는 이를 임의 확장하거나 거래처 문의로 돌리지 않았다.
  Runner의 저장 전 최종 검사 등록과 실제 Store/초안 어댑터 연결은 아래 잔여 그대로다.

요청 두 조건은 check-47에서 실패로 재현했다. 포함 비용 재현의 초기 문구 “단가 포함”은 기존 숫자 단가 원문 검사에도
걸렸으므로 비용 포함 표현을 “상품가격에 포함”으로 명확히 했다. 해당 단가 원문 검증 자체는 완화하지 않았다.
check-48/49에는 이 문구와 옛 LOW_VALUE 보완 요청 기대 실패가 남아 있다. check-50의 유일한 실패는 그래프 unresolved를
최종 단계만의 값으로 가정한 테스트였고, 누적 이력과 최종 ③ HOLD를 분리해 check-51 영향 2건이 통과했다.
최종 check-52에서 두 조건과 비용·③ 분기·최종 검사·④⑤·기존 팀 회귀를 모두 확인했다.
혼합 질문 초안의 부분집합 검사는 비교 결과만 명시적인 대역으로 둔 단위 검사이며, 혼합 원문 통합 성공을 주장하지 않는다.
모델·Store·초안 대역 구분과 실제 호출 금지는 유지한다.

## 앞선 외부 검토 보완: 비용 해석·질문 영향·양방향 역전

외부 실패 6개 조건을 기존 테스트 파일에서 먼저 재현했다(check-41: 6 failed). 같은 재현은 수정 후
check-43: 6 passed로 전환됐다. 외부 검사 파일 자체를 전달받은 것은 아니며 요청의 여섯 조건을 재현한 것이다.
앞선 단계 수정 경로는 tools.py/agents.py/schemas.py, tests/agent_memory_store.py,
tests/test_agents345_priority.py/test_agents45_team_data.py와 이 문서다. 기존 미커밋 이관 결과는 모두 보존했다.

- period_cost: basis 없음/회당/1회/건당은 주문 1회 금액, 업무 단위당은 실제 해당 업체 배분 수량을 곱한다.
  명시적 amount=0은 0원이다. 원문으로 단가 포함이 검증된 비용은 별도 금액이 없어도 추가 가산액 0원이다.
  그 외 다른 단위당·포함 여부 불명·금액 누락은 미확인으로 남긴다.
  기존 비교기간 전체 표기도 지원하지만 유일한 허용 형식으로 제한하지 않는다.
  원문에 이미 금액/포함 여부/단위 기준이 있는데 추출만 빠졌으면 RECHECK_SOURCE로 넘긴다.
- ComparableQuote.cost_rates는 원문으로 검증된 단위당 요율을 보존한다. ④ 배분·⑤ 재가격에서 allocation_costs가
  배분 수량(확인된 고정 물량 포함)에 다시 적용한다. 독립 검사는 원본 요율×수량으로 별도 계산하여
  잘못된 공용 비용/총액을 거부한다. 단가 구간·포장 배수·원문 근거·참조 검증은 유지한다.
- 필수 조건은 MUST_CONFIRM. 계산 가능한 비용 질문은 저장된 영향 근거에 따라 CAN_CHANGE_CHOICE 또는 LOW_VALUE다.
  LOW_VALUE는 비음수 비용의 하한 0원이라는 **명시적 가정 시나리오**에서도 해당 업체를 쓰는 모든 검증된 제공 배분안이
  비용과 무관한 다른 안보다 엄격히 불리한 경우에만 허용한다. 실제 미확인 비용을 0원으로 확정·저장하지 않는다.
  탐색 범위 밖 최적성은 주장하지 않으며, 증명 불가/NOT_CALCULABLE을 LOW_VALUE로 분류하지 않는다.
  공급량·MOQ 경고가 있으면 비용 비교의 조건부 성질을 유지한다.
- 100개 기준 A 상품값 550,000원·배송비 미확인/B 총액 560,000원·배송 무료는 역전 기준 10,000원이다.
  A 미확인은 A 비용 >= 한도, B 미확인은 B 비용 <= 한도일 때 B 총액 <= A 총액이 성립한다.
  반올림 원시 경계는 각각 raw_lower_inclusive/raw_upper_exclusive로 구분한다.
  양쪽 미확인은 RELATION과 두 변수의 조건식을 저장하며 break_even_amount/baseline_total은 null이다.
  null인 금액을 임의 생성하지 않지만 calculation_ref는 실제 조건부 관계 저장을 성공해야 발급된다.
- tools.py의 중간 import와 자기 모듈 재import를 제거하고 상단으로 정리했다. graph.py와 AgentRunner 본문은 변경하지 않았다.

## 1. 역전 기준의 저장 계약과 질문 연결

이전 calculation_ref=null 계산을 완료로 보던 경로를 보완했다. 내부 compute_break_even은 순수 재계산 함수로 남고,
정식 calculate_break_even은 **조건부 저장 및 read-back 검증을 성공한 경우만 STORED_CONDITIONAL**을 반환한다.
반환 question에는 실제 저장소가 발급한 calculation_ref와 같은 break_even_amount가 결합된다.
저장 미설정/None/빈 참조는 HOLD, 저장 예외·revision/source 변경은 실패로 전파하며 참조를 만들어내지 않는다.
최종 결과 검사에서는 이 저장 경로를 호출하지 않는다.

추가한 **필요 계약**(실제 DB 합의/구현 완료가 아니라 담당자 연결 요구):
- PriorityStore.save_break_even(context: RunContext, scope: ExecutionScope, source_digest: str,
  record: BreakEvenRecord) -> str | None. 인증된 run/업무/revision/현재 견적/source digest를 원자적으로 재확인하고 저장.
  실패 시 None 또는 예외. 테스트 대역은 메모리 lock/조건 검사이며 실제 DB CAS 보장이 아니다.
- PriorityStore.read_break_even(context, scope, ref) -> BreakEvenRecord. 소유·업무·revision·종류·불변성 확인 후 읽기 전용 조회.
- BreakEvenRecord: 업체/대안 견적/질문 견적/필드/비용 항목, CONDITIONAL 또는 RELATION, 금액·수량·단위·기간,
  unknown_side/unknown_terms/comparison_operator, choice_priority/priority_basis,
  산식·offset·rounded_component_limit·raw_lower_inclusive/raw_upper_exclusive, 원본 digest, 근거 견적, 배분 경고와 성립 조건.
  source_digest는 정책·보충 사실·원문 근거를 포함한 현재 계산 입력과 결합된다. 읽을 때 원본으로 다시 계산해 대조한다.

미확인 비용이 있는 역전 기준은 **확정 총액이 아니다**. CONDITIONAL/RELATION을 CONFIRMED로 승격하지 않는다.
기존/대안 어느 쪽이든 미확인 운송 또는 기타물류 한 항목, 또는 양쪽 각 한 항목을 지원한다.
동일 업체 두 항목 동시 미확인 등 지원 범위를 넘거나 필요한 근거가 없으면 NOT_CALCULABLE/missing_inputs다.
상품·운송·기타물류(전환 포함)를 각각 합산 후 0.01 ROUND_HALF_UP하는 공통 evaluate/money를 사용한다.
break_even_amount는 명목 구성요소 한도이고 실제 원시 금액 경계는 offset과 raw_lower_inclusive/raw_upper_exclusive/condition으로 판정한다.
전환비 반올림 때문에 0원 답이 가능함에도 LOW_VALUE로 오판하지 않는 경계도 검사했다.

## 2. 공급량·MOQ 경고와 배분 불가 구분

사용자 명세의 경고 요구는 **조건부 금액 비교**에 적용했다. 가격/단위/세금/기간/필수 조건/원문이 확인되면
공급량 부족·MOQ 미달만으로 역전 기준을 무조건 NOT_CALCULABLE로 만들지 않는다.
SUPPLY_SHORTAGE/BELOW_MOQ와 비교 수량/한도, allocation_feasible=False를 저장·반환하며 실제 배분 전 해소 조건을 명시한다.
반면 실제 구매안 배분은 기존 QUANTITY_NOT_FEASIBLE로 INVALID 처리한다(기존 AC-10/분산 회귀 보존).
원문 가격 구간 밖을 외삽하거나 포장 배수·미확인 비용을 추정하지 않는다. 비교 근거 부족과 배분 불가를 섞지 않는다.
필수 조건은 선호 점수로 상쇄하지 않고 과거 가격은 현재 대안에 쓰지 않는다.
④⑤ 목표가/검증은 같은 비용 반올림을 쓰되 원본 구간·수량·배분·총액·참조를 독립 검증한다.
반올림 경계의 정책 증분 전수 범위가 10,000을 넘으면 자원 한도 HOLD다.

## 3. typed 도구 인자와 신뢰 범위

check_comparability, calculate_break_even, build_plan_candidates는 quote_version_ids: list[str]를 필수 도구 인자로 받는다.
calculate_break_even은 alternative_quote_version_id: str 및 선택적인 question_quote_version_id: str | None도 받는다.
후자는 미확인 비용 질문의 실제 견적을 지정하며, 기존 업체 질문에서도 대안 ID와 혼동하지 않는다. validate_purchase_plan의 plans: list[Plan],
calculate_counter_offer의 validated_plans: list[Plan], validate_counter_offer의 target: CounterOfferTarget 계약도 유지한다.
Store의 load_priority_input/load_plan_input/load_counter_offer_input은 **원래 실행 입력과 도구 인자의 범위 대조용**이다.
견적 전체 집합(중복 금지)/대안 ID/validated_plans를 대조하고 현재 저장소의 최신 집합도 검사한다.
업무 ID·revision·실행 ID는 도구 인자에 노출하지 않고 runtime.context만 사용한다. 대화 문자열에서 복원하지 않는다.

## 4. ③ 최종 출력 검사와 공통 초안 요구

check_final_output(INFO_PRIORITY, PriorityIn, RunContext, PriorityOut)은 질문 업체/필드/근거·중복·누락,
저장된 계산의 금액/업체/질문/현재 원본 결합, 실제 초안의 질문 묶음을 읽기 전용 대조한다.
원문에 답이 있는데 추출이 빠졌거나 필수 누락이 남은 BUILD_PLANS, 위조 참조·과거 revision·다른 업무·변조된 기록을 거부한다.
계산 가능한 비용 질문은 평가 기록에 저장된 계산 참조가 있어야 한다. LOW_VALUE만 남은 정상 HOLD도 이 근거를 보존한다.
CAN_CHANGE_CHOICE/LOW_VALUE에는 현재 원본으로 재계산한 분류/근거가 필요하며, 임의 MUST_CONFIRM 변경도 거부한다.
필수 조건의 MUST_CONFIRM은 비용 분류로 낮출 수 없다. RELATION의 null 금액은 저장된 관계/질문 견적/분류와 함께 검사한다.
정상 정책 HOLD는 불필요한 계산·초안 저장소 없이 scope/출력 계약을 검사한다.

추가 요구: DraftLookupStore.read_clarification_draft(context, scope, ref) -> ClarificationDraftBinding.
모델은 purpose=CLARIFICATION, case_id, input_revision, supplier_id, questions:list[Question], send_authorized=False다.
실제 저장된 ID, 같은 업무/revision, 업체별 문의 대상(MUST_CONFIRM/CAN_CHANGE_CHOICE) 전체와 계산 참조/금액 일치,
누락/중복/발송 승인 여부를 검사한다. 평가 기록의 LOW_VALUE를 초안에 포함하면 거부한다.
도구 등록만 있고 이 조회가 없으면 완료하지 않는다. 공통 create_mail_draft의 실제 저장/내용 검증/조회 어댑터는 미연결이다.
⑤의 read_counter_offer_draft/DraftBinding 요구도 유지한다. 가짜 draft_id 또는 별도 초안 완료 경로는 없다.

Runner 등록 API는 없으므로 본문은 수정하지 않았다. 담당자는 AgentRunner.__call__의 try 안에서 check_grounded 다음,
finish_agent_run(COMPLETED) 전에 **③④⑤**의 output=check_final_output(role,data,context,output)을 호출해야 한다.
반환은 검증된 기존 역할 Out. OutputValidationError(code), MissingReviewData(fields/action),
FINAL_INPUT_OR_OUTPUT_SCHEMA_INVALID 및 저장소 예외는 기존 FAILED 처리로 전파한다. 검사 실패를 완료 HOLD로 바꾸지 않는다.
최종 저장의 원자적 revision/source 검사도 백엔드 책임이다. 등록 후 기존 strict xfail 4건을 일반 테스트로 전환해 확인해야 한다.

## 5. 초기·부분 회신의 실제 그래프 재현과 담당자 수정 위치

- test_initial_plan_graph_without_quote_set_reproduces_owner_gap: run_case(PLAN)는 초기 quote_version_ids를 넣지 않는다.
  저장소에는 qv-a/qv-b가 있어도 PlanIn.quote_version_ids=[] → QUOTE_VERSION_IDS_REQUIRED/HOLD를 재현한다.
- test_partial_reply_graph_loses_untouched_supplier_quote_reproduction: REPLY에서 실제 store_structured_mail로 B만 처리하면
  ExtractionOut에는 B ID만 있다. OUTPUTS[INFO_EXTRACTOR]가 그 집합을 덮어써 PriorityIn에서 A가 빠지고
  STALE_QUOTE_VERSIONS로 FAILED가 되는 것을 재현한다. 테스트에서 성공으로 우회 연결하지 않았다.

담당자가 전달해야 할 입력은 **같은 업무/revision의 모든 업체 최신 견적 ID 전체 집합**이다.
기존 CaseStore.get_work_case의 latest_quote_version_ids를 인증·revision 일치가 보장되는 조회로 제공해야 한다.
예: load_current_quote_version_ids(context: RunContext) -> list[str] 콜백(원자적 revision 일치 조회 요구).
수정 위치는 (a) graph.py:run_case의 PLAN 초기 state를 만들 때, (b) agent_node에서 ① STORED 출력 처리 후
다음 PriorityIn/CandidateIn을 만들기 전이다. 이번 저장 ID가 현재 전체 집합에 포함되는지 확인하고 교체해야 한다.
회신의 과거 ID를 단순 union하지 말고 업체별 현재 버전으로 교체하며 revision이 달라지면 STALE 처리한다.
① check_grounded의 “이번 실행 실제 저장 ID” 제약은 완화하지 않는다. graph.py는 이번에 수정하지 않았다.
preprocess/review_card 실구현, SKIPPED 근거, 고정 물량 표현/부분 비교/미정 회사 정책도 계속 담당자 연결 범위다.

## 공통 담당자 연결 요청 — 함수·모델·기존 검사 대응

추가 구현과 테스트 실행을 멈춘 상태에서 정리한 연결 목록이다. 아래는 담당자 작업 요청이며 연결 완료 보고가 아니다.
이번에는 이 문서만 수정했다. 제품·테스트·기존 AGENTS45_REVIEW.patch는 검토된 내용 그대로 보존했다.
따라서 기존 패치는 이번 문서 정리 직전의 스냅샷이다. 검사 수는 아래 기존 실행 기록 그대로이며 새로 합산하지 않는다.
경로 약어: A=app/agents/agents.py, T=app/agents/tools.py, S=app/agents/schemas.py, G=app/agents/graph.py.
테스트 파일 약어: P=tests/test_agents45_product_registration.py, I=tests/test_agents345_priority.py,
D=tests/test_agents45_team_data.py. 아래 이름은 모두 해당 파일에 이미 있는 함수다.

| 요청 | 우리가 제공한 진입점·모델 | 담당자가 연결할 위치 | 연결 후 실행할 기존 검사 |
|---|---|---|---|
| Runner 저장 전 최종 검사 | A.check_final_output(role, data, context, output). 입력은 PriorityIn/PlanIn/CounterOfferIn와 RunContext, 반환은 검증된 PriorityOut/PlanOut/CounterOfferOut. ③은 check_priority_output, ④⑤는 ReviewCalculator.check_output→check_calculator_output으로 위임 | A.AgentRunner.__call__의 try 안, check_grounded 다음에 output=check_final_output(role,data,context,output). finish_agent_run(COMPLETED) 전이며 예외는 기존 FAILED 처리로 전파. 현재 등록 API는 없음 | P::test_open_runner_direct_output_must_not_complete_forged_result의 4개 fault를 등록 후 일반 테스트로 전환. P::test_final_guard_repeated_valid_output_is_read_only, I::test_priority_final_guard_is_read_only_with_stored_calculation_and_draft도 실행 |
| 실제 Store와 원자적 revision 저장 | T.use_case_store(CaseStore), use_quote_store(QuoteStore), use_role_stores(priority=PriorityStore, plans=PlanStore, counter_offers=CounterOfferStore). 실제 메서드/모델 대응은 아래 표 | 백엔드 구성 지점에서 실제 어댑터 주입. load_priority_input/load_plan_input/load_counter_offer_input은 RunContext.agent_run_id로 인증된 원래 PriorityIn/PlanIn/CounterOfferIn 조회. 각 저장 트랜잭션에서 업무·실행·revision·현재 견적 집합·source_digest를 재확인하고 실제 저장 참조만 반환. AgentRunStore.finish_agent_run의 최종 저장도 run에 기록된 revision을 같은 트랜잭션에서 확인 | D::test_revision_change_is_checked_on_reads_and_atomic_reference_writes, I::test_break_even_store_failure_never_issues_a_reference, I::test_priority_final_guard_rejects_stale_or_foreign_calculation, P::test_revision_change_blocks_before_calculation_or_reference_writes |
| 공통 초안 생성·조회 | T.register/@tool과 허용 도구명 create_mail_draft. T.DraftLookupStore.read_clarification_draft→S.ClarificationDraftBinding, read_counter_offer_draft→S.DraftBinding. T.use_role_stores(drafts=...) | 공통 담당자가 실제 생성 도구를 정식 등록하고 저장소 조회 어댑터를 주입. 생성 인자 전체 시그니처는 아직 미합의이며 별도 완료 경로를 만들지 않음. ③은 목적 CLARIFICATION·업체·문의 대상 Question 묶음, ⑤는 검증된 CounterOfferTarget/참조와 실제 저장 ID를 연결. 조회는 소유·업무·revision·실제 저장 내용·미발송 승인 상태를 보장 | I::test_missing_required_fields_group_by_supplier_and_no_fake_draft, I::test_priority_guard_rejects_forged_questions_and_draft_groups, I::test_final_guard_mixed_draft_subset_contract, P::test_final_guard_requires_actual_draft_target_and_unapproved_send, D::test_final_guard_team_validation_row_cannot_replace_missing_shared_draft_lookup |
| 초기/부분 회신의 전체 견적 집합 전달 | 기존 T.CaseStore.get_work_case(case_id)['latest_quote_version_ids'], QuoteStore.latest_quote_version, ExecutionScope.from_input. S.RunCaseInput/ExtractionOut, T.QuoteVersionRecord 및 다음 PriorityIn/PlanIn/CounterOfferIn의 quote_version_ids | G.run_case의 PLAN 초기 state 생성 시 전체 최신 ID 주입. G.build_graph 내부 agent_node에서 ① STORED 후 OUTPUTS[INFO_EXTRACTOR]의 부분 집합을 다음 INPUTS로 넘기기 전에 같은 업무/revision의 전체 최신 집합으로 교체. 저장 ID 포함 여부 확인, 과거 ID 단순 union 금지. load_current_quote_version_ids(context)는 기존 문서의 제안 이름이며 현재 구현 함수가 아님 | I::test_initial_plan_graph_without_quote_set_reproduces_owner_gap, I::test_partial_reply_graph_loses_untouched_supplier_quote_reproduction의 현상 재현 기대를 전체 집합 전달 성공 기대로 전환. I::test_actual_extraction_priority_plan_counter_graph_path, I::test_reply_new_revision_reenters_and_old_reference_is_rejected, I::test_typed_quote_tool_arguments_must_match_trusted_scope도 실행 |

Store 메서드와 실제 모델 대응(T의 Protocol, S의 모델):

| Protocol | 메서드 → 모델 |
|---|---|
| PriorityStore | load_review_facts → ReviewFactsRecord; save_break_even/read_break_even → BreakEvenRecord (저장 반환 str 또는 None) |
| PlanStore | load_review_facts → ReviewFactsRecord; save_plan_set/read_plan_set → PlanSetRecord; save_plan_calculation/read_plan_calculation → PlanCalculationRecord |
| CounterOfferStore | load_review_facts → ReviewFactsRecord; save_price_calculation/read_price_calculation → PriceCalculationRecord; save_price_validation/read_price_validation → PriceValidationRecord; save_policy/read_policy → PolicyRecord |
| 기존 QuoteStore/CaseStore/AgentRunStore | insert_quote_version(record: QuoteVersionRecord, expected_input_revision)는 기존 원자적 견적 저장 계약 유지; CaseStore.save_agent_output은 interim만 저장하고 최종 출력은 AgentRunStore.finish_agent_run이 저장 |

모든 역할 Store는 RunContext와 ExecutionScope를 사용한다. load_review_facts는 현재 업무/revision/견적 집합과
team_record_digest가 결합된 ReviewFactsRecord를 제공한다. 최종 검사는 저장 함수를 호출하지 않는다.
공통 초안 조회에서 ③은 ClarificationDraftBinding의 case_id/input_revision/supplier_id/questions/send_authorized=False를,
⑤는 DraftBinding의 target/send_authorized=False와 조회 어댑터의 범위 보장을 검사한다.
LOW_VALUE는 평가 기록에만 보존하며 초안 대상에서 제외한다. LOW_VALUE만 남는 내부 계약 HOLD는 초안을 요구하지 않는다.

현재 검사들은 모델·Store·초안 대역을 사용한다. 담당자는 연결 후 같은 재현을 실제 어댑터에서도 확인해야 하며,
기존 오프라인 통과만으로 DB 트랜잭션·메일 저장 통합이 증명되지는 않는다. 혼합 초안 검사는 비교 결과 대역을 쓰는 단위 검사다.
Runner 4 xfailed는 실제 등록 전까지 유지한다. 전체 견적 집합의 두 재현도 현재 결함 기대가 통과한 것을 연결 성공으로 세지 않는다.

## 최종 검사와 검토 패치

마지막 제품 수정 이후 이번 PR 준비에서 전체 범위를 **한 번** 실행했다:
`check.py tests -k agent --tb=short -rx` (내부 pytest tests -k agent).
`../verification/check-55.txt` / `.xml` / `.command.json`:
**630 passed / 20 skipped / 4 xfailed / 0 failed / 282 deselected, 경고 13건**.
OFFLINE_AUDIT real_key_reads=0, network_attempts=0, evals_or_reports_reads=0.
20 skipped는 실제 모델 시험이며 4 xfailed는 Runner 저장 전 검사 미등록 재현 그대로다.
경고는 의존성 DeprecationWarning이다. 통과 수는 check-55 한 번의 결과이며 다른 실행 수와 합산하지 않는다.

check-54의 원문 숫자/포함 비용 영향 검사 **42 passed / 185 deselected**, check-52의 이전 소스 전체 검사
**616 passed / 20 skipped / 4 xfailed / 282 deselected**는 역사적 기록으로 보존한다.
check-41~54의 재현·중간 실패·영향 검사 로그도 그대로 보존한다.
기존 패치는 문서 정리 이전 스냅샷이며, 최신 인계 문서와 PR diff는 위 11개 경로를 기준으로 한다.

실제 팀 Runner·등록 도구·build_graph·기본 검증 노드를 사용했다. 모델·Store·공통 초안 저장/조회는 명시적인 대역이며
preprocess는 제공된 텍스트를 위한 no-op 대역이다. 실제 모델·DB·메일·서비스 전체 COMPLETE 성공을 주장하지 않는다.
최종 검사 반복 시 계산 참조·초안·결과 저장이 증가하지 않는 회귀를 유지했다.

검토 파일은 기존 `AGENTS45_REVIEW.patch` **한 개**를 HEAD 85501db… 대비 전체 변경으로 갱신한다.
이전 7파일 증분이 아니다. 수정 9개 + 새 tests/test_agents345_priority.py 1개 + kan60 삭제 21개 = 총 31개 경로,
테스트·이 인계 문서를 모두 포함한다. 패치 자신/실행 로그/키·환경 파일/과거 보고서/ZIP은 포함하지 않는다.
tracked diff와 untracked 신규 파일 diff를 합쳐 파일 집합·바이트 재생성 일치를 확인하고 reverse apply --check로 현재 내용과 대조한다.
실제 apply/stage/commit/push/PR은 하지 않는다. 여러 모델 시험도 모델/공급자/한도/비용 상한/중단 조건 제시 후 별도 승인 단계다.

---

# 이전 단계 기록 — 현행 구조/결과로 해석하지 않음

아래 경로·변경 금지 범위·패치 범위·검사 수는 각 시점의 기록이다. 현행 상태는 위 절을 따른다.
check-17의 **113 passed / 4 xfailed**, check-20의 **167 passed / 4 xfailed** 및 당시 미연결 사실을 보존한다.
기존 167/4 실행 로그: `C:/Data/02_Projects/Gyeonjwo/Agent/delivery_alignment_20261029/verification/check-20.txt`.

﻿# ④·⑤ 팀 납품 연결: 입력 필요성·종료 분기·지원 범위

2026-10-30 갱신. 기존 delivery_alignment_20261029/checkout에서 작업했다.
팀 기준은 1185b120e8e3dd57d5d86c073ea71a38f0b520d1이다. 이번에는 두 원격 브랜치를 fetch했으며
feature/agents는 위 기준, feature/agents-4-5는 직전 반영 85501db870d69da226a911992532b33145b2312e로 변경이 없었다.
새 checkout/폴더/ZIP/실행기를 만들지 않았다. 이번 최종 검증 연결 준비에서는 실제 모델·DB·메일·commit·push를 실행하지 않았다.
아래의 현행 내용이 이전 문서에서 모든 보충 필드를 필수로 나열한 설명을 대체한다.
직전 검토본의 원문 숫자 대조·pack_multiple 누락·revision 0 불일치 세 결함 수정을 유지한다.
수량별 단가 구간을 유지하며 공유 Runner·스키마·① 추출·공통 도구·그래프·최종 노드는 수정하지 않았다.

### 검사 기록과 현재 잔여 요약

| 시점/대상 | 결과 | 의미 |
|---|---|---|
| 단가 구간 구현 직후의 과거 코드 (`verification/check-13`) | 76 passed | 외부 재검토의 세 결함 수정 전 기록. 현재 최종 결과로 표시하지 않음 |
| 세 결함 수정 후 직전 27파일 검토본 (`verification/check-17`) | 113 passed / 4 xfailed | 기존 76개를 포함한 통과 113개. 4건은 Runner 저장 전 검증 공백의 미해결 재현이며 통과가 아님 |
| 사용자 제공 외부 재검토 | 113 passed / 4 xfailed 재현 | 사용자가 재현과 feature/agents=1185b120… 일치를 확인한 직전 검토 기록 |
| 읽기 전용 최종 검증 연결 준비 (`verification/check-20`) | 167 passed / 4 xfailed | 기존 통과 113개 + 새 회귀 54개. Runner 미연결 4건은 그대로 미해결 |

**현재 남은 연결:** Runner의 COMPLETED 저장 전 검증 훅, 공통 create_mail_draft와 초안 조회/검증 계약,
실제 CaseStore·QuoteStore 및 보충 사실·검증 참조 저장소의 인증/원자적 revision 검사 연결.
현재 결과는 모델·저장소 대역을 사용한 오프라인 제품 경로 검증이다. 실제 모델·DB·최종 그래프 완료를 뜻하지 않는다.

## 팀 근거와 유지한 제품 경로

- app/agents/README.md의 입출력·공통 도구 계약: ID로 입력하고 도구로 업무/정책/견적을 조회한다.
  금액·물량은 DecimalStr이고 계산은 도구가 한다. NEEDS_EVIDENCE는 ③, RECHECK_SOURCE는 ①로 연결된다.
- app/agents/schemas.py: Plan/Allocation/CounterOfferTarget, RunContext, PlanIn/CounterOfferIn/…Out.
  Allocation에는 supplier_id/quote_version_id/quantity만 있으며 고정/변경 물량·적용 단가 구분 필드는 없다.
- app/agents/tools.py: CaseStore/QuoteStore/QuoteVersionRecord, EXTRACTION_FIELD_NAMES, 도구 설명표.
  구매안 도구는 유지·전환·최대 2곳 분산, 단가 구간·MOQ·납기·비용 검사를 담당한다.
- app/features/review/domain/calculations.py: 상품/운송/기타물류 각각 합산 후 ROUND_HALF_UP, 0.01.
  이 파일 및 숫자 검증 공유 모듈은 수정하지 않았다.

```text
AgentRunner → RunContext(case_id, input_revision, agent_run_id, review_batch_id)
  → build_agent(PROMPTS, tools_for)
  → tools.py의 정식 등록된 네 전용 도구
  → product.dispatch_review → TeamReviewRepository.for_run
  → CaseStore·QuoteStore·원문 조회 → 팀 데이터 변환
  → BridgeService → 기존 구매안/역제안 계산·독립 검증
  → PlanOut / CounterOfferOut → AgentRunner.finish_agent_run
```

installed_tools나 레지스트리/Runner 교체를 사용하지 않는다. 업무·revision·실행 ID는 RunContext에서 읽는다.
원래 입력의 전체 견적 ID 집합을 보존한다. save_agent_result는 interim/참조만, 최종 …Out은 Runner만 저장한다.
schemas.py·graph.py·nodes.py·다른 역할·공통 도구·공유 저장소 인터페이스는 변경하지 않았다.

## SupplementStore 소비 필드별 판정

분류: **① 팀 계약에서 요구하는 업무 사실**, **② 해당 구매 조건에서만 필요한 사실**,
**③ 독립 계산기/어댑터의 내부 표현·실행 요구**. 이름이 비슷하다고 전용 JSON 필드까지 팀 필수로 간주하지 않는다.

| 소비 필드 | 분류·팀/코드 근거 | 현행 처리 |
|---|---|---|
| as_of | ③. RunContext에는 없음. 구매안 코어가 견적/정책 유효일 비교에 쓰는 재현 가능한 판단일 | 실행 설정으로 계속 필요하나 원문 evidence 요구 제거. 거래처 질문 대상 아님 |
| period | ②. 기간에 따른 비용·공급·가격·정책을 비교하는 현재 코어의 범위. 팀 due_date는 납기이지 비교기간 전체가 아님 | 비용·정책 적용 범위를 특정해야 하므로 명시 입력 유지. due_date로 전체 기간을 추정하지 않음 |
| item_identity.item_id | ③. 팀 업무/견적/Plan에 없는 계산기 식별자 | 요구·비교 제거. 이름/규격으로 연결하며 임의 ID를 생성하지 않음 |
| item_identity.origin/grade/processing | ②. 팀 업무의 품목·규격/mandatory_conditions에서 해당 조건이 중요할 때 | identity 전체 필수 요구 제거. 명시된 조건은 비교하고, 없는 조건 값을 생성하지 않음 |
| terms | ②. 배송·결제·세금 조건은 가격/비교 조건에 관계할 때 필요. 내부 terms 객체 모양은 ③ | 현재 동일조건 비교 프로필의 명시 조건은 유지. 모든 업무에 이 JSON 전체가 팀 필수라고 요청하지 않음 |
| incumbent_supplier_id | ①. ⑤는 기존 업체에 대한 목표 조건을 생성(schema CounterOfferTarget.supplier_id) | 현재 견적 중 previous_unit_price 근거를 가진 업체가 유일하면 실제 레코드에서 연결. 모호하면 명시 입력 필요 |
| fixed_commitment.quantity/verified/source | ②. 고정 계약과 변경 가능 물량 구분은 실제 계약 사실 | 명시적 고정물량 0의 근거는 유지. 계약 정보가 없다는 이유로 0을 넣지 않음 |
| fixed_commitment.unit_price/period | ②. 양수 고정 계약에만 계산 영향; 물량 0에도 요구한 것은 ③ 내부 제약 | 명시된 수량이 0이면 요구/계산 제거. 입력 단가를 0으로 채우지 않음. 양수이면 계속 필요 |
| repricing_scope | ②. 분산안에 남는 기존 업체 물량에 역제안 단가를 적용할지 결정 | 정책상 분산 불가(allow_split=false 또는 max_suppliers=1)일 때 요구 제거, None 보존. KEEP은 제안가, SWITCH는 다른 업체 견적가라 이 구분이 불필요 |
| policy_source | ③. CaseStore.policy에 더해 같은 정책을 메일에서 재확인하던 사본 포트 | 소비 제거. 인증된 CaseStore의 정책 내용·업무·revision·source_ref를 보존. 승인/한도/유효기간을 생성하거나 완화하지 않음 |
| <qid>.item_identity.item_id | ③. 별도 내부 품목 ID | 요구·비교 제거 |
| <qid>.item_identity.grade/processing | ②. 업무에 해당 조건이 명시된 경우 | 조건이 없으면 필수 아님. 조건이 있는데 견적 쪽 확인이 없으면 성공 처리하지 않음 |
| <qid>.terms | ②. 실제 견적의 비교 조건. 필수 객체 구조 자체는 ③ | 기존 명시 조건 비교 유지. 공통 comparability 결과와 연결되기 전에는 의미를 추정해 삭제하지 않음 |
| <qid>.valid_from | ②. 별도 유효 시작 제한이 명시된 견적에만 필요. 무조건 양끝 날짜를 요구한 것은 ③ | 선택 입력으로 변경. 있으면 검사. 없으면 None 유지하며 수신일·effective_from으로 채우지 않음. 최신 버전·CONFIRMED/eligibility·수신일·valid_until 검사는 유지 |
| <qid>.supply_from / supply_to | ②. 기간 공급 가능 범위를 나타내는 내부 표현 | 팀의 명시 delivery_date가 있으면 그 단일 공급일로 연결, 별도 보충 불필요. 기간 공급이면 명시 범위 필요 |
| <qid>.lead_days | ②. 소요일수로 납기를 판단해야 할 때. 확정 납품일과 이중 요구는 ③ | 팀의 N일은 변환하여 검사. 확정 delivery_date가 있고 lead_time이 없으면 None 허용, 0일로 채우지 않음. 제공된 소요일수 제약은 계속 검사 |
| <qid>.switching_cost | ②. 새 업체를 선택하는 안의 전환비 | 기존 업체는 전환하지 않으므로 비적용 None 허용, 비용 행에서 제외. 다른 업체 비용은 누락 시 계속 차단. 알려진 양수 기존업체 전환비를 무시하지 않음 |
| <qid>.cost_scope | ③의 PER_SUPPLIER_PER_PERIOD 레이블과 ②의 실제 비용 적용 범위 | SHIPPING/HANDLING에서 이미 기간 범위/포함 여부를 확인했고 전환비가 비적용 또는 명시적 0이면 별도 레이블 입력 제거. 양수 전환비 범위는 계속 필요 |
| <qid>.eligibility | ①의 비교 가능성 판단, CONFIRMED 문자열/저장 모양은 ③ | 회사 조건·근거의 검토 결과여야 함. 현재 공통 도구가 없어 검토 결과 저장소 대역 사용. 거래처에 CONFIRMED 값을 요청하지 않음 |

item_identity를 줄여도 업무 이름/규격 및 명시된 원산지·등급·가공 조건 검사는 유지한다.
현재 mandatory_conditions의 공통 구조와 비교 결과 연결은 미합의다. 아무 조건이 없다고 기본 가정하지 않으며
별도 eligibility 검토 결과를 회사 조건/레코드 digest에 결합한다. 실제 공통 도구 통과로 표현하지 않는다.

추가로 다른 업체의 effective_from은 현재 계산에서 사용하지 않으므로 누락을 차단하지 않는다.
기존 업체의 인상 적용일과 모든 사용 견적의 단가·공급량·MOQ·단위·통화·세금·유효종료·비용 근거는 계속 검증한다.
④의 KEEP 생성에 무조건 다른 유효 대안까지 요구하던 조건은 제거했다. ⑤는 가격 상한 비교를 위해 여전히 검증 대안이 필요하다.

## 종료 분기: 무엇을 누구에게 해결하도록 보내는가

| 상황 | 제품 반환 | unknown_fields/처리 |
|---|---|---|
| 거래처 단가·수량·MOQ·세금·비용 조건이 없고 원문에 저장된 값도 없음 | NEEDS_EVIDENCE | unit_price, available_quantity, moq, tax_terms, cost_terms 등 실제 확인 필드 |
| 원문 값은 있는데 정규화 값 누락, 값/원문 불일치, 비용 포함 여부 불일치, 근거 위치 재확인 | RECHECK_SOURCE | 반드시 tools.EXTRACTION_FIELD_NAMES에 있는 필드만. qid나 오류 코드/메서드 이름을 보내지 않음 |
| 회사 정책 없음/유효하지 않음, 내부 구매조건 미확정 | HOLD | policy 또는 실제 내부 미확정 경로 |
| 저장소/RunContext/보충 포트/공통 비교·초안 미설정 | HOLD | 해당 의존성 이름. 거래처 질문으로 변환하지 않음 |
| 자료는 있으나 회당 비용·누진 단가 등 현재 변환/코어가 미지원 | HOLD | UNSUPPORTED_* 또는 TIER_GRADUATED_UNSUPPORTED 등. 원문/거래처 탓으로 돌리지 않음 |
| 업무·견적 범위 위반, revision 변경, 검증 참조 변조 | 예외로 차단 | 이전 결과 저장/가격 생성 금지. 보완 질문으로 우회하지 않음 |

여러 종류가 함께 발견되면 HOLD → RECHECK_SOURCE → NEEDS_EVIDENCE 순으로 선택한다.
unknown_fields는 선택한 분기에 해당하는 필드만 넣고, 나머지는 도구의 blocking_issues에 분리한다.
예: 정책과 단가가 모두 없으면 HOLD(policy)이며 개발 의존성과 단가 질문을 섞어 ③으로 넘기지 않는다.
③에서 전달된 questions에도 policy/create_mail_draft/ValidationStore 같은 내부 항목이 있으면 HOLD로 분리한다.
④·⑤ 프롬프트는 도구의 분기를 그대로 전달한다. 모델 대역은 이 경로를 검증하며 실제 모델 준수를 입증한 것은 아니다.

⑤는 가격 검증 후 무조건 HOLD하지 않는다. 실제 공통 초안이 미등록이면 HOLD(create_mail_draft).
등록돼도 검증 target/validation_ref와 같은 업무·revision의 초안을 결합할 수 없으면
HOLD(create_mail_draft.validation_binding). 계약에 맞는 내부 초안 ID를 얻은 경우에만 PROPOSED 경로를 허용한다.
현재 공통 도구 미구현으로 그 정상 초안 완료 경로는 미검증이다.

## 계산 코어와 실제 팀 등록 경로의 지원 범위

| 기능 | 기존 계산 코어 | 현재 팀 등록 경로 |
|---|---|---|
| 유지 | 지원 | 고정물량 0·명시 사실 충족 시 지원. 다른 유효 대안 없이 KEEP만 생성 가능 |
| 전환 | 지원 | 다른 업체가 전체 변경 물량·조건을 충족하면 SWITCH 생성 |
| 2업체 분산 | 지원 | allow_split/max_suppliers/배분 간격 및 근거 충족 시 지원. 전체 검증 가능 집합 보존 |
| 재가격 | 모든 검증 대안을 명시 범위로 재계산하고 독립 검증 | 지원. 정상 사례 목표가 5416 검증. 초안 완료와 별개 |
| 양수 고정 계약 | 고정 가격 물량과 변경 물량을 별도 계산, 재가격에서도 고정 가격 유지 | 미지원: TEAM_FIXED_ALLOCATION_UNREPRESENTABLE. 팀 Allocation은 가격 적용 구분이 없어 임의 합치지 않음 |
| 단가 구간 | 명시된 전량 적용 방식 지원. 업체별 배분 수량마다 구간 선택 | 유지·전환·분산·⑤ 대안 재평가에 연결. 누진 방식은 명시적 HOLD. 아래 근거/경계/탐색 제한 적용 |
| 역제안 생략 | NO_COUNTER_NEEDED 계산·독립 검증 지원 | SKIPPED 최종 노드/카드 연결 미완료로 HOLD. 성공으로 표시하지 않음 |

추가 제한: 실제 DB 어댑터·공통 비교/초안·최종 검증 노드가 없고, 주문단위 환산·일부 비용 분류·부분 비교는 미지원이다.
고정물량 없음의 명시 근거가 필요하고, 내부 policy 프로필의 현재 키 요구를 팀 공통 필수 스키마로 주장하지 않는다.
코어만 가능한 기능을 팀 납품 완료라고 표현하지 않는다. 기존 단독 초안 경로는 원래 전체 입력을 대상으로 하며
이번 간소화 입력을 공통 초안에 연결한 것으로 주장하지 않는다.

## 반올림: 정해진 규칙과 아직 정해야 할 정책 의미

공유 evaluate는 각 상품 행의 곱을 먼저 합산한 뒤 상품 합계에 반올림하고, 운송 합계·기타물류 합계도 각각
0.01 단위 ROUND_HALF_UP을 적용한 다음 세 금액을 더한다. 행별 반올림이나 최종 총액 한 번 반올림이 아니다.
공유 규칙은 이미 코드에 있으므로 팀에 다시 선택을 요청할 사안이 아니다. 우리 계산의 정확한 유한 십진수와
공유 결과가 같으면 사용하고, 다르면 현재 제품은 TEAM_ROUNDING_CONTRACT_REQUIRED로 HOLD한다.
공유 계산기를 변경하거나 오차 허용을 추가하지 않았다.

| 실제 계산 사례 | 정확 계산 | 팀 evaluate | 의미 |
|---|---:|---:|---|
| 상품 두 행 각 0.005×1 | 0.010 | 0.01 | 상품 합산 뒤 반올림. 행별 0.01+0.01=0.02가 아님 |
| 상품 1.004 + 운송 0.004 + 기타 0.004 | 1.012 | 1.00 | 최종 한 번 반올림 1.01과도 다름 |
| 상품 1.006 + 운송 0.006, 총액 상한 1.015 | 1.012: 상한 이하 | 1.02: 상한 초과 | 비교에 어느 금액을 쓰느냐에 따라 정책 합격이 바뀜 |
| 수량 1, 목표 증분 0.001, 총액 상한 100.00 | 최대 목표가 100.000 | 반올림 총액으로 비교하면 100.004 | 100.004→100.00 허용, 다음 100.005→100.01 불허 |
| 같은 조건, 총액 상한 100.005 | 최대 목표가 100.005 | 반올림 총액으로 비교하면 100.004 | 정확 계산의 허용 목표가가 오히려 더 커짐 |

목표가 사례는 요청가 102, 대안 단가 101, 최소 99, 단가 상한 102, 추가비용 0, 단일 수량 1의 명시 입력이다.
기존 calculate_offer/validate_offer의 정확 해와 공유 evaluate를 이용한 경계 가격 비교로 확인했다.
후자는 의미 비교용 검사이며 새로운 제품 최적화기를 구현한 것이 아니다.

남은 결정은 총액 산식 자체가 아니라 **회사 max_total_cost의 의미**다: 이미 반올림한 지급금액에 적용하는가,
원시 정확 비용에 적용하는가? 소수 셋째 자리 이상의 상한과 가격 증분을 허용하는가?
의미가 정해지면 우리 목표가 탐색·독립 검증을 그 기준에 연결해야 한다. 금액이 같아 보인다는 이유로
두 계산의 최대 허용 목표가나 정책 판정을 같다고 가정하지 않는다.

## 팀에 필요한 최소 결정/질문

1. **정책·비교 담당:** 실제 CaseStore.policy와 mandatory_conditions의 기준 스키마 및 check_comparability의
   입력(case/revision/견적 ID·정책·필수/가중 조건), 출력(조건별 충족/누락/불일치·허용 비교범위·검증 참조)은 무엇인가?
   사용 위치는 ④ 후보 평가 전이다. max_total_cost가 어떤 금액에 적용되는지는 위 반올림 사례와 함께 확인해야 한다.
   as_of·내부 item_id·policy_source 사본·고정물량 0의 단가/기간은 팀 필수 사실로 요청하지 않는다.
2. **공통 초안·저장소 담당:** CounterOfferTarget/validation_ref/alternative_plan_ref/policy_ref/basis_quote_version_ids를
   같은 업무·revision의 내부 draft_ids와 결합할 공통 입력·조회 계약은 무엇인가? 검증 참조 저장소는
   put_if_current(scope, source_digest, kind, payload, agent_run_id) → ref 및 resolve(scope, source_digest, kind, ref) → payload의
   소유 범위·원자적 revision/digest 검사·무결성을 누가 제공하는가? 사용 위치는 ④ 검증 저장, ⑤ 재조회·초안 생성 전후다.
   CaseStore.save_agent_output(interim)이나 AgentRunStore.finish_agent_run(최종 응답)으로 대체하지 않는다.
3. **오케스트레이션 담당:** 양수 고정 계약의 고정/변경 물량·단가 적용을 팀 Plan/Allocation에서 어떻게 표현할 것인가?
   또한 plan_validation/price_validation의 담당과 검증 참조 조회, SKIPPED 증명/카드 연결을 확인해야 한다.
   기존 노드 반환 계약은 그대로이며 BridgeService.validate_plans(require_refs=True)/checked_target를 재사용할 수 있다.

조건부 조회 공백: 최신 ID의 업체가 메일 목록에서 확인되지 않는 경우에만
get_quote_version(case_id, quote_version_id) → QuoteVersionRecord | None 같은 ID별 조회가 필요하다.
현재 메서드 latest_quote_version(case_id, supplier_id)로 모든 ID를 해소할 수 있으면 추가 인터페이스를 요구하지 않는다.
인상 전 가격이 현재 행 mail_normalized에 없으면 실제 근거 확인/재추출을 먼저 하며 가격을 만들어 넣지 않는다.

우리 미완료 사항(팀 결정으로 전가하지 않음): 누진 단가 평가, 지원하지 않는 비용/단위 변환, 간소화 입력의 초안 연결,
확정된 공통 계약의 어댑터 구현과 실제 저장소 연결. 내부 JSON 요구 전체를 팀 정책/공급자 요구사항으로 올리지 않는다.

## 수량별 단가 구간: 저장 형식·제품 연결·실행 증거

### 실제 팀 저장 형식과 의미 판정

공유 tools.py의 PriceTier → _tiers → QuoteVersionRecord.pricing_terms_jsonb.price_tiers를 그대로 사용한다.
각 저장 행은 다음 형태다. ① 추출 코드나 공유 스키마에 필드를 추가하지 않았다.

```text
{min_quantity: str|null, max_quantity: str|null, unit_price: str,
 min_quantity_raw: str|null, max_quantity_raw: str|null,
 mail_id: str, location: str}
```

숫자 열만 보면 같은 500이어도 원문은 500kg 이상/초과/이하/미만일 수 있다.
우리 변환은 min/max 원문을 다시 정규화해 저장 숫자와 대조하고, 이상·부터는 하한 포함,
초과는 하한 제외, 이하·까지는 상한 포함, 미만은 상한 제외로 보존한다.
숫자만 있고 경계 연산자가 없으면 포함 여부를 추정하지 않는다.
한쪽이 null이면 반대편의 명시 부등식에 해당하는 열린 범위로 처리한다. 양쪽 null은 거부한다.
실제 주문 수량은 양수이며 MOQ·공급 가능량·현재 비교 수량으로 한정한다.

팀 행에는 전량/누진 방식 필드가 없다. 각 행이 근거로 삼은 같은 원문 쪽에서 명시 문장을 확인한다.
현재 확인하는 전량 적용 문구는 다음 세 가지의 공백·마침표 차이다:

- 주문 총수량에 해당하는 단가를 전량에 적용합니다.
- 주문 전체 수량에 해당하는 구간 단가를 전량에 적용합니다.
- 수량 구간은 주문 총수량 기준이며 해당 단가를 전량에 적용합니다.

“각 구간에 속한 물량에 각 구간 단가를 적용합니다” 또는 “구간별 물량에 각각 단가를 적용하는 누진 방식입니다”는
명시적인 누진 방식으로 구분하고 HOLD(TIER_GRADUATED_UNSUPPORTED)를 반환한다. 누진 방식을 전량 적용으로 바꾸지 않는다.
가격이 내려간다는 이유나 표 모양만으로 방식을 정하지 않는다. 문구가 없거나 서로 충돌하면
NEEDS_EVIDENCE(price_tiers), 이유 TIER_METHOD_AMBIGUOUS를 반환한다.

각 단가 행의 경계 원문과 원/kg(또는 명시 통화·단위) 가격이 같은 원문 줄에서 확인되어야 한다.
경계 단위는 명시되어야 하고 현재 견적 단위와 같아야 한다. 다른 단위는 환산하지 않는다.
현재 형식 밖의 여러 줄/복잡한 표를 임의로 결합하지 않는다. 원문 단가·경계가 저장값과 다르면
RECHECK_SOURCE(price_tiers)와 TIER_ROW_EVIDENCE_MISMATCH 또는 TIER_BOUNDARY_VALUE_MISMATCH를 반환한다.
구간과 단일 단가가 동시에 남아 있으면 기본값/대체값으로 선택하지 않고 TIER_FLAT_PRICE_CONFLICT로 재추출한다.

공유 _tiers는 정규화된 수치의 일부 중복을 검사하지만, 같은 경계의 양쪽 포함까지 모두 검사하지는 않는다.
우리 제품 경로가 다시 검사한다. 닫힌 경계 중복은 TIER_OVERLAP, 열린 경계 사이 단일 점 공백도 TIER_GAP이다.
가능 수량 [MOQ, min(공급량, 비교물량)]의 가격 공백은 보완 대상으로 차단하며, 안을 몰래 제외해 최적이라고 하지 않는다.
범위 밖의 수량에 단가를 외삽하지 않는다. 기존 업체가 전체 유지 물량을 공급할 수 없으면
KEEP_QUANTITY_NOT_FEASIBLE로 보류하며 인상 요청 기준 단가를 만들지 않는다.

### 계산과 독립 검증

변경 파일:

- kan60/team_bridge/team_data.py: 원문 의미·경계·단위·근거 검사와 팀 레코드 변환.
- kan60/purchase_plans/contract.py: 구간 구조/중복/공백 검사, 운영 수량별 단가 선택.
- kan60/purchase_plans/tools.py: 배분별 선택 단가와 총액·재가격 계수, 구간/수량/근거 및 탐색 범위 기록.
- kan60/purchase_plans/negotiation.py: 별도의 Decimal 경계 판정과 직접 재합산, ⑤ 대안 재평가.
- kan60/team_bridge/service.py: 정식 등록 경로의 독립 행/총액 검증 및 결과 참조 보존.
- tests/test_agents45_price_tiers.py, tests/test_agents45_team_data.py: 신규 구간 검사와 이전의 일괄 미지원 기대 수정.

④의 최초 구매안은 각 업체에 배분한 수량에 해당하는 **견적 단가**를 쓴다. 분산 수량이 바뀌면 다시 선택한다.
⑤는 기존 repricing_scope를 따른다. KEEP 및 ALL_INCUMBENT_CHANGEABLE의 기존 업체 물량에는 명시 제안가를,
KEEP_ONLY의 분산 잔량에는 해당 수량의 견적 구간 단가를 쓴다. 다른 업체는 견적 단가를 유지한다.
제안가와 구간 단가 중 싼 값을 임의 선택하거나 전부 제안가로 덮어쓰지 않는다.

기존 업체가 구간 견적이면 업무 전체 물량의 해당 구간 단가를 현재 인상 요청의 비교 기준으로 연결한다.
과거 단가는 기존 previous_unit_price 원문에서 읽고, 그 근거/현재 실제 견적 ID를 보존한다.
내부 계산 행에는 quoted_unit_price, tier_index, tier_quantity, tier_source_ref, unit_price_basis를 남긴다.
pricing_phase는 QUOTE_AT_ALLOCATION / COUNTER_OFFER_REPRICING을 구분한다.
cost_function은 후자의 제안가 재계산용 함수다. 최초 분산 견적 총액을 단일 제안가 함수값이라고 표시하지 않는다.
팀 Plan/Allocation 스키마는 그대로이며 세부 행은 calculation_ref/alternative_plan_ref가 가리키는 검증 레코드에 보존된다.

독립 검증기는 운영 quoted_price 선택 함수를 호출하지 않는다. 원본 구간을 Decimal로 별도 순회하고,
배분 수량·정책 범위에서 단가와 상품액·비용을 직접 재계산해 운영 행·총액과 대조한다.
운영 선택 단가에 의도적으로 1원을 더하는 오류를 주입했을 때 INDEPENDENT_PLAN_COST_MISMATCH로 저장 전에 차단했다.
공유 계산기 반올림 규칙과 기존 상한 의미 미확정 보류는 유지한다. 정수 금액의 정상 구간 계산까지 중단하지 않는다.

### 대표 합성 사례

A는 5,500원/kg, B는 500kg 미만 5,600원/kg·500kg 이상 5,300원/kg 전량 적용,
B 운송비는 비교기간 전체 10,000원, 그 외 비용은 명시적 0인 사례다.

| 구매안 | B 수량 → 적용 단가 → B 상품액 | A 상품액 | 전체 총액(운송비 포함) |
|---|---|---:|---:|
| B로 전환, 총수량 499kg | 499 → 5,600 → 2,794,400 | 0 | 2,804,400 |
| B로 전환, 총수량 500kg | 500 → 5,300 → 2,650,000 | 0 | 2,660,000 |
| B로 전환, 총수량 501kg | 501 → 5,300 → 2,655,300 | 0 | 2,665,300 |
| 총 1,000kg 분산 | 400 → 5,600 → 2,240,000 | 3,300,000 | 5,550,000 |
| 총 1,000kg 분산 | 500 → 5,300 → 2,650,000 | 2,750,000 | 5,410,000 |
| 총 1,000kg 분산 | 600 → 5,300 → 3,180,000 | 2,200,000 | 5,390,000 |

B 공급량이 1,000kg인 이 사례의 ⑤ 목표가는 5,310원/kg로 검증됐다.
별도 사례에서 A가 500kg 미만 5,700원/kg·이상 5,500원/kg 구간 견적이고 B는 5,400원/kg·최대 600kg일 때:
ALL_INCUMBENT_CHANGEABLE 목표가는 5,416원/kg이고 A의 400kg에도 제안가를 적용한다.
KEEP_ONLY 목표가는 5,460원/kg이며 분산 A 400kg에는 견적 구간가 5,700원/kg를 유지한다.
두 경우 모두 기존 견적 구간가와 제안가의 적용 근거를 따로 남긴다. 최종 초안은 공통 도구 미구현으로 기존 HOLD다.

### 탐색 범위와 남은 제한

기존 policy.allocation_step 격자에서 최대 2업체의 모든 허용 배분을 평가한다(기존 1,000개 배분 단위 한도).
격자 위 경계는 평가한다. 예를 들어 배분 간격 100kg에 경계 450kg이면 400/500kg은 평가하지만 450kg을 몰래 추가하지 않는다.
이는 현재 정책의 격자 밖이다. tier_search.off_grid_boundaries에 450을 명시하고 global_optimum_claimed=false를 반환한다.
팀 Plan.conditions에도 격자 한계와 전역 최적 미주장을 남긴다. 시장 전체·연속 수량의 전역 최적은 보장하지 않는다.

누진 방식, 양수 고정 계약과 구간 기준 물량의 합산, 주문 단위 환산, 명시 방식/경계/행 근거가 없는 표는 미지원/보완 대상이다.
공통 비교·초안·검증 저장소 운영 구현·최종 노드 제한은 그대로다. 이번 시험도 모델·저장소 대역을 사용한다.

## 실제 연결과 검증 증거

```python
from app.agents import tools
from app.agents.agents import AgentRunner

tools.use_case_store(case_store)
tools.use_quote_store(quote_store)
tools.use_team_review_stores(supplements=review_fact_store, references=validation_store)
runner = AgentRunner(model, runs=agent_run_store)
```

보충 포트는 팀 공통 계약이 아니라 현재 어댑터의 소비측 경계다:
load(scope) → {case_id, input_revision, quote_version_ids, team_record_digest, facts}.
facts[path]는 {value, evidence:{mail_id, part, page, excerpt}}이며 as_of에는 원문 evidence가 필요 없다.
미설정/모호한 내부 포트는 HOLD하고 거래처 질문으로 보내지 않는다. 사실/참조 저장소는 시험 대역만 제공한다.
실제 DB나 인증된 외부 정책/계약 문서 조회를 구현한 것으로 표시하지 않는다.

검사는 팀 형식 합성 CaseStore·QuoteVersionRecord·메일을 사용한다. 모델·저장소만 대역이며 실제 제품 등록,
데이터 변환·계산·독립 검증·Runner 최종 저장을 실행한다. 공통 check_comparability/create_mail_draft는 여전히 미구현이다.

기존 실행기를 사용해 영향받은 세 검사 파일만 실행한다:

```text
python -B -X utf8 .preupload/delivery_alignment/check.py tests/test_agents45_price_tiers.py tests/test_agents45_team_data.py tests/test_agents45_product_registration.py --tb=short
```

과거 단가 구간 구현 단계 결과: **76 passed** (구간 제품 경로 23, 팀 데이터·분기·코어 경계 40, 기존 제품 등록 13).
세 결함 수정 전 기록이며, 현재 검토본 결과는 아래 **113 passed / 미해결 4건**이다. 두 숫자를 합산하지 않는다.
근거: 상위 verification/check-13.txt, check-13.xml, check-13.command.json.
실제 키 읽기/네트워크/evals·reports 접근 시도는 모두 0회이며 의존 패키지 deprecation 경고 13건이 있다.
이상/초과/이하/미만의 직전·경계·직후, 중복·공백·근거·방식 부족, MOQ/공급량,
분산 수량 변화·⑤ 범위별 재가격·격자 밖 경계·운영 오류 주입과 기존 단일 단가 회귀를 검사했다.
실제 공유 _tiers로 합성 저장 행을 생성했다. 첫 시험에서 구분자 없는 합성 행이 공유 원문 대조에서 거절되어,
합성 원문을 명시 표 구분자로 고쳤다. 공유 추출기를 수정하거나 합격 조건을 낮추지 않았다.
팀 README/schemas/graph/nodes·공유 계산/숫자 검증 모듈 및 다른 역할/공통 도구 보존을 별도로 대조했다.
이전 128/202/40개 기록과 이번 검사를 합산하지 않는다. 상위 DELIVERY_RECEIPT.json은 이전 연결본의 기록이다.
실제 모델 품질·DB·공통 초안 완료·최종 검증 노드·전체 그래프 완료는 검증 범위 밖이다.

## 외부 패치 검토 후 결함 수정 (2026-10-30)

### 수정 전 재현과 수정 결과

- 기존 테스트 파일에 회귀 사례를 먼저 추가해 제품 수정 전 실행했다: **18 failed, 1 passed**.
  원문 15500/저장 5500, 55000/5500, 5500.5/5500, 공급량 1600/600, MOQ 100/0,
  이전 단가 15000/5000이 그대로 PLANS_PROPOSED가 됐다. 같은 숫자를 다른 필드에 추가해도 통과했다.
  구간 경계 1500kg/500kg도 통과했다. 구간 단가 15300/5300 자체는 기존 검사에서 이미 차단됐다.
- `team_data.py`: 공백·천 단위 쉼표 정규화 후 숫자의 앞뒤 경계를 확인하고 줄을 연결해 숫자를 만들지 않는다.
  수치 필드는 명시된 필드명/한글 의미 표지의 절에서 첫 숫자와 raw_value/value를 함께 대조한다.
  같은 메일의 다른 필드·다른 절의 숫자는 근거로 빌리지 않는다. 모호한 표지/복수 근거는 추측하지 않고
  RECHECK_SOURCE와 팀 허용 필드명으로 반환한다. 형식화된 `요청 단가: 5,500원/kg`는 정상 처리한다.
  구간 최소/최대 원문에도 같은 숫자 경계 검사를 적용하며, 한 구간 행에서 가격이 여러 개면 재추출한다.
- `team_data.py`, `purchase_plans/contract.py`, `purchase_plans/tools.py`,
  `purchase_plans/negotiation.py`, `team_bridge/service.py`: 실제 팀 저장 필드
  `pricing_terms_jsonb.pack_multiple`, `pack_multiple_unit` 및
  `evidence_jsonb.pack_multiple.{raw_value,value,quantity_unit,mail_id,location}`를 연결한다.
  팀 tools.py의 `_QUANTITY_FIELDS`, `_quantity`, `quantity_units` 저장 경로와 agents.py의
  “최소 20박스, 5박스 단위” 예시에 따라 MOQ와 별개인 **업체 주문 수량의 배수**로 검사한다.
  계산 입력에는 pack_multiple/pack_multiple_unit/pack_multiple_source_ref를 보존하고 증거 목록에도 연결한다.
  누락을 0이나 1로 만들지 않는다. 명시된 조건이 없으면 기존 단일 단가 사례의 입력을 그대로 유지한다.
  원문·저장 단위 불일치는 RECHECK_SOURCE, 단위 자체의 근거가 없으면 NEEDS_EVIDENCE,
  box→kg 등 지원하지 않는 단위 환산은 HOLD(`PACK_MULTIPLE_UNIT_UNSUPPORTED:<견적ID>`)다.
  제품 계산은 Fraction 나머지, 독립 검증은 원래 입력의 Decimal 나머지로 각각 확인한다.
  B=300kg 배수/공급량600kg/정책100kg 배분이면 300·600kg만 통과하고 100·200·400·500kg는 제외된다.
  MOQ400kg 또는 정책200kg 배분과 결합하면 B600kg만 통과한다. ⑤ 재가격 및 구간 단가 재평가도 같은 배수를 유지한다.
- `purchase_plans/contract.py`: revision을 팀 `AgentInput.input_revision(strict=True, ge=0)`와 맞췄다.
  0으로 ④ 계산·⑤ 가격 독립 검증까지 통과하며, 음수·bool은 거부한다. 실행 중 0→1 변경과 기존 read/write
  revision 변경은 계속 차단한다. 견적 version_no의 양의 정수 조건은 바꾸지 않았다.

### 남은 공통 연결 공백: Runner 완료 저장 전 검증

`tests/test_agents45_product_registration.py::test_open_runner_direct_output_must_not_complete_forged_result`
4개 매개 사례가 **미해결**이다. 모델 대역은 업무 도구를 하나도 호출하지 않고 structured response용
PlanOut/CounterOfferOut만 직접 반환한다. 사전에 정상 참조를 만든 뒤 다음 각각을 변조한다.

1. 정상 calculation_ref에 붙은 위조 총액.
2. 위조 PLAN_CALCULATION 참조.
3. 위조 PRICE_VALIDATION 참조.
4. 정상 검증 target에 붙인 위조 draft_id.

동일 payload는 `BridgeService.check_output(..., for_save=True)`에서 거부되지만,
현재 `AgentRunner.__call__ → check_grounded → finish_agent_run(COMPLETED)`에서는 그대로 저장된다.
네 검사는 저장된 payload와 COMPLETED를 실제 확인한 뒤 전용 RunnerFinalGuardMissing 예외를 발생시킨다.
`xfail(strict=True, raises=RunnerFinalGuardMissing)`로 미해결을 표시하므로 다른 실패가 이 표시로 숨지 않는다.
**4 xfailed는 보안/최종 저장 검증 통과가 아니다.** 프롬프트 강화나 도구 경로 성공으로 해결됐다고 주장하지 않는다.

오케스트레이션 담당의 최소 변경안(미구현):

- Runner의 기존 try 안에서 `check_grounded` 이후, COMPLETED 저장 **이전**에 역할별 최종 출력 검사 훅을 호출한다.
  입력은 신뢰된 `(role, data, RunContext, output)`이다. case/revision/run ID는 RunContext,
  허용 견적 집합은 원래 data.quote_version_ids를 사용하고 현재 저장소와 대조한다. 모델이 보낸 ID로 scope를 만들지 않는다.
- PROPOSED 출력은 업무/revision/정확한 견적 버전/source digest에 묶인 실제 검증 참조와 payload 일치를 요구한다.
  ④는 전체 안·배분·총액·calculation_ref, ⑤는 target/validation_ref/alternative_plan_ref/policy_ref 및
  실제 초안 ID와 target 연결·발송 미승인 상태를 검사한다. 실패/검사기 미설정이면 COMPLETED로 저장하지 않는다.
  아래 준비 작업에서 `check_final_output` 진입점과 읽기 전용 `check_plans`를 구현했다.
  `check_output`은 이제 검증 참조 생성 없이 검사하며, 보류 분기는 전체 snapshot 대신 정책과 독립적인
  check_scope로 범위를 확인한다. Runner 호출 연결 자체는 여전히 미구현이다.
- 완료 저장 시 revision 변경 경쟁은 AgentRunStore/백엔드가 원자적으로 확인해야 한다. 훅의 조회만으로
  조회와 저장 사이 변경을 해결했다고 볼 수 없다. 별도 최종 저장 도구를 만들지 않으며 Runner가 최종 결과 작성자다.
- 그래프 `plan_validation`/`price_validation`은 별도 책임이다. 최신 입력의 공급량·MOQ·포장 배수·배분 정책·
  총액/목표가/생략 정책을 재평가하고 PASS/FIX/NEED_INFO와 이유를 반환한다. 정상 제안의 재수정 루프·③ 이동·
  카드 생성 전 검증을 담당하며, Runner가 위조 참조를 COMPLETED로 저장하는 문제를 사후에 대신 해결하지 않는다.
  기존 nodes.py의 반환 계약을 유지한다. ⑤ SKIPPED의 증명/공통 초안 조회 계약도 해당 담당의 잔여다.

### 직전 27파일 검토본의 실행 명령과 결과

작업 디렉터리: `delivery_alignment_20261029/checkout`. 기존 오프라인 검사기와 기존 team_venv/의존성만 사용했다.
새 패키지·환경·실행기를 만들지 않았다. 최초 일반 venv에는 pytest가 없었고, 기존 의존성은 샌드박스에서 읽기가
차단되어 승인된 권한으로 아래 기존 검사기를 실행했다. 검사기는 환경의 API/DB 키 변수를 제거하고
키 파일 읽기·네트워크를 차단하며 모델·저장소만 대역으로 사용한다.

```powershell
# 제품 수정 전 재현
& ..\..\.venv\Scripts\python.exe -B -X utf8 ..\..\.preupload\delivery_alignment\check.py tests/test_agents45_team_data.py tests/test_agents45_price_tiers.py -k test_review --tb=short
# 수정 후 및 영향받는 기존 회귀 (최종 코드)
& ..\..\.venv\Scripts\python.exe -B -X utf8 ..\..\.preupload\delivery_alignment\check.py tests/test_agents45_team_data.py tests/test_agents45_price_tiers.py tests/test_agents45_product_registration.py --tb=short -rx
```

- 수정 전: check-15, **18 failed / 1 passed**. 수정 전 실패를 유지한 테스트 기준으로 제품을 수정했다.
- 최종: check-17, **113 passed / 4 xfailed** (팀 데이터72, 구간28, 제품 등록13 통과 + 공유 Runner 공백4).
  기존 76개 영향 검사 전부 통과. 의존성 DeprecationWarning 13건이 있으며 제품 검사 실패는 없다.
- OFFLINE_AUDIT: real_key_reads=0, network_attempts=0, evals_or_reports_reads=0.
  실제 모델·DB·Git 업로드는 하지 않았다. 공유 Runner·스키마·그래프·노드 및 다른 역할 코드는 변경하지 않았다.
- AGENTS45_REVIEW.patch는 이후 사용자 요청으로 **세 결함 수정과 미해결 Runner 재현을 포함한 현재 27파일 검토본**으로
  갱신했다. SHA-256은 `3f4d0dee0da8ae97d4fd43fa69571a831f3a8a75374784fb650010958c243e10`이다.
  인계 문서는 패치의 제품·테스트 범위에 포함하지 않으며, 별도 인계 대상이다.

## 기존 담당 브랜치 반영 준비 — 당시 비교 기록

### 저장소와 보존 상태

- 기존 로컬 담당 브랜치가 있는 저장소:
  `C:\Data\02_Projects\Gyeonjwo\gyeonjwo_backend_agents45_review`.
- `refs/heads/feature/agents-4-5` = `1ed40d0182b19c0ff6102f6385978cb869e15180`.
- 해당 작업 트리의 실제 HEAD는 **detached** `541d8368489ac590bb889edbb8b8aa647837991f`다.
  현재 담당 브랜치가 체크아웃되어 있다고 표현하지 않는다. 로컬 origin/feature/agents도 이 과거 커밋을 가리킨다.
- staged 변경 0, 추적 파일의 미커밋 수정 0, **미추적 파일 779개**다.
  app 29 / docs 5 / evals 184 / examples 14 / 합성자료 24 / integration_tests 2 / reports 504 /
  requirements 3 / scripts 1 / tests 12 / tools 1. 과거 작업이며 삭제·stash·덮어쓰지 않았다.
- 검토본 작업 위치: `C:\Data\02_Projects\Gyeonjwo\Agent\delivery_alignment_20261029\checkout`.
  HEAD/feature/agents = `1185b120e8e3dd57d5d86c073ea71a38f0b520d1`.
  이곳의 검토한 27개 제품·테스트 파일과 패치는 그대로 보존하고, 이번에는 이 인계 문서만 갱신했다.

### 브랜치 이력과 적용 차이

기존 로컬 Git 객체로 비교했다. 담당 브랜치 고유 커밋은 **0**, 검토 기준은 **12커밋 앞선 후손**이다.
현재 detached HEAD 대비로는 기준이 **10커밋 앞선 후손**이다.
담당 브랜치→기준 사이 팀 변경은 13파일: app/agents의 README.md·agents.py·tools.py·schemas.py·graph.py·nodes.py,
docs/AGENT1_EXTRACTOR_TEST_REPORT.md, tests/agent_memory_store.py·test_agent_common_tools.py·test_agent_extractor.py·
test_agent_graph.py·test_agent_runner.py·test_agent_schemas.py.
이는 기존 팀 이력으로 별도 정렬해야 할 변경이며 ④·⑤ 신규 구현이나 검토 패치의 추가 대상으로 세지 않는다.

`git apply --check AGENTS45_REVIEW.patch`를 기존 대상 작업 트리에 읽기 전용으로 실행한 결과 **적용 불가**다.
agents.py/tools.py는 기준 문맥이 다르고 신규 패치 중 19개 kan60 파일은 기존 미추적 파일과 겹친다.
반면 검토본 작업 트리에서는 `git apply --reverse --check`가 통과해 현재 제품·테스트가 검토 패치와 맞음을 확인했다.
이 명령은 실제 적용/되돌리기를 하지 않는다.

기존 대상 파일과 검토본의 전체 내용(LF/CRLF 차이 제외)을 비교한 27개 명세:

| 구분 | 대상 파일 (저장소 상대 경로) |
|---|---|
| 동일 14 — 내용 덮어쓰기 불필요, 향후 추적 대상으로 포함 | app/agents/kan60/__init__.py; app/agents/kan60/counter_offer/__init__.py; app/agents/kan60/counter_offer/agent.py; app/agents/kan60/counter_offer/contract.py; app/agents/kan60/counter_offer/draft_contract.py; app/agents/kan60/counter_offer/draft_validation.py; app/agents/kan60/counter_offer/drafting.py; app/agents/kan60/counter_offer/engine.py; app/agents/kan60/counter_offer/model_adapter.py; app/agents/kan60/counter_offer/pricing.py; app/agents/kan60/counter_offer/provider_errors.py; app/agents/kan60/purchase_plans/__init__.py; app/agents/kan60/team_bridge/__init__.py; app/agents/kan60/team_bridge/repository.py |
| 차이 7 — 기존 내용 보존 후 검토본 적용 필요 | app/agents/agents.py; app/agents/tools.py; app/agents/kan60/purchase_plans/contract.py; app/agents/kan60/purchase_plans/negotiation.py; app/agents/kan60/purchase_plans/tools.py; app/agents/kan60/team_bridge/service.py; app/agents/kan60/team_bridge/team_calculations.py |
| 대상에 없음 6 — 신규 반영 대상 | app/agents/kan60/team_bridge/product.py; app/agents/kan60/team_bridge/team_data.py; tests/fixtures/agents45_normal13.json; tests/test_agents45_price_tiers.py; tests/test_agents45_product_registration.py; tests/test_agents45_team_data.py |

차이가 있는 자체 코드의 핵심 diff는 입력 필요성 완화·revision 0/단가 구간/pack_multiple 계약,
배분별 구간 가격·독립 검증·⑤ 재평가, 팀 notice_basis 및 원문/검증 참조 연결,
공유 계산과 정확 계산의 반올림 불일치 차단이다. agents.py/tools.py에는 최신 팀 기준 위의 우리 프롬프트·전용 도구
등록분이 있다. 과거 공통 파일 위에 최신 파일 두 개만 복사하면 스키마/RunContext/저장소 계약이 맞지 않는다.

### 다음 반영의 범위와 보류 지점

1. 기존 779개 미추적 작업과 현재 detached HEAD를 보존한 상태를 먼저 유지한다. `git clean`, 강제 전환,
   브랜치 포인터 강제 이동, 기존 파일 일괄 덮어쓰기를 하지 않는다.
2. 담당 브랜치가 검토 기준보다 뒤처져 있으므로, 실제 반영 전 팀 기준 이력 정렬과 미추적 겹침 파일의 보존 방법을
   먼저 확정해야 한다. 이번에는 병합/브랜치 전환/파일 복사를 실행하지 않았다.
3. 반영 명세는 위 **제품·테스트 27파일**과 **이 인계 문서 1파일**이다. 이전 evals/보고서/실행기/환경 파일/ZIP은
   반영 대상에 추가하지 않는다. 기존 자료를 삭제하거나 과거 검사까지 현재 코드 검증으로 승격하지 않는다.
4. 반영 후에는 27파일을 검토 패치와 전체 내용 대조하고 정확한 대상 diff만 확인한다. 기준 또는 코드가 달라지면
   영향 검사만 판단한다. 이번 준비 작업은 새 기능·테스트 재실행 없이 목록·diff·이력의 읽기 전용 확인과 문서 갱신이다.

위 준비 당시에는 commit·push·PR·병합을 실행하지 않았다. 이후 사용자 요청으로 검토한 28파일을
85501db870d69da226a911992532b33145b2312e로 commit·일반 push했다. 아래 추가 변경은 미커밋이다.

## 읽기 전용 최종 출력 검증 — Runner 연결 준비 (2026-10-30)

우리 쪽 진입점은 `app.agents.kan60.team_bridge.product.check_final_output(role, data, context, output)`이다.
`role`은 ④ RESPONSE_PLAN 또는 ⑤ COUNTER_OFFER, `data`는 Runner가 보유한 원래 PlanIn/CounterOfferIn,
`context`는 Runner가 만든 RunContext, `output`은 check_grounded 이후의 최종 응답이다.
업무·revision은 data/context 일치와 저장소로 확인하고 견적 집합은 원래 data.quote_version_ids를 사용한다.
모델 응답·도구 인자로 scope를 재구성하지 않는다. 정상 반환은 검증된 PlanOut/CounterOfferOut이며 참조를 교체하지 않는다.

- ④: check_plans가 원본 입력에서 재계산한 전체 구매안·배분·총액과 실제 PLAN_CALCULATION/PLAN_SET을 대조한다.
  기존 validate_plans는 도구의 명시적 참조 생성 경로로 유지하며 최종 검사에서는 호출하지 않는다.
- ⑤: PRICE_VALIDATION → OFFER_CALCULATION → PLAN_SET/POLICY를 조회해 목표 조건·가격·근거·검증 결과를 대조한다.
  원래 CounterOfferIn.validated_plans 전체도 읽기 전용으로 검증한다. 기존 초안은 resolve_draft로 조회하며
  동일 target/validation_ref 및 send_authorized=False 결합을 요구한다. 새 초안·참조·결과를 저장하지 않는다.
- HOLD/NEEDS_EVIDENCE/RECHECK_SOURCE는 금지된 제안 payload, 분기·unknown_fields 계약과 scope만 검사한다.
  정책·보충 사실·검증 저장소가 없어도 정상 정책 HOLD는 통과한다. 권한/revision/견적 범위 확인 자체가 불가능하면 거부한다.
  RECHECK_SOURCE는 팀 추출 필드만 허용하며 내부 의존성은 NEEDS_EVIDENCE로 보내지 않는다. SKIPPED는 계속 미지원이다.

오케스트레이터 연결 위치는 **AgentRunner.__call__의 기존 try 안, check_grounded 다음, finish_agent_run(COMPLETED) 이전**이다.
④·⑤에서만 `output = check_final_output(role, data, context, output)`을 호출한다. 불일치/참조 누락은 BridgeError(code),
의존성 누락은 그 하위 MissingTeamData(fields), 스키마 오류는 FINAL_INPUT_OR_OUTPUT_SCHEMA_INVALID로 실패한다.
그 밖의 저장소 예외도 전파한다. 어떤 검사 예외도 HOLD로 바꿔 완료 저장하지 말고 기존 FAILED 처리로 넘겨야 한다.
검사는 저장을 하지 않으므로 이후 COMPLETED 저장 시 revision·권한의 원자적 재확인은 AgentRunStore/백엔드 책임이다.

resolve_draft(scope, ref)는 우리 저장소 경계의 조회 요구이며 공통 초안 계약 확정/구현을 뜻하지 않는다.
TeamReviewRepository는 현재 create_mail_draft.resolve 누락으로 거부한다. ValidationStore의 DRAFT 행을 대용하지 않는다.
실제 공통 초안 ID의 존재·업무·revision·견적/source 결합·target/검증 참조·발송 미승인 조회를 담당자가 연결해야 한다.
정상 ⑤ PROPOSED 검사는 합성 초안 저장소 대역에서만 확인했다. 실제 모델·DB·메일·Runner 연결 완료를 뜻하지 않는다.

기존 검사기로 관련 세 파일만 실행했다. 명령은 위 check.py 명령과 동일하며 최종 기록은 `verification/check-20`이다.
결과는 **167 passed / 4 xfailed**, 의존성 DeprecationWarning 13건이다. 정상/위조/정상 보류/범위 위반/조회 누락 및
반복 검사에서 참조·초안·결과 저장이 없음을 확인했다. OFFLINE_AUDIT는 real_key_reads=0, network_attempts=0,
evals_or_reports_reads=0이다. 중간 check-18/19의 새 테스트 설정 오류(불변 대역 수정·먼저 발생하는 scope 오류 기대값)는 수정했다.
공통 Runner·schemas.py·graph.py·nodes.py와 기존 4건의 strict xfail은 변경하지 않았다. Runner가 호출하기 전까지 4건은 미해결이다.
직전 AGENTS45_REVIEW.patch는 그대로 보존하며 이번 미커밋 변경을 포함하지 않는다.
