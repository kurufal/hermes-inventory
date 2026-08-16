"""HomeBox entity, field, and attachment integration."""

from pathlib import Path

import requests

from inventory.config import (
	HOMEBOX_API_KEY,
	HOMEBOX_ATTACHMENT_TIMEOUT_SECONDS,
	HOMEBOX_TIMEOUT_SECONDS,
	HOMEBOX_URL,
)
from inventory.fields import build_homebox_fields


def _base_url():
	if not HOMEBOX_URL:
		raise RuntimeError("HOMEBOX_URL is not set")

	return HOMEBOX_URL


def auth_headers():
	if not HOMEBOX_API_KEY:
		raise RuntimeError(
			"HOMEBOX_API_KEY is not set"
		)

	return {
		"Authorization": f"Bearer {HOMEBOX_API_KEY}",
	}


def json_headers():
	headers = auth_headers()
	headers["Content-Type"] = "application/json"
	return headers


def get_entity_types():
	response = requests.get(
		f"{_base_url()}/api/v1/entity-types",
		headers=auth_headers(),
		timeout=HOMEBOX_TIMEOUT_SECONDS,
	)

	response.raise_for_status()
	return response.json()


def get_item_entity_type():
	for entity_type in get_entity_types():
		if (
			entity_type.get("name") == "Item"
			and not entity_type.get("isLocation")
		):
			return entity_type

	raise RuntimeError(
		"HomeBox Item entity type not found"
	)


def list_entities():
	response = requests.get(
		f"{_base_url()}/api/v1/entities",
		headers=auth_headers(),
		timeout=HOMEBOX_TIMEOUT_SECONDS,
	)

	response.raise_for_status()
	return response.json()


def get_entity(entity_id):
	response = requests.get(
		f"{_base_url()}/api/v1/entities/{entity_id}",
		headers=auth_headers(),
		timeout=HOMEBOX_TIMEOUT_SECONDS,
	)

	response.raise_for_status()
	return response.json()


def build_notes(record):
	sections = []

	identifiers = record.get(
		"identifiers",
		{},
	)

	identifier_lines = []

	for key, values in identifiers.items():
		if not isinstance(values, list):
			continue

		for value in values:
			identifier_lines.append(
				f"{key}: {value}"
			)

	if identifier_lines:
		sections.append(
			"Identifiers\n"
			+ "\n".join(identifier_lines)
		)

	condition = record.get(
		"condition",
		[],
	)

	condition_lines = []

	for entry in condition:
		observation = entry.get(
			"observation",
			"",
		)

		confidence = entry.get(
			"confidence",
		)

		if not observation:
			continue

		try:
			confidence_text = f"{float(confidence):.2f}"
		except (TypeError, ValueError):
			confidence_text = ""

		if not confidence_text:
			condition_lines.append(
				f"- {observation}"
			)
		else:
			condition_lines.append(
				f"- {observation} "
				f"(confidence: {confidence_text})"
			)

	if condition_lines:
		sections.append(
			"Condition observations\n"
			+ "\n".join(condition_lines)
		)

	sections.append(
		"Inventory metadata generated from "
		"local image analysis."
	)

	return "\n\n".join(sections)


def create_entity(record):
	entity_type = get_item_entity_type()

	payload = {
		"name": record.get(
			"name",
			"Unnamed Item",
		),
		"description": record.get(
			"physical_description",
			"",
		),
		"entityTypeId": entity_type["id"],
		"quantity": 1,
	}

	response = requests.post(
		f"{_base_url()}/api/v1/entities",
		headers=json_headers(),
		json=payload,
		timeout=HOMEBOX_TIMEOUT_SECONDS,
	)

	if not response.ok:
		raise RuntimeError(
			f"HomeBox create failed: "
			f"{response.status_code} "
			f"{response.text}"
		)

	return response.json()


