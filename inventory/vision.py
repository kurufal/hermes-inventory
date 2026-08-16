"""Host-routed vision analysis for a directory of one item's photographs."""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import METADATA_DIR


_LOGGER = logging.getLogger("hermes_plugins.hermes_inventory.vision")


VISION_TASK = "hermes_inventory_vision"

MIME_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".webp": "image/webp",
	".gif": "image/gif",
	".bmp": "image/bmp",
}


BASE_PROMPT = """
You are analyzing ONE physical inventory item using multiple photographs.

The photographs may show different views, labels, packaging, markings,
screens, serial-number stickers, covers, pages, connectors, damage,
accessories, or other details belonging to the same physical item.

Do NOT assume the item is a book or any other specific category.
Infer the object type only from visible evidence.

Return ONLY valid JSON using this structure:

{
  "object_type": {
	"value": "",
	"confidence": 0.0,
	"source_images": []
  },
  "manufacturer_or_publisher": {
	"value": "",
	"confidence": 0.0,
	"source_images": []
  },
  "product_or_title": {
	"value": "",
	"confidence": 0.0,
	"source_images": []
  },
  "identifiers": {
	"isbn_13": [],
	"isbn_10": [],
	"upc": [],
	"ean": [],
	"barcode_text": [],
	"model_number": [],
	"serial_number": [],
	"part_number": [],
	"other": []
  },
  "attributes": [
	{
	  "name": "",
	  "value": "",
	  "confidence": 0.0,
	  "source_images": []
	}
  ],
  "readable_text": [
	{
	  "text": "",
	  "source_images": []
	}
  ],
  "physical_description": {
	"value": "",
	"confidence": 0.0,
	"source_images": []
  },
  "condition_observations": [
	{
	  "observation": "",
	  "confidence": 0.0,
	  "source_images": []
	}
  ],
  "other_observations": [
	{
	  "observation": "",
	  "confidence": 0.0,
	  "source_images": []
	}
  ],
  "uncertainties": [
	{
	  "observation": "",
	  "source_images": []
	}
  ],
  "image_roles": [
	{
	  "filename": "",
	  "inferred_role": "",
	  "confidence": 0.0
	}
  ]
}

Rules:

1. All supplied images belong to ONE inventory item unless there is strong
   visible evidence otherwise.

2. Do not assume the category of the item.

3. Do not invent identifiers or other information.

4. Record visible information and strong visual conclusions only.

5. Every substantive fact should list the supporting filename or filenames.

6. Confidence values must be numbers from 0.0 through 1.0.

7. Infer each image's likely role from its contents, not from its filename.

8. Use "attributes" for useful structured properties that describe the
   specific item and are supported by visible evidence.

9. Attribute names must be short, human-readable labels such as:
   "Format", "Edition", "Color", "Material", "Capacity", "Voltage",
   "Power Source", "Drive Size", "Storage Capacity", "Signed",
   "Signed By", "Numbered Edition", or other item-relevant properties.

10. Do not limit attributes to the examples above. Choose attributes based
	on the actual item shown.

11. Do not emit an attribute merely because it might apply to that kind
	of item. Emit it only when the photographs provide evidence.

12. For absence-based properties such as signatures, damage, accessories,
	seals, or markings, do not infer a negative value merely because the
	property is not visible. For example, do not emit "Signed": false just
	because no signature appears in the supplied photographs.

13. If an attribute is directly readable as text, preserve the observed
	wording when practical.

14. Avoid duplicating identifiers inside attributes when they already fit
	the identifiers object.

15. Return ONLY JSON. No markdown or explanatory text.
"""


def image_files(directory):
	return sorted(
		path
		for path in directory.iterdir()
		if path.is_file() and path.suffix.lower() in MIME_TYPES
	)


def _image_inputs(images):
	inputs = []

	for image in images:
		with image.open("rb") as source:
			data = source.read()

		inputs.append({
			"type": "image",
			"data": data,
			"mime_type": MIME_TYPES[image.suffix.lower()],
			"file_name": image.name,
		})

	return inputs


def analyze_directory(
	item_directory,
	vision_client,
	*,
	task=VISION_TASK,
):
	"""Analyze one item directory and persist the raw structured response."""

	item_dir = Path(item_directory).resolve()

	if not item_dir.exists():
		raise FileNotFoundError(
			f"Directory does not exist: {item_dir}"
		)

	if not item_dir.is_dir():
		raise ValueError(
			f"Expected a directory: {item_dir}"
		)

	images = image_files(item_dir)

	if not images:
		raise ValueError(
			f"No supported images found in {item_dir}"
		)

	METADATA_DIR.mkdir(parents=True, exist_ok=True)

	filename_list = "\n".join(
		f"Image {index + 1}: {image.name}"
		for index, image in enumerate(images)
	)

	prompt = (
		BASE_PROMPT
		+ "\n\nThe supplied images are in this exact order:\n"
		+ filename_list
		+ "\n\nUse these exact filenames in source_images and image_roles."
	)

	_LOGGER.info(
		"Vision request: item_id=%s image_count=%d images=%s sizes=%s task=%s",
		item_dir.name,
		len(images),
		[image.name for image in images],
		[image.stat().st_size for image in images],
		task,
	)

	response = vision_client.complete_structured(
		instructions=prompt,
		input=_image_inputs(images),
		json_mode=True,
		temperature=0.0,
		purpose="inventory.vision",
		task=task,
	)

	parsed = getattr(response, "parsed", None)
	raw_content = str(getattr(response, "text", "")).strip()

	if isinstance(parsed, dict):
		parse_status = "json_ok"
	else:
		parsed = {
			"raw_model_output": raw_content,
		}
		parse_status = "json_parse_failed"

	audit = getattr(response, "audit", {})

	if not isinstance(audit, dict):
		audit = {}

	record = {
		"item_id": item_dir.name,
		"timestamp": datetime.now(UTC).isoformat(),
		"source_directory": str(item_dir),
		"source_images": [
			image.name
			for image in images
		],
		"llm": {
			"provider": str(getattr(response, "provider", "")),
			"model": str(getattr(response, "model", "")),
			"agent_id": str(getattr(response, "agent_id", "")),
			"audit": audit,
		},
		"parse_status": parse_status,
		"result": parsed,
	}

	out_file = METADATA_DIR / f"{item_dir.name}.json"

	out_file.write_text(
		json.dumps(
			record,
			indent=2,
			ensure_ascii=False,
		),
		encoding="utf-8",
	)

	return record, out_file


def main(argv=None):
	del argv
	print(
		"inventory.vision must be invoked by Hermes so it can use "
		"the host-managed LLM configuration.",
		file=sys.stderr,
	)
	return 2


if __name__ == "__main__":
	raise SystemExit(main())
