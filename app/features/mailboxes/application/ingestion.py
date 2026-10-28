"""Gmail full-format metadata adapter. Attachment bodies stay in Gmail."""
import base64
import re
from datetime import datetime, timezone
from email.utils import getaddresses
from email.message import Message
from html.parser import HTMLParser
from uuid import UUID

from app.core.errors import ServiceError


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        elif not self.hidden and tag in {'br', 'p', 'div', 'tr', 'li'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def message_metadata(raw):
    try:
        payload = raw.get('payload') or {}
        headers = {str(row['name']).casefold(): str(row['value']) for row in payload.get('headers', [])}
        attachments, texts, html_texts = [], [], []
        count = 0

        def visit(part, depth=0):
            nonlocal count
            count += 1
            if depth > 20 or count > 1000:
                raise ValueError('MIME limit')
            body = part.get('body') or {}
            filename = part.get('filename') or ''
            if filename or body.get('attachmentId'):
                attachments.append({'gmail_attachment_id': body.get('attachmentId'), 'part_id': part.get('partId'),
                    'filename': filename, 'mime_type': part.get('mimeType', 'application/octet-stream'),
                    'size_bytes': int(body.get('size', 0))})
            elif part.get('mimeType') in {'text/plain', 'text/html'} and body.get('data'):
                data = body['data']
                if len(data) > 1400000:
                    raise ValueError('Text limit')
                content_type = next((row['value'] for row in part.get('headers', [])
                                     if row.get('name', '').casefold() == 'content-type'), part['mimeType'])
                mime = Message()
                mime['Content-Type'] = content_type
                charset = mime.get_content_charset() or 'utf-8'
                decoded = base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True)
                try:
                    text = decoded.decode(charset, errors='replace')
                except LookupError:
                    text = decoded.decode('utf-8', errors='replace')
                if part['mimeType'] == 'text/plain':
                    texts.append(text)
                else:
                    parser = HtmlText()
                    parser.feed(text)
                    html_texts.append(''.join(parser.parts))
                if sum(map(len, texts + html_texts)) > 1000000:
                    raise ValueError('Text limit')
            for child in part.get('parts', []):
                visit(child, depth + 1)

        visit(payload)
        sender = [address.casefold() for _, address in getaddresses([headers.get('from', '')]) if address]
        recipients = [address.casefold() for _, address in getaddresses([headers.get('to', ''), headers.get('cc', '')]) if address]
        return {'gmail_message_id': raw['id'], 'gmail_thread_id': raw.get('threadId'),
            'received_at': datetime.fromtimestamp(int(raw['internalDate']) / 1000, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'subject': headers.get('subject', ''), 'sender': sender, 'recipients': recipients,
            'message_id_header': headers.get('message-id'), 'in_reply_to': headers.get('in-reply-to'),
            'references': headers.get('references'), 'body_text': '\n'.join(texts or html_texts),
            'attachments': attachments, 'source_type': 'GMAIL'}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise ServiceError('SOURCE_READ_FAILED', '메일 본문·메타데이터를 읽을 수 없습니다.', 502) from None


def determine_link(message, candidates):
    """Only unambiguous, server-verified reply evidence may link an existing case.

    The DB RPC rechecks ownership and all evidence again when committing the mail.
    A thread ID, similar subject or model confidence alone cannot establish a link.
    """
    verified = {}
    references = set(re.findall(r'<[^<>\s]+>', (message.get('in_reply_to') or '') + ' ' + (message.get('references') or '')))
    for row in candidates:
        if row.get('same_participants') is True and row.get('matched_reference') in references:
            case_id = str(UUID(row['case_id']))
            verified[case_id] = row
    if len(verified) == 1:
        case_id, row = next(iter(verified.items()))
        return {'kind': 'EXISTING_CASE', 'case_id': case_id, 'matched_reference': row['matched_reference']}
    if candidates or message.get('in_reply_to') or message.get('references'):
        return {'kind': 'AMBIGUOUS', 'case_id': None}
    return {'kind': 'NEW_MESSAGE', 'case_id': None}
