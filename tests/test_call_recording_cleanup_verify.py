import unittest
from unittest.mock import Mock

from app.features.call_recordings.worker import CallRecordingCleanupWorker


class CleanupVerificationTests(unittest.TestCase):
    def test_unlisted_files_are_verified_individually(self):
        repo = Mock()
        repo.expire_uploads.return_value = 0
        paths = ('removed.wav', 'absent.wav', 'still-there.wav', 'unknown.wav')
        repo.claim_deletions.return_value = [{'id': path, 'storage_path': path} for path in paths]
        storage = Mock()
        storage.remove.return_value = ['removed.wav']
        storage.object_size.side_effect = [None, 123, RuntimeError()]
        worker = CallRecordingCleanupWorker(repo, storage)

        self.assertEqual(worker.run_once(), {'expired': 0, 'deleted': 2})
        self.assertEqual([call.args[0] for call in storage.object_size.call_args_list], list(paths[1:]))
        self.assertEqual(
            [(call.args[0], *call.args[2:]) for call in repo.finish_deletion.call_args_list],
            [(paths[0], True, None), (paths[1], True, None),
             (paths[2], False, 'FILE_STILL_EXISTS'), (paths[3], False, 'RuntimeError')],
        )
