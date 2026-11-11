"""서버 로그 설정과 예외 원인 요약.

로그에는 고정 코드, ID, 예외 종류, 코드 위치, DB 오류 코드만 남긴다. 예외 메시지·메일 본문·주소·토큰은 남기지 않는다
(메시지에 입력값이 섞일 수 있다).
"""
import logging
import os
import re
import sys
import traceback
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]          # app/
LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s %(message)s'
# DB 오류 코드: Postgres SQLSTATE(5자리) 또는 PostgREST 코드(PGRST123)
_DB_CODE = re.compile(r'^(?:[0-9A-Z]{5}|PGRST\d{3})$')


def configure_logging(level: str | None = None) -> None:
    """'gyeonjwo' 로거에 표준 오류 출력 핸들러를 한 번만 붙인다. 수준은 level, 없으면 LOG_LEVEL 환경변수(기본 INFO).
    uvicorn은 자기 로거(uvicorn.*)만 설정하므로, 이것이 없으면 'gyeonjwo'의 INFO 로그(토큰 사용량, 워커 복구,
    허용 사용자 거부 사유)가 버려진다."""
    logger = logging.getLogger('gyeonjwo')
    logger.setLevel((level or os.environ.get('LOG_LEVEL') or 'INFO').upper())
    if not any(getattr(handler, '_gyeonjwo', False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler._gyeonjwo = True
        logger.addHandler(handler)


def _app_frames(tb) -> list[traceback.FrameSummary]:
    """traceback에서 app/ 아래 코드의 프레임만."""
    frames = traceback.extract_tb(tb) if tb is not None else []
    return [frame for frame in frames if _in_app(frame.filename)]


def _in_app(filename: str) -> bool:
    try:
        Path(filename).resolve().relative_to(APP_ROOT)
        return True
    except (ValueError, OSError):
        return False


def _where(frame: traceback.FrameSummary) -> str:
    module = Path(frame.filename).resolve().relative_to(APP_ROOT.parent).with_suffix('')
    return f"{'.'.join(module.parts)}.{frame.name}:{frame.lineno}"


def error_origin(error: BaseException) -> str:
    """예외 하나의 원인 요약 'where=<모듈.함수:줄> cause=<원래 예외 종류> db_code=<코드>'.
    저장소가 원인을 숨기고(from None) ServiceError로 바꾼 경우에도 __context__에 남은 원래 예외를 읽는다."""
    original = error.__cause__ or error.__context__
    frames = _app_frames((original or error).__traceback__) or _app_frames(error.__traceback__)
    parts = [f'where={_where(frames[-1]) if frames else "unknown"}']
    if original is not None:
        parts.append(f'cause={type(original).__name__}')
        code = getattr(original, 'code', None)
        if isinstance(code, str) and _DB_CODE.match(code):
            parts.append(f'db_code={code}')
    return ' '.join(parts)


def stack_summary(error: BaseException) -> str:
    """예외가 지나온 app/ 코드 위치 목록(메시지 없이). 예상하지 못한 예외의 로그에 붙인다."""
    frames = _app_frames(error.__traceback__)
    return ' > '.join(_where(frame) for frame in frames) or 'unknown'
