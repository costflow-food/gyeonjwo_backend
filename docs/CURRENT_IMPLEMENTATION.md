# 견줘 MVP1 백엔드 현재 구현

> **2026-10-28 주의:** 이 문서는 10월 26일 구 기획 기반 구현의 기록입니다. 최신 요구사항은 [PROJECT_BASELINE_2026-10-28.md](PROJECT_BASELINE_2026-10-28.md), 실제 미구현/전환 범위는 [IMPLEMENTATION_GAP_2026-10-28.md](IMPLEMENTATION_GAP_2026-10-28.md)를 보세요. 아래의 ‘최신 06/08’은 작성 당시 기준이며 현재 노션과 다릅니다. 구 165 테스트는 새 AC-01~23의 완료 증거가 아닙니다.

2026-10-26. **Python 코드 구현과 모의 외부 의존성 기반 테스트를 완료했다. 실제 팀 Supabase·Gmail·LLM 및 Flutter 통합 검증은 접속 설정이 없어 아직 수행하지 않았다.**

후속 전체 재점검에서 발견한 오류를 수정했다. 현재 테스트 **165개 통과**. 상세 수정 내용과 미검증 항목은 [AUDIT_RESULTS.md](AUDIT_RESULTS.md)를 따른다.

## 구현 범위

| 영역 | 구현 |
|---|---|
| HTTP | 노션 08 업무 API 7개, /health, /ready, 최신 06 notes PATCH |
| 보안 | Supabase Auth 사용자 검증·사용자 문맥 check_app_access, Pub/Sub OIDC 서명/발급자/audience/호출 계정 검증, CORS 허용 목록, 단일 프로세스 요청 제한, no-store·파일 다운로드 보호 |
| DB | 기준자료 7개 조회·페이지네이션·Decimal, 서버 RPC 19개, 직접 DML 없음 |
| 수신 | 알림을 DB 영속 접수한 뒤 204. 워커가 history 모든 페이지 조회, 커서 만료 시 전체 동기화, 메일 중복 방지, 원문 업로드/해시 검증 후 원자적 등록 |
| 운영 | Gmail watch 매일 갱신, 5분 간격 누락 보완 알림, claim/lease/heartbeat·실패 backoff·분류 재개·만료 분석 복구 |
| 분석 | 실행 뒤 PDF/20필드 추출·원문 대조·표준화·단일 거래처/품목 검증·구매 근거/보유 후보·계약/물량 검증·비용 계산·독립 검증·출력 검증 |
| 계산 | 모델은 제한된 키로 곱셈/합산 산식을 제안, 코드가 Decimal 연산. 별도 문맥 3회(최소 2회) 및 독립 검증 모두 통과해야 카드 표시. eval 없음 |
| 결과 | AFTER 1개·CANDIDATE 최대 2개. 미확인 비용 총액·차액 null. 서비스 결과·담당자 결정 영구 저장 없음 |
| 원문 | 업무별 첨부 메타데이터 허용 목록, EML/PDF/텍스트 제공, SHA-256·크기·안전 경로 확인 |

각 역할의 모델 호출은 새 요청이며 이전 계산 회차의 산식/답을 다음 회차에 넣지 않는다. 요청·계약·계산 검증 실패는 REVIEW_REQUIRED, 기술/출력 오류는 FAILED다. 종료 RPC 성공 전에는 COMPLETE를 반환하지 않는다. 종료 기록 실패 시 503과 오류만 반환한다.

## HTTP 계약

모든 업무 API는 Authorization: Bearer {Supabase Auth access token}이 필요하다. 웹훅은 Google OIDC 토큰을 사용한다.

| Method | 경로 | 입력/동작 |
|---|---|---|
| POST | /api/v1/webhooks/gmail | Pub/Sub envelope, 영속 접수 후 204 |
| GET | /api/v1/work-items/{work_id} | 업무·EML 원문 헤더. 서버 토큰 열 제외 |
| POST | /api/v1/work-items/{work_id}/reclassify | {}. 최신 DB에서 COMPLETE 재분류 금지 |
| POST | /api/v1/work-items/{work_id}/analyze | {}. 동기 실행 결과, 십진수 문자열 |
| POST | /api/v1/work-items/{work_id}/skip | {}. PENDING/TRUE/FALSE만 FALSE로 변경 |
| GET | /api/v1/work-items/{work_id}/files | 원문·첨부 목록 |
| GET | /api/v1/work-items/{work_id}/files/{filename} | 메타데이터에 속한 파일만 다운로드 |
| GET | /api/v1/work-items/{work_id}/reference-files | 업무 품목에 연결된 기준자료 원문 목록·file_id |
| GET | /api/v1/work-items/{work_id}/reference-files/{file_id} | 인증된 기준자료 원문 다운로드 |
| PATCH | /api/v1/work-items/{work_id}/notes | {"notes":"업무 비고"}, null로 삭제 |
| GET | /health | 서버 생존 |
| GET | /ready | 설정·DB 읽기·워커 실행 확인, 미준비 503 |

OpenAPI: [openapi.json](openapi.json), 실행 시 /docs. notes와 reference-files는 최신 DB 및 원문 대조 흐름에 맞춰 추가한 API 확장이다. 기준자료 file_id는 source_ref의 SHA-256이며 서버는 DB에서 허용 목록을 다시 확인한다. 결과 복원 GET /results, 승인·발주·카드 선택 저장 API는 없다. Flutter 소스는 이번 작업에서 변경하지 않았다.

