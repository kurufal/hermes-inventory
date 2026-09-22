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
		"": "Commands: setup, status, doctor, refresh, storage, uploads, homebox, backup, recover, version, help",
		"setup": "Usage: /inventory setup [storage <absolute path>|storage default|homebox <url>|secrets|test|help]",
		"refresh": "Usage: /inventory refresh [--dry-run] [--verbose] [--resolve [<ambiguity-number> <inventory-id>] ]",
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
	lines = [
		f"Hermes home: {settings.hermes_home}",
		f"Hermes uploads: {settings.hermes_images_dir}",
		f"Inventory runtime: {settings.runtime_dir}",
		f"Inventory persistent data: {settings.persistent_data_dir}",
		f"Persistent source: {settings.persistent_source}",
		f"HomeBox URL: {'configured' if homebox_url(settings) else 'missing'}",
		f"HomeBox API key: {'configured' if homebox_api_key() else 'missing'}",
	]
	from inventory.refresh import local_diagnostics
	diagnostics = local_diagnostics(settings=settings)
	for label, entries in (("Legacy Inventory data", diagnostics["legacy_candidates"]), ("Unmatched Asset ID reservations", diagnostics["unmatched_reservations"]), ("Incomplete Inventory transactions", diagnostics["incomplete_transactions"])):
		if entries:
			lines.append(f"[WARN] {label} detected: {len(entries)}")
	if any(diagnostics[key] for key in ("legacy_candidates", "unmatched_reservations", "incomplete_transactions")):
		lines.extend(["Run:", "/inventory refresh"])
	return "\n".join(lines)


def _format_ambiguities(report, *, verbose=False):
	from inventory.refresh import ambiguity_groups
	groups = ambiguity_groups(report)
	if not groups:
		return []
	lines = ["", "Ambiguous:"]
	for number, group in enumerate(groups, start=1):
		entity = group["entity"]
		lines.extend([f"#{number} {entity.get('name') or 'Unnamed item'} [{entity.get('asset_id') or 'no Asset ID'}]", f"   {group['reason']}"])
		if verbose:
			lines.extend([f"   Entity ID: {entity['entity_id']}", "   Shared exact image evidence:", *[f"   {digest}" for digest in group.get("shared_hashes", [])], "   Candidates:"])
			legacy = {entry["path"]: entry for entry in report["legacy"]["legacy_candidates"]}
			for path in group["paths"]:
				candidate = legacy.get(path, {})
				inventory_id = candidate.get("item_id") or candidate.get("inventory_id") or "unknown"
				lines.extend([f"   - {inventory_id}", f"     Metadata: {path}", f"     Originals: {Path(path).parent.parent / 'originals' / str(inventory_id)}", f"     Image count: {len(candidate.get('hashes', []))}"])
			lines.extend(["   Automatic resolution: BLOCKED", f"   Preview: /inventory refresh --resolve --dry-run {number} <inventory-id>"])
	return lines


def _format_resolved_retry_groups(report):
	groups = report["legacy"].get("resolved_retry_groups", [])
	if not groups:
		return []
	items = {str(item.get("entity_id")): item for item in report["homebox"]["items"] if isinstance(item, dict)}
	lines = ["", "Historical retry groups:"]
	for group in groups:
		entity = items.get(str(group["entity_id"]), {})
		lines.extend([f"- {entity.get('name') or 'Unnamed item'} [{entity.get('asset_id') or 'no Asset ID'}]", f"  Canonical Inventory ID: {group['canonical_inventory_id']}", f"  Legacy retry records retained: {len(group['legacy_paths'])}", "  Status: RESOLVED"])
	return lines


