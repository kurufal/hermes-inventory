"""Convert a raw vision record into the HomeBox-oriented item record."""

from pathlib import Path

from inventory.hashing import hash_images
from inventory.duplicates import build_duplicate_keys


TYPE_SYNONYMS = {
	"book": "Book", "hardcover": "Book", "paperback": "Book", "novel": "Book",
	"board game": "Board Game", "boardgame": "Board Game", "tabletop game": "Board Game",
	"video game": "Video Game", "game cartridge": "Video Game",
	"figure": "Figure / Statue", "statue": "Figure / Statue", "anime figure": "Figure / Statue", "resin statue": "Figure / Statue", "pvc figure": "Figure / Statue",
	"collectible": "Collectible", "electronics": "Electronics", "computer hardware": "Computer Hardware",
	"tool": "Tool", "appliance": "Appliance", "media": "Media", "toy": "Toy",
}


def normalize_type(value) -> str:
	text = " ".join(str(value or "").casefold().replace("-", " ").split())
	if text in TYPE_SYNONYMS:
		return TYPE_SYNONYMS[text]
	for synonym, canonical in TYPE_SYNONYMS.items():
		if synonym in text:
			return canonical
	return "Other"


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
		"category": normalize_type(value("object_type")),
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
