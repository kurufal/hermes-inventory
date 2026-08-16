# hermes-inventory

`hermes-inventory` is a Hermes Agent plugin for adding photographed physical
items to a HomeBox inventory. It stages one or more photographs of one item,
preserves the originals, calls Hermes' host-configured vision route, extracts
generic item data, checks HomeBox for duplicates, and either creates an item
or returns the matching inventory record.

It is intentionally generic: books, board games, tools, electronics, parts,
media, household equipment, and other physical possessions share the same
pipeline. Item-specific data is stored as generic HomeBox fields rather than
category-specific application code.

## Architecture

```text
Stock Hermes Agent
	|
	| ctx.register_tool(...)
	v
hermes-inventory plugin
	|
	+-- inventory_ingest
	+-- host-routed inventory vision analysis
	+-- normalization and SHA-256 hashing
	+-- HomeBox duplicate inspection
	+-- HomeBox entity, field, and attachment updates
	+-- metadata and receipts
	v
HomeBox
```

The plugin source is self-contained. Persistent evidence remains outside the
repository under `INVENTORY_BASE_DIR`, which defaults to `/opt/data/inventory`.
No file under `/opt/hermes` is modified.

## Installation

Install the plugin from GitHub using Hermes' normal plugin command, then
install its required Python dependency in the environment that runs Hermes:

```sh
hermes plugins install OWNER/hermes-inventory --enable
python -m pip install "requests>=2.31,<3"
hermes plugins doctor ~/.hermes/plugins/hermes-inventory --ci
```

Replace `OWNER` with the GitHub account or organization that owns this
repository. The plugin keeps its requirements in [requirements.txt](requirements.txt),
and Hermes does not install them automatically. If the Hermes installation uses
a virtual environment or a container image, run the second command with that
environment's Python. Use the actual plugin path instead of
`~/.hermes/plugins/hermes-inventory` when `HERMES_HOME` is customized.

Enable the registered toolset for the relevant conversation when required by
the Hermes UI:

```text
/tools enable inventory
```

The plugin uses only `ctx.register_tool()` for Hermes integration. The
development plugin's private `toolsets._HERMES_CORE_TOOLS` mutation was removed
because it is not a supported public plugin API. Consequently, immediate tool
visibility depends on Hermes' normal toolset enablement and discovery behavior.

## Configuration

Configure the HomeBox integration and persistent-storage variables in the
Hermes runtime environment. Do not commit a real API key or place one in the
repository.

```sh
export HOMEBOX_URL="http://homebox.example.internal"
export HOMEBOX_API_KEY="replace-with-a-secret-from-your-runtime"
export INVENTORY_BASE_DIR="/opt/data/inventory"
export HERMES_HOME="/opt/data"
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `HOMEBOX_URL` | Yes | Base URL of the HomeBox API. |
| `HOMEBOX_API_KEY` | Yes | Bearer token used for HomeBox API requests. |
| `INVENTORY_BASE_DIR` | No | Persistent data root. Defaults to `/opt/data/inventory`. |
| `HERMES_HOME` | No | Root that tool image paths must remain beneath. Defaults to `/opt/data`. |

`HOMEBOX_URL` intentionally has no installation-specific default. The code
discovers HomeBox's non-location `Item` entity type at runtime rather than
hard-coding an entity-type UUID.

### Model configuration belongs to Hermes

The plugin deliberately does **not** use direct model HTTP calls or accept
legacy `VISION_MODEL` or `OLLAMA_CHAT_URL` settings. It registers the
`hermes_inventory_vision` auxiliary task and uses the public
`ctx.llm.complete_structured()` API. This repository includes tested defaults
for the local Qwen3-VL deployment used by the target installation; Hermes
operator configuration still takes precedence and remains the correct place
to change provider, model, endpoint, credentials, or fallback behavior.

Configure that task in Hermes through `hermes model` → **Configure auxiliary
models**. The checked-in defaults route to the target installation's
Qwen3-VL endpoint. Operators can instead configure
`auxiliary.hermes_inventory_vision` in Hermes' `config.yaml` to select a
different provider, model, endpoint, credentials, timeout, or fallback chain;
Hermes applies that operator configuration over the plugin defaults.

For a different local OpenAI-compatible vision endpoint, add or override a
deployment-specific block in Hermes' `config.yaml` (commonly
`$HERMES_HOME/config.yaml`), then restart Hermes:

```yaml
auxiliary:
  hermes_inventory_vision:
    base_url: "http://VISION-HOST:PORT/v1"
    api_key: "local-endpoint-key"
    model: "your-vision-model"
    timeout: 600
