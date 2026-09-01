"""Safe edits, reanalysis, and resynchronization of canonical inventory items."""

import json
from datetime import UTC, datetime
from pathlib import Path

from inventory.ingest import run_vision
from inventory.normalize import normalize_record, normalize_type
from inventory.storage import _ASSET_RE, allocate_asset_id, atomic_json_write, canonicalize_images, managed_type_tag, migrate_manifest, write_catalog, write_manifest


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
	exact, matches = [], []
	for manifest in _manifests(settings):
		item = manifest.get("item", {})
		identifiers = manifest.get("identifiers", {})
		strong = {str(value).casefold() for values in identifiers.values() if isinstance(values, list) for value in values}
		if needle in {str(manifest.get("asset_id", "")).casefold(), str(manifest.get("inventory_id", "")).casefold(), str(manifest.get("homebox", {}).get("entity_id", "")).casefold()} or needle in strong:
			exact.append(manifest)
			continue
		name = str(item.get("name", "")).casefold()
		if needle == name or needle in name:
			matches.append(manifest)
	if len(exact) == 1:
		return exact[0], []
	if exact:
		return None, exact
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
		"image_roles": [{"filename": Path(image.get("relative_path", "")).name, "inferred_role": image.get("role", "other")} for image in manifest.get("images", [])],
		"source_filenames": {Path(image.get("relative_path", "")).name: image.get("original_filename", image.get("source_filename", "")) for image in manifest.get("images", [])},
	}


def _apply_changes(manifest, changes):
	item = manifest.setdefault("item", {})
	sources = manifest.setdefault("field_sources", {})
	changes_log = {}
	aliases = {"physical_description": "description", "publisher": "manufacturer", "type": "category"}
	for key, value in changes.items():
		key = aliases.get(key, key)
		if key in {"name", "category", "manufacturer", "description", "condition"}:
			if key == "category":
				value = normalize_type(value)
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
			old = manifest.get("asset_id"); manifest["asset_id"] = str(value)
		elif key == "tags_add":
			old = list(manifest.get("tags", [])); names = {str(tag.get("name", "")).casefold() for tag in old if isinstance(tag, dict)}
			manifest["tags"] = old + [{"name": str(name), "source": "user"} for name in value if str(name).casefold() not in names]
		elif key == "tags_remove":
			old = list(manifest.get("tags", [])); remove = {str(name).casefold() for name in value}
			manifest["tags"] = [tag for tag in old if not (str(tag.get("name", "")).casefold() in remove and tag.get("source") != "system")]
		else:
			continue
		sources[key] = "user"
		changes_log[key] = {"old": old, "new": value}
	return changes_log


def _reconcile_managed_type_tag(manifest):
	managed = managed_type_tag(manifest.get("item", {}).get("category"))
	user_tags = [tag for tag in manifest.get("tags", []) if not (isinstance(tag, dict) and tag.get("source") == "system" and str(tag.get("name", "")).startswith("Type: "))]
	manifest["tags"] = [managed, *user_tags]


def _asset_id_available(asset_id, manifest, settings):
	if not _ASSET_RE.fullmatch(asset_id):
		raise ValueError("Asset ID must use NNN-NNN format")
	for candidate in _manifests(settings):
		if candidate.get("inventory_id") != manifest.get("inventory_id") and candidate.get("asset_id") == asset_id:
			raise ValueError(f"Asset ID is already in use: {asset_id}")
	try:
		from inventory.homebox import list_entities
		response = list_entities()
		entities = response.get("items", []) if isinstance(response, dict) else response
		for entity in entities if isinstance(entities, list) else []:
			if str(entity.get("assetId", "")) == asset_id and str(entity.get("id")) != str(manifest.get("homebox", {}).get("entity_id")):
				raise ValueError(f"Asset ID is already in use in HomeBox: {asset_id}")
	except ValueError:
		raise
	except Exception:
		pass


def _reconcile_image_names(manifest, images):
	if not images.is_dir():
		raise RuntimeError("Canonical Inventory images directory is missing")
	record = _record(manifest, images)
	canonicalize_images(record, images)
	by_name = {Path(image.get("relative_path", "")).name: image for image in manifest.get("images", [])}
	manifest["images"] = [{**by_name.get(source, {}), "relative_path": f"images/{source}", "canonical_filename": source, "original_filename": record["source_filenames"].get(source, source), "source_filename": record["source_filenames"].get(source, source), "role": next((role.get("inferred_role", "other") for role in record["image_roles"] if role.get("filename") == source), "other"), "sha256": next((entry.get("sha256", "") for entry in record["image_hashes"] if entry.get("filename") == source), "")} for source in record["source_images"]]


