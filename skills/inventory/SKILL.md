# Inventory ingestion

Use the `inventory_ingest` tool when the user asks to add, catalog, inventory,
or record a photographed physical item in HomeBox.

## Tool-selection rules

- When the user asks to add, catalog, inventory, or record a physical item from
	an image, use `inventory_ingest` directly. Do not call `vision_analyze` first
	merely to identify the item.
- If Hermes exposes explicit local image paths, pass them as `image_paths`.
- If the user just uploaded or pasted an image but Hermes exposes no usable
	path, call `inventory_ingest` with `recent_dashboard_upload: true`. Do not ask
	the user to upload the same image again solely because the path is missing.
- Pass every relevant photograph of the item together in one `image_paths`
	array. They are views of one physical item unless the user clearly says
	otherwise.
- Preserve attachment paths exactly as Hermes supplied them.
- Do not infer a photograph's semantic role from its filename. The inventory
	vision pipeline determines roles from image contents.
- Multiple recent dashboard uploads from one short burst may be multiple views
	of the same physical item and can be resolved together by the fallback.

## Duplicate rules

- If the tool returns `EXACT_DUPLICATE`, no user decision is required. Report
	that the item is already in inventory using `existing_item` details when
	available.
- Never invent confirm, overwrite, skip, merge, or numbered-choice workflows.
- A matching ISBN, UPC, EAN, model, or product name can identify the same
	product without proving the same physical unit. Do not claim an ambiguous
	match was merged or overwritten.
- Do not silently merge uncertain duplicate candidates.

## Output rules

- The tool performs image analysis, identifier extraction, duplicate checks,
	original preservation, HomeBox updates, metadata writing, and receipts.
- Summarize the structured tool result accurately. Do not speculate about
	filenames or backend actions that the result does not report.
- Report errors clearly and do not imply an item was added when `created` is
	false.
