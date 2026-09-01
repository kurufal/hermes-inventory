"""Canonical Inventory update behavior."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.homebox import synchronized_tag_ids
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
			result = update_item("000-011", "edit", {"purchase_price": 14.99, "purchase_from": "Half Price Books - Tacoma, WA", "attributes": [{"name": "Format", "value": "Hardcover"}]}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "updated")
		self.assertEqual(manifest["asset_id"], "000-011")
		self.assertEqual(manifest["inventory_id"], self.item_id)
		self.assertEqual(manifest["purchase_price"], 14.99)
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

	def test_resync_does_not_run_vision(self):
		with patch("inventory.update.run_vision", side_effect=AssertionError("vision")), patch("inventory.homebox.complete_entity", return_value={"attachments": []}) as sync:
			result = update_item("9780000000001", "resync", settings=self.settings)
		self.assertEqual(result["operation"], "resync")
		self.assertFalse(sync.call_args.kwargs["upload_attachments"])

	def test_asset_id_edit_requires_valid_unused_value(self):
		with self.assertRaisesRegex(ValueError, "NNN-NNN"):
			update_item("000-011", "edit", {"asset_id": "book1"}, settings=self.settings)
		other = self.settings.items_dir / "INV-other"; other.mkdir(parents=True)
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload["inventory_id"] = "INV-other"; payload["asset_id"] = "000-025"
		(other / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "already in use"):
			update_item("000-011", "edit", {"asset_id": "000-025"}, settings=self.settings)

	def test_edit_renames_images_and_retains_hash_and_provenance(self):
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
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
		self.assertIn({"name": "Type: Book", "source": "system"}, manifest["tags"])

	def test_legacy_manifest_gets_asset_id_and_schema_when_resynced(self):
		payload = json.loads((self.item / "item.json").read_text(encoding="utf-8")); payload.pop("asset_id"); payload.pop("field_sources"); payload.pop("history"); payload["schema_version"] = 1
		(self.item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		with patch("inventory.homebox.complete_entity", return_value={"attachments": []}):
			update_item(self.item_id, "resync", settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["schema_version"], 2)
		self.assertEqual(manifest["asset_id"], "000-001")

	def test_homebox_failure_keeps_local_edit_pending_for_later_resync(self):
		with patch("inventory.homebox.complete_entity", side_effect=RuntimeError("offline")):
			result = update_item("000-011", "edit", {"purchase_price": 14.99}, settings=self.settings)
		manifest = json.loads((self.item / "item.json").read_text(encoding="utf-8"))
		self.assertEqual(result["status"], "updated")
		self.assertEqual(manifest["status"], "pending_homebox_sync")
		self.assertEqual(manifest["purchase_price"], 14.99)

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
		current = [{"id": "signed", "name": "Signed"}, {"id": "old-type", "name": "Type: Collectible"}]
		local = [{"name": "Type: Book", "source": "system"}, {"name": "Cyberpunk", "source": "user"}]
		with patch("inventory.homebox.list_tags", return_value=[{"id": "book", "name": "type: book"}, {"id": "cyberpunk", "name": "Cyberpunk"}]), patch("inventory.homebox.create_tag") as create:
			self.assertEqual(synchronized_tag_ids(current, local), ["signed", "book", "cyberpunk"])
		create.assert_not_called()
		with patch("inventory.homebox.list_tags", return_value=[]), patch("inventory.homebox.create_tag", side_effect=[{"id": "book", "name": "Type: Book"}, {"id": "cyber", "name": "Cyberpunk"}]) as create:
			self.assertEqual(synchronized_tag_ids(current, local), ["signed", "book", "cyber"])
		self.assertEqual(create.call_count, 2)

	def test_homebox_tag_removal_and_repeat_are_idempotent(self):
		current = [{"id": "signed", "name": "Signed"}, {"id": "cyber", "name": "Cyberpunk"}, {"id": "book", "name": "Type: Book"}]
		local = [{"name": "Type: Book", "source": "system"}]
		with patch("inventory.homebox.list_tags", return_value=[{"id": "book", "name": "Type: Book"}]):
			self.assertEqual(synchronized_tag_ids(current, local, ["Type: Book", "Cyberpunk"]), ["signed", "book"])
			self.assertEqual(synchronized_tag_ids(current, local, ["Type: Book", "Cyberpunk"]), ["signed", "book"])

	def test_catalog_uses_neutral_preview_field(self):
		write_catalog(self.settings)
		catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
		self.assertIn("preview_image_relative_path", catalog["items"][0])
		self.assertNotIn("primary_image_relative_path", catalog["items"][0])