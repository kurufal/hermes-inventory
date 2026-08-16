"""Contract tests for the Hermes-facing inventory tool handler."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.uploads import PendingUploadBatch, PendingUploadError


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
	spec = importlib.util.spec_from_file_location(
		"hermes_inventory_plugin",
		PLUGIN_ROOT / "__init__.py",
	)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


class InventoryPluginHandlerTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.plugin = load_plugin()

	def setUp(self):
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name)
		self.explicit = self.root / "explicit.png"
		self.explicit.write_bytes(b"explicit")
		self.pending = self.root / "dashboard_20260816_084100_item.png"
		self.pending.write_bytes(b"pending")
		self.batch = PendingUploadBatch(
			batch_id="batch-1",
			image_paths=(self.pending.resolve(),),
			created_at="2026-08-16T08:41:00Z",
			updated_at="2026-08-16T08:41:00Z",
			expires_at="2026-08-16T08:46:00Z",
		)

	def tearDown(self):
		self.temporary_directory.cleanup()

	def run_ingest(self, image_paths, *, use_pending=False, backend_result=None):
		seen = {}

		def fake_ingest(stage_directory, vision_client):
			seen["staged_names"] = [
				path.name for path in Path(stage_directory).iterdir()
			]
			seen["vision_client"] = vision_client
			return backend_result or {"status": "created", "created": True}

		with patch.object(self.plugin, "HERMES_HOME", self.root), patch.object(
			self.plugin,
			"STAGING_ROOT",
			self.root / "staging",
		), patch.object(
			self.plugin,
			"_load_inventory_ingest",
			return_value=fake_ingest,
		), patch.object(
			self.plugin,
			"mark_pending_upload_consumed",
		) as mark_consumed, patch.object(
			self.plugin,
			"release_pending_upload_claim",
		):
			result = self.plugin.inventory_ingest(
				image_paths,
				"fake-llm",
				use_pending_upload=use_pending,
			)

		return result, seen, mark_consumed

	def test_explicit_paths_still_use_existing_pipeline(self):
		result, seen, mark_consumed = self.run_ingest([str(self.explicit)])

		self.assertIn('"created": true', result)
		self.assertEqual(seen["staged_names"], ["explicit.png"])
		mark_consumed.assert_not_called()

	def test_explicit_paths_take_priority_over_pending_mode(self):
		with patch.object(
			self.plugin,
			"resolve_pending_upload_batch",
			side_effect=AssertionError("pending resolver should not be called"),
		):
			result, _, _ = self.run_ingest([str(self.explicit)], use_pending=True)

		self.assertIn('"created": true', result)

	def test_empty_input_without_pending_mode_returns_clear_error(self):
		result, _, _ = self.run_ingest([])

		self.assertIn("image_paths or set use_pending_upload", result)

	def test_pending_batch_is_consumed_after_staging(self):
		with patch.object(
			self.plugin,
			"resolve_pending_upload_batch",
			return_value=self.batch,
		) as resolve_batch:
			result, seen, mark_consumed = self.run_ingest([], use_pending=True)

		resolve_batch.assert_called_once_with(claim=True)
		mark_consumed.assert_called_once_with("batch-1")
		self.assertEqual(seen["staged_names"], ["dashboard_20260816_084100_item.png"])
		self.assertIn('"created": true', result)

	def test_exact_duplicate_pending_batch_is_consumed(self):
		duplicate = {
			"classification": "EXACT_DUPLICATE",
			"created": False,
			"duplicate_check": {"candidates": [{"name": "Existing"}]},
		}
		with patch.object(
			self.plugin,
			"resolve_pending_upload_batch",
			return_value=self.batch,
		):
			result, _, mark_consumed = self.run_ingest(
				[],
				use_pending=True,
				backend_result=duplicate,
			)

		mark_consumed.assert_called_once_with("batch-1")
		self.assertIn("EXACT_DUPLICATE", result)
		self.assertIn("requires_user_action", result)

	def test_no_pending_batch_returns_structured_error(self):
		with patch.object(
			self.plugin,
			"resolve_pending_upload_batch",
			side_effect=PendingUploadError("No recent pending dashboard upload was found."),
		):
			result, _, _ = self.run_ingest([], use_pending=True)

		self.assertIn('"status": "error"', result)
		self.assertIn("No recent pending dashboard upload", result)

	def test_registered_schema_has_optional_pending_mode(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations.update(kwargs)
				return {"registered": True}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())
		schema = registrations["schema"]
		properties = schema["parameters"]["properties"]

		self.assertEqual(schema["parameters"]["required"], [])
		self.assertIn("use_pending_upload", properties)
		self.assertNotIn("recent_dashboard_upload", properties)

	def test_tool_description_contains_natural_language_routing_triggers(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations.update(kwargs)
				return {"registered": True}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())

		description = registrations["schema"]["description"].lower()
		for phrase in (
			"add this to my inventory",
			"add this item to homebox",
			"inventory this",
			"catalog this",
			"record this item",
			"put this in homebox",
			"add the thing i just uploaded",
		):
			self.assertIn(phrase, description)
		self.assertIn("do not ask what kind of inventory", description)
		self.assertIn("do not call vision_analyze first", description)
		self.assertIn("use_pending_upload=true", description)

	def test_inventory_skill_is_a_concise_routing_skill(self):
		skill = (
			PLUGIN_ROOT / "skills" / "inventory" / "SKILL.md"
		).read_text(encoding="utf-8").lower()

		self.assertTrue(skill.startswith("# inventory routing"))
		for phrase in (
			"add this to my inventory",
			"inventory this",
			"catalog this",
			"record this item",
			"add this to homebox",
			"put this in homebox",
			"add the thing i just uploaded",
		):
			self.assertIn(phrase, skill)
		self.assertIn("what kind of inventory?", skill)
		self.assertIn("use_pending_upload", skill)
		self.assertIn("do not call `vision_analyze` first", skill)


if __name__ == "__main__":
	unittest.main()
