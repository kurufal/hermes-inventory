"""The single non-secret `/inventory` command namespace."""

import json
import os
from pathlib import Path

from inventory.config import ConfigurationError, get_settings, storage_health, write_storage_config
from inventory.constants import PLUGIN_VERSION


def _help(topic=""):
	commands = {
		"": "Commands: setup, status, doctor, storage, uploads, homebox, backup, recover, version, help",
		"storage": "Usage: /inventory storage [show|test|set <absolute path>|reset|help]",
		"uploads": "Usage: /inventory uploads [status|help]",
		"homebox": "Usage: /inventory homebox [status|test|help]",
		"backup": "Usage: /inventory backup [create|status|list|verify [backup]|help]",
		"recover": "Usage: /inventory recover [status|scan|plan|help]",
	}
	return commands.get(topic, commands[""])


def _status(settings):
	return "\n".join([f"Hermes home: {settings.hermes_home}", f"Hermes uploads: {settings.hermes_images_dir}", f"Inventory runtime: {settings.runtime_dir}\nSource: {settings.runtime_source}", f"Inventory persistent data: {settings.persistent_data_dir}\nSource: {settings.persistent_source}", f"Inventory backups: {settings.backup_dir}\nSource: {settings.backup_source}", f"HomeBox URL: {'configured' if os.environ.get('HOMEBOX_URL', '').strip() else 'missing'}", f"HomeBox API key: {'configured' if os.environ.get('HOMEBOX_API_KEY', '') else 'missing'}", "TOON: unavailable"])


def inventory_command(raw_args="", **kwargs):
	del kwargs
	parts = str(raw_args or "").strip().split(maxsplit=1)
	command = parts[0].casefold() if parts else "help"
	arguments = parts[1] if len(parts) > 1 else ""
	try:
		settings = get_settings()
	except ConfigurationError as exc:
		return f"Inventory configuration error: {exc}"
	if command in {"help", ""}:
		return _help()
	if command == "version":
		return f"hermes-inventory {PLUGIN_VERSION}"
	if command == "status":
		return _status(settings)
	if command == "setup":
		missing = []
		if not os.environ.get("HOMEBOX_URL", "").strip(): missing.append("HOMEBOX_URL")
		if not os.environ.get("HOMEBOX_API_KEY", ""): missing.append("HOMEBOX_API_KEY")
		return _status(settings) + ("\nSetup complete." if not missing else "\nConfigure: " + ", ".join(missing))
	if command == "doctor":
		checks = []
		for label, path in (("Runtime storage", settings.runtime_dir), ("Persistent storage", settings.persistent_data_dir), ("Backup storage", settings.backup_dir)):
			ok, reason = storage_health(path)
			checks.append(f"[{'PASS' if ok else 'FAIL'}] {label}: {reason}")
		checks.append(f"[{'PASS' if os.environ.get('HOMEBOX_URL', '').strip() else 'FAIL'}] HomeBox URL")
		checks.append(f"[{'PASS' if os.environ.get('HOMEBOX_API_KEY', '') else 'FAIL'}] HomeBox API key")
		checks.append("[WARN] TOON adapter: unavailable")
		return "\n".join(checks)
	if command == "storage":
		operation, _, value = arguments.partition(" ")
		operation = operation.casefold() or "show"
		if operation == "help": return _help("storage")
		if operation == "show": return _status(settings)
		if operation == "test":
			ok, reason = storage_health(settings.persistent_data_dir)
			return f"Storage health: {'PASS' if ok else 'FAIL'} {reason}"
		if operation == "set" and value:
			if os.environ.get("INVENTORY_BASE_DIR", "").strip(): return "INVENTORY_BASE_DIR environment variable has higher priority and cannot be overridden."
			candidate = Path(value)
			ok, reason = storage_health(candidate)
			if not ok: return f"Storage not changed; destination is unavailable: {reason}"
			write_storage_config(candidate)
			return f"Storage configured: {candidate}\nExisting data was not moved."
		if operation == "reset":
			write_storage_config(None)
			return "Storage reset to the Hermes-home default unless INVENTORY_BASE_DIR is set. Existing data was not moved."
		return _help("storage")
	if command == "uploads":
		return _help("uploads") if arguments.casefold() == "help" else f"Watched Hermes image directory: {settings.hermes_images_dir}\nPrefixes: dashboard_, upload_, clip_\nRuntime state: {settings.pending_upload_state_path}"
	if command == "homebox":
		operation = arguments.casefold() or "status"
		if operation == "help": return _help("homebox")
		status = "HomeBox URL: " + ("configured" if os.environ.get("HOMEBOX_URL", "").strip() else "missing") + "\nAPI key: " + ("configured" if os.environ.get("HOMEBOX_API_KEY", "") else "missing") + "\nNative export/import: unavailable (no verified public API)."
		if operation == "status": return status + "\nConnection: not tested"
		if operation == "test":
			try:
				from inventory.homebox import get_entity_types
				entity_types = get_entity_types()
				return status + f"\nConnection: PASS (authenticated; {len(entity_types)} entity types)"
			except Exception as exc:
				return status + f"\nConnection: FAIL ({type(exc).__name__}: {exc})"
		return _help("homebox")
	if command == "backup":
		from inventory.backup import create_backup, list_backups, verify_backup
		operation, _, value = arguments.partition(" "); operation = operation.casefold() or "create"
		if operation == "help": return _help("backup")
		if operation == "create": return json.dumps(create_backup(), indent=2)
		if operation == "list": return "\n".join(str(path) for path in list_backups()) or "No inventory backups found."
		if operation == "status": return "Last backup: " + (str(list_backups()[0]) if list_backups() else "none")
		if operation == "verify":
			path = Path(value) if value else (list_backups()[0] if list_backups() else None)
			return json.dumps(verify_backup(path), indent=2) if path else "No inventory backups found."
		return _help("backup")
	if command == "recover":
		from inventory.recovery import plan, scan
		if arguments.casefold() == "help": return _help("recover")
		if not arguments or arguments.casefold() in {"status", "scan"}: return json.dumps(scan(), indent=2)
		if arguments.casefold() == "plan": return json.dumps(plan(settings=settings), indent=2)
		return _help("recover")
	return f"Unknown Inventory command: {parts[0]}\n\nUse /inventory help to see available commands."