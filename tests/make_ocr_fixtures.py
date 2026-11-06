"""OCR 테스트용 합성 PDF를 tests/fixtures/ocr/에 만든다. 실제 거래 문서가 아닌 가짜 견적 문구만 쓴다.

실행: .venv/Scripts/python.exe tests/make_ocr_fixtures.py
필요: Pillow(개발용, requirements.txt에는 넣지 않는다), 한글 폰트(Windows 맑은 고딕). 만든 PDF는 커밋해 두고 다시 만들 필요는 없다.

만드는 파일
- text_quote.pdf   : 텍스트 레이어가 있는 1쪽 PDF (영문·숫자. 기존 텍스트 추출이 그대로인지 확인)
- scan_quote.pdf   : 그림만 있는 2쪽 PDF. 1쪽 견적 문구, 2쪽 빈 쪽 (스캔본)
- scan_two_items.pdf: 그림만 있는 1쪽 PDF. 품목 2개
- encrypted.pdf    : 암호가 걸린 1쪽 PDF (내용 문제 → 다시 시도하지 않음)
- notice_increase.pdf / notice_increase_scan.pdf
    가격 인상 예고 공문 (회사 공문 양식: 로고·자간 넓힌 회사명·수신/발신/참조/제목·명조 본문·아래 항목·날짜·직인).
    인상 시기만 있고 단가·인상률은 "추후 안내"인 문서 → 단가를 지어내지 않아야 한다
- price_table.pdf / price_table_scan.pdf
    단가 인상 요청 양식 (문서번호·시행일자·결재란·품종명/현재단가/변경단가/비고 표). 품목 3개와 빈 행
  *_scan.pdf는 같은 내용을 스캔한 것처럼 만든다 (0.8도 기울임, 잡티, 흐림, 150dpi, JPEG 압축).
  양식은 실제 공문 이미지를 참고했지만 업체명·주소·전화·대표자·직인은 모두 가상 값이다 (전화는 555 국번).

이미 있는 파일은 다시 만들지 않는다. 다시 만들려면 --force.
"""
import io
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfWriter

OUT = Path(__file__).parent / 'fixtures' / 'ocr'
FONT = 'C:/Windows/Fonts/malgun.ttf'
FONT_BOLD = 'C:/Windows/Fonts/malgunbd.ttf'
FONT_SERIF = 'C:/Windows/Fonts/batang.ttc'      # 바탕 (공문 본문)
FONT_FORM = 'C:/Windows/Fonts/gulim.ttc'        # 굴림 (단가 인상 양식)
A4_200DPI = (1654, 2339)

SCAN_QUOTE_LINES = ['견적서 (합성 시연용)', '품목: 냉동 딸기 특품 10kg/박스', '단가: 54,500원 / 박스',
                    '적용일: 2026년 12월 1일', '유효기간: 2026년 12월 31일까지']
TWO_ITEM_LINES = ['견적서 (합성 시연용)', '1. 냉동 딸기 특품 10kg/박스  단가 54,500원', '2. 냉동 블루베리 5kg/박스  단가 38,000원']


def _page(lines: list[str]) -> Image.Image:
    """A4 비율 흰 바탕에 글자를 그린 회색조 그림 (150dpi 기준 1240x1754)."""
    image = Image.new('L', (1240, 1754), 255)
    draw, font = ImageDraw.Draw(image), ImageFont.truetype(FONT, 40)
    for index, line in enumerate(lines):
        draw.text((120, 160 + index * 90), line, font=font, fill=0)
    return image


def _image_pdf(pages: list[Image.Image]) -> bytes:
    buffer = io.BytesIO()
    pages[0].save(buffer, 'PDF', resolution=150, save_all=True, append_images=pages[1:])
    return buffer.getvalue()


