"""통화 녹음 전사(STT)의 작업·결과 모양과 파일에서 읽는 값.

전사할 녹음은 DB(KAN-250)가 임대해 준다. 워커는 파일을 받아 STT를 부르고, 결과를 TranscriptionResult 하나로 넘긴다.
통화 시각은 파일 이름 → m4a 같은 MP4 계열 컨테이너의 생성 시각 → 없음 순서로 읽는다(업로드 시각은 통화 시각이 아니다).
"""
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
# OpenAI 음성 인식 API가 받는 파일 크기 상한. 버킷은 50MB까지 받으므로 넘는 파일은 전사하지 않고 실패로 남긴다
STT_MAX_BYTES = 25 * 1024 * 1024
# 삼성 통화 녹음 이름: "통화 녹음 <상대>_YYMMDD_HHMMSS.m4a" (기기 시각, 한국 시각으로 본다)
_FILE_NAME_TIME = re.compile(r'(?:^|\D)(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:\D[^/\\]*)?$')
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
_MP4_EXTENSIONS = {'m4a', '3gp', 'mp4'}


class TranscriptionError(Exception):
    """전사 실패. code는 DB에 남기는 실패 코드, retryable이면 다음 주기에 다시 시도한다."""

    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code, self.retryable = code, retryable


@dataclass(frozen=True)
class Segment:
    """발화 하나. speaker는 STT가 목소리로 나눈 이름표(A·B…)이고 화자 분리를 하지 않는 모델이면 None이다."""
    text: str
    speaker: str | None = None
    start: float | None = None
    end: float | None = None


@dataclass(frozen=True)
class TranscriptionJob:
    """DB가 임대해 준 전사 작업 하나. hint_*는 STT 문맥 설명용이라 없어도 된다."""
    job_id: str
    recording_id: str
    owner_user_id: str
    case_id: str
    supplier_id: str
    storage_path: str
    file_name: str
    extension: str
    size_bytes: int
    claim_token: str | None = None
    hint_item_name: str | None = None
    hint_supplier_name: str | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    """DB에 넘기는 전사 결과. body_text는 ①이 근거로 읽고 인용하는 본문이다."""
    segments: list[Segment]
    body_text: str
    model: str
    call_started_at: datetime | None
    call_time_source: str
    duration_seconds: float | None
    usage: dict = field(default_factory=dict)

    def payload(self) -> dict:
        """DB 함수에 넘기는 JSON."""
        return {'segments': [{'speaker': s.speaker, 'start': s.start, 'end': s.end, 'text': s.text} for s in self.segments],
                'body_text': self.body_text, 'model': self.model,
                'call_started_at': self.call_started_at.isoformat() if self.call_started_at else None,
                'call_time_source': self.call_time_source, 'duration_seconds': self.duration_seconds,
                'usage': self.usage}


def _clock(seconds: float | None) -> str:
    """발화 시각 표기(분:초)."""
    total = int(seconds or 0)
    return f'{total // 60:02d}:{total % 60:02d}'


def render_body(segments: list[Segment]) -> str:
    """①이 읽는 근거 본문. 화자가 있으면 한 줄에 발화 하나("[00:16] B: …"), 없으면 문장을 그대로 잇는다."""
    if not any(segment.speaker for segment in segments):
        return '\n'.join(segment.text.strip() for segment in segments if segment.text.strip())
    return '\n'.join(f'[{_clock(segment.start)}] {segment.speaker or "?"}: {segment.text.strip()}'
                     for segment in segments if segment.text.strip())


def call_time_from_file_name(file_name: str) -> datetime | None:
    """파일 이름 끝의 _YYMMDD_HHMMSS를 한국 시각의 통화 시작으로 읽는다. 날짜가 맞지 않으면 None."""
    match = _FILE_NAME_TIME.search(Path(file_name).stem)
    if not match:
        return None
    yy, mm, dd, hh, mi, ss = (int(value) for value in match.groups())
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss, tzinfo=KST)
    except ValueError:
        return None


def _boxes(handle, start: int, end: int):
    """MP4 상자(box)를 차례로 돌려준다: (종류, 내용 시작, 상자 끝)."""
    position = start
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(16)
        if len(header) < 8:
            return
        size, kind = struct.unpack('>I4s', header[:8])
        offset = 8
        if size == 1:
            if len(header) < 16:
                return
            size, offset = struct.unpack('>Q', header[8:16])[0], 16
        elif size == 0:
            size = end - position
        if size < offset:
            return
        yield kind.decode('latin1'), position + offset, position + size
        position += size


def mp4_times(path: Path) -> tuple[datetime | None, float | None]:
    """MP4 계열(m4a·3gp) 파일의 movie header(mvhd)에서 (생성 시각, 길이 초)를 읽는다. 없거나 읽지 못하면 (None, None)."""
    try:
        with open(path, 'rb') as handle:
            end = path.stat().st_size
            for kind, body, box_end in _boxes(handle, 0, end):
                if kind != 'moov':
                    continue
                for inner, inner_body, _ in _boxes(handle, body, box_end):
                    if inner != 'mvhd':
                        continue
                    handle.seek(inner_body)
                    version = handle.read(1)[0]
                    handle.seek(inner_body + 4)
                    if version == 1:
                        created, _, scale, length = struct.unpack('>QQIQ', handle.read(28))
                    else:
                        created, _, scale, length = struct.unpack('>IIII', handle.read(16))
                    when = _MP4_EPOCH + timedelta(seconds=created) if created else None
                    return when, (length / scale if scale else None)
    except (OSError, struct.error, IndexError, OverflowError):
        pass
    return None, None


def resolve_call_time(file_name: str, extension: str, path: Path) -> tuple[datetime | None, str, float | None]:
    """통화 시작 시각, 출처(FILE_NAME·CONTAINER·NONE), 녹음 길이(초).
    컨테이너 생성 시각은 녹음이 끝난 시각이라(삼성 녹음 확인) 길이를 빼서 시작 시각으로 본다."""
    created, duration = mp4_times(path) if extension in _MP4_EXTENSIONS else (None, None)
    named = call_time_from_file_name(file_name)
    if named is not None:
        return named, 'FILE_NAME', duration
    if created is not None:
        return created - timedelta(seconds=duration or 0), 'CONTAINER', duration
    return None, 'NONE', duration
