# 백엔드 연동 마무리 작업

브랜치: `feature/backend-integration-completion`. 실제 Supabase를 변경하지 말라는 사용자 지시에 따라 **SQL은 로컬 초안으로만 제공하며 적용하지 않는다.** 해당 지시 이후 실제 Supabase 조회도 중단했다. 외부 HTTPS 주소도 게시하지 않았다.

## 바로잡은 원인

실행 중인 Docker는 새 Google 연결 API가 없는 이전 버전이었다. 로컬 `.env`에는 키가 있었지만 Supabase 주소가 `YOUR_PROJECT.supabase.co`인 상태도 확인했다. 알려준 공개 프로젝트 URL로 `.env`의 URL만 수정했으며 실제 키·암호화 키는 변경하거나 출력하지 않았다. `.env`는 Git 제외다.

Supabase에서 읽기 조회로 확인한 실제 테이블은 새 13개 업무 테이블이었다. 연결용 RPC는 없었다. 기존 구 `work_items` 계약을 그대로 재사용하지 않고 관측한 UUID·text mailbox_ref·새 mail_messages/work_cases/mail_sync_states 필드에 맞춘 로컬 SQL을 준비했다. 기존 DB의 모든 CHECK/trigger/RLS까지 복제 검증한 것은 아니므로 실제 적용 전 DB 담당자 검토가 필요하다.

## 코드 변경

- `app/features/mail/application/classification.py`: 팀 분류 브랜치의 `14d8d8b` 버전 단일 모듈을 통합했다. 다른 에이전트/실험 파일을 통째로 병합하지 않았다.
- 신규 워커와 분류 API가 같은 `classify_or_raise()`를 호출한다. 광고 제목·명확한 업무 키워드 규칙 후 혼합/불명확 건은 팀 INSTRUCTION으로 LLM을 부른다. 회신 근거가 확실한 기존 업무는 분류기를 우회한다. 기술 오류는 신규 워커에서 실패로 남고 비업무/불확실로 바뀌지 않는다.
- 분류 전용 모델은 `LLM_CLASSIFICATION_MODEL`만 설정해도 사용할 수 있다. 다른 분석 역할의 모델 요구사항은 유지한다.
- Pub/Sub 설정 네 가지가 모두 있을 때만 watch를 요청한다. 부족하면 연결은 유지하고 90초 폴링 경로를 사용한다. 워커를 잠시 꺼도 연결 메일함의 인증된 push는 신규 지속 큐로 들어가며 구 계정 워커 경로로 잘못 들어가지 않는다.
- 연결 모드에서는 구 고정 계정 워커를 꺼야 한다. 계정 연결과 메일 수집의 readiness를 구분한다. 수신 워커 기본 false는 유지했다.
- DB 함수가 없을 때 `MAILBOX_SCHEMA_REQUIRED`를 반환해 일반 503과 구분한다.
- `/health`에 `api_contract`·`build_revision`을 추가했다. `/ready`는 설정/DB/워커 범주를 구분하며 키·내부 예외를 노출하지 않는다. 상세 누락 필드명은 로컬 진단 CLI에서만 출력한다.

## DB 담당자용 로컬 초안

[migrations/001_google_mailbox.sql](../migrations/001_google_mailbox.sql)은 비공개 테이블 6개, public RPC 16개와 고유 인덱스를 제공한다. 기존 업무 테이블은 재사용하며 파괴적 초기화·데이터 삭제·자동 migration은 없다. API Docker 이미지에도 migration을 실행하는 로직은 없다.

- 사용자별 연결 변경·멱등키 선점을 원자적으로 처리하고 첫 응답을 보존한다.
- 연결·암호문·수신 job 저장은 한 트랜잭션이다. 저장 불명확 요청을 자동 재실행하지 않는다.
- 연결 revision·claim token·lease·중복 메시지·재시작·backoff·동기화 도중 들어온 후속 push를 처리한다.
- 신규 업무 메일만 UUID Waiting을 만든다. 비업무·불확실·모호 회신을 구분하고 기술 실패는 롤백한다.
- 기존 회신은 소유/참조/실제 참여자를 다시 검사한다. 보관·수긍 건은 자동 재검토하지 않는다.
- 활성 회신은 `gyeonjwo_private.google_review_events`에 후속 추출/재검토 이벤트만 접수한다. 이 큐의 실제 다섯 에이전트 소비자는 이번 범위에서 구현하지 않았다.
- 비밀 정보는 비공개 스키마 ciphertext로만 보관한다. anon/authenticated의 직접 접근과 RPC 실행은 차단하며 서버 service_role 전용이다.
- private push ledger를 사용한다. 기존 public mail_ingestion_events와 합치려면 DB 담당자가 RPC 계약을 유지하면서 내부 매핑을 조정할 수 있다.

**파일이 생겼다는 것이 실제 Supabase에 함수가 설치됐다는 뜻은 아니다. 적용은 사용자가 금지했으므로 하지 않았다.**

## 로컬 실행 진단

외부 서비스에 접속하지 않고 설정 이름/형식만 확인:

```powershell
.\.venv\Scripts\python.exe -m scripts.check_integration --local-only
```

DB 담당자가 SQL을 적용한 뒤, 사용자가 읽기 점검을 원할 때만:

```powershell
.\.venv\Scripts\python.exe -m scripts.check_integration --db
```

