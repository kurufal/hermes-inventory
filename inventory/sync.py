"""Conservative, explicit three-way synchronization of already linked records."""

import copy
import json
import threading
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from inventory import homebox
from inventory.backup import create_backup, verify_backup
from inventory.constants import ITEM_SCHEMA, SUPPORTED_ITEM_SCHEMA_VERSIONS
from inventory.identity import entity_field_values
from inventory.normalize import TYPE_SYNONYMS
from inventory.storage import atomic_json_write, is_valid_inventory_id, release_asset_id_reservation, reserve_asset_id, write_catalog, write_manifest
from inventory.update import _now, validate_edit_changes


BASELINE_SCHEMA = "hermes-inventory-sync"
BASELINE_VERSION = 1
_SYNC_LOCK = threading.Lock()
_ITEM_FIELDS = {"name", "description", "manufacturer", "category"}
_REMOTE_FIELDS = {"name": "name", "description": "description", "manufacturer": "manufacturer", "purchase_price": "purchasePrice", "purchase_date": "purchaseDate", "purchase_from": "purchaseFrom", "asset_id": "assetId"}
_FIELDS = (*_REMOTE_FIELDS, "location", "notes", "category", "tags")


class MappingUnavailable(ValueError):
	pass


def _text(value):
	if value is None:
		return ""
	if not isinstance(value, str):
		raise MappingUnavailable("expected_text")
	return value.replace("\r\n", "\n").replace("\r", "\n")


def _price(value):
	if value is None or value == "":
		return None
	if isinstance(value, bool) or not isinstance(value, (str, int, float)):
		raise MappingUnavailable("invalid_purchase_price")
	try:
		number = Decimal(str(value))
		if not number.is_finite():
			raise InvalidOperation
		if not number:
			return "0"
		text = format(number, "f")
		return text.rstrip("0").rstrip(".") if "." in text else text
	except (InvalidOperation, ValueError):
		raise MappingUnavailable("invalid_purchase_price") from None


def _date(value):
	value = _text(value)
	if not value:
		return ""
	try:
		if len(value) == 10:
			return date.fromisoformat(value).isoformat()
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		if parsed.hour or parsed.minute or parsed.second or parsed.microsecond or parsed.utcoffset() not in (None, timedelta(0)):
			raise ValueError
		return parsed.date().isoformat()
	except ValueError:
		raise MappingUnavailable("purchase_date_not_lossless") from None


def _protected_tags(manifest):
	names = {str(tag.get("name", "")).casefold() for tag in manifest.get("tags", []) if isinstance(tag, dict) and tag.get("source") == "system"}
	names.update(name.casefold() for name in {*TYPE_SYNONYMS.values(), "Other"})
	names.add(str(manifest.get("item", {}).get("category") or "").casefold())
	return names


def _tags(tags, protected):
	if not isinstance(tags, list) or any(not isinstance(tag, dict) or not isinstance(tag.get("name"), str) or not tag["name"].strip() for tag in tags):
		raise MappingUnavailable("invalid_tags")
	return sorted({tag["name"].strip().casefold() for tag in tags if tag.get("source") != "system" and tag["name"].casefold() not in protected and not tag["name"].startswith("Type: ")})


def _location(value, locations):
	if value is None or value == "" or value == {"name": None, "path": []}:
		return None
	if isinstance(value, str):
		matches = [entry for entry in locations.values() if entry.get("name") == value]
		if len(matches) != 1:
			raise MappingUnavailable("location_requires_unique_verified_reference")
		return matches[0]["id"]
	if not isinstance(value, dict):
		raise MappingUnavailable("invalid_location")
	if not value.get("id"):
		if not value.get("name") and not value.get("path"):
			return None
		matches = [entry for entry in locations.values() if entry.get("name") == value.get("name") and (not value.get("path") or entry.get("path") == value["path"])]
		if len(matches) != 1:
			raise MappingUnavailable("location_requires_unique_verified_reference")
		return matches[0]["id"]
	if not isinstance(value["id"], str) or value["id"] not in locations:
		raise MappingUnavailable("location_reference_unverified")
	return value["id"]


