"""통화 녹음 정리 워커: 기한 지난 업로드를 실패 처리하고, 삭제 대기열의 Storage 파일을 지운다.

업무 수용(삭제)·업로드 실패로 생긴 파일은 DB 가 대기열에 넣는다. 발급된 서명 URL 이 모두 끝난 뒤(not_before)에만
꺼내 주므로 늦은 업로드가 고아 파일을 남기지 않는다. 재시도 간격·최대 횟수는 SQL 함수가 정한다.

Storage 삭제 API 는 지운 파일 목록만 돌려주고, 지우지 못한 파일이 있어도 200 을 준다. 목록에 있는 파일은 지워진
것으로 보고, 목록에 없는 파일은 원래 없었는지 남아 있는지 한 번 더 확인한다. 남아 있으면 실패로 알려 다시 시도하게 한다.
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

    def run_once(self):
        expired = self.repo.expire_uploads(100)
        jobs = self.repo.claim_deletions(self.worker_id, 20, 120)
        if not isinstance(jobs, list):
            raise ValueError('Invalid deletion claim')
        deleted = 0
        if jobs:
            removed, error = set(), None
            try:
                removed = set(self.storage.remove([job['storage_path'] for job in jobs]) or ())
            except Exception as failure:
                error = type(failure).__name__
                logger.warning('call_recording file_delete_failed count=%d', len(jobs))
            for job in jobs:
                path = job['storage_path']
                job_error = error or (None if path in removed else self._still_exists(path))
                self.repo.finish_deletion(job['id'], self.worker_id, job_error is None, job_error)
                deleted += job_error is None
        return {'expired': expired or 0, 'deleted': deleted}

    def _still_exists(self, path):
        """삭제 응답에 없던 파일 확인. 파일이 없으면 None, 남아 있거나 확인하지 못하면 실패 사유."""
        try:
            if self.storage.object_size(path) is None:
                return None
        except Exception as failure:
            logger.warning('call_recording file_delete_unverified')
            return type(failure).__name__
        logger.warning('call_recording file_still_exists')
        return 'FILE_STILL_EXISTS'

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
