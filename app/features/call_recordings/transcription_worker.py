"""통화 녹음 전사 워커: 업로드가 끝난 녹음을 STT로 글자로 옮겨 DB(KAN-250)에 저장하고, 통화 회신으로 연결한다.

- 어떤 녹음을 처리할지는 DB(call_recordings의 stt_* 컬럼)가 임대로 정한다. 재시작·여러 서버에서도 같은 녹음을 두 번
  전사하지 않고, 임대가 끝난 작업은 다시 가져간다(빠뜨리지 않는다). 실패하면 사유를 저장하고, 다시 시도할지는 DB가
  시도 횟수(5번)로 정한다.
- 업로드 완료 API가 wake()로 깨운다. 깨우지 않아도 주기마다 남은 작업을 가져간다.
- 파일은 임시 파일로 받고 끝나면 지운다. 실패하면 실패만 기록하고 음성 파일은 그대로 둔다(재시도할 수 있게).
- 통화 회신 연결(CALL_REPLY)이 준비된 DB면 전사문 저장 전에 회신 자료로 넣는다. 연결이 실패하면 저장하지 않고
  재시도로 남긴다(연결은 같은 녹음을 한 번만 넣으므로 다시 불러도 된다).
"""
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path

from app.features.call_recordings.transcription import (
    STT_MAX_BYTES, TranscriptionError, TranscriptionResult, render_body, resolve_call_time)

logger = logging.getLogger('gyeonjwo.call_recordings')
CYCLE_SECONDS = 60
# 녹음 하나의 임대 시간. 25MB 녹음을 화자 분리 STT로 옮기는 데 수 분이 걸릴 수 있다(KAN-250 상한 3600초)
LEASE_SECONDS = 1800


class CallTranscriptionWorker:
    def __init__(self, repo, storage, transcriber, *, cycle_seconds=CYCLE_SECONDS):
        """repo: CallTranscriptionRepository, storage: CallRecordingStorage, transcriber: OpenAiTranscriber."""
        self.repo, self.storage, self.transcriber, self.cycle_seconds = repo, storage, transcriber, cycle_seconds
        self.worker_id = f'{socket.gethostname()[:80]}:{os.getpid()}:stt'.replace(' ', '_')
        self.stop = threading.Event()
        self.signal = threading.Event()
        self.thread = None

    def wake(self):
        """업로드 완료 직후 다음 주기를 기다리지 않고 바로 처리하게 한다."""
        self.signal.set()

    def run_once(self):
        """작업을 하나씩 임대해 처리한다. 처리한 수와 실패 수를 돌려준다."""
        done = failed = 0
        while not self.stop.is_set():
            jobs = self.repo.claim_transcriptions(self.worker_id, 1, LEASE_SECONDS)
            if not jobs:
                break
            for job in jobs:
                if self.process(job):
                    done += 1
                else:
                    failed += 1
        return {'done': done, 'failed': failed}

    def process(self, job):
        """녹음 하나를 전사하고 저장한다. 성공하면 True, 실패를 기록했으면 False."""
        try:
            result = self.transcribe(job)
            if self.repo.call_reply_ready():
                self.repo.register_call_reply(job, result)
            if self.repo.finish_transcription(job, self.worker_id, result) is False:
                # 임대가 끝나 다른 워커가 가져갔다. 통화 회신 연결은 같은 녹음을 한 번만 넣으므로 그대로 둔다
                logger.warning('call_recording transcription_lease_lost recording=%s', job.recording_id)
                return False
            logger.info('call_recording transcribed recording=%s model=%s segments=%d', job.recording_id,
                        result.model, len(result.segments))
            return True
        except TranscriptionError as error:
            code, retryable = error.code, error.retryable
        except Exception as error:
            code, retryable = 'STT_FAILED', True
            logger.warning('call_recording transcription_failed recording=%s error=%s', job.recording_id,
                           type(error).__name__)
        try:
            self.repo.fail_transcription(job, self.worker_id, code, retryable)
        except Exception:
            logger.warning('call_recording transcription_fail_record_failed recording=%s', job.recording_id)
        return False

    def transcribe(self, job) -> TranscriptionResult:
        """파일을 받아 통화 시각을 읽고 STT를 부른다. 25MB를 넘으면 STT가 받지 않으므로 바로 실패로 둔다."""
        if job.size_bytes > STT_MAX_BYTES:
            raise TranscriptionError('STT_FILE_TOO_LARGE', False)
        with tempfile.TemporaryDirectory(prefix='call-stt-') as folder:
            path = Path(folder) / f'recording.{job.extension}'
            self.storage.download_to(job.storage_path, path)
            if path.stat().st_size > STT_MAX_BYTES:
                raise TranscriptionError('STT_FILE_TOO_LARGE', False)
            started_at, source, duration = resolve_call_time(job.file_name, job.extension, path)
            segments, stt_duration, usage = self.transcriber.transcribe(
                path, job.file_name, item_name=job.hint_item_name, supplier_name=job.hint_supplier_name)
        return TranscriptionResult(segments=segments, body_text=render_body(segments), model=self.transcriber.model,
                                   call_started_at=started_at, call_time_source=source,
                                   duration_seconds=duration or stt_duration, usage=usage)

    def loop(self):
        """주기마다(또는 깨우면 바로) 남은 작업을 처리한다. 오류가 나도 워커는 멈추지 않는다."""
        while not self.stop.is_set():
            self.signal.clear()
            try:
                result = self.run_once()
                if result['done'] or result['failed']:
                    logger.info('call_recording transcription done=%d failed=%d', result['done'], result['failed'])
            except Exception:
                logger.warning('call_recording transcription_cycle_failed')
            self.signal.wait(self.cycle_seconds)

    def start(self):
        """백그라운드 스레드로 시작한다."""
        self.thread = threading.Thread(target=self.loop, name='call-recording-transcription', daemon=True)
        self.thread.start()

    def close(self):
        """멈추고 스레드가 끝나기를 잠깐 기다린다. 처리 중이던 작업은 임대가 끝나면 DB가 다시 내준다."""
        self.stop.set()
        self.signal.set()
        if self.thread:
            self.thread.join(timeout=5)
