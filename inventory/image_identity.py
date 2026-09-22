"""Read-only SHA-256 identity helpers for canonical Inventory images."""

import json
from collections import defaultdict
from pathlib import Path

from inventory.hashing import sha256_file
from inventory.media import is_supported_image


def incoming_images(directory):
	"""Return one deterministic representative per exact incoming image hash."""
	by_hash = {}
	for path in sorted(Path(directory).iterdir()):
		if not path.is_file() or not is_supported_image(path):
			continue
		digest = sha256_file(path)
		entry = by_hash.setdefault(digest, {"path": path, "sha256": digest, "size": path.stat().st_size, "filenames": []})
		entry["filenames"].append(path.name)
	return list(by_hash.values())


def canonical_image_index(settings):
	"""Index verified canonical image bytes by hash without modifying manifests."""
	index, diagnostics = defaultdict(list), {"missing": [], "checksum_mismatches": [], "unsafe": []}
	for manifest_path in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		for image in manifest.get("images", []):
			if not isinstance(image, dict):
				continue
			relative = Path(str(image.get("relative_path", "")))
			if relative.is_absolute() or ".." in relative.parts:
				diagnostics["unsafe"].append(str(relative)); continue
			path = manifest_path.parent / relative
			if not path.is_file():
				diagnostics["missing"].append(str(path)); continue
			actual = sha256_file(path)
			declared = str(image.get("sha256", "")).casefold()
			if declared and declared != actual:
				diagnostics["checksum_mismatches"].append(str(path)); continue
			index[actual].append({"inventory_id": manifest.get("inventory_id"), "asset_id": manifest.get("asset_id"), "name": manifest.get("item", {}).get("name"), "relative_path": str(relative), "canonical_filename": image.get("canonical_filename", path.name), "source_filename": image.get("source_filename", image.get("original_filename", path.name)), "path": str(path)})
	return dict(index), diagnostics