def _category(value, types):
	matches = [entry["id"] for entry in types.values() if not entry.get("isLocation") and entry.get("name") == value]
	if len(matches) != 1:
		raise MappingUnavailable("category_has_no_lossless_entity_type_mapping")
	return matches[0]


def _notes_record(manifest):
	return {"notes": manifest.get("notes", ""), "identifiers": manifest.get("identifiers", {}), "condition": manifest.get("item", {}).get("condition", [])}


def _values(field, manifest, remote, context):
	local = manifest.get("item", {}).get(field) if field in _ITEM_FIELDS else manifest.get(field)
	if field in _REMOTE_FIELDS:
		key = _REMOTE_FIELDS[field]
		if key not in remote:
			raise MappingUnavailable("homebox_field_missing")
		other = remote[key]
		convert = _price if field == "purchase_price" else _date if field == "purchase_date" else _text
		return convert(local), convert(other)
	if field == "location":
		if "location" not in remote and "locationId" not in remote:
			raise MappingUnavailable("homebox_field_missing")
		other = remote.get("location")
		if other is None and remote.get("locationId"):
			other = {"id": remote["locationId"]}
		return _location(local, context["locations"]), _location(other, context["locations"])
	if field == "category":
		remote_type = remote.get("entityType")
		if not isinstance(remote_type, dict) or remote_type.get("id") not in context["types"] or context["types"][remote_type["id"]].get("isLocation"):
			raise MappingUnavailable("entity_type_unverified")
		return _category(local, context["types"]), remote_type["id"]
	if field == "notes":
		if "notes" not in remote:
			raise MappingUnavailable("homebox_field_missing")
		record = _notes_record(manifest)
		local_notes = _text(local)
		if "<!-- hermes-inventory:metadata:" in local_notes:
			raise homebox.NotesMigrationRequired("Local user notes contain reserved ownership delimiters.")
		return local_notes, _text(homebox.split_notes(remote["notes"], record))
	if field == "tags":
		if "tags" not in remote:
			raise MappingUnavailable("homebox_field_missing")
		protected = _protected_tags(manifest)
		return _tags(manifest.get("tags", []), protected), _tags(remote["tags"], protected)
	raise MappingUnavailable("unsupported_field")


def _baseline(manifest):
	baseline = manifest.get("homebox", {}).get("sync_baseline", {})
	if not isinstance(baseline, dict) or baseline.get("schema") != BASELINE_SCHEMA or baseline.get("version") != BASELINE_VERSION or baseline.get("inventory_id") != manifest["inventory_id"] or baseline.get("entity_id") != manifest.get("homebox", {}).get("entity_id") or not isinstance(baseline.get("fields"), dict):
		return {}
	return baseline["fields"]


def _verified_baseline(manifest, fields):
	return {"schema": BASELINE_SCHEMA, "version": BASELINE_VERSION, "inventory_id": manifest["inventory_id"], "entity_id": manifest["homebox"]["entity_id"], "verified_at": _now(), "fields": fields}


def _reliable_baseline(field, value, context):
	if field == "tags":
		return isinstance(value, list) and all(isinstance(name, str) and name == name.strip().casefold() for name in value) and value == sorted(set(value))
	if field == "location":
		return value is None or isinstance(value, str) and value in context["locations"]
	if field == "category":
		return isinstance(value, str) and value in context["types"]
	try:
		convert = _price if field == "purchase_price" else _date if field == "purchase_date" else _text
		return convert(value) == value
	except MappingUnavailable:
		return False


def _entry(manifest, field, local=None, remote=None, baseline=None, **extra):
	remote_source = {"category": "entityType.id", "location": "location.id", "notes": "notes (user-owned text)", "tags": "tags (user tag names)"}.get(field, _REMOTE_FIELDS.get(field, field))
	return {"asset_id": manifest.get("asset_id"), "inventory_id": manifest["inventory_id"], "entity_id": manifest.get("homebox", {}).get("entity_id"), "field": field, "baseline": baseline, "local": local, "homebox": remote, "sources": {"local": f"Inventory.{'item.' if field in _ITEM_FIELDS else ''}{field}", "homebox": f"Homebox.{remote_source}", "baseline": f"Inventory.homebox.sync_baseline.fields.{field}"}, **extra}


