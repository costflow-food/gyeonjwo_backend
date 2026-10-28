# 견줘 Python / Supabase 구현 기준

> 이 문서는 연결 기반 작성 당시 기록입니다. 이후 전체 MVP1 서버 구현 상태는 [CURRENT_IMPLEMENTATION.md](CURRENT_IMPLEMENTATION.md)를 확인하세요.

기준: [노션 06 데이터 스키마·저장 방식](https://app.notion.com/p/06-3ff29a34f85780e1b9f0e4ab016b693c), v2.2, 2026-10-26.

## 저장 경계

- public: suppliers(5), items(5), contract_headers(1), contracts(10), purchase_plans(5), price_references(16), cost_terms(12), work_items(14).
- internal: work_item_sources(7), gmail_mailboxes(9), gmail_notifications(12), daily_work_counters(2), app_members(3).
- 합계 13개 테이블 / 101개 컬럼. 기준자료 7개 / 54개 컬럼은 런타임 읽기 전용. 같은 v2.2 문서의 10월 26일 갱신에서 notes와 update_work_item_notes RPC가 추가됨.
- 본문/첨부 메타데이터는 원문 정보 RPC. 실제 바이트는 비공개 gyeonjwo-files 및 gyeonjwo-reference-files.
- 추출값, 검토 근거, 계산 결과, 검토카드는 실행 메모리에만 존재. 담당자의 최종 결정은 수집하지 않음.
- 사용자 입력 업무 비고 notes는 예외적으로 DB 보관. 카드·계산 결과·최종 결정을 자동 복사하지 않음.
- queue_status: 문자열 PENDING / TRUE / FALSE / RUNNING / COMPLETE. COMPLETE는 성공 여부가 아니라 분석 종료.

## 이번에 구현한 범위

1. FastAPI 서버의 liveness 경로 `/health`(기존 `/api/v1/health`는 호환 경로).
2. 서버용 Supabase 클라이언트와 환경변수 설정. 사용자 JWT 클라이언트와 서버 키를 공유하지 않음.
3. 사용자 JWT를 Auth API로 검증하고 사용자 문맥에서 check_app_access 호출.
4. 기준자료 7개 테이블의 명시적 컬럼 조회, 페이지네이션, Decimal 변환, 단일 거래처·품목 대조.
5. 명세 6.5의 서버 RPC 19개를 허용 목록과 정확한 인자로 호출하는 내부 어댑터. 조건부 변경 RPC false 응답은 소유권 상실 예외, notes RPC false는 업무 없음 예외. boolean 응답이 null/문자열이면 계약 오류로 중단.
6. 원문 EML 업로드 및 다운로드 후 크기·SHA-256 검증. 안전한 work_id/첨부 경로 생성.
7. DB 컬럼 조회와 비공개 버킷 확인용 읽기 전용 연결 점검 스크립트.

어댑터의 존재가 Gmail 워커나 분석 파이프라인의 완성을 의미하지 않는다. public/internal 객체·RLS·RPC·버킷 생성 SQL은 실행하지 않았으며 실제 DB도 아직 연결하지 않았다. 노션에 언급된 SQL 검사(이번 확인 시 107개)를 이 프로젝트에서 실행했다고 주장하지 않는다.

## 사용 예

```python
from app.db.client import get_server_client
from app.db.contracts import RpcRepository
from app.db.references import ReferenceRepository

client = get_server_client()
references = ReferenceRepository(client)
contracts = references.read('contracts', supplier_id='SUP-001', item_id='ITM-001')
# 반드시 사용자 JWT/멤버 검증을 끝낸 오케스트레이터에서만 쓰기 RPC를 호출한다.
# result = RpcRepository(client).call('start_analysis', p_work_id='W-20261026-001')
```

RpcRepository는 외부에 공개하는 범용 HTTP RPC 프록시가 아니다. 상태 변경 시 사용자 권한 검증과 각 기능의 업무 검증이 선행되어야 한다. start_analysis jsonb의 세부 키는 배포된 SQL 응답 확인 후 서비스 모델로 고정한다. 내부 테이블은 Data API에 노출하지 않는다.

## 이어서 구현할 기능

- 배포된 SQL과 실제 Supabase 설정 대조, 실접속·역할 권한·Storage 바이트 검증.
- Gmail OAuth/PubSub OIDC 검증, 모든 history 페이지 처리, lease·heartbeat·backoff·watch 갱신 워커.
- 원문 첨부 메타데이터 허용 목록 매핑과 첨부 업로드, 미등록 업로드 정리. 임의 source_ref를 다운로드 경로로 전달하지 않음.
- PDF, 20개 필드 추출, 다품목 검증, 검토 근거 및 후보 최대 2개 비교.
- 월정액/기간/물량/잔여 비용 검증, Decimal 계산 3회(최소 2회) 및 독립 검증.
- 분석 HTTP 계약, 단일 프로세스 메모리 결과 전달, 종료 RPC와 heartbeat·만료 복구.
- Flutter Auth·허용 열 SELECT·private Broadcast 연결.

[현재 08 API 문서](https://app.notion.com/p/08-API-40029a34f85780398e5bde82da656521)는 10월 23일 편집본이며 7개 테이블/구 is_work_mail 및 WAITING을 기술한다. 10월 26일 스키마 v2.2의 통합 상태·RPC와 차이가 있어 구 컬럼으로 쓰기 엔드포인트를 임의 구현하지 않았다. HTTP 경로(/analyze, /reclassify, /skip, /files)는 이 문서에 맞춰 후속 구현하되 내부 상태는 최신 06 기준으로 매핑해야 한다. 로그 범위도 08의 운영 메타데이터 한정과 06의 요청/응답 진단에 차이가 있으므로 현재 코드는 본문·카드·비밀 키를 로깅하지 않는다. 06 본문 1.1에 남아 있는 private 표기는 1.3·ERD·본문 전체의 internal 표기와 불일치하므로 internal을 기준으로 삼았다.
