"""Durable item manifests, catalogs, and atomic persistent writes."""

from __future__ import annotations

import json
import os
import tempfile
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inventory.config import get_settings
from inventory.constants import CATALOG_SCHEMA, ITEM_SCHEMA, PLUGIN_VERSION, SCHEMA_VERSION
from inventory.media import mime_type


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


def item_directory(item_id: str, settings=None) -> Path:
	settings = settings or get_settings()
	return settings.items_dir / item_id


def begin_item_transaction(item_id: str, settings) -> Path:
	settings.items_dir.mkdir(parents=True, exist_ok=True)
	transaction = settings.items_dir / f".tmp-{item_id}-{uuid.uuid4().hex}"
	transaction.mkdir()
	(transaction / "images").mkdir()
	return transaction


def commit_item_transaction(transaction: Path, item_id: str, settings) -> Path:
	final = item_directory(item_id, settings)
	if final.exists():
		raise FileExistsError(f"Inventory item already exists: {final}")
	if not (transaction / "item.json").is_file() or not (transaction / "vision.json").is_file():
		raise RuntimeError("Incomplete inventory transaction cannot be committed.")
	os.replace(transaction, final)
	return final


def abandon_item_transaction(transaction: Path) -> None:
	shutil.rmtree(transaction, ignore_errors=True)


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
			"source_filename": record.get("source_filenames", {}).get(name, name),
			"mime_type": mime_type(path),
			"sha256": hash_info.get("sha256", ""),
			"size": path.stat().st_size,
			"order": index,
		})
	return result


def build_manifest(item_id: str, record: dict[str, Any], raw_metadata: dict[str, Any], image_dir: Path, *, status: str, homebox: dict[str, Any] | None = None, error: str | None = None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
	previous = previous or {}
	return {
		"schema": ITEM_SCHEMA,
		"schema_version": SCHEMA_VERSION,
		"inventory_id": item_id,
		"created_at": previous.get("created_at", _timestamp()),
		"updated_at": _timestamp(),
		"status": status,
		"plugin_version": PLUGIN_VERSION,
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


def write_manifest(manifest: dict[str, Any], path: Path | None = None, settings=None) -> Path:
	path = path or item_directory(str(manifest["inventory_id"]), settings) / "item.json"
	atomic_json_write(path, manifest)
	return path


def load_manifest(item_id: str, settings=None) -> dict[str, Any] | None:
	path = item_directory(item_id, settings) / "item.json"
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None
	return payload if payload.get("schema") == ITEM_SCHEMA else None


def write_catalog(settings=None) -> Path:
	settings = settings or get_settings()
	items = []
	corrupt = []
	for candidate in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(candidate.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			corrupt.append(str(candidate))
			continue
		if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
			corrupt.append(str(candidate))
			continue
	if corrupt:
		raise RuntimeError("Catalog was not replaced because item manifests are invalid: " + ", ".join(corrupt))
	for candidate in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		manifest = json.loads(candidate.read_text(encoding="utf-8"))
		item = manifest.get("item", {})
		images = manifest.get("images", [])
		items.append({"inventory_id": manifest.get("inventory_id"), "name": item.get("name"), "category": item.get("category"), "manufacturer": item.get("manufacturer"), "identifiers": manifest.get("identifiers", {}), "quantity": item.get("quantity", 1), "primary_image_relative_path": images[0].get("relative_path") if images else None, "homebox_entity_id": manifest.get("homebox", {}).get("entity_id"), "status": manifest.get("status"), "updated_at": manifest.get("updated_at")})
	path = settings.persistent_data_dir / "catalog.json"
	atomic_json_write(path, {"schema": CATALOG_SCHEMA, "schema_version": SCHEMA_VERSION, "updated_at": _timestamp(), "items": items})
	return path