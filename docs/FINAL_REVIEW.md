# Python / Supabase 최종 대조 결과

> **이 문서는 전체 구현 전 점검 기록입니다.** 이후 사용자 요청으로 업무 API·Gmail·분석 파이프라인을 구현했습니다. 아래 미구현 표는 당시 상태이며 현재 상태는 [CURRENT_IMPLEMENTATION.md](CURRENT_IMPLEMENTATION.md)와 [openapi.json](openapi.json)을 따릅니다.

확인일: 2026-10-26. 결론: **DB 연결 기반은 작성했지만 서비스 API 전체는 미구현이다. DB URL과 키만 넣어서 전체 서비스가 완성되는 상태가 아니다.**

## 확인한 노션 문서

- [04 서비스 플로우](https://app.notion.com/p/04-3f929a34f8578099962bde9a5f91a0f4): 사용자 실행 후 상세 처리, 검증 실패 시 중단, 계산 n회·독립 검증.
- [05 아키텍처](https://app.notion.com/p/05-3fa29a34f8578016bb59c89f76b735ca): Python 실행 통제, Flutter 큐 조회, 원문 Storage, 메모리 결과 경계.
- [06 데이터 스키마·ERD](https://app.notion.com/p/06-3ff29a34f85780e1b9f0e4ab016b693c): v2.2의 현재 13개 테이블·101개 컬럼·서버 RPC 19개·비공개 버킷 2개.
- [07 에이전트 구성](https://app.notion.com/p/07-3fa29a34f8578013a237e56197dda902): 역할·도구 권한, 상세 처리 전 사용자 실행, 별도 문맥 계산, 원문 검증·카드 출력 검증.
- [08 API 명세](https://app.notion.com/p/08-API-40029a34f85780398e5bde82da656521): 업무 API 7개, /health·/ready, 동기 analyze, 오류·파일 응답 계약.

기준 SQL: `01_gyeonjwo_mvp1_setup.sql`. 이번에 06 문서에서 읽은 SHA-256: `e510c9a2bc333644398be2c5469f83490caec4391ceda26dc4d000114a7eaf7d`. 이 값은 문서가 제시한 값이며 실제 SQL 파일의 해시를 여기서 직접 계산한 것은 아니다. 같은 v2.2 이름으로 내용이 갱신되었으므로 이름만으로 배포 버전을 판별하지 않는다.

## 우선 수정·구현해야 할 사항

| 우선순위 | 항목 | 현재 판단 |
|---|---|---|
| P1 | 실제 업무 HTTP API | 아래 7개 모두 라우트/서비스가 없음. RPC 호출 어댑터는 HTTP API 구현과 다름 |
| P1
| Gmail 영속 수신/워커 | OIDC 검증, 알림 DB 접수 후 204, history 전체 페이지·중복·재시도·watch 갱신 미구현 |
| P1 | 분석 및 검증 파이프라인 | PDF·20필드 추출·검증·근거/후보·계약·비용·독립 계산/검증·카드 없음 |
| P1 | 실행 소유권/종료 제어 | 실제 start/heartbeat/complete/만료 복구를 호출하는 워커 없음. 어댑터의 토큰 인자 지원만 구현 |
| P1 | 파일 제공·인증 연동 | 인증 함수는 있지만 업무 라우트에 연결되지 않음. 업무별 첨부 허용 목록과 기준자료 원문 제공 서비스 없음 |
| P1 | 실DB 통합 검증 | 접속 설정·배포 SQL·Auth·Storage·Broadcast 검증 전. 단위 테스트 성공과 구분 |
| P2 | 명세 간 상태/저장 구조 차이 | 04·05·07·08에 과거 상태/중복 접수/기준자료 개수 표현이 남음. 실제 SQL 담당자와 최신 06 기준 계약 고정 필요 |
| P2 | 준비 상태·공통 응답 | /ready, 공통 오류, Cache-Control:no-store, 시간 제한·CORS 설정 미구현 |

## HTTP 경로별 상태

| Method | 경로 | 상태 |
|---|---|---|
| POST | /api/v1/webhooks/gmail | 미구현 |
| GET | /api/v1/work-items/{work_id} | 미구현 |
| POST | /api/v1/work-items/{work_id}/reclassify | 미구현 |
| POST | /api/v1/work-items/{work_id}/analyze | 미구현 |
| POST | /api/v1/work-items/{work_id}/skip | 미구현 |
| GET | /api/v1/work-items/{work_id}/files | 미구현 |
| GET | /api/v1/work-items/{work_id}/files/{filename} | 미구현 |
| GET | /health | 구현·단위 검증. 서버 생존 확인만 수행 |
| GET | /ready | 미구현 |

notes의 Python RPC 어댑터는 반영했다. notes 편집용 HTTP 경로·요청·응답·오류 계약은 현재 08 목록에 없으므로 추가 명세가 필요하다. 기존 7개가 구현됐다는 뜻이 아니다.

## 문서에서 맞춰야 할 차이

1. **스키마/열:** 08의 7개 테이블·59개 컬럼·기준자료 6개 → 최신 06은 13개·101개·기준자료 7개. public.work_items에는 notes 포함 14개 열이 있다. Flutter SELECT 허용 열을 배포 SQL과 동일하게 고정하고 `*`를 사용하지 않는다.
2. **상태:** 08의 is_work_mail + WAITING은 최신 06 DB 구조와 다르다. 현재 DB는 queue_status=PENDING/TRUE/FALSE/RUNNING/COMPLETE 하나로 통합된다. 응답 outcome=SUCCESS/REVIEW_REQUIRED/FAILED는 영구 DB 상태로 추가하지 않는다.
3. **재분류·제외:** 08은 기존 classification_reason 유지 등을 설명하지만 06의 restart/exclude RPC는 분류 토큰과 사유를 바꾼다. COMPLETE 재분류는 06에서 미지원이다. 단순 컬럼명 교체만으로 해결되지 않는다.
4. **메일 중복/접수 시점:** 04·07은 같은 식별자도 새 업무, 분류 후 접수로 서술한다. 최신 06은 `(mailbox_id,gmail_message_id)` 중복 방지, 원문 업로드 후 PENDING 등록, 토큰 기반 분류 완료다. Pub/Sub 중복과 Gmail 메일 중복은 별개다.
5. **진행/결과 통신:** 05는 진행·결과 조회를 표현하나 08은 동기 analyze 응답 1회와 결과 복원 API 없음으로 정의한다. Realtime은 private `gyeonjwo:queue`의 `queue_changed` 메타데이터와 큐 재조회용이다. 카드 전달 통로로 사용하지 않는다.
6. **로그:** 05·06의 전체 요청/응답 진단과 08의 메타데이터 한정이 충돌한다. 현재 코드는 본문·카드를 로그에 기록하지 않는다. 팀의 보존·마스킹 정책 확정이 필요하다.
7. **비고:** 최신 06은 사용자 업무 비고(notes) 저장을 허용한다. 자동 카드/계산 결과·최종 결정 저장과 구별한다. 최대 8,192자, 공백뿐이면 NULL, 분석 상태와 별개로 수정된다.
8. **스키마 이름:** 06 1.1의 일부 private 표기는 1.3·ERD·RPC 설명의 internal과 다르다. 실제 배포 SQL과 맞춰 고친다.

노션 원문은 직접 수정하지 않았다. 위 차이를 로컬 구현 검토 문서에 기록했다.

## 이번 검토에서 고친 코드

- notes RPC를 추가해 서버 RPC 계약을 19개로 맞춤. 문자열/NULL·8,192자 검증, false=업무 없음 처리.
- 연결 점검을 work_items 14개 열 전체 검사로 확장. notes가 없는 예전 DB를 연결 성공으로 오판하지 않도록 함.
- /health 경로를 08과 맞춤. /api/v1/health는 호환 경로로 유지.
- 조건부 RPC가 null/문자열/숫자를 반환하면 성공으로 간주하지 않고 계약 오류로 중단.
- UUID·시간대가 있는 datetime을 RPC JSON 형식으로 변환. 시간대 없는 시각은 전송 전 거부.
- 잘못된 Auth 응답 구조와 비문자열 사용자 ID를 거부.
- 업무 ID의 숫자를 ASCII로 제한. DB가 허용하는 0바이트 첨부 다운로드는 허용하되 빈 EML은 거부.

## 팀원과의 역할 분담·인계 항목

| 담당 | 수행 내용 |
|---|---|
| DB 담당 팀원 | 기준 SQL 버전 고정/배포, 테이블·domain·enum·제약·인덱스, internal RPC 구현과 public wrapper, GRANT/RLS, Auth app_members, 버킷·Storage 정책, private Broadcast, 기준자료/원문 준비 |
| Python 백엔드 | 사용자 JWT·멤버 확인, 실제 HTTP 계약, RPC 호출·소유권 제어, Gmail 수신/워커·원문 바이트 검증, 분석/검증/계산/카드, 안전한 파일 제공·진단 로그 |
| Flutter | Auth 로그인, 허용 열로 큐 직접 SELECT, private Broadcast 후 재조회, 사용자 실행 API 호출, 현재 결과의 휘발성 상태 유지, 상태 초기화 후 결과 복원 금지 |

DB 담당자에게 받을 것: 배포된 SQL 파일과 해시, Supabase 프로젝트 URL, 서버 키와 publishable 키의 로컬 설정 절차, 테스트 Auth 사용자·활성 멤버 등록 여부, 샘플 work_id/기준자료 및 실제 원문 파일. 비밀 키는 채팅·Git에 공유하지 않고 로컬 환경으로 설정한다.

연동 완료 기준: 읽기 전용 연결 점검 → 실제 로그인·허용/차단 검사 → 별도 테스트 데이터로 RPC 동시 실행·만료/늦은 응답·notes 검사 → 원문 Storage 바이트 검증 → Gmail 수신/중복·재시도 → 분석 성공/검토 필요/기술 실패/종료 기록 실패 → Flutter 통합. 테스트 대상 DB와 데이터를 확정한 뒤 쓰기 통합 검증을 진행한다.
