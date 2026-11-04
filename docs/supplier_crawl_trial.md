# KAN-98 크롤링 시험 — 신규 거래처 웹 탐색 (MVP2 / Should)

기준 문서: 03 MVP 정의서 C-01, 06 CRAWLED_SUPPLIERS, 07 `crawl_supplier_candidates`.
현재 수집기는 **역할에 노출하지 않는 수집 도구**다. KAN-109 v3 등록 서비스는 별도 보존하며, ② 연결·자동 활성화는 이번 범위가 아니다.

## 1. 수집 흐름과 출처

```
① 업체 발견   품목제조보고(I1250) — 품목명 검색 → 업소·인허가번호·제품명·품목유형
              식품제조가공업(I1220) — 인허가번호 → 소재지
② 사이트 찾기 네이버 웹문서 검색(NAVER API HUB) — '업체명 + 시군구' → 업체 사이트 후보
③ 사이트 확인 첫 화면에 업체명이 있는 사이트만 채택
④ 연락처      연락처·회사소개 페이지 → 업무 메일·전화 (업체 도메인 메일 여부 표시)
⑤ 인증 대조   HACCP 업체 정보(공공데이터포털) — 인허가번호로 인증번호·품목유형·기간
```

| 출처 | 얻는 값 | 성격 |
| --- | --- | --- |
| 식품안전나라 Open API — 품목제조보고(I1250) | 업소·인허가번호·제품명·품목유형·보고일·생산 종료 여부 | 취급 품목의 공식 근거 |
| 식품안전나라 Open API — 식품제조가공업(I1220) | 소재지(`LOCP_ADDR`). 전화(`TELNO`)는 대부분 비어 있음 | 업체 소재지 |
| 공공데이터포털 — 식품 제조가공 HACCP 업체 정보 | 인증번호·품목유형·인증 기간 | 인증 표시(잠정) |
| NAVER API HUB 웹문서 검색 | 업체 사이트 후보 주소 | 사이트 찾기 |
| 업체 홈페이지 | 업무 메일·전화, 품목 언급 문장 | RFQ 연락처 |

`--naver-query`를 주면 네이버 지역 검색도 쓸 수 있지만, 농장·판매처 위주라 제조업체 발견에는 맞지 않았다(11/2 시험).
`--seeds`로 직접 만든 주소 목록(한 줄에 `주소,업체명`)도 쓸 수 있다.

## 2. 수집 항목 (06 기준)

| 항목 | 필드 | 근거 |
| --- | --- | --- |
| 업체명·인허가번호·소재지 | `name`, `license_no`, `location` | 출처별 `CrawlSourceRef` |
| 연락처 | `contacts[]` — PHONE·EMAIL·HOMEPAGE | 값마다 출처 URL·조회 시각, 메일은 '업체 도메인 메일' / '외부 메일' 구분 |
| 취급 품목 | `handled_items[]` — 품목명·유형 | 품목제조보고번호 또는 홈페이지 문장 |
| 인증 | `certifications[]` — HACCP 번호·유형·기간 | HACCP 레코드 |
| 상태 | `checklist_status=UNCONFIRMED`, `verified_supplier_id=null` | 확인 전 잠정 후보 |

## 3. 수집 원칙

- 업체 단위 정보(업체명·업체 연락처·소재지·품목·인증)만 수집한다. 대표자 등 개인 이름 필드는 읽지 않는다.
- 모든 값은 출처 URL과 조회 시각을 함께 남기고, 출처가 다른 값은 덮어쓰지 않고 나란히 둔다.
- 웹·공공데이터 값은 잠정 근거로 다룬다. 현재 공급 가능 여부·가격·인증 적합성은 RFQ 회신과 체크리스트로 확인한다.
- 검색 결과 페이지는 크롤링하지 않고 공식 검색 API로만 조회한다. 크롤링 대상은 업체 사이트뿐이다.
- 업체 사이트는 robots.txt가 허용한 경로만, 같은 사이트 안에서, 사이트당 최대 4쪽을 1초 이상 간격으로 읽고 수집기 이름을 User-Agent로 밝힌다.
- 업체 사이트로 인정하는 곳은 정부·공공기관·채용·기업정보·쇼핑·플랫폼(유튜브·스마트스토어 등)이 아니고, 사이트 첫 화면에 업체명이 있는 곳이다.
- 공개 인터넷 주소만 조회한다(로컬·사설 주소 제외). 응답은 2MB, 10초(HACCP 30초) 안에서만 읽는다.
- 인증키는 `.env`에만 두고, 리포트·로그에는 마스킹한 주소만 남긴다.

