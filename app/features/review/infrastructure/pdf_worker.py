"""Bounded PDF worker; stdin/stdout bytes, no files or credentials.

텍스트 모드(인자 없음): 쪽마다 텍스트 레이어를 읽는다. 글자가 없는 쪽(스캔 이미지)도 빈 문자열로 남겨 쪽수를 보존한다.
OCR 모드(`ocr <쪽 번호> <모델 폴더> <dpi>`): 그 한 쪽만 그림으로 렌더링해 RapidOCR(PaddleOCR 한국어 모델)로 읽는다.
  모델은 <모델 폴더>에 미리 받아 둔 파일만 쓴다. 실행 중에 내려받지 않는다.
모델 준비(`setup <모델 폴더>`, stdin 없음): 모델을 내려받아 SHA256을 확인한다. 서버 빌드·개발 PC에서 한 번 실행한다.

종료 코드 — 호출자(documents._pdf_worker)가 재시도 여부를 정한다
  0 성공 / 1 기술 오류(예상하지 못한 예외, 재시도 대상) / 2 내용 문제(암호화·한도 초과·손상, 재시도하지 않음)
  3 OCR 준비 안 됨(라이브러리·모델 파일 없음, 설정 문제라 재시도하지 않음)

메모리 한도는 부모(documents._pdf_worker)가 이 프로세스의 실제 메모리를 지켜보다 넘으면 끝낸다. 주소 공간 상한(RLIMIT_AS)은
쓰지 않는다: OCR 엔진(onnxruntime)이 실제보다 훨씬 큰 가상 메모리(A4 한 쪽에 약 5GB)를 잡아 정상 OCR까지 죽는다.
"""
import io
import json
import sys
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_PAGES = 100
# OCR 렌더링 한 쪽의 픽셀 수 상한. A4 300dpi가 약 870만, A3 300dpi가 약 1,740만 픽셀이다.
# 더 큰 쪽은 상한에 맞게 해상도를 낮춰 렌더링하고, MIN_RENDER_DPI보다 낮춰야 하면 읽지 않는다(내용 문제).
# OCR 엔진은 어차피 긴 변 2,000픽셀로 줄여 읽으므로(rapidocr Global.max_side_len) 더 크게 그려도 얻는 것이 없다
MAX_RENDER_PIXELS = 20_000_000
MIN_RENDER_DPI = 100
OCR_THREADS = 1
EXIT_TECHNICAL, EXIT_CONTENT, EXIT_OCR_UNAVAILABLE = 1, 2, 3

# 고정한 OCR 모델 (rapidocr 3.10.0 기본 모델 목록 기준, 파일은 SHA256으로 확인된다)
#   검출: PP-OCRv6 det small / 인식: 한국어 PP-OCRv5 rec mobile
#   글자 방향 판단(cls)은 끈다. 켜면 표 안의 바른 글자를 뒤집힌 것으로 보고 '냉동딸기(IQF)'를 '(O|<움'으로 읽었다
#   (strawberry 공문 3개·합성 스캔 2개 비교: 끔 31/31, 켬 23/31 원문 대조 통과)
OCR_MODEL_FILES = ('PP-OCRv6_det_small.onnx', 'korean_PP-OCRv5_rec_mobile.onnx')


class ContentError(Exception):
    """PDF 자체의 문제. 다시 읽어도 결과가 같다."""


class OcrUnavailable(Exception):
    """OCR 라이브러리나 모델 파일이 없다."""


def _reader(raw) -> PdfReader:
    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
        raise ContentError('PDF outside supported bounds')
    return reader


def extract(raw):
    """(전체 텍스트, 쪽별 텍스트). 글자가 하나도 없으면 전체 텍스트는 ''이고 쪽별 텍스트는 쪽수만큼의 ''다."""
    reader = _reader(raw)
    parts, pages = [], []
    has_text = False
    for number, page in enumerate(reader.pages, 1):
        contents = page.get_contents()
        if contents and len(contents.get_data()) > 8_000_000:
            raise ContentError('PDF page too large')
        text = page.extract_text() or ''
        has_text = has_text or bool(text.strip())
        parts.append(f'{number}페이지\n{text}')
        pages.append(text)
        if sum(map(len, parts)) > 300_000:
            raise ContentError('PDF text too large')
    return ('\n'.join(parts), pages) if has_text else ('', pages)


