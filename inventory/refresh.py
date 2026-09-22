"""Read-only reconciliation of local Inventory evidence and HomeBox records."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import get_settings
from inventory.constants import ITEM_SCHEMA, OBSERVATION_SCHEMA, SCHEMA_VERSION
from inventory.hashing import sha256_file
from inventory.identity import match_manifest
from inventory.storage import _ASSET_RE


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
		"checksum_mismatches": [], "unsafe_image_paths": [],
	}
	inventory_ids, asset_ids = [], []
	for path in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		manifest = _read_json(path)
		if manifest is None:
			result["corrupt_manifests"].append({"path": str(path)})
			continue
		if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") not in {1, SCHEMA_VERSION}:
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
	result["duplicate_inventory_ids"] = sorted(value for value, count in Counter(inventory_ids).items() if value and count > 1)
	result["duplicate_asset_ids"] = sorted(value for value, count in Counter(asset_ids).items() if value and count > 1)
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
	for directory in (root / "receipts", root / "observations"):
		if not directory.exists():
			continue
		for path in sorted(directory.glob("*.json")):
			payload = _read_json(path)
			if directory.name == "observations" and payload and payload.get("schema") == OBSERVATION_SCHEMA and payload.get("schema_version") == SCHEMA_VERSION:
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


def _reservation_scan(root, local_assets, homebox_assets):
	reservations, unmatched = [], []
	for path in sorted((root / ".asset-id-reservations").glob("*.json")) if (root / ".asset-id-reservations").exists() else []:
		payload = _read_json(path)
		asset_id = str((payload or {}).get("asset_id") or path.stem)
		if not _ASSET_RE.fullmatch(asset_id):
			reservations.append({"path": str(path), "classification": "malformed_reservation"})
			continue
		in_local, in_homebox = asset_id in local_assets, asset_id in homebox_assets
		classification = "reservation_matches_both" if in_local and in_homebox else "reservation_matches_canonical" if in_local else "reservation_matches_homebox" if in_homebox else "reservation_unmatched"
		entry = {"path": str(path), "asset_id": asset_id, "classification": classification}
		reservations.append(entry)
		if classification == "reservation_unmatched":
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
		entries.append({"path": str(path), "inventory_id": (manifest or {}).get("inventory_id"), "has_item_json": (path / "item.json").is_file(), "has_vision_json": (path / "vision.json").is_file(), "image_count": len(list((path / "images").glob("*"))) if (path / "images").is_dir() else 0, "modified_at": age})
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
	reservations, unmatched = _reservation_scan(settings.persistent_data_dir, local_assets, set())
	return {"legacy_candidates": legacy["legacy_candidates"], "unmatched_reservations": unmatched, "incomplete_transactions": _transactions(settings), "reservations": reservations}


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
	homebox_assets = {str(entity.get("assetId", "")) for entity in homebox_entities if _ASSET_RE.fullmatch(str(entity.get("assetId", "")))}
	reservations, unmatched_reservations = _reservation_scan(settings.persistent_data_dir, local_assets, homebox_assets)
	reserved_assets = {entry["asset_id"] for entry in reservations if entry.get("asset_id")}
	matches = {"represented_in_both": [], "local_only": [], "homebox_only": [], "ambiguous": []}
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
			else:
				matches["local_only"].append({"inventory_id": item["inventory_id"], "asset_id": item["asset_id"]})
		for entity in homebox_entities:
			if str(entity.get("id")) not in matched_homebox_ids:
				matches["homebox_only"].append({"entity_id": entity.get("id"), "asset_id": entity.get("assetId"), "name": entity.get("name")})
	for asset_id in canonical["duplicate_asset_ids"]:
		conflicts.append({"type": "duplicate_local_asset_id", "asset_id": asset_id})
	asset_ids = _asset_report(local_assets, homebox_assets, reserved_assets, homebox_complete)
	transactions = _transactions(settings)
	asset_ids["conflicting"] = sorted({value for conflict in conflicts for value in (conflict.get("local_asset_id"), conflict.get("homebox_asset_id")) if value})
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
	warnings = sum(bool(value) for value in (canonical["corrupt_manifests"], canonical["unsupported_schema_versions"], canonical["duplicate_inventory_ids"], canonical["duplicate_asset_ids"], canonical["missing_images"], canonical["checksum_mismatches"], legacy["legacy_candidates"], transactions, conflicts, matches["ambiguous"], unmatched_reservations, not homebox_complete))
	return {"mode": "read_only", "status": "WARN" if warnings else "PASS", "paths": {"persistent_data_dir": str(settings.persistent_data_dir), "runtime_dir": str(settings.runtime_dir)}, "canonical": canonical, "legacy": legacy, "homebox": {"complete": homebox_complete, "error": homebox_error, "items": [{"entity_id": entity.get("id"), "asset_id": entity.get("assetId"), "name": entity.get("name"), "entity_type": entity.get("entityType"), "fields": entity.get("fields", []), "attachments": entity.get("attachments", []), "tags": entity.get("tags", []), "location": entity.get("location")} for entity in homebox_entities]}, "asset_ids": asset_ids, "reservations": {"entries": reservations, "unmatched": unmatched_reservations}, "transactions": {"incomplete": transactions}, "matches": matches, "conflicts": conflicts, "proposed_actions": sorted(proposed_actions)}