import hashlib

from app.shared.infrastructure.db.storage import MAX_BYTES
from app.core.errors import ServiceError


def reference_path(source_ref):
    path = source_ref.split('#', 1)[0]
    if (not path or path.startswith('/') or '\\' in path or '://' in path or '\x00' in path
            or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ServiceError('INVALID_REFERENCE_PATH', '기준자료 원문 경로가 올바르지 않습니다.', 502)
    return path


def reference_id(source_ref):
    return hashlib.sha256(source_ref.encode('utf-8')).hexdigest()


def download_reference(client, source_ref):
    path = reference_path(source_ref)
    try:
        content = client.storage.from_('gyeonjwo-reference-files').download(path)
        if not isinstance(content, bytes) or len(content) > MAX_BYTES:
            raise ValueError('Invalid reference bytes')
        return content
    except Exception:
        raise ServiceError('SOURCE_READ_FAILED', '기준자료 원문을 읽을 수 없습니다.', 502) from None
