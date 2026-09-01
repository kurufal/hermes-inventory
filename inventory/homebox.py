"""HomeBox entity, field, and attachment integration."""

from pathlib import Path

import requests

from inventory.config import HOMEBOX_ATTACHMENT_TIMEOUT_SECONDS, HOMEBOX_TIMEOUT_SECONDS, homebox_api_key, homebox_url
from inventory.fields import build_homebox_fields


def _base_url():
	url = homebox_url()
	if not url:
		raise RuntimeError("HOMEBOX_URL is not set")
	return url


def auth_headers():
	api_key = homebox_api_key()
	if not api_key:
		raise RuntimeError(
			"HOMEBOX_API_KEY is not set"
		)

	return {
		"Authorization": f"Bearer {api_key}",
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


def list_tags():
	response = requests.get(f"{_base_url()}/api/v1/tags", headers=auth_headers(), timeout=HOMEBOX_TIMEOUT_SECONDS)
	response.raise_for_status()
	payload = response.json()
	return payload.get("items", []) if isinstance(payload, dict) else payload


def synchronized_tag_ids(current_tags, inventory_tags, managed_names=(), category=""):
	"""Match Inventory tags to existing HomeBox tags without creating any."""
	available = list_tags()
	by_name = {str(tag.get("name", "")).casefold(): tag for tag in available if isinstance(tag, dict) and tag.get("id")}
	desired = [str(category).strip()] if category else []
	desired.extend(str(tag.get("name", "")).strip() for tag in inventory_tags if isinstance(tag, dict) and tag.get("source") == "user")
	managed_names = {str(name).casefold() for name in managed_names}
	managed_names.update(name.casefold() for name in desired)
	retained = [tag for tag in current_tags if isinstance(tag, dict) and tag.get("id") and str(tag.get("name", "")).casefold() not in managed_names]
	for name in dict.fromkeys(desired):
		if not name:
			continue
		entry = by_name.get(name.casefold())
		if entry is not None:
			retained.append(entry)
	return list(dict.fromkeys(str(tag["id"]) for tag in retained))


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

	user_notes = str(record.get("notes", "")).strip()
	if user_notes:
		sections.append("User notes\n" + user_notes)

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
		"assetId": record.get("asset_id", ""),
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
		"assetId": record.get("asset_id", current.get("assetId", "")),
		"purchasePrice": record.get("purchase_price", current.get(
			"purchasePrice",
			0,
		)),
		"purchaseDate": record.get("purchase_date", current.get(
			"purchaseDate",
			"",
		)),
		"purchaseFrom": record.get("purchase_from", current.get(
			"purchaseFrom",
			"",
		)),
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
		"tagIds": synchronized_tag_ids(current.get("tags", []), record.get("tags", []), record.get("managed_tag_names", []), record.get("category", "")),
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

		data = {"name": path.name}

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


def complete_entity(
	entity_id,
	record,
	*,
	image_directory=None,
	upload_attachments=True,
):
	updated = update_entity(
		entity_id,
		record,
	)

	source_directory = Path(image_directory) if image_directory is not None else Path(
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
	if not upload_attachments:
		return {"entity": updated, "attachments": attachments}

	for filename in source_images:
		image_path = (
			source_directory
			/ filename
		)

		result = upload_attachment(
			entity_id,
			image_path,
		)

		attachments.append({
			"filename": filename,
			"primary": False,
			"result": result,
		})

	return {
		"entity": updated,
		"attachments": attachments,
	}
