"""'현재 회신으로 진행'(PROCEED): 남은 회신을 기다리지 않고 지금까지 온 견적으로 구매안을 만든다.

DB submit_work_case_proceed가 최종 판단한다. 여기 문구는 DB가 돌려주는 거절 사유(reason)와
목록·상세에서 미리 끄는 사유에 같이 쓴다.
"""

PROCEED_BLOCK_MESSAGES = {
    'UNRESOLVED_DRAFT': '승인·발송이 끝나지 않은 메일 초안을 먼저 처리해 주세요.',
    'REPLY_REVIEW_PENDING': '도착한 회신을 검토하는 중입니다. 검토가 끝난 뒤 진행해 주세요.',
    'NO_REPLY': '아직 견적 회신을 보낸 거래처가 없습니다.',
    'EVIDENCE': '업무의 원본 메일 연결을 확인할 수 없습니다.',
    'STATE': '현재 상태에서는 지금까지의 회신으로 진행할 수 없습니다.',
}


def proceed_block(reason):
    """DB 거절 사유를 (reason_code, message)로 바꾼다. 모르는 사유는 STATE로 본다."""
    code = reason if reason in PROCEED_BLOCK_MESSAGES else 'STATE'
    return code, PROCEED_BLOCK_MESSAGES[code]