def _asset_collision(target, manifest, context):
	if validate_edit_changes({"asset_id": target}):
		return "asset_id_requires_NNN_NNN_format"
	if any(item["manifest"].get("asset_id") == target and item["manifest"]["inventory_id"] != manifest["inventory_id"] for item in context["local"]):
		return "asset_id_collision_local"
	if any(entity.get("assetId") == target and entity["id"] != manifest["homebox"]["entity_id"] for entity in context["entities"]):
		return "asset_id_collision_homebox"
	if (context["settings"].persistent_data_dir / ".asset-id-reservations" / f"{target}.json").exists():
		return "asset_id_reserved"
	return None


def _plan_item(manifest, remote, context, report):
	baseline = _baseline(manifest)
	actions = []
	for field in _FIELDS:
		try:
			local, other = _values(field, manifest, remote, context)
		except (ValueError, TypeError, KeyError, AttributeError) as exc:
			reason = "notes_migration_required" if isinstance(exc, homebox.NotesMigrationRequired) else str(exc) if isinstance(exc, MappingUnavailable) else "invalid_field_shape"
			known = field in baseline and _reliable_baseline(field, baseline[field], context)
			entry = _entry(manifest, field, baseline=baseline.get(field) if known else None, baseline_known=known, reason=reason)
			if field == "notes":
				entry.update({"local": manifest.get("notes", ""), "homebox": remote.get("notes"), "reason": reason})
				report["conflicts"].append(entry)
			else:
				report["skipped"].append(entry)
			continue
		known = field in baseline and _reliable_baseline(field, baseline[field], context)
		previous = baseline.get(field) if known else None
		entry = _entry(manifest, field, local, other, previous, baseline_known=known)
		if local == other:
			if not known or previous != local:
				actions.append({**entry, "direction": "baseline", "value": local})
			continue
		if not known:
			report["conflicts"].append({**entry, "reason": "baseline_missing_or_unreliable"})
			continue
		if local != previous and other != previous:
			report["conflicts"].append({**entry, "reason": "both_sides_changed"})
			continue
		direction = "import" if local == previous else "export"
		value = other if direction == "import" else local
		reason = _asset_collision(value, manifest, context) if field == "asset_id" else None
		if field == "tags" and any(name not in context["tags"] for name in value):
			reason = "tag_vocabulary_missing_or_ambiguous"
		if reason:
			report["conflicts"].append({**entry, "reason": reason})
		else:
			actions.append({**entry, "direction": direction, "value": value})
	return actions


