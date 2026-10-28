# Google 계정 연결 API — 프론트 전달용

2026-10-28 · 로컬 구현, 아직 공개 HTTPS 서버에 배포하지 않음.

## API 계약

- POST `/api/v1/mailboxes/google/connect`
- GET `/api/v1/mailboxes/google/connection`
- 두 경로 모두 Supabase access token을 `Authorization: Bearer ...`로 전달한다. connect에는 `Idempotency-Key`가 필요하다.
- connect JSON: `provider_access_token`은 비어 있지 않은 문자열, `provider_refresh_token`은 문자열/null이다. 두 필드를 모두 전달하고 사용자 ID/이메일은 보내지 않는다.
- 승인된 보완: 미연결의 mailbox_ref/email 및 watch.expiration은 null 허용. 오류는 기존 error.code/message/fields/current_revision/retryable/next_action + meta.request_id 공통 계약을 유지한다.

성공 DTO는 요청한 `data.mailbox_ref/email/status/capabilities/watch`, `meta.request_id` 그대로다. 모든 시각은 UTC ISO 8601 Z다. status는 `CONNECTED`, `PARTIAL`, `NEEDS_RECONNECT`, `FAILED`, `NOT_CONNECTED`다.

| 조건 | 응답 상태 | watch/동기화 |
|---|---|---|
| read+send, refresh 검증 성공 | CONNECTED | read에 대해 watch 시도·최초 수집 job 저장 |
| 한 scope만 허용, refresh 검증 성공 | PARTIAL | read가 있을 때만 수집 |
| refresh 없음/철회/갱신 거절 | NEEDS_RECONNECT | watch/새 수집 접수 없음 |
| Gmail 두 scope 모두 거부, refresh 검증 성공 | PARTIAL | read/send=false, 수집 없음 |
| 저장된 연결 없음 | NOT_CONNECTED | 식별자·주소 null |

capabilities는 Google에서 실제 확인한 동의 scope다. NEEDS_RECONNECT에서 이전 동의 scope가 true여도 사용할 수 있는 연결이라는 뜻은 아니며 서버도 요청을 거절한다. watch.started는 실제 watch 응답을 확인한 경우만 true다. watch가 실패해도 유효 read 연결은 유지하고 90초 주기 수집 job으로 보완한다. CONNECTED는 최초 메일 수집 완료를 뜻하지 않는다.

첨부 Auth 흐름지도 5/11쪽 기준으로, 양쪽 Gmail 권한 거부는 정상적인 `PARTIAL`이며 `FAILED`로 취급하지 않는다. `FAILED`는 처리 실패 상태를 위한 DTO 값으로 유지한다. 현재 connect의 기술 오류는 공통 오류 응답의 5xx/429, 검증 실패는 401 등으로 전달한다. 로그아웃은 브라우저 세션만 종료하며 서버 연결을 삭제하거나 Google 토큰을 철회하지 않는다.

GET은 저장된 연결 상태를 읽고 watch 만료 표시를 계산한다. Google 권한 철회는 이후 서버 사용/주기 수집 시 갱신 검증에서 발견해 NEEDS_RECONNECT로 기록한다. GET만으로 Google에 매번 재동의·갱신 요청을 보내지는 않는다.

## 프론트 처리

