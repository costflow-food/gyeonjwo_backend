# 검증 기록과 적용 범위

2026-10-19 작성. 이 프로젝트의 API 검증은 실제 FastAPI 앱과 가상 Supabase gateway로 수행했습니다.
운영 프로젝트의 비밀키는 조회하거나 복사하지 않았으며 운영 DB에 테스트 데이터를 저장하지 않았습니다.

최종 결과: 41개 테스트 전부 통과, Ruff 검사 통과, Python 구문 컴파일 및 OpenAPI 18개 operation 생성 확인. 원문 실행 기록은 `test-results.txt`에 있습니다.

추가한 로컬 설정 도구는 키 숨김 입력, 브라우저용 키 거부, 기존 환경변수 보존과 원자적 저장을 지원합니다. 연결 검사 도구는 9테이블 읽기, private 버킷 설정과 Auth 사용자 존재를 확인하도록 작성했으며 실제 키가 없어 라이브 실행은 하지 않았습니다. VS Code 작업과 `start.ps1`은 검사 성공 후 서버를 시작합니다. PowerShell 구문 검사를 통과했습니다.

이번 환경에서 Uvicorn 시작을 시도했지만 네트워크 루프 초기화가 완료되지 않아 중단했습니다. 서버가 실행 중이거나 실제 포트 연결에 성공한 상태는 아닙니다.

검증 환경: Python 3.14.6(설치된 Anaconda), FastAPI 0.141.1, Pydantic 2.13.4, Uvicorn 0.40.0, HTTPX 0.28.1, pypdf 6.10.0.

소켓 제한 환경에서는 asyncio의 표준 Windows wakeup socket 생성이 멈추므로 테스트에 한해 Event 기반 wakeup loop를 사용했습니다. FastAPI 요청 파싱·의존성·라우팅·응답 검증·미들웨어를 실행하며, 외부 I/O와 실제 Supabase만 대체합니다. 일반 로컬 및 CI에서는 표준 TestClient 루프를 사용합니다.

확인한 내용:

- 로그인 필요 여부, 사용자 허용 목록, 검토자 역할, 빈 설정의 접근 거부
- 비밀번호/토큰 입력을 오류 응답에 되돌려 보내지 않음, Cache-Control no-store
- 업무 생성·목록·상세·거절·페이지 조회와 검토자 기록
- 19필드 완전성, 중복/불린 행 번호 거부, 실패한 분석의 기존값 보존
- 여러 품목 결정 순서, 종료 후 수정 거부와 RPC 호출 계약
- UUID 경로의 비공개 업로드, 파일 형식/크기 제한, 서명 URL 유효시간 제한
- 실제 생성한 텍스트 PDF 읽기, 스캔 PDF의 OCR 필요 오류, HTML 이메일 스크립트 제외
- 라벨 기반 추출의 원문 위치와 누락값 NULL 처리
- 규격·가격·MOQ·만료·리드타임, Decimal 비용 계산 및 불명확한 비용의 계산 중단
- 신규 서버 키와 사용자 JWT 헤더 분리, SQL 오류 내용 미노출, 리다이렉트 미허용
- Swagger/OpenAPI 생성과 CORS, chunked body 크기 제한

이번 범위에서 확인하지 않은 내용:

- 실제 Supabase Auth 로그인/갱신/로그아웃, REST/RPC 및 Storage HTTP 연동
- 실제 네트워크 포트에서의 Uvicorn 시작·운영 HTTPS·프론트엔드 통합
- 자유 서술 AI, OCR, 이메일 서버 자동 수신, 다중 인스턴스 속도 제한과 부하 시험

DB 함수/트리거 자체의 제약조건 검증은 앞선 Supabase 설정 작업의 `supabase/validation/20261019-db-checks.json` 기록을 참고합니다. 가상 gateway 테스트로 실제 PostgreSQL 제약조건을 검증했다고 해석하면 안 됩니다.

참고 문서:
- https://fastapi.tiangolo.com/tutorial/dependencies/
- https://supabase.com/docs/guides/getting-started/api-keys
- https://docs.postgrest.org/en/v16/references/errors.html