이 명령의 DB 점검은 상태 확인 RPC만 호출하며 SQL을 설치하거나 데이터를 변경하지 않는다. `/ready`도 서버의 DB 상태 확인 RPC를 호출하므로 이번 마지막 로컬 HTTP 점검에서는 사용하지 않았다.

## 팀원용 HTTPS 구성

[compose.https.yaml](../compose.https.yaml)과 [시작 스크립트](../scripts/start_team_server.ps1)를 준비했다. 실제 공개 URL은 아직 없다.

Supabase에 접속하지 않고 서버의 경로·CORS·빌드만 확인하려면:

```powershell
.\scripts\start_team_server.ps1 -Offline
```

이 모드는 수신 워커를 끄고 DB 상태 확인과 `/ready` 호출을 생략하며 외부 공개를 허용하지 않는다. 실행 프로세스에서만 플래그를 바꾸고 `.env`는 수정하지 않는다. 실제 Google 계정 연결이나 DB 준비 완료를 의미하지 않는다.

DB 준비 후 로컬 서버를 최신 코드로 재빌드·재생성하고 build_revision·API 경로·CORS를 검사:

```powershell
.\scripts\start_team_server.ps1
```

외부 공개를 명시적으로 선택할 때만:

```powershell
.\scripts\start_team_server.ps1 -Publish
```

`-Publish`는 공식 cloudflared 컨테이너를 실행해 개발용 trycloudflare HTTPS 주소를 게시한다. 스크립트는 DB 준비·최신 build_revision·CORS 확인에 실패하면 터널을 시작하지 않는다. 인쇄된 HTTPS 주소를 프론트의 API_BASE_URL로 사용한다. CORS에는 백엔드 URL이 아니라 프론트 브라우저 origin을 넣는다.

API 포트는 호스트 loopback에 유지한다. 터널은 API의 network namespace에서 loopback으로 접속하고, Uvicorn은 127.0.0.1의 proxy header만 신뢰한다. `forwarded-allow-ips=*`나 공개 HTTP 포트는 사용하지 않는다. 개발용 터널 URL은 재시작 시 달라질 수 있어 상시 운영 배포로 취급하지 않는다.

이 실행 환경은 Docker 엔진 named pipe에 접근할 수 없어 실제 이미지 재빌드·터널 컨테이너 실행은 수행하지 못했다. Compose 문법과 신뢰 프록시 검사는 로컬에서 검증했다. 사용자가 자신의 PowerShell에서 실행할 수 있게 스크립트를 제공한다.

## 검증의 범위

- 최종 `pytest`: **302 passed**, 기존 Starlette/httpx 사용 중단 예정 경고 1건. 의존성 충돌 없음. Python 컴파일·OpenAPI·Compose·PowerShell 구문·git diff 공백 검사 완료.
- Python 모의·회귀 테스트와 실 PostgreSQL/PGlite 연결 통합 테스트.
- 별도 SQL 검증 58개: 첫 응답 재생·선점·잘못된 사용자·scope·메일/커서 원자성·신규 업무/보관 회신·오래된 lease·재적용·RPC 권한 차단.
- Python API → 실제 격리 SQL → 암호문 저장 → 조회/재시도 → 메일 수신 → UUID Waiting 생성 → 중복 방지까지 연결했다. Google만 모의 공급자이며 실제 Google 계정 연동을 뜻하지 않는다.
- 새 코드를 임시 localhost:8001에서 실행해 `/health 200`, 신규 API 경로, CORS OPTIONS 200, 무인증 요청 401을 확인했다. 수신 워커를 강제로 끈 임시 프로세스였으며 종료했다. 기존 Docker :8000은 교체하지 않았다.
- 실제 팀 Supabase에는 SQL·데이터 변경 없음. DB 금지 지시 후 외부 Supabase/Google 호출 없음. 실제 OAuth 동의·토큰 전송·메일 발송·HTTPS 게시 없음.
- 재개 후 `-Offline` 실행의 워커 강제 중지·기존 환경 변수 복원을 실제 PowerShell에서 검증했다. Docker 명령은 mock으로 대체했으며 설정 검사도 local-only로 실행해 실제 DB 접속은 없었다. 실제 이미지 빌드는 수행하지 않았다.

사용자 요청으로 tests/ 전체, pytest.ini, requirements-dev.txt와 SQL 테스트 런타임 설치 스크립트를 제거했다. 위 검증 수치는 제거 전 실행 기록이다. scripts/check_integration.py는 실제 서버 설정·연결 진단 도구이므로 유지한다.

## 실제 연동의 남은 경계

사용자 지시대로 실 DB는 건드리지 않는다. DB 담당자 SQL 적용/검토, 실제 Docker 재빌드, 동의한 HTTPS 공개 및 동일 OAuth 클라이언트로 프론트 합동 연결 테스트가 끝나야 실제 계정 연결 완료라고 판단한다. 자동 수신은 분류 모델 설정과 워커 활성화 후 별도로 검증한다. 새 업무 목록 API와 후속 다섯 에이전트 전체 구현은 이 연결 작업의 완료와 구분한다.

참고: [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/), [PGlite API](https://pglite.dev/docs/api).

## 2026-10-29 실행 확인

사용자가 PowerShell에서 -Offline 재빌드를 완료했다. 이미지 빌드·컨테이너 재생성 후 build_revision=1cf6a80-dirty, Google 경로·CORS 검사가 통과했다. 이후 -Publish 실행은 DB preflight migration_required/404로 중단되어 HTTPS 주소를 게시하지 않았다. 실제 Supabase 적용과 프론트 계정 연결 검증은 남아 있다.
