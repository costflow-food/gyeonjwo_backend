# CostFlow Python Backend

FastAPI로 업무 접수, Supabase v2 RPC, 비공개 파일, 추출 및 검토 API를 연결한 서버입니다.
기존 Supabase 9테이블/61컬럼을 사용하며 실행 시 DB 구조를 변경하거나 seed를 넣지 않습니다.

## 이 컴퓨터에서 시작

### 입력 도구로 설정·시작

이 폴더의 VS Code 터미널에서 다음을 실행합니다. 키는 숨김 입력으로 받고 `.env`에만 저장합니다. 키를 채팅에 보내지 마세요.

```powershell
./start.ps1 -Setup -CheckOnly
```

Supabase의 Settings > API Keys에서 서버 전용 secret/service_role 키를 확인하고, Authentication > Users에서 팀원·검토자 UUID를 확인합니다. UUID는 이메일 주소와 다릅니다. 기존 키·목록이 설정되어 있으면 Enter로 유지할 수 있습니다.

설정 도구는 브라우저용 anon/publishable 키를 거부합니다. 연결 검사는 9테이블 읽기, 버킷의 private 여부와 등록한 Auth 사용자의 존재를 확인하며 데이터를 변경하지 않습니다. 실제 로그인·쓰기 RPC·업로드 성공을 대신하는 검사는 아닙니다.

검사 통과 후 서버를 시작합니다.

```powershell
./start.ps1
```

VS Code의 **Terminal > Run Task**에서도 `CostFlow: local setup and connection check`와 `CostFlow: check and start API`를 선택할 수 있습니다. PowerShell 정책이 실행을 막으면 정책을 바꾸지 말고 아래 개별 Python 실행 절차를 사용하세요.

VS Code에서 이 폴더를 열고 `.env.example`을 `.env`로 복사합니다. 로컬 가상환경과 실행/디버그 설정을 준비했습니다.

1. `.env`의 `SUPABASE_SERVICE_KEY`에 서버 전용 키를 **로컬에서만** 입력합니다.
2. Supabase Authentication에서 팀 사용자 계정을 준비합니다.
3. 해당 사용자의 UUID를 `COSTFLOW_USER_IDS` 또는 `COSTFLOW_REVIEWER_IDS`에 입력합니다. 검토자 목록에 있는 사용자는 일반 업무 권한도 가집니다. 두 목록이 모두 비어 있으면 업무 API를 차단합니다.
4. VS Code에서 `.venv/Scripts/python.exe`를 인터프리터로 선택하고 **CostFlow API** 디버그 구성을 실행하거나 아래 명령을 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn costflow.main:app --app-dir src --host 127.0.0.1 --port 8000 --reload
```

- Swagger: http://127.0.0.1:8000/docs
- 실행 확인: http://127.0.0.1:8000/health
- 실제 DB 연결 및 사용자 목록 설정 확인: http://127.0.0.1:8000/ready

`health` 성공은 실제 Supabase 연결 성공을 뜻하지 않습니다. 설정 전 `ready`의 503은 의도한 동작입니다. `.env`에 실제 키를 저장하기 전 서버 응답/로그/스크린샷에 키를 출력하지 마세요.

설정 후 데이터나 키를 출력하지 않고 연결만 확인하려면 다음을 실행합니다.

```powershell
$env:PYTHONPATH = "$PWD/src"
.\.venv\Scripts\python.exe tools/check_connection.py
```

VS Code가 Restricted Mode를 표시하면 실행/디버깅 전 사용자가 직접 이 폴더를 신뢰하도록 선택해야 합니다. 자동으로 보안 설정을 바꾸지 않았습니다.

현재 로컬 가상환경은 설치된 Anaconda의 패키지를 이용하며, 이미 제공된 pypdf를 추가했습니다. 다른 컴퓨터로 가상환경을 복사하지 말고 아래 설치 절차를 사용하세요.

## 새 컴퓨터 설치

Python 3.12 이상이 필요합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn costflow.main:app --host 127.0.0.1 --port 8000 --reload
```

환경변수는 `.env`보다 OS 환경변수 값이 우선합니다. 서비스 키는 신규 `sb_secret_...`와 기존 service_role JWT를 모두 지원합니다. 키를 브라우저/프론트엔드에 전달하지 않습니다.

## API 사용 순서

