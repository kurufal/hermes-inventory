import tempfile
import copy
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
	def _entity(self):
		return {"id": "entity-1", "name": "Old", "description": "Keep description", "assetId": "000-011", "entityType": {"id": "item"}, "parent": {"id": "shelf"}, "tags": [{"id": "tag"}], "fields": [{"id": "field-id", "name": "Custom", "textValue": "Keep"}], "notes": "Manual notes", "purchasePrice": "12.50", "purchaseFrom": "Store", "purchaseDate": "2026-01-01", "warrantyExpires": "2030-01-01", "warrantyDetails": "Full warranty", "insured": True, "archived": False, "quantity": 2, "soldNotes": "Sale notes"}

	def test_known_put_mapping_preserves_existing_values_and_field_ids(self):
		from inventory.homebox import _entity_put_payload
		current = self._entity()
		before = copy.deepcopy(current)
		payload = _entity_put_payload(current, {"name": "New"})
		for key in ("assetId", "notes", "purchasePrice", "purchaseFrom", "purchaseDate", "warrantyExpires", "warrantyDetails", "insured", "archived", "quantity", "soldNotes", "fields"):
			self.assertEqual(payload[key], current[key])
		self.assertEqual(payload["fields"][0]["id"], "field-id")
		self.assertEqual(payload["parentId"], "shelf")
		self.assertEqual(payload["entityTypeId"], "item")
		self.assertEqual(payload["tagIds"], ["tag"])
		self.assertNotIn("parent", payload)
		self.assertNotIn("entityType", payload)
		self.assertNotIn("tags", payload)
		self.assertEqual(current, before)

	def test_unverified_preservation_and_unsupported_location_write_are_blocked(self):
		from inventory.homebox import SyncWriteBlocked, _entity_put_payload
		current = self._entity()
		with self.assertRaisesRegex(SyncWriteBlocked, "unsupported_entity_write_field"):
			_entity_put_payload(current, {"locationId": "shelf"})
		current.pop("parent")
		with self.assertRaisesRegex(SyncWriteBlocked, "entity_put_preservation_unverified"):
			_entity_put_payload(current, {"name": "New"})

	def test_precondition_rejections_do_not_retry_unconditionally(self):
		from inventory.homebox import SyncWriteBlocked, put_entity_fields
		current = self._entity()
		capability = {"remote_exports": True, "conditional_write_enforcement": "verified", "put_preservation": "verified"}
		for status in (409, 412, 428):
			with self.subTest(status=status), patch("inventory.homebox.remote_write_compatibility", return_value=capability), patch("inventory.homebox.get_entity_snapshot", return_value=(current, '"revision-1"')), patch("inventory.homebox.homebox_url", return_value="http://homebox"), patch("inventory.homebox.homebox_api_key", return_value="test-only"), patch("inventory.homebox.requests.put") as put:
				put.return_value.status_code = status
				with self.assertRaisesRegex(SyncWriteBlocked, f"remote_precondition_rejected_{status}"):
					put_entity_fields("entity-1", {"name": "New"}, expected=current, etag='"revision-1"')
				put.assert_called_once()
				self.assertEqual(put.call_args.kwargs["headers"]["If-Match"], '"revision-1"')

	def test_success_response_cannot_hide_untouched_field_loss(self):
		from inventory.homebox import SyncWriteBlocked, put_entity_fields
		current = self._entity()
		capability = {"remote_exports": True, "conditional_write_enforcement": "verified", "put_preservation": "verified"}
		for damaged in ({"fields": []}, {"tags": []}, {"assetId": ""}, {"parent": None}, {"notes": ""}, {"warrantyDetails": ""}, {"fields": [{"id": "different-id", "name": "Custom", "textValue": "Keep"}]}):
			with self.subTest(damaged=damaged), patch("inventory.homebox.remote_write_compatibility", return_value=capability), patch("inventory.homebox.get_entity_snapshot", side_effect=[(current, '"revision-1"'), ({**current, "name": "New", **damaged}, '"revision-2"')]), patch("inventory.homebox.homebox_url", return_value="http://homebox"), patch("inventory.homebox.homebox_api_key", return_value="test-only"), patch("inventory.homebox.requests.put") as put:
				put.return_value.status_code = 200
				with self.assertRaisesRegex(SyncWriteBlocked, "remote_untouched_field_changed"):
					put_entity_fields("entity-1", {"name": "New"}, expected=current, etag='"revision-1"')

	def test_server_that_would_ignore_if_match_is_never_probed_automatically(self):
		from inventory.homebox import SyncWriteBlocked, put_entity_fields
		current = self._entity()
		with patch("inventory.homebox.get_entity_snapshot", return_value=(current, '"strong"')), patch("inventory.homebox.requests.put") as put:
			put.return_value.status_code = 200
			with self.assertRaisesRegex(SyncWriteBlocked, "conditional_enforcement_unverified"):
				put_entity_fields("entity-1", {"name": "New"}, expected=current, etag='"strong"')
			put.assert_not_called()

	def test_put_preserves_unrelated_values_and_sends_if_match(self):
		from inventory.homebox import put_entity_fields
		current = {"id": "entity-1", "name": "Old", "assetId": "000-011", "notes": "Manual notes", "entityType": {"id": "item"}, "tags": [{"id": "tag"}], "parent": {"id": "shelf"}, "fields": [{"id": "custom-id", "name": "Custom", "textValue": "Keep"}], "soldNotes": "Do not remove"}
		capability = {"remote_exports": True, "conditional_write_enforcement": "verified", "put_preservation": "verified"}
		with patch("inventory.homebox.remote_write_compatibility", return_value=capability), patch("inventory.homebox.get_entity_snapshot", side_effect=[(current, '"revision-1"'), ({**current, "name": "New"}, '"revision-2"')]), patch("inventory.homebox.homebox_url", return_value="http://homebox"), patch("inventory.homebox.homebox_api_key", return_value="test-only"), patch("inventory.homebox.requests.put") as put:
			put_entity_fields("entity-1", {"name": "New"}, expected=current, etag='"revision-1"')
		payload = put.call_args.kwargs["json"]
		self.assertEqual(payload["notes"], "Manual notes")
		self.assertEqual(payload["fields"], current["fields"])
		self.assertEqual(payload["soldNotes"], "Do not remove")
		self.assertEqual(payload["parentId"], "shelf")
		self.assertNotIn("locationId", payload)
		self.assertEqual(payload["assetId"], "000-011")
		self.assertEqual(payload["tagIds"], ["tag"])
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

	def test_strong_etag_does_not_establish_if_match_enforcement(self):
		from inventory.homebox import SyncWriteBlocked, put_entity_fields, remote_write_compatibility
		current = {"id": "entity-1", "fields": [], "tags": [], "entityType": {"id": "item"}, "parent": None}
		capability = remote_write_compatibility(current, '"strong"')
		self.assertEqual(capability["strong_etag"], "available")
		self.assertEqual(capability["conditional_write_enforcement"], "unknown")
		self.assertFalse(capability["remote_exports"])
		with patch("inventory.homebox.get_entity_snapshot", return_value=(current, '"strong"')), patch("inventory.homebox.requests.put") as put:
			with self.assertRaisesRegex(SyncWriteBlocked, "conditional_enforcement_unverified"):
				put_entity_fields("entity-1", {"name": "New"}, expected=current, etag='"strong"')
			put.assert_not_called()