def _scan(settings, item, *, inventory_id=None):
	report = {"status": "preview", "local_status": "unchanged", "homebox_status": "not_attempted", "planned": [], "applied": [], "conflicts": [], "skipped": [], "backup": None}
	context = {"settings": settings, "local": [], "entities": [], "types": {}, "tags": {}, "locations": {}, "snapshots": {}}
	for path in sorted(settings.items_dir.glob("*/item.json")):
		try:
			if path.is_symlink() or path.parent.is_symlink():
				raise ValueError
			data = path.read_bytes()
			manifest = json.loads(data)
			if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") not in SUPPORTED_ITEM_SCHEMA_VERSIONS or not is_valid_inventory_id(manifest.get("inventory_id")) or manifest["inventory_id"] != path.parent.name or not isinstance(manifest.get("item"), dict) or not isinstance(manifest.get("homebox", {}), dict):
				raise ValueError
			context["local"].append({"path": path, "data": data, "manifest": manifest})
		except (OSError, ValueError, AttributeError):
			report["status"] = "error"
			report["skipped"].append({"reason": "invalid_local_manifest", "inventory_id": path.parent.name})
	if report["status"] == "error":
		return report, context, []
	selected = [entry for entry in context["local"] if entry["manifest"]["inventory_id"] == inventory_id] if inventory_id is not None else [entry for entry in context["local"] if item is None or entry["manifest"].get("asset_id") == item]
	if item is not None and len(selected) != 1:
		report["skipped"].append({"asset_id": item, "reason": "item_not_found_or_ambiguous"})
		return report, context, []
	try:
		context["entities"] = homebox.list_all_entities()
		types = homebox.get_entity_types()
		tags = homebox.list_all_tags()
		if not isinstance(types, list) or not isinstance(tags, list) or any(not isinstance(entry, dict) or not entry.get("id") or not isinstance(entry.get("name"), str) for entry in [*types, *tags]):
			raise ValueError
		context["types"] = {entry["id"]: entry for entry in types}
		for tag in tags:
			name = tag["name"].strip().casefold()
			if sum(other["name"].strip().casefold() == name for other in tags) == 1:
				context["tags"][name] = tag
		for entity in context["entities"]:
			if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
				raise ValueError
			if context["types"].get((entity.get("entityType") or {}).get("id"), {}).get("isLocation"):
				context["locations"][entity["id"]] = entity
	except Exception:
		report.update({"status": "error", "homebox_status": "failed"})
		report["skipped"].append({"reason": "homebox_enumeration_or_catalog_unavailable", "message": "No changes made. Check Homebox connectivity and authentication, then retry /inventory sync."})
		return report, context, []
	linked = {entry["manifest"].get("homebox", {}).get("entity_id") for entry in context["local"]}
	for entity in context["entities"]:
		if item is None and entity["id"] not in linked and entity["id"] not in context["locations"]:
			report["skipped"].append({"entity_id": entity["id"], "asset_id": entity.get("assetId"), "reason": "homebox_only_use_refresh"})
	plans = []
	for entry in selected:
		manifest = entry["manifest"]
		entity_id = manifest.get("homebox", {}).get("entity_id")
		if not entity_id:
			report["skipped"].append(_entry(manifest, "identity", reason="local_only_unlinked_use_refresh"))
			continue
		if sum(candidate["manifest"].get("homebox", {}).get("entity_id") == entity_id for candidate in context["local"]) != 1:
			report["conflicts"].append(_entry(manifest, "identity", reason="multiple_local_records_linked_to_entity"))
			continue
		try:
			remote, etag = homebox.get_entity_snapshot(entity_id)
			if not isinstance(remote, dict) or remote.get("id") != entity_id or not isinstance(remote.get("fields"), list):
				raise ValueError
			identifiers = entity_field_values(remote, "Inventory Item ID")
			if identifiers and identifiers != {manifest["inventory_id"].casefold()}:
				report["conflicts"].append(_entry(manifest, "identity", manifest["inventory_id"], sorted(identifiers), reason="inventory_id_link_mismatch"))
				continue
		except Exception:
			report["skipped"].append(_entry(manifest, "identity", reason="linked_homebox_entity_unavailable"))
			report["homebox_status"] = "failed"
			continue
		context["snapshots"][manifest["inventory_id"]] = {"remote": remote, "etag": etag}
		actions = _plan_item(manifest, remote, context, report)
		baseline = manifest.get("homebox", {}).get("sync_baseline")
		if isinstance(baseline, dict) and (baseline.get("schema") != BASELINE_SCHEMA or baseline.get("version") != BASELINE_VERSION):
			report["skipped"].extend({**action, "reason": "unsupported_sync_baseline_format"} for action in actions)
			actions = []
		if not homebox.is_strong_etag(etag):
			report["skipped"].extend({**action, "reason": "conditional_write_unavailable", "message": "Export was not attempted. The server must provide a strong ETag and enforce If-Match."} for action in actions if action["direction"] == "export")
			actions = [action for action in actions if action["direction"] != "export"]
		if actions:
			report["planned"].extend(actions)
			plans.append({**entry, "remote": remote, "etag": etag, "actions": actions})
	return report, context, plans


