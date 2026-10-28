# 메일 수신·LLM 연결용 API 서버

2026-10-28. 작업 브랜치: `feature/mail-api-server`. 현재 요청은 Docker로 실행 가능한 백엔드 서버와 메일 수신/LLM 호출 연결 기반이다. 다섯 에이전트의 전체 실행 조정기·새 DB 계약은 아직 구현하지 않았다. 최신 요구사항 차이는 [프로젝트 차이 분석](IMPLEMENTATION_GAP_2026-10-28.md)을 따른다.

## 이번 구현

- Dockerfile: Python 3.12 slim·잠긴 requirements·비root 사용자·단일 Uvicorn 프로세스·/health 검사.
- compose.yaml: localhost 포트, .env 런타임 주입, read-only root·/tmp·종료 유예·프로세스/메모리 제한·restart.
- .dockerignore: 필요한 소스만 build context 허용. .env·키·.git·가상환경·문서·테스트는 이미지에 복사하지 않는다.
- `scripts/start_server.ps1`: Docker 또는 로컬 Python 실행. 기존 .env를 덮어쓰지 않는다.
- 기존 `POST /api/v1/webhooks/gmail`: Google Pub/Sub 인증·입력 검증 후 DB에 알림을 영속 접수하고 204 응답. 워커가 Gmail 원문을 조회·분류한다. 메일 발신자에게 보내는 답장이 아니다.
- 추가 `POST /api/v1/llm/classify-mail`: Supabase 사용자 인증 후 제한된 메일 제목/본문으로 실제 LLM 어댑터를 호출한다. 프롬프트·모델은 서버 설정이며 클라이언트 임의 system prompt를 받지 않는다. 업무 생성·DB 저장·외부 메일 발송은 하지 않는다.
- `GET /health`: 프로세스 생존. `GET /ready`: 기존 DB·설정·수신 워커 준비 검사. health 성공은 실연동 완료가 아니다.

## Docker 실행

Docker Desktop과 실행 중인 Docker engine이 필요하다. Docker CLI 29.9.2·Compose v5.6.1 설치와 Compose 설정 검사를 확인했다. 사용자가 로컬 터미널에서 이미지 build·컨테이너 시작을 완료하고 `docker compose ps`의 **Up 50 seconds (healthy)** 결과를 제공했다. 이어서 실행 중인 127.0.0.1:8000의 /health·/docs·/openapi.json HTTP200을 직접 확인했다.

자동화 환경에서는 Windows Docker 엔진 파이프 접근이 제한되므로 build/inspect를 직접 수행했다고 기록하지 않는다. Docker 실행 증거는 사용자 제공 Compose 출력과 직접 확인한 HTTP 응답이다. 이전 WSL 설치 장애는 해소됐다.

```powershell
cd 'C:\Users\user\Documents\Codex\gyeonjwo_backend'
.\scripts\start_server.ps1 -Mode docker
```

