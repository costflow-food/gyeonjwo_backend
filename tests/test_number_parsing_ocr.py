"""① 원문 값 해석: 스캔 OCR 공백과 숫자가 여러 개인 원문 (평가 v2 2026-11-19).

- 스캔 PDF의 OCR 결과는 '9 ,600원', 'December 1 , 2026'처럼 구분 기호 앞뒤에 공백이 생긴다. 숫자로 읽혀야 한다.
- '12월 공급 계획 물량 1,000kg'처럼 숫자가 여러 개인 원문은 unit_code가 있어도 거절해야 한다(앞의 12를 수량으로 저장한 버그, #179).
"""
import pytest

from app.agents import tools as T
from app.core.errors import ReviewRequired


@pytest.mark.parametrize('raw, expected', [('9 ,600원', '9600'), ('14 ,500원', '14500'), ('9,600원', '9600'),
                                           ('1 . 35 USD', '1.35')])
def test_price_reads_ocr_spaced_numbers(raw, expected):
    assert T._price(raw, 'unit_price')[0] == expected


@pytest.mark.parametrize('raw, expected', [('1 ,000kg', ('1000', 'kg')), ('2 ,000kg', ('2000', 'kg')),
                                           ('월 1,000kg', ('1000', 'kg')), ('박스당 10kg', ('10', 'kg'))])
def test_quantity_reads_ocr_spaced_numbers(raw, expected):
    assert T._quantity(raw, 'available_quantity') == expected


@pytest.mark.parametrize('raw', ['December 1 , 2026', 'from December 1 , 2026', 'December 1, 2026', '2026. 12. 1.'])
def test_date_reads_ocr_spaced_english_date(raw):
    assert T._date(raw, 'effective_from') == '2026-12-01'


@pytest.mark.parametrize('code', [None, 'kg'])
def test_quantity_with_several_numbers_is_rejected_even_with_unit_code(code):
    with pytest.raises(ReviewRequired) as error:
        T._quantity('12월 공급 계획 물량 1,000kg', 'available_quantity', code)
    assert error.value.issue['code'] == 'INVALID_NUMBER'


def test_ocr_spacing_does_not_join_separate_numbers():
    assert T._ocr_spacing('1,000kg 2,000kg') == '1,000kg 2,000kg'
    assert T._ocr_spacing('12 월 1 ,000kg') == '12 월 1,000kg'


def test_price_source_lookup_uses_the_whole_ocr_spaced_number():
    """원문 줄에서 단가를 찾을 때 '3 ,300원'의 3만 찾아 '컷 3cm' 줄을 잡지 않는다(평가 v2 #163: PRICE_NOTATION_UNKNOWN)."""
    assert T._number_pattern('3 ,300원') == T._number_pattern('3,300원')
