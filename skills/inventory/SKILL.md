# Inventory Routing

When the user asks to add, inventory, catalog, record, save, or put a
photographed physical item into inventory or HomeBox, use `inventory_ingest`.

Use `inventory_search` for check my inventory, do I have, find, search, look
up, what is item, show me, or what details requests. It is read-only: use it
before updates when the target is unclear. Do not confuse search with
`inventory_update`, which is only for edits, reanalysis, and resync.

For an existing canonical item, use `inventory_update`; never bypass Inventory
with raw HomeBox tools. Route update, change, correct, edit, rename, set,
purchased at, paid, cost, located at, move, tag, and untag to `operation=edit`. Route
reanalyze, re-analyze, re-evaluate, reevaluate, redo, recheck, and refresh
analysis to `operation=reanalyze`; route resync to `operation=resync`. Use the
Asset ID or Inventory ID from the conversation's prior result. Do not guess a
globally most-recent item without that context. On ambiguity, present the
candidates and make no mutation.
If the user wants a different main photo, explain that primary-photo selection is
managed manually in HomeBox; do not manipulate attachment order or primary state.

When `inventory_ingest` returns `not_configured`, direct the user to
`/inventory setup`. Do not request or accept a HomeBox API key in chat.
`/inventory setup secrets` provides the local secure terminal workflow.
Configuration may be Windows Desktop or Docker/container based; use resolved
paths reported by `/inventory setup`, and never assume a host path is visible
inside a container.
Explicit existing Desktop paths under the trusted Hermes `composer-images` root
are valid current attachments; that directory is never background-watched.
If a deferred `tool_call` fails once with a bridge error such as "requires a
name argument" after discovery succeeds, do not repeat the same call endlessly,
inspect plugin source, reverse-engineer HomeBox, or bypass this plugin. Report
the Hermes bridge failure and point to Inventory troubleshooting. Never call
raw HomeBox APIs as a replacement for `inventory_ingest` and never solicit keys.

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
