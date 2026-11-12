"""통화 녹음 정리 워커: 삭제 뒤 파일이 실제로 없어졌는지 확인한다."""
import unittest

from app.features.call_recordings.worker import CallRecordingCleanupWorker


class Repo:
    def __init__(self, jobs):
        self.jobs, self.finished = jobs, []

    def expire_uploads(self, limit):
        return 0

    def claim_deletions(self, worker, limit, lease):
        jobs, self.jobs = self.jobs, []
        return jobs

    def finish_deletion(self, deletion_id, worker, ok, error=None):
        self.finished.append((deletion_id, ok, error))


class Storage:
    """remove 는 keep 에 없는 파일만 지운다. Storage 삭제 API 처럼 지우지 못해도 예외를 내지 않는다."""

    def __init__(self, objects, keep=(), broken_check=False):
        self.objects, self.keep, self.broken_check, self.remove_calls = dict(objects), set(keep), broken_check, []

    def remove(self, paths):
        self.remove_calls.append(list(paths))
        for path in paths:
            if path not in self.keep:
                self.objects.pop(path, None)

    def object_size(self, path):
        if self.broken_check:
            raise RuntimeError('storage down')
        return self.objects.get(path)


def jobs(*names):
    return [{'id': name, 'storage_path': f'o/c/{name}.m4a'} for name in names]


class CleanupVerifyTests(unittest.TestCase):
    def test_deleted_files_are_confirmed_gone(self):
        repo, storage = Repo(jobs('a', 'b')), Storage({'o/c/a.m4a': 10, 'o/c/b.m4a': 20, 'o/c/other.m4a': 30})
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 0, 'deleted': 2})
        self.assertEqual(storage.remove_calls, [['o/c/a.m4a', 'o/c/b.m4a']])
        self.assertEqual(storage.objects, {'o/c/other.m4a': 30})
        self.assertEqual(repo.finished, [('a', True, None), ('b', True, None)])

    def test_file_left_behind_is_reported_for_retry(self):
        repo = Repo(jobs('a', 'b'))
        storage = Storage({'o/c/a.m4a': 10, 'o/c/b.m4a': 20}, keep={'o/c/b.m4a'})
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 0, 'deleted': 1})
        self.assertEqual(repo.finished, [('a', True, None), ('b', False, 'FILE_STILL_EXISTS')])

    def test_missing_file_counts_as_deleted(self):
        repo, storage = Repo(jobs('gone')), Storage({})
        self.assertEqual(CallRecordingCleanupWorker(repo, storage).run_once(), {'expired': 0, 'deleted': 1})
        self.assertEqual(repo.finished, [('gone', True, None)])

    def test_unverifiable_delete_is_reported_for_retry(self):
        repo, storage = Repo(jobs('a')), Storage({'o/c/a.m4a': 10}, broken_check=True)
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 0, 'deleted': 0})
        self.assertEqual(repo.finished, [('a', False, 'RuntimeError')])


if __name__ == '__main__':
    unittest.main()
