"""Read-only canonical Inventory search."""

import json


def _text(value):
	if isinstance(value, dict):
		return " ".join(f"{key} {_text(entry)}" for key, entry in value.items())
	if isinstance(value, list):
		return " ".join(_text(entry) for entry in value)
	return str(value or "")


def search_inventory(query="", category=None, tags=None, limit=10, *, settings):
	"""Search local canonical manifests without performing any writes or HTTP calls."""
	needles = [part.casefold() for part in str(query or "").split() if part]
	category = str(category or "").casefold()
	tags = {str(tag).casefold() for tag in tags or []}
	results = []
	for path in sorted(settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else []:
		try:
			manifest = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		if manifest.get("schema") != "hermes-inventory-item":
			continue
		item = manifest.get("item", {})
		local_tags = {str(tag.get("name", "")).casefold() for tag in manifest.get("tags", []) if isinstance(tag, dict)}
		if category and item.get("category", "").casefold() != category:
			continue
		if tags and not tags.issubset(local_tags):
			continue
		haystack = _text({"asset_id": manifest.get("asset_id"), "inventory_id": manifest.get("inventory_id"), "item": item, "identifiers": manifest.get("identifiers"), "attributes": manifest.get("attributes"), "tags": manifest.get("tags"), "location": manifest.get("location")}).casefold()
		if needles and not all(needle in haystack for needle in needles):
			continue
		exact = str(query or "").casefold() in {str(manifest.get("asset_id", "")).casefold(), str(manifest.get("inventory_id", "")).casefold(), str(item.get("name", "")).casefold()}
		results.append({"asset_id": manifest.get("asset_id"), "inventory_id": manifest.get("inventory_id"), "name": item.get("name"), "category": item.get("category"), "manufacturer": item.get("manufacturer"), "description": item.get("description"), "identifiers": manifest.get("identifiers", {}), "attributes": manifest.get("attributes", []), "tags": manifest.get("tags", []), "location": manifest.get("location"), "purchase_price": manifest.get("purchase_price"), "purchase_from": manifest.get("purchase_from"), "exact": exact})
	results.sort(key=lambda result: (not result.pop("exact"), str(result.get("asset_id", ""))))
	return {"status": "found" if results else "not_found", "count": len(results), "items": results[:max(1, min(int(limit or 10), 100))]}