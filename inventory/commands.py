"""The `/inventory` command namespace and local secret setup command."""

import getpass
import json
import os
from pathlib import Path

from inventory.config import (
	ConfigurationError, get_settings, homebox_api_key, homebox_url, storage_health,
	storage_root_for_setup, write_homebox_api_key, write_homebox_url, write_storage_config,
)
from inventory.constants import PLUGIN_VERSION


def _help(topic=""):
	commands = {
		"": "Commands: setup, status, doctor, storage, uploads, homebox, backup, recover, version, help",
		"setup": "Usage: /inventory setup [storage <absolute path>|storage default|homebox <url>|secrets|test|help]",
		"storage": "Usage: /inventory storage [show|test|set <absolute path>|reset|help]",
	}
	return commands.get(topic, commands[""])


def _secret_status():
	if homebox_api_key():
		return "Hermes Inventory Secrets\n\nHOMEBOX_API_KEY: configured\n\nNo additional secrets are required."
	return "Hermes Inventory Secrets\n\nHOMEBOX_API_KEY: not configured\n\nAPI keys should not be entered into chat.\n\nRun this once from a local terminal:\n\nhermes inventory setup --secrets\n\nThe key will be entered using hidden input and stored in Hermes' normal secret environment file.\n\nAfterward return to Hermes Desktop and run:\n\n/inventory setup"


def _verification(settings):
	checks = []
	for label, path in (("Persistent storage", settings.persistent_data_dir), ("Hermes upload directory", settings.hermes_images_dir), ("Inventory runtime", settings.runtime_dir)):
		ok, reason = storage_health(path)
		state = "WARN" if label == "Persistent storage" and settings.persistent_source == "default" and ok else ("PASS" if ok else "FAIL")
		detail = "using local default" if state == "WARN" else reason
		checks.append((state, label, detail))
	url, key = homebox_url(settings), homebox_api_key()
	checks.append(("PASS" if url else "FAIL", "HomeBox URL", ""))
	checks.append(("PASS" if key else "FAIL", "HomeBox API key", ""))
	if url and key:
		try:
			from inventory.homebox import get_entity_types
			get_entity_types()
			checks.append(("PASS", "HomeBox authentication", ""))
		except Exception as exc:
			checks.append(("FAIL", "HomeBox authentication", f"{type(exc).__name__}: {exc}"))
	else:
		checks.append(("WARN", "HomeBox authentication", "credentials missing"))
	return checks


def _format_checks(checks):
	return "\n".join(f"[{state}] {label}" + (f": {detail}" if detail else "") for state, label, detail in checks)


def _status(settings):
	return "\n".join([
		f"Hermes home: {settings.hermes_home}",
		f"Hermes uploads: {settings.hermes_images_dir}",
		f"Inventory runtime: {settings.runtime_dir}",
		f"Inventory persistent data: {settings.persistent_data_dir}",
		f"Persistent source: {settings.persistent_source}",
		f"HomeBox URL: {'configured' if homebox_url(settings) else 'missing'}",
		f"HomeBox API key: {'configured' if homebox_api_key() else 'missing'}",
	])


def _setup_status(settings):
	checks = _verification(settings)
	lines = ["Hermes Inventory Setup", _format_checks(checks)]
	url, key = homebox_url(settings), homebox_api_key()
	if url and not key:
		lines.extend(["", "Inventory configuration is complete.", "", "Credential required:", "HomeBox API key: missing", "", "Run:", "/inventory setup secrets"])
	elif url and key and all(state == "PASS" for state, _, _ in checks):
		lines.extend(["", "Setup complete.", "", "Attach one or more photos of one physical item and say:", "", "Add this to my inventory."])
	elif url and key:
		lines.extend(["", "HomeBox authentication failed.", "Run:", "/inventory setup secrets"])
	else:
		lines.extend(["", "Configure HomeBox URL with:", "/inventory setup homebox <url>"])
	if settings.persistent_source == "default":
		lines.extend(["", "Persistent storage is using the local default:", str(settings.persistent_data_dir), "For permanent/NAS storage:", "/inventory setup storage <path>"])
	return "\n".join(lines)


