"""Contract tests for the Hermes-facing inventory tool handler."""

import importlib.util
import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
		(self.root / "images").mkdir()
		self.explicit = self.root / "images" / "explicit.png"
		self.explicit.write_bytes(b"explicit")
		self.pending = self.root / "images" / "dashboard_20260816_084100_item.png"
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
		settings = SimpleNamespace(
			hermes_images_dir=self.root / "images",
			runtime_dir=self.root / "runtime",
			pending_upload_state_path=self.root / "runtime" / "pending.json",
			pending_ttl_seconds=300,
			state_retention_seconds=86400,
			batch_window_seconds=10,
		)

		def fake_ingest(stage_directory, vision_client, **kwargs):
			del kwargs
			seen["staged_names"] = [
				path.name for path in Path(stage_directory).iterdir()
				if path.suffix in {".jpg", ".jpeg", ".png", ".webp"}
			]
			seen["vision_client"] = vision_client
			return backend_result or {"status": "created", "created": True, "durable": True}

		with patch.object(self.plugin, "get_settings", return_value=settings), patch.object(
			self.plugin, "homebox_url", return_value="http://homebox",
		), patch.object(
			self.plugin, "homebox_api_key", return_value="configured-key",
		), patch.object(
			self.plugin,
			"_load_inventory_ingest",
			return_value=fake_ingest,
		), patch.object(
			self.plugin,
			"mark_pending_upload_consumed",
		) as mark_consumed, patch.object(
			self.plugin,
			"mark_pending_upload_processing",
		), patch.object(
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

	def test_windows_composer_images_are_trusted_only_when_explicit(self):
		composer = self.root / "Roaming" / "Hermes" / "composer-images"
		composer.mkdir(parents=True)
		first = composer / "composer_2026-09-01_20-16-27-642_9b27f5.jpg"
		second = composer / "composer_2026-09-01_20-16-27-691_575083.png"
		first.write_bytes(b"first")
		second.write_bytes(b"second")
		with patch.dict("os.environ", {"APPDATA": str(self.root / "Roaming")}, clear=False):
			result, seen, _ = self.run_ingest([str(first), str(second)])
		self.assertIn('"created": true', result)
		self.assertEqual(set(seen["staged_names"]), {first.name, second.name})

	def test_arbitrary_appdata_path_is_rejected(self):
		outside = self.root / "Roaming" / "other.jpg"
		outside.parent.mkdir(parents=True)
		outside.write_bytes(b"image")
		with patch.dict("os.environ", {"APPDATA": str(self.root / "Roaming")}, clear=False):
			result, _, _ = self.run_ingest([str(outside)])
		self.assertIn("outside trusted Hermes attachment roots", result)

	def test_unavailable_composer_path_explains_remote_backend_limit(self):
		settings = SimpleNamespace(hermes_images_dir=self.root / "images", runtime_dir=self.root / "runtime")
		with patch.object(self.plugin, "get_settings", return_value=settings), patch.object(
			self.plugin, "homebox_url", return_value="http://homebox",
		), patch.object(self.plugin, "homebox_api_key", return_value="configured-key"):
			result = self.plugin.inventory_ingest(r"C:\Users\Desktop\AppData\Roaming\Hermes\composer-images\composer_missing.jpg", "fake-llm")
		self.assertIn("unavailable to this backend", result)

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

	def test_ingest_before_homebox_configuration_returns_not_configured(self):
		settings = SimpleNamespace(hermes_images_dir=self.root / "images")
		with patch.object(self.plugin, "get_settings", return_value=settings), patch.object(
			self.plugin, "homebox_url", return_value="",
		), patch.object(
			self.plugin, "homebox_api_key", return_value="",
		), patch.object(
			self.plugin, "_load_inventory_ingest", side_effect=AssertionError("must not stage or ingest"),
		):
			result = self.plugin.inventory_ingest([str(self.explicit)], "fake-llm")
		self.assertIn('"status": "not_configured"', result)
		self.assertIn("/inventory setup", result)

	def test_pending_batch_is_consumed_after_staging(self):
		with patch.object(
			self.plugin,
			"resolve_pending_upload_batch",
			return_value=self.batch,
		) as resolve_batch:
			result, seen, mark_consumed = self.run_ingest([], use_pending=True)

		self.assertTrue(resolve_batch.called)
		self.assertEqual(mark_consumed.call_args.args, ("batch-1",))
		self.assertIn("state_path", mark_consumed.call_args.kwargs)
		self.assertEqual(seen["staged_names"], ["dashboard_20260816_084100_item.png"])
		self.assertIn('"created": true', result)

	def test_exact_duplicate_pending_batch_is_consumed(self):
		duplicate = {
			"classification": "EXACT_DUPLICATE",
			"created": False,
			"durable": True,
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

		self.assertEqual(mark_consumed.call_args.args, ("batch-1",))
		self.assertIn("state_path", mark_consumed.call_args.kwargs)
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

	def test_no_pending_batch_includes_structured_diagnostics(self):
		error = PendingUploadError("No recent pending dashboard upload was found.")
		error.debug = {
			"state_file": "/opt/data/inventory/pending-uploads.json",
			"pending_batch_count": 0,
			"total_batch_count": 2,
			"candidate_count": 0,
		}
		with patch.object(
			self.plugin,
			"resolve_pending_upload_batch",
			side_effect=error,
		):
			result, _, _ = self.run_ingest([], use_pending=True)

		self.assertIn('"error_stage": "pending_upload_resolution"', result)
		self.assertIn('"pending_debug"', result)
		self.assertIn('"candidate_count": 0', result)
		self.assertIn('"total_batch_count": 2', result)

	def test_vision_parse_failure_surfaces_structured_diagnostics(self):
		def failing_ingest(stage_directory, vision_client, **kwargs):
			del stage_directory, vision_client, kwargs
			error = RuntimeError("Vision model did not return valid structured JSON")
			error.debug = {
				"error_stage": "vision_json_parse",
				"provider": "test-provider",
				"model": "test-vision-model",
				"content_type": "text/plain",
				"metadata_path": "/opt/data/inventory/metadata/INV-test.json",
				"input_image_count": 1,
				"input_image_filenames": ["front.jpg"],
				"raw_response_preview": "not valid json",
			}
			raise error

		settings = SimpleNamespace(hermes_images_dir=self.root / "images", runtime_dir=self.root / "runtime")
		with patch.object(self.plugin, "get_settings", return_value=settings), patch.object(
			self.plugin, "homebox_url", return_value="http://homebox",
		), patch.object(
			self.plugin, "homebox_api_key", return_value="configured-key",
		), patch.object(
			self.plugin,
			"_load_inventory_ingest",
			return_value=failing_ingest,
		):
			result = self.plugin.inventory_ingest([str(self.explicit)], "fake-llm")

		self.assertIn('"status": "error"', result)
		self.assertIn('"error_stage": "vision_json_parse"', result)
		self.assertIn('"provider": "test-provider"', result)
		self.assertIn('"input_image_count": 1', result)
		self.assertIn('"raw_response_preview": "not valid json"', result)

	def test_registered_schema_has_optional_pending_mode(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations[kwargs["name"]] = kwargs
				return {"registered": True}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())
		schema = registrations["inventory_ingest"]["schema"]
		properties = schema["parameters"]["properties"]

		self.assertNotIn("required", schema["parameters"])
		self.assertIn("use_pending_upload", properties)
		self.assertIn("image_paths", properties)
		self.assertNotIn("minItems", properties["image_paths"])
		self.assertNotIn("recent_dashboard_upload", properties)

	def test_registers_commands_without_homebox_credentials(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations.setdefault("tools", {})[kwargs["name"]] = kwargs

			def register_command(self, **kwargs):
				registrations["command"] = kwargs

			def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
				registrations["cli"] = {
					"name": name,
					"help": help,
					"setup_fn": setup_fn,
					"handler_fn": handler_fn,
					"description": description,
				}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())
		self.assertEqual(registrations["command"]["name"], "inventory")
		self.assertEqual(registrations["cli"]["name"], "inventory")
		self.assertNotIn("requires_env", registrations["tools"]["inventory_ingest"])
		parser = argparse.ArgumentParser()
		registrations["cli"]["setup_fn"](parser)
		namespace = parser.parse_args(["setup", "--secrets"])
		with patch("inventory.cli.inventory_cli", return_value="ok") as cli:
			self.assertEqual(registrations["cli"]["handler_fn"](namespace), "ok")
		cli.assert_called_once_with(["setup", "--secrets"])
		with patch.object(self.plugin, "inventory_ingest", return_value="handled") as handler:
			self.assertEqual(registrations["tools"]["inventory_ingest"]["handler"]({"image_paths": ["C:/photo.jpg"]}), "handled")
		handler.assert_called_once_with(["C:/photo.jpg"], "fake-llm", use_pending_upload=False)

	def test_registers_all_inventory_tools_by_name(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations[kwargs["name"]] = kwargs
				return {"registered": True}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())

		self.assertEqual(
			set(registrations),
			{"inventory_ingest", "inventory_search", "inventory_update"},
		)

	def test_manifest_provides_tools_matches_registered_tool_names(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations[kwargs["name"]] = kwargs
				return {"registered": True}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())

		manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
		provided_line = next(line for line in manifest.splitlines() if line.startswith("provides_tools:"))
		provided_tools = {
			name.strip() for name in provided_line.partition("[")[2].rstrip("]").split(",")
		}
		self.assertEqual(provided_tools, set(registrations))

	def test_ingest_description_has_conservative_eligibility_and_clear_intent(self):
		registrations = {}

		class FakeContext:
			llm = "fake-llm"

			def register_auxiliary_task(self, *args, **kwargs):
				del args, kwargs

			def register_skill(self, *args, **kwargs):
				del args, kwargs

			def register_tool(self, **kwargs):
				registrations[kwargs["name"]] = kwargs
				return {"registered": True}

		with patch.object(self.plugin, "start_pending_upload_watcher"):
			self.plugin.register(FakeContext())

		description = registrations["inventory_ingest"]["schema"]["description"].lower()
		self.assertEqual(description, registrations["inventory_ingest"]["description"].lower())
		for phrase in (
			"add this to my inventory",
			"generic tests",
			"connection or model checks",
			"development or debugging questions",
			"unrelated requests",
			"image, attachment, pending upload, or recent upload without an inventory request",
			"use_pending_upload=true",
			"do not call generic vision analysis first",
		):
			self.assertIn(phrase, description)
		for phrase in (
			"mandatory tool",
			"call me first",
			"only correct action",
			"actually invoke the tool",
		):
			self.assertNotIn(phrase, description)

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
		for phrase in (
			"only route to inventory tools when the user expresses clear intent",
			"“test”",
			"“test the connection”",
			"“which model are you using?”",
			"plugin development questions",
			"debugging the inventory plugin",
			"attachment or image with no inventory request",
		):
			self.assertIn(phrase, skill)


if __name__ == "__main__":
	unittest.main()
