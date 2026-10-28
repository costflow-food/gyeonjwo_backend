"""Bounded text-only PDF worker; stdin/stdout bytes, no files or credentials."""
import io
import json
import sys

from pypdf import PdfReader


def extract(raw):
    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted or len(reader.pages) > 100:
        raise ValueError('PDF outside supported bounds')
    parts = []
    has_text = False
    for number, page in enumerate(reader.pages, 1):
        contents = page.get_contents()
        if contents and len(contents.get_data()) > 8_000_000:
            raise ValueError('PDF page too large')
        text = page.extract_text() or ''
        has_text = has_text or bool(text.strip())
        parts.append(f'{number}페이지\n{text}')
        if sum(map(len, parts)) > 300_000:
            raise ValueError('PDF text too large')
    return '\n'.join(parts) if has_text else ''


if __name__ == '__main__':
    try:
        text = extract(sys.stdin.buffer.read(52_428_801))
        sys.stdout.buffer.write(json.dumps({'text': text}, ensure_ascii=False).encode('utf-8'))
    except Exception:
        sys.exit(1)
