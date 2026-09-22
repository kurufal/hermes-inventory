"""Tests for ingest orchestration and vision failure diagnostics."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.ingest import VisionParseError, ingest, prepare_originals, run_vision


class FakeVisionClient:
	def __init__(self, response):
		self.response = response

	def complete_structured(self, **kwargs):
		del kwargs
		return self.response


class RunVisionDiagnosticsTests(unittest.TestCase):
	def test_unparseable_response_raises_structured_vision_parse_error(self):
		response = SimpleNamespace(
			parsed=None,
			text="not valid JSON" * 200,  # exceeds the 1000-char preview bound
			provider="test-provider",
			model="test-vision-model",
			agent_id="default",
			audit={"content_type": "text/plain"},
		)
		client = FakeVisionClient(response)

		with tempfile.TemporaryDirectory() as temporary_directory:
			temporary_path = Path(temporary_directory)
			item_directory = temporary_path / "INV-test"
			item_directory.mkdir()
			(item_directory / "front.jpg").write_bytes(b"jpg-bytes")

			with patch("inventory.vision.METADATA_DIR", temporary_path / "metadata"):
				with self.assertRaises(VisionParseError) as ctx:
					run_vision(item_directory, client)

			error = ctx.exception
			self.assertEqual(
				str(error),
				"Vision model did not return valid structured JSON",
			)
			debug = error.debug
			self.assertEqual(debug["error_stage"], "vision_json_parse")
			self.assertEqual(debug["provider"], "test-provider")
			self.assertEqual(debug["model"], "test-vision-model")
			self.assertEqual(debug["content_type"], "text/plain")
			self.assertEqual(debug["input_image_count"], 1)
			self.assertEqual(debug["input_image_filenames"], ["front.jpg"])
			self.assertTrue(Path(debug["metadata_path"]).is_file())
			self.assertLessEqual(len(debug["raw_response_preview"]), 1000)
			self.assertTrue(debug["raw_response_preview"].startswith("not valid JSON"))

	def test_does_not_claim_image_quality_unless_backend_said_so(self):
		response = SimpleNamespace(
			parsed=None,
			text="unparseable",
			provider="test-provider",
			model="test-vision-model",
			agent_id="default",
			audit={},
		)
		client = FakeVisionClient(response)

		with tempfile.TemporaryDirectory() as temporary_directory:
			temporary_path = Path(temporary_directory)
			item_directory = temporary_path / "INV-test"
			item_directory.mkdir()
			(item_directory / "front.jpg").write_bytes(b"jpg-bytes")

			with patch("inventory.vision.METADATA_DIR", temporary_path / "metadata"):
				with self.assertRaises(VisionParseError) as ctx:
					run_vision(item_directory, client)

		message = str(ctx.exception).lower()
		for banned_phrase in ("blurry", "low quality", "poor image", "unclear"):
			self.assertNotIn(banned_phrase, message)


class IngestCommittedImageTests(unittest.TestCase):
	def setUp(self):
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name)
		self.source = self.root / "Roaming" / "Hermes" / "composer-images"
		self.source.mkdir(parents=True)
		(self.source / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").write_bytes(b"front-image")
		(self.source / "composer_2026-09-01_20-16-27-691_575083.jpg").write_bytes(b"back-image")
		(self.source / ".inventory-provenance.json").write_text(
			json.dumps({
				"composer_2026-09-01_20-16-27-642_9b27f5.jpg": "composer_2026-09-01_20-16-27-642_9b27f5.jpg",
				"composer_2026-09-01_20-16-27-691_575083.jpg": "composer_2026-09-01_20-16-27-691_575083.jpg",
			}),
			encoding="utf-8",
		)
		self.settings = SimpleNamespace(
			persistent_data_dir=self.root / "inventory",
			items_dir=self.root / "inventory" / "items",
		)
		self.vision_directories = []
		self.homebox_directories = []

	def tearDown(self):
		self.temporary_directory.cleanup()

	def fake_vision(self, image_directory, vision_client, *, metadata_path):
		del vision_client
		image_directory = Path(image_directory)
		self.vision_directories.append(image_directory)
		self.assertTrue(image_directory.name == "images")
		self.assertTrue(image_directory.parent.name.startswith(".tmp-"))
		self.assertTrue((image_directory / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").is_file())
		self.assertTrue((image_directory / "composer_2026-09-01_20-16-27-691_575083.jpg").is_file())
		raw = {
			"item_id": image_directory.parent.name,
			"source_directory": str(image_directory),
			"source_images": ["composer_2026-09-01_20-16-27-642_9b27f5.jpg", "composer_2026-09-01_20-16-27-691_575083.jpg"],
			"parse_status": "json_ok",
			"result": {
				"object_type": {"value": "Camera", "confidence": 1.0},
				"product_or_title": {"value": "Test Camera", "confidence": 1.0},
				"manufacturer_or_publisher": {"value": "Hermes", "confidence": 1.0},
				"identifiers": {"serial_number": ["SER-1"]},
				"physical_description": {"value": "Test item", "confidence": 1.0},
				"image_roles": [
					{"filename": "composer_2026-09-01_20-16-27-642_9b27f5.jpg", "inferred_role": "front", "confidence": 1.0},
					{"filename": "composer_2026-09-01_20-16-27-691_575083.jpg", "inferred_role": "back", "confidence": 1.0},
				],
			},
		}
		metadata_path.write_text(json.dumps(raw), encoding="utf-8")
		return raw, metadata_path

	def _run_ingest(self, complete_side_effect=None, duplicate_result=None):
		def create(record):
			self.homebox_directories.append(Path(record["source_directory"]))
			return {"id": "entity-1", "assetId": "asset-1", "groupId": "group-1", "entityTypeId": "type-1"}

		def complete(entity_id, record, *, image_directory=None, attachment_sync=None, on_attachment_uploaded=None):
			self.assertEqual(entity_id, "entity-1")
			self.assertEqual(attachment_sync, {})
			directory = Path(image_directory)
			self.homebox_directories.append(directory)
			self.assertEqual(Path(record["source_directory"]), directory)
			self.assertTrue(all((directory / name).is_file() for name in record["source_images"]))
			self.assertTrue(all(name.startswith(record["asset_id"] + "_") for name in record["source_images"]))
			if complete_side_effect:
				raise complete_side_effect
			return {"entity": {"assetId": "asset-1", "groupId": "group-1", "entityType": {"id": "type-1"}}, "attachments": [{"id": "attachment-1"}, {"id": "attachment-2"}]}

		with patch("inventory.ingest.run_vision", side_effect=self.fake_vision), patch(
			"inventory.ingest.check_homebox_duplicates",
			return_value=duplicate_result or {"classification": "NEW_ITEM", "candidates": []},
		), patch("inventory.ingest.create_entity", side_effect=create), patch(
			"inventory.ingest.complete_entity", side_effect=complete,
		), patch(
			"inventory.homebox.list_all_entities", return_value=[],
		):
			return ingest(self.source, object(), settings=self.settings)

	def _assert_canonical_evidence(self, result):
		item_root = self.settings.items_dir / result["item_id"]
		images = item_root / "images"
		self.assertTrue(item_root.is_dir())
		self.assertTrue((item_root / "item.json").is_file())
		self.assertTrue((item_root / "vision.json").is_file())
		self.assertFalse(list(self.settings.items_dir.glob(".tmp-*")))
		manifest = json.loads((item_root / "item.json").read_text(encoding="utf-8"))
		raw = json.loads((item_root / "vision.json").read_text(encoding="utf-8"))
		self.assertNotIn(".tmp-", json.dumps(manifest))
		self.assertNotIn(".tmp-", json.dumps(raw))
		self.assertEqual(raw["source_directory"], str(images))
		self.assertEqual(len(manifest["images"]), 2)
		self.assertTrue(all((images / Path(image["relative_path"]).name).is_file() for image in manifest["images"]))
		self.assertTrue(all(Path(image["relative_path"]).name.startswith(manifest["asset_id"] + "_") for image in manifest["images"]))
		self.assertEqual({image["source_filename"] for image in manifest["images"]}, {
			"composer_2026-09-01_20-16-27-642_9b27f5.jpg",
			"composer_2026-09-01_20-16-27-691_575083.jpg",
		})
		return item_root, images, manifest

	def test_attachments_use_final_committed_image_directory(self):
		result = self._run_ingest()
		_, images, manifest = self._assert_canonical_evidence(result)

		self.assertTrue(result["durable"])
		self.assertEqual(result["status"], "created")
		self.assertEqual(manifest["status"], "synced")
		self.assertTrue(self.vision_directories[0].parent.name.startswith(".tmp-"))
		self.assertTrue(all(directory == images for directory in self.homebox_directories))
		self.assertTrue(all(".tmp-" not in str(directory) for directory in self.homebox_directories))

	def test_completion_failure_retains_entity_and_canonical_evidence(self):
		result = self._run_ingest(RuntimeError("attachment upload failed"))
		_, images, manifest = self._assert_canonical_evidence(result)

		self.assertTrue(result["durable"])
		self.assertEqual(result["status"], "pending_homebox_sync")
		self.assertEqual(manifest["status"], "pending_homebox_sync")
		self.assertEqual(manifest["homebox"]["entity_id"], "entity-1")
		self.assertTrue(all(directory == images for directory in self.homebox_directories))
		self.assertTrue(all(".tmp-" not in str(directory) for directory in self.homebox_directories))

	def test_direct_source_directory_api_supports_manual_development_fixture(self):
		manual_source = self.root / "user_media" / "next-item"
		manual_source.mkdir(parents=True, exist_ok=True)
		(manual_source / "manual.jpg").write_bytes(b"manual")
		destination = self.root / "copied-originals"
		destination.mkdir()
		prepare_originals(manual_source, destination)
		self.assertEqual((destination / "manual.jpg").read_bytes(), b"manual")

	def test_exact_canonical_image_duplicate_skips_vision_and_transaction(self):
		item = self.settings.items_dir / "INV-existing"
		images = item / "images"
		images.mkdir(parents=True)
		duplicate = images / "existing.jpg"
		duplicate.write_bytes((self.source / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").read_bytes())
		from inventory.hashing import sha256_file
		(item / "item.json").write_text(json.dumps({"schema": "hermes-inventory-item", "schema_version": 3, "inventory_id": "INV-existing", "asset_id": "000-001", "images": [{"relative_path": "images/existing.jpg", "sha256": sha256_file(duplicate)}]}), encoding="utf-8")
		with patch("inventory.ingest.run_vision", side_effect=AssertionError("vision must not run")):
			result = ingest(self.source, object(), settings=self.settings)
		self.assertEqual(result["status"], "existing_item_with_new_image_evidence")
		self.assertFalse(list(self.settings.items_dir.glob(".tmp-*")))

	def test_all_exact_images_skip_vision_transaction_and_homebox_creation(self):
		item = self.settings.items_dir / "INV-existing"; images = item / "images"; images.mkdir(parents=True)
		from inventory.hashing import sha256_file
		entries = []
		for source in self.source.glob("*.jpg"):
			target = images / source.name; target.write_bytes(source.read_bytes())
			entries.append({"relative_path": f"images/{target.name}", "sha256": sha256_file(target)})
		(item / "item.json").write_text(json.dumps({"schema": "hermes-inventory-item", "schema_version": 3, "inventory_id": "INV-existing", "asset_id": "000-001", "images": entries}), encoding="utf-8")
		with patch("inventory.ingest.run_vision", side_effect=AssertionError("vision")), patch("inventory.ingest.begin_item_transaction", side_effect=AssertionError("transaction")), patch("inventory.ingest.create_entity", side_effect=AssertionError("create")):
			result = ingest(self.source, object(), settings=self.settings)
		self.assertEqual(result["status"], "exact_image_duplicate")
		self.assertEqual(result["classification"], "EXACT_IMAGE_DUPLICATE")

	def test_identical_incoming_filenames_bytes_use_one_vision_representative(self):
		(self.source / "duplicate.jpg").write_bytes((self.source / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").read_bytes())
		result = self._run_ingest()
		_, _, manifest = self._assert_canonical_evidence(result)
		self.assertEqual(len(manifest["images"]), 2)

	def test_copy_checksum_mismatch_stops_before_vision(self):
		def corrupt_copy(source, destination, images=None):
			prepare_originals(source, destination, images)
			(destination / Path(images[0]).name).write_bytes(b"corrupt")
			return {}
		with patch("inventory.ingest.prepare_originals", side_effect=corrupt_copy), patch("inventory.ingest.run_vision", side_effect=AssertionError("vision")):
			with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
				ingest(self.source, object(), settings=self.settings)

	def test_same_filename_with_different_bytes_is_not_an_exact_duplicate(self):
		item = self.settings.items_dir / "INV-existing"; images = item / "images"; images.mkdir(parents=True)
		filename = "composer_2026-09-01_20-16-27-642_9b27f5.jpg"
		(images / filename).write_bytes(b"different bytes")
		from inventory.hashing import sha256_file
		(item / "item.json").write_text(json.dumps({"schema": "hermes-inventory-item", "schema_version": 3, "inventory_id": "INV-existing", "asset_id": "000-001", "images": [{"relative_path": f"images/{filename}", "sha256": sha256_file(images / filename)}]}), encoding="utf-8")
		result = self._run_ingest()
		self.assertEqual(result["status"], "created")

	def test_same_bytes_owned_by_multiple_items_returns_exact_image_conflict(self):
		from inventory.hashing import sha256_file
		for item_id in ("INV-one", "INV-two"):
			item = self.settings.items_dir / item_id; images = item / "images"; images.mkdir(parents=True)
			image = images / "same.jpg"; image.write_bytes((self.source / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").read_bytes())
			(item / "item.json").write_text(json.dumps({"schema": "hermes-inventory-item", "schema_version": 3, "inventory_id": item_id, "asset_id": "000-001", "images": [{"relative_path": "images/same.jpg", "sha256": sha256_file(image)}]}), encoding="utf-8")
		with patch("inventory.ingest.run_vision", side_effect=AssertionError("vision")):
			result = ingest(self.source, object(), settings=self.settings)
		self.assertEqual(result["status"], "exact_image_conflict")
		self.assertEqual(result["classification"], "EXACT_IMAGE_CONFLICT")

	def test_product_and_name_duplicate_evidence_do_not_block_second_physical_copy(self):
		for classification in ("SAME_IDENTIFIED_PRODUCT", "POSSIBLE_SAME_PRODUCT"):
			result = self._run_ingest(duplicate_result={"classification": classification, "candidates": []})
			self.assertEqual(result["status"], "created")
			shutil.rmtree(self.settings.items_dir)

	def test_serial_and_homebox_exact_duplicate_still_block(self):
		for classification in ("SAME_PHYSICAL_UNIT", "EXACT_DUPLICATE"):
			result = self._run_ingest(duplicate_result={"classification": classification, "candidates": []})
			self.assertEqual(result["status"], "duplicate_candidate")
			self.assertEqual(result["classification"], classification)


if __name__ == "__main__":
	unittest.main()
