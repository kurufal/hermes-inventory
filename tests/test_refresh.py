import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from inventory.homebox import HomeBoxEnumerationError, list_all_entities
from inventory.refresh import _apply_action, apply_refresh, build_plan, refresh
from inventory.storage import write_catalog


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
		reservation.write_text(json.dumps({"asset_id": "000-010", "reservation_id": "legacy-test", "reserved_at": "2026-09-22T17:00:00Z"}), encoding="utf-8")
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
		reservation.write_text(json.dumps({"asset_id": "000-010", "reservation_id": "current-test", "reserved_at": "2026-09-22T17:00:00Z"}), encoding="utf-8")
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
		digest = "a" * 64
		manifest = self.write_item(image_hash=digest)
		image = self.settings.items_dir / manifest["inventory_id"] / "images" / "image.jpg"
		with patch("inventory.refresh.sha256_file", return_value=digest):
			report = refresh(settings=self.settings, homebox_entities=[entity("one", "000-001", [{"name": "Image SHA-256", "textValue": digest}])])
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

	def test_asset_conflict_summary_includes_all_conflict_shapes(self):
		self.write_item("INV-1", "000-001")
		self.write_item("INV-2", "000-001")
		report = refresh(settings=self.settings, homebox_entities=[entity("one", "000-001"), entity("two", "000-001"), entity("bad", "bad-id")])
		self.assertIn("000-001", report["asset_ids"]["conflicting"])
		self.assertIn("bad-id", report["asset_ids"]["conflicting"])

	def test_only_stale_tokenized_orphan_reservation_becomes_cleanup_action(self):
		reservations = self.root / ".asset-id-reservations"; reservations.mkdir(parents=True)
		(reservations / "000-010.json").write_text(json.dumps({"asset_id": "000-010", "reservation_id": "stale", "reserved_at": "2000-01-01T00:00:00Z"}), encoding="utf-8")
		(reservations / "000-011.json").write_text(json.dumps({"asset_id": "000-011"}), encoding="utf-8")
		report = refresh(settings=self.settings, homebox_entities=[])
		plan = build_plan(report)
		self.assertEqual([action["type"] for action in plan["actions"]], ["cleanup_stale_reservation"])
		self.assertEqual({entry["classification"] for entry in report["reservations"]["entries"]}, {"stale_orphaned_reservation", "legacy_tokenless_reservation"})
		result = apply_refresh(settings=self.settings, homebox_entities=[])
		self.assertEqual(result["applied"][0]["type"], "cleanup_stale_reservation")
		self.assertFalse((reservations / "000-010.json").exists())
		self.assertTrue((reservations / "000-011.json").exists())

	def test_refresh_normalizes_only_system_type_tags(self):
		payload = self.write_item()
		payload["tags"] = [{"name": "Type: Book", "source": "system"}, {"name": "Type: Personal", "source": "user"}]
		path = self.settings.items_dir / "INV-1" / "item.json"; path.write_text(json.dumps(payload), encoding="utf-8")
		report = refresh(settings=self.settings, homebox_entities=[])
		action = next(action for action in build_plan(report)["actions"] if action["type"] == "update_manifest")
		self.assertEqual(action["reasons"], ["upgrade_schema", "normalize_system_type_tags"])
		_apply_action(action, report, self.settings)
		self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tags"], [{"name": "Type: Personal", "source": "user"}])

	def test_link_precondition_includes_homebox_identity_evidence(self):
		self.write_item(identifiers={"serial_number": ["SER-1"]})
		report = refresh(settings=self.settings, homebox_entities=[entity("one", "000-001", [{"name": "Serial Number", "textValue": "SER-1"}])])
		action = next(action for action in build_plan(report)["actions"] if action.get("reasons") == ["upgrade_schema", "link_homebox"])
		changed = {**report, "homebox": {**report["homebox"], "items": [{**report["homebox"]["items"][0], "fields": [{"name": "Serial Number", "textValue": "DIFFERENT"}]}]}}
		with self.assertRaisesRegex(RuntimeError, "precondition"):
			_apply_action(action, changed, self.settings)

	def test_partial_apply_rebuilds_catalog_before_reporting_action_failure(self):
		payload = self.write_item(); payload["tags"] = [{"name": "Type: Book", "source": "system"}]
		(self.settings.items_dir / "INV-1" / "item.json").write_text(json.dumps(payload), encoding="utf-8")
		reservation = self.root / ".asset-id-reservations" / "000-010.json"; reservation.parent.mkdir()
		reservation.write_text(json.dumps({"asset_id": "000-010", "reservation_id": "stale", "reserved_at": "2000-01-01T00:00:00Z"}), encoding="utf-8")
		calls = []
		def fail_second(action, report, settings):
			calls.append(action)
			if len(calls) == 1:
				return _apply_action(action, report, settings)
			raise RuntimeError("injected action failure")
		with patch("inventory.refresh._apply_action", side_effect=fail_second), patch("inventory.refresh.write_catalog", wraps=write_catalog) as catalog:
			result = apply_refresh(settings=self.settings, homebox_entities=[])
		self.assertEqual(result["status"], "ERROR")
		self.assertEqual(len(result["applied"]), 1)
		catalog.assert_called()
		self.assertTrue((self.root / "catalog.json").is_file())

	def write_legacy(self, inventory_id, asset_id, entity_id=None):
		metadata = self.root / "metadata" / f"{inventory_id}.json"; metadata.parent.mkdir(parents=True, exist_ok=True)
		payload = {"inventory_id": inventory_id, "asset_id": asset_id, "source_images": ["cover.jpg"], "result": {"object_type": {"value": "Book"}, "product_or_title": {"value": inventory_id}}}
		if entity_id:
			payload["homebox_entity_id"] = entity_id
		metadata.write_text(json.dumps(payload), encoding="utf-8")
		original = self.root / "originals" / inventory_id; original.mkdir(parents=True, exist_ok=True); (original / "cover.jpg").write_bytes(b"cover-" + inventory_id.encode())

	def test_legacy_claims_linked_homebox_before_adoption(self):
		self.write_legacy("INV-old", "000-009", "hb-1")
		entities = [entity("hb-1", "000-009", [{"name": "Inventory Item ID", "textValue": "INV-old"}])]
		plan = build_plan(refresh(settings=self.settings, homebox_entities=entities))
		self.assertEqual([(action["type"], action["inventory_id"], action.get("entity_id")) for action in plan["actions"]], [("migrate_legacy", "INV-old", "hb-1")])

	def test_legacy_explicit_link_without_inventory_field_prevents_adoption(self):
		self.write_legacy("INV-old", "000-009", "hb-1")
		plan = build_plan(refresh(settings=self.settings, homebox_entities=[entity("hb-1", "000-009")]))
		self.assertEqual([action["type"] for action in plan["actions"]], ["migrate_legacy"])

	def test_legacy_identity_conflict_blocks_migration_and_adoption(self):
		self.write_legacy("INV-old", "000-009", "hb-1")
		entities = [entity("hb-1", "000-009"), entity("hb-2", "000-010", [{"name": "Inventory Item ID", "textValue": "INV-old"}])]
		report = refresh(settings=self.settings, homebox_entities=entities)
		self.assertIn("legacy_homebox_identity_conflict", [entry["type"] for entry in report["conflicts"]])
		self.assertFalse([action for action in build_plan(report)["actions"] if action["type"] in {"migrate_legacy", "adopt_homebox"}])

	def test_unrelated_legacy_and_homebox_each_get_one_action(self):
		self.write_legacy("INV-old", "000-009")
		plan = build_plan(refresh(settings=self.settings, homebox_entities=[entity("hb-1", "000-010")]))
		self.assertEqual({action["type"] for action in plan["actions"]}, {"migrate_legacy", "adopt_homebox"})

	def test_adoption_drops_none_identifiers_and_keeps_top_level_serial_model(self):
		entities = [dict(entity("hb-1", "000-010"), serialNumber="SER-1", modelNumber="MODEL-1")]
		result = apply_refresh(settings=self.settings, homebox_entities=entities)
		manifest = json.loads(next(self.settings.items_dir.glob("*/item.json")).read_text(encoding="utf-8"))
		self.assertEqual(manifest["identifiers"], {"serial_number": ["SER-1"], "model_number": ["MODEL-1"]})
		self.assertEqual(result["applied"][0]["type"], "adopt_homebox")

	def test_empty_adoption_identifiers_remain_empty(self):
		result = apply_refresh(settings=self.settings, homebox_entities=[entity("hb-empty", "000-010")])
		manifest = json.loads(next(self.settings.items_dir.glob("*/item.json")).read_text(encoding="utf-8"))
		self.assertEqual(manifest["identifiers"], {})
		self.assertEqual(result["applied"][0]["type"], "adopt_homebox")

	def test_adoption_merges_repeated_custom_identifier_fields(self):
		fields = [{"name": "Serial Number", "textValue": "SER-1"}, {"name": "Serial Number", "textValue": "SER-2"}]
		apply_refresh(settings=self.settings, homebox_entities=[entity("hb-many", "000-010", fields)])
		manifest = json.loads(next(self.settings.items_dir.glob("*/item.json")).read_text(encoding="utf-8"))
		self.assertEqual(manifest["identifiers"], {"serial_number": ["SER-1", "SER-2"]})

	def test_asset_conflicts_block_adoption_and_links(self):
		self.write_item("INV-local", "000-001")
		report = refresh(settings=self.settings, homebox_entities=[entity("hb-other", "000-001")])
		self.assertFalse([action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox"])
		report = refresh(settings=self.settings, homebox_entities=[entity("a", "000-002"), entity("b", "000-002")])
		self.assertFalse([action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox"])

	def test_unsafe_external_inventory_ids_never_become_item_directories(self):
		for unsafe in ("../escape", "..\\escape", "INV-/slash", "INV-\\slash", "C:\\outside"):
			report = refresh(settings=self.settings, homebox_entities=[entity("hb-" + str(len(unsafe)), "000-010", [{"name": "Inventory Item ID", "textValue": unsafe}])])
			action = next(action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox")
			self.assertTrue(action["inventory_id"].startswith("INV-HB-"))
		metadata = self.root / "metadata" / "unsafe.json"; metadata.parent.mkdir(parents=True, exist_ok=True)
		metadata.write_text(json.dumps({"inventory_id": "../unsafe", "asset_id": "000-011"}), encoding="utf-8")
		self.assertFalse(build_plan(refresh(settings=self.settings, homebox_entities=[]))["actions"])

	def test_combined_legacy_and_homebox_repair_is_idempotent(self):
		self.write_legacy("INV-A", "000-009", "hb-a")
		self.write_legacy("INV-B", "000-010", "hb-b")
		entities = [entity("hb-a", "000-009", [{"name": "Inventory Item ID", "textValue": "INV-A"}]), entity("hb-b", "000-010"), entity("hb-c", "000-011")]
		plan = build_plan(refresh(settings=self.settings, homebox_entities=entities))
		self.assertEqual([entry["type"] for entry in plan["actions"]], ["migrate_legacy", "migrate_legacy", "adopt_homebox"])
		adopted_id = plan["actions"][-1]["inventory_id"]
		result = apply_refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(len(result["applied"]), 3)
		manifests = [json.loads(path.read_text(encoding="utf-8")) for path in self.settings.items_dir.glob("*/item.json")]
		self.assertEqual({manifest["inventory_id"] for manifest in manifests}, {"INV-A", "INV-B", adopted_id})
		self.assertEqual(next(manifest for manifest in manifests if manifest["inventory_id"] == "INV-A")["homebox"]["entity_id"], "hb-a")
		self.assertEqual(next(manifest for manifest in manifests if manifest["inventory_id"] == "INV-B")["status"], "synced")
		self.assertEqual(apply_refresh(settings=self.settings, homebox_entities=entities)["applied"], [])

	def test_live_link_revalidates_current_homebox_identity(self):
		self.write_item(identifiers={"serial_number": ["SER-1"]})
		report = refresh(settings=self.settings, homebox_entities=[entity("hb-1", "000-001", [{"name": "Serial Number", "textValue": "SER-1"}])])
		action = next(action for action in build_plan(report)["actions"] if "link_homebox" in action.get("reasons", []))
		with patch("inventory.homebox.get_entity", return_value=entity("hb-1", "000-001", [{"name": "Serial Number", "textValue": "changed"}])):
			with self.assertRaisesRegex(RuntimeError, "HomeBox identity changed"):
				_apply_action(action, report, self.settings, live_homebox=True)

	def test_refresh_uses_detailed_homebox_fields_for_adoption_and_live_check(self):
		hash_a, hash_b = "a" * 64, "b" * 64
		summary = entity("hb-1", "000-001", [], "Cyberpunk")
		detail = entity("hb-1", "000-001", [{"name": "Inventory Item ID", "value": "INV-20260901-204535-601208be"}, {"name": "Image SHA-256", "value": f"{hash_a}; {hash_b}"}], "Cyberpunk")
		with patch("inventory.homebox.list_all_entities", return_value=[summary]), patch("inventory.homebox.get_entity", return_value=detail):
			report = refresh(settings=self.settings)
			action = next(action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox")
			self.assertEqual(action["inventory_id"], "INV-20260901-204535-601208be")
			self.assertEqual([field["textValue"] for field in report["homebox"]["items"][0]["fields"] if field["name"] == "Image SHA-256"], [hash_a, hash_b])
			_apply_action(action, report, self.settings, live_homebox=True)
		manifest = json.loads(next(self.settings.items_dir.glob("*/item.json")).read_text(encoding="utf-8"))
		self.assertEqual(manifest["inventory_id"], "INV-20260901-204535-601208be")

	def test_live_adoption_identity_change_still_fails_closed(self):
		planned = entity("hb-1", "000-001", [{"name": "Inventory Item ID", "value": "INV-A"}])
		report = refresh(settings=self.settings, homebox_entities=[planned])
		action = next(action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox")
		with patch("inventory.homebox.get_entity", return_value=entity("hb-1", "000-001", [{"name": "Inventory Item ID", "value": "INV-B"}])):
			with self.assertRaisesRegex(RuntimeError, "HomeBox identity changed"):
				_apply_action(action, report, self.settings, live_homebox=True)
		with patch("inventory.homebox.get_entity", return_value=entity("hb-1", "000-002", [{"name": "Inventory Item ID", "value": "INV-A"}])):
			with self.assertRaisesRegex(RuntimeError, "HomeBox identity changed"):
				_apply_action(action, report, self.settings, live_homebox=True)

	def test_duplicate_and_unsafe_homebox_inventory_ids_are_not_adopted_as_paths(self):
		duplicate = [entity("one", "000-001", [{"name": "Inventory Item ID", "value": "INV-duplicate"}]), entity("two", "000-002", [{"name": "Inventory Item ID", "value": "INV-duplicate"}])]
		report = refresh(settings=self.settings, homebox_entities=duplicate)
		self.assertIn("duplicate_homebox_inventory_id", [conflict["type"] for conflict in report["conflicts"]])
		self.assertFalse([action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox"])
		report = refresh(settings=self.settings, homebox_entities=[entity("unsafe", "000-003", [{"name": "Inventory Item ID", "value": "../unsafe"}])])
		action = next(action for action in build_plan(report)["actions"] if action["type"] == "adopt_homebox")
		self.assertTrue(action["inventory_id"].startswith("INV-HB-"))

	def test_detailed_homebox_enumeration_failure_fails_closed(self):
		with patch("inventory.homebox.list_all_entities", return_value=[entity("hb-1", "000-001")]), patch("inventory.homebox.get_entity", side_effect=RuntimeError("unavailable")):
			report = refresh(settings=self.settings)
		self.assertFalse(report["homebox"]["complete"])
		self.assertIn("detail enumeration failed", report["homebox"]["error"])
		self.assertEqual(build_plan(report)["actions"], [])

	def test_malformed_homebox_image_hash_is_not_strong_identity_evidence(self):
		digest = "c" * 64
		self.write_item(image_hash=digest)
		image = self.settings.items_dir / "INV-1" / "images" / "image.jpg"
		with patch("inventory.refresh.sha256_file", return_value=digest):
			report = refresh(settings=self.settings, homebox_entities=[entity("hb-1", "000-002", [{"name": "Image SHA-256", "value": f"not-a-hash; {digest[:-1]}x"}])])
		self.assertEqual(report["matches"]["represented_in_both"], [])
		self.assertEqual(report["matches"]["local_only"][0]["inventory_id"], "INV-1")

	def test_legacy_retry_group_uses_verified_hashes_and_migrates_only_complete_evidence(self):
		images = {"one.png": b"one", "two.png": b"two"}
		for inventory_id, names in (("INV-full", ("one.png", "two.png")), ("INV-one", ("one.png",)), ("INV-two", ("two.png",))):
			metadata = self.root / "metadata" / f"{inventory_id}.json"; metadata.parent.mkdir(parents=True, exist_ok=True)
			metadata.write_text(json.dumps({"inventory_id": inventory_id, "asset_id": "000-009"}), encoding="utf-8")
			directory = self.root / "originals" / inventory_id; directory.mkdir(parents=True)
			for name in names:
				(directory / name).write_bytes(images[name])
		hashes = [__import__("hashlib").sha256(value).hexdigest() for value in images.values()]
		entity_fields = [{"name": "Inventory Item ID", "value": "INV-HIST"}, {"name": "Image SHA-256", "value": "; ".join(hashes)}]
		report = refresh(settings=self.settings, homebox_entities=[entity("hb-1", "000-009", entity_fields)])
		self.assertEqual(len(next(entry for entry in report["legacy"]["legacy_candidates"] if entry["inventory_id"] == "INV-full")["hashes"]), 2)
		plan = build_plan(report)
		migrations = [action for action in plan["actions"] if action["type"] == "migrate_legacy"]
		self.assertEqual([(action["inventory_id"], action["entity_id"]) for action in migrations], [("INV-HIST", "hb-1")])
		self.assertFalse([action for action in plan["actions"] if action["type"] == "adopt_homebox"])

	def test_apply_coalesces_upgrade_and_system_type_tag_normalization_once(self):
		payload = self.write_item()
		payload["tags"] = [{"name": "Type: Book", "source": "system"}, {"name": "Keep", "source": "user"}]
		path = self.settings.items_dir / "INV-1" / "item.json"; path.write_text(json.dumps(payload), encoding="utf-8")
		plan = build_plan(refresh(settings=self.settings, homebox_entities=[]))
		self.assertEqual([(action["type"], action["reasons"]) for action in plan["actions"]], [("update_manifest", ["upgrade_schema", "normalize_system_type_tags"])])
		result = apply_refresh(settings=self.settings, homebox_entities=[])
		self.assertNotEqual(result["status"], "ERROR")
		self.assertEqual(result["backup_verification"]["status"], "PASS")
		manifest = json.loads(path.read_text(encoding="utf-8"))
		self.assertEqual(manifest["schema_version"], 3)
		self.assertEqual(manifest["tags"], [{"name": "Keep", "source": "user"}])
		self.assertTrue((self.root / "catalog.json").is_file())
		self.assertEqual(apply_refresh(settings=self.settings, homebox_entities=[])["applied"], [])

	def test_apply_composes_all_compatible_manifest_mutations(self):
		payload = self.write_item()
		payload["tags"] = [{"name": "Type: Book", "source": "system"}]
		path = self.settings.items_dir / "INV-1" / "item.json"; path.write_text(json.dumps(payload), encoding="utf-8")
		entities = [entity("hb-1", "000-001", [{"name": "Inventory Item ID", "textValue": "INV-1"}])]
		plan = build_plan(refresh(settings=self.settings, homebox_entities=entities))
		self.assertEqual([(action["type"], action["reasons"]) for action in plan["actions"]], [("update_manifest", ["upgrade_schema", "normalize_system_type_tags", "link_homebox"])])
		result = apply_refresh(settings=self.settings, homebox_entities=entities)
		self.assertEqual(result["status"], "PASS")
		manifest = json.loads(path.read_text(encoding="utf-8"))
		self.assertEqual(manifest["schema_version"], 3)
		self.assertEqual(manifest["tags"], [])
		self.assertEqual(manifest["homebox"]["entity_id"], "hb-1")
		self.assertEqual(apply_refresh(settings=self.settings, homebox_entities=entities)["applied"], [])

	def test_partially_upgraded_manifest_plans_only_remaining_tag_cleanup(self):
		payload = self.write_item()
		payload["schema_version"] = 3
		payload["tags"] = [{"name": "Type: Book", "source": "system"}]
		path = self.settings.items_dir / "INV-1" / "item.json"; path.write_text(json.dumps(payload), encoding="utf-8")
		plan = build_plan(refresh(settings=self.settings, homebox_entities=[]))
		self.assertEqual([(action["type"], action["reasons"]) for action in plan["actions"]], [("update_manifest", ["normalize_system_type_tags"])])
		result = apply_refresh(settings=self.settings, homebox_entities=[])
		self.assertNotEqual(result["status"], "ERROR")
		self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tags"], [])

	def test_external_manifest_change_fails_closed_before_coalesced_write(self):
		payload = self.write_item(); payload["tags"] = [{"name": "Type: Book", "source": "system"}]
		path = self.settings.items_dir / "INV-1" / "item.json"; path.write_text(json.dumps(payload), encoding="utf-8")
		original_apply = _apply_action
		def externally_change_then_apply(action, report, settings, **kwargs):
			external = json.loads(path.read_text(encoding="utf-8")); external["item"]["name"] = "External edit"; path.write_text(json.dumps(external), encoding="utf-8")
			return original_apply(action, report, settings, **kwargs)
		with patch("inventory.refresh._apply_action", side_effect=externally_change_then_apply):
			result = apply_refresh(settings=self.settings, homebox_entities=[])
		self.assertEqual(result["status"], "ERROR")
		self.assertEqual(result["applied"], [])
		self.assertIn("precondition changed", result["failed"]["error"])
		manifest = json.loads(path.read_text(encoding="utf-8"))
		self.assertEqual(manifest["item"]["name"], "External edit")
		self.assertEqual(manifest["schema_version"], 2)


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