# Thoner Python 백엔드

공급업체의 단가 인상 메일을 받아 대체 업체 견적 요청, 회신 비교, 구매안·역제안 작성까지 이어 주는 FastAPI 서버입니다.
Gmail로 메일을 받고, LangGraph 에이전트(①~⑤)가 업무를 진행하며, 결과는 Supabase(PostgreSQL)에 저장합니다.

## Thoner 전체 구성

Thoner는 구매 담당자가 공급업체의 단가 인상 통보를 받았을 때, 대체 업체 견적 요청부터 회신 비교, 구매안과
역제안 초안까지 이어 주는 구매 견적 검토 AI 서비스입니다. 메일 발송과 최종 결정은 담당자가 화면에서 확인하고 승인합니다.
서비스는 두 저장소로 나뉩니다.

| 저장소 | 맡은 일 | 기술 | 배포 |
|---|---|---|---|
| [gyeonjwo_frontend](https://github.com/costflow-food/gyeonjwo_frontend) | 업무 큐, 원문·첨부 보기, 검토 카드, 메일 초안 확인·발송 승인, 거래처, 실행 로그 화면 | Flutter Web, Riverpod | Vercel |
| gyeonjwo_backend (이 저장소) | API, Gmail 수신·발송, 원문·첨부 읽기, 에이전트 ①~⑤와 LLM 검증자, 저장 | FastAPI, LangGraph, Supabase | AWS Lightsail |

```
담당자(Chrome) ─ Flutter Web ─ HTTPS + Supabase 로그인 토큰 ─ FastAPI ─┬─ Supabase(PostgreSQL)
                   └ Supabase Auth (Google 로그인)                       ├─ Gmail API (메일 수신·발송)
                                                                         └─ LLM API (에이전트 ①~⑤, 검증자)
```

- 프론트는 Supabase Auth로 Google 로그인을 하고, 그 토큰을 `Authorization: Bearer`로 붙여 이 서버의 API를 부릅니다.
  프론트에는 공개해도 되는 값(Supabase publishable 키, API 주소)만 들어가고, 비밀 키는 모두 이 서버의 환경변수에 있습니다.
- 에이전트 다섯 역할은 아래와 같습니다. 흐름과 계약은 [에이전트 작업 안내](app/agents/README.md)에 있습니다.

| 역할 | 하는 일 |
|---|---|
| ① 정보 추출 | 메일 본문·첨부에서 업체·품목·단가·조건을 읽어 견적으로 저장 |
| ② 후보 추천 | 대체 업체 후보를 고르고 견적 요청(RFQ) 메일 초안 작성 |
| ③ 확인 우선순위 | 회신을 보고 업체에 더 물을 것을 골라 확인 메일 초안을 쓰거나, 구매안 단계로 넘김 |
| ④ 대응 구매안 | 유지·전환·분할 구매안을 만들고 도구로 금액 계산 |
| ⑤ 역제안 | 기존 업체에 보낼 가격 역제안 초안 작성 |

- 금액 계산은 LLM이 아니라 도구(Decimal)가 합니다. 초안과 최종 응답은 코드 규칙과 LLM 검증자를 거친 뒤 저장됩니다.

## 실행 (로컬)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m scripts.check_connection
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

- `.env`에 넣는 값은 `.env.example`에 있습니다: Supabase URL·서버 키·publishable 키, LLM endpoint·키, Gmail OAuth·토큰 암호화 키,
  Pub/Sub(선택), LangSmith(선택), `CORS_ORIGINS`, `APP_ENVIRONMENT`. 비밀 키는 채팅·프론트 코드에 넣지 않습니다.
- 모델·추론 수준·시간 제한은 환경변수가 아니라 `app/core/config.py` 상수입니다.
- 로그 수준은 `LOG_LEVEL`(기본 INFO), PDF 워커 메모리 상한은 `PDF_WORKER_MEMORY_MB`(기본 760)로 바꿀 수 있습니다.
- 스캔 PDF OCR 모델은 한 번 받아 둡니다: `python -m app.features.review.infrastructure.pdf_worker setup models/ocr`.
  서버 이미지는 빌드할 때 받습니다.
- 서버 문서: http://127.0.0.1:8000/docs (`APP_ENVIRONMENT=production`이면 `/docs`·`/redoc`·`/openapi.json`을 열지 않습니다). `/health`는 서버 생존, `/ready`는 필수 설정·DB 읽기·워커 상태입니다.
- 메일함 수신·RFQ·그래프 워커는 서버 시작 때 같이 뜹니다. 비밀값이 없으면 항목 이름만 경고하고 그 워커만 꺼집니다.
  같은 DB에 붙는 서버는 하나만 띄웁니다(단일 프로세스). 분석 중에는 `--reload`로 재시작하지 않습니다.
- 코드는 DB 구조를 만들거나 바꾸지 않습니다. DB 변경은 Supabase 마이그레이션으로 따로 합니다.

## 배포

- `dev` → `main` PR이 머지되면 `.github/workflows/deploy-server.yml`이 Docker 이미지를 빌드해 AWS Lightsail 컨테이너
  서비스(`gyeonjwo-api`)에 올립니다. 배포 뒤 `/health`·`/ready`로 새 빌드를 확인하고, 실패하면 직전 배포로 되돌립니다
  (`.github/scripts/deploy_lightsail.sh`).
- 운영 컨테이너는 메모리 1GB·CPU 0.5개 한 대입니다. API·메일 수신·워커·PDF 워커가 같이 돕니다.
- 프론트엔드(Flutter Web)는 별도 저장소에서 Vercel로 배포합니다. 운영 `CORS_ORIGINS`에 그 주소가 있어야 합니다.

## 환경변수와 긴급 정지

- 서버가 읽는 환경변수는 `.env.example`에 있는 이름이 전부입니다. 그 밖의 이름은 넣어도 조용히 무시됩니다
  (`Settings`가 `extra='ignore'`). 모델·시간 제한(`LLM_MODEL`, `LLM_TIMEOUT_SECONDS` 등)도 코드 상수라 환경변수로 바뀌지 않습니다.
- **워커·발송을 끄는 환경변수는 없습니다.** `WORKER_ENABLED`, `RFQ_MAIL_SENDING_ENABLED`,
  `GOOGLE_MAILBOX_WORKER_ENABLED` 같은 이름은 예전 코드의 것이고, 지금은 값을 바꿔도 아무 일도 일어나지 않습니다.
- `FORWARDED_ALLOW_IPS`는 프록시 뒤에서 꼭 맞춰야 합니다(Lightsail `172.26.0.0/16`). 틀리면 계정 연결이 `HTTPS_REQUIRED`로 거절됩니다.
- 이름 점검(값은 출력하지 않습니다):

  ```bash
  python -m scripts.check_env_names                      # .env.example ↔ 코드
  python -m scripts.check_env_names --env-file prod.env  # 운영 값(.env 또는 Lightsail environment JSON)
  ```

  배포할 때도 같은 점검이 돌아 코드가 읽지 않는 이름을 경고로 알려 줍니다. 저장소 변수 `PRUNE_UNREAD_ENV=1`로
  배포하면 그 이름을 빼고, 배포 확인에 실패하면 직전 환경변수로 되돌립니다. 정리가 끝나면 변수를 지웁니다.

실제로 멈추는 방법:

| 멈출 것 | 방법 | 되돌리기 |
|---|---|---|
| 메일 발송 | 메일함 기본 `mail_send_settings`에 `configured=false`인 새 버전을 추가합니다(DB, 메일함 단위, 즉시). 업무별 설정이 있으면 그 업무도 같은 방식입니다. 기존 행은 고칠 수 없고(append-only), 이미 받은 승인도 무효가 됩니다 | `configured=true` 새 버전 추가 |
| 메일 수신 | 사용자가 Google 연결을 해제합니다. `poll_seconds`는 간격만 늘릴 뿐 끄는 값이 없습니다 | 다시 연결 |
| 워커 전체 | 컨테이너를 내리거나 직전 배포로 되돌립니다. DB·환경변수 스위치는 없습니다 | 재배포 |
| 워커 일부 | 그 워커에 필요한 비밀값을 비우고 다시 배포합니다. 수신·RFQ는 `GMAIL_CLIENT_ID`·`GMAIL_CLIENT_SECRET`·`GOOGLE_TOKEN_ENCRYPTION_KEY`, 그래프(에이전트)는 `LLM_BASE_URL`·`LLM_API_KEY`(LLM 값을 비우면 분류가 필요한 수신 워커도 함께 꺼집니다) | 값을 다시 넣고 배포 |

## 코드 위치

| 위치 | 내용 |
|---|---|
| `app/main.py` | FastAPI 앱, 미들웨어(요청 제한·공통 헤더), 오류 처리, 라우터·워커 등록 |
| `app/core/` | 설정(`config.py`), 오류, 로그(`logs.py`), LangSmith 기록(`tracing.py`), 요청 ID, 멱등성, 작업 임대 |
| `app/api/` | 공통 응답 형식(`{data, meta}` / `{error, meta}`)과 의존성 |
| `app/agents/` | LangGraph 그래프, 에이전트 ①~⑤ 프롬프트·도구·계약, LLM 검증자. [에이전트 작업 안내](app/agents/README.md) |
| `app/features/<기능>/` | 기능별 `api` / `application` / `domain` / `infrastructure` |
| `app/shared/` | 여러 기능이 쓰는 도메인 규칙, DB 클라이언트·저장소, LLM 어댑터 |
| `scripts/` | 연결 점검(`check_connection.py`, 서버가 읽는 테이블 접근 확인), 로컬 회귀 점검(`check_*.py`), 에이전트 평가(`agent_eval/`), 서버 실행 스크립트 |

기능 폴더:

| 기능 | 내용 |
|---|---|
| `mailboxes` | Google 로그인 사용자 확인·허용 사용자 확인, Gmail 연결·토큰, 메일함 수신 워커 |
| `mail` | Gmail 알림(Pub/Sub) 수신, 원문·첨부 조회 |
| `work_cases` | 업무 큐·상세, 그래프 워커, 후보 선택, RFQ 초안·발송, 견적, 검토 카드 |
| `review` | 원문·첨부 읽기(PDF 텍스트·OCR 워커), 계산식·값 검사 규칙 |
| `suppliers`, `workflow_logs`, `mail_management`, `system` | 거래처, 실행 로그, 메일 관리, `/health`·`/ready` |
| `supplier_crawl` | 검색어로 공급처 홈페이지 연락처를 모으는 도구(아직 에이전트에 연결하지 않음) |

## 점검

Supabase·Gmail·LLM 실호출 없이 그래프 보류 코드, 로그 커서, 회신 큐 분기와 실패 경로를 확인합니다.

```bash
python -m unittest scripts.check_graph_hold scripts.check_workflow_logs scripts.check_review_queue scripts.check_env_names
```

### 에이전트 평가 (`scripts/agent_eval`)

에이전트 ②~⑤를 평가 시나리오 201개로 실행하고 자동 채점합니다. 서비스와 같은 `AgentRunner`(프롬프트·도구·최종 검사·LLM 검증자)를
그대로 쓰고, DB 대신 시나리오마다 새로 만든 메모리 저장소를 씁니다. 시나리오마다 새 프로세스에서 돌아 앞 시나리오의 결과가
다음 시나리오에 섞이지 않습니다. 실행 방법과 측정 기록은 [scripts/agent_eval/README.md](scripts/agent_eval/README.md)에 있습니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.agent_eval.oracle   # API 없이 시나리오 정답과 도구 계산이 맞는지 점검
.\.venv\Scripts\python.exe -m scripts.agent_eval.smoke    # API 없이 실행기 배선 점검
```

### 에이전트 실패 원인 찾기

역할 실행이 실패하면 `agent_runs.error_jsonb`에 코드형 원인만 남습니다. 메일 원문·예외 메시지·도구 인자는 저장하지 않습니다.

| 키 | 뜻 |
|---|---|
| `code` | 항상 `AGENT_FAILED` |
| `stage` | `TOOL`(마지막 도구가 예외) / `TOOL_LIMIT`(도구 호출 한도) / `FINAL_CHECK`(최종 검사 불일치) / `OTHER`(모델·그 밖) / `SUPPLIER_NAMES`(역할 실행 뒤 업체명 바꾸기) / `FINISH`(역할 완료 저장) |
| `error_type` | 예외 클래스 이름 (예: `ServiceError`, `UngroundedOutputError`) |
| `error_code` | `ServiceError` 코드 (예: `PROCESSING_UNAVAILABLE`) |
| `db_code`·`db_sqlstate` | DB 함수가 올린 코드형 예외와 SQLSTATE (예: `REVIEW_SUPPLIER_NOT_AUTHORIZED`, `23514`) |
| `last_tool`·`tool_calls` | 마지막으로 부른 도구, 도구 호출 수 |

DB 함수 `internal.graph_finish_role`이 이 값을 저장하도록 바뀐 뒤부터 남습니다(gyeonjwo_db `proposals/agent-failure-reason`). 그 전에는 `{"code": "AGENT_FAILED"}`만 남습니다.

```sql
select agent_role, status, error_jsonb, finished_at from agent_runs
where work_case_id = '<업무 ID>' order by started_at;
```

실행 로그 화면(API-28)의 `GRAPH_ROLE_FINISHED` 항목에도 `error_code`·`tool_name`이 함께 보입니다.

예외 메시지 원문까지 봐야 하면 서버 로그를 봅니다. 그래프는 `agent failed role=… error=…` 줄을 남깁니다. FAILED 저장까지 실패하면 `agent failure not saved role=… run=… stage=…` 줄이, 역할 호출 한도(8회)로 끝나면 `graph role call limit case_id=… role=…` 줄이 남습니다(DB에는 `GRAPH_FAILED`만 남음). Lightsail 컨테이너 서비스 `gyeonjwo-api`(서울, `ap-northeast-2`), 컨테이너 이름 `api`입니다.

- 콘솔: Lightsail → 컨테이너 → `gyeonjwo-api` → 로그(Logs) 탭
- CLI (Lightsail 읽기 권한이 있는 AWS 자격 증명 필요):

```bash
aws lightsail get-container-log --region ap-northeast-2 --service-name gyeonjwo-api --container-name api \
  --filter-pattern "agent failed" --start-time "2026-11-16T00:00:00Z"
```

로그는 Lightsail이 보관하는 기간만큼만 남습니다. 오래된 실패는 DB의 `error_jsonb`로 봅니다.

VS Code에서는 `gyeonjwo-backend.code-workspace`를 엽니다.
