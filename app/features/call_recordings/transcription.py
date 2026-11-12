"""Storage에서 음성을 읽고 STT 결과를 현재 임대에 한해 저장한다."""
import logging
from pathlib import PurePosixPath
import threading
from uuid import uuid4

import httpx

from app.features.call_recordings.domain import CONTENT_TYPES

logger = logging.getLogger('gyeonjwo.call_recordings')
LEASE_SECONDS = 300
POLL_SECONDS = 10
BATCH_SIZE = 8


class HttpTranscriber:
    def __init__(self, url, model, api_key=None, *, post=httpx.post):
        if httpx.URL(url).scheme != 'https':
            raise ValueError('STT endpoint must use HTTPS')
        self.url = url
        self.model = model
        self.headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        self.post = post

    def transcribe(self, path, audio):
        suffix = PurePosixPath(path).suffix
        response = self.post(
            self.url, headers=self.headers, data={'model': self.model},
            files={'file': (f'recording{suffix}', audio, CONTENT_TYPES[suffix[1:]])},
            timeout=120, follow_redirects=False,
        )
        response.raise_for_status()
        result = response.json()
        return result['text'], result.get('language')


class TranscriptionWorker:
    def __init__(self, repo, storage, transcriber, *, poll_seconds=POLL_SECONDS):
        self.repo = repo
        self.storage = storage
        self.transcriber = transcriber
        self.poll_seconds = poll_seconds
        self.worker_id = f'stt-{uuid4().hex}'
        self.stop = threading.Event()
        self.thread = None

    def process(self, recording_id):
        claims = self.repo.claim_transcription(recording_id, LEASE_SECONDS, self.worker_id)
        if claims == []:
            return
        if not isinstance(claims, list) or len(claims) != 1:
            raise ValueError('Invalid transcription claim')

        job = claims[0]
        path = job['storage_path']
        language = None
        failure = None
        try:
            transcript, language = self.transcriber.transcribe(path, self.storage.download(path))
            if not isinstance(transcript, str) or not transcript.strip() or '\x00' in transcript or len(transcript) > 200000:
                raise ValueError('Invalid STT transcript')
            if not isinstance(language, str) or '\x00' in language:
                language = None
        except Exception as error:
            logger.warning('call recording transcription failed type=%s', type(error).__name__)
            transcript = None
            failure = type(error).__name__

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
            try:
                self.run_once()
            except Exception as error:
                logger.warning('call recording transcription poll failed type=%s', type(error).__name__)
            self.stop.wait(self.poll_seconds)

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='call-recording-transcription', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
