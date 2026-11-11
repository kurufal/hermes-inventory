import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.homebox import complete_entity


class AuthenticationTests(unittest.TestCase):
	def test_validation_uses_get_and_bearer_authentication(self):
		from inventory.homebox import get_entity_types
		with patch("inventory.homebox.homebox_url", return_value="http://homebox:3100"), patch("inventory.homebox.homebox_api_key", return_value="hb_test-only"), patch("inventory.homebox.requests.get") as request:
			request.return_value.json.return_value = []
			self.assertEqual(get_entity_types(), [])
		request.assert_called_once_with("http://homebox:3100/api/v1/entity-types", headers={"Authorization": "Bearer hb_test-only"}, timeout=30)
		request.return_value.raise_for_status.assert_called_once_with()


class AttachmentSyncTests(unittest.TestCase):
	def test_retry_skips_recorded_hash_and_uploads_only_missing_file(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			images = Path(temporary_directory)
			(images / "a.jpg").write_bytes(b"a")
			(images / "b.jpg").write_bytes(b"b")
			record = {"source_images": ["a.jpg", "b.jpg"], "image_hashes": [{"filename": "a.jpg", "sha256": "hash-a"}, {"filename": "b.jpg", "sha256": "hash-b"}]}
			known = {"hash-a": {"filename": "a.jpg", "sha256": "hash-a", "result": {"id": "a"}, "primary": False}}
			persisted = []
			with patch("inventory.homebox.update_entity", return_value={"id": "entity-1"}), patch("inventory.homebox.upload_attachment", return_value={"id": "b"}) as upload:
				result = complete_entity("entity-1", record, image_directory=images, attachment_sync=known, on_attachment_uploaded=persisted.append)
			upload.assert_called_once_with("entity-1", images / "b.jpg")
			self.assertEqual([entry["sha256"] for entry in result["attachments"]], ["hash-a", "hash-b"])
			self.assertEqual(persisted[0]["sha256"], "hash-b")
			self.assertTrue(all(entry["primary"] is False for entry in result["attachments"]))