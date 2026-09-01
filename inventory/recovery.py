"""Non-destructive inspection of canonical inventory evidence."""

import json
from collections import Counter
from pathlib import Path

from inventory.config import get_settings
from inventory.hashing import sha256_file
from inventory.storage import ITEM_SCHEMA, SCHEMA_VERSION


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
	report = {"valid_items": [], "corrupt_manifests": [], "unsupported_schema_versions": [], "incomplete_transactions": [], "missing_images": [], "unsafe_image_paths": [], "checksum_mismatches": [], "pending_homebox_sync": [], "homebox_linked_items": [], "duplicate_inventory_ids": [], "duplicate_observations": [], "catalog_inconsistencies": []}
	ids = []
	if settings.items_dir.exists():
		report["incomplete_transactions"] = [str(path) for path in settings.items_dir.glob(".tmp-*") if path.is_dir()]
	for path in settings.items_dir.glob("*/item.json") if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(path.read_text(encoding="utf-8"))
			if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
				report["unsupported_schema_versions"].append(str(path))
				continue
		except (OSError, json.JSONDecodeError, ValueError) as exc:
			report["corrupt_manifests"].append({"path": str(path), "error": str(exc)})
			continue
		item_id = str(manifest.get("inventory_id", ""))
		ids.append(item_id)
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
		entity_id = str(manifest.get("homebox", {}).get("entity_id") or "")
		if not entity_id:
			report["persistent_missing_from_homebox"].append(inventory_id)
		elif entity_id not in by_id:
			report["persistent_missing_from_homebox"].append(inventory_id)
		else:
			entity = by_id[entity_id]
			if entity.get("name") and entity.get("name") != manifest.get("item", {}).get("name"):
				report["homebox_entity_mismatch"].append(inventory_id)
			else:
				report["present_in_both"].append(inventory_id)
	report["status"] = "PASS" if not any(report[key] for key in ("persistent_missing_from_homebox", "homebox_entity_mismatch", "pending_homebox_sync", "possible_conflicts")) else "WARN"
	return report