class AuthenticationTests(unittest.TestCase):
	def test_compatibility_diagnostics_are_get_only_and_do_not_enable_exports(self):
		from inventory.homebox import remote_write_diagnostics
		entity = {"id": "entity-1", "parent": None, "entityType": {"id": "item"}, "fields": [], "tags": []}
		with patch("inventory.homebox.homebox_url", return_value="http://homebox"), patch("inventory.homebox.homebox_api_key", return_value="test-only"), patch("inventory.homebox.requests.get") as get, patch("inventory.homebox.requests.put") as put, patch("inventory.homebox.requests.patch") as patch_request, patch("inventory.homebox.requests.post") as post, patch("inventory.homebox.requests.delete") as delete:
			get.return_value.json.side_effect = [[entity], entity]
			get.return_value.headers = {"ETag": '"strong"'}
			result = remote_write_diagnostics()
			self.assertEqual(get.call_count, 2)
			for mutation in (put, patch_request, post, delete):
				mutation.assert_not_called()
		self.assertEqual(result["connectivity"], "available")
		self.assertEqual(result["strong_etag"], "available")
		self.assertEqual(result["conditional_write_enforcement"], "unknown")
		self.assertEqual(result["put_preservation"], "unverified")
		self.assertEqual(result["patch_support"], "unverified")
		self.assertFalse(result["remote_exports"])

	def test_diagnostics_report_missing_weak_etags_and_redact_outages(self):
		from inventory.homebox import remote_write_diagnostics
		entity = {"id": "entity-1", "entityType": {"id": "item"}, "fields": [], "tags": []}
		for etag, state in ((None, "missing_or_invalid"), ('W/"weak"', "weak"), ('"strong"', "available")):
			with self.subTest(etag=etag), patch("inventory.homebox.list_all_entities", return_value=[entity]), patch("inventory.homebox.get_entity_snapshot", return_value=(entity, etag)):
				result = remote_write_diagnostics()
				self.assertEqual(result["strong_etag"], state)
				self.assertFalse(result["remote_exports"])
		with patch("inventory.homebox.list_all_entities", side_effect=RuntimeError("private API body and credentials")):
			result = remote_write_diagnostics()
		self.assertEqual(result["connectivity"], "unavailable")
		self.assertNotIn("credentials", str(result))

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