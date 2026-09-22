"""Durable item manifests, catalogs, and atomic persistent writes."""

from __future__ import annotations

import json
import os
import tempfile
import shutil
import uuid
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inventory.config import get_settings
from inventory.constants import CATALOG_SCHEMA, CATALOG_SCHEMA_VERSION, ITEM_SCHEMA, PLUGIN_VERSION, SCHEMA_VERSION, SUPPORTED_ITEM_SCHEMA_VERSIONS
from inventory.media import mime_type
from inventory.hashing import sha256_file


_ASSET_LOCK = threading.Lock()
_ASSET_RE = re.compile(r"^\d{3}-\d{3}$")
_INVENTORY_ID_RE = re.compile(r"^INV-[A-Za-z0-9._-]{1,120}$")


def is_valid_inventory_id(value: object) -> bool:
	"""Accept compatible Inventory IDs without allowing filesystem components."""
	return isinstance(value, str) and bool(_INVENTORY_ID_RE.fullmatch(value))


def format_asset_id(number: int) -> str:
	if number < 1 or number > 999999:
		raise ValueError("Asset ID number must be between 1 and 999999")
	text = f"{number:06d}"
	return f"{text[:3]}-{text[3:]}"


def reserve_asset_id(settings, requested: str | None = None, external_used_ids=()) -> tuple[str, str]:
	with _ASSET_LOCK:
		used = set()
		for path in settings.items_dir.glob("*/item.json") if settings.items_dir.exists() else []:
			try:
				asset_id = json.loads(path.read_text(encoding="utf-8")).get("asset_id", "")
				if _ASSET_RE.fullmatch(str(asset_id)):
					used.add(str(asset_id))
			except (OSError, json.JSONDecodeError):
				continue
		reservations = settings.persistent_data_dir / ".asset-id-reservations"
		reservations.mkdir(parents=True, exist_ok=True)
		used.update(path.stem for path in reservations.glob("*.json") if _ASSET_RE.fullmatch(path.stem))
		used.update(str(asset_id) for asset_id in external_used_ids if _ASSET_RE.fullmatch(str(asset_id)))
		def reserve(candidate):
			reservation_id = uuid.uuid4().hex
			try:
				fd = os.open(reservations / f"{candidate}.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
				with os.fdopen(fd, "w", encoding="utf-8") as handle:
					json.dump({"asset_id": candidate, "reservation_id": reservation_id, "reserved_at": _timestamp()}, handle)
				return candidate, reservation_id
			except FileExistsError:
				return None
		if requested:
			if not _ASSET_RE.fullmatch(requested):
				raise ValueError("Asset ID must use NNN-NNN format")
			if requested in used:
				raise ValueError(f"Asset ID is already in use: {requested}")
			reserved = reserve(requested)
			if reserved:
				return reserved
			raise ValueError(f"Asset ID is already in use: {requested}")
		for number in range(1, 1000000):
			candidate = format_asset_id(number)
			if candidate not in used:
				reserved = reserve(candidate)
				if reserved:
					return reserved
		raise RuntimeError("No Asset IDs remain")


def allocate_asset_id(settings, requested: str | None = None, external_used_ids=()) -> str:
	"""Compatibility allocation API; callers that need cleanup use reserve_asset_id."""
	return reserve_asset_id(settings, requested, external_used_ids)[0]


def release_asset_id_reservation(settings, asset_id: str, reservation_id: str) -> bool:
	"""Release only the reservation created by this operation."""
	path = settings.persistent_data_dir / ".asset-id-reservations" / f"{asset_id}.json"
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
		if payload.get("reservation_id") != reservation_id:
			return False
		path.unlink()
		return True
	except (OSError, json.JSONDecodeError, AttributeError):
		return False


def _slug(value: str, limit: int) -> str:
	value = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
	return (value[:limit].strip("-") or "item")


_ROLE_ALIASES = {
	"front cover": "front-cover", "back cover": "back-cover", "copyright page": "copyright-page",
	"isbn page": "isbn-page", "copyright isbn page": "copyright-isbn-page", "serial number label": "serial-label",
	"model number label": "model-label", "box front": "box-front", "box back": "box-back",
	"left side": "left-side", "right side": "right-side",
}


def normalize_image_role(value) -> str:
	text = " ".join(str(value or "other").replace("_", " ").replace("-", " ").casefold().split())
	return _ROLE_ALIASES.get(text, _slug(text, 30))


def canonical_image_name(asset_id: str, name: str, role: str, extension: str, index: int = 1) -> str:
	role = _slug(role, 30)
	suffix = f"_{index:02d}" if index > 1 else ""
	return f"{asset_id}_{_slug(name, 40)}_{role}{suffix}{extension.lower()}"


def canonicalize_images(record: dict[str, Any], image_dir: Path) -> dict[str, str]:
	"""Rename only files in an Inventory transaction/canonical images directory."""
	asset_id = record.get("asset_id")
	if not _ASSET_RE.fullmatch(str(asset_id)):
		raise ValueError("A valid Asset ID is required before canonicalizing images")
	roles = {str(entry.get("filename")): normalize_image_role(entry.get("inferred_role")) for entry in record.get("image_roles", []) if isinstance(entry, dict)}
	mapping, role_counts = [], {}
	for source_name in record.get("source_images", []):
		source = image_dir / source_name
		if not source.is_file():
			raise FileNotFoundError(source)
		role = roles.get(source_name, "image")
		role_key = _slug(role, 30)
		role_counts[role_key] = role_counts.get(role_key, 0) + 1
		target_name = canonical_image_name(str(asset_id), record.get("name", ""), role, source.suffix, role_counts[role_key])
		mapping.append((source_name, target_name))
	if len({target for _, target in mapping}) != len(mapping):
		raise RuntimeError("Canonical image names would collide")
	sources = {source for source, _ in mapping}
	for source_name, target_name in mapping:
		if target_name not in sources and (image_dir / target_name).exists():
			raise RuntimeError(f"Canonical image name already exists: {target_name}")
	before_hashes = {source: sha256_file(image_dir / source) for source, _ in mapping}
	temporary = {}
	for source_name, target_name in mapping:
		if source_name != target_name:
			staged = image_dir / f".rename-{uuid.uuid4().hex}"
			os.replace(image_dir / source_name, staged)
			temporary[source_name] = staged
	try:
		for source_name, target_name in mapping:
			if source_name != target_name:
				os.replace(temporary[source_name], image_dir / target_name)
	except Exception:
		for source_name, target_name in mapping:
			if source_name != target_name and (image_dir / target_name).exists():
				os.replace(image_dir / target_name, image_dir / source_name)
		for source_name, staged in temporary.items():
			if staged.exists():
				os.replace(staged, image_dir / source_name)
		raise
	for source_name, target_name in mapping:
		if sha256_file(image_dir / target_name) != before_hashes[source_name]:
			raise RuntimeError(f"Canonical image rename changed bytes: {source_name}")
	originals = dict(record.get("source_filenames", {}))
	name_map = dict(mapping)
	record["source_images"] = [name_map[name] for name in record.get("source_images", [])]
	record["source_filenames"] = {name_map[name]: originals.get(name, name) for name in name_map}
	for entry in record.get("image_roles", []):
		if isinstance(entry, dict) and entry.get("filename") in name_map:
			entry["filename"] = name_map[entry["filename"]]
	for entry in record.get("image_hashes", []):
		if isinstance(entry, dict) and entry.get("filename") in name_map:
			entry["filename"] = name_map[entry["filename"]]
	return name_map


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


def upgrade_manifest_schema(manifest: dict[str, Any]) -> dict[str, Any]:
	"""Upgrade a supported canonical manifest in memory without fabricating evidence."""
	if manifest.get("schema") != ITEM_SCHEMA:
		raise ValueError("Not an Inventory item manifest")
	if manifest.get("schema_version") not in SUPPORTED_ITEM_SCHEMA_VERSIONS:
		raise ValueError("Unsupported Inventory item schema version")
	manifest.setdefault("field_sources", {})
	manifest.setdefault("history", [])
	manifest.setdefault("tags", [])
	manifest.setdefault("location", {"name": None, "path": []})
	manifest.setdefault("homebox", {"entity_id": None, "attachments": []})
	for image in manifest.get("images", []):
		if not isinstance(image, dict):
			continue
		filename = Path(str(image.get("relative_path", ""))).name
		image.setdefault("canonical_filename", filename)
		image.setdefault("original_filename", image.get("source_filename", filename))
		image.setdefault("source_filename", image["original_filename"])
		image.setdefault("role", "other")
	provenance = manifest.get("provenance")
	if not isinstance(provenance, dict):
		provenance = {}
	if "origin" not in provenance:
		provenance["origin"] = "native_ingest" if manifest.get("images") else "legacy_migration"
	if "local_originals" not in provenance:
		provenance["local_originals"] = bool(manifest.get("images"))
	provenance.setdefault("sources", [])
	manifest["provenance"] = provenance
	vision = manifest.get("vision")
	if not isinstance(vision, dict):
		vision = {}
	if not provenance["local_originals"] and not manifest.get("images"):
		vision.setdefault("raw_metadata_relative_path", None)
		vision.setdefault("parse_status", "not_applicable")
	else:
		vision.setdefault("raw_metadata_relative_path", "vision.json")
	manifest["vision"] = vision
	manifest["schema_version"] = SCHEMA_VERSION
	return manifest


migrate_manifest = upgrade_manifest_schema


def item_directory(item_id: str, settings=None) -> Path:
	settings = settings or get_settings()
	if not is_valid_inventory_id(item_id):
		raise ValueError("Invalid Inventory ID")
	return settings.items_dir / item_id


def begin_item_transaction(item_id: str, settings) -> Path:
	if not is_valid_inventory_id(item_id):
		raise ValueError("Invalid Inventory ID")
	settings.items_dir.mkdir(parents=True, exist_ok=True)
	transaction = settings.items_dir / f".tmp-{item_id}-{uuid.uuid4().hex}"
	transaction.mkdir()
	(transaction / "images").mkdir()
	return transaction


def commit_item_transaction(transaction: Path, item_id: str, settings) -> Path:
	final = item_directory(item_id, settings)
	if final.exists():
		raise FileExistsError(f"Inventory item already exists: {final}")
	if not (transaction / "item.json").is_file():
		raise RuntimeError("Incomplete inventory transaction cannot be committed.")
	manifest = json.loads((transaction / "item.json").read_text(encoding="utf-8"))
	upgraded = upgrade_manifest_schema(manifest)
	if upgraded.get("provenance", {}).get("local_originals") and not (transaction / "vision.json").is_file():
		raise RuntimeError("Photographed inventory transactions require vision.json.")
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
		"asset_id": record.get("asset_id") or previous.get("asset_id"),
		"created_at": previous.get("created_at", _timestamp()),
		"updated_at": _timestamp(),
		"status": status,
		"plugin_version": PLUGIN_VERSION,
		"item": {"name": record.get("name", ""), "category": record.get("category", ""), "manufacturer": record.get("manufacturer", ""), "description": record.get("physical_description", ""), "condition": record.get("condition", []), "quantity": 1},
		"identifiers": record.get("identifiers", {}),
		"attributes": record.get("attributes", []),
		"tags": previous.get("tags", record.get("tags", [])),
		"location": previous.get("location", record.get("location", {"name": None, "path": []})),
		"images": _image_entries(record, image_dir),
		"homebox": homebox or previous.get("homebox", {"entity_id": None, "asset_id": None, "collection_id": None, "entity_type": None, "last_synced_at": None}),
		"vision": {"raw_metadata_relative_path": "vision.json", "parse_status": raw_metadata.get("parse_status")},
		"provenance": previous.get("provenance", {"origin": "native_ingest", "local_originals": True, "sources": []}),
		"error": error,
		"field_sources": previous.get("field_sources", record.get("field_sources", {})),
		"history": previous.get("history", []),
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
		if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") not in SUPPORTED_ITEM_SCHEMA_VERSIONS:
			corrupt.append(str(candidate))
			continue
	if corrupt:
		raise RuntimeError("Catalog was not replaced because item manifests are invalid: " + ", ".join(corrupt))
	for candidate in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		manifest = upgrade_manifest_schema(json.loads(candidate.read_text(encoding="utf-8")))
		item = manifest.get("item", {})
		images = manifest.get("images", [])
		items.append({"inventory_id": manifest.get("inventory_id"), "asset_id": manifest.get("asset_id"), "name": item.get("name"), "category": item.get("category"), "manufacturer": item.get("manufacturer"), "identifiers": manifest.get("identifiers", {}), "quantity": item.get("quantity", 1), "preview_image_relative_path": images[0].get("relative_path") if images else None, "homebox_entity_id": manifest.get("homebox", {}).get("entity_id"), "status": manifest.get("status"), "updated_at": manifest.get("updated_at")})
	path = settings.persistent_data_dir / "catalog.json"
	atomic_json_write(path, {"schema": CATALOG_SCHEMA, "schema_version": CATALOG_SCHEMA_VERSION, "updated_at": _timestamp(), "items": items})
	return path