def update_item(target, operation, changes=None, vision_client=None, *, settings):
	manifest, candidates = resolve_item(target, settings)
	if manifest is None:
		if candidates:
			return {"status": "ambiguous", "candidates": [{"asset_id": item.get("asset_id"), "name": item.get("item", {}).get("name")} for item in candidates]}
		return {"status": "not_found"}
	manifest = migrate_manifest(manifest)
	root = settings.items_dir / manifest["inventory_id"]
	images = root / "images"
	if not manifest.get("asset_id"):
		manifest["asset_id"] = allocate_asset_id(settings)
	raw = None
	if operation == "edit":
		if "asset_id" in (changes or {}):
			_asset_id_available(str(changes["asset_id"]), manifest, settings)
		changes_log = _apply_changes(manifest, changes or {})
	elif operation == "reanalyze":
		old_vision = root / "vision.json"
		if old_vision.exists():
			atomic_json_write(root / "history" / f"vision-{_now().replace(':', '')}.json", json.loads(old_vision.read_text(encoding="utf-8")))
		raw, _ = run_vision(images, vision_client, metadata_path=root / "vision.json")
		fresh = normalize_record(raw)
		changes_log = {}
		for key, field, fresh_key in (("name", "name", "name"), ("category", "category", "category"), ("manufacturer", "manufacturer", "manufacturer"), ("description", "description", "physical_description"), ("condition", "condition", "condition"), ("identifiers", "identifiers", "identifiers"), ("attributes", "attributes", "attributes")):
			if manifest.get("field_sources", {}).get(key) != "user":
				if key in {"identifiers", "attributes"}:
					old = manifest.get(key); manifest[key] = fresh.get(fresh_key, old)
				else:
					old = manifest["item"].get(field); manifest["item"][field] = fresh.get(fresh_key, old)
				changes_log[key] = {"old": old, "new": fresh.get(fresh_key, old)}
		if manifest.get("field_sources", {}).get("image_roles") != "user":
			for image in manifest.get("images", []):
				name = Path(image.get("relative_path", "")).name
				image["role"] = next((role.get("inferred_role", "other") for role in fresh.get("image_roles", []) if role.get("filename") == name), image.get("role", "other"))
			changes_log["image_roles"] = "refreshed"
	elif operation == "resync":
		changes_log = {}
	else:
		return {"status": "error", "error": "operation must be edit, reanalyze, or resync"}
	if operation in {"edit", "reanalyze"}:
		_reconcile_image_names(manifest, images)
		if raw is not None:
			raw["source_images"] = [Path(image["relative_path"]).name for image in manifest.get("images", [])]
			raw.setdefault("result", {})["image_roles"] = [{"filename": Path(image["relative_path"]).name, "inferred_role": image.get("role", "other")} for image in manifest.get("images", [])]
			atomic_json_write(root / "vision.json", raw)
	_reconcile_managed_type_tag(manifest)
	manifest.setdefault("history", []).append({"timestamp": _now(), "operation": operation, "source": "user" if operation == "edit" else "system", "changes": changes_log})
	manifest["updated_at"] = _now()
	write_manifest(manifest, settings=settings)
	record = _record(manifest, images)
	from inventory.homebox import complete_entity
	entity_id = manifest.get("homebox", {}).get("entity_id")
	if entity_id:
		try:
			completed = complete_entity(entity_id, record, image_directory=images, upload_attachments=False)
			manifest["homebox"]["attachments"] = manifest["homebox"].get("attachments", completed.get("attachments", []))
			manifest["status"] = "synced"; manifest["error"] = None
		except Exception as exc:
			manifest["status"] = "pending_homebox_sync"; manifest["error"] = str(exc)
	write_manifest(manifest, settings=settings)
	write_catalog(settings)
	return {"status": "updated", "durable": True, "operation": operation, "asset_id": manifest["asset_id"], "inventory_id": manifest["inventory_id"], "name": manifest["item"].get("name"), "changes": changes_log}