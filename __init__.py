"""
Hermes inventory plugin.

Registers inventory_ingest while keeping inventory business logic in the
local inventory package bundled with this plugin.
"""

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

# Hermes imports this file as a submodule of its own plugin package and does
# not add the plugin's own directory to sys.path. Every module in this
# repository imports its sibling "inventory" package with an absolute import
# (from inventory.xxx import yyy), which only resolves when this directory is
# itself importable as a top-level package location. Prepend it explicitly so
# loading works both under Hermes' plugin loader and under a plain test
# runner, without switching every file to relative imports.
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
	sys.path.insert(0, _PLUGIN_DIR)

from inventory.config import get_settings, storage_health, write_storage_config
from inventory.media import is_supported_image
from inventory.uploads import (
	PendingUploadError,
	mark_pending_upload_consumed,
	resolve_pending_upload_batch,
	release_pending_upload_claim,
	start_pending_upload_watcher,
)

PLUGIN_ROOT = Path(__file__).resolve().parent

# These are plugin defaults, not hard overrides. Hermes operator configuration
# under auxiliary.hermes_inventory_vision takes precedence when present. The
# Hermes operator configuration selects provider, model, endpoint, and secrets.
VISION_TASK_DEFAULTS = {"timeout": 600}


def _json_error(message: str, **extra) -> str:
	payload = {
		"status": "error",
		"created": False,
		"error": message,
	}
	payload.update(extra)
	return json.dumps(payload, indent=2)


def _normalize_image_paths(raw_paths, settings=None):
	settings = settings or get_settings()
	if isinstance(raw_paths, str):
		raw_paths = [raw_paths]

	if not isinstance(raw_paths, list) or not raw_paths:
		raise ValueError(
			"image_paths must contain at least one attached image path"
		)

	resolved = []

	for raw in raw_paths:
		if not isinstance(raw, str) or not raw.strip():
			raise ValueError("Every image path must be a non-empty string")

		path = Path(raw.strip()).expanduser()

		if not path.is_absolute():
			path = Path.cwd() / path

		try:
			path = path.resolve(strict=True)
		except FileNotFoundError:
			raise ValueError(f"Attached image does not exist: {raw}")

		if not path.is_file():
			raise ValueError(f"Attached image is not a regular file: {path}")

		if not is_supported_image(path):
			raise ValueError(
				f"Unsupported image type: {path.name}"
			)

		# Hermes-managed attachments live below HERMES_HOME/images.
		# Keeping this restriction prevents arbitrary host-file ingestion
		# through model-generated paths.
		try:
			path.relative_to(settings.hermes_images_dir.resolve())
		except ValueError:
			raise ValueError(
				f"Image path is outside HERMES_HOME and was refused: {path}"
			)

		if path not in resolved:
			resolved.append(path)

	if not resolved:
		raise ValueError("No usable attached images were supplied")

	return resolved


def _load_inventory_ingest():
	"""Load the backend that is packaged next to this plugin entry point."""

	root_string = str(PLUGIN_ROOT)

	if root_string not in sys.path:
		sys.path.insert(0, root_string)

	from inventory.ingest import ingest

	return ingest


def _stage_images(image_paths, settings=None):
	settings = settings or get_settings()
	staging_root = settings.runtime_dir / "tool-staging"
	staging_root.mkdir(parents=True, exist_ok=True)

	stage_dir = staging_root / f"ingest-{uuid.uuid4().hex}"
	stage_dir.mkdir(parents=True, exist_ok=False)
	provenance = {}

	for index, source in enumerate(image_paths, start=1):
		destination = stage_dir / source.name

		if destination.exists():
			destination = stage_dir / f"{index:02d}_{source.name}"

		shutil.copy2(source, destination)
		provenance[destination.name] = source.name

	(stage_dir / ".inventory-provenance.json").write_text(
		json.dumps(provenance, ensure_ascii=False), encoding="utf-8"
	)

	return stage_dir