```

`base_url` makes Hermes call that endpoint directly, instead of resolving the
main chat model. The checked-in defaults are intentionally tied to the
deployment that this repository serves (`192.168.1.160:30068` and
`qwen3-vl:8b-instruct-q8_0`). Forks or public distributions should replace
those defaults with host configuration rather than publishing private network
details.

## Runtime data

The plugin never stores runtime inventory evidence in its Git checkout. These
directories are created as needed below `INVENTORY_BASE_DIR`:

```text
/opt/data/inventory/
├── originals/       # immutable copied source photographs by inventory ID
├── metadata/        # raw structured model response by inventory ID
├── receipts/        # auditable ingest outcomes, including duplicate attempts
└── tool-staging/    # temporary copied Hermes attachments, removed per call
```

Plugin upgrades must preserve these paths. The original photographs remain the
raw evidence for inspection, reprocessing, and hash verification.

## Tool behavior

The plugin registers `inventory_ingest` in the `inventory` toolset. Its input
is an `image_paths` array of one or more local image paths. All paths represent
one physical item and are staged together before processing.

The entry point accepts only regular files below `HERMES_HOME`, rejects
unsupported extensions, resolves symlinks, and de-duplicates repeated paths.
This prevents model-generated calls from reading arbitrary host files. The
plugin accepts PNG, JPEG, GIF, WebP, and BMP paths and passes their proper MIME
types to Hermes' structured-vision interface.

The tool performs its own vision analysis. Hermes should call it directly when
the request is to add, catalog, inventory, or record the attached physical
item; calling generic `vision_analyze` first is unnecessary.

## Vision and generic fields

`inventory/vision.py` sends every staged photo in one call to Hermes'
host-managed structured LLM interface. Hermes selects the configured auxiliary
route, provider, model, credentials, timeout, and fallback behavior; the
plugin supplies only the inventory-specific prompt, image grouping, and output
handling. The prompt instructs the model to infer image roles from pixels
rather than filenames and to return a single generic record containing:

- item category, name, manufacturer, and physical description;
- ISBN-10, ISBN-13, UPC, EAN, barcode, model, serial, part, and other IDs;
- condition observations, uncertainties, readable text, and image roles; and
- arbitrary supported attributes such as `Players`, `Ages`, `Format`,
  `Voltage`, or `Storage Capacity`.

HomeBox fields preserve recognized identifiers, the inventory ID, exact image
hashes, and arbitrary attributes. Fields not managed by this plugin are kept
when a HomeBox entity is updated.

## Duplicate behavior

Each archived image receives a SHA-256 hash. Duplicate comparison prioritizes:

1. exact SHA-256 image hashes;
2. serial-number matches for a physical unit;
3. product IDs such as ISBN, UPC, EAN, barcode, and part number; and
4. matching normalized name and manufacturer.

The returned classifications are `EXACT_DUPLICATE`, `SAME_PHYSICAL_UNIT`,
`SAME_IDENTIFIED_PRODUCT`, `POSSIBLE_SAME_PRODUCT`, and `NEW_ITEM`.

An `EXACT_DUPLICATE` does not create an entity. The tool result sets
`requires_user_action` to `false`, includes the matching HomeBox name,
manufacturer, asset ID, and entity ID when available, and tells Hermes not to
invent confirm, overwrite, skip, merge, or numbered-choice workflows.

### Migration compatibility note

The exported, previously tested pipeline returns `duplicate_candidate` without
creating a HomeBox entity for **every** classification other than `NEW_ITEM`.
This is preserved in this behavior-first migration. It conflicts with the
long-term architectural rule that an ISBN, UPC, or model match identifies a
product but does not necessarily prove the same owned physical unit. A future,
explicitly designed policy must decide when a user-confirmed additional copy
creates a new entity; this migration does not silently redesign that behavior.

## HomeBox requirements

The configured HomeBox instance must expose the authenticated entity endpoints
used by the prior working implementation:

```text
GET  /api/v1/entity-types
GET  /api/v1/entities
GET  /api/v1/entities/{id}
POST /api/v1/entities
PUT  /api/v1/entities/{id}
POST /api/v1/entities/{id}/attachments
```

HomeBox is the user-facing inventory source of truth. The local metadata and
receipts are an audit and provenance layer, not a competing inventory database.

## Development and testing

Run the supported Hermes plugin validation path before publishing:

```sh
hermes plugins doctor . --ci
```

The repository also contains a no-network contract test for the image-to-LLM
wiring. It uses a fake Hermes LLM facade and does not contact HomeBox or a
model provider:

```sh
python -m unittest discover -s tests
```

Vision extraction requires Hermes because the model route, authentication, and
fallback policy are host-owned. Exercise a live ingest through
`inventory_ingest` after configuring HomeBox and
`auxiliary.hermes_inventory_vision`; there is intentionally no standalone
vision command with a separate provider or model configuration.

Suggested regression checks are:

1. Ingest a general non-book item with multiple photographs and generic fields.
2. Re-ingest exact copies of previously stored photos and verify
   `EXACT_DUPLICATE` with no new HomeBox entity.
3. Verify all photos arrive in one `inventory_ingest` call and are attached.
4. Verify unrelated HomeBox fields remain after an entity update.
5. Verify originals, metadata, and receipts are written below the configured
   persistent inventory root.

## Security

- Keep `HOMEBOX_API_KEY` only in runtime environment configuration.
- Do not put keys, bearer tokens, or authorization headers in logs, receipts,
  tool results, examples, or Git history.
- Configure deployment-specific model providers, endpoints, credentials, and
	fallbacks in Hermes' model and auxiliary-task configuration. Do not replace
	the checked-in `ollama` placeholder with a real secret in a public fork.
- The plugin refuses image paths outside `HERMES_HOME` and copies accepted files
  into a unique staging directory before ingestion.
- Original source files are copied rather than modified in place.

## Known limitation: stock dashboard attachments

The inventory backend accepts valid attached-image paths and does not require
Hermes core changes. A clean stock Hermes dashboard deployment was observed to
upload an image and display an `/image /opt/data/images/...` command without
attaching that file to the active conversation. Historical experiments fixed
that platform issue by modifying Hermes' `image.attach` routing, but those
patches are deliberately not included here.

Until Hermes provides a supported plugin-level attachment bridge, this remains
a Hermes dashboard limitation rather than an inventory backend workaround. No
file under `/opt/hermes` is modified by this repository.
