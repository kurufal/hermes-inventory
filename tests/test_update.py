"""Canonical Inventory update behavior."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.homebox import synchronized_tag_ids
from inventory.search import search_inventory
from inventory.storage import format_asset_id, write_catalog
from inventory.update import update_item


class InventoryUpdateTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name) / "inventory"
		self.settings = SimpleNamespace(persistent_data_dir=self.root, items_dir=self.root / "items")
		self.item_id = "INV-20260901-204535-601208be"
		self.item = self.settings.items_dir / self.item_id
		(self.item / "images").mkdir(parents=True)
		(self.item / "images" / "old-front.jpg").write_bytes(b"front")
		(self.item / "images" / "old-page.jpg").write_bytes(b"page")
		manifest = {
			"schema": "hermes-inventory-item", "schema_version": 1, "inventory_id": self.item_id,
			"asset_id": "000-011", "status": "synced", "item": {"name": "Cyberpunk 2077: No Coincidence", "category": "Book", "manufacturer": "CD Projekt", "description": "old", "condition": []},
			"identifiers": {"isbn_13": ["9780000000001"]}, "attributes": [{"name": "Format", "value": "Trade Paperback"}],
			"images": [{"relative_path": "images/old-front.jpg", "sha256": "x"}, {"relative_path": "images/old-page.jpg", "sha256": "y"}],
			"homebox": {"entity_id": "entity-1", "attachments": [{"id": "a1"}]}, "field_sources": {}, "history": [],
		}
		(self.item / "item.json").write_text(json.dumps(manifest), encoding="utf-8")
		(self.item / "vision.json").write_text(json.dumps({"source_images": ["old-front.jpg", "old-page.jpg"]}), encoding="utf-8")

	def tearDown(self): self.temp.cleanup()

	def test_asset_id_formatting_crosses_thousand_boundary(self):
		self.assertEqual(format_asset_id(11), "000-011")
		self.assertEqual(format_asset_id(999), "000-999")
		self.assertEqual(format_asset_id(1000), "001-000")

	def test_edit_by_asset_id_preserves_ids_images_and_marks_manual_override(self):
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}) as homebox:
			result = update_item("000-011", "edit", {"purchase_price": 14.99, "purchase_from": "Example Bookstore", "attributes": [{"name": "Format", "value": "Hardcover"}]}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "updated")
		self.assertEqual(manifest["asset_id"], "000-011")
		self.assertEqual(manifest["inventory_id"], self.item_id)
		self.assertEqual(manifest["purchase_price"], 14.99)
		self.assertEqual(manifest["purchase_from"], "Example Bookstore")
		self.assertEqual(manifest["attributes"][0]["value"], "Hardcover")
		self.assertEqual(manifest["field_sources"]["attributes.format"], "user")
		self.assertEqual(len(manifest["images"]), 2)
		self.assertEqual(manifest["history"][-1]["operation"], "edit")
		homebox.assert_called_once()

	def test_reanalysis_preserves_manual_format_and_uses_durable_images(self):
		self.test_edit_by_asset_id_preserves_ids_images_and_marks_manual_override()
		def vision(directory, client, *, metadata_path):
			self.assertEqual(Path(directory), self.item / "images")
			return ({"source_directory": str(directory), "source_images": ["old-front.jpg"], "parse_status": "json_ok", "result": {"object_type": {"value": "Book"}, "product_or_title": {"value": "Cyberpunk 2077: No Coincidence"}, "manufacturer_or_publisher": {"value": "CD Projekt"}, "physical_description": {"value": "new"}, "attributes": [{"name": "Format", "value": "Trade Paperback"}]}}, metadata_path)
		with patch("inventory.update.run_vision", side_effect=vision), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item(self.item_id, "reanalyze", vision_client=object(), settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["attributes"][0]["value"], "Hardcover")
		self.assertTrue(list((self.item / "history").glob("vision-*.json")))

	def test_ambiguous_name_does_not_mutate(self):
		other = self.settings.items_dir / "INV-other"; other.mkdir(parents=True)
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload["inventory_id"] = "INV-other"; payload["asset_id"] = "000-012"; payload["item"]["name"] = "Cyberpunk guide"
		(other / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		result = update_item("Cyberpunk", "edit", {"name": "changed"}, settings=self.settings)
		self.assertEqual(result["status"], "ambiguous")

	def test_invalid_edits_are_rejected_without_any_side_effects(self):
		cases = [
			({"tags_add": "Books"}, "tags_add"),
			({"tags_remove": "Books"}, "tags_remove"),
			({"tags_add": [""]}, "tags_add[0]"),
			({"tags_add": [" "]}, "tags_add[0]"),
			({"tags_add": [5]}, "tags_add[0]"),
			({"identifiers": []}, "identifiers"),
			({"identifiers": {"isbn": "123"}}, "identifiers.isbn"),
			({"identifiers": {"isbn": [123]}}, "identifiers.isbn[0]"),
			({"identifiers": {"isbn": [["123"]]}}, "identifiers.isbn[0]"),
			({"identifiers": {" ": []}}, "identifiers"),
			({"attributes": {}}, "attributes"),
			({"attributes": ["Format"]}, "attributes[0]"),
			({"attributes": [{"value": "Hardcover"}]}, "attributes[0].name"),
			({"attributes": [{"name": "Format"}]}, "attributes[0].value"),
			({"attributes": [{"name": " ", "value": "Hardcover"}]}, "attributes[0].name"),
			({"attributes": [{"name": "Format", "value": {"nested": "bad"}}]}, "attributes[0].value"),
			({"attributes": [{"name": "Format", "value": "Hardcover", "confidence": True}]}, "attributes[0].confidence"),
			({"attributes": [{"name": "Format", "value": "Hardcover", "source_images": [{}]}]}, "attributes[0].source_images[0]"),
			({"unsupported": "ignored before"}, "unsupported"),
			({"publisher": "A", "manufacturer": "B"}, "publisher"),
			({"physical_description": "A", "description": "B"}, "physical_description"),
			({"type": "Book", "category": "Electronics"}, "type"),
			({"asset_id": "invalid"}, "asset_id"),
			({"asset_id": "000-025\n"}, "asset_id"),
			({"asset_id": 25}, "asset_id"),
			({"asset_id": None}, "asset_id"),
			({"location": []}, "location"),
			({"location": {"name": []}}, "location.name"),
			({"location": {"path": "Room"}}, "location.path"),
			({"location": {"path": [{"name": "Room"}]}}, "location.path[0]"),
			({"location": {"unknown": "Room"}}, "location.unknown"),
			({"condition": "good"}, "condition"),
			({"condition": [{}]}, "condition[0].observation"),
			({"condition": [{"observation": {"nested": "bad"}}]}, "condition[0].observation"),
			({"condition": [{"observation": "Good", "source_images": [1]}]}, "condition[0].source_images[0]"),
			({"purchase_price": True}, "purchase_price"),
			({"purchase_price": float("nan")}, "purchase_price"),
			({"purchase_price": float("inf")}, "purchase_price"),
			({"purchase_price": "not a price"}, "purchase_price"),
			({"purchase_date": {}}, "purchase_date"),
			({"name": 42}, "name"),
			({"notes": ["nested"]}, "notes"),
		]
		before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
		for invalid, field in cases:
			changes = {"name": "Must not rename images", "asset_id": "000-025", **invalid}
			with self.subTest(invalid=invalid), patch("inventory.homebox.complete_entity") as sync, patch("inventory.homebox.list_all_entities") as entities, patch("inventory.update.canonicalize_images") as rename:
				result = update_item("000-011", "edit", changes, settings=self.settings)
				self.assertEqual(result["status"], "error")
				self.assertEqual(result["field"], field)
				self.assertTrue(result["error"])
				sync.assert_not_called()
				entities.assert_not_called()
				rename.assert_not_called()
				self.assertEqual({str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

	def test_changes_must_be_an_object_but_omitted_changes_remain_valid(self):
		for changes in ("", "name", [], ["name"], False, 0):
			with self.subTest(changes=changes), patch("inventory.homebox.complete_entity") as sync:
				result = update_item("000-011", "edit", changes, settings=self.settings)
				self.assertEqual(result["status"], "error")
				self.assertEqual(result["field"], "changes")
				sync.assert_not_called()
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			self.assertEqual(update_item("000-011", "edit", settings=self.settings)["status"], "updated")

	def test_valid_multifield_edit_preserves_aliases_metadata_and_system_tags(self):
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		manifest["tags"] = [{"name": "Protected", "source": "system"}, {"name": "Old", "source": "user"}]
		(self.item / "item.json").write_text(json.dumps(manifest), encoding="utf-8")
		changes = {
			"name": "New book", "category": "Book", "type": "Book", "publisher": "Publisher", "manufacturer": "Publisher",
			"physical_description": "Description", "description": "Description", "notes": "User notes",
			"purchase_price": "12.50", "purchase_from": "Bookshop", "purchase_date": "2026-11-11",
			"location": {"name": "Shelf", "path": ["Room", "Shelf"]},
			"condition": [{"observation": "Good", "confidence": 1.0, "source_images": ["old-front.jpg"]}],
			"identifiers": {"ISBN_13": ["new-isbn"], "serial": []},
			"attributes": [{"name": "Format", "value": "Hardcover", "confidence": 1.0, "source_images": []}, {"name": "Count", "value": 2}, {"name": "Signed", "value": True}],
			"asset_id": "000-025", "tags_add": ["New"], "tags_remove": ["Old", "Protected"],
		}
		with patch("inventory.config.homebox_url", return_value=""), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			result = update_item("000-011", "edit", changes, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "updated")
		self.assertEqual(result["homebox_status"], "synced")
		self.assertEqual(manifest["item"]["manufacturer"], "Publisher")
		self.assertEqual(manifest["item"]["description"], "Description")
		self.assertEqual(manifest["purchase_price"], "12.50")
		self.assertEqual(manifest["location"], changes["location"])
		self.assertEqual(manifest["item"]["condition"], changes["condition"])
		self.assertEqual(manifest["identifiers"]["isbn_13"], ["new-isbn"])
		self.assertEqual(manifest["attributes"], changes["attributes"])
		self.assertEqual({tag["name"] for tag in manifest["tags"]}, {"New", "Protected"})
		self.assertEqual(manifest["field_sources"]["manufacturer"], "user")
		self.assertTrue(all(image["canonical_filename"].startswith("000-025_new-book_") for image in manifest["images"]))

	def test_nullable_and_empty_values_preserve_clearing_and_merge_semantics(self):
		changes = {"notes": "", "purchase_from": None, "purchase_date": None, "purchase_price": None, "location": {"name": None, "path": []}, "condition": [], "identifiers": {"isbn_13": []}, "attributes": [{"name": "Format", "value": None}], "tags_add": [], "tags_remove": []}
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			result = update_item("000-011", "edit", changes, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "updated")
		self.assertIsNone(manifest["purchase_price"])
		self.assertIsNone(manifest["purchase_from"])
		self.assertEqual(manifest["identifiers"]["isbn_13"], [])
		self.assertEqual(manifest["attributes"], [{"name": "Format", "value": None}])
		for location in ("Shelf", None):
			with self.subTest(location=location), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
				self.assertEqual(update_item("000-011", "edit", {"location": location}, settings=self.settings)["status"], "updated")

	def test_resync_does_not_run_vision(self):
		with patch("inventory.update.run_vision", side_effect=AssertionError("vision")), patch("inventory.homebox.complete_entity", return_value={"attachments": []}) as sync:
			result = update_item("9780000000001", "resync", settings=self.settings)
		self.assertEqual(result["operation"], "resync")
		self.assertFalse(sync.call_args.kwargs["upload_attachments"])

	def test_old_notes_history_does_not_authorize_overwriting_homebox_notes(self):
		from inventory.homebox import build_notes, merge_notes
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}) as sync:
			update_item("000-011", "edit", {"notes": "Local notes"}, settings=self.settings)
		self.assertIn("previous_notes", sync.call_args.args[1])
		def complete(entity_id, record, **kwargs):
			self.assertNotIn("previous_notes", record)
			merge_notes(build_notes({"notes": "New Homebox notes"}), record, linked=True)
		with patch("inventory.homebox.complete_entity", side_effect=complete):
			result = update_item("000-011", "resync", settings=self.settings)
		self.assertEqual(result["status"], "partial_success")
		self.assertEqual(result["local_status"], "saved")
		self.assertEqual(json.loads((self.item / "item.json").read_text(encoding="utf-8"))["notes"], "Local notes")

	def test_asset_id_edit_requires_valid_unused_value(self):
		result = update_item("000-011", "edit", {"asset_id": "book1"}, settings=self.settings)
		self.assertEqual(result["status"], "error")
		self.assertIn("NNN-NNN", result["error"])
		other = self.settings.items_dir / "INV-other"; other.mkdir(parents=True)
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload["inventory_id"] = "INV-other"; payload["asset_id"] = "000-025"
		(other / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		result = update_item("000-011", "edit", {"asset_id": "000-025"}, settings=self.settings)
		self.assertEqual(result["status"], "error")
		self.assertIn("already in use", result["error"])

	def test_asset_id_edit_fails_closed_when_configured_homebox_unavailable(self):
		with patch("inventory.config.homebox_url", return_value="http://homebox"), patch("inventory.homebox.list_all_entities", side_effect=RuntimeError("offline")):
			result = update_item("000-011", "edit", {"asset_id": "000-026"}, settings=self.settings)
		self.assertEqual(result["status"], "error")
		self.assertIn("configured HomeBox", result["error"])

	def test_asset_id_edit_allows_unconfigured_local_only_and_rejects_homebox_collision(self):
		with patch("inventory.config.homebox_url", return_value=""), patch("inventory.homebox.list_all_entities", side_effect=AssertionError("not configured")), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "edit", {"asset_id": "000-026"}, settings=self.settings)
		with patch("inventory.config.homebox_url", return_value="http://homebox"), patch("inventory.homebox.list_all_entities", return_value=[{"id": "different", "assetId": "000-027"}]):
			result = update_item("000-026", "edit", {"asset_id": "000-027"}, settings=self.settings)
		self.assertEqual(result["status"], "error")
		self.assertEqual(result["field"], "asset_id")

	def test_edit_renames_images_and_retains_hash_and_provenance(self):
		with patch("inventory.config.homebox_url", return_value=""), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "edit", {"name": "Cyberpunk / No: Coincidence", "asset_id": "000-025"}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertTrue(all(image["canonical_filename"].startswith("000-025_cyberpunk-no-coincidence_") for image in manifest["images"]))
		self.assertEqual({image["sha256"] for image in manifest["images"]}, {"x", "y"})
		self.assertTrue(all(image["original_filename"] == Path(image["source_filename"]).name for image in manifest["images"]))

	def test_reanalysis_refreshes_vision_fields_and_preserves_user_description(self):
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "edit", {"description": "manual description", "category": "Book", "tags_add": ["Cyberpunk"]}, settings=self.settings)
		raw = {"source_directory": str(self.item / "images"), "source_images": ["old-front.jpg", "old-page.jpg"], "parse_status": "json_ok", "result": {"object_type": {"value": "electronics"}, "product_or_title": {"value": "New title"}, "manufacturer_or_publisher": {"value": "New maker"}, "physical_description": {"value": "vision description"}, "identifiers": {"serial_number": ["NEW"]}, "condition_observations": [{"observation": "scratched"}], "attributes": [{"name": "Color", "value": "Black"}], "image_roles": [{"filename": "old-front.jpg", "inferred_role": "serial label"}, {"filename": "old-page.jpg", "inferred_role": "back"}]}}
		with patch("inventory.update.run_vision", return_value=(raw, self.item / "vision.json")), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "reanalyze", vision_client=object(), settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["item"]["description"], "manual description")
		self.assertEqual(manifest["item"]["name"], "New title")
		self.assertEqual(manifest["identifiers"]["serial_number"], ["NEW"])
		self.assertEqual(manifest["item"]["condition"][0]["observation"], "scratched")
		self.assertIn({"name": "Cyberpunk", "source": "user"}, manifest["tags"])
		self.assertEqual(manifest["item"]["category"], "Book")
		self.assertNotIn({"name": "Type: Book", "source": "system"}, manifest["tags"])

	def test_legacy_manifest_can_resync_without_implicit_asset_id(self):
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload.pop("asset_id"); payload.pop("field_sources"); payload.pop("history"); payload["schema_version"] = 1
		(self.item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item(self.item_id, "resync", settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["schema_version"], 3)
		self.assertIsNone(manifest.get("asset_id"))

	def test_homebox_failure_keeps_local_edit_pending_for_later_resync(self):
		with patch("inventory.homebox.complete_entity", side_effect=RuntimeError("secret-token raw API response")):
			result = update_item("000-011", "edit", {"purchase_price": 14.99}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "partial_success")
		self.assertEqual(result["local_status"], "saved")
		self.assertEqual(result["homebox_status"], "failed")
		self.assertTrue(result["durable"])
		self.assertEqual(result["asset_id"], "000-011")
		self.assertEqual(result["inventory_id"], self.item_id)
		self.assertEqual(result["changes"]["purchase_price"]["new"], 14.99)
		self.assertIn("saved locally", result["message"])
		self.assertIn("not yet synchronized", result["message"])
		self.assertIn("inventory_update", result["message"])
		self.assertIn('operation="resync"', result["message"])
		self.assertNotIn("secret-token", json.dumps(result))
		self.assertNotIn("raw API response", json.dumps(result))
		self.assertEqual(manifest["status"], "pending_homebox_sync")
		self.assertEqual(manifest["purchase_price"], 14.99)
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}) as sync:
			result = update_item("000-011", "resync", settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "updated")
		self.assertEqual(result["homebox_status"], "synced")
		self.assertEqual(manifest["status"], "synced")
		self.assertIsNone(manifest["error"])
		self.assertEqual(manifest["purchase_price"], 14.99)
		self.assertEqual(sync.call_args.args[1]["purchase_price"], 14.99)
		self.assertTrue(sync.call_args.kwargs["upload_attachments"])

	def test_all_operations_report_local_and_homebox_success(self):
		raw = {"parse_status": "json_ok", "result": {"physical_description": {"value": "reanalyzed"}}}
		for operation in ("edit", "reanalyze", "resync"):
			with self.subTest(operation=operation), patch("inventory.update.run_vision", return_value=(raw, self.item / "vision.json")), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
				result = update_item("000-011", operation, {"notes": "saved"}, settings=self.settings)
				self.assertEqual(result["status"], "updated")
				self.assertEqual(result["local_status"], "saved")
				self.assertEqual(result["homebox_status"], "synced")
				self.assertEqual(result["operation"], operation)

	def test_all_operations_report_sync_failure_without_losing_local_changes(self):
		raw = {"parse_status": "json_ok", "result": {"physical_description": {"value": "reanalyzed"}}}
		for operation in ("edit", "reanalyze", "resync"):
			with self.subTest(operation=operation), patch("inventory.update.run_vision", return_value=(raw, self.item / "vision.json")), patch("inventory.homebox.complete_entity", side_effect=RuntimeError("offline")):
				result = update_item("000-011", operation, {"notes": "saved"}, settings=self.settings)
				manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
				self.assertEqual(result["status"], "partial_success")
				self.assertEqual(result["local_status"], "saved")
				self.assertEqual(result["homebox_status"], "failed")
				self.assertEqual(result["operation"], operation)
				self.assertEqual(manifest["status"], "pending_homebox_sync")
				self.assertEqual(manifest["notes"], "saved")
				if operation != "edit":
					self.assertEqual(manifest["item"]["description"], "reanalyzed")

	def test_unlinked_item_does_not_report_remote_success(self):
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		payload["homebox"] = {}
		payload["status"] = "pending_homebox_sync"
		(self.item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		raw = {"parse_status": "json_ok", "result": {}}
		for operation in ("edit", "reanalyze", "resync"):
			with self.subTest(operation=operation), patch("inventory.update.run_vision", return_value=(raw, self.item / "vision.json")), patch("inventory.homebox.complete_entity") as sync:
				result = update_item("000-011", operation, {"notes": "local only"}, settings=self.settings)
				self.assertEqual(result["status"], "local_only")
				self.assertEqual(result["local_status"], "saved")
				self.assertEqual(result["homebox_status"], "not_attempted")
				self.assertIn("no Homebox entity is linked", result["message"])
				sync.assert_not_called()
				manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
				self.assertEqual(manifest["status"], "pending_homebox_sync")
				self.assertEqual(manifest["notes"], "local only")

	def test_pending_resync_skips_recorded_attachment_hash_and_persists_completion(self):
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		payload["status"] = "pending_homebox_sync"
		payload["homebox"] = {"entity_id": "entity-1", "attachments": [], "attachment_sync": {"x": {"filename": "old-front.jpg", "sha256": "x", "result": {"id": "a"}}}}
		(self.item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		def complete(entity_id, record, **kwargs):
			self.assertTrue(kwargs["upload_attachments"])
			self.assertIn("x", kwargs["attachment_sync"])
			kwargs["on_attachment_uploaded"]({"filename": "old-page.jpg", "sha256": "y", "result": {"id": "b"}, "primary": False})
			return {"attachments": list(kwargs["attachment_sync"].values()) + [{"filename": "old-page.jpg", "sha256": "y", "result": {"id": "b"}, "primary": False}]}
		with patch("inventory.homebox.complete_entity", side_effect=complete):
			update_item("000-011", "resync", settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(set(manifest["homebox"]["attachment_sync"]), {"x", "y"})
		self.assertEqual({entry["sha256"] for entry in manifest["homebox"]["attachments"]}, {"x", "y"})

	def test_partial_attributes_merge_case_insensitively_without_duplicates(self):
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload["attributes"] += [{"name": "Edition", "value": "First"}, {"name": "Language", "value": "English"}]
		(self.item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "edit", {"attributes": [{"name": "format", "value": "Hardcover"}, {"name": "Publisher", "value": "Orbit"}]}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual({attribute["name"].casefold(): attribute["value"] for attribute in manifest["attributes"]}, {"format": "Hardcover", "edition": "First", "language": "English", "publisher": "Orbit"})
		self.assertEqual(manifest["field_sources"]["attributes.format"], "user")

	def test_partial_identifiers_merge_without_removing_siblings(self):
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload["identifiers"]["upc"] = ["123"]
		(self.item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "edit", {"identifiers": {"ISBN_13": ["9780759555952", "9780759555952"]}}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["identifiers"]["isbn_13"], ["9780759555952"])
		self.assertEqual(manifest["identifiers"]["upc"], ["123"])
		self.assertEqual(manifest["field_sources"]["identifiers.isbn_13"], "user")

	def test_user_owned_identifier_survives_reanalysis(self):
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "edit", {"identifiers": {"isbn_13": ["manual"]}}, settings=self.settings)
		raw = {"source_directory": str(self.item / "images"), "source_images": ["old-front.jpg"], "parse_status": "json_ok", "result": {"identifiers": {"isbn_13": ["vision"], "upc": ["123"]}}}
		with patch("inventory.update.run_vision", return_value=(raw, self.item / "vision.json")), patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item("000-011", "reanalyze", vision_client=object(), settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["identifiers"]["isbn_13"], ["manual"])
		self.assertEqual(manifest["identifiers"]["upc"], ["123"])

	def test_homebox_tags_reuse_create_and_preserve_unrelated_tags(self):
		current = [{"id": "signed", "name": "Signed"}, {"id": "old-type", "name": "Collectible"}]
		local = [{"name": "Type: Book", "source": "system"}, {"name": "Cyberpunk", "source": "user"}]
		with patch("inventory.homebox.list_tags", return_value=[{"id": "book", "name": "bOoK"}, {"id": "cyberpunk", "name": "Cyberpunk"}]):
			self.assertEqual(synchronized_tag_ids(current, local, ["Collectible", "Book", "Cyberpunk"], "Book"), ["signed", "book", "cyberpunk"])
		with patch("inventory.homebox.list_tags", return_value=[]):
			self.assertEqual(synchronized_tag_ids(current, local, ["Collectible", "Book", "Cyberpunk"], "Book"), ["signed"])

	def test_homebox_tag_removal_and_repeat_are_idempotent(self):
		current = [{"id": "signed", "name": "Signed"}, {"id": "cyber", "name": "Cyberpunk"}, {"id": "book", "name": "Book"}]
		local = [{"name": "Type: Book", "source": "system"}]
		with patch("inventory.homebox.list_tags", return_value=[{"id": "book", "name": "Book"}]):
			self.assertEqual(synchronized_tag_ids(current, local, ["Book", "Cyberpunk"], "Book"), ["signed", "book"])
			self.assertEqual(synchronized_tag_ids(current, local, ["Book", "Cyberpunk"], "Book"), ["signed", "book"])

	def test_catalog_uses_neutral_preview_field(self):
		write_catalog(self.settings)
		catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
		self.assertIn("preview_image_relative_path", catalog["items"][0])
		self.assertNotIn("primary_image_relative_path", catalog["items"][0])

	def test_searches_local_inventory_without_writing(self):
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8")); manifest["tags"] = [{"name": "Cyberpunk", "source": "user"}]
		(self.item / "item.json").write_text(json.dumps(manifest), encoding="utf-8")
		payload = (self.item / "item.json").read_text(encoding="utf-8")
		for query in ("000-011", self.item_id, "Cyberpunk 2077: No Coincidence", "Cyberpunk", "9780000000001"):
			self.assertEqual(search_inventory(query, settings=self.settings)["count"], 1)
		self.assertEqual(search_inventory(category="Book", settings=self.settings)["count"], 1)
		self.assertEqual(search_inventory(tags=["Cyberpunk"], settings=self.settings)["count"], 1)
		self.assertEqual(search_inventory(tags=["missing"], settings=self.settings)["status"], "not_found")
		self.assertEqual(search_inventory("nothing", settings=self.settings)["status"], "not_found")
		self.assertEqual((self.item / "item.json").read_text(encoding="utf-8"), payload)

	def test_search_returns_multiple_matches_without_selecting_one(self):
		other = self.settings.items_dir / "INV-other"; other.mkdir(parents=True)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8")); manifest["inventory_id"] = "INV-other"; manifest["asset_id"] = "000-012"; manifest["item"]["name"] = "Cyberpunk Figure"
		(other / "item.json").write_text(json.dumps(manifest), encoding="utf-8")
		result = search_inventory("Cyberpunk", settings=self.settings)
		self.assertEqual(result["count"], 2)
		self.assertEqual([item["asset_id"] for item in result["items"]], ["000-011", "000-012"])