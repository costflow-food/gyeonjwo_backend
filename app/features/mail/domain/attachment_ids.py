"""Opaque server IDs, independent of Gmail attachment IDs and filenames."""
from uuid import NAMESPACE_URL, uuid5


def attachment_id(mail_id, attachment, index):
    part = attachment.get('provider_part_ref', attachment.get('part_id'))
    identity = 'part:' + part if isinstance(part, str) else 'position:' + str(index)
    return str(uuid5(NAMESPACE_URL, f'urn:gyeonjwo:mail-attachment:v1:{mail_id}:{identity}'))
