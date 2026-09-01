"""Portable configuration and storage diagnostics tests."""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.config import get_settings, storage_health, write_storage_config


class InventorySettingsTests(unittest.TestCase):
	def test_environment_paths_override_and_separate_runtime(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory) / "Hermes Home"
			with patch.dict(os.environ, {"HERMES_HOME": str(home), "INVENTORY_BASE_DIR": "\\\\NAS\\Inventory\\Anime Figures", "INVENTORY_RUNTIME_DIR": str(home / "runtime")}, clear=True):
				settings = get_settings()
			self.assertEqual(str(settings.persistent_data_dir), "\\\\NAS\\Inventory\\Anime Figures")
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
				unc = Path("\\\\truenas\\Inventory\\Anime Figures")
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