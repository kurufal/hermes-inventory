import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.commands import inventory_command


class InventoryCommandTests(unittest.TestCase):
	def test_status_is_read_only(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=False), patch("inventory.commands.storage_health") as health:
				result = inventory_command("STATUS")
			self.assertIn("Inventory persistent data", result)
			health.assert_not_called()

	def test_storage_set_preserves_argument_case(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=False), patch("inventory.commands.storage_health", return_value=(True, "reachable")):
				result = inventory_command(r"storage set \\NAS\Inventory\Anime Figures")
			self.assertIn(r"\\NAS\Inventory\Anime Figures", result)

	def test_unknown_command_is_helpful(self):
		self.assertIn("Unknown Inventory command: NoPe", inventory_command("NoPe"))

	def test_homebox_test_uses_non_destructive_api_check(self):
		with patch.dict(os.environ, {"HOMEBOX_URL": "http://homebox", "HOMEBOX_API_KEY": "not-in-output"}, clear=False), patch("inventory.homebox.get_entity_types", return_value=[{"id": "item"}]):
			result = inventory_command("homebox TEST")
		self.assertIn("Connection: PASS", result)
		self.assertNotIn("not-in-output", result)