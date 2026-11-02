"""업체명 비교용 정규화. 출처마다 다른 법인 표기·띄어쓰기를 걷어 같은 업체를 묶는다."""
import re

_COMPANY_MARKS = re.compile(r'\(주\)|㈜|\(유\)|\(농\)|주식회사|유한회사|농업회사법인|영농조합법인|농업법인|협동조합')


def normalize_name(name: str | None) -> str:
    text = _COMPANY_MARKS.sub('', name or '')
    return re.sub(r'[\s\W_]+', '', text).lower()


def normalize_item_type(value: str | None) -> str:
    """품목유형 표기 차이(과·채가공품 / 과.채가공품 / 과 채가공품)를 없앤다."""
    return re.sub(r'[\s.·ㆍ\-]+', '', value or '')


def region_of(address: str | None) -> str | None:
    """소재지에서 시·군·구 이름 하나를 꺼낸다(검색어 보조용)."""
    for token in (address or '').split()[1:3]:
        if token.endswith(('시', '군', '구')):
            return token
    return None
