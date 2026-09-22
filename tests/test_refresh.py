import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from inventory.homebox import HomeBoxEnumerationError, list_all_entities
from inventory.refresh import apply_refresh, build_plan, refresh


def entity(entity_id, asset_id="", fields=None, name="Item"):
	return {"id": entity_id, "assetId": asset_id, "name": name, "fields": fields or []}


class RefreshTests(unittest.TestCase):
	def setUp(self):
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name) / "inventory"
		self.settings = SimpleNamespace(persistent_data_dir=self.root, runtime_dir=self.root.parent / "runtime", items_dir=self.root / "items", backup_dir=self.root / "backups")

	def tearDown(self):
		self.temporary_directory.cleanup()

	def write_item(self, inventory_id="INV-1", asset_id="000-001", entity_id=None, identifiers=None, image_hash=""):
		item = self.settings.items_dir / inventory_id
		(item / "images").mkdir(parents=True)
		images = []
		if image_hash:
			image = item / "images" / "image.jpg"
			image.write_bytes(b"image")
			images = [{"relative_path": "images/image.jpg", "sha256": image_hash}]
		payload = {"schema": "hermes-inventory-item", "schema_version": 2, "inventory_id": inventory_id, "asset_id": asset_id, "item": {"name": "Local item"}, "identifiers": identifiers or {}, "images": images, "homebox": {"entity_id": entity_id}}
		(item / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		return payload

	def test_empty_local_root_and_empty_homebox(self):
		report = refresh(settings=self.settings, homebox_entities=[])
		self.assertEqual(report["mode"], "read_only")
		self.assertEqual(report["status"], "PASS")
		self.assertEqual(report["asset_ids"]["next_available_candidate"], "000-001")

	def test_matches_inventory_item_id_and_stored_entity_id(self):
		self.write_item("INV-field", "000-001")
		self.write_item("INV-stored", "000-002", entity_id="two")
		report = refresh(settings=self.settings, homebox_entities=[entity("one", "000-001", [{"name": "Inventory Item ID", "textValue": "INV-field"}]), entity("two", "000-002")])
		self.assertEqual(report["matches"]["represented_in_both"], [{"inventory_id": "INV-field", "entity_id": "one", "kind": "inventory_item_id"}, {"inventory_id": "INV-stored", "entity_id": "two", "kind": "stored_entity_id"}])

	def test_homebox_only_and_local_only_and_homebox_asset_occupancy(self):
		self.write_item()
		report = refresh(settings=self.settings, homebox_entities=[entity("homebox", "000-009")])
		self.assertEqual(report["matches"]["local_only"][0]["inventory_id"], "INV-1")
		self.assertEqual(report["matches"]["homebox_only"][0]["entity_id"], "homebox")
		self.assertIn("000-009", report["asset_ids"]["used_in_homebox"])

	def test_gapped_assets_produce_in_memory_next_candidate_only_when_complete(self):
		self.write_item("INV-1", "000-001")
		self.write_item("INV-2", "000-002")
		report = refresh(settings=self.settings, homebox_entities=[entity("nine", "000-009"), entity("ten", "000-010")])
		self.assertEqual(report["asset_ids"]["next_available_candidate"], "000-003")
		with patch("inventory.homebox.list_all_entities", side_effect=HomeBoxEnumerationError("failed")):
			report = refresh(settings=self.settings)
		self.assertIsNone(report["asset_ids"]["next_available_candidate"])
		self.assertEqual(report["asset_ids"]["reason"], "HomeBox state unavailable/incomplete")

	def test_reservations_transactions_and_legacy_are_reported_untouched(self):
		reservation = self.root / ".asset-id-reservations" / "000-010.json"
		reservation.parent.mkdir(parents=True)
		reservation.write_text(json.dumps({"asset_id": "000-010"}), encoding="utf-8")
		transaction = self.settings.items_dir / ".tmp-INV-transaction"
		(transaction / "images").mkdir(parents=True)
		(transaction / "images" / "photo.jpg").write_bytes(b"photo")
		metadata = self.root / "metadata" / "old.json"
		metadata.parent.mkdir(parents=True)
		metadata.write_text(json.dumps({"inventory_id": "OLD-1", "asset_id": "000-099"}), encoding="utf-8")
		pending = self.root / "pending-uploads.json"
		pending.write_text("{}", encoding="utf-8")
		report = refresh(settings=self.settings, homebox_entities=[])
		self.assertEqual(report["reservations"]["unmatched"][0]["asset_id"], "000-010")
		self.assertEqual(report["transactions"]["incomplete"][0]["path"], str(transaction))
		self.assertEqual(report["legacy"]["legacy_candidates"][0]["inventory_id"], "OLD-1")
		self.assertEqual(report["legacy"]["stale_runtime_candidates"][0]["path"], str(pending))
		self.assertTrue(reservation.exists())
		self.assertTrue(transaction.exists())
		self.assertTrue(pending.exists())

	def test_reservation_matching_homebox_is_not_unmatched(self):
		reservation = self.root / ".asset-id-reservations" / "000-010.json"
		reservation.parent.mkdir(parents=True)
		reservation.write_text(json.dumps({"asset_id": "000-010"}), encoding="utf-8")
		report = refresh(settings=self.settings, homebox_entities=[entity("homebox", "000-010")])
		self.assertEqual(report["reservations"]["entries"][0]["classification"], "reservation_matches_homebox")
		self.assertFalse(report["reservations"]["unmatched"])

	def test_current_receipts_and_valid_observations_are_not_legacy(self):
		for directory, payload in (("receipts", {"status": "created"}), ("observations", {"schema": "hermes-inventory-observation", "schema_version": 2})):
			path = self.root / directory / "current.json"
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(json.dumps(payload), encoding="utf-8")
		report = refresh(settings=self.settings, homebox_entities=[])
		self.assertFalse(report["legacy"]["legacy_candidates"])

	def test_ambiguous_product_identifier_does_not_auto_match(self):
		self.write_item(identifiers={"isbn_13": ["9780000000001"]})
		fields = [{"name": "ISBN-13", "textValue": "9780000000001"}]
		report = refresh(settings=self.settings, homebox_entities=[entity("one", fields=fields), entity("two", fields=fields)])
		self.assertEqual(report["matches"]["ambiguous"][0]["kind"], "product_identifier")
		self.assertEqual(report["matches"]["represented_in_both"], [])

	def test_image_hash_is_a_strong_match(self):
		manifest = self.write_item(image_hash="abc")
		image = self.settings.items_dir / manifest["inventory_id"] / "images" / "image.jpg"
		with patch("inventory.refresh.sha256_file", return_value="abc"):
			report = refresh(settings=self.settings, homebox_entities=[entity("one", "000-001", [{"name": "Image SHA-256", "textValue": "abc"}])])
		self.assertEqual(report["matches"]["represented_in_both"][0]["kind"], "image_sha256")

	def test_refresh_makes_no_filesystem_writes_or_homebox_mutations(self):
		self.write_item()
		before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
		with patch("inventory.homebox.requests.get", side_effect=AssertionError("refresh should use injected entities")), patch("inventory.homebox.requests.post", side_effect=AssertionError("no POST")), patch("inventory.homebox.requests.put", side_effect=AssertionError("no PUT")):
			refresh(settings=self.settings, homebox_entities=[])
		after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
		self.assertEqual(after, before)

	def test_apply_adopts_homebox_item_once_with_verified_backup(self):
		entities = [entity("homebox-1", "000-010", [{"name": "Serial Number", "textValue": "SER-1"}], "Adopted")]
		result = apply_refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(result["backup_verification"]["status"], "PASS")
		self.assertEqual([action["type"] for action in result["applied"]], ["adopt_homebox"])
		manifest_path = next(self.settings.items_dir.glob("*/item.json"))
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		self.assertEqual(manifest["homebox"]["entity_id"], "homebox-1")
		self.assertEqual(manifest["asset_id"], "000-010")
		self.assertFalse(manifest["provenance"]["local_originals"])
		self.assertEqual(manifest["images"], [])
		self.assertFalse((manifest_path.parent / "vision.json").exists())
		repeated = apply_refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(repeated["applied"], [])

	def test_apply_aborts_when_backup_fails_or_plan_changes(self):
		entities = [entity("homebox-1", "000-010")]
		with patch("inventory.backup.create_backup", return_value={"path": str(self.root / "missing.zip")}), patch("inventory.backup.verify_backup", return_value={"status": "FAIL"}):
			result = apply_refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(result["status"], "ERROR")
		self.assertFalse(self.settings.items_dir.exists())
		first = refresh(settings=self.settings, homebox_entities=entities)
		second = {**first, "homebox": {**first["homebox"], "complete": False}}
		with patch("inventory.refresh.refresh", side_effect=[first, second]):
			result = apply_refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(result["skipped"][0]["reason"], "state_changed_during_refresh")

	def test_strong_identity_conflict_is_not_adopted(self):
		self.write_item(entity_id="one")
		entities = [entity("one", "000-001"), entity("two", "000-001", [{"name": "Inventory Item ID", "textValue": "INV-1"}])]
		report = refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(report["matches"]["conflicts"][0]["type"], "strong_identity_conflict")
		self.assertFalse([action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox"])

	def test_legacy_original_images_are_reported_without_migration_guess(self):
		image = self.root / "originals" / "INV-old" / "cover.jpg"
		image.parent.mkdir(parents=True)
		image.write_bytes(b"cover")
		report = refresh(settings=self.settings, homebox_entities=[])
		self.assertEqual(report["legacy"]["orphaned_evidence"][0]["filename"], "cover.jpg")
		self.assertTrue(image.exists())


class HomeBoxPaginationTests(unittest.TestCase):
	def response(self, payload):
		result = Mock()
		result.json.return_value = payload
		result.raise_for_status.return_value = None
		return result

	def test_pagination_deduplicates_and_stops_on_repeated_page(self):
		with patch("inventory.homebox._base_url", return_value="http://homebox"), patch("inventory.homebox.auth_headers", return_value={}), patch("inventory.homebox.requests.get", side_effect=[self.response({"items": [entity("one")], "pagination": {"page": 1, "totalPages": 2}}), self.response({"items": [entity("one")], "pagination": {"page": 2, "totalPages": 3}})]) as get:
			with self.assertRaises(HomeBoxEnumerationError) as raised:
				list_all_entities()
		self.assertEqual([item["id"] for item in raised.exception.partial_entities], ["one"])
		self.assertEqual(get.call_count, 2)

	def test_pagination_returns_all_items_with_get_only(self):
		with patch("inventory.homebox._base_url", return_value="http://homebox"), patch("inventory.homebox.auth_headers", return_value={}), patch("inventory.homebox.requests.get", side_effect=[self.response({"items": [entity("one")], "pagination": {"page": 1, "totalPages": 2}}), self.response({"items": [entity("two")], "pagination": {"page": 2, "totalPages": 2}})]):
			self.assertEqual([item["id"] for item in list_all_entities()], ["one", "two"])

	def test_bare_list_pagination_requests_next_page_and_stops_empty(self):
		with patch("inventory.homebox._base_url", return_value="http://homebox"), patch("inventory.homebox.auth_headers", return_value={}), patch("inventory.homebox.requests.get", side_effect=[self.response([entity("one")]), self.response([])]) as get:
			self.assertEqual([item["id"] for item in list_all_entities(page_size=1)], ["one"])
		self.assertEqual(get.call_count, 2)

	def test_bare_list_repeated_page_is_incomplete(self):
		with patch("inventory.homebox._base_url", return_value="http://homebox"), patch("inventory.homebox.auth_headers", return_value={}), patch("inventory.homebox.requests.get", side_effect=[self.response([entity("one")]), self.response([entity("one")])]):
			with self.assertRaises(HomeBoxEnumerationError):
				list_all_entities(page_size=1)