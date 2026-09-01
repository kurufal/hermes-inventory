"""Tests for ingest orchestration and vision failure diagnostics."""

import json
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

	def _run_ingest(self, complete_side_effect=None):
		def create(record):
			self.homebox_directories.append(Path(record["source_directory"]))
			return {"id": "entity-1", "assetId": "asset-1", "groupId": "group-1", "entityTypeId": "type-1"}

		def complete(entity_id, record, *, image_directory=None):
			self.assertEqual(entity_id, "entity-1")
			directory = Path(image_directory)
			self.homebox_directories.append(directory)
			self.assertEqual(Path(record["source_directory"]), directory)
			self.assertTrue((directory / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").is_file())
			self.assertTrue((directory / "composer_2026-09-01_20-16-27-691_575083.jpg").is_file())
			if complete_side_effect:
				raise complete_side_effect
			return {"entity": {"assetId": "asset-1", "groupId": "group-1", "entityType": {"id": "type-1"}}, "attachments": [{"id": "attachment-1"}, {"id": "attachment-2"}]}

		with patch("inventory.ingest.run_vision", side_effect=self.fake_vision), patch(
			"inventory.ingest.check_homebox_duplicates",
			return_value={"classification": "NEW_ITEM", "candidates": []},
		), patch("inventory.ingest.create_entity", side_effect=create), patch(
			"inventory.ingest.complete_entity", side_effect=complete,
		):
			return ingest(self.source, object(), settings=self.settings)

	def _assert_canonical_evidence(self, result):
		item_root = self.settings.items_dir / result["item_id"]
		images = item_root / "images"
		self.assertTrue(item_root.is_dir())
		self.assertTrue((images / "composer_2026-09-01_20-16-27-642_9b27f5.jpg").is_file())
		self.assertTrue((images / "composer_2026-09-01_20-16-27-691_575083.jpg").is_file())
		self.assertTrue((item_root / "item.json").is_file())
		self.assertTrue((item_root / "vision.json").is_file())
		self.assertFalse(list(self.settings.items_dir.glob(".tmp-*")))
		manifest = json.loads((item_root / "item.json").read_text(encoding="utf-8"))
		raw = json.loads((item_root / "vision.json").read_text(encoding="utf-8"))
		self.assertNotIn(".tmp-", json.dumps(manifest))
		self.assertNotIn(".tmp-", json.dumps(raw))
		self.assertEqual(raw["source_directory"], str(images))
		self.assertEqual(len(manifest["images"]), 2)
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


if __name__ == "__main__":
	unittest.main()
