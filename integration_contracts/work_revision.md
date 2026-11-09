# 업무 수정 저장 RPC 계약

dev 마이그레이션 `20261106150401` 기준이다. 아래 data는 응답 예시다. 현재 백엔드는 응답 envelope와 HTTP 상태를 검사하며 data 내부 필드는 저장소에서 받은 그대로 반환한다.

## 호출 공통

`submit_work_revision(p_owner, p_target, p_operation, p_body, p_key, p_hash, p_request_id)`는 service_role 전용이다. 백엔드는 인증된 사용자 UUID를 p_owner로, URL의 대상 UUID를 p_target으로 전달한다.

p_body는 검증된 DTO를 JSON으로 직렬화한 객체다. 선택 필드의 기본값도 포함한다. p_hash는 현재 Python `json.dumps(p_body, sort_keys=True, separators=(',', ':'), ensure_ascii=True)` 결과를 UTF-8로 인코딩한 SHA-256 소문자 64자리다. 이 규칙은 START 해시와 별개다. START는 `ensure_ascii=False`를 사용하며 revision만 있는 요청의 해시는 유지된다.

## 연산별 요청과 응답

| 연산 | HTTP 및 대상 | p_body 예시 | 성공 data 예시 |
|---|---|---|---|
| EDIT_CONDITIONS | PATCH /api/v1/work-cases/{case_id}/conditions · 202 | `{"expected_revision":3,"changes":{"quantity":"600","unit":"kg","due_date":"2026-11-30"},"reason":null,"evidence_refs":[]}` | `{"case_id":"<case UUID>","input_revision":4,"progress_url":"/api/v1/work-cases/<case UUID>/progress"}` |
| CORRECT_QUOTE | POST /api/v1/quote-versions/{quote_id}/corrections · 202 | `{"expected_revision":3,"source_quote_id":"<원본 UUID>","corrections":{"unit_price":"5000"},"reason":"원문 확인","evidence_refs":[]}` | `{"case_id":"<case UUID>","quote_id":"<새 견적 UUID>","source_quote_id":"<원본 UUID>","input_revision":4,"progress_url":"/api/v1/work-cases/<case UUID>/progress"}` |
| EDIT_DRAFT | POST /api/v1/mail-drafts/{draft_id}/revisions · 201 | `{"expected_revision":3,"expected_snapshot_hash":"<hash>","recipient":"supplier@example.com","subject":"견적 요청","body_text":"견적을 부탁드립니다.","edit_reason":"문안 수정"}` | `{"case_id":"<case UUID>","draft_id":"<새 초안 UUID>","replaces_id":"<원본 UUID>","input_revision":3}` |
| WITHDRAW_DRAFT | POST /api/v1/mail-drafts/{draft_id}/withdraw · 200 | `{"expected_revision":3,"expected_snapshot_hash":"<hash>","reason":"발송 취소"}` | `{"draft_id":"<초안 UUID>","withdrawn":true}` |

견적 정정의 source_quote_id는 HTTP 요청에서 생략할 수 있다. RPC에는 경로 UUID가 항상 들어간다. 지원 필드는 dev 배포 계약을 따른다.

- 조건은 `quantity`, `unit`, `due_date`를 허용한다.
- 견적 정정은 `unit_price`, `shipping_cost`, `available_quantity`, `moq_quantity`, `order_increment_quantity`, `unit`, `moq_unit`, `order_increment_unit`, `effective_from`, `valid_until`을 허용한다.
- `evidence_refs`는 기존 `graph_check_refs` 형식의 문자열 배열이다.

## 저장과 멱등성

- 소유자·연산·대상·키를 묶어 요청을 식별한다. 동일 해시의 재요청은 상태 검사보다 먼저 최초 응답을 재사용한다. 다른 해시는 IDEMPOTENCY_KEY_REUSED, 동시 처리 중이면 REQUEST_IN_PROGRESS를 반환한다.
- 변경·버전·로그·최초 응답은 한 트랜잭션에 기록한다. 조건과 견적의 변경은 이전 결과를 stale로 표시한다. 이 배포에서 조건 변경 후 PLAN 자동 예약은 후속 작업이다.
- 초안 수정은 새 버전을 만들고 승인을 승계하지 않는다. 철회는 미발송 말단 초안에 한정한다. 발송 중·성공·결과 불명 상태와 동일 발송 의도의 우회는 DB에서 차단한다.
- 성공은 `{"kind":"ACCEPTED","http_status":202,"response":{"data":{},"meta":{"request_id":"req-example"}}}` 형태다. 재응답은 kind만 REPLAY로 바꾸고 최초 HTTP 상태, data, request_id를 유지한다.
- 오류는 `{"kind":"ERROR","code":"REVISION_CONFLICT","current_revision":4}` 형태다. RESOURCE_NOT_FOUND는 404, REVISION_CONFLICT·DRAFT_CHANGED·SEND_RESULT_UNKNOWN·ACTION_NOT_ALLOWED·REQUEST_IN_PROGRESS·IDEMPOTENCY_KEY_REUSED는 409, INVALID_CONDITIONS는 422다. RPC 미배포는 백엔드가 503 WORK_REVISION_INTEGRATION_REQUIRED로 반환한다.
