"""Durable item manifests, catalogs, and atomic persistent writes."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inventory.config import get_settings


ITEM_SCHEMA = "hermes-inventory-item"
CATALOG_SCHEMA = "hermes-inventory-catalog"
SCHEMA_VERSION = 1


def _timestamp() -> str:
	return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary_name, path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def item_directory(item_id: str) -> Path:
	return get_settings().items_dir / item_id


def _image_entries(record: dict[str, Any], image_dir: Path) -> list[dict[str, Any]]:
	roles = {str(role.get("filename")): str(role.get("inferred_role", "")) for role in record.get("image_roles", []) if isinstance(role, dict)}
	hashes = {str(entry.get("filename")): entry for entry in record.get("image_hashes", []) if isinstance(entry, dict)}
	result = []
	for index, name in enumerate(record.get("source_images", []), start=1):
		path = image_dir / name
		if not path.is_file():
			continue
		hash_info = hashes.get(name, {})
		result.append({
			"role": roles.get(name) or "image",
			"relative_path": f"images/{name}",
			"source_filename": name,
			"mime_type": {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}.get(path.suffix.lower(), "application/octet-stream"),
			"sha256": hash_info.get("sha256", ""),
			"size": path.stat().st_size,
			"order": index,
		})
	return result


def build_manifest(item_id: str, record: dict[str, Any], raw_metadata: dict[str, Any], image_dir: Path, *, status: str, homebox: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
	previous = load_manifest(item_id) or {}
	return {
		"schema": ITEM_SCHEMA,
		"schema_version": SCHEMA_VERSION,
		"inventory_id": item_id,
		"created_at": previous.get("created_at", _timestamp()),
		"updated_at": _timestamp(),
		"status": status,
		"plugin_version": "0.2.0",
		"item": {"name": record.get("name", ""), "category": record.get("category", ""), "manufacturer": record.get("manufacturer", ""), "description": record.get("physical_description", ""), "condition": record.get("condition", []), "quantity": 1},
		"identifiers": record.get("identifiers", {}),
		"attributes": record.get("attributes", []),
		"tags": [],
		"location": {"name": None, "path": []},
		"images": _image_entries(record, image_dir),
		"homebox": homebox or previous.get("homebox", {"entity_id": None, "asset_id": None, "collection_id": None, "entity_type": None, "last_synced_at": None}),
		"vision": {"raw_metadata_relative_path": "vision.json", "parse_status": raw_metadata.get("parse_status")},
		"error": error,
	}


def write_manifest(manifest: dict[str, Any]) -> Path:
	path = item_directory(str(manifest["inventory_id"])) / "item.json"
	atomic_json_write(path, manifest)
	return path


def load_manifest(item_id: str) -> dict[str, Any] | None:
	path = item_directory(item_id) / "item.json"
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None
	return payload if payload.get("schema") == ITEM_SCHEMA else None


def write_catalog() -> Path:
	settings = get_settings()
	items = []
	for candidate in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(candidate.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		item = manifest.get("item", {})
		images = manifest.get("images", [])
		items.append({"inventory_id": manifest.get("inventory_id"), "name": item.get("name"), "category": item.get("category"), "manufacturer": item.get("manufacturer"), "identifiers": manifest.get("identifiers", {}), "quantity": item.get("quantity", 1), "primary_image_relative_path": images[0].get("relative_path") if images else None, "homebox_entity_id": manifest.get("homebox", {}).get("entity_id"), "status": manifest.get("status"), "updated_at": manifest.get("updated_at")})
	path = settings.persistent_data_dir / "catalog.json"
	atomic_json_write(path, {"schema": CATALOG_SCHEMA, "schema_version": SCHEMA_VERSION, "updated_at": _timestamp(), "items": items})
	return path