## 4. 실행 방법 (PowerShell)

1. 키 발급 후 `.env`에 입력 (`.env.example` 하단 참고)
   - `FOOD_SAFETY_API_KEY`: 식품안전나라 공공데이터활용 인증키 (I1250·I1220 이용신청)
   - `DATA_GO_KR_SERVICE_KEY`: 공공데이터포털 'HACCP 업체 정보' 활용신청 후 일반 인증키 중 **디코딩 키**
   - `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: 네이버 클라우드 NAVER API HUB Application(웹문서·지역 선택)의 인증 정보
2. 실행

```powershell
# 업체 발견 → 사이트 찾기 → 연락처 (확보율 측정)
.\.venv\Scripts\python.exe -m scripts.crawl_trial --keyword 동결건조 --sources public,homepage --business-limit 30 --site-limit 30
# 품목유형으로 좁히기, HACCP 대조 포함
.\.venv\Scripts\python.exe -m scripts.crawl_trial --keyword 딸기 --sources public,haccp,homepage --item-types 과·채가공품 --haccp-pages 50
```

키가 없는 출처는 건너뛰고 리포트에 이유를 남긴다. 결과는 `logs/crawl_trial/`에 JSON(전체)·MD(공유용)로 저장되며 Git에 올라가지 않는다.

## 5. 시험에서 확인한 공식 명세와 실제 응답의 차이 (코드에 반영)

| 대상 | 명세 | 실제 |
| --- | --- | --- |
| I1220 소재지 | `ADDR` | `LOCP_ADDR` |
| HACCP 응답 형식 인자 | `retunType` | `returnType`에만 JSON으로 응답 (둘 다 보냄) |
| HACCP 한 쪽 결과 수 | 제한 표기 없음 | 1000건은 결과 코드 13으로 거절 → 100건 |
| HACCP 성공 코드 | `00` / `0000` | `OK` |
| HACCP 목록 구조 | `items.item[]` | `items[].item` |
| 공공 API 압축 응답 | — | 해제 오류 → 압축 없이 요청 |

## 6. 측정 결과 (11/2, 검색어 '동결건조', 업체 22곳)

| 항목 | 결과 |
| --- | --- |
| 업체명·인허가번호·소재지·취급 품목 | 22/22 |
| 업체 사이트 확인 | 2/22 |
| 업체 도메인 업무 메일 | 1/22 |

검색 결과에 업체명만 있으면 채택하던 1차 방식은 군청 공지·뉴스·기업 목록 페이지를 업체 사이트로 잘못 잡았다. 사이트 첫 화면 확인으로 걸러 냈고, 이미지 위주 사이트는 누락될 수 있다. 처리 기준(A·B·C)은 KAN-109에서 결정한다.

## 7. 2026-11-04 구조 변경 — 미등록 수집 도구

위 11/2 측정은 과거 실제 수집 결과이며 이번 오프라인 결과와 합산하지 않는다.
현재 구조는 `app.agents.tools.crawl_supplier_candidates(options: TrialOptions)`의 `@tool` 정의다.
`@register`를 붙이지 않고 ②를 포함한 모든 `ROLE_TOOLS`에서 제외한다.
`KNOWN_TOOLS`에도 없으므로 수동 `register()` 시도는 기존 검증으로 거부된다.
등록 검증·Runner·그래프·역할별 호출 조건은 변경하지 않았다.

### 이동 대응

기존 경로의 접두사는 `app/features/supplier_crawl/`다.

| 기존 구현 | 현재 위치/보존 범위 |
| --- | --- |
| `domain/names.py` | `app/agents/tools.py`: 업체명·품목유형·지역 정규화 |
| `domain/schemas.py` | 공통 수집 모델은 tools의 `CrawlSourceRef`, `CrawlContact`, `CrawlHandledItem`, `CrawlCertification`, `CrawlCandidate`, `CrawlReport`. 기존 파일에는 KAN-109 등록용 report_no·인허가/필드 근거 확장만 유지 |
| `infrastructure/config.py` | tools의 `CrawlSettings`. import만으로 설정 로딩/활성화하지 않음 |
| `infrastructure/network.py`, `http.py` | tools의 `PublicHTTPTransport`, `Fetcher` 및 DNS/IP 고정·리다이렉트·크기·마스킹 검사 |
| `infrastructure/public_data.py`, `haccp.py`, `homepage.py` | tools의 `PublicDataClient`, `HaccpClient`, `HomepageCrawler` |
| `application/trial.py` | tools의 `TrialOptions`, `SupplierCrawlTrial`, `fill_rates`. 기존 파일에는 공통 수집기를 상속하는 KAN-109 근거 확장만 유지 |
| `application/report.py` | tools의 `to_markdown` |
| `scripts/crawl_trial.py` | 실행 명령·옵션·JSON/MD 저장은 유지하고 위 공통 도구 모듈에서 import |
| `application/tool.py` | `install_crawl_tool`과 중복 도구 정의 제거. KAN 등록용 `bind_crawl_service`·일반 함수 `run_crawl_registration(context)`만 비활성 보존 |

이동한 구현 8파일은 제거했고, KAN-109가 사용하는 패키지 초기화 파일은 남겼다.
컬렉터의 출처 관찰/모델 생성 확장 지점으로 KAN 근거만 보강하므로 별도 수집·보안 계산을 복제하지 않는다.
공통 도구는 기존 PR #33 수집 형식만 반환하고 DB RPC·RFQ·초안·메일을 호출하지 않는다.
새 typed 도구 인자는 CLI와 같은 출처 및 수량 상한을 검증한다.
CLI의 기본값·필터·재시도·HACCP 한도·robots·동일 사이트 범위·DNS/IP 고정·비밀 마스킹을 유지한다.

### KAN-109와의 분리

미커밋 v3 등록 서비스·payload·응답 검증은 `app/features/supplier_crawl/`에 보존한다.
DB 소유 표준 키 생략/반환 검증, ITEM_KEY_MISMATCH 전체 실패, 불확실 결과의 자동 재시도 금지,
CONTACT_REQUIRED 및 LINKED_REGISTERED의 후속 매핑 상태, blocker의 평가 ID 제외를 유지한다.
등록용 report_no·인허가번호·필드 근거는 공통 수집 결과에 임의로 합치지 않는다.
홈페이지에서 실제 보고번호를 얻지 못한 품목은 등록용 모델에서도 report_no=None 그대로다.

`install_crawl_tool`로 Runner 전에 등록하거나 에이전트 실행 주위에 자동 바인딩한다는 이전 계획은 폐기한다.
보존한 KAN 서비스는 명시적으로 조립·바인딩한 일반 함수로만 호출 가능하다.
현재 ② 또는 서버에 연결/활성화하지 않으며, 호출 조건·서버 조립·REGISTERED 품목 매핑은 별도 합의/구현 범위다.
PR #39의 UNKNOWN/provisional 소비자 변경 및 다른 담당자의 서버 연결을 이 이동에 포함하지 않는다.

### 재현 및 이번 검사

저장소 requirements.txt의 의존성이 설치된 Python 환경에서 실행한다.
이번에는 기존 의존성 환경을 사용했고 새 설치·실제 DB·API·모델·메일 호출은 하지 않았다.

```powershell
python -B -X utf8 -m unittest discover -s tests -p test_kan109_v3.py -v
python -B -X utf8 -m unittest discover -s tests -p test_supplier_crawl_tool.py -v
```

- 이전 구조의 부모 3건·자식 45건 통과 기록과 diff/로그는 별도 보존본에 그대로 남겼다.
- 이번 최종 제품 소스의 KAN v3: 부모 격리 3건 통과, 독립 자식 회귀 45건 통과.
- 이동 영향의 최종 검사: 부모 격리 2건 통과, 독립 자식 회귀 29건 통과.
- 부모에는 자식 실행 결과 확인이 포함되므로 부모·자식 수를 제품 시나리오 수로 합산하지 않는다.
- 각 자식의 네트워크·키 파일 접근 시도 0. 최종 검사 실패/오류/skip 0.
- 초기 새 홈페이지 fixture가 text/plain을 선언해 1건 실패한 기록은 보존했다.
  HTML MIME type을 바로잡고 영향받은 이동 검사만 재실행했다. 제품의 MIME 보안 검사는 완화하지 않았다.
- 기존 격리 테스트 파일과 v3 RPC 파일은 바이트 동일. 기존 공통 함수/클래스 118개와
  이동한 수집/보안 선언 82개는 이름 변경을 정규화한 AST 대조로 보존을 확인했다.
- 과거 85개·8개·PR #33 보안 59개 결과를 이번 결과로 재사용하지 않는다.

## 8. PR 기준 정렬 — feature/agents@90ccca5

`app/agents/README.md`의 “PR은 feature/agents로 올린다” 규칙을 적용한다.
이번 수집 도구의 @tool/미등록/전 역할 제외는 명시적으로 합의한 예외이며 register 검증은 바꾸지 않는다.
별도의 dev 대상 합의는 없으므로 이전 Draft의 dev_1.0 제안을 feature/agents로 정정한다.

- 최신 확인: feature/agents=`90ccca5a5aba290baa2b9aa8c0c1150c37538d7b`,
  dev_1.0=`9eed329ff21120fa458700d8bff9c1e451a218d2`.
- feature/kan-109-v3를 137555b에서 위 feature/agents로 fast-forward했다. 새 커밋은 만들지 않았다.
  이동 패치를 따로 보존/적용 검사하고, 해당 패치를 되돌린 후 fast-forward하고 같은 이동 패치만 재적용했다.
  전체 tools.py를 다른 브랜치 파일로 교체하지 않았다.
- 팀의 PR #39 UNKNOWN/provisional 및 PR #40 KEEP 누락 검사가 현재 기준에 포함된다.
  대상의 tools 함수/클래스 119개를 AST로 대조했고 agents.py/nodes.py/README.md는 대상과 동일하다.
  수집 이동 구역·KAN v3·기존 격리 테스트·CLI는 정렬 전과 동일하다.
- PR #33 수집 파일은 dev에는 있지만 현재 feature/agents에는 없다.
  따라서 이 대상 대비 실제 변경에는 이전 이동 단계의 8파일 삭제가 나타나지 않는다.
  새 공통 수집 도구/CLI/문서/이동 검사 4파일과 KAN v3 제품 7파일·초기화 4파일·검사 1파일,
  총 16파일이 현재 미커밋 PR 준비 범위다. agents.py/nodes.py/서버 파일은 이 diff에 포함하지 않는다.
- PR #39는 이제 원격 반영 대기가 아니다. wrapper의 두 인증 blocker 소비자 조정은 별도 작업으로 남긴다.
  공통 UNKNOWN/provisional을 재구현하거나 기존 차단을 이번 기준 정렬에서 해제하지 않았다.
- install_crawl_tool 계획은 폐기 상태를 유지한다. 호출 조건·명시적 서버 조립·REGISTERED 품목 매핑은 미연결이다.
  최신 dev의 별도 서버 변경을 feature/agents 기준에 임의로 병합하지 않는다.

기준 정렬 영향 검사만 실행했다. 저장소의 두 관련 unittest 모듈을 같은 독립 프로세스 방식으로 실행하여
부모 격리 5건(기존 3 + 이동 2), 자식 v3 45건, 자식 이동 29건이 각각 통과했다.
실패/오류/skip 0, 자식의 네트워크/키 파일 접근 시도 0. 이전 결과와 합산하지 않는다.
이번에는 전체 에이전트·서버 검사나 실제 서비스 통합 검사를 실행하지 않았다.
제품 코드는 이 실행 후 변경하지 않았다. 기존 검증 기록은 7절의 당시 결과로 보존한다.
