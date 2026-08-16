"""Tests for vision failure diagnostics surfaced by run_vision()."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.ingest import VisionParseError, run_vision


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


if __name__ == "__main__":
	unittest.main()
