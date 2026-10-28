from app.core.errors import ServiceError
from app.features.mail.domain.schemas import Classification


def classify(agent, subject, body):
    sample = (subject + '\n' + body[:6000]).lower()
    if any(word in sample for word in ('단가 변경', '단가변경', '단가 인상', '가격 인상', '견적서', '견적 요청')):
        return Classification(status='TRUE', reason='구매 관련 단가·견적 키워드 확인')
    if any(word in sample for word in ('수신거부', 'unsubscribe')) and any(word in sample for word in ('광고', 'newsletter')):
        return Classification(status='FALSE', reason='광고·구독 안내 규칙에 해당')
    try:
        return agent.run('classification', 'Classify purchasing work mail only. Return status and short Korean reason. '
                         'Do not extract prices, quantities or detailed fields. Uncertain means PENDING.',
                         {'subject': subject, 'body_excerpt': body[:6000]}, Classification)
    except (ServiceError, RuntimeError):
        return Classification(status='PENDING', reason='분류 실패: CLASSIFICATION_UNAVAILABLE')
