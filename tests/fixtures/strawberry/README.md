# 냉동딸기 E2E 합성 데이터 (①·② 실제 테스트용)

시나리오별 폴더 `V1`(정상 kg 단가)·`V2`(박스 단가)·`V3`(첨부에만 값). 각 폴더에 공문 PDF, `email.md`(메일 본문·기대 결과),
`seed.sql`/`cleanup.sql`(owner `f412a4ec-…`), `seed_sesackt.sql`/`cleanup_sesackt.sql`(owner sesackt `bee2b4e4-…`)가 있다.
모든 행은 `evidence_jsonb.synthetic = SCN-STR-V{n}`로 표시되고, seed는 `on conflict (id) do nothing`이라 다시 실행해도 된다.

## 절차 (시나리오 하나씩)

1. `cleanup.sql` 실행 — 이전 적재분을 지운다.
2. `seed.sql` 실행.
3. 테스트 메일 발송 — `email.md`의 제목·본문·첨부(같은 폴더 PDF) 그대로. **수신(연결) 계정과 다른 발신 계정**에서 보낸다.
4. 동기화 확인 — 업무 큐에 Gmail 인입으로 들어왔는지 본다.
5. 업무 큐에서 실행.
6. `email.md`의 "기대 결과"와 ①·② 결과를 대조한다.
7. `cleanup.sql` 실행.

한 번에 한 시나리오만 적재한다. 같은 owner 아래 다른 시나리오의 딸기 품목(`frozen_strawberry`)이 있으면 ② 후보가 섞인다.

## 예외 시나리오 (`EX/`)

V1 시드를 그대로 쓰고 메일 본문 한 곳만 바꾼다. 공문 PDF 생성 스크립트가 저장소에 없어 첨부 없이 본문만 보낸다.
절차는 위와 같고, 시드·정리는 `V1/seed.sql`·`V1/cleanup.sql`(sesackt면 `_sesackt`)을 쓴다.

| 파일 | 바꾼 곳 | 추가 시드 | 기대 결과 (①) |
|---|---|---|---|
| `EX/E1_item_missing.md` | 제목·본문에서 품목명 삭제 | 없음 | HOLD `MISSING_FIELD`·`item_name` (`NO_QUOTE_DATA` 가능성, 첫 실행으로 확정) |
| `EX/E2_supplier_mismatch.md` | 회사명을 등록 업체 솔빛푸드로 | `EX/E2_link_sender.sql` (한결 contact_email = 발신 주소) | HOLD `SUPPLIER_MISMATCH` |
| `EX/E3_supplier_unregistered.md` | 회사명을 시드에 없는 새봄냉동으로 | 없음 | HOLD `SUPPLIER_NOT_MATCHED` |

V1 자체가 공급 물량·납기 없는 인상 통보라, 수량·납기 누락은 별도 파일 없이 V1 결과로 확인한다
(현재 코드는 수량 없으면 HOLD `MISSING_FIELD`·`quantity`).

## 결정 대기

- 적재 owner·권한: 어느 계정 owner로 적재할지(`seed.sql` / `seed_sesackt.sql`), 누가 실행하는지.
- cleanup의 `internal.*` 삭제: 실행 결과로 생긴 내부 테이블 행 정리. 권한이 정해질 때까지 cleanup은 시드 테이블만 지운다.
- company_items 추가: V2 가온냉동 HACCP 제외를 확인하려면 업무 필수조건이 필요하다(본혁 허용 대기).
- 업체 연결 기준: 발신 주소인지 업체명인지. 정해질 때까지 시드 `contact_email`은 합성 주소 그대로 둔다.
- 수량 출처: 공문에 공급 물량이 없을 때 업무 비교 수량을 어디서 가져올지. 현재 코드는 ① HOLD `MISSING_FIELD`·`quantity`.
