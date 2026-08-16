"""Contract tests for the Hermes-facing inventory tool handler."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
		self.images = self.root / "images"
		self.images.mkdir()
		self.explicit = self.images / "explicit.png"
		self.explicit.write_bytes(b"explicit")
		self.fallback = self.images / "dashboard_20260816_031915_item.png"
		self.fallback.write_bytes(b"fallback")

	def tearDown(self):
		self.temporary_directory.cleanup()

	def run_ingest(self, image_paths, *, recent=False, backend_result=None):
		seen = {}

		def fake_ingest(stage_directory, vision_client):
			seen["stage_directory"] = Path(stage_directory)
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
			"mark_dashboard_uploads_consumed",
		) as mark_consumed:
			result = self.plugin.inventory_ingest(
				image_paths,
				"fake-llm",
				recent_dashboard_upload=recent,
			)

		return result, seen, mark_consumed

	def test_explicit_paths_still_use_existing_pipeline(self):
		result, seen, mark_consumed = self.run_ingest([str(self.explicit)])

		self.assertIn('"created": true', result)
		self.assertEqual(
			[p.name for p in seen["stage_directory"].iterdir()],
			["explicit.png"],
		)
		mark_consumed.assert_not_called()

	def test_explicit_paths_take_priority_over_fallback_flag(self):
		with patch.object(
			self.plugin,
			"resolve_recent_dashboard_uploads",
			side_effect=AssertionError("fallback should not be called"),
		):
			result, _, _ = self.run_ingest(
				[self.explicit],
				recent=True,
			)

		self.assertIn('"created": true', result)

	def test_empty_input_without_fallback_returns_clear_error(self):
		result, _, _ = self.run_ingest([])

		self.assertIn("image_paths or set recent_dashboard_upload", result)

	def test_fallback_upload_is_marked_consumed_after_staging(self):
		with patch.object(
			self.plugin,
			"resolve_recent_dashboard_uploads",
			return_value=[self.fallback.resolve()],
		) as resolve_uploads:
			result, seen, mark_consumed = self.run_ingest([], recent=True)

		resolve_uploads.assert_called_once_with()
		mark_consumed.assert_called_once_with([self.fallback.resolve()])
		self.assertEqual(
			[p.name for p in seen["stage_directory"].iterdir()],
			["dashboard_20260816_031915_item.png"],
		)
		self.assertIn('"created": true', result)

	def test_exact_duplicate_fallback_upload_is_still_consumed(self):
		duplicate = {
			"classification": "EXACT_DUPLICATE",
			"created": False,
			"duplicate_check": {"candidates": [{"name": "Existing"}]},
		}
		with patch.object(
			self.plugin,
			"resolve_recent_dashboard_uploads",
			return_value=[self.fallback.resolve()],
		):
			result, _, mark_consumed = self.run_ingest(
				[],
				recent=True,
				backend_result=duplicate,
			)

		mark_consumed.assert_called_once_with([self.fallback.resolve()])
		self.assertIn("EXACT_DUPLICATE", result)
		self.assertIn("requires_user_action", result)

	def test_registered_schema_makes_image_paths_optional(self):
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

		self.plugin.register(FakeContext())
		schema = registrations["schema"]

		self.assertEqual(schema["parameters"]["required"], [])
		self.assertIn("recent_dashboard_upload", schema["parameters"]["properties"])


if __name__ == "__main__":
	unittest.main()
