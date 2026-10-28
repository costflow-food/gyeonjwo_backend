import hashlib
import re

WORK_ID = re.compile(r'W-[0-9]{8}-[0-9]{3,}\Z')
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
MAX_BYTES = 52_428_800


def mail_path(work_id: str) -> str:
    if not WORK_ID.fullmatch(work_id):
        raise ValueError('Invalid work ID')
    return f'{work_id}/mail.eml'


def attachment_path(work_id: str, safe_name: str) -> str:
    mail_path(work_id)
    if not SAFE_NAME.fullmatch(safe_name) or safe_name in ('.', '..', 'mail.eml'):
        raise ValueError('Invalid attachment filename')
    return f'{work_id}/{safe_name}'


class SourceStorage:
    def __init__(self, client):
        self.bucket = client.storage.from_('gyeonjwo-files')

    def upload_mail(self, work_id: str, content: bytes) -> dict:
        path = mail_path(work_id)
        if not 0 < len(content) <= MAX_BYTES:
            raise ValueError('Mail must contain between 1 byte and 50 MiB')
        # Never overwrite an existing source. Registration follows successful byte verification.
        self.bucket.upload(path, content, {'content-type': 'message/rfc822', 'upsert': 'false'})
        self.download_verified(path, len(content), hashlib.sha256(content).hexdigest())
        return {'raw_size_bytes': len(content), 'raw_sha256': hashlib.sha256(content).hexdigest()}

    def upload_attachment(self, work_id, name, original_name, content_type, content):
        path = attachment_path(work_id, name)
        if len(content) > MAX_BYTES:
            raise ValueError('Attachment exceeds 50 MiB')
        checksum = hashlib.sha256(content).hexdigest()
        self.bucket.upload(path, content, {'content-type': content_type, 'upsert': 'false'})
        self.download_verified(path, len(content), checksum)
        return {'original_name': original_name, 'storage_path': path, 'content_type': content_type,
                'size_bytes': len(content), 'sha256': checksum}

    def download_verified(self, path: str, expected_size: int, expected_sha256: str) -> bytes:
        parts = path.split('/')
        if len(parts) != 2:
            raise ValueError('Source path must have exactly two components')
        if parts[1] == 'mail.eml':
            mail_path(parts[0])
        else:
            attachment_path(*parts)
        minimum_size = 1 if parts[1] == 'mail.eml' else 0
        if type(expected_size) is not int or not minimum_size <= expected_size <= MAX_BYTES or not re.fullmatch('[a-f0-9]{64}', expected_sha256):
            raise ValueError('Invalid source metadata')
        content = self.bucket.download(path)
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError('Source byte size or SHA-256 mismatch')
        return content
