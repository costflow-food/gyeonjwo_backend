# Google 메일함 DB 연동 계약 — 본혁 담당자 검토용

2026-10-28 · 서버 브랜치 `feature/google-mailbox-connection`

이 문서는 Python에서 호출하는 **신규 RPC 제안**이다. 현재 Supabase에 존재하거나 실행을 검증한 함수 목록이 아니다. 배포된 DDL을 조회·변경하지 않았다. DB 담당자는 최신 06의 `mail_sync_states`, `mail_ingestion_events`, `mail_messages`, `work_cases`와 매핑하고 함수명·인자 변경이 필요하면 서버 담당자와 맞춘다. API URL·프론트 필드는 변경하지 않는다.

대상 프로젝트: `gyeonjwo-dev`, ref `qlabhitzpdiawtbdmlgw`. 모든 함수는 public 스키마에서 service_role 서버 전용이어야 한다. anon/authenticated 호출과 토큰 테이블 직접 SELECT/INSERT/UPDATE/DELETE를 금지하고 RLS를 켠다. 함수에 전달되는 user_id는 서버가 Supabase `/auth/v1/user`로 확인한 값이다. 클라이언트 본문의 user_id를 전달하지 않는다.

## 1. 연결·멱등성

| RPC | 인자 | 반환 |
|---|---|---|
| `claim_google_connection_request` | `p_user_id uuid`, `p_key text`, `p_body_hash text` | 아래 claim 객체 |
| `finish_google_connection_request` | `p_request_id uuid`, `p_user_id uuid`, `p_response jsonb`, `p_connection jsonb/null` | boolean true |
| `retry_google_connection_request` | `p_request_id uuid`, `p_user_id uuid` | boolean true |
| `get_google_mailbox_connection` | `p_user_id uuid` | connection 객체/null |
| `mark_google_mailbox_reconnect` | `p_mailbox_ref uuid`, `p_connection_revision bigint` | boolean |
| `rotate_connected_google_token` | `p_mailbox_ref uuid`, `p_connection_revision bigint`, `p_previous_ciphertext text`, `p_ciphertext text` | CAS 성공 boolean |

claim 범위는 `(user_id, POST, /api/v1/mailboxes/google/connect, key)`이다. 이 전용 RPC에서 method/path는 고정값이며 다른 API가 이 레코드를 공유하면 안 된다. body_hash는 서버 비밀키에서 파생한 HMAC-SHA256이다. 원본 요청·access token·refresh token은 저장하지 않는다.

```json
{"kind":"ACQUIRED","request_id":"uuid","mailbox_ref":"uuid"}
{"kind":"REPLAY","response":{"status":200,"body":{"data":{},"meta":{"request_id":"req-..."}}}}
{"kind":"IN_PROGRESS"}
{"kind":"CONFLICT"}
```

동일 키의 본문 해시 비교가 먼저이며 다르면 CONFLICT, 완료면 최초 응답 그대로 REPLAY, 처리 중이면 IN_PROGRESS다. 사용자별 연결 변경도 직렬화해 서로 다른 키의 reconnect가 토큰을 덮어쓰지 않도록 한다. claim과 재시도 선점은 DB 원자적 연산이다. 보존 기간 미확정이므로 자동 삭제·시간 경과 재선점은 하지 않는다. 프로세스 중단으로 남은 IN_PROGRESS는 운영자가 저장 결과를 대조한 후 복구해야 한다.

`retry_*`는 Google 조회 등 **최종 저장 트랜잭션 진입 전**의 일시 오류만 RETRYABLE로 바꾼다. 동일 키/해시로 다시 선점할 수 있어야 한다. 다른 본문으로 재사용은 계속 금지한다. `finish_*` 응답이 유실되면 상태를 되돌리지 않으며 같은 키 재조회로 완료 여부를 확인한다. 이 저장 전 일시 오류 재시도/저장 후 최초 응답 재생/저장 불명확 재실행 금지 정책은 10/28 사용자가 승인했다.

`finish_*`는 아래를 하나의 트랜잭션으로 처리해야 한다.

1. 요청 소유/처리 상태/claim을 검사한다.
2. `p_connection`이 있으면 메일함 연결을 저장·버전 증가한다. user_id와 mailbox_ref/Google sub 소유 범위를 재검증하고 같은 Google 계정을 다른 사용자에게 중복 연결하지 않는다.
3. `sync_requested=true`면 기존 수신 커서를 건너뛰지 않고 최초 수집 또는 후속 수집 job을 지속 큐에 한 번 접수한다. reconnect 시 이전 버전 job은 커서/메일을 수정하지 못한다.
4. 첫 HTTP 상태/본문 전체를 저장하고 요청을 COMPLETE로 바꾼다.

