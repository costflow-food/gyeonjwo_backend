import email.policy
import hashlib
import json
import subprocess
import sys
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from app.shared.infrastructure.db.storage import MAX_BYTES, attachment_path, mail_path
from app.core.errors import ReviewRequired, ServiceError


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


def parse_mail(raw: bytes) -> dict:
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Invalid EML size')
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    sender, subject = str(message.get('From', '')).strip(), str(message.get('Subject', '')).strip()
    plain, html, attachments = [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True) or b''
        filename = part.get_filename()
        if filename or part.get_content_disposition() == 'attachment':
            attachments.append({'original_name': filename or 'attachment',
                                'content_type': part.get_content_type(), 'content': content})
        elif part.get_content_type() in ('text/plain', 'text/html'):
            text = content.decode(part.get_content_charset() or 'utf-8', errors='replace')
            (plain if part.get_content_type() == 'text/plain' else html).append(text)
    body = '\n'.join(plain)
    if not body.strip():
        parser = HtmlText()
        parser.feed('\n'.join(html))
        body = ''.join(parser.parts)
    if not sender or not subject or not body.strip() or len(attachments) > 100:
        raise ValueError('Mail sender, subject and full body are required; at most 100 attachments')
    return {'sender': sender, 'subject': subject, 'body': body, 'attachments': attachments,
            'headers': {'message_id': str(message.get('Message-ID', '')),
                        'date': str(message.get('Date', '')), 'from': sender, 'subject': subject}}


def safe_attachment_name(index: int, content_type: str) -> str:
    extensions = {'application/pdf': '.pdf', 'text/plain': '.txt', 'text/html': '.html'}
    return f'attach_{index:03d}' + extensions.get(content_type, '.bin')


def source_file(source: dict, work_id: str, filename: str) -> dict:
    if filename == 'mail.eml':
        return {'storage_path': mail_path(work_id), 'size_bytes': source['raw_size_bytes'],
                'sha256': source['raw_sha256'], 'content_type': 'message/rfc822', 'original_name': filename}
    path = attachment_path(work_id, filename)
    matches = [item for item in source.get('attachments', []) if item.get('storage_path') == path]
    if len(matches) != 1:
        raise ServiceError('SOURCE_FILE_NOT_FOUND', '해당 업무의 파일을 찾을 수 없습니다.', 404)
    return matches[0]


def read_pdf(content: bytes) -> str:
    # Child process has no API keys; bounds runtime and keeps parser failures out of the server.
    import os
    env = {key: value for key, value in os.environ.items()
           if key.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH')}
    try:
        result = subprocess.run([sys.executable, '-m', 'app.features.review.infrastructure.pdf_worker'], input=content,
                                capture_output=True, timeout=20, env=env,
                                cwd=Path(__file__).resolve().parents[4], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('PDF text extraction failed')
        return json.loads(result.stdout)['text']
    except (ValueError, subprocess.SubprocessError):
        raise ValueError('PDF text extraction failed') from None


def document_text(work_id, source, storage, max_chars, with_sections=False):
    sections = {'메일 본문': source['mail_body']}
    text = ['메일 본문\n' + source['mail_body']]
    for attachment in source.get('attachments', []):
        name = attachment['storage_path'].rsplit('/', 1)[-1]
        meta = source_file(source, work_id, name)
        content = storage.download_verified(meta['storage_path'], meta['size_bytes'], meta['sha256'])
        content_type = meta['content_type']
        if content_type == 'application/pdf':
            for attempt in range(4):
                try:
                    extracted = read_pdf(content)
                    break
                except ValueError:
                    if attempt == 3:
                        raise ReviewRequired('PDF_READ_FAILED', 'PDF를 읽지 못했습니다. 읽을 수 있는 원문이 필요합니다.', name)
            if not extracted.strip():
                raise ReviewRequired('OCR_REQUIRED', '텍스트가 없는 PDF는 OCR 처리가 필요합니다.', name)
        elif content_type == 'text/plain':
            extracted = content.decode('utf-8', errors='strict')
        elif content_type == 'text/html':
            parser = HtmlText()
            parser.feed(content.decode('utf-8', errors='strict'))
            extracted = ''.join(parser.parts)
        else:
            raise ReviewRequired('UNSUPPORTED_ATTACHMENT', '지원하지 않는 첨부 형식입니다.', name)
        text.append(name + '\n' + extracted)
        sections[name] = extracted
    result = '\n\n'.join(text)
    if len(result) > max_chars:
        raise ReviewRequired('DOCUMENT_TOO_LARGE', '원문이 분석 범위를 초과했습니다. 일부를 잘라서 분석하지 않습니다.')
    return (result, sections) if with_sections else result
