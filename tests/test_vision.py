"""Contract tests for the Hermes-managed inventory vision call."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.vision import VISION_TASK, analyze_directory


class FakeVisionClient:
	def __init__(self, response):
		self.response = response
		self.call = None

	def complete_structured(self, **kwargs):
		self.call = kwargs
		return self.response


class InventoryVisionTests(unittest.TestCase):
	def test_sends_all_images_to_the_registered_hermes_task(self):
		response = SimpleNamespace(
			parsed={"identifiers": {}, "image_roles": []},
			text='{"identifiers": {}, "image_roles": []}',
			provider="test-provider",
			model="test-vision-model",
			agent_id="default",
			audit={"task": VISION_TASK},
		)
		client = FakeVisionClient(response)

		with tempfile.TemporaryDirectory() as temporary_directory:
			temporary_path = Path(temporary_directory)
			item_directory = temporary_path / "INV-test"
			metadata_directory = temporary_path / "metadata"
			item_directory.mkdir()
			(item_directory / "rear.png").write_bytes(b"png-bytes")
			(item_directory / "front.jpg").write_bytes(b"jpg-bytes")

			with patch("inventory.vision.METADATA_DIR", metadata_directory):
				record, metadata_path = analyze_directory(
					item_directory,
					client,
				)
			self.assertTrue(metadata_path.is_file())

		self.assertEqual(client.call["task"], VISION_TASK)
		self.assertEqual(client.call["purpose"], "inventory.vision")
		self.assertTrue(client.call["json_mode"])
		self.assertEqual(
			[(image["file_name"], image["mime_type"]) for image in client.call["input"]],
			[("front.jpg", "image/jpeg"), ("rear.png", "image/png")],
		)
		self.assertEqual(record["parse_status"], "json_ok")
		self.assertEqual(record["llm"]["provider"], "test-provider")
		self.assertFalse("ollama_url" in record)

	def test_preserves_unparseable_model_output_for_audit(self):
		response = SimpleNamespace(
			parsed=None,
			text="not valid JSON",
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
			(item_directory / "item.webp").write_bytes(b"webp-bytes")

			with patch("inventory.vision.METADATA_DIR", temporary_path / "metadata"):
				record, _ = analyze_directory(item_directory, client)

		self.assertEqual(record["parse_status"], "json_parse_failed")
		self.assertEqual(
			record["result"],
			{"raw_model_output": "not valid JSON"},
		)


if __name__ == "__main__":
	unittest.main()
