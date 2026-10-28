# 서버 기반 코드 팀원 인계

2026-10-28. 로컬 `feature/mail-api-server` 브랜치. **현재 GitHub 업로드 전**이며 이 파일이 있다는 사실이 원격 브랜치 공개를 뜻하지 않는다.

## 준비된 것

- Dockerfile·Compose·환경변수 예시·PowerShell 실행 스크립트.
- FastAPI 서버와 Swagger, 생존/준비 상태 구분.
- 인증된 Pub/Sub 알림 영속 접수와 Gmail 원문 조회·구 DB 저장 워커 기반.
- 인증된 메일 분류 LLM 호출 API. 임의 system prompt·모델·키를 요청 본문에서 받지 않는다.
- 구 DB 계약의 원문/업무 조회·분석/계산·비고·첨부 API와 회귀 테스트.
- 기능별 API/application/domain/infrastructure 폴더와 환경설정/외부 연동 분리.

## 팀원이 먼저 볼 파일

| 목적 | 경로 |
|---|---|
| 서버 시작·라우터 등록 | `app/main.py` |
| 메일 수신 HTTP 응답 | `app/features/mail/api/router.py` |
| 원문 조회·등록·복구 워커 | `app/features/mail/application/worker.py` |
| Gmail/OAuth/푸시 검증 | `app/features/mail/infrastructure/gmail.py` |
| LLM 제공자 호출 | `app/shared/infrastructure/llm.py` |
| 구 업무 서비스·분석 | `app/features/review/application/` |
| 계산·원문/단위 검증 | `app/features/review/domain/` |
| Supabase 계약·파일 | `app/shared/infrastructure/db/` |
| 설정 | `app/core/config.py`, `.env.example` |

서버 기능과 실제 준비 여부는 [실행 안내](MAIL_API_SERVER.md), 전체 폴더는 [레이어 안내](FOLDER_STRUCTURE.md)를 따른다.

## API 사용 구분

- `/api/v1/webhooks/gmail`: Google Pub/Sub 호출용. 실제 메일이 아니라 Gmail 변경 알림을 받는다. OIDC 검증 후 DB에 접수하고204, 이후 워커가 원문·첨부를 가져온다. Google에서 접근 가능한 HTTPS 주소가 필요하다.
- `/api/v1/llm/classify-mail`: Supabase 사용자 인증 후 제목/본문을 LLM으로 분류한다. 결과는 BUSINESS/NON_BUSINESS/UNCERTAIN이며 **이 API 자체는 DB에 저장하거나 메일을 발송하지 않는다**.
- 기존 `/api/v1/work-items/...`: 구 스키마 기반 상세/분석/파일/비고 API다. 최신 노션의 새 업무 그룹 API 전체를 대신하지 않는다.

## 검증된 상태

사용자 터미널의 Docker build·컨테이너 healthy 출력을 확인했고 실제8000번 서버의 /health·/docs·OpenAPI가200을 반환했다. 최신 전체 자동 테스트175개·컴파일·의존성 검사 통과. /ready는503이며 실제 외부 서비스 연동 미완료를 정확히 표시한다.

## 에이전트 담당자와 연결 전에 정할 것

1. **DB 담당자:** 최신06 논리 설계의 물리 테이블/RPC·소유/RLS·UUID·revision·마이그레이션을 확정한다. 현재 코드는 구06 v2.2 계약이라 새 DB에 바로 연결하면 안 된다.
2. **API/에이전트 담당자:** 동일 FastAPI 프로세스의 application 서비스/도구를 호출할지, 별도 에이전트 프로세스에서 인증된 HTTP API를 사용할지 정한다. 임의 SQL·서버키를 에이전트에 직접 넘기지 않는다.
3. **팀 문서:** 최신08 HTTP 계약과09 화면을 추가 대조해 조회·결과 저장·상태·권한·멱등성·행동 코드를 맞춘다. 현재 LLM 어댑터는 구조화 응답 호출이며 다섯 역할의 도구 선택 조정기 완성본이 아니다.
4. **접속 설정:** 승인된 Supabase/Gmail/LLM 계정·모델·키는 각 서버의 .env/비밀정보 설정에 넣는다. Git에 실제 .env·키를 올리지 않는다.
5. **실수신 인수:** HTTPS·Pub/Sub/OIDC·OAuth·watch·DB 접수·원문 저장·분류를 실제 계정으로 확인한다. WORKER_ENABLED는 이 설정을 마친 뒤 켠다.

기획에서 아직 필요한 기능은 [기획 대비 구현 차이](IMPLEMENTATION_GAP_2026-10-28.md), 완료 판정은 [AC-01~23](ACCEPTANCE_BASELINE_2026-10-28.md)을 따른다. 서버 healthy·단위 테스트 통과를 전체 MVP 완성으로 해석하지 않는다.
