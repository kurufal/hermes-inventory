"""Portable configuration and storage diagnostics tests."""

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inventory.config import get_settings, storage_health, write_storage_config


class InventorySettingsTests(unittest.TestCase):
	def test_environment_paths_override_and_separate_runtime(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory) / "Hermes Home"
			with patch.dict(os.environ, {"HERMES_HOME": str(home), "INVENTORY_BASE_DIR": "\\\\Server\\Share\\Example Collection", "INVENTORY_RUNTIME_DIR": str(home / "runtime")}, clear=True):
				settings = get_settings()
			self.assertEqual(str(settings.persistent_data_dir), "\\\\Server\\Share\\Example Collection")
			self.assertEqual(settings.runtime_dir, home / "runtime")
			self.assertNotEqual(settings.runtime_dir, settings.persistent_data_dir)

	def test_fallback_home_is_portable(self):
		environment = dict(os.environ)
		environment.pop("HERMES_HOME", None)
		with patch.dict(os.environ, environment, clear=True):
			self.assertEqual(get_settings().hermes_home, Path.home() / ".hermes")

	def test_storage_health_leaves_no_probe_files(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			path = Path(temporary_directory) / "persistent data"
			self.assertEqual(storage_health(path), (True, "reachable"))
			self.assertEqual(list(path.iterdir()), [])

	def test_storage_write_merges_json_and_round_trips_unc(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			config = home / "inventory-config.json"
			config.write_text('{"storage":{"runtime_dir":"C:\\\\Runtime"},"toon":{"enabled":false},"future":{"value":1}}', encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				unc = Path("\\\\Server\\Share\\Example Collection")
				write_storage_config(unc)
				self.assertEqual(str(get_settings().persistent_data_dir), str(unc))
			payload = __import__("json").loads(config.read_text(encoding="utf-8"))
			self.assertEqual(payload["storage"]["runtime_dir"], "C:\\Runtime")
			self.assertFalse(payload["toon"]["enabled"])
			self.assertEqual(payload["future"]["value"], 1)

	def test_relative_configured_path_is_rejected(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			(home / "inventory-config.json").write_text('{"storage":{"persistent_data_dir":"relative"}}', encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				with self.assertRaisesRegex(ValueError, "absolute path"):
					get_settings()

	def test_legacy_yaml_migration_preserves_unrelated_sections(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			(home / "inventory-config.yaml").write_text(
				"storage:\n  runtime_dir: C:\\Runtime\nuploads:\n  batch_window_seconds: 22\ntoon:\n  enabled: false\nfuture:\n  retained: yes\n",
				encoding="utf-8",
			)
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				self.assertFalse(get_settings().toon_enabled)
				write_storage_config(Path("C:/Inventory"))
			payload = json.loads((home / "inventory-config.json").read_text(encoding="utf-8"))
			self.assertEqual(payload["storage"]["runtime_dir"], "C:\\Runtime")
			self.assertEqual(payload["uploads"]["batch_window_seconds"], 22)
			self.assertFalse(payload["toon"]["enabled"])
			self.assertEqual(payload["future"]["retained"], True)

	def test_invalid_toon_boolean_is_rejected(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			(home / "inventory-config.json").write_text('{"toon":{"enabled":"sometimes"}}', encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				with self.assertRaisesRegex(ValueError, "toon.enabled must be a boolean"):
					get_settings()

	def test_secret_fallback_preserves_unrelated_dotenv_values(self):
		from inventory.config import homebox_api_key, write_homebox_api_key
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			(home / ".env").write_text("UNRELATED=value\nHOMEBOX_API_KEY=old\n", encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=True):
				write_homebox_api_key("new")
			contents = (home / ".env").read_text(encoding="utf-8")
		self.assertIn("UNRELATED=value", contents)
		self.assertIn("HOMEBOX_API_KEY=new", contents)

	def test_existing_fallback_dotenv_secret_is_detected(self):
		from inventory.config import homebox_api_key
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			(home / ".env").write_text("HOMEBOX_API_KEY=configured-secret\n", encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=True):
				self.assertEqual(homebox_api_key(), "configured-secret")
	def test_legacy_credential_exports_are_not_cached(self):
		import inventory.config as config
		with tempfile.TemporaryDirectory() as temporary_directory:
			dotenv = Path(temporary_directory) / ".env"
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				for suffix in ("first", "second"):
					dotenv.write_text(f"HOMEBOX_URL=http://{suffix}\nHOMEBOX_API_KEY=hb_{suffix}\n", encoding="utf-8")
					self.assertEqual(config.HOMEBOX_URL, f"http://{suffix}")
					self.assertEqual(config.HOMEBOX_API_KEY, f"hb_{suffix}")
	def test_optional_write_does_not_cache_file_only_credentials(self):
		from inventory.config import homebox_api_key, write_homebox_api_key
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				write_homebox_api_key("hb_first")
				self.assertEqual(homebox_api_key(), "hb_first")
				self.assertNotIn("HOMEBOX_API_KEY", os.environ)
				(Path(temporary_directory) / ".env").write_text("HOMEBOX_API_KEY=hb_changed\n", encoding="utf-8")
				self.assertEqual(homebox_api_key(), "hb_changed")

	def test_optional_write_refreshes_existing_process_override_with_hermes_helper(self):
		from inventory.config import homebox_api_key, write_homebox_api_key
		helper = SimpleNamespace(save_env_value=lambda name, value: None)
		with patch.dict(sys.modules, {"hermes_cli.config": helper}), patch.dict(os.environ, {"HOMEBOX_API_KEY": "hb_old"}, clear=False):
			write_homebox_api_key("hb_new", SimpleNamespace())
			self.assertEqual(homebox_api_key(), "hb_new")

	def test_optional_writer_updates_exported_key_without_losing_other_values(self):
		from dotenv import dotenv_values
		from inventory.config import write_homebox_api_key
		with tempfile.TemporaryDirectory() as temporary_directory:
			dotenv = Path(temporary_directory) / ".env"
			dotenv.write_text("# Hermes settings\nAPI_SERVER_KEY=unrelated\nexport HOMEBOX_API_KEY = 'hb_old'\n", encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				write_homebox_api_key("hb_new")
			values = dotenv_values(dotenv)
			self.assertEqual(values["HOMEBOX_API_KEY"], "hb_new")
			self.assertEqual(values["API_SERVER_KEY"], "unrelated")
			self.assertIn("# Hermes settings", dotenv.read_text(encoding="utf-8"))
			self.assertEqual([path.name for path in dotenv.parent.iterdir()], [".env"])

	def test_homebox_environment_url_overrides_plugin_json_for_docker(self):
		from inventory.config import homebox_url
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			(home / "inventory-config.json").write_text('{"homebox":{"url":"http://desktop-host"}}', encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home), "HOMEBOX_URL": "http://docker-host"}, clear=True):
				self.assertEqual(homebox_url(), "http://docker-host")
	def test_dotenv_loads_both_homebox_values_and_reloads_changes(self):
		from inventory.config import homebox_api_key, homebox_url
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			dotenv = home / ".env"
			(home / "inventory-config.json").write_text('{"homebox":{"url":"http://fallback"}}', encoding="utf-8")
			dotenv.write_text('export HOMEBOX_URL="http://homebox:3100"\nHOMEBOX_API_KEY = \'hb_test\' # comment\n', encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=True):
				self.assertEqual(homebox_url(), "http://homebox:3100")
				self.assertEqual(homebox_api_key(), "hb_test")
				dotenv.write_text('HOMEBOX_API_KEY="hb_changed"\n', encoding="utf-8")
				self.assertEqual(homebox_api_key(), "hb_changed")
				self.assertEqual(homebox_url(), "http://fallback")

	def test_process_environment_overrides_dotenv_and_hermes_helper(self):
		from inventory.config import homebox_api_key, homebox_url
		helper = SimpleNamespace(get_env_value=lambda name: "helper-value")
		with tempfile.TemporaryDirectory() as temporary_directory:
			(Path(temporary_directory) / ".env").write_text("HOMEBOX_API_KEY=hb_file\nHOMEBOX_URL=http://file\n", encoding="utf-8")
			with patch.dict(sys.modules, {"hermes_cli.config": helper}), patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "HOMEBOX_API_KEY": "hb_process", "HOMEBOX_URL": "http://process"}, clear=True):
				self.assertEqual(homebox_api_key(), "hb_process")
				self.assertEqual(homebox_url(), "http://process")

	def test_hermes_server_key_is_not_a_homebox_credential(self):
		from inventory.config import homebox_api_key
		with tempfile.TemporaryDirectory() as temporary_directory:
			(Path(temporary_directory) / ".env").write_text("API_SERVER_KEY=server-only\n", encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "API_SERVER_KEY": "server-only"}, clear=True):
				self.assertEqual(homebox_api_key(), "")

	def test_homebox_secret_uses_hermes_environment_helper_when_available(self):
		from inventory.config import write_homebox_api_key
		calls = []
		helper = SimpleNamespace(set_environment_value=lambda name, value: calls.append((name, value)))
		with patch.dict(sys.modules, {"hermes_constants": helper}):
			write_homebox_api_key("secret", SimpleNamespace())
		self.assertEqual(calls, [("HOMEBOX_API_KEY", "secret")])

	def test_homebox_secret_prefers_hermes_cli_environment_helpers(self):
		from inventory.config import environment_value, write_homebox_api_key
		calls = []
		helper = SimpleNamespace(get_env_value=lambda name: "hb_from_helper", save_env_value=lambda name, value: calls.append((name, value)))
		with patch.dict(sys.modules, {"hermes_cli.config": helper}):
			self.assertEqual(environment_value("HOMEBOX_API_KEY"), "hb_from_helper")
			write_homebox_api_key("hb_new", SimpleNamespace())
		self.assertEqual(calls, [("HOMEBOX_API_KEY", "hb_new")])

	def test_default_persistent_inventory_is_not_in_hermes_media_directories(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				settings = get_settings()
		self.assertEqual(settings.persistent_data_dir, Path(temporary_directory) / "inventory")
		for media_directory in ("images", "media", "image_cache", "user_media"):
			self.assertNotEqual(settings.persistent_data_dir, Path(temporary_directory) / media_directory)