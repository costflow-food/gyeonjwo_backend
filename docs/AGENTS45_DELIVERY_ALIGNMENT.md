# ④·⑤ 팀 납품 연결: 입력 필요성·종료 분기·지원 범위

2026-10-30 갱신. 기존 delivery_alignment_20261029/checkout에서 작업했다.
팀 기준은 checkout의 1185b120e8e3dd57d5d86c073ea71a38f0b520d1이며 이번에는 원격을 다시 조회하지 않았다.
새 checkout/폴더/ZIP/실행기를 만들지 않았다. 실제 모델·DB·Git 업로드는 실행하지 않았다.
아래의 현행 내용이 이전 문서에서 모든 보충 필드를 필수로 나열한 설명을 대체한다.
현재 검토본은 외부 패치 검토의 원문 숫자 대조·pack_multiple 누락·revision 0 불일치 세 결함을 수정한 버전이다.
수량별 단가 구간을 유지하며 공유 Runner·스키마·① 추출·공통 도구·그래프·최종 노드는 수정하지 않았다.

### 검사 기록과 현재 잔여 요약

| 시점/대상 | 결과 | 의미 |
|---|---|---|
| 단가 구간 구현 직후의 과거 코드 (`verification/check-13`) | 76 passed | 외부 재검토의 세 결함 수정 전 기록. 현재 최종 결과로 표시하지 않음 |
| 세 결함 수정 후 현재 27파일 검토본 (`verification/check-17`) | 113 passed / 4 xfailed | 기존 76개를 포함한 통과 113개. 4건은 Runner 저장 전 검증 공백의 미해결 재현이며 통과가 아님 |
| 사용자 제공 외부 재검토 | 113 passed / 4 xfailed 재현 | 사용자가 재현과 최신 feature/agents=1185b120… 일치를 확인. 이번 브랜치 준비 작업에서는 테스트·원격 조회를 반복하지 않음 |

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
  기존 `check_output(for_save=True)`가 출발점이나 그대로 모든 분기에 붙이지 않는다:
  현재 ④ validate_plans는 재검증 참조도 저장하고, check_output의 마지막 snapshot은 정책 누락 HOLD까지 막을 수 있다.
  저장 전 훅은 검증과 기록 생성을 분리하고, HOLD/NEEDS_EVIDENCE/RECHECK_SOURCE에서는 금지 결과가 없는지와
  분기·필드 계약을 검사해야 한다. 누락 정책 자체가 정당한 HOLD를 예외로 바꾸면 안 된다.
- 완료 저장 시 revision 변경 경쟁은 AgentRunStore/백엔드가 원자적으로 확인해야 한다. 훅의 조회만으로
  조회와 저장 사이 변경을 해결했다고 볼 수 없다. 별도 최종 저장 도구를 만들지 않으며 Runner가 최종 결과 작성자다.
- 그래프 `plan_validation`/`price_validation`은 별도 책임이다. 최신 입력의 공급량·MOQ·포장 배수·배분 정책·
  총액/목표가/생략 정책을 재평가하고 PASS/FIX/NEED_INFO와 이유를 반환한다. 정상 제안의 재수정 루프·③ 이동·
  카드 생성 전 검증을 담당하며, Runner가 위조 참조를 COMPLETED로 저장하는 문제를 사후에 대신 해결하지 않는다.
  기존 nodes.py의 반환 계약을 유지한다. ⑤ SKIPPED의 증명/공통 초안 조회 계약도 해당 담당의 잔여다.

### 실행 명령과 결과

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

## 기존 담당 브랜치 반영 준비 — 비교 완료, 반영·병합 미실행

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

commit·push·PR·병합은 미실행이며, 현재 상태를 담당 브랜치 반영 완료로 표현하지 않는다.
