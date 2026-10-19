# API 계약

기본 주소: `/api/v1`. 보호 API는 `Authorization: Bearer <access_token>`을 사용합니다.
프론트엔드는 서버 전용 Supabase 키를 받지 않습니다.

| 메서드 | 경로 | 권한 / 용도 |
|---|---|---|
| POST | /auth/login | 이메일·비밀번호 로그인 |
| POST | /auth/refresh | refresh_token으로 갱신 |
| POST | /auth/logout | 로그인 사용자의 현재 세션 로그아웃 |
| GET | /auth/me | 현재 사용자 ID·검토자 여부 |
| POST | /work-items | 업무 생성 |
| GET | /work-items | status 필터, limit 1~100, offset 페이지 조회 |
| GET | /work-items/{work_id} | 업무·추출값·결정 상세 |
| POST | /work-items/{work_id}/reject | 검토자만 거절, reason 필수 |
| PUT | /work-items/{work_id}/request-fields | 품목당 19필드 원자적 교체 |
| POST | /work-items/{work_id}/files | multipart file 업로드 |
| GET | /work-items/{work_id}/files | 파일 목록·페이지 조회 |
| POST | /work-items/{work_id}/files/{file_id}/signed-url | expires_in 30~900초 |
| POST | /work-items/{work_id}/analyze | file_ids 1~10개 분석·검증 후 저장 |
| GET | /work-items/{work_id}/review | 품목별 검토 카드, offset·limit |
| POST | /work-items/{work_id}/decisions | 검토자만 품목별 최종 결정 |
| GET | /reference-data/{table} | 기준자료 6개 중 하나 읽기 전용 조회 |

업무 생성 예시:

```json
{"classification_reason": "공급사 가격 변경 요청"}
```

파일 업로드의 반환값에는 UUID 기반 `file_id`가 있습니다. 분석 요청은 그 ID만 지정합니다. 다른 업무의 경로나 임의 URL은 입력할 수 없습니다.

```json
{"file_ids": ["0123456789abcdef0123456789abcdef.txt"]}
```

분석할 TXT/이메일/PDF 텍스트의 명시적 라벨 예시입니다. 이 파일은 합성 예시이며 기준자료 seed가 아닙니다.

```text
거래처: 공급사 이름
통화: KRW
[품목 1]
품목명: 품목 이름
규격: 1 kg
가공: raw
기존 단가: 100
변경 단가: 105
단위: kg
적용일: 2026-11-01
요청 수량: 10
요청 납기: 2026-11-15
변경 사유: 공급사 통지
```

필드 이름은 다음 19개입니다. 수동 저장은 각 품목행에 모든 필드를 정확히 한 번씩 포함해야 합니다. 값이 없으면 raw_value를 null로 지정하고 원문 확인 위치를 source_location에 기록합니다.

```text
supplier_name, item_name, specification, origin, grade, processing,
old_unit_price, new_unit_price, unit, currency, effective_date, reason,
requested_quantity, requested_delivery_date, transport_amount,
other_logistics_amount, cost_basis, cost_payer, cost_included_in_price
```

수동 저장 요청 구조:

```json
{"rows": [{"line_no": 1, "field_name": "item_name", "raw_value": "품목 이름", "source_location": "manual:담당자 원문 확인 위치"}]}
```

위 예시는 구조 설명이며 1필드만 있으므로 그대로 호출하면 422입니다. 실제 요청에는 19필드를 모두 포함합니다. `line_no`는 1~1000의 정수입니다.

최종 결정 예시:

```json
{
  "line_no": 1,
  "reference_id": null,
  "summary": "검토 결과 기존 거래 유지",
  "comparison_confirmed": true,
  "comparison_note": "원문과 규격, 단위, 통화 및 비용 조건을 직접 확인했습니다."
}
```

`reference_id=null`은 대안 견적을 선택하지 않는 결정입니다. 견적 ID를 지정하면 서버가 실제 QUOTE인지, 품목명·규격·가공상태, 알려진 원산지·등급, 적용일과 유효기간, MOQ 및 확인 가능한 납기를 검사합니다. unit/currency는 기준자료에 컬럼이 없으므로 사람의 확인이 필요합니다. 메모는 변경 불가한 DB summary에 검토자 ID·한국 시각과 함께 보관합니다.

JSON 오류 형식:

```json
{"error": {"code": "work_closed", "message": "종료된 업무는 변경할 수 없습니다."}}
```

- 401: 토큰 누락·실패
- 403: 팀 또는 검토자 권한 없음
- 404: 업무·파일·가격 근거 없음
- 409: 업무 상태/비교/DB 제약조건 충돌
- 413/415/422: 크기·형식·추출값 검증 실패
- 429: 로그인 시도 제한
- 502/503: 외부 응답 오류·서버 설정/연결 필요

검토 카드는 후보 범위를 명시하며 자동 결정을 하지 않습니다. 품목 후보는 행당 5개, 견적은 품목당 최근 10개까지 표시합니다. `landed_unit_price=null`이면 issues를 먼저 해결해야 하며 임의로 0원으로 처리하면 안 됩니다. 비용 숫자는 문자열로 반환하므로 프론트엔드에서 금액 계산이 필요하면 십진수 라이브러리를 사용하세요.

로그아웃 후 이미 발급된 access token은 만료 전까지 유효할 수 있습니다. 프론트엔드는 토큰을 즉시 버리고, 긴급 접근 차단은 서버 허용 목록에서 해당 사용자를 제거한 뒤 서버에 반영합니다. 멀티 테넌트·행별 사용자 소유권은 현재 스키마에서 지원하지 않습니다.

