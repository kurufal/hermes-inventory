"""Duplicate keys and HomeBox-aware inventory duplicate classification."""

import re


STRONG_PRODUCT_FIELDS = {
	"ISBN-13",
	"ISBN-10",
	"UPC",
	"EAN",
	"Barcode",
	"Part Number",
}

UNIT_FIELDS = {
	"Serial Number",
}


def normalize_identifier(value: str) -> str:
	return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def build_duplicate_keys(record: dict) -> list[str]:
	keys = []

	category = record.get("category", "")
	manufacturer = record.get("manufacturer", "")
	name = record.get("name", "")
	identifiers = record.get("identifiers", {})

	for key, values in identifiers.items():
		if not isinstance(values, list):
			continue

		for value in values:
			if not isinstance(value, str):
				continue

			normalized = normalize_identifier(value)

			if normalized:
				keys.append(
					f"identifier:{key}:{normalized}"
				)

	if category and name:
		keys.append(
			"product:"
			+ normalize_identifier(category)
			+ ":"
			+ normalize_identifier(manufacturer)
			+ ":"
			+ normalize_identifier(name)
		)

	return sorted(set(keys))


def normalize_text(value):
	return re.sub(
		r"[^a-z0-9]+",
		"",
		str(value).lower(),
	)


def split_values(value):
	if value is None:
		return []

	return [
		part.strip()
		for part in str(value).split(";")
		if part.strip()
	]


def fields_by_name(entity):
	result = {}

	for field in entity.get(
		"fields",
		[],
	):
		name = field.get("name")

		if not name:
			continue

		field_type = field.get("type")

		if field_type == "text":
			value = field.get(
				"textValue",
				"",
			)

		elif field_type == "number":
			value = field.get(
				"numberValue",
			)

		elif field_type == "boolean":
			value = field.get(
				"booleanValue",
			)

		else:
			value = None

		result[name] = value

	return result


def incoming_identifier_map(record):
	identifiers = record.get(
		"identifiers",
		{},
	)

	mapping = {
		"isbn_13": "ISBN-13",
		"isbn_10": "ISBN-10",
		"upc": "UPC",
		"ean": "EAN",
		"barcode_text": "Barcode",
		"model_number": "Model Number",
		"serial_number": "Serial Number",
		"part_number": "Part Number",
		"other": "Other Identifier",
	}

	result = {}

	for key, field_name in mapping.items():
		values = identifiers.get(
			key,
			[],
		)

		if not isinstance(values, list):
			continue

		normalized = {
			normalize_text(value)
			for value in values
			if normalize_text(value)
		}

		if normalized:
			result[field_name] = normalized

	return result


def incoming_hashes(record):
	return {
		str(image.get("sha256", ""))
		.strip()
		.lower()
		for image in record.get(
			"image_hashes",
			[],
		)
		if str(
			image.get("sha256", "")
		).strip()
	}


def compare_entity(record, entity):
	fields = fields_by_name(entity)

	matches = {
		"image_hashes": [],
		"unit_identifiers": [],
		"product_identifiers": [],
		"name_manufacturer": False,
	}

	current_hashes = {
		value.lower()
		for value in split_values(
			fields.get(
				"Image SHA-256",
				"",
			)
		)
	}

	shared_hashes = (
		incoming_hashes(record)
		& current_hashes
	)

	matches["image_hashes"] = sorted(
		shared_hashes
	)

	incoming_ids = incoming_identifier_map(
		record
	)

	for field_name, incoming_values in incoming_ids.items():
		stored_values = {
			normalize_text(value)
			for value in split_values(
				fields.get(
					field_name,
					"",
				)
			)
			if normalize_text(value)
		}

		shared = (
			incoming_values
			& stored_values
		)

		if not shared:
			continue

		match = {
			"field": field_name,
			"values": sorted(shared),
		}

		if field_name in UNIT_FIELDS:
			matches[
				"unit_identifiers"
			].append(match)

		elif field_name in STRONG_PRODUCT_FIELDS:
			matches[
				"product_identifiers"
			].append(match)

	incoming_name = normalize_text(
		record.get(
			"name",
			"",
		)
	)

	incoming_manufacturer = normalize_text(
		record.get(
			"manufacturer",
			"",
		)
	)

	existing_name = normalize_text(
		entity.get(
			"name",
			"",
		)
	)

	existing_manufacturer = normalize_text(
		entity.get(
			"manufacturer",
			"",
		)
	)

	if (
		incoming_name
		and existing_name
		and incoming_name == existing_name
	):
		if (
			not incoming_manufacturer
			or not existing_manufacturer
			or incoming_manufacturer
			== existing_manufacturer
		):
			matches[
				"name_manufacturer"
			] = True

	if matches["image_hashes"]:
		classification = "EXACT_DUPLICATE"

	elif matches["unit_identifiers"]:
		classification = "SAME_PHYSICAL_UNIT"

	elif matches["product_identifiers"]:
		classification = "SAME_IDENTIFIED_PRODUCT"

	elif matches["name_manufacturer"]:
		classification = "POSSIBLE_SAME_PRODUCT"

	else:
		classification = "NO_MATCH"

	return {
		"classification": classification,
		"entity_id": entity.get("id"),
		"asset_id": entity.get("assetId"),
		"name": entity.get("name"),
		"manufacturer": entity.get(
			"manufacturer",
			"",
		),
		"matches": matches,
	}


def check_homebox_duplicates(record):
	"""Compare an incoming normalized record with all HomeBox entities."""

	from inventory.homebox import get_entity, list_entities

	summaries = list_entities()

	candidates = []

	for summary in summaries.get(
		"items",
		[],
	):
		entity_id = summary.get("id")

		if not entity_id:
			continue

		entity = get_entity(
			entity_id
		)

		comparison = compare_entity(
			record,
			entity,
		)

		if (
			comparison["classification"]
			!= "NO_MATCH"
		):
			candidates.append(
				comparison
			)

	priorities = {
		"EXACT_DUPLICATE": 4,
		"SAME_PHYSICAL_UNIT": 3,
		"SAME_IDENTIFIED_PRODUCT": 2,
		"POSSIBLE_SAME_PRODUCT": 1,
	}

	if not candidates:
		classification = "NEW_ITEM"

	else:
		classification = max(
			candidates,
			key=lambda candidate: priorities[
				candidate["classification"]
			],
		)["classification"]

		candidates.sort(
			key=lambda candidate: priorities[
				candidate["classification"]
			],
			reverse=True,
		)

	return {
		"classification": classification,
		"candidate_count": len(
			candidates
		),
		"candidates": candidates,
	}
