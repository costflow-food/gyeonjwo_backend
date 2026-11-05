# MVP1 메일 발송·회신·검토 통합 검증

2026-11-05 소스 확인 기준은 BE `dev_1.0` `210ebfa`다. 11월 4일 구현 현황의 BE `a027e5b`, FE `fb84449`는 비교 기준이다. 인수 판정은 03 MVP Must를 사용한다.

| 구분 | 현재 코드와 확인 결과 |
| --- | --- |
| 과거 문서의 기본 모드 | `graph=false`, `stop_after_extraction=true`에 해당하는 환경 설정은 현재 제거됐다. 앱 시작 시 독립 ExtractionWorker로 대체 실행하는 분기가 없다. |
| 현재 START | `app/main.py`에서 설정과 DB 준비 상태가 맞으면 GraphWorker를 시작한다. `graph_repository.py`의 운영 claim은 `p_start_from='START'`로 고정된다. 그래프 START는 ①②를 실행하고 후보 선택 대기로 이어진다. |
| 메일 워커 | Gmail 인입, RFQ 메타데이터 및 RfqWorkflowWorker가 각 준비 조건에 따라 시작한다. 그래프가 START를 실행해도 RFQ 준비·발송 워커 수명 주기는 별도다. |
| REPLY와 PLAN | GraphWorker에는 작업 형식 검사와 검토 완료 처리 분기가 있다. 기준 SHA의 claim은 START로 고정돼 있었다. 작업 브랜치는 DB health가 검토 기능을 확인한 경우에만 회신을 예약하고 검토 큐를 조회한다. 실제 회신 처리는 배포 후 확인한다. |
| 완료 판정 | 검토 그래프 COMPLETE는 저장이 확인된 카드 ID와 일치해야 하며, 완료 RPC가 STORED이면 같은 입력 revision, review revision 및 업무 Complete를 확인한다. FAILED·STALE 결과는 실패 처리로 분기한다. |

## 팀 서버 준비

- 서버 배포 SHA, 실행 인스턴스 수, FE SHA, DB 배포 버전과 환경, 테스트 시간대를 기록한다.
- DB에 `SUPABASE_URL`, `SUPABASE_SECRET_KEY`를 설정한다. 브라우저 연결에는 publishable key를 사용한다.
- `LLM_BASE_URL`, `LLM_API_KEY`와 실제 실행 모델을 확인한다. 모델과 시간 제한은 `app/core/config.py`에서 확인한다.
- `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GOOGLE_TOKEN_ENCRYPTION_KEY`를 설정하고 테스트 메일함의 연결 상태와 Gmail 발송 권한을 확인한다. Pub/Sub는 선택이며 미설정 시 폴링을 확인한다.
- 실행 플래그 기본 false는 과거 문서 설정이다. 현재 시작 조건은 설정과 DB 준비 상태이며, 발송의 기본 차단은 메일함 설정 미설정으로 확인한다. 승인 ON은 승인 필요, OFF는 자동 발송을 뜻한다. 테스트 주소와 발송 시점은 KAN-132 합의값을 사용한다.
- 공유 dev DB에는 팀 서버 하나를 연결한다. 중복 워커와 lease 만료 실험은 격리 DB에서 한다.

## 배포 후 실행 순서

1. KAN-133 DB 배포와 KAN-150 회신 예약·claim 연결 버전을 확인한다. KAN-94의 그래프 범위와 KAN-132 수신 주소·시점을 확인한다. 서버 재시작 후 `/ready`에서 메일 수집과 검토 준비 상태를 따로 기록한다.
2. 합의된 메일을 테스트 메일함에 수신한다. 업무 시작과 실제 ①② 모델 실행, 후보 세트 저장, 사용자 선택 전 발송 0건을 확인한다.
3. 후보 선택 후 승인 ON에서 명시 승인 전 0건·승인 후 1건을 확인한다. 승인 OFF에서는 발송 조건 충족 시 1건을 확인한다. Gmail 보낸편지함과 Supabase draft·outbound 상태를 대조한다.
4. 합의된 회신을 보내 대상 업무와 연결된 REPLY 실행, ③④⑤ 모델 결과 및 현재 revision을 확인한다. KAN-132가 허용한 분기에서 RFQ·CLARIFICATION·COUNTER_OFFER 각각의 초안, 정책 판정, 발송과 Gmail 결과를 기록한다. 보완질문이 있으면 답변이 들어온 다음 검토를 이어간다. 역제안은 실제 생성 조건을 충족하는 사례로 검증한다.
5. 대조할 수 있는 발송 응답 유실 사례에서 UNKNOWN으로 전환되는지 확인한다. 같은 요청을 재실행해 Gmail 중복 발송이 없는지 확인한다. 공급자 증거가 일치할 때만 SENT로 대조하며, 증거가 없으면 UNKNOWN을 유지한다.
6. 격리 DB에서 동일 작업 동시 claim, lease 갱신·만료·복구, 입력 revision 변경, 모델 실패를 검증한다. 만료 토큰의 뒤늦은 결과와 STALE·실패가 최신 카드나 Complete를 만들지 않는지 확인한다.
7. 현재 입력 revision의 카드 저장과 업무 Complete를 API 및 Supabase에서 대조한다. KAN-117~119의 기존 완료 결과와 비교하고 실제 차이가 확인된 경우에만 근거를 남긴다.

## 실행 증거

각 시나리오에 BE/FE SHA, DB 버전, 실행 모드, case ID, batch/revision, draft 목적과 상태, 요청 ID, Gmail 공급자 ID와 발송 건수, LLM 역할·모델·결과 코드, Supabase 상태·카드 ID를 남긴다. 비밀값과 메일 본문은 증거에서 제외한다.

현재 상태는 소스 대조 완료다. 이 작업 환경에서 점검 스크립트는 Python 의존성 누락으로 시작되지 않았다. KAN-133 배포 증거, KAN-150 실행 버전과 KAN-132 승인 시나리오를 확보한 뒤 팀 서버에서 실계정 검증을 진행해야 한다. Gmail·LLM·Supabase 실호출 결과는 미검증이다.