def _engine(model_dir: str):
    try:
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion
    except ImportError:
        raise OcrUnavailable() from None
    return RapidOCR(params={
        'Global.model_root_dir': model_dir, 'Global.use_cls': False, 'Global.log_level': 'error',
        'Det.ocr_version': OCRVersion.PPOCRV6, 'Det.model_type': ModelType.SMALL,
        'Rec.lang_type': LangRec.KOREAN, 'Rec.ocr_version': OCRVersion.PPOCRV5, 'Rec.model_type': ModelType.MOBILE,
        # 스레드는 하나. 기본값(-1)은 호스트 코어 수만큼 띄워, CPU 0.5개 컨테이너에서 A4 한 쪽이 82초 걸렸다(1개면 13초,
        # 결과 같음. 서버 이미지·--cpus=0.5 측정)
        'EngineConfig.onnxruntime.intra_op_num_threads': OCR_THREADS,
        'EngineConfig.onnxruntime.inter_op_num_threads': OCR_THREADS})


def _lines(boxes, texts) -> str:
    """글자 상자를 줄로 묶는다. 세로 중심이 상자 높이의 절반 안이면 같은 줄이고, 줄 안에서는 왼쪽부터 잇는다."""
    items = sorted((float(box[:, 1].mean()), float(box[:, 0].min()), float(box[:, 1].max() - box[:, 1].min()), text)
                   for box, text in zip(boxes, texts))
    lines, current = [], []
    for item in items:
        if current and abs(item[0] - current[-1][0]) > max(item[2], current[-1][2]) * 0.5:
            lines.append(current)
            current = []
        current.append(item)
    if current:
        lines.append(current)
    return '\n'.join(' '.join(text for *_, text in sorted(line, key=lambda i: i[1])) for line in lines)


def render_scale(width_pt: float, height_pt: float, dpi: int) -> float:
    """쪽 크기(포인트, 1/72인치)에 쓸 렌더링 배율. 픽셀 수가 MAX_RENDER_PIXELS를 넘으면 그 안에 들도록 배율을 낮춘다.
    MIN_RENDER_DPI보다 낮춰야 할 만큼 큰 쪽(또는 크기를 알 수 없는 쪽)은 ContentError다."""
    area = float(width_pt) * float(height_pt)
    if not area > 0:
        raise ContentError('PDF page size invalid')
    scale = min(dpi / 72, (MAX_RENDER_PIXELS / area) ** 0.5)
    if scale * 72 < MIN_RENDER_DPI:
        raise ContentError('PDF page too large to render')
    return scale


def ocr_page(raw, number: int, model_dir: str, dpi: int) -> str:
    """number쪽(1부터)을 렌더링해 OCR한 텍스트."""
    if not 1 <= number <= len(_reader(raw).pages):
        raise ContentError('PDF page out of range')
    if not all((Path(model_dir) / name).is_file() for name in OCR_MODEL_FILES):
        raise OcrUnavailable()
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise OcrUnavailable() from None
    try:
        pdf = pdfium.PdfDocument(raw)
    except pdfium.PdfiumError:
        raise ContentError('PDF cannot be rendered') from None
    try:
        page = pdf[number - 1]
        scale = render_scale(*page.get_size(), dpi)
        # 회색조로 렌더링해 3채널로 넘긴다 (모델 입력이 BGR)
        gray = page.render(scale=scale, grayscale=True).to_numpy()
    finally:
        pdf.close()
    image = gray[:, :, :1].repeat(3, axis=2) if gray.ndim == 3 else gray[:, :, None].repeat(3, axis=2)
    result = _engine(model_dir)(image)
    if result.boxes is None or not len(result.boxes):
        return ''
    return _lines(result.boxes, result.txts)


def main(argv) -> int:
    if argv[:1] == ['setup']:
        # 모델은 처음 쓸 때 받는다. 빈 그림으로 검출·인식을 한 번씩 돌려 받게 하고 SHA256을 확인한다
        import numpy as np
        engine, blank = _engine(argv[1]), np.full((64, 256, 3), 255, dtype=np.uint8)
        engine(blank, use_det=True, use_cls=False, use_rec=False)
        engine(blank, use_det=False, use_cls=False, use_rec=True)
        missing = [name for name in OCR_MODEL_FILES if not (Path(argv[1]) / name).is_file()]
        print('OCR models ready' if not missing else f'OCR models missing: {missing}')
        return 1 if missing else 0
    raw = sys.stdin.buffer.read(52_428_801)
    try:
        if argv[:1] == ['ocr']:
            output = {'text': ocr_page(raw, int(argv[1]), argv[2], int(argv[3]))}
        else:
            text, pages = extract(raw)
            output = {'text': text, 'pages': pages}
    except (ContentError, PyPdfError, MemoryError):
        return EXIT_CONTENT
    except OcrUnavailable:
        return EXIT_OCR_UNAVAILABLE
    except Exception:
        return EXIT_TECHNICAL
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
