"""Opaque server IDs, independent of Gmail attachment IDs and filenames."""
from uuid import NAMESPACE_URL, uuid5


def part_reference(attachment):
    # Migrated metadata may contain a null new key alongside a valid legacy key.
    # Empty string is a valid root MIME part ID and must not be dropped by `or`.
    for key in ('provider_part_ref', 'part_id'):
        value = attachment.get(key)
        if isinstance(value, str):
            return value
    return None


def attachment_id(mail_id, attachment, index):
    part = part_reference(attachment)
    identity = 'part:' + part if isinstance(part, str) else 'position:' + str(index)
    return str(uuid5(NAMESPACE_URL, f'urn:gyeonjwo:mail-attachment:v1:{mail_id}:{identity}'))