또는 `.env` 준비 후:

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 api
```

종료: `docker compose down`. 코드 수정 후에는 `docker compose up -d --build`로 다시 build한다. .env만 바꿔도 컨테이너 재생성이 필요하다. secret 값은 이미지 build 인자로 넣지 않는다. Compose 버전/환경 호환성은 실제 Docker에서 확인해야 한다.

## Docker 없는 로컬 실행

```powershell
.\scripts\start_server.ps1 -Mode local -Port 8000
```

- 상태: http://127.0.0.1:8000/health
- API 문서: http://127.0.0.1:8000/docs
- 종료: 실행한 터미널에서 Ctrl+C.

이번 로컬 확인은 WORKER_ENABLED=false로 수신 작업을 정지하고 실행했다. 실제 DB·Gmail·LLM 설정이 준비되면 명시적으로 워커를 활성화한다. 1프로세스/1수신기 제한을 유지한다. 컨테이너 안에서는 0.0.0.0:8000에 bind하지만 호스트 publish는 기본127.0.0.1이다.

## LLM 호출 API

필요 설정: `LLM_BASE_URL`(승인된 Chat Completions 호환 /v1 주소), `LLM_API_KEY`, `LLM_MODEL`, 필요 시 `LLM_CLASSIFICATION_MODEL`. 모델을 임의 선택하지 않았다. 사용자는 Supabase Auth access token과 app 접근 권한이 필요하다. 키/토큰을 Git·채팅·스크린샷에 넣지 않는다.

Swagger /docs의 Authorize에 사용자 access token을 입력하고 `POST /api/v1/llm/classify-mail`을 실행한다.

```json
{
  "subject": "딸기 단가 인상 요청",
  "body": "다음 달부터 단가를 변경하고자 합니다. 검토 부탁드립니다."
}
```

성공 예시(형식 설명용, 실모델 결과 아님):

```json
{
  "classification": "BUSINESS",
  "reason": "단가 변경 검토를 요청하는 구매 관련 메일입니다.",
  "persisted": false
}
```

분류는 BUSINESS/NON_BUSINESS/UNCERTAIN. 미인증401, 잘못된/초과입력422(본문 바이트 한도 초과413), LLM설정누락503, 제공자실패502다. 본문 최대12,000자·제목500자·HTTP본문64KB 한도가 함께 적용된다. 실제 LLM 호출은 비용을 발생시킬 수 있으며 이 엔드포인트는 인증된 명시 실행용이다.

## 실제 메일 알림 연결

Gmail → Cloud Pub/Sub → 공개 HTTPS `POST /api/v1/webhooks/gmail` → 영속 알림 접수204 → 단일 워커의 원문 조회·후속 분류 순서다. Gmail 푸시는 원문 메일 자체가 아니므로 알림 본문을 바로 견적 원문으로 LLM에 보내지 않는다. 웹훅 요청 안에서 장시간 분석을 기다리지 않는다.

외부 Google 서버는 이 PC의 localhost에 접근할 수 없다. 팀 서버의 HTTPS 도메인·Pub/Sub 구독 endpoint/audience·OIDC 호출 계정·Gmail OAuth/watch·Supabase 구 계약 설정이 필요하다. 이번에는 공개 배포·터널·OAuth/IAM 변경·실제 메일 전송을 하지 않았다. 새 Notion의 그룹/견적/다섯 역할/발송 계약은 별도 구현해야 하므로 구 워커를 최신 MVP 완성이라고 소개하지 않는다.

## 최종 검증·현재 제한 — 2026-10-28

| 확인 | 결과 |
|---|---|
| 사용자 터미널 이미지 build | `gyeonjwo-backend:local Built` |
| 사용자 터미널 컨테이너 상태 | `gyeonjwo-backend-api-1 Up (healthy)` |
| 실제 서버 `/health` | HTTP200, status=ok, schema_contract=notion-06-v2.2 |
| 실제 서버 `/docs` | HTTP200 |
| 실제 서버 `/openapi.json` | HTTP200, 경로13개, mail/classify-mail 포함 |
| 실제 서버 `/ready` | HTTP503·not_ready, 외부 연동 준비 전의 정상적인 차단 |
| 최신 코드 전체 자동 테스트 | **175개 통과**, 4.14초 |
| Python 컴파일·모듈 import·설정 루트 | 통과 |
| pip check | 의존성 충돌 없음 |
| Git 업로드 | 커밋·푸시·PR·main 병합 수행하지 않음 |

테스트는 외부 Supabase·Gmail·LLM 응답을 대체한다. 실제 계정·권한·모델 응답 품질·실수신 검증이 아니다. Starlette/httpx deprecation 경고1개가 있으며 현재 실패는 아니다. Docker healthcheck는 /health 생존 확인이므로 healthy가 /ready나 실연동 완료를 뜻하지 않는다.

기능별 레이어 이동 후 API/import/monkeypatch·프로젝트 루트 .env·PDF 하위 프로세스 경로를 맞췄다. API 문서는 system/mail/review 태그로 구분한다. 인증 서비스 장애는 AUTH_UNAVAILABLE로 구분하고, 공백뿐인 메일 입력을 차단하며, 서버 종료 시 finally에서 워커를 정리한다. [폴더 안내](FOLDER_STRUCTURE.md), [팀원 인계](TEAM_HANDOFF.md)를 참고한다.

서버 기반 확인은 마쳤지만 최신 Notion의 업무 그룹·새 견적/카드 저장·승인/발송·다섯 에이전트는 아직 별도 구현 대상이다. 구 SQL/RPC와 최신 논리 스키마를 혼동하지 않는다. [기획 대비 차이](IMPLEMENTATION_GAP_2026-10-28.md)를 따른다.

구현 참고: [FastAPI Docker 안내](https://fastapi.tiangolo.com/deployment/docker/), [Docker Compose services](https://docs.docker.com/reference/compose-file/services/).