def _format_refresh(report, *, apply_result=None, verbose=False, resolution_preview=False, plan=None, resolution=None):
	matches = report["matches"]
	mode = "RESOLUTION PREVIEW" if resolution_preview else ("READ-ONLY PREVIEW" if apply_result is None else "APPLY")
	lines = ["Hermes Inventory Refresh", f"Mode: {mode}"]
	if apply_result is None or resolution_preview:
		lines.append("No changes were made.")
	else:
		status = apply_result.get("status", "ERROR")
		applied = apply_result.get("applied", [])
		failed = apply_result.get("failed")
		skipped = apply_result.get("skipped", [])
		lines.append(f"Status: {status}")
		if status != "ERROR" and not applied and not failed and not skipped and apply_result.get("backup") is None and report["homebox"].get("complete") and status == "PASS":
			lines.append("Inventory is already reconciled.\nNo backup or changes were required.")
		elif status == "ERROR":
			lines.append("No reconciliation changes were applied." if not applied else "Reconciliation stopped after partial application.")
			if failed:
				lines.extend(["Failed:", f"- {failed.get('action', {}).get('type', 'action')}: {failed.get('error', 'unknown error')}"])
			elif skipped:
				reason = skipped[0].get("reason", "refresh_aborted")
				lines.append(f"Reason: {reason.replace('_', ' ')}.")
			lines.append("Run /inventory refresh again.")
		if applied:
			lines.extend(["Applied:", *[f"- {action.get('type', 'action')}" for action in applied]])
		if failed and any(entry.get("reason") == "not_attempted_after_failure" for entry in skipped):
			lines.append("Remaining actions were not attempted.")
	lines.extend(["", f"Canonical items: {len(report['canonical']['valid_items'])}", f"Legacy candidates: {len(report['legacy']['legacy_candidates'])}", f"HomeBox items: {len(report['homebox']['items'])}", f"HomeBox-only items: {len(matches['homebox_only'])}", f"Local-only items: {len(matches['local_only'])}", f"Asset ID conflicts: {len(report.get('asset_ids', {}).get('conflicting', []))}", f"Unmatched reservations: {len(report['reservations']['unmatched'])}", f"Incomplete transactions: {len(report['transactions']['incomplete'])}", f"Ambiguous matches: {len(matches['ambiguous'])}"])
	lines.extend(_format_ambiguities(report, verbose=verbose))
	if matches["ambiguous"] and not verbose:
		lines.extend(["", "Run:", "/inventory refresh --verbose"])
	if verbose:
		lines.extend(["", "Canonical:", *[f"- {entry['inventory_id']} [{entry['asset_id']}]" for entry in report["canonical"]["valid_items"]], "HomeBox-only:", *[f"- {entry.get('name') or 'Unnamed item'} [{entry.get('asset_id') or 'no Asset ID'}]" for entry in matches["homebox_only"]], "Local-only:", *[f"- {entry['inventory_id']} [{entry['asset_id']}]" for entry in matches["local_only"]], "Conflicts:", *[f"- {entry['type']}" for entry in report["conflicts"]]])
		lines.extend(_format_resolved_retry_groups(report))
		if plan is not None:
			lines.extend(["Deterministic plan:", *[f"- {action['type']}" for action in plan["actions"]]])
	if resolution is not None:
		action, group = resolution
		lines.extend(["", f"Ambiguous match #{group['display_number']}: {group['entity'].get('name') or 'Unnamed item'}", f"Selected legacy record: {action['inventory_id']}", f"Metadata: {action['metadata_path']}", f"Originals: {action['source_directory']}", "Validation: PASS", "Planned action: migrate_legacy", "HomeBox mutation: NONE", "Legacy source deletion: NONE", "Backup required on apply: YES", "", "To apply:", f"/inventory refresh --resolve {group['display_number']} {action['inventory_id']}"])
	if not report["homebox"]["complete"]:
		lines.append("[WARN] HomeBox enumeration incomplete; no globally safe next Asset ID is reported.")
	if plan is not None:
		suggestions = []
		if any(action["type"] == "adopt_homebox" for action in plan["actions"]): suggestions.append("adopt_homebox_item")
		if any(action["type"] == "migrate_legacy" for action in plan["actions"]): suggestions.append("migrate_legacy_record")
		if _format_ambiguities(report):
			suggestions.extend(["inspect_ambiguous_legacy_group", "resolve_ambiguous_match"])
		if report["transactions"]["incomplete"]: suggestions.append("inspect_incomplete_transaction")
		if suggestions:
			lines.extend(["", "Suggested next actions:", *[f"- {action}" for action in suggestions]])
	elif report.get("proposed_actions"):
		lines.extend(["", "Suggested next actions:", *[f"- {action}" for action in report["proposed_actions"]]])
	return "\n".join(lines)


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
	if any(state == "PASS" and label == "HomeBox authentication" for state, label, _ in checks):
		try:
			from inventory.refresh import refresh
			report = refresh(settings=settings)
			if report["legacy"]["legacy_candidates"]:
				lines.append(f"[WARN] Legacy Inventory data detected: {len(report['legacy']['legacy_candidates'])} records")
			if report["matches"]["homebox_only"]:
				lines.append(f"[WARN] HomeBox contains {len(report['matches']['homebox_only'])} items not represented locally")
			if report["legacy"]["legacy_candidates"] or report["matches"]["homebox_only"]:
				lines.extend(["Run:", "/inventory refresh"])
		except Exception as exc:
			lines.append(f"[WARN] Reconciliation preview unavailable: {type(exc).__name__}")
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
	if command == "refresh":
		arguments = ([parts[1]] if len(parts) > 1 else []) + (parts[2].split() if len(parts) > 2 else [])
		flags = {argument for argument in arguments if argument.startswith("--")}
		values = [argument for argument in arguments if not argument.startswith("--")]
		if flags - {"--dry-run", "--verbose", "--resolve"}:
			return _help("refresh")
		resolve, dry_run, verbose = "--resolve" in flags, "--dry-run" in flags, "--verbose" in flags
		if not resolve and values:
			return _help("refresh")
		if resolve and values and len(values) != 2:
			return "Usage: /inventory refresh --resolve [--dry-run] <ambiguity-number> <inventory-id>"
		from inventory.refresh import apply_refresh, apply_resolution, build_plan, build_resolution_action, refresh
		report = refresh(settings=settings)
		try:
			plan = build_plan(report)
		except KeyError:
			plan = {"actions": []}
		if not resolve:
			return _format_refresh(report, verbose=verbose, plan=plan)
		if values:
			try:
				ambiguity_number = int(values[0])
				action, group = build_resolution_action(report, ambiguity_number, values[1])
				group = {**group, "display_number": ambiguity_number}
			except ValueError as exc:
				return str(exc)
			if dry_run:
				return _format_refresh(report, verbose=verbose, resolution_preview=True, plan=plan, resolution=(action, group))
			result = apply_resolution(ambiguity_number, values[1], settings=settings)
		else:
			if dry_run:
				return _format_refresh(report, verbose=verbose, resolution_preview=True, plan=plan)
			result = apply_refresh(settings=settings)
		final_report = result.get("final_report", result["report"])
		try:
			final_plan = build_plan(final_report)
		except KeyError:
			final_plan = plan
		lines = _format_refresh(final_report, apply_result=result, verbose=verbose, plan=final_plan)
		if result.get("backup"):
			lines += f"\n\nBackup:\n{result['backup']['path']}\nBackup verification: {result.get('backup_verification', {}).get('status', 'not_run')}"
		return lines
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