def capture_baseline_state(manifest, *, settings):
	"""Capture the existing three-way comparison before a local-to-remote write."""
	try:
		report, context, _ = _scan(settings, None, inventory_id=manifest["inventory_id"])
		snapshot = context["snapshots"].get(manifest["inventory_id"])
		conflicts = [*report["conflicts"], *[action for action in report["planned"] if action["direction"] == "import"]]
		if report["status"] == "error" or snapshot is None and not conflicts:
			return None
		return {"manifest": copy.deepcopy(manifest), "remote": copy.deepcopy(snapshot["remote"]) if snapshot else None, "conflicts": conflicts}
	except Exception:
		return None


def maintain_sync_baseline(manifest, *, settings, before=None, initialize=False):
	"""Advance only agreeing verified fields, without trusting a write response."""
	recovery = "The item remains saved. Bidirectional synchronization tracking could not be fully initialized or updated. Retry /inventory sync --resolve to establish verified equal fields; differing values require review."
	result = {"status": "unavailable", "verified_fields": [], "updated_fields": [], "skipped": [], "message": recovery}
	try:
		report, context, _ = _scan(settings, None, inventory_id=manifest["inventory_id"])
		snapshot = context["snapshots"].get(manifest["inventory_id"])
		entry = next((entry for entry in context["local"] if entry["manifest"]["inventory_id"] == manifest["inventory_id"]), None)
		if report["status"] == "error" or snapshot is None or entry is None or entry["manifest"] != manifest:
			result["skipped"] = [{"field": problem.get("field", "availability"), "reason": problem["reason"]} for problem in [*report["conflicts"], *report["skipped"]] if problem.get("inventory_id") in (None, manifest["inventory_id"]) and "reason" in problem]
			return result
		stored = manifest.get("homebox", {}).get("sync_baseline")
		if isinstance(stored, dict) and (stored.get("schema") != BASELINE_SCHEMA or stored.get("version") != BASELINE_VERSION):
			result["skipped"].append({"reason": "unsupported_sync_baseline_format"})
			return result
		if not initialize and before is None:
			return result
		blocked = set()
		if not initialize:
			previous = before["manifest"]
			if previous["inventory_id"] != manifest["inventory_id"] or previous.get("homebox", {}).get("entity_id") != manifest.get("homebox", {}).get("entity_id"):
				return result
			previous_report = {"conflicts": [], "skipped": []}
			previous_plan = _plan_item(previous, before["remote"], context, previous_report)
			blocked.update(action["field"] for action in previous_plan if action["direction"] == "import")
			blocked.update(entry["field"] for entry in [*previous_report["conflicts"], *previous_report["skipped"]])
		fields = dict(_baseline(manifest))
		for field in _FIELDS:
			try:
				if field in blocked:
					raise MappingUnavailable("preexisting_divergence_or_unverified_field")
				local, remote = _values(field, manifest, snapshot["remote"], context)
				if local != remote:
					raise MappingUnavailable("local_homebox_values_differ")
				result["verified_fields"].append(field)
				if field not in fields or fields[field] != local:
					fields[field] = local
					result["updated_fields"].append(field)
			except (ValueError, TypeError, KeyError, AttributeError) as exc:
				reason = str(exc) if isinstance(exc, MappingUnavailable) else "notes_migration_required" if isinstance(exc, homebox.NotesMigrationRequired) else "invalid_field_shape"
				result["skipped"].append({"field": field, "reason": reason})
		if result["updated_fields"]:
			updated = copy.deepcopy(manifest)
			updated["homebox"]["sync_baseline"] = _verified_baseline(manifest, fields)
			if entry["path"].read_bytes() != entry["data"]:
				return {**result, "status": "unavailable", "updated_fields": []}
			write_manifest(updated, settings=settings)
			manifest["homebox"]["sync_baseline"] = updated["homebox"]["sync_baseline"]
		result["status"] = "partial" if result["skipped"] else "verified"
		if not result["skipped"]:
			result["message"] = "Bidirectional synchronization tracking is verified."
		return result
	except Exception:
		return {**result, "status": "unavailable", "updated_fields": []}


def _import_value(field, value, manifest, context):
	if field == "tags":
		protected = _protected_tags(manifest)
		return [tag for tag in manifest.get("tags", []) if tag.get("source") == "system" or str(tag.get("name", "")).casefold() in protected] + [{"name": context["tags"][name]["name"], "source": "user"} for name in value]
	if field == "category":
		return context["types"][value]["name"]
	if field == "location" and value is not None:
		location = context["locations"][value]
		return {"id": value, "name": location["name"], "path": location.get("path", [])}
	return value


