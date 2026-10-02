import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from drive_sync import DriveSync, SyncConfig, _md5_file


class DriveSyncTests(unittest.TestCase):
    def test_md5_file_matches_drive_checksum_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            path.write_bytes(b"book contents")
            self.assertEqual(
                hashlib.md5(b"book contents", usedforsecurity=False).hexdigest(),
                _md5_file(path),
            )

    def test_matching_drive_file_is_not_uploaded_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            path.write_bytes(b"book contents")
            checksum = _md5_file(path)
            service = Mock()
            sync = DriveSync(service, SyncConfig())
            remote_by_name = {
                path.name: [{"id": "existing-id", "md5Checksum": checksum}]
            }

            sync._sync_file(path, "books-folder-id", remote_by_name)

            service.files.assert_not_called()

    def test_book_name_collision_does_not_overwrite_drive_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            path.write_bytes(b"local book")
            service = Mock()
            sync = DriveSync(service, SyncConfig())

            with self.assertRaisesRegex(RuntimeError, "não será sobrescrito"):
                sync._sync_file(
                    path,
                    "books-folder-id",
                    {"book.epub": [{"id": "existing-id", "md5Checksum": "different"}]},
                )

            service.files.assert_not_called()

    def test_new_book_upload_uses_resumable_drive_upload_and_records_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new-book.epub"
            path.write_bytes(b"local book")
            checksum = _md5_file(path)
            service = Mock()
            upload_request = service.files.return_value.create.return_value
            upload_request.next_chunk.return_value = (
                None,
                {"id": "uploaded-id", "md5Checksum": checksum},
            )
            sync = DriveSync(service, SyncConfig())
            remote_by_name = {}

            with patch(
                "googleapiclient.http.MediaFileUpload", return_value="media"
            ) as media_upload:
                sync._sync_file(path, "books-folder-id", remote_by_name)

            media_upload.assert_called_once()
            self.assertTrue(media_upload.call_args.kwargs["resumable"])
            self.assertEqual(8 * 1024 * 1024, media_upload.call_args.kwargs["chunksize"])
            service.files.return_value.create.assert_called_once_with(
                body={"name": "new-book.epub", "parents": ["books-folder-id"]},
                media_body="media",
                supportsAllDrives=True,
                fields="id,name,md5Checksum",
            )
            self.assertEqual(
                [{"id": "uploaded-id", "name": "new-book.epub", "md5Checksum": checksum}],
                remote_by_name["new-book.epub"],
            )

    def test_sync_needs_only_the_books_folder_not_local_metadata_files(self):
        with tempfile.TemporaryDirectory() as directory:
            books_dir = Path(directory) / "Books"
            books_dir.mkdir()
            sync = DriveSync(
                Mock(),
                SyncConfig(books_dir=books_dir),
            )
            with patch.object(sync, "_sync_directory") as sync_directory:
                sync.sync_once()

            sync_directory.assert_called_once_with(books_dir, sync.config.drive_folder_id)


if __name__ == "__main__":
    unittest.main()
