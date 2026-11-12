"""Three-way synchronization with mocked authoritative Homebox snapshots."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.homebox import NotesMigrationRequired, SyncWriteBlocked, _legacy_notes, build_notes, split_notes
from inventory.sync import sync_inventory


class InventorySyncTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		root = Path(self.temp.name)
		self.settings = SimpleNamespace(persistent_data_dir=root, items_dir=root / "items", backup_dir=root / "backups")
		self.path = self.settings.items_dir / "INV-test" / "item.json"
		self.path.parent.mkdir(parents=True)
		self.manifest = {"schema": "hermes-inventory-item", "schema_version": 3, "inventory_id": "INV-test", "asset_id": "000-011", "status": "synced", "item": {"name": "Book", "description": "Description", "manufacturer": "Publisher", "category": "Book", "condition": []}, "identifiers": {}, "attributes": [], "images": [], "tags": [], "notes": "User notes", "location": {"name": None, "path": []}, "purchase_price": "10.00", "purchase_from": "Shop", "purchase_date": "2026-01-01", "homebox": {"entity_id": "entity-1"}, "history": [], "field_sources": {}}
		self.save(self.manifest)
		self.remote = {"id": "entity-1", "assetId": "000-011", "name": "Book", "description": "Description", "manufacturer": "Publisher", "entityType": {"id": "book", "name": "Book"}, "fields": [{"name": "Inventory Item ID", "textValue": "INV-test"}], "tags": [], "location": None, "purchasePrice": 10, "purchaseFrom": "Shop", "purchaseDate": "2026-01-01T00:00:00Z", "notes": build_notes({"notes": "User notes"}), "quantity": 1}
		self.etag = '"1"'
		self.puts = []
		self.types = [{"id": "book", "name": "Book", "isLocation": False}]
		self.tags = []
		self.entities = [self.remote]
		for target, kwargs in (
			("inventory.homebox.list_all_entities", {"side_effect": lambda: copy.deepcopy(self.entities)}),
			("inventory.homebox.get_entity_types", {"side_effect": lambda: copy.deepcopy(self.types)}),
			("inventory.homebox.list_all_tags", {"side_effect": lambda: copy.deepcopy(self.tags)}),
			("inventory.homebox.get_entity_snapshot", {"side_effect": lambda entity_id: (copy.deepcopy(self.remote), self.etag)}),
			("inventory.homebox.put_entity_fields", {"side_effect": self.put}),
		):
			mock = patch(target, **kwargs)
			mock.start()
			self.addCleanup(mock.stop)

	def save(self, manifest):
		self.path.write_text(json.dumps(manifest), encoding="utf-8")

	def load(self):
		return json.loads(self.path.read_text(encoding="utf-8"))

	def put(self, entity_id, changes, **kwargs):
		self.assertEqual(entity_id, self.remote["id"])
		self.assertEqual(kwargs["expected"], self.remote)
		self.assertEqual(kwargs["etag"], self.etag)
		self.puts.append(copy.deepcopy(changes))
		self.remote.update(changes)
		if "entityTypeId" in changes:
			self.remote["entityType"] = next(entry for entry in self.types if entry["id"] == changes["entityTypeId"])
		if "tagIds" in changes:
			self.remote["tags"] = [tag for tag in self.tags if tag["id"] in changes["tagIds"]]
		if "locationId" in changes:
			self.remote["location"] = next((entry for entry in self.entities if entry["id"] == changes["locationId"]), None)
		self.etag = '"2"'

	def seed(self):
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertFalse(self.puts)

	def test_equal_values_establish_verified_baseline_and_are_idempotent(self):
		self.seed()
		baseline = self.load()["homebox"]["sync_baseline"]
		self.assertEqual(baseline["fields"]["purchase_price"], "10")
		self.assertEqual(baseline["version"], 1)
		self.assertEqual(sync_inventory(settings=self.settings, resolve=True)["applied"], [])

	def test_remote_name_is_imported_and_manual_ownership_preserved(self):
		self.seed()
		self.remote["name"] = "Edited in Homebox"
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["item"]["name"], "Edited in Homebox")
		self.assertEqual(self.load()["field_sources"]["name"], "user")
		self.assertFalse(self.puts)

	def test_local_name_is_exported_and_verified(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Edited locally"
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.remote["name"], "Edited locally")
		self.assertEqual(self.load()["homebox"]["sync_baseline"]["fields"]["name"], "Edited locally")

	def test_independent_edits_merge_and_repeated_sync_has_zero_changes(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["description"] = "Local description"
		self.save(manifest)
		self.remote["name"] = "Remote title"
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["item"]["name"], "Remote title")
		self.assertEqual(self.remote["description"], "Local description")
		self.assertEqual(self.puts, [{"description": "Local description"}])
		self.assertEqual(sync_inventory(settings=self.settings, resolve=True)["planned"], [])

	def test_same_field_conflict_preserves_both_values_and_baseline(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Local name"
		self.save(manifest)
		self.remote["name"] = "Remote name"
		before = self.path.read_bytes()
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "conflicts")
		conflict = result["conflicts"][0]
		self.assertEqual((conflict["field"], conflict["baseline"], conflict["local"], conflict["homebox"]), ("name", "Book", "Local name", "Remote name"))
		self.assertEqual(conflict["reason"], "both_sides_changed")
		self.assertIn("sources", conflict)
		self.assertEqual(self.path.read_bytes(), before)
		self.assertFalse(self.puts)

	def test_identical_simultaneous_edits_advance_baseline_without_put(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Same name"
		self.save(manifest)
		self.remote["name"] = "Same name"
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["applied"][0]["direction"], "baseline")
		self.assertFalse(self.puts)
		self.assertEqual(self.load()["homebox"]["sync_baseline"]["fields"]["name"], "Same name")

	def test_missing_baseline_classifies_differences_instead_of_choosing_winner(self):
		self.remote["name"] = "Different remote name"
		result = sync_inventory(settings=self.settings, resolve=True)
		conflict = next(entry for entry in result["conflicts"] if entry["field"] == "name")
		self.assertFalse(conflict["baseline_known"])
		self.assertEqual(conflict["reason"], "baseline_missing_or_unreliable")
		self.assertNotIn("name", self.load()["homebox"]["sync_baseline"]["fields"])
		self.assertEqual(self.load()["item"]["name"], "Book")
		self.assertFalse(self.puts)

	def test_wrong_version_or_link_does_not_make_baseline_authoritative(self):
		self.seed()
		for field, value in (("version", 99), ("entity_id", "wrong-link"), ("inventory_id", "INV-wrong")):
			manifest = self.load()
			manifest["homebox"]["sync_baseline"][field] = value
			self.save(manifest)
			self.remote["name"] = "Remote name"
			with self.subTest(field=field):
				result = sync_inventory(settings=self.settings)
				conflict = next(entry for entry in result["conflicts"] if entry["field"] == "name")
				self.assertEqual(conflict["reason"], "baseline_missing_or_unreliable")

	def test_homebox_user_notes_import_without_importing_generated_content(self):
		self.seed()
		self.remote["notes"] = build_notes({"identifiers": {"generated": ["not user text"]}, "notes": "Manually edited Homebox notes\nSecond line"})
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["notes"], "Manually edited Homebox notes\nSecond line")
		self.assertFalse(self.puts)
		self.assertEqual(sync_inventory(settings=self.settings, resolve=True)["planned"], [])

	def test_local_notes_export_preserves_freeform_text_and_owned_boundary(self):
		self.seed()
		manifest = self.load()
		manifest["notes"] = "Edited locally"
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(split_notes(self.remote["notes"], {}), "Edited locally")

	def test_conflicting_manual_notes_are_not_overwritten(self):
		self.seed()
		manifest = self.load()
		manifest["notes"] = "Local notes"
		self.save(manifest)
		self.remote["notes"] = build_notes({"notes": "Remote notes"})
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["conflicts"][0]["field"], "notes")
		self.assertEqual(self.load()["notes"], "Local notes")
		self.assertEqual(split_notes(self.remote["notes"], {}), "Remote notes")

	def test_legacy_notes_migrate_only_when_ownership_is_provable(self):
		self.remote["notes"] = _legacy_notes({"notes": "User notes"})
		self.seed()
		self.remote["notes"] = self.remote["notes"].replace("User notes\nUser notes", "User notes\nManual legacy edit")
		before = self.remote["notes"]
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["notes"], "Manual legacy edit")
		self.assertEqual(self.remote["notes"], before)
		self.assertFalse(self.puts)

	def test_ambiguous_legacy_notes_are_a_migration_conflict(self):
		self.remote["notes"] = "Identifiers\nisbn: unknown legacy data\n\nInventory metadata generated from local image analysis."
		result = sync_inventory(settings=self.settings, resolve=True)
		conflict = next(entry for entry in result["conflicts"] if entry["field"] == "notes")
		self.assertEqual(conflict["reason"], "notes_migration_required")
		self.assertEqual(conflict["homebox"], self.remote["notes"])
		self.assertNotIn("notes", self.load()["homebox"]["sync_baseline"]["fields"])
		self.assertFalse(self.puts)

	def test_user_tags_round_trip_while_system_and_category_tags_are_protected(self):
		self.tags[:] = [{"id": "system", "name": "Protected"}, {"id": "type", "name": "Book"}, {"id": "new", "name": "New"}, {"id": "remote", "name": "Remote"}]
		manifest = self.load()
		manifest["tags"] = [{"name": "Protected", "source": "system"}]
		self.save(manifest)
		self.remote["tags"] = self.tags[:2]
		self.seed()
		manifest = self.load()
		manifest["tags"].append({"name": "New", "source": "user"})
		self.save(manifest)
		sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(set(self.puts[-1]["tagIds"]), {"system", "type", "new"})
		self.remote["tags"] = self.tags[:2] + [self.tags[3]]
		sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual({tag["name"] for tag in self.load()["tags"]}, {"Protected", "Remote"})
		self.assertEqual(self.load()["tags"][0]["source"], "system")

	def test_unknown_tag_vocabulary_blocks_export_instead_of_creating_tags(self):
		self.seed()
		manifest = self.load()
		manifest["tags"] = [{"name": "Does not exist in Homebox", "source": "user"}]
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["conflicts"][0]["reason"], "tag_vocabulary_missing_or_ambiguous")
		self.assertFalse(self.puts)

	def test_asset_collisions_and_reservations_are_not_overwritten(self):
		self.seed()
		self.remote["assetId"] = "000-020"
		self.entities.append({"id": "another", "assetId": "000-020"})
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["conflicts"][0]["reason"], "asset_id_collision_homebox")
		self.entities.pop()
		reservation = self.settings.persistent_data_dir / ".asset-id-reservations" / "000-020.json"
		reservation.parent.mkdir()
		reservation.write_text('{"reservation_id": "someone-else"}', encoding="utf-8")
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["conflicts"][0]["reason"], "asset_id_reserved")
		self.assertTrue(reservation.exists())
		self.assertEqual(self.load()["asset_id"], "000-011")

	def test_asset_import_uses_reservation_and_does_not_rename_originals(self):
		self.seed()
		images = self.path.parent / "images"
		images.mkdir()
		(images / "original.jpg").write_bytes(b"original-image")
		self.remote["assetId"] = "000-020"
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["asset_id"], "000-020")
		self.assertEqual((images / "original.jpg").read_bytes(), b"original-image")
		self.assertEqual(list((self.settings.persistent_data_dir / ".asset-id-reservations").glob("*.json")), [])

	def test_location_and_verified_category_mapping_import(self):
		self.seed()
		self.types.extend([{"id": "electronics", "name": "Electronics", "isLocation": False}, {"id": "location", "name": "Location", "isLocation": True}])
		location = {"id": "shelf", "name": "Shelf", "entityType": self.types[2]}
		self.entities.append(location)
		self.remote.update({"location": location, "entityType": self.types[1]})
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["location"], {"id": "shelf", "name": "Shelf", "path": []})
		self.assertEqual(self.load()["item"]["category"], "Electronics")

	def test_generic_item_type_does_not_invent_reverse_category_mapping(self):
		self.types[:] = [{"id": "item", "name": "Item", "isLocation": False}]
		self.remote["entityType"] = self.types[0]
		result = sync_inventory(settings=self.settings)
		self.assertTrue(any(entry["field"] == "category" and entry["reason"] == "category_has_no_lossless_entity_type_mapping" for entry in result["skipped"]))

	def test_missing_and_unlinked_entities_are_not_automatically_copied(self):
		manifest = self.load()
		manifest["homebox"] = {}
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual({entry["reason"] for entry in result["skipped"]}, {"local_only_unlinked_use_refresh", "homebox_only_use_refresh"})
		self.assertFalse(self.puts)
		self.save(self.manifest)
		with patch("inventory.homebox.get_entity_snapshot", side_effect=RuntimeError("raw secret")):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "error")
		self.assertNotIn("raw secret", json.dumps(result))

	def test_identity_mismatch_blocks_all_fields(self):
		self.remote["fields"][0]["textValue"] = "INV-unrelated"
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["conflicts"][0]["reason"], "inventory_id_link_mismatch")
		self.assertFalse(result["planned"])
		self.assertFalse(self.puts)

	def test_incomplete_enumeration_or_auth_outage_blocks_all_mutations(self):
		before = self.path.read_bytes()
		with patch("inventory.homebox.list_all_entities", side_effect=RuntimeError("authentication secret raw body")):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "error")
		self.assertFalse(result["applied"])
		self.assertEqual(self.path.read_bytes(), before)
		self.assertNotIn("authentication secret", json.dumps(result))

	def test_remote_change_between_preview_and_apply_is_refused(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Local change"
		self.save(manifest)
		initial = copy.deepcopy(self.remote)
		changed = {**initial, "name": "Concurrent remote change"}
		with patch("inventory.homebox.get_entity_snapshot", side_effect=[(initial, '"1"'), (changed, '"2"')]):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertFalse(self.puts)
		self.assertEqual(result["skipped"][0]["reason"], "state_changed_since_preview")
		self.assertEqual(self.load()["homebox"]["sync_baseline"]["fields"]["name"], "Book")

	def test_remote_failure_keeps_successful_import_and_only_verified_baselines(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["description"] = "Local change"
		self.save(manifest)
		self.remote["name"] = "Remote change"
		with patch("inventory.homebox.put_entity_fields", side_effect=RuntimeError("raw secret")):
			result = sync_inventory(settings=self.settings, resolve=True)
		manifest = self.load()
		self.assertEqual(result["status"], "partial_success", result)
		self.assertEqual(result["local_status"], "saved")
		self.assertEqual(result["homebox_status"], "failed")
		self.assertEqual(manifest["item"]["name"], "Remote change")
		self.assertEqual(manifest["item"]["description"], "Local change")
		self.assertEqual(manifest["homebox"]["sync_baseline"]["fields"]["name"], "Remote change")
		self.assertEqual(manifest["homebox"]["sync_baseline"]["fields"]["description"], "Description")
		self.assertEqual(manifest["status"], "pending_homebox_sync")
		self.assertNotIn("raw secret", json.dumps(result))

	def test_missing_conditional_write_support_does_not_block_safe_imports(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["description"] = "Local change"
		self.save(manifest)
		self.remote["name"] = "Remote change"
		with patch("inventory.homebox.put_entity_fields", side_effect=SyncWriteBlocked("conditional_write_unavailable")):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "partial_success", result)
		self.assertEqual(self.load()["item"]["name"], "Remote change")
		self.assertEqual(result["skipped"][0]["reason"], "conditional_write_unavailable")

	def test_successful_put_with_failed_reread_does_not_advance_baseline(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Local change"
		self.save(manifest)
		initial = copy.deepcopy(self.remote)
		with patch("inventory.homebox.get_entity_snapshot", side_effect=[(initial, self.etag), (initial, self.etag), RuntimeError("network")]):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(self.remote["name"], "Local change")
		self.assertEqual(result["status"], "partial_success", result)
		self.assertEqual(self.load()["homebox"]["sync_baseline"]["fields"]["name"], "Book")
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["applied"][0]["direction"], "baseline")
		self.assertEqual(len(self.puts), 1)

	def test_backup_failure_prevents_all_application(self):
		before = self.path.read_bytes()
		with patch("inventory.sync.create_backup", side_effect=RuntimeError("secret")):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "error")
		self.assertEqual(result["skipped"][0]["reason"], "backup_failed_or_unverified")
		self.assertEqual(self.path.read_bytes(), before)
		self.assertFalse(self.puts)

	def test_preview_is_read_only_even_when_differences_and_conflicts_exist(self):
		self.remote["name"] = "Manual edit"
		before = {str(path.relative_to(self.settings.persistent_data_dir)): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.settings.persistent_data_dir.rglob("*") if path.is_file()}
		with patch("inventory.sync.create_backup") as backup, patch("inventory.sync.write_manifest") as write:
			result = sync_inventory(settings=self.settings)
			self.assertTrue(result["conflicts"])
			backup.assert_not_called()
			write.assert_not_called()
		after = {str(path.relative_to(self.settings.persistent_data_dir)): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.settings.persistent_data_dir.rglob("*") if path.is_file()}
		self.assertEqual(before, after)
		self.assertFalse(self.puts)

	def test_pending_local_edit_cannot_be_overwritten_by_stale_homebox(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Durable pending edit"
		manifest["status"] = "pending_homebox_sync"
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.remote["name"], "Durable pending edit")
		self.assertEqual(self.load()["status"], "pending_homebox_sync")

	def test_null_cleared_and_format_equivalent_values_are_explicit(self):
		self.seed()
		manifest = self.load()
		manifest["purchase_price"] = 10.0
		manifest["item"]["description"] = None
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(self.puts, [{"description": ""}])
		self.assertFalse(result["conflicts"])
		self.remote["purchasePrice"] = None
		sync_inventory(settings=self.settings, resolve=True)
		self.assertIsNone(self.load()["purchase_price"])

	def test_successful_remote_write_with_local_save_failure_is_partial_success(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Local name"
		self.save(manifest)
		with patch("inventory.sync.write_manifest", side_effect=OSError("secret filesystem details")):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "partial_success", result)
		self.assertEqual(result["homebox_status"], "synced")
		self.assertEqual(result["local_status"], "failed")
		self.assertFalse(result["applied"][0]["baseline_updated"])
		self.assertEqual(self.remote["name"], "Local name")
		self.assertEqual(self.load()["homebox"]["sync_baseline"]["fields"]["name"], "Book")
		self.assertNotIn("secret filesystem details", json.dumps(result))

	def test_verified_remote_timeout_does_not_repeat_the_write(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Local name"
		self.save(manifest)
		def put_then_timeout(*args, **kwargs):
			self.put(*args, **kwargs)
			raise TimeoutError("sensitive response")
		with patch("inventory.homebox.put_entity_fields", side_effect=put_then_timeout):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(result["warnings"][0]["reason"], "write_response_failed_but_values_verified")
		self.assertEqual(sync_inventory(settings=self.settings, resolve=True)["planned"], [])
		self.assertEqual(len(self.puts), 1)

	def test_decimal_projection_is_lossless_and_normalizes_negative_zero(self):
		from inventory.sync import _price
		self.assertEqual(_price("123456789012345678901234567890.1200"), "123456789012345678901234567890.12")
		self.assertEqual(_price("-0.00"), "0")

	def test_local_change_after_remote_verification_is_never_overwritten(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Exported name"
		self.save(manifest)
		def put_and_local_edit(*args, **kwargs):
			self.put(*args, **kwargs)
			manifest["item"]["name"] = "Concurrent local name"
			self.save(manifest)
		with patch("inventory.homebox.put_entity_fields", side_effect=put_and_local_edit):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "partial_success", result)
		self.assertEqual(self.load()["item"]["name"], "Concurrent local name")
		self.assertEqual(self.load()["homebox"]["sync_baseline"]["fields"]["name"], "Book")

	def test_unverified_backup_blocks_all_mutations(self):
		before = self.path.read_bytes()
		with patch("inventory.sync.verify_backup", return_value={"status": "FAIL"}):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "error")
		self.assertEqual(self.path.read_bytes(), before)
		self.assertFalse(self.puts)

	def test_cleared_homebox_notes_import_as_clear_not_local_restore(self):
		self.seed()
		self.remote["notes"] = ""
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.load()["notes"], "")
		self.assertEqual(self.remote["notes"], "")
		self.assertFalse(self.puts)

	def test_future_baseline_format_is_never_downgraded(self):
		self.seed()
		manifest = self.load()
		manifest["homebox"]["sync_baseline"]["version"] = 99
		self.save(manifest)
		before = self.path.read_bytes()
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "blocked")
		self.assertTrue(all(action["reason"] == "unsupported_sync_baseline_format" for action in result["skipped"]))
		self.assertEqual(self.path.read_bytes(), before)

	def test_missing_etag_reports_blocked_export_during_preview_without_writes(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["name"] = "Local name"
		self.save(manifest)
		self.etag = None
		before = self.path.read_bytes()
		result = sync_inventory(settings=self.settings)
		self.assertEqual(result["skipped"][0]["reason"], "conditional_write_unavailable")
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "blocked")
		self.assertEqual(result["homebox_status"], "not_attempted")
		self.assertEqual(self.path.read_bytes(), before)
		self.assertFalse(self.puts)

	def test_later_success_does_not_hide_earlier_item_sync_failure(self):
		self.seed()
		manifest = self.load()
		manifest["item"]["description"] = "Local description"
		self.save(manifest)
		other = copy.deepcopy(self.manifest)
		other.update({"inventory_id": "INV-zother", "asset_id": "000-012", "homebox": {"entity_id": "entity-2"}})
		other_path = self.settings.items_dir / "INV-zother" / "item.json"
		other_path.parent.mkdir()
		other_path.write_text(json.dumps(other), encoding="utf-8")
		other_remote = copy.deepcopy(self.remote)
		other_remote.update({"id": "entity-2", "assetId": "000-012", "fields": [{"name": "Inventory Item ID", "textValue": "INV-zother"}]})
		self.entities.append(other_remote)
		def snapshot(entity_id):
			return copy.deepcopy(self.remote if entity_id == "entity-1" else other_remote), self.etag
		with patch("inventory.homebox.get_entity_snapshot", side_effect=snapshot), patch("inventory.homebox.put_entity_fields", side_effect=RuntimeError("outage")):
			result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "partial_success", result)
		self.assertEqual(result["homebox_status"], "failed")
		self.assertTrue(any(action["inventory_id"] == "INV-zother" for action in result["applied"]))

	def test_verified_category_asset_and_location_exports_update_baselines(self):
		self.seed()
		self.types.extend([{"id": "electronics", "name": "Electronics", "isLocation": False}, {"id": "location", "name": "Location", "isLocation": True}])
		self.entities.append({"id": "shelf", "name": "Shelf", "entityType": self.types[2]})
		manifest = self.load()
		manifest["item"]["category"] = "Electronics"
		manifest["asset_id"] = "000-020"
		manifest["location"] = {"id": "shelf", "name": "Shelf", "path": []}
		self.save(manifest)
		result = sync_inventory(settings=self.settings, resolve=True)
		self.assertEqual(result["status"], "updated", result)
		self.assertEqual(self.puts[-1], {"assetId": "000-020", "locationId": "shelf", "entityTypeId": "electronics"})
		self.assertEqual(self.load()["homebox"]["entity_type"], "electronics")
		self.assertEqual(self.load()["homebox"]["asset_id"], "000-020")
		self.assertEqual(sync_inventory(settings=self.settings, resolve=True)["planned"], [])

	def test_asset_collision_with_another_local_record_is_blocked(self):
		self.seed()
		other = copy.deepcopy(self.manifest)
		other.update({"inventory_id": "INV-other", "asset_id": "000-020", "homebox": {}})
		path = self.settings.items_dir / "INV-other" / "item.json"
		path.parent.mkdir()
		path.write_text(json.dumps(other), encoding="utf-8")
		self.remote["assetId"] = "000-020"
		result = sync_inventory(settings=self.settings, resolve=True, item="000-011")
		self.assertEqual(result["conflicts"][0]["reason"], "asset_id_collision_local")
		self.assertFalse(self.puts)