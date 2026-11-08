# 깐마늘 합성데이터 작성·적재·실행 가이드

2026-11-08. 기존 `gyeonjwo_합성데이터_작성적재_가이드_2026-11-06.md`의 묶음·단계·원문 근거 원칙을 유지한 깐마늘 버전이다.

## 1. 준비된 것과 실행 담당자의 작업

| 준비물 | 파일 / 위치 | 현재 상태 |
| --- | --- | --- |
| 시나리오·업체·기대 결과·검산 | `scenario_garlic_daeseo.md` | 작성·로컬 검증 |
| 제목·본문·답장 순서 | `mails_garlic_daeseo.md` | 작성 |
| 기준자료 시드 | `seed_garlic_daeseo.sql` | 실제 컬럼·제약 대조 완료, 실행 전 |
| 견적·보완 첨부 | `attachments/` PDF 5개 | 실제 파서 READ/1쪽 및 ①→⑤ 근거 검증 |
| 실행용 소유자·메일함·주소 | 담당자가 지정 | 아직 미지정 |
| 정책·발송 설정 | 앱/서버 담당자 관리 | 설정 확인 필요 |
| 실서비스 E2E 기록 | 아래 실제 결과 양식 | 실제 수신·발송·DB 커밋·화면은 미실행 |

이 묶음의 `.env`나 Flutter SDK를 우영이 새로 만들 필요는 없다. 백엔드 담당자는 실제 서버 환경변수와 브랜치, 프런트 담당자는 SDK·화면 빌드, DB 담당자는 시드 적재와 저장 계약을 담당한다. 현재 검증용 실행기는 이 환경 없이도 도구·PDF·그래프·카드 계약을 검사한다.

## 2. 기존 가이드에서 바꾼 부분

- ‘미사용 품목만 사용’ 대신 **깐마늘을 쓰되 테스트 소유자를 분리**한다. 기존 계정에 마늘 자료가 있으면 제공 SQL이 중단한다. 원본 망고·딸기·기존 마늘 자료를 덮어쓰지 않는다.
- 회사 정책 4%를 ‘자동으로 항상 있음’으로 가정하지 않는다. 실제 업무 스냅샷을 확인한다. 없으면 역제안은 NO_POLICY로 보류한다.
- ‘③④⑤ DB 함수 배포 전’은 11월 6일 당시 설명이다. 최신 망고 점검에는 관련 함수 배포가 확인됐고, 실제 회신 검토는 INFO_EXTRACTOR(REPLY)에서 실패했다. 배포됐다는 사실과 E2E 성공을 구분한다.
- 최신 코드에는 다중 메일 정보추출 호출 한도와 규격 구분자 정규화 수정이 들어 있다. 규격 공백·구분자만 다르다고 무조건 불일치로 판정하지 않는다.
- 단가 단위는 전부 kg로 통일한다. 포장 20kg는 규격이다. 박스/kg 혼합을 현재 DB 카드까지 통과시켰다고 표시하지 않는다.
- 발송 설정은 앱에서 저장한다. `send_settings_after_case.sql`을 새로 만들거나 과거 SQL로 우회하지 않는다.

## 3. 데이터 계약

직접 적재하는 표는 `suppliers`, `supplier_items`, `purchase_history`, `company_items` 네 개뿐이다.
업무·메일·견적·실행 기록·검토 카드는 서비스가 생성해야 한다.

| 표 | 기대 건수 | 확인 |
| --- | ---: | --- |
| suppliers | 5 | 고유 이름, REGISTERED, seed/role/synthetic 근거 |
| supplier_items | 5 | 품목명 `깐마늘` 동일, 규격 `국산 대서 상품, 20kg 포장`, unit kg, weight_per_box 20 |
| purchase_history | 3 | 기존 9,400×1,000 / A 9,700×300 / D 9,500×200, 원화, 합성 근거 |
| company_items | 1 | 공급처 품목과 같은 자동 키, 동일 규격, 구매자 필수 조건 HACCP |

`standardized_item_key`는 직접 만들지 않고 DB 트리거에 맡긴다. 로컬 검증 JSON의 UUID는 메모리 저장소용이다. SQL은 실제 적재 시 새 UUID를 만들므로 JSON UUID를 실DB 조회 키로 쓰지 않는다.

## 4. 시드 적재

