# 공통 계약 검증 및 다음 단계

2026-10-28 · 작업 브랜치 `feature/api-common-contract`

## 기준

- [06 데이터 스키마·저장 방식](https://app.notion.com/p/06-3ff29a34f85780e1b9f0e4ab016b693c)
- [08 API 명세서](https://app.notion.com/p/08-API-40029a34f85780398e5bde82da656521)

이번에 두 문서를 직접 재조회했다. 06은 새 논리 설계이며 실제 DB·코드 전환이 별도 작업이라고 명시한다. 로컬 DB 어댑터는 여전히 구 `work_items`·19개 RPC 계약이다. 배포된 Supabase의 테이블/함수는 이번에 조회하거나 변경하지 않았다.

## 완료한 로컬 작업

기존 staged 공통 계약 수정은 그대로 보존했다. 응답 봉투·오류·request_id·CORS·429·웹훅 제한 제외 테스트와 함께 캐시 방지·병렬 요청 ID 격리를 추가 검증한다.

새 `app/features/work_cases/domain/contracts.py`에는 다음 도메인 계약과 순수 검증 함수를 추가했다.

- UUID 업무 식별자와 정확한 `Waiting/Running/Complete` 상태.
- 사용자 판단과 행동 코드, 서로 모순되지 않는 `allowed_actions/disabled_actions` DTO.
- `expected_revision`의 정수 검증 및 충돌 시 현재 revision 반환.
- 최초 시작 조건: 수긍/보관/이미 실행한 업무의 START 차단.
- 실패·회신 대기는 Running, 카드 저장 후 Complete 경계.
- 카드 버전과 입력 버전을 별도 비교하고 최신 VALID 구매안만 허용하는 선택 검증.

`app/core/idempotency.py`는 사용자·method·path·key·본문 해시의 식별자 및 재사용 충돌 검증이다. **지속 저장·동시 선점·최초 응답 재생을 구현한 멱등성 서비스가 아니다.** 메모리 딕셔너리로 저장 기능을 흉내 내지 않는다.

두 모듈은 기존 HTTP 라우터에 연결하지 않았다. 따라서 현재 `/work-items`의 기존 동작이나 실패 시 COMPLETE 처리 문제를 해결했다고 표시하면 안 된다. `allowed_actions`도 모든 상태/초안의 자동 계산기가 아니라 DTO와 일부 요청 검증 기반이다. 새 기능은 실제 DB 계약과 연결한 뒤 API 테스트로 다시 검증해야 한다.

## DB 담당자에게 필요한 자료

1. 새 스키마의 migration SQL 또는 실제 배포 DDL: UUID `work_cases.id`, 메일 FK, 소유 범위, 상태·입력/카드 버전, 판단·보관 필드.
2. 사용자 변경의 원자적 저장 계약: revision 비교·상태 변경·workflow log·실행 job 접수가 한 트랜잭션이어야 한다. RPC 이름과 인자·반환값 또는 동등한 트랜잭션 인터페이스를 공유한다.
3. 멱등 요청의 지속 저장: `(user_id, method, path, key)` 고유성, 본문 hash, 처리 상태, 최초 HTTP 상태/응답, 동일 키 동시 요청의 단일 선점. 보존 기간 확정 전 자동 삭제 금지.
4. 카드/구매안 스냅샷과 견적 버전의 저장 위치 및 FK. 과거 카드 보존·최신성 검사·결과 조회·선택 저장을 원자적으로 보장한다.
5. 비동기 실행의 지속 job/lease/재시작 복구 및 실패 기록 계약. 202는 job 저장 성공 후에만 반환하며 FastAPI BackgroundTasks만으로 내구성을 대신하지 않는다.
6. RLS/소유 검증: service-role 사용 시에도 API와 RPC가 같은 사용자·메일함·업무 범위를 검증해야 한다.

위 자료는 채팅에 비밀키를 전달할 필요 없이 SQL 파일로 공유하면 된다. 실제 DB가 새 스키마인지 확인되기 전 구 RPC를 새 이름으로 임의 변경하지 않는다.

## 미완료

- 새 DB 어댑터와 `/work-cases` API 전환, 목록/상세/진행 조회.
- 지속 멱등성·revision 원자적 갱신·202 실행·결과 저장·재시작 복구.
- 조건/후보/견적/카드/판단/초안/설정/접수/원문/거래처/로그 API 전체.
- Gmail 송신 및 UNKNOWN 대조, 다섯 역할 오케스트레이션과 최신 인수 기준.

## 검토·검증

공통 오류의 OpenAPI 정의도 실제 ErrorEnvelope와 일치하도록 지정하고, 405 응답은 Allow 헤더를 보존한다. 테스트는 실제 외부 자격증명 없이 실행한다. 캐시/병렬 요청 ID·OpenAPI 오류 계약·405 헤더와 신규 도메인 규칙을 포함하며 실제 Supabase·Gmail·LLM 통합 인수를 대체하지 않는다.

최종 로컬 검증: `python -m pytest -q -p no:cacheprovider` — **224 passed**, Starlette/httpx 사용 중단 예정 경고 1건. `python -m pip check` — 의존성 충돌 없음. OpenAPI는 현재 `create_app`에서 다시 생성했다.

변경된 JSON 응답의 필드는 `data` 안으로 이동하므로 프론트 소비 코드를 함께 맞춰야 한다. 기존 동기 분석의 502 결과 본문은 이전 분석 DTO를 `data`로 감싼 형태로 유지하며 새 비동기 API 전환 때 별도로 정리한다.

이 문서의 코드 검토 범위에서 실제 메일 발송·DB migration은 실행하지 않는다. GitHub PR과 병합 이력은 저장소에서 확인한다.