HTTP 200은 OAuth 연결 상태를 뜻하며 초기 메일 수집 완료를 뜻하지 않는다. refresh 없음/철회는 NEEDS_RECONNECT이며 ciphertext=null, sync_requested=false다. 검증 실패의 HTTP 401은 `p_connection=null`로 기존 연결을 덮어쓰지 않고 요청 결과만 기록한다.

### connection 저장 입력

```json
{
  "mailbox_ref":"uuid", "google_sub":"immutable Google subject", "email":"verified@example.com",
  "public": {
    "mailbox_ref":"uuid", "email":"verified@example.com", "status":"CONNECTED",
    "capabilities":{"read":true,"send":true},
    "watch":{"started":true,"expiration":"2026-11-01T00:00:00Z"}
  },
  "scopes":["https://www.googleapis.com/auth/gmail.readonly","https://www.googleapis.com/auth/gmail.send"],
  "encrypted_refresh_token":"Fernet ciphertext or null", "sync_requested":true
}
```

get 반환은 이 객체에 `user_id`, `connection_revision`을 더한다. `sync_requested`는 저장 DTO에서 생략해도 된다. 반환 범위를 사용자로 한정하며 ciphertext는 서버 전용이다. API 응답에는 public 객체만 사용한다. Fernet 키는 서버 비밀 설정이며 DB에 저장하지 않는다. Vault는 추후 교체 가능한 후보이며 이번 코드가 Vault 연동을 구현했다는 뜻은 아니다. 토큰 회전은 연결 버전과 이전 ciphertext를 비교해 CAS하며 업무 연결 revision은 증가시키지 않는다. 키를 잃으면 복호화할 수 없으므로 지속 보관해야 하고, 키 회전은 ciphertext·멱등 hash 호환 정책을 별도 적용한다.

## 2. Gmail 수집 job·이벤트

| RPC | 인자 | 반환 |
|---|---|---|
| `enqueue_connected_gmail_notification` | `p_subscription text`, `p_message_id text`, `p_email text`, `p_history_id text` | 지속 접수/기접수 true |
| `schedule_connected_gmail_sync` | 없음 | 구현별 요약/null |
| `claim_connected_gmail_sync` | 없음 | job 객체/null |
| `heartbeat_connected_gmail_sync` | `p_job_id uuid`, `p_claim_token uuid` | 현재 lease 연장 true |
| `finish_connected_gmail_sync` | `p_job_id uuid`, `p_claim_token uuid`, `p_connection_revision bigint`, `p_history_id text`, `p_watch jsonb` | 소유/버전/lease 검증 후 true |
| `fail_connected_gmail_sync` | `p_job_id uuid`, `p_claim_token uuid`, `p_code text` | 구현별 boolean |
| `connected_google_mailbox_health` | 없음 | 문자열 `google-mailbox-v1` |

```json
{
  "job_id":"uuid", "claim_token":"uuid", "user_id":"uuid", "mailbox_ref":"uuid",
  "connection_revision":1, "last_history_id":"1234567890 or null"
}
```

- enqueue는 OIDC 검증 후 호출한다. 서버 설정과 같은 subscription인지 확인하고 저장된 연결 이메일의 읽기 권한·상태를 검사한다. 알 수 없는/수집 불가 메일함을 성공 접수로 위장하지 않는다. `(subscription, message_id)` 중복은 한 번만 처리한다. SQL 성공 전에 204를 반환하지 않는다.
- schedule은 연결 상태 CONNECTED/PARTIAL + read=true + 유효 ciphertext인 메일함만 대상으로 한다. 사용자별/메일함별 동시 수집을 방지하고 같은 버전의 진행/대기 job이 있으면 중복 생성하지 않는다. 알림 수신과 폴링이 경합해도 직렬화한다.
- claim은 `FOR UPDATE SKIP LOCKED`와 동등한 원자적 선점, 120초 이상 lease, 고유 claim_token을 사용한다. heartbeat는 30초마다 실행한다. 만료된 lease를 복구해 재시작 가능해야 한다. 실패 job은 최소 30초~최대 수분 backoff 후 재시도해 busy loop를 막는다.
- finish만 커서를 전진시킨다. 모든 메일 저장 성공·현재 lease·현재 연결 버전·커서 단조 증가를 검사한다. history ID는 문자열이며 정수 비교 시 bigint 범위 초과를 고려해 numeric 또는 안전한 십진수 비교를 사용한다.
- 큐 처리 중 추가 알림이 오면 더 최신 history까지 후속 job을 남긴다. finish로 진행 중 수신된 알림을 지우거나 건너뛰면 안 된다.
- health는 실제 새 RPC/스키마 전환이 적용되고 연결·job·메일 저장 기능이 준비된 뒤 반환한다. 상수 반환만으로 미구현 함수를 준비 완료로 표시하지 않는다.

## 3. 메일·분류·회신 연결

