# 견줘 Python 백엔드

공급업체의 단가 인상 메일을 받아 대체 업체 견적 요청, 회신 비교, 구매안·역제안 작성까지 이어 주는 FastAPI 서버입니다.
Gmail로 메일을 받고, LangGraph 에이전트(①~⑤)가 업무를 진행하며, 결과는 Supabase(PostgreSQL)에 저장합니다.

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

## 코드 위치

| 위치 | 내용 |
|---|---|
| `app/main.py` | FastAPI 앱, 미들웨어(요청 제한·공통 헤더), 오류 처리, 라우터·워커 등록 |
| `app/core/` | 설정(`config.py`), 오류, 로그(`logs.py`), LangSmith 기록(`tracing.py`), 요청 ID, 멱등성, 작업 임대 |
| `app/api/` | 공통 응답 형식(`{data, meta}` / `{error, meta}`)과 의존성 |
| `app/agents/` | LangGraph 그래프, 에이전트 ①~⑤ 프롬프트·도구·계약, LLM 검증자. [에이전트 작업 안내](app/agents/README.md) |
| `app/features/<기능>/` | 기능별 `api` / `application` / `domain` / `infrastructure` |
| `app/shared/` | 여러 기능이 쓰는 도메인 규칙, DB 클라이언트·저장소, LLM 어댑터 |
| `scripts/` | 연결 점검(`check_connection.py`, 서버가 읽는 테이블 접근 확인), 로컬 회귀 점검(`check_*.py`), 서버 실행 스크립트 |

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
python -m unittest scripts.check_graph_hold scripts.check_workflow_logs scripts.check_review_queue
```

VS Code에서는 `gyeonjwo-backend.code-workspace`를 엽니다.
