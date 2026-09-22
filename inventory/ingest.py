"""End-to-end inventory ingestion orchestration."""

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import get_settings, storage_health
from inventory.constants import OBSERVATION_SCHEMA, PLUGIN_VERSION, SCHEMA_VERSION
from inventory.duplicates import check_homebox_duplicates
from inventory.homebox import complete_entity, create_entity
from inventory.image_identity import canonical_image_index, incoming_images
from inventory.media import is_supported_image
from inventory.normalize import normalize_record
from inventory.storage import (
	abandon_item_transaction, atomic_json_write, begin_item_transaction,
	build_manifest, canonicalize_images, commit_item_transaction, load_manifest, release_asset_id_reservation, reserve_asset_id, write_catalog, write_manifest,
)
from inventory.vision import analyze_directory


class VisionParseError(RuntimeError):
	"""Structured vision response could not be parsed as JSON."""


def generate_item_id():
	return f"INV-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def source_images(directory):
	return sorted(path for path in Path(directory).iterdir() if path.is_file() and is_supported_image(path))


def prepare_originals(source_directory, destination, images=None):
	source = Path(source_directory).resolve()
	if not source.is_dir():
		raise ValueError(f"Expected source directory: {source}")
	images = images if images is not None else source_images(source)
	if not images:
		raise ValueError("No supported images found")
	provenance_path = source / ".inventory-provenance.json"
	try:
		provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
		if not isinstance(provenance, dict): provenance = {}
	except (OSError, json.JSONDecodeError):
		provenance = {}
	for image in images:
		image = Path(image)
		shutil.copy2(image, destination / image.name)
	return provenance


def run_vision(item_directory, vision_client, *, metadata_path=None):
	raw, metadata_path = analyze_directory(item_directory, vision_client, metadata_path=metadata_path)
	if raw.get("parse_status") != "json_ok":
		info = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
		audit = info.get("audit") if isinstance(info.get("audit"), dict) else {}
		error = VisionParseError("Vision model did not return valid structured JSON")
		error.debug = {"error_stage": "vision_json_parse", "provider": info.get("provider", ""), "model": info.get("model", ""), "content_type": audit.get("content_type", ""), "metadata_path": str(metadata_path), "input_image_count": len(raw.get("source_images", [])), "input_image_filenames": raw.get("source_images", []), "raw_response_preview": str(raw.get("result", {}).get("raw_model_output", ""))[:1000]}
		raise error
	return raw, metadata_path


def save_receipt(item_id, data, settings):
	path = settings.persistent_data_dir / "receipts" / f"{item_id}.json"
	atomic_json_write(path, data)
	return path


