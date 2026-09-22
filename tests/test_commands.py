import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from inventory.cli import handle_inventory_cli
from inventory.commands import _format_refresh, inventory_cli, inventory_command


class InventoryCommandTests(unittest.TestCase):
	def test_status_is_read_only(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=False), patch("inventory.commands.storage_health") as health:
				result = inventory_command("STATUS")
			self.assertIn("Inventory persistent data", result)
			health.assert_not_called()

	def test_refresh_and_dry_run_are_read_only_preview(self):
		preview = {"canonical": {"valid_items": []}, "legacy": {"legacy_candidates": []}, "homebox": {"items": [], "complete": True}, "matches": {"homebox_only": [], "local_only": [], "ambiguous": []}, "conflicts": [], "reservations": {"unmatched": []}, "transactions": {"incomplete": []}, "proposed_actions": []}
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True), patch("inventory.refresh.refresh", return_value=preview) as scanner, patch("inventory.refresh.apply_refresh", return_value={"report": preview, "applied": [], "backup": None}):
				result = inventory_command("refresh --dry-run")
				dry_run = inventory_command("refresh --dry-run")
		self.assertIn("Mode: READ-ONLY PREVIEW", result)
		self.assertIn("No changes were made.", dry_run)
		self.assertEqual(scanner.call_count, 2)

	def test_refresh_apply_errors_are_not_formatted_as_reconciled(self):
		report = {"canonical": {"valid_items": []}, "legacy": {"legacy_candidates": []}, "homebox": {"items": [], "complete": True}, "matches": {"homebox_only": [], "local_only": [], "ambiguous": []}, "conflicts": [], "reservations": {"unmatched": []}, "transactions": {"incomplete": []}, "proposed_actions": []}
		for result in ({"status": "ERROR", "applied": [], "skipped": [{"reason": "backup_verification_failed"}], "backup": {"path": "backup.zip"}}, {"status": "ERROR", "applied": [{"type": "link_homebox"}], "failed": {"action": {"type": "adopt_homebox"}, "error": "boom"}, "skipped": [{"reason": "not_attempted_after_failure"}], "backup": {"path": "backup.zip"}}):
			output = _format_refresh(report, apply_result=result)
			self.assertIn("Status: ERROR", output)
			self.assertNotIn("already reconciled", output)
		self.assertIn("Remaining actions were not attempted.", _format_refresh(report, apply_result={"status": "ERROR", "applied": [{"type": "link_homebox"}], "failed": {"action": {"type": "adopt_homebox"}, "error": "boom"}, "skipped": [{"reason": "not_attempted_after_failure"}]}))

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
		self.assertIn("[WARN] Persistent storage: using local default", result)
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

	def test_setup_normalizes_hermes_rich_link_url(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				result = inventory_command("setup homebox @url:`[http://192.168.1.160:3100](http://192.168.1.160:3100)`")
				payload = json.loads((Path(temporary_directory) / "inventory-config.json").read_text(encoding="utf-8"))
		self.assertIn("http://192.168.1.160:3100", result)
		self.assertEqual(payload["homebox"]["url"], "http://192.168.1.160:3100")

	def test_setup_normalizes_https_trailing_slash(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				inventory_command("setup homebox https://homebox.example/inventory/")
				payload = json.loads((Path(temporary_directory) / "inventory-config.json").read_text(encoding="utf-8"))
		self.assertEqual(payload["homebox"]["url"], "https://homebox.example/inventory")

	def test_status_shows_default_storage_source(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				result = inventory_command("status")
		self.assertIn("Persistent source: default", result)

	def test_setup_rejects_invalid_homebox_url(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True):
				with self.assertRaisesRegex(ValueError, "HTTP or HTTPS"):
					inventory_command("setup homebox ftp://host")

	def test_setup_storage_existing_inventory_root_stays_exact(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory) / "inventory"
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory}, clear=True), patch("inventory.commands.storage_health", return_value=(True, "reachable")):
				result = inventory_command(f"setup storage {root}")
		self.assertIn(str(root), result)
		self.assertNotIn("inventory\\inventory", result)

	def test_cli_adapter_prints_handler_result(self):
		with patch("inventory.cli.inventory_cli", return_value="HomeBox authentication: PASS"), patch("builtins.print") as output:
			result = handle_inventory_cli(Namespace(inventory_command="setup", secrets=True))
		self.assertEqual(result, "HomeBox authentication: PASS")
		output.assert_called_once_with("HomeBox authentication: PASS")

	def test_cli_rejects_non_static_homebox_key_without_echoing_it(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "HOMEBOX_URL": "http://homebox"}, clear=True), patch("inventory.commands.getpass.getpass", return_value="not-a-key"):
				result = inventory_cli(["setup", "--secrets"])
		self.assertIn("must start with hb_", result)
		self.assertNotIn("not-a-key", result)

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
			with patch.dict(os.environ, {"HERMES_HOME": temporary_directory, "HOMEBOX_URL": "http://homebox"}, clear=True), patch("inventory.commands.getpass.getpass", return_value="hb_new-secret"), patch("inventory.homebox.get_entity_types", return_value=[]):
				result = inventory_cli(["setup", "--secrets"])
				dotenv = (Path(temporary_directory) / ".env").read_text(encoding="utf-8")
		self.assertEqual(result, "HomeBox authentication: PASS")
		self.assertIn("HOMEBOX_API_KEY=hb_new-secret", dotenv)
		self.assertNotIn("hb_new-secret", (Path(temporary_directory) / "inventory-config.json").read_text(encoding="utf-8") if (Path(temporary_directory) / "inventory-config.json").exists() else "")