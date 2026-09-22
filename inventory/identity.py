"""Pure, conservative identity comparison for Inventory reconciliation."""

from collections import defaultdict


PRODUCT_FIELDS = {
	"isbn_10": "ISBN-10",
	"isbn_13": "ISBN-13",
	"upc": "UPC",
	"ean": "EAN",
	"barcode_text": "Barcode",
	"model_number": "Model Number",
}


def normalized_values(value):
	if isinstance(value, (list, tuple, set)):
		parts = value
	else:
		parts = str(value or "").replace(";", "\n").splitlines()
	return {str(part).strip().casefold() for part in parts if str(part).strip()}


def entity_field_values(entity, name):
	values = set()
	for field in entity.get("fields", []) if isinstance(entity, dict) else []:
		if not isinstance(field, dict) or str(field.get("name", "")).casefold() != name.casefold():
			continue
		for key in ("textValue", "numberValue", "value"):
			if field.get(key) is not None:
				values.update(normalized_values(field[key]))
	return values


def entity_identifiers(entity):
	result = {key: entity_field_values(entity, field_name) for key, field_name in PRODUCT_FIELDS.items()}
	for key, top_level in (("serial_number", "serialNumber"), ("model_number", "modelNumber")):
		result.setdefault(key, set()).update(normalized_values(entity.get(top_level, "")))
	result["serial_number"].update(entity_field_values(entity, "Serial Number"))
	return result


def match_manifest(manifest, entities):
	"""Return conservative matches without treating product IDs or names as identity."""
	entities = [entity for entity in entities if isinstance(entity, dict) and entity.get("id")]
	inventory_id = str(manifest.get("inventory_id", "")).strip().casefold()
	stored_entity_id = str(manifest.get("homebox", {}).get("entity_id") or "").strip()
	hashes = {
		str(image.get("sha256", "")).strip().casefold()
		for image in manifest.get("images", []) if isinstance(image, dict) and image.get("sha256")
	}
	serials = normalized_values(manifest.get("identifiers", {}).get("serial_number", []))
	product_ids = {
		key: normalized_values(manifest.get("identifiers", {}).get(key, []))
		for key in PRODUCT_FIELDS
	}

	def candidates(predicate):
		return [entity for entity in entities if predicate(entity)]

	checks = [
		("stored_entity_id", candidates(lambda entity: stored_entity_id and str(entity.get("id")) == stored_entity_id)),
		("inventory_item_id", candidates(lambda entity: inventory_id and inventory_id in entity_field_values(entity, "Inventory Item ID"))),
		("image_sha256", candidates(lambda entity: hashes & entity_field_values(entity, "Image SHA-256"))),
		("serial_number", candidates(lambda entity: serials & entity_identifiers(entity).get("serial_number", set()))),
	]
	for kind, matches in checks:
		if len(matches) == 1:
			return {"classification": "strong_match", "kind": kind, "entity": matches[0], "candidates": []}
		if len(matches) > 1:
			return {"classification": "ambiguous", "kind": kind, "entity": None, "candidates": matches}

	product_matches = []
	for key, values in product_ids.items():
		if not values:
			continue
		for entity in candidates(lambda entry: values & entity_identifiers(entry).get(key, set())):
			product_matches.append(entity)
	if product_matches:
		by_id = {str(entity["id"]): entity for entity in product_matches}
		return {"classification": "candidate", "kind": "product_identifier", "entity": None, "candidates": [by_id[key] for key in sorted(by_id)]}
	return {"classification": "unmatched", "kind": None, "entity": None, "candidates": []}