def inventory_cli(args=None):
	"""Secure local CLI handler; keys are prompted, never parsed from args."""
	args = list(args or [])
	if args != ["setup", "--secrets"]:
		return "Usage: hermes inventory setup --secrets"
	settings = get_settings()
	if not homebox_url(settings):
		return "HomeBox URL is missing. Configure it first with: /inventory setup homebox <url>"
	existing = homebox_api_key()
	value = getpass.getpass("HomeBox API key (Enter to retain existing key): " if existing else "HomeBox API key: ")
	if value and not value.startswith("hb_"):
		return "HomeBox API key was not saved: current HomeBox static API keys must start with hb_.\nRetry with: hermes inventory setup --secrets"
	if value:
		write_homebox_api_key(value, settings)
	if not value and not existing:
		return "No API key was saved. Retry with: hermes inventory setup --secrets"
	try:
		from inventory.homebox import get_entity_types
		get_entity_types()
		return "HomeBox authentication: PASS"
	except Exception as exc:
		return f"HomeBox authentication: FAIL ({type(exc).__name__}: {exc})\nRetry with: hermes inventory setup --secrets"


def inventory_command(raw_args="", **kwargs):
	del kwargs
	parts = str(raw_args or "").strip().split(maxsplit=2)
	command = parts[0].casefold() if parts else "help"
	try:
		settings = get_settings()
	except ConfigurationError as exc:
		return f"Inventory configuration error: {exc}"
	if command in {"help", ""}:
		return _help()
	if command == "version":
		return f"hermes-inventory {PLUGIN_VERSION}"
	if command == "setup":
		operation = parts[1].casefold() if len(parts) > 1 else ""
		value = parts[2] if len(parts) > 2 else ""
		if operation in {"", "status"}:
			return _setup_status(settings)
		if operation == "help":
			return _help("setup")
		if operation == "secrets":
			return "Secrets are not accepted in chat.\n\n" + _secret_status() if value else _secret_status()
		if operation == "test":
			return _format_checks(_verification(settings))
		if operation == "storage":
			if value.casefold() == "default":
				write_storage_config(None)
				return "Storage reset to the Hermes-home default. Existing data was not moved."
			if not value:
				return _help("setup")
			if os.environ.get("INVENTORY_BASE_DIR", "").strip():
				return "INVENTORY_BASE_DIR environment variable has higher priority and cannot be overridden."
			candidate = storage_root_for_setup(value)
			ok, reason = storage_health(candidate)
			if not ok:
				return f"Storage not changed; destination is unavailable: {reason}"
			write_storage_config(candidate)
			return f"Storage configured.\n\nRequested parent:\n{value}\n\nInventory root:\n{candidate}\n\nStorage test: PASS\nExisting Inventory data was not moved."
		if operation == "homebox":
			if not value:
				return _help("setup")
			write_homebox_url(value)
			return f"HomeBox URL configured: {homebox_url(settings)}"
		return _help("setup")
	if command == "status":
		return _status(settings)
	if command == "doctor":
		return _format_checks(_verification(settings))
	if command == "storage":
		operation = parts[1].casefold() if len(parts) > 1 else "show"
		value = parts[2] if len(parts) > 2 else ""
		if operation == "show":
			return f"Persistent data: {settings.persistent_data_dir}\nRuntime: {settings.runtime_dir}\nBackups: {settings.backup_dir}"
		if operation == "test":
			ok, reason = storage_health(settings.persistent_data_dir)
			return f"Storage health: {'PASS' if ok else 'FAIL'} {reason}"
		if operation == "set":
			return inventory_command(f"setup storage {value}")
		if operation == "reset":
			return inventory_command("setup storage default")
		return _help("storage")
	if command == "homebox":
		if len(parts) > 1 and parts[1].casefold() == "test":
			checks = _verification(settings)
			authentication = next((state for state, label, _ in checks if label == "HomeBox authentication"), "FAIL")
			return _secret_status() + f"\nConnection: {authentication}"
		return _secret_status()
	if command == "uploads":
		return f"Watched Hermes image directory: {settings.hermes_images_dir}\nRuntime state: {settings.pending_upload_state_path}"
	if command == "backup":
		from inventory.backup import create_backup, list_backups, verify_backup
		operation = parts[1].casefold() if len(parts) > 1 else "create"
		value = parts[2] if len(parts) > 2 else ""
		if operation == "create": return json.dumps(create_backup(), indent=2)
		if operation == "list": return "\n".join(str(path) for path in list_backups()) or "No inventory backups found."
		if operation == "verify":
			path = Path(value) if value else (list_backups()[0] if list_backups() else None)
			return json.dumps(verify_backup(path), indent=2) if path else "No inventory backups found."
	if command == "recover":
		from inventory.recovery import plan, scan
		return json.dumps(plan(settings=settings) if len(parts) > 1 and parts[1].casefold() == "plan" else scan(), indent=2)
	return f"Unknown Inventory command: {parts[0]}\n\nUse /inventory help to see available commands."