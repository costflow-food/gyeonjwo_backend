"""통화 녹음 정리 워커: 기한 지난 업로드를 실패 처리하고, 삭제 대기열의 Storage 파일을 지운다.

업무 수용(삭제)·업로드 실패로 생긴 파일은 DB 가 대기열에 넣는다. 발급된 서명 URL 이 모두 끝난 뒤(not_before)에만
꺼내 주므로 늦은 업로드가 고아 파일을 남기지 않는다. 재시도 간격·최대 횟수는 SQL 함수가 정한다.
"""
import logging
import os
import socket
import threading

logger = logging.getLogger('gyeonjwo.call_recordings')
CYCLE_SECONDS = 60


class CallRecordingCleanupWorker:
    def __init__(self, repo, storage, *, cycle_seconds=CYCLE_SECONDS):
        self.repo, self.storage, self.cycle_seconds = repo, storage, cycle_seconds
        self.worker_id = f'{socket.gethostname()[:80]}:{os.getpid()}'.replace(' ', '_')
        self.stop = threading.Event()
        self.thread = None

    def release_extracted_audio(self):
        """견적 추출이 끝난 통화의 음성 파일을 삭제 대기열에 넣는다. DB가 통화 회신 연결(CALL_REPLY)을 제공할 때만."""
        ready = getattr(self.repo, 'call_reply_ready', None)
        if ready is None or not ready():
            return 0
        released = self.repo.release_extracted_audio(20)
        return released if type(released) is int else 0

    def run_once(self):
        try:
            self.release_extracted_audio()
        except Exception:
            logger.warning('call_recording audio_release_failed')
        expired = self.repo.expire_uploads(100)
        jobs = self.repo.claim_deletions(self.worker_id, 20, 120)
        if not isinstance(jobs, list):
            raise ValueError('Invalid deletion claim')
        if jobs:
            try:
                self.storage.remove([job['storage_path'] for job in jobs])
                ok, error = True, None
            except Exception as failure:
                ok, error = False, type(failure).__name__
                logger.warning('call_recording file_delete_failed count=%d', len(jobs))
            for job in jobs:
                self.repo.finish_deletion(job['id'], self.worker_id, ok, error)
        return {'expired': expired or 0, 'deleted': len(jobs) if jobs else 0}

    def loop(self):
        while not self.stop.is_set():
            try:
                result = self.run_once()
                if result['expired'] or result['deleted']:
                    logger.info('call_recording cleanup expired=%d deleted=%d', result['expired'], result['deleted'])
            except Exception:
                logger.warning('call_recording cleanup_failed')
            self.stop.wait(self.cycle_seconds)

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='call-recording-cleanup', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