| RPC | 인자 | 반환 |
|---|---|---|
| `connected_gmail_message_exists` | `p_mailbox_ref uuid`, `p_message_id text` | 이미 최종 저장된 메일이면 true |
| `find_connected_gmail_reply_candidates` | `p_mailbox_ref uuid`, `p_message jsonb` | candidate 배열 |
| `ingest_connected_gmail_message` | `p_job_id uuid`, `p_claim_token uuid`, `p_connection_revision bigint`, `p_message jsonb`, `p_classification jsonb/null`, `p_link jsonb` | `{outcome: ...}` |

메일 입력은 `message_metadata()`가 만든 JSON이다. Gmail message/thread ID, RFC Message-ID/In-Reply-To/References, 송수신자, UTC 수신시각, 원문 제목/body_text, 첨부의 Gmail attachment ID/part ID/파일명/MIME/크기를 가진다. 첨부 본체·원문 EML은 Storage에 자동 복제하지 않는다. 원본은 Gmail 식별자로 다시 조회한다. 사용자에게 제공할 원문/첨부 조회 API는 메일 소유 검증 후 이 식별자를 통해 읽어야 한다. 해당 신규 원문 HTTP API는 이번 두 API 요청 범위에 추가하지 않았다.

회신 후보는 서버 DB의 송신 메일과 RFC 참조, 같은 메일함/업무, 실제 송수신자 범위로만 만든다. 제목 유사도·thread ID·모델 추측만으로 연결하지 않는다. 예:

```json
[{"case_id":"uuid","same_participants":true,"matched_reference":"<sent-message@example.com>"}]
```

Python은 실제 수신 헤더에 matched_reference가 있는 유일한 업무만 `EXISTING_CASE`로 보낸다. 여러 업무·참조 불명확은 AMBIGUOUS이며 DB가 최종 저장 시 근거/소유/현재 보관 상태를 다시 검사한다. 후보를 찾지 못한 최초 메일은 NEW_MESSAGE다.

분류는 `{status: BUSINESS|NON_BUSINESS|UNCERTAIN, reason: string}`이다. 기술 실패는 분류값이 아니라 fail job이며 커서 전진 금지다. 확실한 기존 업무 회신은 짧은 “네” 같은 내용으로 신규 비업무 분류가 되지 않도록 classification=null + EXISTING_CASE로 전달한다.

ingest는 다음을 원자적으로 수행한다.

1. lease·연결 버전·메일함 소유 재검증, `(mailbox_ref, gmail_message_id)` 고유성 검사. DUPLICATE는 기존 내용을 덮어쓰거나 새 Work ID를 발급하지 않는다.
2. 원문·첨부 메타 저장. NEW_MESSAGE + BUSINESS일 때만 새 UUID work_cases를 Waiting으로 생성한다. 상세 추출·가격 생성은 하지 않는다.
3. NON_BUSINESS는 Work ID 없이 저장한다. UNCERTAIN/AMBIGUOUS는 별도 접수 확인으로 보내며 Work ID를 자동 생성하지 않는다.
4. 검증된 활성 회신은 같은 업무에 저장하고 추출/새 견적/재검토 이벤트를 지속 큐에 접수한다. 이 RPC 자체가 LLM을 호출하거나 HTTP를 붙잡지 않는다.
5. 수긍·보관된 업무 회신은 원문·알림만 저장하고 자동 재분석·발송을 접수하지 않는다.
6. 조회 중 삭제된 메일은 `{gmail_message_id, deleted:true}` + link.kind=DELETED로 수집 제외 증거를 저장한다.

outcome 허용값: `NEW_CASE`, `LINKED_REPLY`, `ARCHIVED_REPLY`, `INTAKE`, `NON_BUSINESS`, `DUPLICATE`, `DELETED`. 값만 반환하고 실제 저장을 생략하면 안 된다. 실패하면 트랜잭션을 롤백하고 API 워커가 다시 처리하도록 한다.

## 4. 팀 인수 순서

1. 본혁: 이 RPC 계약을 실제 06 스키마에 매핑한 SQL·RLS·테스트 결과를 공유한다. 서버는 임의 새 물리 테이블을 배포하지 않았다.
2. 정훈: 승인한 null/오류 계약, 같은 OAuth 클라이언트, refresh token 확보와 추가 scope 동의 흐름을 확인한다. Secret은 안전한 서버 설정으로만 전달한다.
3. 성찬: `ClassificationNode`의 3분기 결과와 실패 예외 인터페이스를 확정/교체한다. 현재 기본 어댑터는 기존 JsonAgent를 호출하며 성찬의 실제 코드를 가져온 상태는 아니다.
4. 서버: RPC 적용 후 `GOOGLE_MAILBOX_WORKER_ENABLED=true`, 기존 `WORKER_ENABLED=false`로 실행한다. 실제 연결·부분 동의·철회·push/폴링·중복·재시작을 함께 인수한다.

API 코드는 DB가 없어도 모의 검증할 수 있다. 실제 계정 수집·DB 저장의 완료 판정에는 위 적용/통합 테스트가 필요하다.