def inventory_command(raw_args="", **kwargs):
	"""Handle the single non-secret /inventory command namespace."""
	del kwargs
	parts = str(raw_args or "").strip().split(maxsplit=1)
	command = parts[0].casefold() if parts else "help"
	arguments = parts[1] if len(parts) > 1 else ""
	settings = get_settings()
	if command in {"help", ""}:
		return "Commands: /inventory setup, status, doctor, homebox, storage, uploads, backup, recover, version, help"
	if command == "version":
		return "hermes-inventory 0.2.0"
	if command in {"status", "setup", "doctor"}:
		ok, reason = storage_health(settings.persistent_data_dir)
		return "\n".join([f"Hermes home: {settings.hermes_home}", f"Hermes uploads: {settings.hermes_images_dir}", f"Inventory runtime: {settings.runtime_dir} ({settings.runtime_source})", f"Inventory persistent data: {settings.persistent_data_dir} ({settings.persistent_source})", f"Inventory backups: {settings.backup_dir} ({settings.backup_source})", f"Persistent storage: {'PASS' if ok else 'FAIL'} {reason}", f"HomeBox URL: {'configured' if os.environ.get('HOMEBOX_URL') else 'missing'}", f"HomeBox API key: {'configured' if os.environ.get('HOMEBOX_API_KEY') else 'missing'}", f"TOON: {'enabled (adapter unavailable)' if settings.toon_enabled else 'disabled'}"])
	if command == "storage":
		operation, _, path = arguments.partition(" ")
		operation = operation.casefold() or "show"
		if operation in {"show", "test"}:
			ok, reason = storage_health(settings.persistent_data_dir)
			return f"Persistent data: {settings.persistent_data_dir}\nRuntime: {settings.runtime_dir}\nBackups: {settings.backup_dir}\nStorage health: {'PASS' if ok else 'FAIL'} {reason}"
		if operation == "set" and path:
			if os.environ.get("INVENTORY_BASE_DIR", "").strip():
				return "INVENTORY_BASE_DIR environment variable has higher priority and cannot be overridden by plugin config."
			candidate = Path(path)
			ok, reason = storage_health(candidate)
			if not ok:
				return f"Storage not changed; destination is unavailable: {reason}"
			write_storage_config(candidate)
			return f"Storage configured: {candidate}\nPrevious location was not moved."
		if operation == "reset":
			write_storage_config(None)
			return "Storage config reset to the Hermes-home default unless INVENTORY_BASE_DIR is set."
		return "Usage: /inventory storage [show|test|set <path>|reset]"
	if command == "uploads":
		return f"Watched Hermes image directory: {settings.hermes_images_dir}\nPrefixes: dashboard_, upload_, clip_\nRuntime state: {settings.pending_upload_state_path}"
	if command == "backup":
		from inventory.backup import create_backup, list_backups, verify_backup
		operation, _, value = arguments.partition(" ")
		operation = operation.casefold() or "create"
		if operation in {"create", ""}:
			return json.dumps(create_backup(), indent=2)
		if operation == "list":
			return "\n".join(str(path) for path in list_backups()) or "No inventory backups found."
		if operation == "verify":
			path = Path(value) if value else (list_backups()[0] if list_backups() else None)
			return json.dumps(verify_backup(path), indent=2) if path else "No inventory backups found."
		return "Usage: /inventory backup [create|list|verify [path]]"
	if command == "recover":
		from inventory.recovery import scan
		if not arguments or arguments.casefold() in {"status", "scan", "plan"}:
			return json.dumps(scan(), indent=2)
		return "Usage: /inventory recover [status|scan|plan]. Automatic apply is not supported."
	if command == "homebox":
		return "HomeBox URL and API key are configured through Hermes environment/secrets. Native export/import is unavailable because no verified public API was found in this environment."
	return f"Unknown Inventory command: {parts[0]}\n\nUse /inventory help to see available commands."


def inventory_ingest(
	image_paths=None,
	vision_client=None,
	use_pending_upload=False,
):
	"""Ingest one physical item represented by attached photographs."""

	settings = get_settings()
	pending_batch = None
	using_pending_upload = not image_paths and use_pending_upload is True
	if using_pending_upload:
		try:
			pending_batch = resolve_pending_upload_batch(
				claim=True, images_dir=settings.hermes_images_dir,
				state_path=settings.pending_upload_state_path,
				ttl_seconds=settings.pending_ttl_seconds,
				retention_seconds=settings.state_retention_seconds,
				batch_window_seconds=settings.batch_window_seconds,
			)
			image_paths = [str(path) for path in pending_batch.image_paths]
		except PendingUploadError as exc:
			return _json_error(
				str(exc),
				error_stage="pending_upload_resolution",
				pending_debug=getattr(exc, "debug", {}) or {},
			)
	elif not image_paths:
		return _json_error(
			"No image paths were supplied. Provide image_paths or set "
			"use_pending_upload to true."
		)

	try:
		images = _normalize_image_paths(image_paths, settings)
	except Exception as exc:
		if pending_batch is not None:
			release_pending_upload_claim(pending_batch.batch_id)
		return _json_error(str(exc))

	stage_dir = None

	try:
		ingest = _load_inventory_ingest()
		stage_dir = _stage_images(images, settings)
		if pending_batch is not None:
			# Staging succeeded, so the upload has been accepted for processing.
			# Mark it before vision/HomeBox work so an EXACT_DUPLICATE and a
			# later processing failure cannot cause accidental reuse.
			mark_pending_upload_consumed(pending_batch.batch_id)

		result = ingest(
			str(stage_dir), vision_client, settings=settings,
		)

		if not isinstance(result, dict):
			return _json_error(
				"Inventory backend returned an unexpected result",
				backend_result=str(result),
			)

		result["tool"] = "inventory_ingest"
		result["input_image_count"] = len(images)

		if result.get("classification") == "EXACT_DUPLICATE":
			candidates = (
				result.get("duplicate_check", {})
				.get("candidates", [])
			)

			candidate = candidates[0] if candidates else {}

			result["requires_user_action"] = False
			result["assistant_instruction"] = (
				"This is an EXACT_DUPLICATE. No new HomeBox item was created "
				"and no user decision is required. Tell the user that the item "
				"is already in inventory and identify the existing item using "
				"the candidate name and asset_id when available. Do not offer "
				"confirm, overwrite, skip, merge, or numbered choices. Do not "
				"speculate about the image filename or question the duplicate "
				"classification."
			)

			result["existing_item"] = {
				"name": candidate.get("name"),
				"manufacturer": candidate.get("manufacturer"),
				"asset_id": candidate.get("asset_id"),
				"entity_id": candidate.get("entity_id"),
			}

		return json.dumps(result, indent=2)

	except Exception as exc:
		debug = getattr(exc, "debug", None)
		if isinstance(debug, dict) and debug:
			return _json_error(str(exc), **debug)
		return _json_error(
			f"Inventory ingestion failed: {type(exc).__name__}: {exc}"
		)

	finally:
		if pending_batch is not None:
			release_pending_upload_claim(pending_batch.batch_id)
		if stage_dir is not None:
			shutil.rmtree(stage_dir, ignore_errors=True)