def save_duplicate_observation(item_id, record, raw, duplicate_result, image_dir, settings):
	path = settings.persistent_data_dir / "observations" / f"{item_id}.json"
	atomic_json_write(path, {"schema": OBSERVATION_SCHEMA, "schema_version": SCHEMA_VERSION, "observation_id": item_id, "plugin_version": PLUGIN_VERSION, "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "record": record, "duplicate_check": duplicate_result, "source_images": [image.name for image in image_dir.iterdir() if image.is_file()], "vision": raw})
	return path


def ingest(source_directory, vision_client, *, settings=None):
	settings = settings or get_settings()
	available, reason = storage_health(settings.persistent_data_dir)
	if not available:
		raise RuntimeError(f"Persistent inventory storage is unavailable; HomeBox was not changed: {reason}")
	incoming = incoming_images(source_directory)
	if not incoming:
		raise ValueError("No supported images found")
	index, diagnostics = canonical_image_index(settings)
	if diagnostics["checksum_mismatches"]:
		raise RuntimeError("Canonical image checksum mismatches must be repaired before ingesting image evidence.")
	incoming_hashes = {entry["sha256"] for entry in incoming}
	matched = {digest: index[digest] for digest in incoming_hashes if digest in index}
	matched_items = {entry["inventory_id"] for entries in matched.values() for entry in entries}
	if matched:
		if len(matched_items) > 1:
			return {"status": "exact_image_conflict", "created": False, "durable": False, "classification": "EXACT_IMAGE_CONFLICT", "inventory_ids": sorted(matched_items), "hashes": sorted(matched)}
		existing = next(iter(matched_items))
		if len(matched) == len(incoming_hashes):
			result = {"status": "exact_image_duplicate", "created": False, "durable": True, "classification": "EXACT_IMAGE_DUPLICATE", "inventory_id": existing, "hashes": sorted(incoming_hashes)}
			result["receipt_path"] = str(save_receipt(f"observation-{uuid.uuid4().hex}", result, settings))
			return result
		return {"status": "existing_item_with_new_image_evidence", "created": False, "durable": False, "inventory_id": existing, "existing_hashes": sorted(matched), "new_hashes": sorted(incoming_hashes - set(matched))}
	item_id = generate_item_id()
	transaction = begin_item_transaction(item_id, settings)
	images_dir = transaction / "images"
	reservation = None
	try:
		provenance = prepare_originals(source_directory, images_dir, [entry["path"] for entry in incoming])
		raw, metadata_path = run_vision(images_dir, vision_client, metadata_path=transaction / "vision.json")
		raw["item_id"] = item_id
		atomic_json_write(metadata_path, raw)
		record = normalize_record(raw)
		record["source_filenames"] = provenance
		verified_hashes = {entry["path"].name: entry["sha256"] for entry in incoming}
		record["image_hashes"] = [
			{"filename": filename, "sha256": verified_hashes[filename]}
			for filename in record.get("source_images", []) if filename in verified_hashes
		]
		duplicate_result = check_homebox_duplicates(record)
		if duplicate_result["classification"] != "NEW_ITEM":
			save_duplicate_observation(item_id, record, raw, duplicate_result, images_dir, settings)
			abandon_item_transaction(transaction)
			result = {"status": "duplicate_candidate", "created": False, "durable": True, "item_id": item_id, "classification": duplicate_result["classification"], "duplicate_check": duplicate_result}
			result["receipt_path"] = str(save_receipt(item_id, result, settings))
			return result
		try:
			from inventory.homebox import list_all_entities
			entities = list_all_entities()
		except Exception as exc:
			raise RuntimeError("Cannot allocate a globally safe Asset ID because HomeBox state could not be completely enumerated.") from exc
		reservation = reserve_asset_id(settings, external_used_ids=[entity.get("assetId") for entity in entities if isinstance(entity, dict)])
		record["asset_id"] = reservation[0]
		record["field_sources"] = {key: "vision" for key in ("name", "description", "category", "manufacturer", "condition", "identifiers", "attributes", "image_roles")}
		if any(str(entity.get("assetId", "")) == record["asset_id"] for entity in entities if isinstance(entity, dict)):
			raise RuntimeError(f"Asset ID is already in use in HomeBox: {record['asset_id']}")
		canonicalize_images(record, images_dir)
		initial = build_manifest(item_id, record, raw, images_dir, status="pending_homebox_create")
		write_manifest(initial, transaction / "item.json", settings)
		final_item_root = commit_item_transaction(transaction, item_id, settings)
		final_images_dir = final_item_root / "images"
		record["source_directory"] = str(final_images_dir)
		raw["source_directory"] = str(final_images_dir)
		atomic_json_write(final_item_root / "vision.json", raw)
		previous_manifest = load_manifest(item_id, settings) or initial
		entity_id = None
		created = {}
		attachment_sync = {}
		try:
			created = create_entity(record)
			entity_id = created.get("id")
			if not entity_id:
				raise RuntimeError("HomeBox returned no entity ID")
			previous_manifest = build_manifest(item_id, record, raw, final_images_dir, status="pending_homebox_completion", homebox={"entity_id": entity_id, "asset_id": created.get("assetId"), "collection_id": created.get("groupId"), "entity_type": created.get("entityTypeId"), "last_synced_at": None, "attachment_sync": attachment_sync}, previous=previous_manifest)
			write_manifest(previous_manifest, settings=settings)
			def persist_attachment(attachment):
				nonlocal previous_manifest
				digest = attachment.get("sha256")
				if digest:
					attachment_sync[digest] = attachment
				previous_manifest = build_manifest(item_id, record, raw, final_images_dir, status="pending_homebox_completion", homebox={"entity_id": entity_id, "asset_id": created.get("assetId"), "collection_id": created.get("groupId"), "entity_type": created.get("entityTypeId"), "last_synced_at": None, "attachment_sync": attachment_sync}, previous=previous_manifest)
				write_manifest(previous_manifest, settings=settings)
			completed = complete_entity(entity_id, record, image_directory=final_images_dir, attachment_sync=attachment_sync, on_attachment_uploaded=persist_attachment)
		except Exception as exc:
			write_manifest(build_manifest(item_id, record, raw, final_images_dir, status="pending_homebox_sync", homebox={"entity_id": entity_id, "asset_id": created.get("assetId"), "collection_id": created.get("groupId"), "entity_type": created.get("entityTypeId"), "last_synced_at": None, "attachment_sync": attachment_sync}, error=str(exc), previous=previous_manifest), settings=settings)
			write_catalog(settings)
			result = {"status": "pending_homebox_sync", "created": False, "durable": True, "item_id": item_id, "homebox_entity_id": entity_id, "error": str(exc)}
			result["receipt_path"] = str(save_receipt(item_id, result, settings))
			return result
		result = {"status": "created", "created": True, "durable": True, "item_id": item_id, "inventory_id": item_id, "homebox_entity_id": entity_id, "asset_id": record["asset_id"], "name": record.get("name"), "category": record.get("category"), "manufacturer": record.get("manufacturer"), "duplicate_check": duplicate_result, "attachments": completed.get("attachments", [])}
		homebox_entity = completed.get("entity", {})
		write_manifest(build_manifest(item_id, record, raw, final_images_dir, status="synced", homebox={"entity_id": entity_id, "asset_id": result["asset_id"], "collection_id": homebox_entity.get("groupId"), "entity_type": homebox_entity.get("entityType", {}).get("id") or created.get("entityTypeId"), "last_synced_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "fields": homebox_entity.get("fields"), "tags": homebox_entity.get("tags"), "location": homebox_entity.get("location"), "attachments": completed.get("attachments", []), "attachment_sync": attachment_sync}, previous=previous_manifest), settings=settings)
		write_catalog(settings)
		result["receipt_path"] = str(save_receipt(item_id, result, settings))
		return result
	except Exception:
		# Leave the same-filesystem transaction directory for non-destructive
		# recovery inspection; it has no completion marker or item manifest path.
		raise
	finally:
		if reservation is not None:
			release_asset_id_reservation(settings, reservation[0], reservation[1])