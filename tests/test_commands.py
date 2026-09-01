import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.commands import inventory_cli, inventory_command


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

	def test_setup_defaults_need_no_storage_selection(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True), patch("inventory.commands.storage_health", return_value=(True, "reachable")):
				result = inventory_command("SeTuP")
		self.assertIn("[PASS] Persistent storage", result)
		self.assertIn("HomeBox URL", result)
		self.assertIn("/inventory setup homebox <url>", result)

	def test_setup_storage_and_homebox_preserve_values(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True), patch("inventory.commands.storage_health", return_value=(True, "reachable")):
				storage = inventory_command(r"setup STORAGE \\Server\Share\HermesInventory")
				url = inventory_command("setup HOMEBOX http://Host:8080")
				payload = json.loads((Path(temporary_directory) / "inventory-config.json").read_text(encoding="utf-8"))
		self.assertIn(r"\\Server\Share\HermesInventory", storage)
		self.assertIn("http://Host:8080", url)
		self.assertEqual(payload["homebox"]["url"], "http://Host:8080")

	def test_setup_secrets_rejects_chat_secret_without_echoing_it(self):
		secret = "do-not-echo-this-key"
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				result = inventory_command(f"setup secrets {secret}")
		self.assertIn("Secrets are not accepted in chat", result)
		self.assertNotIn(secret, result)
		self.assertIn("not configured", result)

	def test_setup_secrets_detects_existing_key_without_displaying_it(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "HOMEBOX_API_KEY": "do-not-echo-this-key"}, clear=True):
				result = inventory_command("setup secrets")
		self.assertIn("HOMEBOX_API_KEY: configured", result)
		self.assertNotIn("do-not-echo-this-key", result)

	def test_secure_cli_retains_existing_secret_when_skipped(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "HOMEBOX_URL": "http://homebox", "HOMEBOX_API_KEY": "existing"}, clear=True), patch("inventory.commands.getpass.getpass", return_value=""), patch("inventory.homebox.get_entity_types", return_value=[]):
				result = inventory_cli(["setup", "--secrets"])
		self.assertEqual(result, "HomeBox authentication: PASS")

	def test_secure_cli_writes_new_secret_and_authenticates(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "HOMEBOX_URL": "http://homebox"}, clear=True), patch("inventory.commands.getpass.getpass", return_value="new-secret"), patch("inventory.homebox.get_entity_types", return_value=[]):
				result = inventory_cli(["setup", "--secrets"])
				dotenv = (Path(temporary_directory) / ".env").read_text(encoding="utf-8")
		self.assertEqual(result, "HomeBox authentication: PASS")
		self.assertIn("HOMEBOX_API_KEY=new-secret", dotenv)
		self.assertNotIn("new-secret", (Path(temporary_directory) / "inventory-config.json").read_text(encoding="utf-8") if (Path(temporary_directory) / "inventory-config.json").exists() else "")