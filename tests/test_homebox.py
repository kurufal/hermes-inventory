import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.homebox import NOTES_START, NotesMigrationRequired, _legacy_notes, build_notes, complete_entity, merge_notes, split_notes


class NotesOwnershipTests(unittest.TestCase):
	def test_delimited_metadata_and_user_text_round_trip(self):
		record = {"identifiers": {"isbn": ["123"]}, "notes": "My notes\nKeep this."}
		notes = build_notes(record)
		self.assertIn(NOTES_START, notes)
		self.assertEqual(split_notes(notes, record), record["notes"])
		self.assertEqual(split_notes(notes.replace("My notes", "Edited in Homebox"), record), "Edited in Homebox\nKeep this.")

	def test_existing_freeform_notes_are_not_overwritten(self):
		self.assertEqual(split_notes(merge_notes("Manual Homebox text", {}), {}), "Manual Homebox text")
		with self.assertRaises(NotesMigrationRequired):
			merge_notes("Homebox text", {"notes": "Different local text"})

	def test_verified_legacy_notes_can_migrate_including_manual_user_section(self):
		record = {"identifiers": {"isbn": ["123"]}, "notes": "Local notes"}
		legacy = _legacy_notes(record)
		self.assertEqual(split_notes(legacy, record), "Local notes")
		self.assertEqual(split_notes(legacy.replace("Local notes", "Manual edit"), record), "Manual edit")
		with self.assertRaises(NotesMigrationRequired):
			split_notes(legacy.replace("isbn: 123", "isbn: manually changed"), record)

	def test_malformed_or_unsupported_delimiters_are_blocked(self):
		for notes in (NOTES_START, build_notes({}) + NOTES_START, "<!-- hermes-inventory:metadata:v2:start -->"):
			with self.subTest(notes=notes), self.assertRaises(NotesMigrationRequired):
				split_notes(notes, {})

	def test_explicit_local_note_edit_requires_unchanged_previous_user_text(self):
		record = {"notes": "New notes", "previous_notes": "Old notes"}
		self.assertEqual(split_notes(merge_notes(build_notes({"notes": "Old notes"}), record), {}), "New notes")
		with self.assertRaises(NotesMigrationRequired):
			merge_notes(build_notes({"notes": "Concurrent Homebox edit"}), record)

	def test_line_endings_do_not_change_user_notes_identity(self):
		self.assertEqual(split_notes(build_notes({"notes": "User\nText"}).replace("\n", "\r\n"), {}), "User\nText")

	def test_cleared_homebox_notes_are_not_silently_restored(self):
		with self.assertRaises(NotesMigrationRequired):
			merge_notes("", {"notes": "Old local text"}, linked=True)
		with self.assertRaises(NotesMigrationRequired):
			merge_notes(build_notes({}), {"notes": "Old local text"})
		self.assertEqual(split_notes(merge_notes("", {"notes": "Initial notes"}), {}), "Initial notes")


class ConditionalSyncWriteTests(unittest.TestCase):
	def test_put_preserves_unrelated_values_and_sends_if_match(self):
		from inventory.homebox import put_entity_fields
		current = {"id": "entity-1", "name": "Old", "notes": "Manual notes", "entityType": {"id": "item"}, "tags": [{"id": "tag"}], "location": {"id": "shelf"}, "fields": [{"name": "Custom", "textValue": "Keep"}], "soldNotes": "Do not remove"}
		with patch("inventory.homebox.get_entity_snapshot", return_value=(current, '"revision-1"')), patch("inventory.homebox.homebox_url", return_value="http://homebox"), patch("inventory.homebox.homebox_api_key", return_value="test-only"), patch("inventory.homebox.requests.put") as put:
			put_entity_fields("entity-1", {"name": "New"}, expected=current, etag='"revision-1"')
		payload = put.call_args.kwargs["json"]
		self.assertEqual(payload["notes"], "Manual notes")
		self.assertEqual(payload["fields"], current["fields"])
		self.assertEqual(payload["soldNotes"], "Do not remove")
		self.assertEqual(payload["locationId"], "shelf")
		self.assertEqual(put.call_args.kwargs["headers"]["If-Match"], '"revision-1"')

	def test_missing_or_weak_etag_blocks_put(self):
		from inventory.homebox import SyncWriteBlocked, put_entity_fields
		for etag in (None, 'W/"weak"', "not-quoted", '"invalid\nheader"'):
			with self.subTest(etag=etag), patch("inventory.homebox.get_entity_snapshot", return_value=({"id": "entity-1"}, etag)), patch("inventory.homebox.requests.put") as put:
				with self.assertRaisesRegex(SyncWriteBlocked, "conditional_write_unavailable"):
					put_entity_fields("entity-1", {"name": "New"}, expected={"id": "entity-1"}, etag=etag)
				put.assert_not_called()

	def test_last_moment_remote_change_blocks_put(self):
		from inventory.homebox import SyncWriteBlocked, put_entity_fields
		with patch("inventory.homebox.get_entity_snapshot", return_value=({"id": "entity-1", "name": "Concurrent"}, '"new"')), patch("inventory.homebox.requests.put") as put:
			with self.assertRaisesRegex(SyncWriteBlocked, "remote_changed_since_preview"):
				put_entity_fields("entity-1", {"name": "Local"}, expected={"id": "entity-1", "name": "Original"}, etag='"old"')
			put.assert_not_called()


class AuthenticationTests(unittest.TestCase):
	def test_complete_tag_pagination_and_repeated_terminal_pages(self):
		from inventory.homebox import HomeBoxEnumerationError, list_all_tags
		with patch("inventory.homebox.homebox_url", return_value="http://homebox"), patch("inventory.homebox.homebox_api_key", return_value="test-only"), patch("inventory.homebox.requests.get") as get:
			get.return_value.json.side_effect = [{"items": [{"id": "one", "name": "One"}], "totalPages": 2, "page": 1}, {"items": [{"id": "two", "name": "Two"}], "totalPages": 2, "page": 2}]
			self.assertEqual([tag["id"] for tag in list_all_tags()], ["one", "two"])
			self.assertTrue(all(call.args[0].endswith("/tags") for call in get.call_args_list))
			get.return_value.json.side_effect = [{"items": [{"id": "one", "name": "One"}], "totalPages": 2, "page": 1}, {"items": [{"id": "one", "name": "One"}], "totalPages": 2, "page": 2}]
			with self.assertRaises(HomeBoxEnumerationError):
				list_all_tags()

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