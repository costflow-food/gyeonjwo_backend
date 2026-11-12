"""Storage에서 음성을 읽고 STT 결과를 현재 임대에 한해 저장한다.

- STT는 STT_URL·STT_MODEL(환경 변수)로 정한다. 화자 분리 모델(gpt-4o-transcribe-diarize)이면 diarized_json으로 받아
  발화마다 화자·시각을 붙인 근거 본문("[분:초] 화자: 말")을 저장하고, 그 밖의 모델은 받은 text를 그대로 저장한다.
- 파일은 임시 파일로 받아(메모리에 통째로 올리지 않음) STT에 스트리밍으로 보낸다. 25MB(OpenAI STT 상한)를 넘으면 부르지 않는다.
- 통화 시각은 파일 이름 → m4a 생성 시각 순서로 읽는다.
- DB가 통화 회신 연결(CALL_REPLY)을 제공하면 전사문 저장 전에 메일 회신과 같은 회신 자료로 넣는다. 연결이 실패하면 저장하지 않고
  임대가 끝나기를 기다려 다시 한다(연결은 같은 녹음을 한 번만 넣는다).
- 업로드 완료 API가 wake()로 깨운다. 깨우지 않아도 poll_seconds마다 남은 녹음을 가져간다.
"""
import logging
from pathlib import Path, PurePosixPath
import tempfile
import threading
from uuid import uuid4

import httpx

from app.features.call_recordings.call_transcript import (
    STT_MAX_BYTES, Segment, TranscriptionError, render_body, resolve_call_time)
from app.features.call_recordings.domain import CONTENT_TYPES

logger = logging.getLogger('gyeonjwo.call_recordings')
# 녹음 하나의 임대 시간. 25MB 녹음을 화자 분리 STT로 옮기는 데 수 분이 걸릴 수 있다(DB 상한 3600초)
LEASE_SECONDS = 1800
POLL_SECONDS = 10
BATCH_SIZE = 8
STT_TIMEOUT_SECONDS = 300
DIARIZE_MODELS = frozenset({'gpt-4o-transcribe-diarize'})


class HttpTranscriber:
    def __init__(self, url, model, api_key=None, *, post=httpx.post):
        if httpx.URL(url).scheme != 'https':
            raise ValueError('STT endpoint must use HTTPS')
        self.url = url
        self.model = model
        self.headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        self.post = post

    @property
    def diarize(self):
        """화자 분리 모델인지."""
        return self.model in DIARIZE_MODELS

    def transcribe(self, path, audio):
        """audio(파일 객체 또는 바이트)를 전사한다. 반환: (근거 본문, 언어).
        화자 분리 모델이면 발화 목록을 "[분:초] 화자: 말" 줄로, 아니면 받은 text를 그대로 돌려준다."""
        suffix = PurePosixPath(path).suffix
        data = {'model': self.model}
        if self.diarize:
            data |= {'response_format': 'diarized_json', 'chunking_strategy': 'auto', 'language': 'ko'}
        response = self.post(
            self.url, headers=self.headers, data=data,
            files={'file': (f'recording{suffix}', audio, CONTENT_TYPES[suffix[1:]])},
            timeout=STT_TIMEOUT_SECONDS, follow_redirects=False,
        )
        response.raise_for_status()
        result = response.json()
        segments = [Segment(text=str(item.get('text') or '').strip(), speaker=item.get('speaker'),
                            start=item.get('start'), end=item.get('end'))
                    for item in result.get('segments') or [] if isinstance(item, dict)]
        body = render_body(segments) if self.diarize and any(s.text for s in segments) else result['text']
        return body, result.get('language') or ('ko' if self.diarize else None)


class TranscriptionWorker:
    def __init__(self, repo, storage, transcriber, *, poll_seconds=POLL_SECONDS):
        self.repo = repo
        self.storage = storage
        self.transcriber = transcriber
        self.poll_seconds = poll_seconds
        self.worker_id = f'stt-{uuid4().hex}'
        self.stop = threading.Event()
        self.signal = threading.Event()
        self.thread = None

    def wake(self):
        """업로드 완료 직후 다음 주기를 기다리지 않고 바로 처리하게 한다."""
        self.signal.set()

    def transcribe(self, recording, path):
        """녹음 파일을 받아 통화 시각을 읽고 STT를 부른다. 반환: (근거 본문, 언어, 통화 시작 시각)."""
        if int(recording.get('size_bytes') or 0) > STT_MAX_BYTES:
            raise TranscriptionError('STT_FILE_TOO_LARGE')
        with tempfile.TemporaryDirectory(prefix='call-stt-') as folder:
            local = Path(folder) / f"recording.{recording['extension']}"
            self.storage.download_to(path, local)
            if local.stat().st_size > STT_MAX_BYTES:
                raise TranscriptionError('STT_FILE_TOO_LARGE')
            started_at, _, _ = resolve_call_time(recording['file_name'], recording['extension'], local)
            with open(local, 'rb') as audio:
                transcript, language = self.transcriber.transcribe(path, audio)
        return transcript, language, started_at

    def process(self, recording_id):
        claims = self.repo.claim_transcription(recording_id, LEASE_SECONDS, self.worker_id)
        if claims == []:
            return
        if not isinstance(claims, list) or len(claims) != 1:
            raise ValueError('Invalid transcription claim')

        job = claims[0]
        path = job['storage_path']
        recording = self.repo.recording(recording_id)
        if recording is None:
            raise ValueError('Claimed recording missing')
        language = None
        failure = None
        started_at = None
        try:
            transcript, language, started_at = self.transcribe(recording, path)
            if not isinstance(transcript, str) or not transcript.strip() or '\x00' in transcript or len(transcript) > 200000:
                raise TranscriptionError('STT_NO_SPEECH')
            if not isinstance(language, str) or '\x00' in language:
                language = None
        except Exception as error:
            logger.warning('call recording transcription failed type=%s', type(error).__name__)
            transcript = language = None
            failure = error.code if isinstance(error, TranscriptionError) else type(error).__name__

        if transcript is not None and self.repo.call_reply_ready():
            # 실패하면 예외로 나가 저장하지 않는다. 임대가 끝나면 다시 가져가 같은 녹음으로 다시 연결한다
            self.repo.register_call_reply(recording, transcript, started_at)
        # A DB transport failure leaves the lease available for recovery on expiry.
        stored = self.repo.finish_transcription(recording_id, job['lease_token'], transcript,
                                                language=language, model=self.transcriber.model, error=failure)
        if stored is False:
            logger.info('call recording transcription result discarded after lease loss')
        elif stored is not True:
            raise ValueError('Invalid transcription result acknowledgement')

    def run_once(self):
        for recording_id in self.repo.transcription_candidates(BATCH_SIZE):
            if self.stop.is_set():
                break
            try:
                self.process(recording_id)
            except Exception as error:
                logger.warning('call recording transcription job unavailable type=%s', type(error).__name__)

    def loop(self):
        while not self.stop.is_set():
            self.signal.clear()
            try:
                self.run_once()
            except Exception as error:
                logger.warning('call recording transcription poll failed type=%s', type(error).__name__)
            self.signal.wait(self.poll_seconds)

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='call-recording-transcription', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.signal.set()
        if self.thread:
            self.thread.join(timeout=5)