## 이번 구현에서 확정한 보수적 처리

- 최신 06 기준으로 queue_status=PENDING/TRUE/FALSE/RUNNING/COMPLETE를 사용. 구 08의 is_work_mail·WAITING 컬럼은 쓰지 않음.
- 단일 서버 프로세스, 상세 분석은 한 번에 한 건. 프로세스 수를 늘리면 메모리 결과 공유·라우팅을 별도 설계해야 함.
- 검토 기간은 적용 요청일이 속한 달의 전체 구매계획. 월정액의 기간/물량이 정확히 맞지 않으면 자동 배분하지 않고 중단. 팀의 다른 기간 규칙이 확정되면 이 계약을 변경할 것.
- 금액은 Decimal, 0.01원 단위 ROUND_HALF_UP, 회차별 항목 합계 기준. DB numeric(14,2)와 맞췄으며 팀이 원 단위 반올림을 택하면 규칙·테스트를 함께 변경할 것.
- 요청 비용은 명시된 값·조건 또는 ‘기존과 동일’이라는 명시적 근거가 있어야 함. 없는 비용은 0으로 만들지 않음.
- 거래처 전환 후 남는 기존 월정액을 임의 제거하지 않음. 잔여 부담이 확인되지 않으면 후보 총액 미확정.
- 텍스트 PDF·텍스트/HTML 첨부 지원. PDF 파싱은 별도 제한 시간 프로세스에서 최초 1회+재시도 최대 3회. 이미지 PDF는 OCR_REQUIRED, 지원하지 않는 첨부는 검토 필요. OCR·문서 해석 정확도 95% 달성은 아직 검증한 사실이 아님.
- LLM 제공자·모델은 노션 07 10번에서 미확정. Chat Completions JSON Schema 호환 어댑터를 환경설정으로 연결. 팀의 실제 제공자에서 json_schema·store 옵션 지원 여부를 실검증해야 함.
- 기본 진단 로그는 단계·모델·토큰 사용량·업무 ID·결과 메타데이터. 비밀 키·메일 본문·카드 전체를 기록하지 않음. 노션 05/06/08의 로그 범위 차이는 별도 정책 확정 대상.

## 팀원 인계·실연동 순서

1. DB 팀원이 기준 SQL 배포: 13개 테이블·101개 컬럼, RPC 19개, 버킷 2개, GRANT/RLS·Auth app_members·private Broadcast. 서버 코드가 DB를 자동 생성하지 않음.
2. 기존 .env를 덮어쓰지 말고 .env.example의 누락 설정을 추가. Supabase URL/서버키/publishable키, 팀 승인 LLM URL/모델/키 설정.
3. Google Cloud에서 Gmail API, 승인된 읽기 OAuth, Pub/Sub topic/subscription·OIDC 호출 계정/audience를 준비. Gmail push 발행 권한과 HTTPS 도메인은 GCP 담당자가 설정. 서버 코드가 IAM 권한·OAuth 동의 화면을 자동 변경하지 않음.
4. 시연용 기준자료와 실제 원문 파일을 비공개 버킷에 올리고 테스트 Auth 사용자를 활성 멤버로 등록.
5. `python -m scripts.check_connection`으로 DB 열·버킷 읽기 점검. 사용자 로그인·허용/차단·원문 API 검사.
6. WORKER_ENABLED=true로 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1` 실행. HTTPS 프록시의 분석 요청 제한 시간은 ANALYSIS_TIMEOUT_SECONDS보다 길게 설정. 실제 Pub/Sub 호출 URL과 audience를 일치시킴.
7. 새 메일·중복 알림·history 만료·서버 중단 복구, 정상/누락/비용 미확인/계산 불일치/종료 기록 실패를 실제 서비스에서 통합 검증.
8. Flutter는 최신 허용 큐 열 SELECT와 private gyeonjwo:queue/queue_changed 후 재조회. analyze 응답은 앱 메모리에 보관하고 결과 복원 API를 가정하지 않음. CORS_ORIGINS에 실제 web origin을 지정.

키는 채팅·Git에 넣지 않는다. 현재 .env에는 실키를 채우지 않았고, 외부 서비스에 실제 메일·원문을 전송하는 검증은 수행하지 않았다. 작성 코드는 로컬 feature/supabase-foundation 브랜치에 있고 커밋·푸시는 아직 하지 않았다.

## 검증과 참고 문서

자동 테스트는 Supabase/Gmail/모델 응답을 대체한 상태에서 API 인증, 수신 영속화 경계, 페이지네이션·중복·커서 복구, 분석 중단/완료, 예시 400kg 고정+600kg 변경의 300,000원 영향액, 미확인 후보 총액 null, 회차/출력 검증을 확인한다. 실제 계정 권한·모델 응답 정확도·운영 부하 테스트의 대체물이 아니다.

- [노션 최신 스키마](https://app.notion.com/p/06-3ff29a34f85780e1b9f0e4ab016b693c)
- [노션 API](https://app.notion.com/p/08-API-40029a34f85780398e5bde82da656521)
- [노션 에이전트](https://app.notion.com/p/07-3fa29a34f8578013a237e56197dda902)
- [Gmail Push 공식 문서](https://developers.google.com/workspace/gmail/api/guides/push)
- [Gmail 동기화 공식 문서](https://developers.google.com/workspace/gmail/api/guides/sync)
- [Pub/Sub OIDC 공식 문서](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions)
- [구조화된 모델 출력 공식 문서](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)
