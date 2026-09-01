"""Safe edits, reanalysis, and resynchronization of canonical inventory items."""

import json
from datetime import UTC, datetime
from pathlib import Path

from inventory.ingest import run_vision
from inventory.normalize import normalize_record
from inventory.storage import allocate_asset_id, atomic_json_write, load_manifest, write_catalog, write_manifest


def _now():
	return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _manifests(settings):
	for path in settings.items_dir.glob("*/item.json") if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(path.read_text(encoding="utf-8"))
			if manifest.get("inventory_id"):
				yield manifest
		except (OSError, json.JSONDecodeError):
			continue


def resolve_item(target, settings):
	needle = str(target or "").strip().casefold()
	if not needle:
		return None, []
	matches = []
	for manifest in _manifests(settings):
		item = manifest.get("item", {})
		identifiers = manifest.get("identifiers", {})
		strong = {str(value).casefold() for values in identifiers.values() if isinstance(values, list) for value in values}
		if needle in {str(manifest.get("asset_id", "")).casefold(), str(manifest.get("inventory_id", "")).casefold(), str(manifest.get("homebox", {}).get("entity_id", "")).casefold()} or needle in strong:
			return manifest, []
		name = str(item.get("name", "")).casefold()
		if needle == name or needle in name:
			matches.append(manifest)
	return (matches[0], []) if len(matches) == 1 else (None, matches)


def _record(manifest, image_directory):
	item = manifest.get("item", {})
	return {
		"item_id": manifest["inventory_id"], "asset_id": manifest.get("asset_id"),
		"name": item.get("name", ""), "category": item.get("category", ""),
		"manufacturer": item.get("manufacturer", ""), "physical_description": item.get("description", ""),
		"condition": item.get("condition", []), "identifiers": manifest.get("identifiers", {}),
		"attributes": manifest.get("attributes", []), "source_directory": str(image_directory),
		"purchase_price": manifest.get("purchase_price"), "purchase_from": manifest.get("purchase_from", ""),
		"purchase_date": manifest.get("purchase_date", ""), "notes": manifest.get("notes", ""),
		"source_images": [Path(image.get("relative_path", "")).name for image in manifest.get("images", [])],
		"image_hashes": [{"filename": Path(image.get("relative_path", "")).name, "sha256": image.get("sha256", "")} for image in manifest.get("images", [])],
	}


def _apply_changes(manifest, changes):
	item = manifest.setdefault("item", {})
	sources = manifest.setdefault("field_sources", {})
	changes_log = {}
	aliases = {"description": "description", "physical_description": "description", "publisher": "manufacturer"}
	for key, value in changes.items():
		key = aliases.get(key, key)
		if key in {"name", "category", "manufacturer", "description", "condition"}:
			old = item.get(key); item[key] = value
		elif key in {"purchase_price", "purchase_from", "purchase_date", "notes"}:
			old = manifest.get(key); manifest[key] = value
		elif key == "location":
			old = manifest.get("location"); manifest["location"] = {"name": value, "path": []} if isinstance(value, str) else value
		elif key == "identifiers":
			old = manifest.get("identifiers"); manifest["identifiers"] = value
		elif key == "attributes":
			old = manifest.get("attributes"); manifest["attributes"] = value
		elif key == "asset_id":
			old = manifest.get("asset_id"); manifest["asset_id"] = value
		else:
			continue
		sources[key] = "user"
		changes_log[key] = {"old": old, "new": value}
	return changes_log


def update_item(target, operation, changes=None, vision_client=None, *, settings):
	manifest, candidates = resolve_item(target, settings)
	if manifest is None:
		if candidates:
			return {"status": "ambiguous", "candidates": [{"asset_id": item.get("asset_id"), "name": item.get("item", {}).get("name")} for item in candidates]}
		return {"status": "not_found"}
	root = settings.items_dir / manifest["inventory_id"]
	images = root / "images"
	if not manifest.get("asset_id"):
		manifest["asset_id"] = allocate_asset_id(settings)
	if operation == "edit":
		changes_log = _apply_changes(manifest, changes or {})
	elif operation == "reanalyze":
		old_vision = root / "vision.json"
		if old_vision.exists():
			atomic_json_write(root / "history" / f"vision-{_now().replace(':', '')}.json", json.loads(old_vision.read_text(encoding="utf-8")))
		raw, _ = run_vision(images, vision_client, metadata_path=root / "vision.json")
		fresh = normalize_record(raw)
		changes_log = {}
		for key, field in (("name", "name"), ("category", "category"), ("manufacturer", "manufacturer"), ("physical_description", "description")):
			if manifest.get("field_sources", {}).get(key) != "user":
				old = manifest["item"].get(field); manifest["item"][field] = fresh.get(key, old); changes_log[key] = {"old": old, "new": manifest["item"][field]}
		if manifest.get("field_sources", {}).get("attributes") != "user": manifest["attributes"] = fresh.get("attributes", [])
	elif operation == "resync":
		changes_log = {}
	else:
		return {"status": "error", "error": "operation must be edit, reanalyze, or resync"}
	manifest.setdefault("history", []).append({"timestamp": _now(), "operation": operation, "source": "user" if operation == "edit" else "system", "changes": changes_log})
	manifest["updated_at"] = _now()
	write_manifest(manifest, settings=settings)
	record = _record(manifest, images)
	from inventory.homebox import complete_entity
	entity_id = manifest.get("homebox", {}).get("entity_id")
	if entity_id:
		completed = complete_entity(entity_id, record, image_directory=images, upload_attachments=False)
		manifest["homebox"]["attachments"] = manifest["homebox"].get("attachments", completed.get("attachments", []))
	write_manifest(manifest, settings=settings)
	write_catalog(settings)
	return {"status": "updated", "durable": True, "operation": operation, "asset_id": manifest["asset_id"], "inventory_id": manifest["inventory_id"], "name": manifest["item"].get("name"), "changes": changes_log}