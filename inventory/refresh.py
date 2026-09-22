"""Read-only reconciliation of local Inventory evidence and HomeBox records."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import get_settings
from inventory.constants import ITEM_SCHEMA, OBSERVATION_SCHEMA, PLUGIN_VERSION, SCHEMA_VERSION, STALE_RESERVATION_AGE_SECONDS, SUPPORTED_ITEM_SCHEMA_VERSIONS, SUPPORTED_OBSERVATION_SCHEMA_VERSIONS
from inventory.hashing import sha256_file
from inventory.identity import match_manifest
from inventory.storage import _ASSET_RE, atomic_json_write, begin_item_transaction, build_manifest, canonicalize_images, commit_item_transaction, upgrade_manifest_schema, write_catalog
from inventory.media import is_supported_image
from inventory.normalize import normalize_record


def _read_json(path):
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
		return payload if isinstance(payload, dict) else None
	except (OSError, json.JSONDecodeError, ValueError):
		return None


def _safe_image_path(item_root, relative_path):
	candidate = Path(str(relative_path or ""))
	if candidate.is_absolute() or str(relative_path).startswith("\\\\"):
		return None
	try:
		resolved = (item_root / candidate).resolve()
		resolved.relative_to(item_root.resolve())
		return resolved
	except ValueError:
		return None


def _canonical_scan(settings):
	result = {
		"valid_items": [], "corrupt_manifests": [], "unsupported_schema_versions": [],
		"duplicate_inventory_ids": [], "duplicate_asset_ids": [], "missing_images": [],
		"checksum_mismatches": [], "unsafe_image_paths": [], "duplicate_image_hashes": [],
	}
	inventory_ids, asset_ids, image_hashes = [], [], {}
	for path in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		manifest = _read_json(path)
		if manifest is None:
			result["corrupt_manifests"].append({"path": str(path)})
			continue
		if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") not in SUPPORTED_ITEM_SCHEMA_VERSIONS:
			result["unsupported_schema_versions"].append({"path": str(path), "schema": manifest.get("schema"), "schema_version": manifest.get("schema_version")})
			continue
		inventory_id = str(manifest.get("inventory_id", ""))
		asset_id = str(manifest.get("asset_id", ""))
		inventory_ids.append(inventory_id)
		asset_ids.append(asset_id)
		result["valid_items"].append({
			"path": str(path), "inventory_id": inventory_id, "asset_id": asset_id,
			"homebox_entity_id": manifest.get("homebox", {}).get("entity_id"), "status": manifest.get("status"),
			"manifest": manifest,
		})
		for image in manifest.get("images", []):
			if not isinstance(image, dict):
				continue
			image_path = _safe_image_path(path.parent, image.get("relative_path", ""))
			if image_path is None:
				result["unsafe_image_paths"].append({"item": inventory_id, "path": image.get("relative_path", "")})
			elif not image_path.is_file():
				result["missing_images"].append({"item": inventory_id, "path": str(image_path)})
			elif image.get("sha256") and sha256_file(image_path) != image["sha256"]:
				result["checksum_mismatches"].append({"item": inventory_id, "path": str(image_path)})
			else:
				digest = sha256_file(image_path)
				image_hashes.setdefault(digest, []).append({"inventory_id": inventory_id, "path": str(image_path)})
	result["duplicate_inventory_ids"] = sorted(value for value, count in Counter(inventory_ids).items() if value and count > 1)
	result["duplicate_asset_ids"] = sorted(value for value, count in Counter(asset_ids).items() if value and count > 1)
	result["duplicate_image_hashes"] = [{"sha256": digest, "images": entries} for digest, entries in sorted(image_hashes.items()) if len({entry["inventory_id"] for entry in entries}) > 1]
	return result


def _legacy_scan(root, canonical_paths):
	result = {"current": [], "legacy_candidates": [], "unknown_format": [], "orphaned_evidence": [], "stale_runtime_candidates": []}
	pending = root / "pending-uploads.json"
	if pending.is_file():
		result["stale_runtime_candidates"].append({"path": str(pending), "reason": "persistent-root pending upload state"})
	for directory in (root / "metadata", root / "originals"):
		if not directory.exists():
			continue
		for path in sorted(directory.rglob("*.json")):
			payload = _read_json(path)
			entry = {"path": str(path), "schema": payload.get("schema") if payload else None, "schema_version": payload.get("schema_version") if payload else None}
			if payload:
				entry.update({key: payload.get(key) for key in ("inventory_id", "asset_id", "homebox_entity_id", "entity_id", "identifiers", "source_images", "source_directory") if payload.get(key) is not None})
				entry["hashes"] = payload.get("image_hashes", payload.get("hashes", []))
			result["legacy_candidates" if payload else "unknown_format"].append(entry)
	originals = root / "originals"
	if originals.exists():
		for path in sorted(originals.rglob("*")):
			if not path.is_file() or not is_supported_image(path):
				continue
			entry = {"path": str(path), "filename": path.name, "size": path.stat().st_size, "sha256": sha256_file(path)}
			metadata = root / "metadata" / f"{path.parent.name}.json"
			if metadata.is_file():
				entry["related_metadata_path"] = str(metadata)
			result["orphaned_evidence"].append(entry)
	for directory in (root / "receipts", root / "observations"):
		if not directory.exists():
			continue
		for path in sorted(directory.glob("*.json")):
			payload = _read_json(path)
			if directory.name == "observations" and payload and payload.get("schema") == OBSERVATION_SCHEMA and payload.get("schema_version") in SUPPORTED_OBSERVATION_SCHEMA_VERSIONS:
				result["current"].append({"path": str(path), "kind": "observation"})
			elif directory.name == "receipts" and payload:
				result["current"].append({"path": str(path), "kind": "receipt"})
			elif payload:
				result["unknown_format"].append({"path": str(path), "schema": payload.get("schema"), "schema_version": payload.get("schema_version")})
			else:
				result["unknown_format"].append({"path": str(path), "schema": None, "schema_version": None})
	known = {root / "catalog.json", *canonical_paths}
	for path in sorted(root.rglob("*.json")) if root.exists() else []:
		if path in known or any(parent in path.parents for parent in (root / "items", root / "metadata", root / "originals", root / "receipts", root / "observations", root / "backups", root / ".asset-id-reservations")):
			continue
		payload = _read_json(path)
		if payload and payload.get("schema") == ITEM_SCHEMA:
			result["orphaned_evidence"].append({"path": str(path), "inventory_id": payload.get("inventory_id"), "asset_id": payload.get("asset_id")})
		elif payload:
			result["unknown_format"].append({"path": str(path), "schema": payload.get("schema"), "schema_version": payload.get("schema_version")})
	return result


def _reservation_scan(root, local_assets, homebox_assets, transactions=()):
	reservations, unmatched = [], []
	for path in sorted((root / ".asset-id-reservations").glob("*.json")) if (root / ".asset-id-reservations").exists() else []:
		payload = _read_json(path)
		asset_id = str((payload or {}).get("asset_id") or path.stem)
		if payload is None or not _ASSET_RE.fullmatch(asset_id):
			reservations.append({"path": str(path), "classification": "malformed_reservation"})
			continue
		reservation_id = str(payload.get("reservation_id") or "").strip()
		if not reservation_id:
			reservations.append({"path": str(path), "asset_id": asset_id, "classification": "legacy_tokenless_reservation"})
			continue
		try:
			reserved_at = datetime.fromisoformat(str(payload.get("reserved_at", "")).replace("Z", "+00:00"))
			if reserved_at.tzinfo is None:
				raise ValueError
			age_seconds = (datetime.now(UTC) - reserved_at.astimezone(UTC)).total_seconds()
		except (TypeError, ValueError):
			reservations.append({"path": str(path), "asset_id": asset_id, "classification": "malformed_reservation"})
			continue
		in_local, in_homebox = asset_id in local_assets, asset_id in homebox_assets
		classification = "reservation_matches_both" if in_local and in_homebox else "reservation_matches_canonical" if in_local else "reservation_matches_homebox" if in_homebox else "reservation_unmatched"
		associated_transaction = any(entry.get("asset_id") == asset_id for entry in transactions)
		if classification == "reservation_unmatched" and age_seconds >= STALE_RESERVATION_AGE_SECONDS and not associated_transaction:
			classification = "stale_orphaned_reservation"
		entry = {"path": str(path), "asset_id": asset_id, "reservation_id": reservation_id, "reserved_at": reserved_at.isoformat().replace("+00:00", "Z"), "classification": classification}
		reservations.append(entry)
		if classification in {"reservation_unmatched", "stale_orphaned_reservation"}:
			unmatched.append(entry)
	return reservations, unmatched


def _transactions(settings):
	entries = []
	for path in sorted(settings.items_dir.glob(".tmp-*")) if settings.items_dir.exists() else []:
		if not path.is_dir():
			continue
		manifest = _read_json(path / "item.json")
		try:
			age = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")
		except OSError:
			age = None
		entries.append({"path": str(path), "inventory_id": (manifest or {}).get("inventory_id"), "asset_id": (manifest or {}).get("asset_id"), "has_item_json": (path / "item.json").is_file(), "has_vision_json": (path / "vision.json").is_file(), "image_count": len(list((path / "images").glob("*"))) if (path / "images").is_dir() else 0, "modified_at": age})
	return entries


def _asset_report(local_assets, homebox_assets, reserved_assets, complete):
	local_assets, homebox_assets, reserved_assets = set(local_assets), set(homebox_assets), set(reserved_assets)
	occupied = local_assets | homebox_assets | reserved_assets
	result = {
		"used_locally": sorted(local_assets), "used_in_homebox": sorted(homebox_assets), "reserved": sorted(reserved_assets),
		"present_in_local_and_homebox": sorted(local_assets & homebox_assets), "homebox_only": sorted(homebox_assets - local_assets),
		"local_only": sorted(local_assets - homebox_assets), "reservation_only": sorted(reserved_assets - local_assets - homebox_assets), "conflicting": [],
		"next_available_candidate": None,
	}
	if complete:
		for number in range(1, 1000000):
			candidate = f"{number:06d}"
			asset_id = f"{candidate[:3]}-{candidate[3:]}"
			if asset_id not in occupied:
				result["next_available_candidate"] = asset_id
				break
	else:
		result["reason"] = "HomeBox state unavailable/incomplete"
	return result


def local_diagnostics(*, settings=None):
	"""Return lightweight local-only findings for status without HomeBox access."""
	settings = settings or get_settings()
	canonical = _canonical_scan(settings)
	local_assets = {item["asset_id"] for item in canonical["valid_items"] if _ASSET_RE.fullmatch(item["asset_id"])}
	legacy = _legacy_scan(settings.persistent_data_dir, [Path(item["path"]) for item in canonical["valid_items"]])
	transactions = _transactions(settings)
	reservations, unmatched = _reservation_scan(settings.persistent_data_dir, local_assets, set(), transactions)
	return {"legacy_candidates": legacy["legacy_candidates"], "unmatched_reservations": unmatched, "incomplete_transactions": transactions, "reservations": reservations}


def refresh(*, settings=None, homebox_entities=None):
	"""Return a detailed reconciliation preview without filesystem or HTTP mutation."""
	settings = settings or get_settings()
	canonical = _canonical_scan(settings)
	canonical_paths = [Path(item["path"]) for item in canonical["valid_items"]]
	legacy = _legacy_scan(settings.persistent_data_dir, canonical_paths)
	local_assets = {item["asset_id"] for item in canonical["valid_items"] if _ASSET_RE.fullmatch(item["asset_id"])}
	homebox_complete, homebox_error = True, None
	if homebox_entities is None:
		try:
			from inventory.homebox import HomeBoxEnumerationError, list_all_entities
			homebox_entities = list_all_entities()
		except Exception as exc:
			homebox_entities = list(getattr(exc, "partial_entities", []))
			homebox_complete, homebox_error = False, f"{type(exc).__name__}: {exc}"
	homebox_entities = [entity for entity in homebox_entities if isinstance(entity, dict)]
	homebox_asset_values = [str(entity.get("assetId", "")) for entity in homebox_entities if entity.get("assetId")]
	homebox_assets = {asset_id for asset_id in homebox_asset_values if _ASSET_RE.fullmatch(asset_id)}
	malformed_homebox_assets = sorted(asset_id for asset_id in homebox_asset_values if not _ASSET_RE.fullmatch(asset_id))
	transactions = _transactions(settings)
	reservations, unmatched_reservations = _reservation_scan(settings.persistent_data_dir, local_assets, homebox_assets, transactions)
	reserved_assets = {entry["asset_id"] for entry in reservations if entry.get("asset_id")}
	matches = {"represented_in_both": [], "local_only": [], "homebox_only": [], "ambiguous": [], "conflicts": []}
	matched_homebox_ids = set()
	conflicts = []
	if homebox_complete:
		for item in canonical["valid_items"]:
			match = match_manifest(item["manifest"], homebox_entities)
			if match["classification"] == "strong_match":
				entity = match["entity"]
				matched_homebox_ids.add(str(entity["id"]))
				matches["represented_in_both"].append({"inventory_id": item["inventory_id"], "entity_id": entity["id"], "kind": match["kind"]})
				if item["asset_id"] and entity.get("assetId") and item["asset_id"] != entity.get("assetId"):
					conflicts.append({"type": "asset_id_mismatch", "inventory_id": item["inventory_id"], "entity_id": entity["id"], "local_asset_id": item["asset_id"], "homebox_asset_id": entity.get("assetId")})
			elif match["classification"] in {"ambiguous", "candidate"}:
				matches["ambiguous"].append({"inventory_id": item["inventory_id"], "kind": match["kind"], "entity_ids": [entity["id"] for entity in match["candidates"]]})
			elif match["classification"] == "conflict":
				entry = {"type": "strong_identity_conflict", "inventory_id": item["inventory_id"], "evidence": match.get("evidence", [])}
				matches["conflicts"].append(entry)
				conflicts.append(entry)
			else:
				matches["local_only"].append({"inventory_id": item["inventory_id"], "asset_id": item["asset_id"]})
		for entity in homebox_entities:
			if str(entity.get("id")) not in matched_homebox_ids:
				matches["homebox_only"].append({"entity_id": entity.get("id"), "asset_id": entity.get("assetId"), "name": entity.get("name")})
	for asset_id in canonical["duplicate_asset_ids"]:
		conflicts.append({"type": "duplicate_local_asset_id", "asset_id": asset_id})
	for asset_id, count in Counter(homebox_asset_values).items():
		if _ASSET_RE.fullmatch(asset_id) and count > 1:
			conflicts.append({"type": "duplicate_homebox_asset_id", "asset_id": asset_id})
	for asset_id in malformed_homebox_assets:
		conflicts.append({"type": "malformed_homebox_asset_id", "asset_id": asset_id})
	for asset_id in sorted(local_assets & homebox_assets):
		paired = any(entry.get("inventory_id") and next((item for item in canonical["valid_items"] if item["inventory_id"] == entry["inventory_id"]), {}).get("asset_id") == asset_id and next((entity for entity in homebox_entities if str(entity.get("id")) == str(entry["entity_id"])), {}).get("assetId") == asset_id for entry in matches["represented_in_both"])
		if not paired:
			conflicts.append({"type": "unmatched_local_homebox_asset_id", "asset_id": asset_id})
	asset_ids = _asset_report(local_assets, homebox_assets, reserved_assets, homebox_complete)
	asset_ids["conflicting"] = sorted({str(value) for conflict in conflicts for value in (conflict.get("asset_id"), conflict.get("local_asset_id"), conflict.get("homebox_asset_id")) if value})
	proposed_actions = []
	if legacy["legacy_candidates"]:
		proposed_actions.append("migrate_legacy_record")
	if matches["homebox_only"]:
		proposed_actions.append("adopt_homebox_item")
	if matches["local_only"]:
		proposed_actions.append("recover_local_only_item")
	if matches["ambiguous"]:
		proposed_actions.append("inspect_ambiguous_match")
	if unmatched_reservations:
		proposed_actions.append("inspect_unmatched_reservation")
	if transactions:
		proposed_actions.append("inspect_incomplete_transaction")
	warnings = sum(bool(value) for value in (canonical["corrupt_manifests"], canonical["unsupported_schema_versions"], canonical["duplicate_inventory_ids"], canonical["duplicate_asset_ids"], canonical["duplicate_image_hashes"], canonical["missing_images"], canonical["checksum_mismatches"], canonical["unsafe_image_paths"], legacy["legacy_candidates"], legacy["unknown_format"], transactions, conflicts, matches["ambiguous"], matches["homebox_only"], matches["local_only"], unmatched_reservations, not homebox_complete))
	return {"mode": "read_only", "status": "WARN" if warnings else "PASS", "paths": {"persistent_data_dir": str(settings.persistent_data_dir), "runtime_dir": str(settings.runtime_dir)}, "canonical": canonical, "legacy": legacy, "homebox": {"complete": homebox_complete, "error": homebox_error, "items": [{"entity_id": entity.get("id"), "asset_id": entity.get("assetId"), "name": entity.get("name"), "description": entity.get("description", ""), "manufacturer": entity.get("manufacturer", ""), "quantity": entity.get("quantity", 1), "entity_type": entity.get("entityType"), "fields": entity.get("fields", []), "attachments": entity.get("attachments", []), "tags": entity.get("tags", []), "location": entity.get("location"), "group_id": entity.get("groupId"), "purchase_price": entity.get("purchasePrice"), "purchase_date": entity.get("purchaseDate", ""), "purchase_from": entity.get("purchaseFrom", "")} for entity in homebox_entities]}, "asset_ids": asset_ids, "reservations": {"entries": reservations, "unmatched": unmatched_reservations}, "transactions": {"incomplete": transactions}, "matches": matches, "conflicts": conflicts, "proposed_actions": sorted(proposed_actions)}


def _adoption_inventory_id(entity_id):
	return f"INV-HB-{hashlib.sha256(str(entity_id).encode('utf-8')).hexdigest()[:12].upper()}"


def _field_map(entity):
	result = {}
	for field in entity.get("fields", []):
		if isinstance(field, dict) and field.get("name"):
			result[str(field["name"]).casefold()] = field
	return result


def _adopted_manifest(entity, inventory_id):
	fields = _field_map(entity)
	def value(name):
		field = fields.get(name.casefold(), {})
		return field.get("textValue", field.get("numberValue", ""))
	identifiers = {}
	for key, name in (("isbn_10", "ISBN-10"), ("isbn_13", "ISBN-13"), ("upc", "UPC"), ("ean", "EAN"), ("barcode_text", "Barcode"), ("serial_number", "Serial Number"), ("model_number", "Model Number")):
		values = [part.strip() for part in str(value(name) or entity.get("serialNumber" if key == "serial_number" else "modelNumber" if key == "model_number" else "")).split(";") if part.strip()]
		if values:
			identifiers[key] = values
	return {"schema": ITEM_SCHEMA, "schema_version": SCHEMA_VERSION, "inventory_id": inventory_id, "asset_id": entity.get("assetId") or None, "status": "synced", "plugin_version": PLUGIN_VERSION, "item": {"name": entity.get("name", ""), "category": "", "manufacturer": entity.get("manufacturer", ""), "description": entity.get("description", ""), "condition": [], "quantity": entity.get("quantity", 1)}, "identifiers": identifiers, "attributes": [], "tags": entity.get("tags", []), "location": entity.get("location", {"name": None, "path": []}), "purchase_price": entity.get("purchase_price"), "purchase_date": entity.get("purchase_date", ""), "purchase_from": entity.get("purchase_from", ""), "images": [], "homebox": {"entity_id": entity.get("id"), "asset_id": entity.get("assetId"), "collection_id": entity.get("groupId"), "entity_type": (entity.get("entityType") or {}).get("id") if isinstance(entity.get("entityType"), dict) else entity.get("entityTypeId"), "last_synced_at": None, "fields": entity.get("fields", []), "tags": entity.get("tags", []), "location": entity.get("location"), "attachments": entity.get("attachments", [])}, "vision": {"raw_metadata_relative_path": None, "parse_status": "not_applicable"}, "field_sources": {}, "history": [{"operation": "homebox_adoption", "source": "system"}], "provenance": {"origin": "homebox_adoption", "local_originals": False, "sources": [{"type": "homebox_entity", "entity_id": entity.get("id")}]} }


def build_plan(report):
	"""Build deterministic local-only actions from a completed reconciliation scan."""
	actions, skipped = [], []
	if not report["homebox"]["complete"]:
		return {"actions": [], "skipped": [{"reason": "homebox_incomplete"}], "fingerprint": _fingerprint([])}
	canonical_ids = {item["inventory_id"] for item in report["canonical"]["valid_items"]}
	entity_ids = {str(item.get("homebox_entity_id")) for item in report["canonical"]["valid_items"] if item.get("homebox_entity_id")}
	entity_ids.update(str(entry["entity_id"]) for entry in report["matches"]["represented_in_both"])
	entity_ids.update(entity_id for conflict in report["matches"]["conflicts"] for evidence in conflict.get("evidence", []) for entity_id in evidence.get("entity_ids", []))
	ambiguous_entities = {str(entity_id) for entry in report["matches"]["ambiguous"] for entity_id in entry["entity_ids"]}
	for item in report["canonical"]["valid_items"]:
		if item["manifest"].get("schema_version") != SCHEMA_VERSION:
			actions.append({"type": "upgrade_manifest", "path": item["path"], "inventory_id": item["inventory_id"]})
		if any(isinstance(tag, dict) and tag.get("source") == "system" and str(tag.get("name", "")).startswith("Type: ") for tag in item["manifest"].get("tags", [])):
			actions.append({"type": "normalize_system_type_tags", "path": item["path"], "inventory_id": item["inventory_id"]})
	for entry in report["matches"]["represented_in_both"]:
		item = next(item for item in report["canonical"]["valid_items"] if item["inventory_id"] == entry["inventory_id"])
		if not item.get("homebox_entity_id"):
			actions.append({"type": "link_homebox", "path": item["path"], "inventory_id": entry["inventory_id"], "entity_id": entry["entity_id"]})
	for entity in report["homebox"]["items"]:
		entity_id = str(entity.get("entity_id"))
		if entity_id in entity_ids or entity_id in ambiguous_entities:
			continue
		if any(conflict.get("entity_id") == entity_id for conflict in report["conflicts"]):
			continue
		inventory_field = next((field for field in entity.get("fields", []) if isinstance(field, dict) and str(field.get("name", "")).casefold() == "inventory item id"), {})
		candidate = str(inventory_field.get("textValue", "")).strip()
		inventory_id = candidate if candidate and candidate not in canonical_ids else _adoption_inventory_id(entity_id)
		actions.append({"type": "adopt_homebox", "entity_id": entity_id, "inventory_id": inventory_id})
		canonical_ids.add(inventory_id)
	for legacy in report["legacy"]["legacy_candidates"]:
		metadata_path = Path(legacy["path"])
		payload = _read_json(metadata_path)
		inventory_id = str((payload or {}).get("item_id") or (payload or {}).get("inventory_id") or "").strip()
		asset_id = str((payload or {}).get("asset_id") or "").strip()
		source = metadata_path.parent.parent / "originals" / inventory_id
		linked_entity_id = str((payload or {}).get("homebox_entity_id") or (payload or {}).get("entity_id") or "")
		if metadata_path.parent.name != "metadata" or not inventory_id or inventory_id in canonical_ids or not _ASSET_RE.fullmatch(asset_id) or asset_id in report["asset_ids"]["used_locally"] or not source.is_dir():
			continue
		if asset_id in report["asset_ids"]["used_in_homebox"] and not any(str(entity["entity_id"]) == linked_entity_id and entity.get("asset_id") == asset_id for entity in report["homebox"]["items"]):
			continue
		images = sorted(path for path in source.iterdir() if path.is_file() and is_supported_image(path))
		if images:
			actions.append({"type": "migrate_legacy", "metadata_path": str(metadata_path), "source_directory": str(source), "inventory_id": inventory_id, "asset_id": asset_id})
			canonical_ids.add(inventory_id)
	for reservation in report["reservations"]["entries"]:
		if reservation.get("classification") == "stale_orphaned_reservation":
			actions.append({"type": "cleanup_stale_reservation", "path": reservation["path"], "asset_id": reservation["asset_id"], "reservation_id": reservation["reservation_id"]})
	for action in actions:
		action["precondition"] = _action_precondition(action, report)
	return {"actions": actions, "skipped": skipped, "fingerprint": _fingerprint(actions)}


def _path_digest(path):
	try:
		return sha256_file(Path(path))
	except OSError:
		return None


def _identity_snapshot(entity):
	fields = entity.get("fields", []) if isinstance(entity, dict) else []
	values = {}
	for field in fields:
		if not isinstance(field, dict):
			continue
		name = str(field.get("name", "")).casefold()
		if name in {"inventory item id", "image sha-256", "serial number"}:
			values.setdefault(name, []).append(str(field.get("textValue", field.get("numberValue", ""))).strip())
	return {"entity_id": str(entity.get("entity_id", entity.get("id", ""))), "asset_id": entity.get("asset_id", entity.get("assetId")), "fields": {name: sorted(value for value in entries if value) for name, entries in sorted(values.items())}}


def _action_precondition(action, report):
	"""Digest only evidence an action is allowed to mutate or depend on."""
	payload = {"type": action["type"]}
	if action["type"] in {"upgrade_manifest", "link_homebox"}:
		payload["manifest"] = _path_digest(action["path"])
		payload["entity_id"] = action.get("entity_id")
	if action["type"] == "link_homebox":
		entity = next((entry for entry in report["homebox"]["items"] if str(entry["entity_id"]) == str(action["entity_id"])), None)
		payload["identity"] = _identity_snapshot(entity or {})
	if action["type"] == "normalize_system_type_tags":
		payload["manifest"] = _path_digest(action["path"])
	if action["type"] == "adopt_homebox":
		payload["entity"] = next((entry for entry in report["homebox"]["items"] if str(entry["entity_id"]) == str(action["entity_id"])), None)
		payload["destination_exists"] = any(item["inventory_id"] == action["inventory_id"] for item in report["canonical"]["valid_items"])
	if action["type"] == "migrate_legacy":
		payload["metadata"] = _path_digest(action["metadata_path"])
		payload["images"] = [(path.name, _path_digest(path)) for path in sorted(Path(action["source_directory"]).iterdir()) if path.is_file() and is_supported_image(path)] if Path(action["source_directory"]).is_dir() else None
	if action["type"] == "cleanup_stale_reservation":
		payload["reservation"] = _path_digest(action["path"])
		payload["asset_id"] = action["asset_id"]
		payload["reservation_id"] = action["reservation_id"]
	return _fingerprint([payload])


def _fingerprint(actions):
	payload = json.dumps(actions, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
	return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _apply_action(action, report, settings):
	if action.get("precondition") != _action_precondition(action, report):
		raise RuntimeError(f"Refresh precondition changed for {action['type']}")
	if action["type"] == "upgrade_manifest":
		path = Path(action["path"])
		atomic_json_write(path, upgrade_manifest_schema(_read_json(path)))
	elif action["type"] == "link_homebox":
		path = Path(action["path"]); manifest = upgrade_manifest_schema(_read_json(path))
		manifest.setdefault("homebox", {})["entity_id"] = action["entity_id"]
		atomic_json_write(path, manifest)
	elif action["type"] == "normalize_system_type_tags":
		path = Path(action["path"])
		manifest = upgrade_manifest_schema(_read_json(path))
		manifest["tags"] = [tag for tag in manifest.get("tags", []) if not (isinstance(tag, dict) and tag.get("source") == "system" and str(tag.get("name", "")).startswith("Type: "))]
		atomic_json_write(path, manifest)
	elif action["type"] == "cleanup_stale_reservation":
		path = Path(action["path"])
		payload = _read_json(path)
		if not isinstance(payload, dict) or payload.get("asset_id") != action["asset_id"] or payload.get("reservation_id") != action["reservation_id"]:
			raise RuntimeError("Reservation changed during refresh")
		path.unlink()
	elif action["type"] == "adopt_homebox":
		entity = next(entry for entry in report["homebox"]["items"] if str(entry["entity_id"]) == action["entity_id"])
		transaction = begin_item_transaction(action["inventory_id"], settings)
		try:
			atomic_json_write(transaction / "item.json", _adopted_manifest({"id": entity["entity_id"], "assetId": entity["asset_id"], "name": entity["name"], "description": entity["description"], "manufacturer": entity["manufacturer"], "quantity": entity["quantity"], "entityType": entity["entity_type"], "fields": entity["fields"], "attachments": entity["attachments"], "tags": entity["tags"], "location": entity["location"], "groupId": entity["group_id"], "purchase_price": entity["purchase_price"], "purchase_date": entity["purchase_date"], "purchase_from": entity["purchase_from"]}, action["inventory_id"]))
			commit_item_transaction(transaction, action["inventory_id"], settings)
		except Exception:
			raise
	elif action["type"] == "migrate_legacy":
		raw = _read_json(Path(action["metadata_path"]))
		if raw is None:
			raise RuntimeError("Legacy metadata changed during refresh")
		transaction = begin_item_transaction(action["inventory_id"], settings)
		try:
			images_dir = transaction / "images"
			for source in sorted(Path(action["source_directory"]).iterdir()):
				if not source.is_file() or not is_supported_image(source):
					continue
				destination = images_dir / source.name
				shutil.copy2(source, destination)
				if sha256_file(source) != sha256_file(destination):
					raise RuntimeError(f"Legacy image copy verification failed: {source.name}")
			record = normalize_record(raw)
			record["asset_id"] = action["asset_id"]
			record["source_images"] = [path.name for path in sorted(images_dir.iterdir()) if path.is_file()]
			record["source_filenames"] = {name: name for name in record["source_images"]}
			record["image_hashes"] = [{"filename": name, "sha256": sha256_file(images_dir / name)} for name in record["source_images"]]
			canonicalize_images(record, images_dir)
			raw["item_id"] = action["inventory_id"]
			raw["source_directory"] = str(images_dir)
			raw["source_images"] = list(record["source_images"])
			atomic_json_write(transaction / "vision.json", raw)
			manifest = build_manifest(action["inventory_id"], record, raw, images_dir, status="synced", homebox={"entity_id": raw.get("homebox_entity_id") or raw.get("entity_id"), "asset_id": action["asset_id"], "collection_id": None, "entity_type": None, "last_synced_at": None})
			manifest["provenance"] = {"origin": "legacy_migration", "local_originals": True, "sources": [{"type": "legacy_metadata", "path": action["metadata_path"]}, {"type": "legacy_originals", "path": action["source_directory"]}]}
			atomic_json_write(transaction / "item.json", manifest)
			commit_item_transaction(transaction, action["inventory_id"], settings)
		except Exception:
			raise


def apply_refresh(*, settings=None, homebox_entities=None):
	"""Apply only deterministic local changes after a verified Inventory backup."""
	settings = settings or get_settings()
	report = refresh(settings=settings, homebox_entities=homebox_entities)
	plan = build_plan(report)
	if not plan["actions"]:
		return {"mode": "apply", "status": report["status"], "backup": None, "applied": [], "skipped": plan["skipped"], "report": report}
	from inventory.backup import create_backup, verify_backup
	backup = create_backup(settings=settings)
	verification = verify_backup(Path(backup["path"]))
	if verification.get("status") != "PASS":
		return {"mode": "apply", "status": "ERROR", "backup": backup, "backup_verification": verification, "applied": [], "skipped": [{"reason": "backup_verification_failed"}], "report": report}
	current = refresh(settings=settings, homebox_entities=homebox_entities)
	if build_plan(current)["fingerprint"] != plan["fingerprint"]:
		return {"mode": "apply", "status": "ERROR", "backup": backup, "backup_verification": verification, "applied": [], "skipped": [{"reason": "state_changed_during_refresh"}], "report": current}
	applied, failed = [], None
	for index, action in enumerate(plan["actions"]):
		try:
			_apply_action(action, current, settings)
			applied.append(action)
		except Exception as exc:
			failed = {"action": action, "error": f"{type(exc).__name__}: {exc}"}
			remaining = [{"reason": "not_attempted_after_failure", "action": entry} for entry in plan["actions"][index + 1:]]
			catalog_error = None
			if applied:
				try:
					write_catalog(settings)
				except Exception as catalog_exc:
					catalog_error = f"{type(catalog_exc).__name__}: {catalog_exc}"
			final = refresh(settings=settings, homebox_entities=homebox_entities)
			result = {"mode": "apply", "status": "ERROR", "backup": backup, "backup_verification": verification, "applied": applied, "failed": failed, "skipped": [*plan["skipped"], *remaining], "report": final, "final_report": final}
			if catalog_error:
				result["catalog_error"] = catalog_error
			return result
	write_catalog(settings)
	final = refresh(settings=settings, homebox_entities=homebox_entities)
	return {"mode": "apply", "status": final["status"], "backup": backup, "backup_verification": verification, "applied": applied, "failed": failed, "skipped": plan["skipped"], "report": final, "final_report": final}