def _export_values(actions, manifest, remote, context):
	payload = {}
	for action in actions:
		if action["direction"] != "export":
			continue
		field, value = action["field"], action["value"]
		if field in _REMOTE_FIELDS:
			payload[_REMOTE_FIELDS[field]] = manifest.get("purchase_price") if field == "purchase_price" and value is not None else value
		elif field == "category":
			payload["entityTypeId"] = value
		elif field == "location":
			payload["locationId"] = value
		elif field == "tags":
			protected = _protected_tags(manifest)
			payload["tagIds"] = list(dict.fromkeys([tag["id"] for tag in remote["tags"] if tag["name"].casefold() in protected or tag["name"].startswith("Type: ")] + [context["tags"][name]["id"] for name in value]))
		elif field == "notes":
			record = _notes_record(manifest)
			payload["notes"] = homebox.build_notes(record, user_notes=value)
	return payload


def _apply_item(plan, context, report):
	manifest = copy.deepcopy(plan["manifest"])
	entity_id = manifest["homebox"]["entity_id"]
	actions = plan["actions"]
	reservation = None
	remote_applied = []
	committing = False
	try:
		remote, etag = homebox.get_entity_snapshot(entity_id)
		if remote != plan["remote"] or etag != plan["etag"] or plan["path"].read_bytes() != plan["data"]:
			raise homebox.SyncWriteBlocked("state_changed_since_preview")
		for action in actions:
			if action["field"] == "asset_id" and action["direction"] != "baseline":
				used = [entity.get("assetId") for entity in homebox.list_all_entities() if entity.get("id") != entity_id]
				reservation = reserve_asset_id(context["settings"], action["value"], used, exclude_inventory_id=manifest["inventory_id"])
		atomic_json_write(plan["path"].parent / "history" / f"sync-before-{uuid.uuid4().hex}.json", {"operation": "sync", "timestamp": _now(), "homebox": remote, "inventory": plan["manifest"]})
		payload = _export_values(actions, manifest, remote, context)
		write_failed = False
		blocked_reason = None
		if payload:
			try:
				homebox.put_entity_fields(entity_id, payload, expected=remote, etag=etag)
			except homebox.SyncWriteBlocked as exc:
				if str(exc) != "conditional_write_unavailable":
					raise
				blocked_reason = "conditional_write_unavailable"
			except Exception:
				write_failed = True
		try:
			verified, _ = homebox.get_entity_snapshot(entity_id)
		except Exception:
			verified = None
		if verified is not None and (verified.get("id") != entity_id or entity_field_values(verified, "Inventory Item ID") not in (set(), {manifest["inventory_id"].casefold()})):
			verified = None
		fields = dict(_baseline(manifest))
		applied, failures = [], []
		for action in actions:
			field, value = action["field"], action["value"]
			try:
				if blocked_reason and action["direction"] == "export":
					raise MappingUnavailable(blocked_reason)
				if verified is None:
					raise MappingUnavailable("remote_verification_unavailable")
				_, actual = _values(field, plan["manifest"], verified, context)
				if actual != value:
					raise MappingUnavailable("remote_verification_mismatch")
				if action["direction"] == "import":
					incoming = _import_value(field, value, manifest, context)
					if field != "tags" and validate_edit_changes({field: incoming}):
						raise MappingUnavailable("import_does_not_match_local_schema")
					if field in _ITEM_FIELDS:
						manifest["item"][field] = incoming
					else:
						manifest[field] = incoming
					manifest.setdefault("field_sources", {})[field] = "user"
				if field == "category":
					manifest["homebox"]["entity_type"] = value
				if field == "asset_id":
					manifest["homebox"]["asset_id"] = value
				fields[field] = value
				applied.append(action)
				if action["direction"] == "export":
					remote_applied.append(action)
			except (MappingUnavailable, homebox.NotesMigrationRequired) as exc:
				failures.append({**action, "reason": str(exc) if isinstance(exc, MappingUnavailable) else "notes_migration_required"})
		if plan["path"].read_bytes() != plan["data"]:
			raise homebox.SyncWriteBlocked("local_changed_during_apply")
		if applied:
			manifest["homebox"]["sync_baseline"] = _verified_baseline(manifest, fields)
			manifest["updated_at"] = _now()
			manifest.setdefault("history", []).append({"timestamp": _now(), "operation": "sync", "source": "homebox", "changes": [{"field": action["field"], "direction": action["direction"], "value": action["value"]} for action in applied]})
		if failures and payload:
			manifest["status"] = "pending_homebox_sync"
			manifest["error"] = "Changes remain saved locally. Homebox field synchronization is incomplete; correct the reported issue and retry /inventory sync."
		if applied or failures and payload:
			committing = True
			write_manifest(manifest, settings=context["settings"])
			persisted = json.loads(plan["path"].read_bytes())
			if persisted != manifest:
				raise homebox.SyncWriteBlocked("local_verification_failed")
			report["applied"].extend({**action, "baseline_updated": True} for action in applied)
			if report["local_status"] != "failed":
				report["local_status"] = "saved"
			if applied and report["homebox_status"] != "failed":
				report["homebox_status"] = "synced"
		if failures:
			report["skipped"].extend(failures)
			report["homebox_status"] = "failed"
		if write_failed and not failures:
			report.setdefault("warnings", []).append({"inventory_id": manifest["inventory_id"], "reason": "write_response_failed_but_values_verified"})
	except Exception as exc:
		reason = str(exc) if isinstance(exc, homebox.SyncWriteBlocked) else "apply_failed_no_baseline_advanced"
		report["skipped"].extend({**action, "reason": reason} for action in actions)
		if remote_applied:
			report["applied"].extend({**action, "baseline_updated": False} for action in remote_applied)
			if report["homebox_status"] != "failed":
				report["homebox_status"] = "synced" if len(remote_applied) == sum(action["direction"] == "export" for action in actions) else "failed"
		else:
			report["homebox_status"] = "failed"
		if committing or remote_applied:
			report["local_status"] = "failed"
	finally:
		if reservation:
			release_asset_id_reservation(context["settings"], *reservation)


