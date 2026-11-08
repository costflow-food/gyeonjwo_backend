"""Rebuild five text PDFs from the same synthetic JSON used by the graph check.

Requires reportlab only when regenerating documents; it is not a server dependency.
python -m scripts.build_garlic_attachments --font C:/Windows/Fonts/malgun.ttf
"""
import argparse
import json
from pathlib import Path


def build(font):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / 'tests/fixtures/garlic_market_scenario.json').read_text(encoding='utf-8'))
    directory = root / 'tests/fixtures/garlic_attachments'
    directory.mkdir(exist_ok=True)
    filenames = ['incumbent_quote.pdf', 'a_quote.pdf', 'b_quote.pdf', 'c_quote.pdf', 'b_clarification.pdf']
    assert len(fixture['mails']) == len(filenames) == 5
    pdfmetrics.registerFont(TTFont('ScenarioKorean', str(font)))
    for filename, mail in zip(filenames, fixture['mails']):
        pdf = canvas.Canvas(str(directory / filename), pagesize=(595.28, 841.89), invariant=1)
        pdf.setTitle('깐마늘 합성 테스트 · ' + filename)
        pdf.setFont('ScenarioKorean', 19)
        pdf.drawString(48, 786, '깐마늘 공급 안내' if filename == filenames[0] else '견적·조건 안내')
        pdf.setFont('ScenarioKorean', 10)
        pdf.setFillColorRGB(.45, .45, .45)
        pdf.drawString(48, 758, '합성 테스트 자료 · 실제 거래나 인증 증빙이 아닙니다.')
        pdf.setFillColorRGB(0, 0, 0)
        text = pdf.beginText(48, 720)
        text.setFont('ScenarioKorean', 11)
        text.setLeading(26)
        for line in mail['body_text'].splitlines():
            assert pdfmetrics.stringWidth(line, 'ScenarioKorean', 11) < 500
            text.textLine(line)
        pdf.drawText(text)
        pdf.setFont('ScenarioKorean', 9)
        pdf.drawString(48, 46, '견줘 통합 검증 / 국산 대서 상품 / 모든 금액과 조건은 테스트 가정')
        pdf.save()
    return directory


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, required=True, help='Local Korean TrueType font to embed')
    print(build(parser.parse_args().font))
