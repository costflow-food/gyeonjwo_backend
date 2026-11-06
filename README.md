# 견줘 Python 백엔드

**연동 마무리 브랜치:** 진단·팀 HTTPS 구성·DB 초안·검증 범위. 팀 분류 함수 통합, 격리 PostgreSQL로 검증한 저장 RPC 초안, 설정/오래된 서버/CORS 진단과 개발용 HTTPS 실행 구성을 추가했습니다. 실제 Supabase 변경 및 외부 공개는 실행하지 않았습니다.

**Google 로그인 계정 연결 작업:** 프론트 계약·설정, DB 담당자 RPC 계약. `POST /api/v1/mailboxes/google/connect`, `GET /api/v1/mailboxes/google/connection`과 연결 사용자용 수신 워커를 추가했습니다. 새 DB RPC·자격증명·HTTPS 배포 및 실제 계정 통합 인수는 별도입니다.

**2026-10-28 API 공통 계약:** JSON 성공 응답은 `{data, meta}`, 오류는 `{error, meta}`이며 `meta.request_id`와 `X-Request-ID`가 같습니다. 파일 스트림·204 웹훅·운영 `/health`, `/ready`는 각 기존 응답을 유지합니다. 프론트는 기존 JSON 본문 필드를 `data` 안에서 읽어야 합니다. 적용 범위·다음 단계·DB 연동 계약을 확인하세요. `/work-cases` 전환·비동기 처리·지속 멱등성은 아직 연결되지 않았습니다.

**메일 수신 API·LLM 호출용 서버 실행:** Docker/로컬 실행 가이드. 작업 브랜치 `feature/mail-api-server`, Dockerfile·compose.yaml·인증된 메일 분류 LLM API를 추가했습니다. Docker 이미지 빌드·컨테이너 healthy 및 실제 HTTP 응답 확인을 마쳤습니다. 전체 테스트175개 통과이며 실제 외부 서비스 연동은 별도입니다.

**에이전트 작업 폴더:** `app/agents/` (LangGraph 그래프·계약·프롬프트·도구). 담당·브랜치·작업 방법은 [에이전트 작업 안내](app/agents/README.md)를 보세요.

**기능별 코드 위치:** 폴더·레이어 안내. `mail`, `review`, `auth`, `system` 기능과 API/application/domain/infrastructure 책임으로 분리했습니다.

**2026-10-28 기획 개정:** 최신 Notion 01~07을 읽고 프로젝트 기준을 새로 분석·저장했습니다. 현재 Python 코드는 10월 26일 구 기획 기반이며 최신 Must 전체를 구현한 상태가 아닙니다.

- 최신 프로젝트 기준·문서별 분석
- 기존 코드와의 차이·개발 순서
- 최신 인수 조건 AC-01~23

아래 실행·SQL/RPC 안내는 **기존 구현**에 대한 것입니다. 새 논리 스키마와 호환된다고 가정하지 마세요. 기존 구현에는 업무 API·notes·Gmail 영속 수신·분석/계산·카드 응답이 있으며, 실Supabase·Gmail·LLM 및 Flutter 통합 검증은 아직 수행하지 않았습니다.

## 실행 (PowerShell)

```powershell
cd C:\Users\user\Documents\Codex\gyeonjwo_backend
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

`.env`에 Supabase URL·서버 키·publishable 키, LLM endpoint·키, Gmail OAuth·토큰 암호화 키(Pub/Sub는 선택)를 입력합니다. 모델·추론 수준·시간 제한은 `app/core/config.py` 상수입니다. 비밀 키는 채팅이나 Flutter 코드에 넣지 않습니다. SQL v2.2(13개 테이블·101개 컬럼·서버 RPC 19개)가 배포된 Supabase가 필요합니다. 코드는 DB 구조를 자동 생성하거나 변경하지 않습니다.

현재 이 PC에는 `.venv`와 패키지를 설치했습니다. 다른 PC에서 처음 받을 때는 아래를 먼저 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
.\.venv\Scripts\python.exe -m scripts.check_connection
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

서버 문서: http://127.0.0.1:8000/docs. `/health`는 서버 생존 확인, `/ready`는 필수 설정·DB 읽기 점검·Gmail 워커 실행 상태 확인입니다. `/ready`의 llm=configured는 모델 응답 품질·접속 성공을 뜻하지 않습니다. 연결 메일함 워커와 RFQ 메타데이터 워커는 서버 시작 시 자동으로 시작되고, 비밀값이 없으면 항목 이름만 경고한 뒤 그 워커만 꺼집니다. 같은 DB에 붙는 서버는 하나만 띄웁니다. 단일 프로세스, 한 번에 한 업무 분석을 사용합니다. 분석 중 --reload로 재시작하지 마세요.

## 코드 위치

- `app/db/contracts.py`: 스키마 컬럼과 서버 RPC 19개 계약·호출(notes 포함).
- `app/db/references.py`: 기준자료 조회·페이지네이션·Decimal·거래처/품목 대조.
- `app/db/storage.py`: 원문 EML 업로드, 안전 경로, 다운로드 후 해시 검증.
- `app/auth.py`: Supabase JWT 검증 + 사용자 문맥의 접속 허용 목록 확인.
- `app/config.py`, `app/db/client.py`: 로컬 환경설정·서버 클라이언트.
- `app/main.py`, `app/services.py`: 업무 HTTP API·인증·상태 제어.
- `app/gmail.py`, `app/worker.py`: 인증된 Gmail 알림·history 동기화·재처리.
- `app/pipeline.py`, `app/calculations.py`, `app/validation.py`: 검토 흐름·제한 연산·검증.
- `app/llm.py`, `app/models.py`: 교체 가능한 JSON 모델 어댑터·역할별 출력 형식.
- `docs/openapi.json`: Flutter 팀에 전달할 현재 HTTP 계약.

VS Code에서는 `gyeonjwo-backend.code-workspace`를 열면 됩니다.

자동 테스트는 네트워크·실키 없이 실행되며 실제 외부 서비스를 통과했다는 의미가 아닙니다. 텍스트 PDF·텍스트/HTML 메일을 지원합니다. 스캔/OCR·기타 첨부는 검토 필요로 중단합니다. MVP2 크롤링·외부 시세·초안·증빙 반영 및 MVP3 발주는 구현 범위에 포함하지 않습니다.


테스트 파일과 테스트 전용 의존성은 사용자 요청으로 제거했습니다. 문서의 테스트 통과 수치는 제거 전 검증 기록입니다. 실행 설정 진단 scripts/check_integration.py는 유지합니다.

### 백엔드 로컬 회귀 점검

저장소 루트에서 다음 스크립트를 실행한다. Supabase·Gmail·LLM 실호출 없이
보류 코드, 로그 커서, 회신 큐 분기와 실패 경로를 확인한다.

```bash
python -m unittest scripts.check_graph_hold scripts.check_workflow_logs scripts.check_review_queue
```

전체 앱을 실행하려면 `requirements.txt`를 설치한다. 배포 후 검증은
[메일 발송·회신·검토 통합 검증](docs/mvp1-mail-review-integration.md)을 따른다.