def sync_inventory(*, settings, resolve=False, item=None, conflicts_only=False):
	"""Preview without writes, or apply a freshly revalidated nonconflicting plan."""
	if conflicts_only and resolve:
		return {"status": "error", "error": "--conflicts is read-only and cannot be combined with --resolve."}
	if resolve and not _SYNC_LOCK.acquire(blocking=False):
		return {"status": "error", "error": "An Inventory synchronization is already in progress."}
	try:
		report, context, plans = _scan(settings, item)
		if not resolve or report["status"] == "error":
			if conflicts_only:
				report["planned"] = []
			return report
		if not plans:
			report["status"] = "error" if report["homebox_status"] == "failed" else "conflicts" if report["conflicts"] else "blocked" if report["skipped"] else "unchanged"
			return report
		try:
			backup = create_backup(settings)
			if verify_backup(Path(backup["path"]), settings).get("status") != "PASS":
				raise ValueError
			report["backup"] = backup["path"]
		except Exception:
			report["status"] = "error"
			report["skipped"].append({"reason": "backup_failed_or_unverified", "message": "No synchronization changes applied. Check backup storage and retry."})
			return report
		for plan in plans:
			_apply_item(plan, context, report)
		if report["local_status"] == "saved" or any(action.get("baseline_updated") for action in report["applied"]):
			try:
				write_catalog(settings)
			except Exception:
				report["skipped"].append({"reason": "catalog_refresh_failed", "message": "Item changes are durable; retry /inventory refresh."})
		report["status"] = "partial_success" if (report["applied"] or report["local_status"] == "saved") and (report["conflicts"] or report["skipped"]) else "updated" if report["applied"] else "error" if report["homebox_status"] == "failed" or report["local_status"] == "failed" else "conflicts"
		return report
	finally:
		if resolve:
			_SYNC_LOCK.release()