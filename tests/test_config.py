"""Portable configuration and storage diagnostics tests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.config import get_settings, storage_health


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