"""Non-destructive inspection of canonical inventory evidence."""

import json
from collections import Counter
from pathlib import Path

from inventory.config import get_settings
from inventory.hashing import sha256_file
from inventory.storage import ITEM_SCHEMA, SCHEMA_VERSION


def scan() -> dict:
	settings = get_settings()
	report = {"valid_items": [], "corrupt_manifests": [], "missing_images": [], "checksum_mismatches": [], "pending_homebox_sync": [], "duplicate_inventory_ids": [], "catalog_inconsistencies": []}
	ids = []
	for path in settings.items_dir.glob("*/item.json") if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(path.read_text(encoding="utf-8"))
			if manifest.get("schema") != ITEM_SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
				raise ValueError("unsupported item manifest schema")
		except (OSError, json.JSONDecodeError, ValueError) as exc:
			report["corrupt_manifests"].append({"path": str(path), "error": str(exc)})
			continue
		item_id = str(manifest.get("inventory_id", ""))
		ids.append(item_id)
		report["valid_items"].append(item_id)
		if manifest.get("status") == "pending_homebox_sync":
			report["pending_homebox_sync"].append(item_id)
		for image in manifest.get("images", []):
			image_path = path.parent / str(image.get("relative_path", ""))
			if not image_path.is_file():
				report["missing_images"].append(str(image_path))
			elif image.get("sha256") and sha256_file(image_path) != image["sha256"]:
				report["checksum_mismatches"].append(str(image_path))
	report["duplicate_inventory_ids"] = [value for value, count in Counter(ids).items() if value and count > 1]
	report["status"] = "PASS" if not any(report[key] for key in report if key not in {"valid_items", "status"}) else "WARN"
	return report