# Kept as a compatibility name above; command implementation lives separately.
from inventory.commands import inventory_command


def register(ctx):
	"""Register inventory_ingest through Hermes' public plugin context."""

	import logging

	logger = logging.getLogger("hermes_plugins.hermes_inventory")
	start_pending_upload_watcher(logger=logger)

	ctx.register_auxiliary_task(
		"hermes_inventory_vision",
		display_name="Inventory vision",
		description=(
			"Analyze photographs of one physical inventory item."
		),
		defaults={
			**VISION_TASK_DEFAULTS,
		},
	)

	ctx.register_skill(
		"inventory",
		PLUGIN_ROOT / "skills" / "inventory" / "SKILL.md",
		description=(
			"Routing for physical-item inventory/HomeBox requests. Load this skill "
			"for phrases such as 'add this to my inventory', 'inventory this', "
			"'catalog this', 'record this item', or 'add the thing I just uploaded'."
		),
	)

	schema = {
		"name": "inventory_ingest",
		"description": (
			"MANDATORY tool for adding photographed physical items to HomeBox "
			"inventory. Call me first. I gather the details myself. CALL THIS TOOL "
			"immediately when the user says things like 'add this to my inventory', "
			"'add this item to my inventory', 'inventory this', 'catalog this', "
			"'add this to HomeBox', or 'add the item I just uploaded'. Do NOT ask "
			"the user for the item name, description, category, quantity, location, "
			"model, condition, or other item details. Do NOT ask the user what kind "
			"of inventory they mean. Do NOT call vision_analyze first. This tool "
			"performs its own vision analysis and determines those fields itself. "
			"If image_paths are available, pass them. If the user refers to an image "
			"they just uploaded and no explicit path is available, call this tool "
			"with use_pending_upload=true. When use_pending_upload=true, DO NOT ask "
			"for confirmation before calling. Actually invoke the tool. Do not call "
			"Clarify or ask the user which inventory system, tool, or plugin to use; "
			"this tool is the only correct action for a photographed physical item. "
			"When classification is EXACT_DUPLICATE, no new HomeBox item was created "
			"and no follow-up decision is required: report the existing item and do "
			"not offer confirm, overwrite, skip, merge, or numbered choices."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"image_paths": {
					"type": "array",
					"items": {
						"type": "string"
					},
					"description": (
						"Explicit local image paths, when Hermes provides them."
					),
				},
				"use_pending_upload": {
					"type": "boolean",
					"description": (
						"Set this to true when the user refers to an image or physical "
						"item they just uploaded but Hermes does not expose an explicit "
						"image path. For requests such as 'add this to my inventory', "
						"use true automatically. Do not ask the user for item details "
						"first."
					),
				},
			},
			"additionalProperties": False,
		},
	}

	def handle_inventory_ingest(params, **kwargs):
		del kwargs

		if not isinstance(params, dict):
			return _json_error("Tool parameters must be an object")

		return inventory_ingest(
			params.get("image_paths", []),
			ctx.llm,
			use_pending_upload=params.get(
				"use_pending_upload",
				False,
			),
		)

	registration = ctx.register_tool(
		name="inventory_ingest",
		toolset="inventory",
		schema=schema,
		handler=handle_inventory_ingest,
		requires_env=[
			"HOMEBOX_URL",
			"HOMEBOX_API_KEY",
		],
		description=(
			"MANDATORY tool for adding photographed physical items to HomeBox "
			"inventory. Call me first. I gather the details myself. Call immediately "
			"for 'add this to my inventory', 'inventory this', 'catalog this', 'add "
			"this to HomeBox', or 'add the item I just uploaded'. Do not ask for item "
			"details first. Prefer image_paths; use use_pending_upload=true when a "
			"recent dashboard upload has no exposed path, and invoke it immediately "
			"without asking for confirmation. Do not call vision_analyze or Clarify."
		),
		emoji="📦",
	)

	logger.info(
		"inventory plugin register_tool result=%r",
		registration,
	)
	register_command = getattr(ctx, "register_command", None)
	if callable(register_command):
		register_command(name="inventory", handler=inventory_command, description="Inventory setup, storage, backup, and recovery commands.")