1. DB 담당자가 개발 DB와 시나리오 전용 구매자 계정을 확인한다.
2. SQL 상단 `v_owner`, `v_buyer_email`, `v_emails`를 입력한다. 이메일 배열 순서는 **기존, A, 제외 B(NULL), C, D**이다.
3. 업체 메일 4개는 실제 수신 가능한 테스트 주소이며 서로 달라야 하고 구매자 주소와도 달라야 한다. 이전 테스트가 진행 중이면 같은 주소를 재사용하지 않는다.
4. SQL Editor에서 파일 전체를 실행한다. 하나의 트랜잭션이며 기존 시드·이름·주소·마늘 품목이 겹치면 변경 전에 중단한다. 재실행해서 기존 것을 갱신하는 스크립트가 아니다.
5. 아래 Q0을 실행해 건수·품목 키·이력을 확인한다. 실패하면 다른 팀 데이터를 삭제해서 우회하지 않는다.

제공 SQL은 2026-11-08 실제 표의 컬럼·제약과 대조했다. **개발 DB에 실행하거나 적재 성공을 확인한 상태는 아니다.**

## 5. 서비스 설정과 실행

- 백엔드: 작업 브랜치의 `DATA_GO_KR_API_KEY` 읽기 확인. 이미 발급받은 공공데이터 키를 사용하며, endpoint는 코드에 고정되어 있다.
- LLM: 서버가 쓰는 endpoint/key, 모델별 설정을 백엔드 담당자가 확인한다. 문서·채팅·Git에 키 값을 넣지 않는다.
- 프런트: 해당 변경 브랜치 적용 후 `flutter analyze`, 파싱 회귀 테스트, 검토 카드/후보 화면을 실행한다.
- 구매자 정책: 허용 인상률 0.04, 기존 업체 선호 폭 0.04, 분산 true, 최대 업체 2를 실제 업무 스냅샷에서 확인한다.
- 발송 설정: RFQ·보완·역제안 세 목적 모두 **승인 후 발송**으로 저장한다. 설정/정책은 시드 SQL로 조작하지 않는다.
- RFQ 세 건을 전부 발송한 뒤 대안 회신을 보낸다. 반드시 각 업체의 RFQ에 답장한다. 보완 회신 역시 실제 보완 요청에 답장한다.

## 6. 공공 시세 확인 기준

카드에는 ‘공공 시세 참고’, **중도매**, 국산/대서/상품, 원자료 20kg와 환산 kg, 조사일·조회 시각·출처를 보여야 한다.
현재 공급가를 공공가격으로 대체하지 않는다. 임시 유효기간 7일을 넘긴 개별 시리즈는 오래된 자료로 표시한다.
소매인 수입 냉동 블루베리는 별도 시리즈로 이미 지원하지만, 이번 업체/메일 세트는 깐마늘이다. 냉동 딸기나 냉동 삼겹살에 깐마늘 값을 붙이지 않는다.

실행일 가격은 공공기관의 최신 조사값을 읽는다. 8,960원/kg은 사용자가 받은 11월 6일 응답의 재현값이므로 최신값이 달라졌다고 실패로 판정하지 않는다.

## 7. 확인 쿼리

아래 `:owner`와 `:case`를 실제 UUID로 바꾼 뒤 읽기 전용으로 실행한다.

