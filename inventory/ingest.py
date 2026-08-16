"""End-to-end inventory ingestion orchestration."""

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import (
	METADATA_DIR,
	ORIGINALS_DIR,
	RECEIPTS_DIR,
)
from inventory.duplicates import check_homebox_duplicates
from inventory.homebox import create_entity, complete_entity
from inventory.normalize import normalize_record
from inventory.vision import analyze_directory


class VisionParseError(RuntimeError):
	"""Raised when the vision backend does not return parseable structured JSON.

	Carries a ``.debug`` dict with non-secret diagnostic context (provider,
	model, source image names, a bounded raw response preview, and the
	metadata file path) so the caller can report something more useful than
	a bare "vision failed" message, and so the chat model is not left to
	invent unfounded explanations such as poor image quality.
	"""


SUPPORTED_IMAGES = {
	".jpg",
	".jpeg",
	".png",
	".webp",
}


def generate_item_id():
	timestamp = datetime.now(
		UTC
	).strftime(
		"%Y%m%d-%H%M%S"
	)

	suffix = (
		uuid.uuid4()
		.hex[:8]
	)

	return (
		f"INV-{timestamp}-{suffix}"
	)


def source_images(directory):
	return sorted(
		path
		for path in directory.iterdir()
		if (
			path.is_file()
			and path.suffix.lower()
			in SUPPORTED_IMAGES
		)
	)


def prepare_originals(
	source_directory,
	item_id,
):
	source_directory = Path(
		source_directory
	).resolve()

	if not source_directory.exists():
		raise FileNotFoundError(
			f"Source directory does not exist: "
			f"{source_directory}"
		)

	if not source_directory.is_dir():
		raise ValueError(
			f"Expected directory: "
			f"{source_directory}"
		)

	images = source_images(
		source_directory
	)

	if not images:
		raise ValueError(
			"No supported images found"
		)

	destination = (
		ORIGINALS_DIR
		/ item_id
	)

	destination.mkdir(
		parents=True,
		exist_ok=False,
	)

	for image in images:
		shutil.copy2(
			image,
			destination / image.name,
		)

	return destination


def run_vision(item_directory, vision_client):
	raw, metadata_path = analyze_directory(
		item_directory,
		vision_client,
	)

	if not metadata_path.exists():
		raise RuntimeError(
			"Vision extractor completed but "
			"metadata file was not created: "
			f"{metadata_path}"
		)

	if (
		raw.get("parse_status")
		!= "json_ok"
	):
		llm_info = raw.get("llm")
		if not isinstance(llm_info, dict):
			llm_info = {}
		audit = llm_info.get("audit")
		if not isinstance(audit, dict):
			audit = {}
		result_payload = raw.get("result")
		if not isinstance(result_payload, dict):
			result_payload = {}
		source_images = raw.get("source_images")
		if not isinstance(source_images, list):
			source_images = []

		error = VisionParseError(
			"Vision model did not return valid structured JSON"
		)
		error.debug = {
			"error_stage": "vision_json_parse",
			"provider": llm_info.get("provider", ""),
			"model": llm_info.get("model", ""),
			"content_type": audit.get("content_type", ""),
			"metadata_path": str(metadata_path),
			"input_image_count": len(source_images),
			"input_image_filenames": list(source_images),
			"raw_response_preview": str(
				result_payload.get("raw_model_output", "")
			)[:1000],
		}
		raise error

	return (
		raw,
		metadata_path,
	)


def save_receipt(
	item_id,
	data,
):
	RECEIPTS_DIR.mkdir(
		parents=True,
		exist_ok=True,
	)

	path = (
		RECEIPTS_DIR
		/ f"{item_id}.json"
	)

	path.write_text(
		json.dumps(
			data,
			indent=2,
			ensure_ascii=False,
		),
		encoding="utf-8",
	)

	return path


def ingest(
	source_directory,
	vision_client,
):
	ORIGINALS_DIR.mkdir(
		parents=True,
		exist_ok=True,
	)

	METADATA_DIR.mkdir(
		parents=True,
		exist_ok=True,
	)

	item_id = generate_item_id()

	original_directory = (
		prepare_originals(
			source_directory,
			item_id,
		)
	)

	raw, metadata_path = (
		run_vision(
			original_directory,
			vision_client,
		)
	)

	record = normalize_record(
		raw
	)

	duplicate_result = (
		check_homebox_duplicates(
			record
		)
	)

	if (
		duplicate_result[
			"classification"
		]
		!= "NEW_ITEM"
	):
		result = {
			"status": (
				"duplicate_candidate"
			),
			"created": False,
			"item_id": item_id,
			"classification": (
				duplicate_result[
					"classification"
				]
			),
			"source_directory": str(
				original_directory
			),
			"metadata_path": str(
				metadata_path
			),
			"duplicate_check": (
				duplicate_result
			),
		}

		receipt = save_receipt(
			item_id,
			result,
		)

		result[
			"receipt_path"
		] = str(receipt)

		return result

	created = create_entity(
		record
	)

	entity_id = created.get(
		"id"
	)

	if not entity_id:
		raise RuntimeError(
			"HomeBox returned no entity ID"
		)

	try:
		completed = complete_entity(
			entity_id,
			record,
		)

	except Exception as exc:
		result = {
			"status": (
				"homebox_partial_failure"
			),
			"created": True,
			"item_id": item_id,
			"homebox_entity_id": (
				entity_id
			),
			"source_directory": str(
				original_directory
			),
			"metadata_path": str(
				metadata_path
			),
			"error": str(exc),
		}

		receipt = save_receipt(
			item_id,
			result,
		)

		result[
			"receipt_path"
		] = str(receipt)

		raise RuntimeError(
			json.dumps(
				result,
				indent=2,
			)
		) from exc

	result = {
		"status": "created",
		"created": True,
		"item_id": item_id,
		"homebox_entity_id": (
			entity_id
		),
		"asset_id": (
			completed
			.get(
				"entity",
				{},
			)
			.get(
				"assetId"
			)
		),
		"name": (
			record.get(
				"name"
			)
		),
		"category": (
			record.get(
				"category"
			)
		),
		"manufacturer": (
			record.get(
				"manufacturer"
			)
		),
		"source_directory": str(
			original_directory
		),
		"metadata_path": str(
			metadata_path
		),
		"duplicate_check": (
			duplicate_result
		),
		"attachments": (
			completed.get(
				"attachments",
				[],
			)
		),
	}

	receipt = save_receipt(
		item_id,
		result,
	)

	result[
		"receipt_path"
	] = str(receipt)

	return result
