# 공공 시세·크롤링·깐마늘 시나리오 연결 검증

2026-11-08 작업 기록. **로컬 구현과 오프라인 연결 검증을 마쳤으며, 실서비스 E2E 완료로 판정하지 않는다.**

## 작업 위치와 기준

- 백엔드: `gyeonjwo_backend_e2e`, 브랜치 `feature/market-reference-e2e`
- 백엔드 기준: `origin/dev_1.0`, `8bc06ad0d1a3b44da5c6b671d998b3c4b2c33a73` (PR #70 반영)
- 프런트: 인접 `gyeonjwo_frontend_e2e`, 같은 이름의 브랜치
- 프런트 기준: `origin/dev`, `c8491a39289c967d6639fffcf366d1ddf4faf046`
- 원래 `C:\Data\02_Projects\Gyeonjwo`의 코드와 `.env`는 수정하지 않았다. PR·push·DB 변경·메일 발송 없음.

## 시세 소재

| 소재 | 확인된 자료 | 이번 적용 |
| --- | --- | --- |
| 국산 깐마늘, 대서, 상품 | 중도매 조사, 코드 `258/03/02/04` | 첫 ①→⑤ 통합 시나리오 |
| 수입 냉동 블루베리, 상품 | 소매 조사, 코드 `429/02/01/04` | 같은 조회 모듈의 두 번째 품목; 소매로 명시 |
| 냉동 딸기 | 사용자가 조회한 466행에서는 설향 딸기만 확인 | 냉동품 가격으로 대체하지 않음 |
| 수입 냉동 삼겹살 | 같은 응답에서는 미발견 | 이 API로 확인되지 않음; 다른 공공자료 전체의 부재를 뜻하지 않음 |

출처: [aT 최근일자 도·소매가격 정보](https://www.data.go.kr/data/15156063/openapi.do), `https://apis.data.go.kr/B552845/recent/price`.
사용자가 성공적으로 받은 응답의 2026-11-06 조사값은 깐마늘 179,200원/20kg(8,960원/kg), 냉동 블루베리 11,567원/kg이다. 이는 이번 환경에서 재조회한 값이 아니다.

공공 조사는 순간 체결가가 아니다. 화면 명칭은 **공공 시세 참고**로 두고 조사일·유통 단계·등급·원단위·kg 환산가·조회 시각을 표시한다. 납품 견적과의 차이는 물류·포장·조건 차이를 포함할 수 있으므로 인상의 부당성이나 역제안 목표가로 자동 해석하지 않는다.

`recent` 응답에 2018년 등 오래된 개별 시리즈가 섞인 것을 확인했다. 품목명 포함 검색 대신 품목/품종/유통단계/등급 4개 코드를 맞추고, 미래 날짜·중복·중량 외 단위·비정상 수치는 사용하지 않는다. 7일을 넘긴 값은 `STALE`로 표시하고 차이 비교에서 제외한다. **7일은 이번 MVP의 임시 기준이며 공공기관 보증 주기가 아니다.**

## 연결한 코드

- `app/features/work_cases/application/market_reference.py`: 두 품목의 공공 참고가격, 페이지 완전성 확인, Decimal 중량 환산, 전일/주/월 변동, 동일 규격 견적 차이. 기존 httpx 사용, 신규 운영 의존성 없음.
- `GET /api/v1/work-cases/{case_id}/market-reference`: 로그인과 업무 소유자 확인 후 조회. 카드 생성 전에도 호출 가능.
- 기존 검토 카드(API-14)의 `market_reference`: 기존 카드·계산 결과와 함께 반환. 가격 도구가 없거나 실패해도 구매안 계산은 그대로 유지.
- 성공 응답 캐시 5분, 실패 캐시 30초, 요청별 타임아웃 3초, 페이지 반복 시간 예산 12초. 첫 조회에는 외부 요청 지연이 추가될 수 있다. 캐시는 프로세스별이며 주기적 백그라운드 수집은 아니다.
- 인증키는 환경변수로만 받는다. 제공기관이 쿼리 키를 요구하므로 URL 전체를 INFO에 기록하는 Client 경로를 피하고 transport로 요청한다. 예외 원문도 반환/기록하지 않는다. 인코딩/디코딩 키 모두 한 번만 URL 인코딩되도록 처리한다.
- 프런트 카드에 참고가격·견적 차이를 표시했다. 과거 카드의 `plan_id=null` 때문에 화면 전체가 실패하던 파싱을 보완하고, ID 없는 안은 선택할 수 없게 했다.

`DATA_GO_KR_API_KEY`를 백엔드 `.env`에 둔다. endpoint는 위 고정 출처이므로 `.env`에 추가로 적지 않는다. 이번 구현은 최근값과 응답 내 전일/주/월 값만 사용하며 기간별 API를 이용한 시계열 그래프는 포함하지 않는다.

## 크롤링 확인

[PR #57](https://github.com/costflow-food/gyeonjwo_backend/pull/57)의 연결은 현재 백엔드에 있다. 등록 후보가 없을 때 ②가 웹 도구를 사용할 수 있고, 검색 결과는 `web_observations`로 별도 보존된다. 해당 결과는 등록 거래처·검증된 견적이 아니다.

현재 프런트에는 이 필드의 파싱과 표시가 빠져 있었다. API-11 → CandidateSet → 후보 화면을 연결하고 `NO_SUITABLE_CANDIDATES` 보류에서도 볼 수 있게 했다. 검색 차단과 검색 결과 없음은 구분한다. 공개 이메일·전화·홈페이지를 보여주며, 웹 결과만으로 거래처 등록이나 발송을 수행하지 않는다.

네트워크 주소 검증, 도구 근거 → 후보 결과 → 저장 스냅숏, 미등록 결과의 선택 금지 등 기존 경계 테스트 6건을 실행했다. 현재 실행 환경에 bs4/scrapling/브라우저가 없어 실제 검색·홈페이지 파싱 및 브라우저 관련 4건은 실행하지 못했다. 새 검색 성공 사례가 생긴 것으로 보고하지 않는다.

## 개편한 시나리오

**품목: 깐마늘 / 규격: 국산 대서 상품, 20kg 포장 / 구매 수량: 1,000kg.**
모든 견적의 단가 기준은 kg, MOQ 100kg, 발주 배수 20kg, 부가세 면세로 통일한다. 20kg는 포장 규격이며 단가 기준 단위를 박스로 바꾸지 않는다.

| 거래처(합성) | 단가(원/kg) | 공급량 | 운송비 |
| --- | ---: | ---: | --- |
| 시연마늘농산(기존) | 9,400 → 10,340 | 1,000kg | 무료 |
| 시연대서유통(A) | 9,700 | 1,000kg | 무료 |
| 시연미확인농산(제외 B) | 없음 | 미검토 | 필수 인증 불충족으로 제외 |
| 시연깐마늘식품(C) | 9,600 | 1,000kg | 미확인 → 추가 회신에서 1회 70,000원 |
| 시연알마늘(D) | 9,500 | 600kg | 무료 |

정책: 분산 허용, 최대 2개 업체, 허용 인상률 4%, 기존 거래처 프리미엄 4%. 거래처와 견적은 전부 합성이며 실거래 가격이 아니다. 이메일은 발송할 수 없는 `example.invalid`를 사용했다.

1. ① 인상 통보 저장 → ② 등록 후보 A/C/D 판정, 필수 인증이 없는 B 제외, RFQ 초안 3개 → `WAITING_REPLY`.
2. 세 업체 회신 저장 → ③ C의 운송비 확인 요청. A와의 역전 기준 100,000원. D는 전체 물량을 단독 공급할 수 없어 단독 경쟁 기준에서 제외 → `WAITING_REPLY`.
3. C가 운송비 70,000원/배송 1회라고 추가 회신 → ③ 비교 가능 판정 → ④ 유지·전환·분산 → ⑤ 정책 기반 역제안 초안 → 검토 카드 → `COMPLETE`.

문서의 C/D는 실행기 내부 키 `b`/`c`에 해당한다. 기존 3종 세트의 제외 B 역할을 보존한 표기다.

| 검산한 구매안 | 총액 |
| --- | ---: |
| 기존 유지 | 10,340,000원 |
| C 전량 전환 | 9,670,000원 |
| A 전량 전환 | 9,700,000원 |
| A 400kg + D 600kg | **9,580,000원** |
| C 400kg + D 600kg | 9,610,000원 |

역제안 상한은 `min(9400×1.04, 9580×1.04) = 9,776원/kg`. 이는 정책상 허용 상한으로 선택한 합성 테스트 값이며 최저 구매가격은 아니다. 공공 참고가격 8,960원은 이 계산에 넣지 않는다.

합성 입력 원문: [garlic_market_scenario.json](../tests/fixtures/garlic_market_scenario.json). 메일 5개(초기 1, 후보 회신 3, 보완 1)와 업체·취급 품목·정책을 포함한다. 실행기에서 재생성하므로 별도 수작업 데이터와 엇갈리지 않게 한다. 이 파일은 테스트 입력 자료이며 Supabase에 바로 넣는 SQL seed가 아니다.

## 검증 범위와 남은 항목

실제 PDF 전처리·도구·검증기·LangGraph 전이·검토 카드 저장 검증·API 응답 변환을 실행했다. **역할 선택은 결정적 테스트 드라이버, 저장은 메모리 저장소다. Gmail/큐 워커/Supabase RPC/UI/LLM까지 포함한 E2E가 아니다.** 본문형과 텍스트 PDF 첨부형을 각각 검사했다. PDF 5개가 모두 READ/1쪽으로 읽혔고 PDF 파일명·쪽 번호가 가격 근거에 보존됐다. 스캔 이미지 OCR은 검사하지 않았다.

최종 실행 결과: 본체 회귀 123건과 크롤링 경계 6건, **총 129 통과 / 실모델 3건 건너뜀 / 브라우저 관련 4건 제외**. 전체 저장소 테스트 전부를 실행했다는 뜻은 아니다. 프런트용 파싱 회귀 2건은 작성했으나 SDK 부재로 미실행이다.

최신 원본 `MVP1_냉동망고_E2E_테스트 1.html`(11/6 16:50 KST 기준)을 추가 대조했다. 문서의 ‘③ 회신 추출’은 진행 단계 번호이고 역할은 ① INFO_EXTRACTOR(REPLY)다. 다음 두 코드 문제를 재현·수정했다.

- M4-1: 개별 초안이 수동 승인을 요구하는데 API가 메일함의 자동 발송 설정만 보여 주던 불일치. 발송 노드와 API가 같은 초안 승인 판정을 사용하도록 했다. 사용자 저장 설정·설정 버전은 바꾸지 않는다.
- M6-1: ⑤ 재시도 전에 생성된 초안 ID가 누적돼 최종 카드와 완료 결과가 달라지던 문제. COMPLETE 결과에는 최종 역제안 결과가 참조한 초안만 포함한다. 제안/생략/보류 세 가지 최종 결과를 회귀검사했다.

실행 중인 백엔드의 환경변수·실모델 연결은 백엔드/DB 담당자가, Flutter SDK와 화면 실행은 프런트 담당자가 관리한다. 준비한 [깐마늘 시나리오](../tests/data/integration/garlic/scenario_garlic_daeseo.md), [메일 세트](../tests/data/integration/garlic/mails_garlic_daeseo.md), [적재·실행 가이드](../tests/data/integration/garlic/gyeonjwo_garlic_data_setup_guide.md), [seed SQL](../tests/data/integration/garlic/seed_garlic_daeseo.sql)을 함께 인계한다. SQL은 실제 스키마와 대조했지만 실행하지 않았다.

- 정상 시나리오: 5개 역할, RFQ 3·보완 요청 1·역제안 1, 고유 plan_id 5개, 카드 선택 가능 확인.
- 정책 누락: 구매안은 보존, 역제안 `HOLD/NO_POLICY`, `REVIEW_REQUIRED_CLOSED`, 임의 목표가 없음.
- 시세 키 누락/조회 장애: 상태만 표시하며 구매안 총액·역제안 상한은 유지.
- 기존 보류·후보 테스트를 최신 병합 코드의 원문 근거 계약, 공급 가능량 필수, 규격 구분자 정규화에 맞춰 정비했다. 에이전트 계산 로직을 테스트에 맞춰 약화하지 않았다.
- 개발 DB를 읽었을 때 망고 사례 `cb832d0e-1df1-4915-89be-50928dec1146`의 실패 역할은 **① INFO_EXTRACTOR**였다. ③ 실행 기록은 없어, 현재 증거로 “③ 충돌”을 확정할 수 없다. 실패 시각은 PR #70 병합 전이다. 해당 사례에는 4% 정책이 이미 있다.
- [PR #70](https://github.com/costflow-food/gyeonjwo_backend/pull/70)에 다중 메일 도구 한도·운송비/공급량·규격 등 수정이 반영돼 있다. [PR #65](https://github.com/costflow-food/gyeonjwo_backend/pull/65)의 plan_id/근거 저장 수정도 기준 코드에 있다. 실모델 재실행은 별도 확인이 필요하다.
- KAN-178의 **구매안 0건 카드**는 현재 DB `review_card_check`가 구매안 1~5건 및 ④·⑤ 결과를 요구해 수용하지 못한다. 그래프의 보류와 DB 종료 계약을 함께 바꿔야 하므로 완료로 처리하지 않았다. 정상 흐름만 통과시켜 이 문제를 숨기지 않는다.
- DB는 구매안 배분 견적의 `unit`이 업무의 `unit`과 같아야 한다. 이번 시나리오는 kg로 통일했다. 박스·kg 혼합을 실DB에서 해결했다고 주장하지 않는다.
- KAN-94/95/177/179: 연결·계산·카드 계약 회귀 확인, 실서비스 검증 남음. KAN-98: 표시 누락 수정, 실크롤링 재검증 남음. KAN-183: 합성 입력 작성 완료, 개발 DB 적재·실계정 실행 남음. Jira 상태는 변경하지 않았다.

현재 원본 `.env`에는 `DATA_GO_KR_API_KEY`만 있어 실모델 명령은 `LLM endpoint and key must be configured`로 중단됐고 모델 호출은 0회였다. 이번 실행 환경에서 실제 공공 API 호출은 `SOURCE_UNAVAILABLE`; 사용자 PC의 기존 성공 응답과 구분한다. Flutter/Dart SDK가 없어 프런트 분석·테스트·빌드는 미실행이다.

## 재현 명령

정상적인 백엔드 개발 환경(프로젝트 의존성 설치 완료)에서:

```powershell
python -m scripts.check_market_scenario
python -m scripts.check_market_scenario --attachments-dir tests/fixtures/garlic_attachments
python -m scripts.check_market_scenario --fixture-output tests/fixtures/garlic_market_scenario.json
python -m scripts.check_market_scenario --live-market --env "C:\Data\02_Projects\Gyeonjwo\gyeonjwo_backend\.env"
# LLM_BASE_URL / LLM_API_KEY가 준비되면 실모델을 사용한다. 여전히 DB 쓰기/발송 없음.
python -m scripts.check_market_scenario --live-llm --live-market --env "C:\Data\02_Projects\Gyeonjwo\gyeonjwo_backend\.env"
```

이번 작업 폴더의 복구된 실행 환경에서는 먼저:

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Resolve-Path '..\.agent5-deps').Path + ';' + (Resolve-Path '..\.e2e-deps').Path
& '..\.agent5-venv\Scripts\python.exe' -m scripts.check_market_scenario
```

회귀검사:

```powershell
python -m pytest tests/test_market_reference.py tests/test_review_completion_contract.py tests/test_hold_codes.py tests/test_hold_integration.py tests/test_agent_candidate.py scripts/check_graph_hold.py scripts/check_review_start.py scripts/check_review_queue.py -q
python -m pytest tests/test_collector_orchestration.py -k "not container_uses and not calls_scoped and not registry_and and not revision_change_after" -q
```

Flutter 설치 환경에서 프런트 브랜치 확인 후:

```powershell
flutter analyze
flutter test test/market_and_web_reference_test.dart
```

실서비스 완료 판정은 합성 업체/메일을 승인된 개발 계정에서 준비한 뒤 ① 저장 → ② RFQ → 회신 → ③ 보완 요청 → 보완 회신 → ④⑤ → 검토 카드·초안 화면까지 확인해야 한다. 미등록 웹 업체 검색은 별도 보류 경로로 검사한다. 등록 후보가 충분한 정상 흐름에서 크롤링을 억지로 실행하지 않는다.
