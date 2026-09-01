"""Non-destructive inspection of canonical inventory evidence."""

import json
from collections import Counter
from pathlib import Path

from inventory.config import get_settings
from inventory.hashing import sha256_file
from inventory.storage import ITEM_SCHEMA, SCHEMA_VERSION
from inventory.constants import CATALOG_SCHEMA


def _safe_item_path(item_root: Path, relative_path: str) -> Path | None:
	candidate = Path(relative_path)
	if candidate.is_absolute() or relative_path.startswith("\\\\"):
		return None
	try:
		resolved = (item_root / candidate).resolve()
		resolved.relative_to(item_root.resolve())
		return resolved
	except ValueError:
		return None


def scan() -> dict:
	settings = get_settings()
	report = {"valid_items": [], "corrupt_manifests": [], "unsupported_schema_versions": [], "incomplete_transactions": [], "missing_images": [], "unsafe_image_paths": [], "checksum_mismatches": [], "pending_homebox_sync": [], "homebox_linked_items": [], "duplicate_inventory_ids": [], "duplicate_asset_ids": [], "duplicate_observations": [], "catalog_inconsistencies": []}
	ids, asset_ids = [], []
	if settings.items_dir.exists():
		report["incomplete_transactions"] = [str(path) for path in settings.items_dir.glob(".tmp-*") if path.is_dir()]
	for path in settings.items_dir.glob("*/item.json") if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(path.read_text(encoding="utf-8"))
			if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") not in {1, SCHEMA_VERSION}:
				report["unsupported_schema_versions"].append(str(path))
				continue
		except (OSError, json.JSONDecodeError, ValueError) as exc:
			report["corrupt_manifests"].append({"path": str(path), "error": str(exc)})
			continue
		item_id = str(manifest.get("inventory_id", ""))
		ids.append(item_id)
		asset_ids.append(str(manifest.get("asset_id", "")))
		report["valid_items"].append(item_id)
		if manifest.get("status") == "pending_homebox_sync":
			report["pending_homebox_sync"].append(item_id)
		if manifest.get("homebox", {}).get("entity_id"):
			report["homebox_linked_items"].append(item_id)
		for image in manifest.get("images", []):
			image_path = _safe_item_path(path.parent, str(image.get("relative_path", "")))
			if image_path is None:
				report["unsafe_image_paths"].append(str(image.get("relative_path", "")))
				continue
			if not image_path.is_file():
				report["missing_images"].append(str(image_path))
			elif image.get("sha256") and sha256_file(image_path) != image["sha256"]:
				report["checksum_mismatches"].append(str(image_path))
	report["duplicate_inventory_ids"] = [value for value, count in Counter(ids).items() if value and count > 1]
	report["duplicate_asset_ids"] = [value for value, count in Counter(asset_ids).items() if value and count > 1]
	catalog_path = settings.persistent_data_dir / "catalog.json"
	if catalog_path.exists():
		try:
			catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
			if catalog.get("schema") != CATALOG_SCHEMA or catalog.get("schema_version") != SCHEMA_VERSION:
				raise ValueError("unsupported catalog schema")
			entries = catalog.get("items")
			if not isinstance(entries, list):
				raise ValueError("catalog items is not a list")
			catalog_ids = [str(entry.get("inventory_id", "")) for entry in entries if isinstance(entry, dict)]
			for item_id in sorted(set(ids) - set(catalog_ids)):
				report["catalog_inconsistencies"].append(f"canonical item missing from catalog: {item_id}")
			for item_id in sorted(set(catalog_ids) - set(ids)):
				report["catalog_inconsistencies"].append(f"catalog entry has no canonical item: {item_id}")
			for item_id, count in Counter(catalog_ids).items():
				if item_id and count > 1:
					report["catalog_inconsistencies"].append(f"duplicate catalog inventory ID: {item_id}")
		except (OSError, json.JSONDecodeError, ValueError) as exc:
			report["catalog_inconsistencies"].append(f"invalid catalog: {exc}")
	observations = settings.persistent_data_dir / "observations"
	if observations.exists():
		report["duplicate_observations"] = [str(path) for path in observations.glob("*.json")]
	report["status"] = "PASS" if not any(report[key] for key in report if key not in {"valid_items", "status"}) else "WARN"
	return report


def plan(*, settings=None, homebox_entities=None) -> dict:
	"""Compare persisted canonical records with HomeBox without mutations."""
	settings = settings or get_settings()
	report = {"present_in_both": [], "persistent_missing_from_homebox": [], "homebox_entity_mismatch": [], "pending_homebox_sync": [], "possible_conflicts": [], "status": "WARN"}
	if homebox_entities is None:
		try:
			from inventory.homebox import list_entities
			response = list_entities()
			homebox_entities = response.get("items", []) if isinstance(response, dict) else []
		except Exception as exc:
			return {**report, "homebox": f"unavailable: {type(exc).__name__}: {exc}"}
	by_id = {str(entity.get("id")): entity for entity in homebox_entities if isinstance(entity, dict) and entity.get("id")}
	def field_values(entity, name):
		values = []
		for field in entity.get("fields", []):
			if isinstance(field, dict) and field.get("name") == name:
				values.extend(str(field.get(key, "")) for key in ("textValue", "numberValue") if field.get(key) is not None)
		return {value.strip().casefold() for value in values if value.strip()}
	def matched_by_identity(manifest):
		inventory_id = str(manifest.get("inventory_id", "")).casefold()
		for entity in homebox_entities:
			if inventory_id and inventory_id in field_values(entity, "Inventory Item ID"):
				return entity, "inventory_id"
		old_id = str(manifest.get("homebox", {}).get("entity_id") or "")
		if old_id in by_id:
			return by_id[old_id], "entity_id"
		hashes = {str(image.get("sha256", "")).casefold() for image in manifest.get("images", []) if image.get("sha256")}
		for entity in homebox_entities:
			if hashes & field_values(entity, "Image SHA-256"):
				return entity, "image_hash"
		field_names = {"isbn_13": "ISBN-13", "isbn_10": "ISBN-10", "upc": "UPC", "ean": "EAN", "barcode_text": "Barcode", "serial_number": "Serial Number"}
		for key, field_name in field_names.items():
			values = {str(value).casefold() for value in manifest.get("identifiers", {}).get(key, []) if str(value)}
			matches = [entity for entity in homebox_entities if values & field_values(entity, field_name)]
			if len(matches) == 1:
				return matches[0], field_name
		return None, None
	for path in settings.items_dir.glob("*/item.json") if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		if manifest.get("schema") != ITEM_SCHEMA:
			continue
		inventory_id = str(manifest.get("inventory_id", ""))
		if manifest.get("status") == "pending_homebox_sync":
			report["pending_homebox_sync"].append(inventory_id)
			continue
		entity, match_kind = matched_by_identity(manifest)
		entity_id = str(manifest.get("homebox", {}).get("entity_id") or "")
		if entity is None:
			name = str(manifest.get("item", {}).get("name") or "").casefold()
			if name and any(str(candidate.get("name", "")).casefold() == name for candidate in homebox_entities):
				report["possible_conflicts"].append(inventory_id)
			report["persistent_missing_from_homebox"].append(inventory_id)
		else:
			if match_kind != "entity_id" or (entity.get("name") and entity.get("name") != manifest.get("item", {}).get("name")):
				report["homebox_entity_mismatch"].append(inventory_id)
			else:
				report["present_in_both"].append(inventory_id)
	report["status"] = "PASS" if not any(report[key] for key in ("persistent_missing_from_homebox", "homebox_entity_mismatch", "pending_homebox_sync", "possible_conflicts")) else "WARN"
	return report