```sql
-- Q0: 시드와 업체별 이력. 기대 업체/품목 5행, purchase_count 합계 3, 품목 키 1종.
select s.name,s.contact_email,i.item_name,i.specification,i.unit,i.weight_per_box,
       i.standardized_item_key,i.certifications_jsonb,
       (select count(*) from public.purchase_history h
        where h.owner_user_id=s.owner_user_id and h.supplier_item_id=i.id) purchase_count
from public.suppliers s join public.supplier_items i
  on i.owner_user_id=s.owner_user_id and i.supplier_id=s.id
where s.owner_user_id=':owner' and s.evidence_jsonb->>'seed'='it-garlic-daeseo-2026-11'
order by s.name;
select item_name,standardized_item_key,mandatory_conditions_jsonb
from public.company_items where owner_user_id=':owner' and item_name='깐마늘';

-- Q1: 업무 상태·수량·정책. 회신 연결 때 revision 증가 여부 포함.
select id,status,processing_step,input_revision,review_revision,item_name,specification,quantity,unit,
       mandatory_conditions_jsonb,policy_jsonb,error_jsonb,decision
from public.work_cases where id=':case' and owner_user_id=':owner';

-- Q2: 메일 연결 및 첨부 상태. PDF 5개가 READ인지 확인.
select direction,message_role,link_status,processing_status,sender,subject,
       normalized_jsonb->'documents' documents
from public.mail_messages where work_case_id=':case' and owner_user_id=':owner'
order by sent_or_received_at;

-- Q3: 견적 및 첨부 근거. 보완 회신 후 C의 새 버전과 가격 근거 승계 확인.
select s.name,q.version_no,q.unit_price,q.unit,q.available_quantity,q.effective_from,
       q.evidence_jsonb->'unit_price'->>'location' price_location,q.checks_jsonb
from public.quote_versions q join public.suppliers s
  on s.owner_user_id=q.owner_user_id and s.id=q.supplier_id
where q.work_case_id=':case' and q.owner_user_id=':owner' order by s.name,q.version_no;

-- Q4: 단계 번호 대신 실제 agent_role, status, next_action으로 판정.
select agent_role,status,output_jsonb->>'next_action' next_action,
       output_jsonb->>'reason' reason,error_jsonb,started_at
from public.agent_runs where work_case_id=':case' and owner_user_id=':owner' order by started_at;

-- Q5: 초안·발송. 후보 확정 전 내부 스테이징 초안은 이 표에 없을 수 있다.
select purpose,recipient,status,sent_at,left(body_text,160) body
from public.mail_drafts where work_case_id=':case' and owner_user_id=':owner' order by created_at;

-- Q6: 카드와 완료 결과. 숫자는 scenario_garlic_daeseo.md와 대조.
select status,processing_step,review_revision,jsonb_pretty(latest_review_jsonb)
from public.work_cases where id=':case' and owner_user_id=':owner';
```

## 8. 재현 명령과 기록

백엔드 개발 환경에서:

```powershell
python -m scripts.check_market_scenario --attachments-dir tests/fixtures/garlic_attachments
python -m pytest tests/test_market_reference.py tests/test_review_completion_contract.py -q
# 백엔드 담당자 환경에서만: 실모델 + 공공 API. 저장소는 여전히 메모리, 실제 발송 없음.
python -m scripts.check_market_scenario --attachments-dir tests/fixtures/garlic_attachments --live-llm --live-market --env <서버_개발환경_env_경로>
```

프런트 개발 환경에서:

```powershell
flutter analyze
flutter test test/market_and_web_reference_test.dart
```

실행 기록에는 날짜, 코드 SHA, DB 버전, 업무 ID, 단계별 기대/실제, 오류와 재현 조건을 쓴다. 모델 호출이 없는 로컬 검사와 실제 모델·Gmail·DB·화면 검사를 각각 표시한다.

## 9. 남은 담당 범위

| 영역 | 이번에 진행한 것 | 남은 확인 |
| --- | --- | --- |
| ①~⑤ | 본문 및 PDF 근거 저장→보완→5개 구매안→역제안→카드 로컬 연결 | 서버 LLM 호출·Gmail 이벤트·RPC 커밋 |
| M4-1 승인 | 초안별 수동 승인 필요 조건을 API와 발송 노드가 공유 | 실제 화면의 승인 버튼·승인 후 발송 |
| M6-1 완료 초안 | 재시도 전 초안 ID를 최종 완료 목록에서 제외 | DB complete RPC 실호출 |
| 시세 | 정확한 시리즈·단위·날짜·캐시·장애 처리 | 서버 네트워크에서 실제 재조회 |
| 크롤링 | 수집 결과를 후보 화면에 연결, 차단과 빈 결과 구분 | 실제 브라우저 검색·연락처 표시 |
| KAN-178 | 구매안 없음이 현재 카드 저장 계약과 충돌함을 명시 | 0건 카드의 그래프/DB 완료 계약 변경·통합 검증 |
| 재시도·수동 회신 연결 등 | 최신 망고 문서의 미완료 DB 항목으로 인계 | 담당 DB/백엔드 작업과 실서비스 재검증 |

이번 묶음으로 코드와 기대 결과를 준비했지만, 팀 공용 서버의 E2E 성공 또는 Jira 완료 상태를 대신 선언하지 않는다.
