# 기능별 폴더와 레이어

실제 Python 코드를 아래 위치로 이동했다. 이전 `app/gmail.py`, `app/services.py`, `app/models.py` 같은 평면 모듈을 복제해 남기지 않는다. 실행 진입점은 계속 `app.main:app`이므로 Docker·기존 실행 명령은 같다.

```text
app/
  main.py                         # 앱 생성, 미들웨어, 공통 오류, 라우터/워커 연결
  api/
    dependencies.py               # 사용자 인증·서비스 의존성
    responses.py                  # 공통 JSON 직렬화
  core/
    config.py                     # 환경설정·프로젝트 루트
    errors.py                     # 공통 예외
    http_limits.py                # 요청 크기 제한
    leases.py                     # 실행 소유권 heartbeat
  features/
    auth/application/auth.py      # Supabase 사용자·접근 권한 확인
    mail/
      api/router.py               # Gmail 웹훅·인증된 LLM 분류 API
      application/
        classification.py         # 수신 메일의 경량 분류
        worker.py                 # 자동 수신·동기화·저장·복구 흐름
      domain/schemas.py           # 메일 분류 요청·결과 모델
      infrastructure/gmail.py     # Gmail OAuth·API·Pub/Sub 검증
    review/
      api/router.py               # 업무 상세·분석·첨부·비고 API
      application/
        services.py               # 업무 조회·변경·분석 실행 서비스
        pipeline.py               # 기존 검토 파이프라인
      domain/
        calculations.py           # Decimal 금액 계산
        validation.py             # 숫자·단위·원문 검증
        schemas.py                # 추출·산식·카드 텍스트 모델
        responses.py              # 공개 검토 결과 모델·불변식
      infrastructure/
        documents.py              # 메일·문서 읽기
        pdf_worker.py             # 격리 PDF 프로세스
        reference_files.py        # 참고자료 원문 조회
    system/api/router.py          # /health·/ready
  agents/                         # 에이전트 (작업 안내: app/agents/README.md)
    schemas.py                    # 실행 한도·백엔드↔그래프 계약·역할별 입출력
    graph.py                      # LangGraph 실행 조정기
    agents.py                     # 역할별 프롬프트·create_agent 실행기
    tools.py                      # 역할별 허용 도구·도구 구현
    nodes.py                      # 코드 노드 (전처리·구매안/가격 검증·검토 카드)
  shared/
    domain/schemas.py             # 공통 StrictModel·빈 요청
    infrastructure/
      llm.py                      # 공통 LLM 제공자 어댑터
      db/                         # Supabase 클라이언트·RPC·기준자료·Storage
scripts/                          # 실행·연결 진단·계산 검증 화면
tests/                            # 기능/회귀 검증
```

## 어디를 수정할까

| 작업 | 먼저 볼 파일 |
|---|---|
| 메일 수신 응답·LLM API 입력 | `app/features/mail/api/router.py` |
| 메일 원문 조회·등록·분류·재시도 | `app/features/mail/application/worker.py` |
| Gmail 제공자 통신·인증 | `app/features/mail/infrastructure/gmail.py` |
| Supabase RPC/컬럼 계약 | `app/shared/infrastructure/db/contracts.py` |
| 에이전트 역할·도구·조정기 | `app/agents/` |
| 기존 분석 파이프라인 | `app/features/review/application/` |
| 계산·물량/가격 검증 | `app/features/review/domain/` |
| 환경변수·실행 설정 | `app/core/config.py`, `.env.example`, `compose.yaml` |

API 레이어는 HTTP 입력·인증·응답을, application은 업무의 진행 순서를, domain은 데이터/계산 규칙을, infrastructure는 Gmail·LLM·DB·파일 같은 외부 연동을 맡는다. 에이전트 담당자는 application 서비스와 허용된 조회/계산 도구를 연결한다. 이 폴더 정리가 최신 기획의 다섯 에이전트·새 논리 DB를 모두 구현했다는 뜻은 아니다.

## Google 로그인 메일함 연결

`app/features/mailboxes/`에 별도로 분리했다. 기존 `mail`은 구 고정 계정 워커이며 동시에 실행하지 않는다.

- `api/router.py`: 두 연결 API, HTTPS·인증·멱등 응답.
- `application/auth.py`: Supabase Auth의 검증된 Google identity.
- `application/service.py`: OAuth 확인·갱신·연결 상태·권한 검사.
- `application/worker.py`: 저장된 연결 정보로 수집, 분류 노드 연결, 커서 관리.
- `application/ingestion.py`: Gmail 본문·첨부 메타 파싱 및 회신 참조 검사.
- `domain/schemas.py`: 프론트 DTO·입력 비밀 문자열·scope 상수.
- `infrastructure/google.py`: 토큰 및 Gmail REST 호출, 토큰 URL 비기록.
- `infrastructure/crypto.py`: 서버 비밀키 기반 암호화/HMAC.
- `infrastructure/repository.py`: DB 담당자와 맞출 Supabase RPC 어댑터.

기존 `Services`는 구 DB에 연결된 서비스 진입점으로 유지했다. 최신 업무 그룹/견적/발송 기능을 구현할 때 각 기능 application으로 분해할 수 있지만, 이번 정리에서 새 DB 테이블이나 동작을 임의 생성하지 않았다.

## 이동 시 함께 수정한 것

- 모든 Python import와 테스트 monkeypatch 경로, 계산 확인 화면의 import를 새 경로로 수정.
- 설정 파일 기준 경로가 프로젝트 루트의 `.env`를 계속 가리키도록 수정.
- PDF 하위 프로세스의 모듈 경로와 실행 디렉터리를 새 위치에 맞춤.
- 테스트가 같은 인증 dependency를 교체하도록 수정.
- API 문서는 system/mail/review 태그로 구분.
- 인증 서비스 장애를 설정 누락과 구별하고, lifespan 종료 시 수신 워커 정리를 `finally`로 보장.

검증 현황은 [서버 가이드](MAIL_API_SERVER.md)의 최종 확인 기록을 참고한다. 현재 작업은 로컬 브랜치이며 사용자의 요청에 따라 커밋·푸시·PR·main 병합은 수행하지 않는다.
