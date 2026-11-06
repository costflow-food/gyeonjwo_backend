# 냉동딸기 E2 업체 불일치 테스트 메일

- 시드: `../V1/seed.sql`(또는 `seed_sesackt.sql`) 실행 후 `E2_link_sender.sql` 실행. 정리는 `../V1/cleanup.sql`(행째 지운다).
- `E2_link_sender.sql`은 한결냉동식품의 contact_email을 실제 발신 주소로 바꿔 메일이 한결냉동식품에 연결되게 한다.
  서버는 발신 주소와 contact_email이 정확히 같을 때만 연결한다(`agent_store.py` `mails`).
- 발신: hongmo9212@gmail.com (연결 업체: 한결냉동식품)
- 발송 기준일(시나리오): 2026-10-16
- 제목: `[솔빛푸드] 냉동딸기 공급단가 조정 안내 (2026.11.01 적용)`
- 첨부: 없음 (V1 본문에서 회사명만 등록 업체 솔빛푸드로 바꿈)

## 본문

```
박 대리님, 안녕하세요. 솔빛푸드 영업팀 최서윤입니다.

국산 딸기 작황 부진과 인건비·냉동 보관비 상승으로
냉동딸기 공급단가를 아래와 같이 조정하게 되었습니다.

 - 냉동딸기(IQF) 특품 10kg/박스: 5,000원/kg → 5,500원/kg
 - 적용일: 2026년 11월 1일 납품분부터
 - 운송비: 기존과 같이 단가 포함, 별도 물류비 없음

감사합니다.

최서윤 드림
솔빛푸드 영업팀 | 041-555-0177
```

## 기대 결과

- ① 정보 추출
  - store_structured_mail이 `SUPPLIER_MISMATCH` 반환(연결 업체 한결냉동식품 ≠ 원문 업체 솔빛푸드)
  - next_action: HOLD, hold `SUPPLIER_MISMATCH`·field null
  - reason: "메일에 연결된 거래처와 원문의 업체명이 달라 견적을 저장하지 않았습니다."
  - quote_version_ids 비움
- ② 후보 추천: 견적이 없으므로 진행하지 않는다
- 확인 필요: Gmail 인입 시 mail_messages.sender가 주소만 저장되는지. `이름 <주소>` 형태면 연결이 안 되어 `SUPPLIER_MISMATCH`가 아니라 저장(솔빛푸드 견적)으로 끝난다.
