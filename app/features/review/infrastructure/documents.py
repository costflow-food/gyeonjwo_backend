import json
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


PDF_MAX_ATTEMPTS = 4             # 기술 오류는 최초 1회 + 재시도 3회까지 (내용 문제는 다시 시도하지 않는다)
PDF_TEXT_TIMEOUT_SECONDS = 20    # 텍스트 레이어 읽기 한 번의 시간 제한
# OCR 엔진: RapidOCR + PaddleOCR 한국어 모델 (모델·설정은 pdf_worker.OCR_MODEL_FILES)
# 엔진 비교(strawberry 공문 3개·합성 스캔 2개, 정답 값 31개의 원문 대조 통과):
#   RapidOCR 한국어 v5 31/31, Tesseract 5.4 kor+eng 17/31 ('원'→'21', '10kg'→'1069' 등 오인식)
OCR_DPI = 300                    # 제안값. 위 비교에 쓴 해상도
OCR_TIMEOUT_SECONDS = 45         # 한 쪽 OCR 한 번의 시간 제한. CPU 0.5개 서버에서 A4 한 쪽이 약 13초(모델 로딩 포함)
# PDF 워커 자식 프로세스의 실제 메모리 상한(MB, /proc가 있는 리눅스만). 넘으면 끝내고 내용 문제로 본다.
# 서버는 메모리 1GB 컨테이너 하나에 API·메일 수신·워커가 같이 돈다(운영 스펙). 서버 이미지에서 잰 OCR 한 쪽의
# 최고 실제 메모리: A4·A3 약 620MB, 픽셀 상한까지 줄인 A0 약 700MB. 서버 프로세스는 기동 직후 약 120MB.
# 환경변수 PDF_WORKER_MEMORY_MB로 바꿀 수 있다
PDF_WORKER_MEMORY_MB = 760
PDF_WORKER_POLL_SECONDS = 0.05
# OCR 워커는 한 번에 하나만 띄운다(한 쪽에 약 620MB라 둘이면 1GB 컨테이너가 넘친다). 텍스트 읽기(약 50MB)는 제한하지 않는다
_OCR_SLOT = threading.BoundedSemaphore(1)
# 모델 파일 폴더. 서버·개발 PC에서 `python -m app.features.review.infrastructure.pdf_worker setup <폴더>`로 한 번 받는다
OCR_MODEL_DIR_DEFAULT = Path(__file__).resolve().parents[4] / 'models' / 'ocr'


class PdfReadError(ValueError):
    """PDF를 읽지 못했다. kind: TECHNICAL(다시 시도) / CONTENT(PDF 자체 문제) / OCR_UNAVAILABLE(OCR 설치·설정 문제)
    / TIMEOUT(시간 제한 초과. 같은 PDF는 다시 읽어도 오래 걸리므로 다시 시도하지 않는다)"""
    kind = 'TECHNICAL'


class PdfTechnicalError(PdfReadError):
    kind = 'TECHNICAL'


class PdfContentError(PdfReadError):
    kind = 'CONTENT'


class OcrUnavailableError(PdfReadError):
    kind = 'OCR_UNAVAILABLE'


class PdfTimeoutError(PdfReadError):
    kind = 'TIMEOUT'


def read_pdf_pages(content: bytes) -> list[str]:
    """PDF 쪽마다의 텍스트 레이어(한 번만 시도). 글자가 없는 쪽(스캔 이미지)은 ''이라 쪽수는 실제 쪽수와 같다."""
    return _pdf_worker(content)['pages']


def ocr_model_dir() -> str:
    """OCR_MODEL_DIR 환경변수, 없으면 저장소의 models/ocr. 모델 파일이 없으면 OCR은 OCR_UNAVAILABLE이다."""
    import os
    return os.environ.get('OCR_MODEL_DIR') or str(OCR_MODEL_DIR_DEFAULT)


def _worker_command(args: tuple) -> list[str]:
    """PDF 워커 실행 명령."""
    return [sys.executable, '-m', 'app.features.review.infrastructure.pdf_worker', *args]


def _rss_mb(pid: int) -> int | None:
    """프로세스의 지금 실제 메모리(MB). /proc가 없는 OS(Windows)나 끝난 프로세스는 None."""
    try:
        with open(f'/proc/{pid}/status') as handle:
            for line in handle:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


def _run_bounded(command: list[str], content: bytes, env: dict, timeout: float, limit_mb: int) -> tuple[int, bytes]:
    """자식 프로세스를 띄워 (종료 코드, 표준 출력)을 돌려준다. 시간을 넘기면 PdfTimeoutError, 실제 메모리가 limit_mb를
    넘으면 PdfContentError로 자식을 끝낸다."""
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                             cwd=Path(__file__).resolve().parents[4],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output = {}
    talker = threading.Thread(target=lambda: output.update(stdout=child.communicate(content)[0]), daemon=True)
    talker.start()
    deadline = time.monotonic() + timeout
    try:
        while talker.is_alive():
            if time.monotonic() > deadline:
                raise PdfTimeoutError('PDF worker timed out')
            rss = _rss_mb(child.pid)
            if rss is not None and rss > limit_mb:
                raise PdfContentError('PDF worker memory limit exceeded')
            talker.join(PDF_WORKER_POLL_SECONDS)
    except PdfReadError:
        child.kill()
        talker.join(5)
        raise
    return child.returncode, output.get('stdout') or b''