1. `POST /api/v1/auth/login`으로 로그인하고 받은 access_token을 Swagger의 **Authorize**에 입력합니다.
2. `POST /api/v1/work-items`로 업무를 생성합니다.
3. `POST /api/v1/work-items/{work_id}/files`에 PDF/EML/UTF-8 TXT를 업로드합니다.
4. 반환된 file_id로 `POST .../analyze`를 호출하거나 `PUT .../request-fields`로 검증한 추출값을 저장합니다.
5. `GET .../review`로 기준자료·계약·견적 및 비용 검토 카드를 조회합니다.
6. 검토자는 `POST .../decisions`로 품목행별 결정 또는 `POST .../reject`로 거절합니다.

자세한 계약과 예시는 [API 안내](docs/api.md)를 확인하세요. 기계가 읽는 명세는 [OpenAPI JSON](docs/openapi.json)에도 저장했습니다.

## 구현 범위

- JWT를 Supabase Auth 서버에서 검증하고, 서버 환경변수의 팀 사용자·검토자 허용 목록을 검사합니다. 클라이언트의 user_metadata나 요청 본문으로 검토자 권한을 부여하지 않습니다.
- 기준자료 6개는 읽기 전용입니다. 추출값과 결정은 기존 원자적 RPC만 사용합니다.
- 파일 이름 대신 UUID 저장 경로를 사용합니다. 비공개 버킷의 다운로드 링크는 인증 사용자에게 최대 15분만 발급합니다. 업로드는 최대 50 MiB이며 PDF/EML/TXT만 받습니다.
- 분석은 **명시된 라벨 기반**입니다. 없는 값은 NULL이며 원문 위치를 보존합니다. 자유 서술 AI 추론, 이미지 OCR, Outlook MSG, 이메일 수신 자동화는 연결되어 있지 않습니다.
- PDF는 텍스트가 있는 문서만 지원합니다. 스캔/암호화 PDF는 명시적인 오류로 반환합니다.
- 규격 불일치·가격 없는 견적·만료·MOQ·리드타임을 검사합니다. 기준자료에는 단위·통화 컬럼이 없으므로 최종 결정에 검토자의 명시적 비교 확인과 설명이 필요합니다. 자동으로 최저가나 승인 결정을 만들지 않습니다.
- 비용 총액은 `coverage_quantity`가 확인된 경우에만 단가로 나눕니다. 적용 수량이나 비용 조건이 없으면 계산 불가 사유를 반환합니다.
- 여러 품목 중 첫 결정 후에는 추출값을 고정하고, 모든 품목 결정이 끝나면 DB가 APPROVED로 전환합니다. 결정/거절 요약에 검토자 UUID와 한국 시각을 기록합니다.

## 테스트

```powershell
$env:PYTHONPATH = "$PWD/src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

네트워크 소켓 자체가 금지된 실행 환경에서는 다음 옵션으로 **소켓 없는 프로세스 내 ASGI 테스트**를 실행할 수 있습니다. 실제 FastAPI 라우팅/검증/의존성은 실행하며 Supabase만 가상 객체로 대체합니다. 운영 코드에는 이 테스트 루프를 사용하지 않습니다.

```powershell
$env:COSTFLOW_TEST_NO_SOCKETS = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

검증 상태와 한계는 [검증 기록](docs/validation.md)을 참고하세요.

## 운영 전 남은 설정

- 실제 서버 키, Auth 계정, 허용 UUID와 프론트엔드 CORS 주소 설정
- 실제 Supabase에서 로그인/RPC/업로드/서명 URL 통합 검증
- 실제 기준자료 입력과 원본 스키마 문서 대조
- 자유 서술 AI/OCR/메일 자동 수집이 필요하면 공급자 선택 및 별도 인증 설정
- HTTPS 배포와 프록시의 공통 속도 제한/파일 크기 제한, 모니터링

이 버전은 **한 팀이 공유하는 업무**용입니다. 현재 9테이블에는 테넌트/사용자 소유자 컬럼이 없으므로 여러 고객 조직을 격리하는 서비스로 그대로 배포하면 안 됩니다. 로그인 속도 제한은 서버 프로세스 단위이며 다중 인스턴스 공통 제한은 프록시에서 구성해야 합니다. DB 검증과 Storage 업로드는 서로 다른 서비스라 하나의 트랜잭션으로 묶이지 않습니다.

`supabase/manual/`의 SQL은 이전 빈 v1.8 suppliers를 전환할 때 적용한 기록입니다. 이미 v2.0인 DB에 다시 실행하지 마세요. 새 빈 DB의 자동 초기화용 migration은 아닙니다.