1. Google 가입/로그인 시 offline access와 필요한 Gmail readonly/send scope를 요청한다. refresh token을 못 받으면 서버가 NEEDS_RECONNECT를 반환한다. refresh token을 얻기 위한 재동의 UX는 프론트에서 제공한다.
2. 새 사용자 연결 행동에 새 멱등키를 만들고 타임아웃·5xx·429에는 같은 키·같은 본문으로 확인한다. 토큰이 바뀐 새 요청에는 새 키를 쓴다. 10/28 사용자 승인 정책: 최종 DB 저장 전의 일시 오류만 같은 키로 재처리하며, 저장 완료 응답은 그대로 재생하고 저장 여부가 불명확하면 IN_PROGRESS를 유지해 재실행하지 않는다.
3. 409 REQUEST_IN_PROGRESS는 같은 키로 대기 후 확인한다. IDEMPOTENCY_KEY_REUSED는 기존 접수와 다른 본문이므로 새 행동인지 확인한다. 최초 응답을 request_id까지 그대로 재생한다.
4. 401은 재연결 UI, NEEDS_RECONNECT는 추가 동의 UI, PARTIAL은 read/send별 제한을 표시한다.
5. http://localhost:3000 CORS와 Authorization/Idempotency-Key/Content-Type/Cache-Control을 허용한다. 토큰을 브라우저 URL·콘솔·이슈·분석 로그에 넣지 않는다.
6. 프로덕션은 HTTPS만 쓴다. 서버는 직접 loopback 접속의 localhost HTTP만 예외로 허용한다. Docker 포트 변환/프록시가 있으면 HTTPS 프록시를 사용하고 Uvicorn에 신뢰하는 프록시만 지정한다. 임의 외부 X-Forwarded-Proto를 신뢰하지 않는다.

## 서버 설정·현재 한계

로컬 개발 기준 주소는 `http://127.0.0.1:8000`이며 Docker/기존 서버가 실행 중이라는 사실만으로 새 코드가 배포됐다고 판단하지 않는다. 새 이미지/프로세스로 다시 실행해야 한다. 외부 프론트에 전달할 공개 HTTPS 주소는 아직 없다. 배포 호스트/도메인이 정해진 뒤 전달한다.

Supabase `https://qlabhitzpdiawtbdmlgw.supabase.co`의 publishable key와 서버 secret key, 같은 Google OAuth client ID/secret, 서버의 지속 Fernet 키가 필요하다. 이 값들은 `.env` 또는 배포 secret에만 넣는다. Google OAuth 클라이언트는 프론트가 사용한 `gyeonjwo-web-supabase`와 일치해야 한다. 모델 API 설정도 별도로 필요하다.

`GOOGLE_TOKEN_ENCRYPTION_KEY`는 서버에 고정 보관한다. 코드가 임의로 생성·교체하지 않는다. DB에는 refresh token 암호문과 요청 HMAC만 전달하고 access token은 저장하지 않는다. Vault 연동은 아직 구현하지 않았다.

자동 수집은 [DB 계약](GOOGLE_MAILBOX_DB_CONTRACT.md)의 RPC 적용 후 `GOOGLE_MAILBOX_WORKER_ENABLED=true`로 켠다. 기존 고정 계정 워커와 동시에 켜면 시작을 거절한다. Pub/Sub 미설정/실패 시 기본 90초 폴링으로 수집하므로 시연 지연을 알린다. 기본 false에서는 자동 수집을 실행하지 않는다.

수신·분류·저장·회신 분기는 Python 코드와 RPC 인터페이스를 작성했다. 새 Supabase RPC, 성찬의 실제 분류 모듈, 후속 상세 추출/견적·재검토 큐, 실제 Google 계정은 통합 미검증이다. 실제 메일 발송은 구현/실행하지 않으며 승인 설정 검사 노드가 붙기 전 직접 발송을 차단한다.

## 로컬 검증 기록

2026-10-28 PDF 대조 후: `python -m pytest -q -p no:cacheprovider` — **282 passed**, 기존 Starlette/httpx 사용 중단 예정 경고 1건. 신규 연결/수집 테스트 58개와 기존 회귀 테스트 224개다. `python -m pip check` 충돌 없음, Python 컴파일·OpenAPI 경로/필수 필드/비밀 입력 표기·공백 검사 완료. 실제 Google·Supabase 호출 대신 모의 공급자/저장소를 사용한 결과이며 운영 연동 성공을 뜻하지 않는다. [PDF 대조 결과](AUTH_PDF_REVIEW_2026-10-28.md)를 함께 확인한다.

## 공식 참고

- [Google OAuth 웹 서버 흐름·refresh/offline access](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google OAuth scope와 offline 동작](https://developers.google.com/identity/protocols/oauth2/)
- [Supabase의 Google identity sub 모델](https://github.com/supabase/auth/blob/master/internal/models/identity.go)
