"""Convert a raw vision record into the HomeBox-oriented item record."""

from pathlib import Path

from inventory.hashing import hash_images
from inventory.duplicates import build_duplicate_keys


def normalize_record(record: dict) -> dict:
	result = record.get(
		"result",
		{},
	)

	if not isinstance(result, dict):
		result = {}

	def mapping(value):
		return value if isinstance(value, dict) else {}

	def mappings(value):
		if not isinstance(value, list):
			return []

		return [
			entry
			for entry in value
			if isinstance(entry, dict)
		]

	def value(field):
		data = result.get(
			field,
			{},
		)

		if isinstance(data, dict):
			return data.get(
				"value",
				"",
			)

		return ""

	identifiers = mapping(
		result.get(
			"identifiers",
			{},
		)
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

	if not isinstance(source_images, list):
		source_images = []

	source_images = [
		filename
		for filename in source_images
		if isinstance(filename, str) and filename
	]

	normalized = {
		"item_id": record.get(
			"item_id",
			"",
		),
		"category": value(
			"object_type"
		),
		"name": value(
			"product_or_title"
		),
		"manufacturer": value(
			"manufacturer_or_publisher"
		),
		"identifiers": identifiers,
		"physical_description": value(
			"physical_description"
		),
		"condition": mappings(
			result.get(
				"condition_observations",
				[],
			)
		),
		"attributes": mappings(
			result.get(
				"attributes",
				[],
			)
		),
		"image_roles": mappings(
			result.get(
				"image_roles",
				[],
			)
		),
		"source_directory": str(
			source_directory
		),
		"source_images": source_images,
		"image_hashes": hash_images(
			source_directory,
			source_images,
		),
		"review_required": True,
	}

	normalized[
		"duplicate_keys"
	] = build_duplicate_keys(
		normalized
	)

	return normalized
