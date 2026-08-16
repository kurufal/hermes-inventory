"""Mapping between generic inventory metadata and HomeBox custom fields."""


def identifier_field_name(key: str) -> str:
	names = {
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

	return names.get(
		key,
		key.replace("_", " ").title(),
	)


def infer_field_type(value):
	if isinstance(value, bool):
		return {
			"type": "boolean",
			"booleanValue": value,
		}

	if isinstance(value, int) and not isinstance(value, bool):
		return {
			"type": "number",
			"numberValue": value,
		}

	return {
		"type": "text",
		"textValue": str(value),
	}


def existing_field_map(fields):
	return {
		field.get("name"): field
		for field in fields
		if field.get("name")
	}


def make_field(name, value, existing=None):
	field = {
		"name": name,
	}

	if existing and existing.get("id"):
		field["id"] = existing["id"]

	field.update(
		infer_field_type(value)
	)

	return field


def build_homebox_fields(
	record: dict,
	existing_fields=None,
):
	existing_fields = existing_fields or []

	existing = existing_field_map(
		existing_fields
	)

	generated = []
	generated_names = set()

	item_id = str(
		record.get("item_id", "")
	).strip()

	if item_id:
		name = "Inventory Item ID"

		generated.append(
			make_field(
				name,
				item_id,
				existing.get(name),
			)
		)

		generated_names.add(name)

	image_hashes = []

	for image in record.get(
		"image_hashes",
		[],
	):
		sha256 = str(
			image.get("sha256", "")
		).strip().lower()

		if sha256:
			image_hashes.append(sha256)

	if image_hashes:
		name = "Image SHA-256"

		generated.append(
			make_field(
				name,
				"; ".join(
					sorted(set(image_hashes))
				),
				existing.get(name),
			)
		)

		generated_names.add(name)

	identifiers = record.get(
		"identifiers",
		{},
	)

	for key, values in identifiers.items():
		if not isinstance(values, list):
			continue

		clean_values = [
			str(value).strip()
			for value in values
			if str(value).strip()
		]

		if not clean_values:
			continue

		name = identifier_field_name(key)

		value = "; ".join(clean_values)

		generated.append(
			make_field(
				name,
				value,
				existing.get(name),
			)
		)

		generated_names.add(name)

	attributes = record.get(
		"attributes",
		[],
	)

	for attribute in attributes:
		if not isinstance(attribute, dict):
			continue

		name = str(
			attribute.get("name", "")
		).strip()

		value = attribute.get("value")

		if not name:
			continue

		if value is None:
			continue

		if isinstance(value, str):
			value = value.strip()

			if not value:
				continue

			if value.lower() in {
				"unknown",
				"uncertain",
				"not observed",
				"not visible",
			}:
				continue

		generated.append(
			make_field(
				name,
				value,
				existing.get(name),
			)
		)

		generated_names.add(name)

	for field in existing_fields:
		name = field.get("name")

		if (
			name
			and name not in generated_names
		):
			generated.append(field)

	return generated