def _text_pdf(text: str) -> bytes:
    """Helvetica로 영문 한 줄을 쓴 최소 PDF. 오프셋 표(xref)를 직접 계산한다."""
    stream = f'BT /F1 18 Tf 72 720 Td ({text}) Tj ET'.encode('latin-1')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
               b'/Resources << /Font << /F1 5 0 R >> >> >>',
               b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    out += b''.join(b'%010d 00000 n \n' % offset for offset in offsets)
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return bytes(out)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill=(0, 0, 0)):
    width = draw.textlength(text, font=font)
    draw.text(((A4_200DPI[0] - width) / 2, y), text, font=font, fill=fill)


def _seal(draw: ImageDraw.ImageDraw, x: int, y: int, text: str):
    """붉은 원형 직인 (가상). 글자가 겹쳐 OCR 잡음이 되는 부분이다."""
    red = (215, 40, 40)
    draw.ellipse((x, y, x + 150, y + 150), outline=red, width=6)
    draw.ellipse((x + 18, y + 18, x + 132, y + 132), outline=red, width=3)
    font = _font(FONT_BOLD, 34)
    for index, char in enumerate(text):
        draw.text((x + 40 + (index % 2) * 38, y + 32 + (index // 2) * 40), char, font=font, fill=red)


NOTICE_BODY = [
    '안녕하십니까?',
    '항상 (주)새솔식품과 함께 해주신 모든 분들께 진심으로 감사드립니다.',
    '현재 국내 냉동 딸기 원물의 40% 이상을 차지하는 남부 지역에서',
    '이상 고온과 장마로 인한 작황 부진, 인건비 상승, 냉동 보관비 인상 등으로',
    '원물 수급에 차질이 발생했습니다.',
    '위 여파로 딸기 수매가가 35% 이상 올랐고, 현재도 가파르게 상승하고 있습니다.',
    '이에 폐사는 지난 7월 단가를 조정하였음에도 급격한 원가 상승을 감당할 수 없어',
    '부득이 2026년 12월 1일 가격 인상을 예정하고 있음을 안내해드립니다.',
    '정확한 인상률은 현재 수매가가 계속 변동 중인 관계로, 11월까지 동향을 살핀 후',
    '확정하여 안내 드리겠습니다.',
    '앞으로도 폐사 제품을 이용해주시기를 부탁드립니다.',
    '감사합니다.',
]
NOTICE_ITEMS = [('인상품목', '냉동과일 전 품목'), ('인상시기', '2026년 12월 01일 발주 분 부터 적용'),
                ('인상률', '11월 하순 안내 예정'), ('별첨', '원물 수매가 변동 내역')]


def _notice_page() -> Image.Image:
    """가격 인상 예고 공문 (첫 번째 참고 이미지의 양식)."""
    image = Image.new('RGB', A4_200DPI, 'white')
    draw = ImageDraw.Draw(image)
    blue, red, gray = (30, 100, 180), (220, 40, 40), (110, 110, 110)
    draw.text((1300, 110), '서식 2026-1101', font=_font(FONT, 26), fill=(60, 60, 60))
    # 로고 (가상): 파란 사각형과 붉은 띠, 영문 이름
    draw.rectangle((180, 210, 230, 330), fill=blue)
    draw.polygon([(250, 330), (300, 210), (340, 300), (420, 200), (440, 215), (350, 330)], fill=blue)
    for offset in (0, 18):
        draw.line((330, 225 + offset, 440, 195 + offset), fill=red, width=8)
    draw.text((330, 300), 'SAESOL', font=_font(FONT_BOLD, 22), fill=blue)
    draw.text((500, 200), '주 식 회 사 새 솔 식 품', font=_font(FONT_BOLD, 76), fill='black')
    _center(draw, 360, '경기도 ○○시 ○○구 ○○로 30 새솔빌딩 3층', _font(FONT, 28))
    _center(draw, 405, 'T : 031-555-0101  /  F : 031-555-0102', _font(FONT, 28))
    draw.line((180, 470, 1475, 470), fill=gray, width=2)
    header = _font(FONT, 32)
    for index, (label, value) in enumerate([('수         신', '각 거래처'), ('발         신', '(주) 새 솔 식 품'),
                                            ('참         조', '대표님, 구매 담당자님 귀하'),
                                            ('제         목', '제품 가격 인상 예고')]):
        draw.text((180, 510 + index * 52), f'{label} : {value}', font=header, fill='black')
    draw.line((180, 740, 1475, 740), fill=gray, width=2)
    body = _font(FONT_SERIF, 32)
    for index, line in enumerate(NOTICE_BODY):
        draw.text((180, 790 + index * 54), line, font=body, fill='black')
    _center(draw, 1500, '-    아        래    -', _font(FONT, 32))
    items = _font(FONT, 31)
    for index, (label, value) in enumerate(NOTICE_ITEMS):
        draw.text((520, 1590 + index * 52), f'{label}  :  {value}', font=items, fill='black')
    _center(draw, 1900, '2026 년    10 월    20 일', _font(FONT_SERIF, 52))
    draw.text((1150, 2070), '주 식 회 사 새 솔 식 품', font=_font(FONT_BOLD, 36), fill='black')
    draw.text((1150, 2125), '대표   한   서   진', font=_font(FONT, 32), fill='black')
    _seal(draw, 1390, 2040, '새솔식품')
    return image


PRICE_ROWS = [('냉동딸기(IQF) 특품 10kg/박스', '50,000원', '55,000원', '부가세 별도'),
              ('냉동 블루베리 5kg/박스', '38,000원', '41,000원', '부가세 별도'),
              ('냉동 망고 다이스 1kg/봉', '9,800원', '10,500원', '')]


def _price_table_page() -> Image.Image:
    """단가 인상 요청 양식 (두 번째 참고 이미지의 양식)."""
    image = Image.new('RGB', A4_200DPI, 'white')
    draw = ImageDraw.Draw(image)
    small, normal = _font(FONT_FORM, 26), _font(FONT_FORM, 28)
    _center(draw, 110, '(주) 새 솔 식 품', _font(FONT_BOLD, 56))
    draw.text((150, 230), '주 소 : 경기도 ○○시 ○○구 ○○로 30 새솔빌딩 3층', font=small, fill='black')
    draw.text((150, 280), 'T E L : 031-555-0101', font=small, fill='black')
    draw.text((680, 280), 'F A X : 031-555-0102', font=small, fill='black')
    draw.text((1150, 280), '담 당 : 영업팀 박하늘', font=small, fill='black')
    draw.line((140, 330, 1515, 330), fill='black', width=3)
    # 왼쪽 문서 정보
    info = [('문서번호', '제 1027 호'), ('시행일자', '2026 년 10 월 27 일'), ('수    신', '(주)트루나스 대표이사'),
            ('참    조', '구매팀 담당자'), ('제    목', '단가 인상에 대한 건')]
    draw.rectangle((150, 360, 900, 560), outline='black', width=2)
    for index, (label, value) in enumerate(info[:2]):
        draw.text((165, 375 + index * 50), f'{label} : {value}', font=small, fill='black')
    draw.line((150, 470, 900, 470), fill='black', width=1)
    for index, (label, value) in enumerate(info[2:]):
        draw.text((165, 480 + index * 46) if index < 2 else (165, 590), f'{label} : {value}', font=small, fill='black')
    # 오른쪽 결재란 (빈 칸과 작은 글자 → OCR 잡음)
    left, top, cell_w, cell_h = 940, 360, 95, 48
    labels = [['선 결', '', '', '지시', ''], ['접 수', '일자/시간', '', '결재', ''], ['', '번호', '', '·', ''],
              ['처리과', '', '', '공람', ''], ['담당자', '', '', '', '']]
    widths = [95, 140, 120, 70, 145]
    for row, cells in enumerate(labels):
        x = left
        for col, text in enumerate(cells):
            draw.rectangle((x, top + row * cell_h, x + widths[col], top + (row + 1) * cell_h), outline='black', width=1)
            if text:
                draw.text((x + 8, top + row * cell_h + 12), text, font=_font(FONT_FORM, 20), fill='black')
            x += widths[col]
    body = [
        '1. 귀사의 무궁한 발전을 기원합니다.',
        '2. 당사에서 귀사에 납품중인 품목의 원물 가격 상승으로 인하여 부득이 현행 납품 가격에',
        '   대하여 2026 년 12 월 01 일 부로 인상을 하게 되었습니다.',
        '3. 원물 가격의 상승을 고려하여 아래와 같이 단가를 인상하도록 하오니 아무쪼록 양해해 주',
        '   시기 바랍니다.',
    ]
    for index, line in enumerate(body):
        draw.text((150, 700 + index * 52), line, font=normal, fill='black')
    _center(draw, 1010, '-  아    래  -', _font(FONT_BOLD, 34))
    # 단가 표
    columns = [(150, '품    종    명'), (760, '현 재 단 가'), (1010, '변 경 단 가'), (1260, '비    고')]
    right, top, row_h = 1510, 1090, 66
    rows = [None] + PRICE_ROWS + [('', '', '', '')] * 2
    for index, row in enumerate(rows):
        y = top + index * row_h
        draw.rectangle((150, y, right, y + row_h), outline='black', width=2 if index == 0 else 1)
        for col, (x, title) in enumerate(columns):
            if col:
                draw.line((x, y, x, y + row_h), fill='black', width=1)
            text = title if row is None else row[col]
            if text:
                draw.text((x + 16, y + 18), text, font=normal, fill='black')
    draw.text((420, 1720), '(주) 새 솔 식 품          대표이사   한 서 진', font=_font(FONT_BOLD, 40), fill='black')
    _seal(draw, 1290, 1680, '새솔식품')
    return image


def _scanned(image: Image.Image, seed: int) -> Image.Image:
    """스캔한 것처럼: 회색조, 0.8도 기울임, 잡티, 약한 흐림, 150dpi로 축소, JPEG 압축."""
    rng = random.Random(seed)
    gray = image.convert('L').rotate(0.8, resample=Image.BICUBIC, expand=False, fillcolor=255)
    noisy = Image.blend(gray, Image.effect_noise(gray.size, 18), 0.08)   # 종이 결 잡티
    draw = ImageDraw.Draw(noisy)
    for _ in range(400):                                   # 먼지 점
        x, y = rng.randrange(gray.size[0]), rng.randrange(gray.size[1])
        draw.ellipse((x, y, x + rng.randint(1, 3), y + rng.randint(1, 3)), fill=rng.randint(60, 140))
    blurred = noisy.filter(ImageFilter.GaussianBlur(0.7))
    small = blurred.resize((1240, 1754), Image.LANCZOS)    # 150dpi
    buffer = io.BytesIO()
    small.save(buffer, 'JPEG', quality=55)
    return Image.open(io.BytesIO(buffer.getvalue()))


def _image_pdf_dpi(image: Image.Image, dpi: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, 'PDF', resolution=dpi)
    return buffer.getvalue()


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt('synthetic-password')
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def main(force: bool = False):
    OUT.mkdir(parents=True, exist_ok=True)
    files = {'text_quote.pdf': lambda: _text_pdf('Unit price 54,500 KRW per box'),
             'scan_quote.pdf': lambda: _image_pdf([_page(SCAN_QUOTE_LINES), _page([])]),
             'scan_two_items.pdf': lambda: _image_pdf([_page(TWO_ITEM_LINES)]),
             'encrypted.pdf': _encrypted_pdf,
             'notice_increase.pdf': lambda: _image_pdf_dpi(_notice_page(), 200),
             'notice_increase_scan.pdf': lambda: _image_pdf_dpi(_scanned(_notice_page(), seed=1), 150),
             'price_table.pdf': lambda: _image_pdf_dpi(_price_table_page(), 200),
             'price_table_scan.pdf': lambda: _image_pdf_dpi(_scanned(_price_table_page(), seed=2), 150)}
    for name, build in files.items():
        if (OUT / name).exists() and not force:
            continue
        content = build()
        (OUT / name).write_bytes(content)
        print(name, len(content), 'bytes')


if __name__ == '__main__':
    main(force='--force' in sys.argv)