def update_entity(entity_id, record):
	current = get_entity(entity_id)

	identifiers = record.get(
		"identifiers",
		{},
	)

	model_numbers = identifiers.get(
		"model_number",
		[],
	)

	serial_numbers = identifiers.get(
		"serial_number",
		[],
	)

	payload = {
		"id": entity_id,
		"name": record.get(
			"name",
			current.get("name", "Unnamed Item"),
		),
		"description": record.get(
			"physical_description",
			current.get("description", ""),
		),
		"entityTypeId": current[
			"entityType"
		]["id"],
		"quantity": current.get(
			"quantity",
			1,
		),
		"assetId": current.get(
			"assetId",
			"",
		),
		"purchasePrice": current.get(
			"purchasePrice",
			0,
		),
		"purchaseDate": current.get(
			"purchaseDate",
			"",
		),
		"purchaseFrom": current.get(
			"purchaseFrom",
			"",
		),
		"warrantyExpires": current.get(
			"warrantyExpires",
			"",
		),
		"warrantyDetails": current.get(
			"warrantyDetails",
			"",
		),
		"soldDate": current.get(
			"soldDate",
			"",
		),
		"soldTo": current.get(
			"soldTo",
			"",
		),
		"soldPrice": current.get(
			"soldPrice",
			0,
		),
		"soldNotes": current.get(
			"soldNotes",
			"",
		),
		"manufacturer": record.get(
			"manufacturer",
			"",
		),
		"modelNumber": (
			model_numbers[0]
			if model_numbers
			else current.get(
				"modelNumber",
				"",
			)
		),
		"serialNumber": (
			serial_numbers[0]
			if serial_numbers
			else current.get(
				"serialNumber",
				"",
			)
		),
		"notes": build_notes(record),
		"insured": current.get(
			"insured",
			False,
		),
		"archived": current.get(
			"archived",
			False,
		),
		"lifetimeWarranty": current.get(
			"lifetimeWarranty",
			False,
		),
		"syncChildEntityLocations": (
			current.get(
				"syncChildEntityLocations",
				False,
			)
		),
		"tagIds": [
			tag["id"]
			for tag in current.get(
				"tags",
				[],
			)
			if "id" in tag
		],
		"fields": build_homebox_fields(
			record,
			current.get(
				"fields",
				[],
			),
		),
	}

	response = requests.put(
		f"{_base_url()}/api/v1/entities/{entity_id}",
		headers=json_headers(),
		json=payload,
		timeout=HOMEBOX_TIMEOUT_SECONDS,
	)

	if not response.ok:
		raise RuntimeError(
			f"HomeBox update failed: "
			f"{response.status_code} "
			f"{response.text}"
		)

	return response.json()


def upload_attachment(
	entity_id,
	path,
	primary=False,
):
	path = Path(path)

	if not path.exists():
		raise FileNotFoundError(
			str(path)
		)

	with path.open("rb") as f:
		files = {
			"file": (
				path.name,
				f,
			)
		}

		data = {
			"name": path.name,
			"primary": (
				"true"
				if primary
				else "false"
			),
		}

		response = requests.post(
			(
				f"{_base_url()}/api/v1/entities/"
				f"{entity_id}/attachments"
			),
			headers=auth_headers(),
			files=files,
			data=data,
			timeout=HOMEBOX_ATTACHMENT_TIMEOUT_SECONDS,
		)

	if not response.ok:
		raise RuntimeError(
			f"Attachment upload failed for "
			f"{path.name}: "
			f"{response.status_code} "
			f"{response.text}"
		)

	if response.content:
		try:
			return response.json()
		except ValueError:
			return {
				"status_code": response.status_code,
				"body": response.text,
			}

	return {
		"status_code": response.status_code,
	}


def choose_primary_image(record):
	source_images = record.get(
		"source_images",
		[],
	)

	if not source_images:
		return None

	preferred_terms = (
		"front cover",
		"front view",
		"front",
		"primary",
		"overview",
		"overall",
		"exterior",
		"main",
	)

	roles = record.get(
		"image_roles",
		[],
	)

	candidates = []

	for role in roles:
		filename = role.get(
			"filename",
			"",
		)

		inferred_role = str(
			role.get(
				"inferred_role",
				"",
			)
		).lower()

		try:
			confidence = float(
				role.get(
					"confidence",
					0.0,
				)
				or 0.0
			)
		except (TypeError, ValueError):
			confidence = 0.0

		if filename not in source_images:
			continue

		score = 0

		for index, term in enumerate(
			preferred_terms
		):
			if term in inferred_role:
				score = (
					100 - index
				)
				break

		candidates.append(
			(
				score,
				confidence,
				filename,
			)
		)

	if candidates:
		candidates.sort(
			reverse=True
		)

		if candidates[0][0] > 0:
			return candidates[0][2]

	return source_images[0]


def complete_entity(
	entity_id,
	record,
):
	updated = update_entity(
		entity_id,
		record,
	)

	source_directory = Path(
		record.get(
			"source_directory",
			"",
		)
	)

	source_images = record.get(
		"source_images",
		[],
	)

	attachments = []

	primary_filename = choose_primary_image(
		record
	)

	for filename in source_images:
		image_path = (
			source_directory
			/ filename
		)

		is_primary = (
			filename == primary_filename
		)

		result = upload_attachment(
			entity_id,
			image_path,
			primary=is_primary,
		)

		attachments.append({
			"filename": filename,
			"primary": is_primary,
			"result": result,
		})

	return {
		"entity": updated,
		"attachments": attachments,
	}
