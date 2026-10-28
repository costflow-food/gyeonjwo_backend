# Auth 흐름지도 대조 검토

기준: 사용자가 제공한 `Auth 흐름지도.pdf`, 12쪽 전체. 텍스트를 추출하고 12쪽 모두 이미지로 렌더링해 도식·표·코드 경로를 확인했다. 5·11·12쪽의 상태 표와 서버 요구를 확대 대조했다. 원본 PDF는 수정하지 않았다.

작업 브랜치: `feature/google-mailbox-connection`. 프론트 소스/실제 브라우저 로그인 대신 제공 문서와 서버 코드·모의 테스트를 비교한 검토다. PDF에 적힌 프론트 테스트 70/70·실제 가입 성공은 문서의 보고이며 이번에 재실행한 결과가 아니다.

## 수정한 차이

5쪽 표와 11쪽 응답 규칙은 **read/send 모두 거부한 경우도 PARTIAL**이다. 기존 서버 분기는 scope가 모두 없으면 FAILED였으므로 PARTIAL로 수정했다. 이 경우 capabilities=false/false, watch.started=false, 첫 수집 job 없음이며 read/send 작업을 모두 거절한다. 처리 실패와 사용자 권한 거부를 구분한다. URL·필드명·상태 enum은 변경하지 않았다.

refresh token 없음/철회의 NEEDS_RECONNECT가 우선한다. 네트워크·제공자·저장 오류는 공통 HTTP 오류 계약으로 전달하며 동의 거부를 장애로 표시하지 않는다.

## 요구와 구현 대조

| PDF | 항목 | 판정·현재 근거 |
|---|---|---|
| 1~4 | Google 동의·Supabase PKCE·메모리 토큰 전달 | 프론트 책임. Python은 코드 교환/로그인 동의 화면을 중복 구현하지 않는다. Supabase가 검증한 사용자 조회만 사용한다. |
| 3·4·6 | 새로고침 시 Google 토큰 없이 GET 상태 조회 | 일치. 저장된 사용자별 연결을 읽고 Google tokeninfo/refresh/watch를 재호출하지 않는 모의 테스트 추가. |
| 4·10 | POST/GET의 경로·Bearer·Idempotency-Key·본문 두 필드 | 일치. 두 입력은 필수이며 refresh 값만 null 허용. 사용자 ID/이메일 추가 입력은 거절한다. |
| 5·11 | 네 가지 read/send 조합 | 모두 허용 CONNECTED, 한쪽/양쪽 거부 PARTIAL로 수정·검증. |
| 5·11 | refresh 없음·철회·401 | NEEDS_RECONNECT/401과 원인별 처리. 타 계정·aud·만료 토큰 거절, 갱신 토큰도 같은 Google subject로 재검증. |
| 6 | 브라우저 로그아웃 후 서버 연결 유지 | 일치. Supabase 세션 종료가 서버 ciphertext/메일함 삭제를 유발하지 않는다. 무인증 GET 401 후 재로그인 GET 복원 모의 테스트 추가. |
| 7 | 라우트 가드는 권한 검사를 대체하지 않음 | 두 신규 API는 Supabase `/auth/v1/user`의 UUID·Google identity로 사용자 확정. 수신 작업은 저장된 scope/현재 refresh 권한과 연결 버전을 검사한다. 기존 전체 업무 API의 새 DB/소유 모델 전환이 완료됐다는 의미는 아니다. |
| 11 | 암호화 보관 | 서버 비밀 Fernet 키 사용. DB에는 ciphertext/HMAC만 전달. Vault는 후보이며 이번 구현에 Vault 호출은 없다. 실제 저장은 신규 RPC 적용 후 가능. |
| 11 | watch·첫 동기화·mail_sync_states | read가 있고 refresh 검증 성공일 때 watch 호출 및 첫 job 저장 RPC 연결. 물리 mail_sync_states 매핑/원자적 저장은 DB 담당자 구현·인수가 남음. |
| 12 | 비밀정보 비노출 | SecretStr, 원문 없는 오류, 토큰 query를 기록하지 않는 Google 어댑터, 로그 없는 입력/응답 검증. 실제 자격증명은 사용하거나 Git에 추가하지 않았다. |
| 12 | CORS·Cache-Control·HTTPS | localhost:3000과 네 헤더 허용. 배포 origin은 Settings.cors_origins에 추가해야 한다. 프록시 없는 실제 loopback만 HTTP 허용, 외부 HTTP는 거절. |
| 12 | 업무 큐까지 실제 수신·부분 동의 테스트 | 아직 미완료. 실제 DB RPC·Google client secret·Pub/Sub/폴링 배포·분류 노드 연결과 프론트 합동 테스트 필요. |

## 기존 합의와 문서보다 구체적인 정책

- 미연결 mailbox_ref/email, watch.expiration은 null 허용한다. 사용자 승인 완료.
- 오류는 기존 08의 확장 error 객체 + meta.request_id를 유지한다. error.code 접근은 동일하다. 사용자 승인 완료.
- 최종 DB 저장 전 일시 오류만 같은 키로 다시 처리한다. 완료 응답은 request_id까지 재생하며 저장 여부가 불명확하면 IN_PROGRESS를 유지한다. 사용자 승인 완료. PDF의 같은 키 재시도와 함께 적용한다.
- watch를 못 시작해도 유효 read 연결은 보존하고 90초 폴링 job으로 보완한다. 수집 시작은 연결 200과 구분하며 자동 수집 기능은 기본 false다.

## 남은 연결 작업

1. 본혁: [DB RPC 계약](GOOGLE_MAILBOX_DB_CONTRACT.md)을 실제 스키마에 매핑·배포하고 RLS/소유 범위·동시 선점·커서/메일 원자성을 검증한다. 서버는 테이블을 임의로 만들지 않았다.
2. 안전한 서버 설정: 같은 `gyeonjwo-web-supabase` OAuth 클라이언트, Supabase key, 지속 암호화 키, LLM, Gmail topic/subscription/OIDC를 설정한다. 자격증명을 이슈나 채팅에 올리지 않는다.
3. 성찬의 실제 분류 노드를 `ClassificationNode` 인터페이스에 연결한다. 기본 기존 JsonAgent 어댑터의 모의 성공을 성찬 코드 인수로 표시하지 않는다.
4. HTTPS 서버와 실제 프론트 origin을 확정한다. 프론트의 `config/dart_defines.local.json`에 `API_BASE_URL`을 설정한다. 현재 공개 서버 URL은 없다. 임의 예시 URL을 운영 주소로 전달하지 않는다.
5. 두 권한 모두 허용·읽기만·발송만·모두 거부·refresh 없음/철회·새로고침·로그아웃/재로그인·중복 push·수신 실패/복구를 같은 계정으로 검증한다.

로컬 테스트 통과와 실제 계정 연동 완료를 구분한다. 기존 업무 큐 `/work-cases` API 전환/통합도 별도이며 문서만으로 기존 데모 큐가 실제 새 Work ID를 표시한다고 판단하지 않는다.

## 최종 로컬 검증

전체 테스트 **282 passed**(기존 224 + 신규 58), Starlette/httpx 사용 중단 예정 경고 1건. 의존성 충돌 없음, git diff 공백 검사 통과. 원본 PDF 및 `.env`는 수정하지 않았다. 코드의 PR·병합 이력은 GitHub에서 확인하며 실제 DB·Google·프론트 통합 인수는 이 테스트에 포함하지 않는다.
