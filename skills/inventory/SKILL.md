# Inventory Routing

When the user asks to add, inventory, catalog, record, save, or put a
photographed physical item into inventory or HomeBox, use `inventory_ingest`.

Treat these as direct inventory commands:

- “add this to my inventory”
- “add this item to my inventory”
- “inventory this”
- “catalog this”
- “record this item”
- “add this to HomeBox”
- “put this in HomeBox”
- “add the thing I just uploaded”

If “this”, “item”, “thing”, “image”, “photo”, “these images”, “these photos”,
or “the uploaded item” refers to a recent upload, do not ask what kind of
inventory the user means. Do not ask the user to identify the object first.
This plugin means physical-item inventory; do not reinterpret the request as a
task, note, reminder, bookmark, subscription, or digital resource unless the
user explicitly says so.

Do not call `vision_analyze` first. `inventory_ingest` performs its own vision
analysis.

## Choose the input

- If explicit local attachment paths exist, pass every relevant path in
  `image_paths`, preserving the paths exactly.
- If the user refers to a recent upload but no usable local path is exposed,
  call `inventory_ingest` with:

  ```json
  {"use_pending_upload": true}
  ```

- Do not ask the user to upload the same image again merely because Hermes did
  not expose its path.
- Multiple images in the newest pending upload batch are views of one physical
  item unless the user explicitly says otherwise.
- If no pending upload exists, report that clearly instead of inventing a path.

## Anti-clarification rule

When the user combines an inventory verb (`add`, `inventory`, `catalog`,
`record`, `save`, or `put`), an inventory/HomeBox destination, and a referent
such as “this”, “this item”, “this thing”, “this image”, “these photos”, or
“what I uploaded”, the correct action is `inventory_ingest`.

Do not ask:

- “What kind of inventory?”
- “What physical item?”
- “Is this a digital resource, task, or note?”
- “What would you like to add?”

Do not stop after `tool_search` or `tool_describe`; once `inventory_ingest` is
found, invoke it. Do not route every occurrence of “add” to this tool: “add a
reminder”, “add this sentence to my notes”, and “add this bookmark” are not
physical inventory commands.

Do not call `Clarify` for an inventory verb plus a recent-upload referent. Do
not ask which inventory tool, system, or plugin to use. There is exactly one
correct action: `inventory_ingest`.

## Duplicates and multiple images

If the tool returns `EXACT_DUPLICATE`, no new HomeBox item was created and no
user decision is required. Report the existing item using `existing_item` when
available. Never offer confirm, overwrite, skip, merge, or numbered choices.
