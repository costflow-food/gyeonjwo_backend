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
    """remove 는 keep 에 없는 파일만 지우고 지운 경로를 돌려준다. Storage 삭제 API 처럼 지우지 못해도 예외를 내지 않는다."""

    def __init__(self, objects, keep=(), broken_check=False, silent=False):
        self.objects, self.keep, self.broken_check, self.silent = dict(objects), set(keep), broken_check, silent
        self.remove_calls, self.checked = [], []

    def remove(self, paths):
        self.remove_calls.append(list(paths))
        removed = [path for path in paths if path not in self.keep and self.objects.pop(path, None) is not None]
        return None if self.silent else removed

    def object_size(self, path):
        self.checked.append(path)
        if self.broken_check:
            raise RuntimeError('storage down')
        return self.objects.get(path)


def jobs(*names):
    return [{'id': name, 'storage_path': f'o/c/{name}.m4a'} for name in names]


class BucketStub:
    def __init__(self, response):
        self.response, self.calls = response, []

    def remove(self, paths):
        self.calls.append(paths)
        return self.response


class StorageRemoveTests(unittest.TestCase):
    def storage(self, response):
        from app.features.call_recordings.infrastructure import CallRecordingStorage
        storage = CallRecordingStorage.__new__(CallRecordingStorage)
        storage.bucket = BucketStub(response)
        return storage

    def test_returns_names_storage_reports_removed(self):
        storage = self.storage([{'name': 'o/c/a.m4a', 'bucket_id': 'call-recordings'}, {'name': 'o/c/b.m4a'}])
        self.assertEqual(storage.remove(('o/c/a.m4a', 'o/c/b.m4a', 'o/c/x.m4a')), ['o/c/a.m4a', 'o/c/b.m4a'])
        self.assertEqual(storage.bucket.calls, [['o/c/a.m4a', 'o/c/b.m4a', 'o/c/x.m4a']])

    def test_empty_or_unexpected_response_means_nothing_confirmed(self):
        for response in ([], None, {'message': 'ok'}, ['o/c/a.m4a'], [{'bucket_id': 'call-recordings'}]):
            self.assertEqual(self.storage(response).remove(['o/c/a.m4a']), [])


class CleanupVerifyTests(unittest.TestCase):
    def test_deleted_files_are_confirmed_gone(self):
        repo, storage = Repo(jobs('a', 'b')), Storage({'o/c/a.m4a': 10, 'o/c/b.m4a': 20, 'o/c/other.m4a': 30})
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 0, 'deleted': 2})
        self.assertEqual(storage.remove_calls, [['o/c/a.m4a', 'o/c/b.m4a']])
        self.assertEqual(storage.objects, {'o/c/other.m4a': 30})
        self.assertEqual(repo.finished, [('a', True, None), ('b', True, None)])
        self.assertEqual(storage.checked, [])  # 삭제 응답에 있는 파일은 다시 확인하지 않는다

    def test_file_left_behind_is_reported_for_retry(self):
        repo = Repo(jobs('a', 'b'))
        storage = Storage({'o/c/a.m4a': 10, 'o/c/b.m4a': 20}, keep={'o/c/b.m4a'})
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 0, 'deleted': 1})
        self.assertEqual(repo.finished, [('a', True, None), ('b', False, 'FILE_STILL_EXISTS')])
        self.assertEqual(storage.checked, ['o/c/b.m4a'])

    def test_missing_file_counts_as_deleted(self):
        repo, storage = Repo(jobs('gone')), Storage({})
        self.assertEqual(CallRecordingCleanupWorker(repo, storage).run_once(), {'expired': 0, 'deleted': 1})
        self.assertEqual(repo.finished, [('gone', True, None)])

    def test_delete_without_removed_list_is_verified_per_file(self):
        repo, storage = Repo(jobs('a', 'b')), Storage({'o/c/a.m4a': 10}, silent=True)
        self.assertEqual(CallRecordingCleanupWorker(repo, storage).run_once(), {'expired': 0, 'deleted': 2})
        self.assertEqual(storage.checked, ['o/c/a.m4a', 'o/c/b.m4a'])

    def test_unverifiable_delete_is_reported_for_retry(self):
        repo, storage = Repo(jobs('a')), Storage({'o/c/a.m4a': 10}, keep={'o/c/a.m4a'}, broken_check=True)
        result = CallRecordingCleanupWorker(repo, storage).run_once()
        self.assertEqual(result, {'expired': 0, 'deleted': 0})
        self.assertEqual(repo.finished, [('a', False, 'RuntimeError')])


if __name__ == '__main__':
    unittest.main()
