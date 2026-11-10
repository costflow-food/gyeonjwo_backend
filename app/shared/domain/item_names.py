"""품목명 띄어쓰기 무시 대조 (KAN-192). DB internal.item_name_norm과 같은 기준.

공백 종류(일반·탭·줄바꿈·NBSP·전각·폭 없는 공백)만 빼고 소문자로 바꾼다. 괄호·특수문자는 그대로 둔다.
'냉동 망고' == '냉동망고', '망고' != '냉동망고', '냉동망고(IQF)' != '냉동망고'.
"""
import re

_SPACES = re.compile(r'[\s\u00a0\u3000\u200b\ufeff]+')


def item_name_key(text) -> str:
    return _SPACES.sub('', str(text or '')).lower()


def contains_ignoring_spaces(haystack, needle) -> bool:
    """needle이 haystack 안에 있는지 띄어쓰기와 대소문자를 무시하고 본다."""
    key = item_name_key(needle)
    return bool(key) and key in item_name_key(haystack)


def spaced_ilike_pattern(text) -> str:
    """ilike 1차 조회용 패턴. 글자 사이 공백을 허용한다('냉동망고' → '%냉%동%망%고%').
    느슨하므로 결과는 contains_ignoring_spaces로 다시 거른다."""
    escaped = [ch.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_') for ch in item_name_key(text)]
    return '%' + '%'.join(escaped) + '%'
