"""
Hermes inventory plugin.

Registers inventory_ingest while keeping inventory business logic in the
local inventory package bundled with this plugin.
"""

import json
import shutil
import sys
import uuid
from pathlib import Path

from inventory.config import HERMES_HOME, INVENTORY_BASE_DIR
from inventory.uploads import (
	PendingUploadError,
	mark_pending_upload_consumed,
	resolve_pending_upload_batch,
	release_pending_upload_claim,
	start_pending_upload_watcher,
)

PLUGIN_ROOT = Path(__file__).resolve().parent
STAGING_ROOT = INVENTORY_BASE_DIR / "tool-staging"

SUPPORTED_IMAGES = {
	".png",
	".jpg",
	".jpeg",
	".gif",
	".webp",
	".bmp",
}

# These are plugin defaults, not hard overrides. Hermes operator configuration
# under auxiliary.hermes_inventory_vision takes precedence when present. The
# defaults match the local Qwen3-VL deployment used by this installation while
# still allowing the host to select another provider, model, or endpoint.
VISION_TASK_DEFAULTS = {
	"provider": "custom",
	"model": "qwen3-vl:8b-instruct-q8_0",
	"base_url": "http://192.168.1.160:30068/v1",
	"api_key": "ollama",
	"timeout": 600,
}


def _json_error(message: str, **extra) -> str:
	payload = {
		"status": "error",
		"created": False,
		"error": message,
	}
	payload.update(extra)
	return json.dumps(payload, indent=2)


def _normalize_image_paths(raw_paths):
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

		if path.suffix.lower() not in SUPPORTED_IMAGES:
			raise ValueError(
				f"Unsupported image type: {path.name}"
			)

		# Dashboard-pasted images live below HERMES_HOME/images.
		# Keeping this restriction prevents arbitrary host-file ingestion
		# through model-generated paths.
		try:
			path.relative_to(HERMES_HOME)
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


def _stage_images(image_paths):
	STAGING_ROOT.mkdir(parents=True, exist_ok=True)

	stage_dir = STAGING_ROOT / f"ingest-{uuid.uuid4().hex}"
	stage_dir.mkdir(parents=True, exist_ok=False)

	for index, source in enumerate(image_paths, start=1):
		destination = stage_dir / source.name

		if destination.exists():
			destination = stage_dir / f"{index:02d}_{source.name}"

		shutil.copy2(source, destination)

	return stage_dir


def inventory_ingest(
	image_paths=None,
	vision_client=None,
	use_pending_upload=False,
):
	"""Ingest one physical item represented by attached photographs."""

	pending_batch = None
	using_pending_upload = not image_paths and use_pending_upload is True
	if using_pending_upload:
		try:
			pending_batch = resolve_pending_upload_batch(claim=True)
			image_paths = [str(path) for path in pending_batch.image_paths]
		except PendingUploadError as exc:
			return _json_error(str(exc))
	elif not image_paths:
		return _json_error(
			"No image paths were supplied. Provide image_paths or set "
			"use_pending_upload to true."
		)

	try:
		images = _normalize_image_paths(image_paths)
	except Exception as exc:
		if pending_batch is not None:
			release_pending_upload_claim(pending_batch.batch_id)
		return _json_error(str(exc))

	stage_dir = None

	try:
		ingest = _load_inventory_ingest()
		stage_dir = _stage_images(images)
		if pending_batch is not None:
			# Staging succeeded, so the upload has been accepted for processing.
			# Mark it before vision/HomeBox work so an EXACT_DUPLICATE and a
			# later processing failure cannot cause accidental reuse.
			mark_pending_upload_consumed(pending_batch.batch_id)

		result = ingest(
			str(stage_dir),
			vision_client,
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
		return _json_error(
			f"Inventory ingestion failed: {type(exc).__name__}: {exc}"
		)

	finally:
		if pending_batch is not None:
			release_pending_upload_claim(pending_batch.batch_id)
		if stage_dir is not None:
			shutil.rmtree(stage_dir, ignore_errors=True)


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
			"Guidance for ingesting photographed physical items into HomeBox."
		),
	)

	schema = {
		"name": "inventory_ingest",
		"description": (
			"Authoritative action for physical-item inventory and HomeBox requests. "
			"Use this tool whenever the user asks to add, inventory, catalog, record, "
			"save, or put a photographed physical item into their inventory or HomeBox. "
			"Direct triggers include: 'add this to my inventory', 'add this item to "
			"HomeBox', 'inventory this', 'catalog this', 'record this item', 'put this "
			"in HomeBox', and 'add the thing I just uploaded'. Do not ask what kind of "
			"inventory the user means when this, item, thing, image, photo, or a recent "
			"upload is the referent, and do not ask the user to identify the object first. "
			"Prefer explicit image_paths when available. If paths are unavailable for a "
			"recent dashboard upload, call this tool with use_pending_upload=true. "
			"Multiple images in the newest pending batch may be views of one physical item. "
			"Do not call vision_analyze first; this tool performs its own vision analysis, "
			"identifier extraction, duplicate checking, image preservation, and HomeBox "
			"update. When classification is EXACT_DUPLICATE, no new HomeBox item was "
			"created and no follow-up decision is required: report the existing item and "
			"do not offer confirm, overwrite, skip, merge, or numbered choices."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"image_paths": {
					"type": "array",
					"items": {
						"type": "string"
					},
					"minItems": 1,
					"description": (
						"Local paths for all photographs of the same physical item "
						"attached to the current user message. Preserve the paths "
						"exactly as Hermes supplied them."
					),
				},
				"use_pending_upload": {
					"type": "boolean",
					"default": False,
					"description": (
						"Use the newest unexpired pending Hermes dashboard upload batch "
						"when explicit image_paths are unavailable."
					),
				},
			},
			"required": [],
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
			"Authoritative physical-item inventory/HomeBox action. Use for requests to "
			"add, inventory, catalog, record, save, or put a photographed item into "
			"inventory or HomeBox, including 'add this to my inventory', 'inventory this', "
			"'catalog this', and 'add the thing I just uploaded'. Prefer image_paths; use "
			"use_pending_upload=true when a recent dashboard upload has no exposed path. "
			"Do not ask what kind of inventory the user means or call vision_analyze first."
		),
		emoji="📦",
	)

	logger.info(
		"inventory plugin register_tool result=%r",
		registration,
	)