def _pdf_worker(content: bytes, args: tuple = (), timeout: float = PDF_TEXT_TIMEOUT_SECONDS) -> dict:
    # Child process has no API keys; bounds runtime and memory and keeps parser failures out of the server.
    import os
    env = {key: value for key, value in os.environ.items()
           if key.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH')}
    limit_mb = int(os.environ.get('PDF_WORKER_MEMORY_MB') or PDF_WORKER_MEMORY_MB)
    ocr = args[:1] == ('ocr',)
    started = time.monotonic()
    # OCR은 한 번에 하나. 기다린 시간도 이 호출의 시간 제한에 든다. 자리가 안 나면 다시 시도할 기술 오류다
    if ocr and not _OCR_SLOT.acquire(timeout=timeout):
        raise PdfTechnicalError('OCR worker busy')
    try:
        returncode, stdout = _run_bounded(_worker_command(args), content, env,
                                          max(1.0, timeout - (time.monotonic() - started)), limit_mb)
    except (OSError, subprocess.SubprocessError):
        raise PdfTechnicalError('PDF text extraction failed') from None
    finally:
        if ocr:
            _OCR_SLOT.release()
    if returncode == 2:
        raise PdfContentError('PDF text extraction failed')
    if returncode == 3:
        raise OcrUnavailableError('PDF text extraction failed')
    if returncode:
        raise PdfTechnicalError('PDF text extraction failed')
    try:
        return json.loads(stdout)
    except ValueError:
        raise PdfTechnicalError('PDF text extraction failed') from None


def _with_retry(read) -> tuple[object, dict]:
    """기술 오류만 PDF_MAX_ATTEMPTS번까지 시도한다. 반환: (결과 또는 None, 시도 기록)
    시도 기록: {attempts, error_kind: None·TECHNICAL·CONTENT·OCR_UNAVAILABLE·TIMEOUT, retry_exhausted}"""
    for attempt in range(1, PDF_MAX_ATTEMPTS + 1):
        try:
            return read(), {'attempts': attempt, 'error_kind': None, 'retry_exhausted': False}
        except PdfTechnicalError:
            continue
        except PdfReadError as error:
            return None, {'attempts': attempt, 'error_kind': error.kind, 'retry_exhausted': False}
    return None, {'attempts': PDF_MAX_ATTEMPTS, 'error_kind': 'TECHNICAL', 'retry_exhausted': True}


def read_pdf_document(content: bytes) -> dict:
    """PDF의 텍스트 레이어를 쪽 단위로 읽는다(OCR 없음). 기술 오류는 다시 시도한다.
    반환: {status: READ(글자가 있는 쪽이 하나라도 있음)·NO_TEXT(쪽은 있지만 모두 글자 없음)·FAILED, pages,
           attempts, error_kind, retry_exhausted}
    NO_TEXT여도 pages는 쪽수만큼의 ''라서, 빈 쪽을 골라 ocr_pdf_page로 읽을 수 있다."""
    pages, log = _with_retry(lambda: read_pdf_pages(content))
    if pages is None:
        return {'status': 'FAILED', 'pages': [], **log}
    return {'status': 'READ' if any(text.strip() for text in pages) else 'NO_TEXT', 'pages': pages, **log}


def ocr_pdf_page(content: bytes, page: int, *, remaining_seconds=None) -> dict:
    """PDF의 page쪽(1부터) 하나를 그림으로 렌더링해 OCR한다. 기술 오류는 다시 시도한다.
    반환: {status: READ·NO_TEXT(OCR해도 글자 없음)·FAILED, text, text_source: 'OCR', attempts, error_kind,
           retry_exhausted}
    OCR 텍스트는 원문 그림을 읽은 결과라 글자를 잘못 읽었을 수 있다. 원문에 없는 값을 채우지 않는다."""
    args = ('ocr', str(page), ocr_model_dir(), str(OCR_DPI))
    def read():
        timeout = OCR_TIMEOUT_SECONDS
        if remaining_seconds is not None:
            timeout = min(timeout, remaining_seconds() - 1)
            if timeout <= 0:
                raise OcrUnavailableError('Insufficient OCR execution time')
        return _pdf_worker(content, args, timeout=timeout)

    result, log = _with_retry(read)
    if result is None:
        return {'status': 'FAILED', 'text': '', 'text_source': 'OCR', **log}
    text = result['text']
    return {'status': 'READ' if text.strip() else 'NO_TEXT', 'text': text, 'text_source': 'OCR', **log}
