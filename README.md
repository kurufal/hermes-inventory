# Hermes Inventory

Hermes Inventory catalogs photographs of physical items in HomeBox. It keeps durable original images, vision metadata, manifests, receipts, and backups separately from Hermes operational media.

## Documentation

This is the v0.5.0 documentation pass. See the [flow guide](docs/flows.md) for setup, command, tool, and reconciliation diagrams. See the [POAM](docs/POAM.md) for planned work and milestones; it distinguishes future items from current functionality.

## Quick Start: Windows Hermes Desktop

### Step 1: Open the Agent plugin folder

In Hermes Desktop, open **Settings** > **Plugins** > **Agent plugins** > **Open plugins folder**. The usual path is `%LOCALAPPDATA%\hermes\plugins`, but use the folder Hermes opens because profiles may differ.

### Step 2: Install the plugin

```powershell
cd "$env:LOCALAPPDATA\hermes\plugins"
git clone https://github.com/kurufal/hermes-inventory.git
```

The checkout contains `plugin.yaml`, `__init__.py`, `inventory\`, and `skills\`.

### Step 3: Restart and enable

Fully close and reopen Hermes Desktop. Open **Settings** > **Plugins** > **Agent plugins** and enable `hermes-inventory`. Fully restart Hermes Desktop again after enabling it, then start a new chat. Plugin registrations apply to new sessions.

### Step 4: Setup

```text
/inventory setup
/inventory setup storage \\192.168.1.160\hermes-data
/inventory setup homebox http://192.168.1.160:3100
/inventory setup secrets
```

Storage defaults to `%LOCALAPPDATA%\hermes\inventory` (more precisely, `$HERMES_HOME\inventory`) and needs no acceptance step, but setup marks it as a local-default warning so a NAS choice is visible. A parent such as `\\192.168.1.160\hermes-data` resolves to `\\192.168.1.160\hermes-data\inventory`; a path already ending in `inventory` is used as the exact root. The storage command checks access before saving and never moves existing Inventory data. When the API key is missing, `/inventory setup secrets` explains the secure terminal command:

```powershell
hermes inventory setup --secrets
```

The key is hidden while entered and is stored in Hermes' normal `%LOCALAPPDATA%\hermes\.env`. Do not paste it in chat.

### Step 5: Test and use

```text
/inventory setup test
```

`/inventory doctor` runs the same checks. Then attach one or more photos and say: `Add this to my inventory.`

## Quick Start: Docker/Container Hermes Agent

The plugin sees paths inside the container only. For a host data root mounted at `/opt/data`:

```text
HOST PATH:      /mnt/Data16/media/hermes-data
CONTAINER PATH: /opt/data
PLUGIN PATH:    /opt/data/plugins/hermes-inventory
UPLOAD PATH:    /opt/data/images
```

Clone into the host-mounted plugin directory, then restart or redeploy the container:

```sh
cd /mnt/Data16/media/hermes-data/plugins
git clone https://github.com/kurufal/hermes-inventory.git
```

The container sees that checkout at `/opt/data/plugins/hermes-inventory`. This is the supported Git-folder approach; no verified Hermes container plugin-installer command is available in this development environment, so one is not documented here.

For separately mounted durable inventory storage:

```text
HOST PATH:      /mnt/Data16/inventory/hermes-inventory
CONTAINER PATH: /inventory-data
```

Set `INVENTORY_BASE_DIR=/inventory-data` in the container environment. Do not set it to the host path. Existing configurations using `HERMES_HOME=/opt/data`, `INVENTORY_BASE_DIR=/opt/data/inventory`, `HOMEBOX_URL`, and `HOMEBOX_API_KEY` continue to work.

For Docker, normally provide `HOMEBOX_API_KEY` through the container `.env` or environment configuration. The local CLI wizard is unnecessary when that environment is already configured.

## Guided Setup

`/inventory setup` reports the resolved persistent storage, HomeBox URL/key/authentication, Hermes upload inbox, and Inventory runtime directory. Commands are case-insensitive; values are preserved exactly.

- `/inventory setup storage <absolute-path>` sets a tested override.
- `/inventory setup storage default` restores `$HERMES_HOME/inventory`.
- `/inventory setup homebox <url>` stores the non-secret URL in `inventory-config.json`.
- `/inventory setup secrets` reports only secret status and the local secure workflow.
- `/inventory setup test` verifies storage, upload/runtime paths, and a non-destructive authenticated HomeBox request.

Environment values have priority over plugin configuration. A non-empty `HOMEBOX_URL` is therefore Docker-compatible.

## Secrets

API keys are never accepted as slash-command arguments and never appear in output. On Windows, use `hermes inventory setup --secrets`; on Docker, use the container environment configuration. Keys are never written to `inventory-config.json`, `item.json`, `catalog.json`, receipts, backups, TOON, or logs.

## Using Inventory

Automatic detection watches only `$HERMES_HOME/images` and only Hermes-managed `dashboard_*`, `upload_*`, and `clip_*` image filenames. It supports existing Dashboard filenames such as `dashboard_20260815_190339_<id>_signal-photo.jpg` and Desktop upload/clip names.

Hermes exposes three Inventory tools: `inventory_ingest` to add or catalog a
physical item, `inventory_search` to read existing canonical Inventory data,
and `inventory_update` to edit, reanalyze, or resync an existing item. They
apply only when the user clearly asks to operate on Inventory or HomeBox data;
greetings, tests, connection/model checks, plugin development questions, and
attachments without an Inventory request do not trigger them.

`$HERMES_HOME/media`, `$HERMES_HOME/image_cache`, and `$HERMES_HOME/user_media` are never automatically scanned. Desktop composer images at `%APPDATA%\Hermes\composer-images` are also never watched, but an existing path Hermes explicitly supplies in `image_paths` is trusted on a local Desktop backend. The internal Python `ingest(source_directory, ...)` API can use an explicitly supplied source directory for development/testing. The chat tool otherwise accepts only `$HERMES_HOME/images` and trusted current Desktop composer paths, never arbitrary filesystem paths. A composer path unavailable to a remote Linux/Docker backend is rejected rather than guessed.

## Updating the Plugin

Windows Desktop:

```powershell
cd "$env:LOCALAPPDATA\hermes\plugins\hermes-inventory"
git pull
```

Fully close Hermes Desktop, reopen it, and verify `hermes-inventory` remains enabled. If it must be enabled again, restart Hermes Desktop a second time. Start a new chat/session.

For Docker management UIs, determine the host-mounted Hermes data directory, update `<host Hermes data>/plugins/hermes-inventory` with Git, restart/redeploy the Hermes container, then start a new Hermes session. No verified dashboard plugin update control is available, so this document does not claim one exists.

## Network/NAS Storage

On Windows prefer UNC storage such as `\\server\share\HermesInventory` rather than a mapped drive. Windows manages SMB credentials; the plugin neither mounts shares nor stores SMB credentials. On Linux/Docker, mount the share on the host and configure the container mount path.

## Media and Path Layout

Hermes operational media is owned by Hermes:

```text
$HERMES_HOME/
├── images/       automatic Inventory inbox only
├── image_cache/  never scanned
├── media/        never scanned
├── user_media/   never scanned
└── plugins/
```

Inventory owns `$HERMES_HOME/inventory-runtime` for local pending state/staging and `<configured persistent root>/` for `items/`, `observations/`, `receipts/`, `catalog.json`, and `backups/`. Canonical Inventory items are never placed in Hermes media paths by default; recovery uses Inventory-owned originals rather than Hermes caches.

## Commands

`/inventory setup`, `/inventory status`, `/inventory doctor`, `/inventory refresh`, `/inventory storage`, `/inventory uploads`, `/inventory homebox`, `/inventory backup`, and `/inventory recover` are available before HomeBox setup. `inventory_ingest` returns `not_configured` and directs to `/inventory setup` until HomeBox URL and API key exist.

`/inventory refresh` is a read-only reconciliation preview; `--dry-run` is an equivalent explicit preview alias. Add `--verbose` to show canonical, HomeBox-only, local-only, conflict, deterministic-plan, and ambiguous legacy-candidate detail. Refresh compares canonical local Inventory manifests, legacy or historical evidence, Asset ID reservations, incomplete transactions, and configured HomeBox records.

`/inventory refresh --resolve` safely applies only deterministic local reconciliation actions. Before changing canonical data it creates and verifies an Inventory-owned local backup, then rechecks the plan. It can adopt a HomeBox-only record, migrate the verified historical `metadata/<Inventory ID>.json` plus `originals/<Inventory ID>/` layout, repair a missing local HomeBox link, normalize old system-owned `Type: ` tags, remove only provably stale tokenized orphan Asset ID reservations, and rebuild the local catalog. Tokenless legacy reservations, malformed reservations, incomplete transactions, and historical duplicate image evidence are reported but never automatically deleted. It never deletes historical evidence, guesses fuzzy identity matches, or modifies an existing HomeBox item during adoption. Conflicts and ambiguous records are skipped.

For an ambiguity, inspect `/inventory refresh --verbose`, preview a chosen legacy record with `/inventory refresh --resolve --dry-run <ambiguity-number> <inventory-id>`, then apply it with `/inventory refresh --resolve <ambiguity-number> <inventory-id>`. The chosen ID must be one of the displayed candidates and is revalidated against its metadata, original-image hashes, and HomeBox identity immediately before mutation. Explicit resolution writes only a new canonical local record; it never changes HomeBox or deletes legacy evidence.

HomeBox adoption stores the existing HomeBox metadata locally but does not download attachments as immutable originals. Adopted schema-v3 records can therefore have no local images or vision result; local search remains canonical/local. Existing schema-v1 and schema-v2 photographed records remain readable.

## Updating Items

Use `inventory_update` for factual corrections, durable-image reanalysis, and HomeBox resyncs. Target the item from the prior result with its Asset ID (for example, `000-011`) or immutable Inventory ID (for example, `INV-20260901-204535-601208be`). The Asset ID is the readable label; the Inventory ID remains the recovery identity.

```text
inventory_update target=000-011 operation=edit changes={"attributes":[{"name":"Format","value":"Hardcover"}],"purchase_from":"Half Price Books - Tacoma, WA","purchase_price":14.99}
inventory_update target=000-011 operation=reanalyze
inventory_update target=000-011 operation=resync
```

Edits are marked user-owned and survive later reanalysis. Supported corrections include names, descriptions, type/category, manufacturer, condition, identifiers, attributes, purchase details, locations, notes, and tags. Examples: `This is hardcover, not paperback.`, `Tag 000-011 as Cyberpunk.`, `Update the description of 000-011.`, `Redo the Cyberpunk book.`, and `Resync 000-011.`

**Asset ID vs Inventory ID:** `000-011` is the human-friendly Asset ID used in conversation and HomeBox. `INV-20260901-204535-601208be` is the immutable Inventory ID used for durable storage and recovery.

Inventory normalizes types to Book, Board Game, Video Game, Figure / Statue, Collectible, Electronics, Computer Hardware, Tool, Appliance, Media, Toy, or Other. Category is the canonical item type; explicit local tags such as `Cyberpunk` are user tags. Existing unrelated HomeBox tags are preserved.

HomeBox owns the tag vocabulary. Inventory matches category and explicit user tags separately against existing HomeBox tags by name and never creates HomeBox tags automatically. The canonical category is matched directly, so local `Book` classification uses a HomeBox `Book` tag when it exists.

## Search

Use `inventory_search` to query the local canonical Inventory without changing it:

```text
Check my inventory for The Martian.
Do I have Cyberpunk 2077?
What's item 000-011?
Show me my board games.
```

Search matches Asset ID, Inventory ID, names, descriptions, categories, identifiers, attributes, tags, and locations. A single match includes its stored details; multiple matches are returned as a list without guessing the intended item.

Canonical originals use names such as `000-011_cyberpunk-2077-no-coincidence_front-cover.jpg` and `000-011_cyberpunk-2077-no-coincidence_copyright-isbn-page.jpg`. Manifests retain original filenames and hashes, which remain stronger recovery signals than filenames. Primary/display image selection is managed through the HomeBox UI. Inventory never reorders or changes HomeBox primary attachments.

Exact SHA-256 image preflight runs before vision. All-exact incoming evidence can stop before vision; identical bytes are intentionally not stored again as another canonical image. Mixed old/new evidence is not automatically merged into an existing item. Perceptually similar but byte-different or re-encoded images are not automatic duplicates. Attachment retries use SHA-256-keyed `attachment_sync` state, so a pending resync skips already uploaded bytes and uploads only missing images. The installed HomeBox integration has no verified attachment-metadata rename contract, so attachment display names are not renamed automatically; local canonical filenames remain authoritative.

## Backup and Recovery

Items are persisted before HomeBox mutation with original images, `vision.json`, and canonical `item.json`. A failed sync remains `pending_homebox_sync`. Refresh reports historical duplicate canonical image hashes. Backups exclude runtime state and secrets; recovery is non-destructive. Item schema remains version 3 and the plugin release is 0.5.0.

## Advanced Configuration

Paths resolve as environment variable, then `$HERMES_HOME/inventory-config.json`, then defaults. `INVENTORY_RUNTIME_DIR`, `INVENTORY_BASE_DIR`, and `INVENTORY_BACKUP_DIR` override their respective locations. `HOMEBOX_URL` environment configuration overrides plugin JSON.

## Development and Testing

The direct Python ingestion API accepts explicitly supplied local source directories for development/testing. Run:

```powershell
python -m unittest discover -s tests -q
python -m compileall -q .
```

## Troubleshooting

### Tool Search bridge error

If Hermes discovers and describes `inventory_ingest`, then reports `tool_call requires a 'name' argument`, the failure may be the Hermes deferred Tool Search bridge rather than Inventory registration or HomeBox. Do not manually call HomeBox as a substitute: Inventory owns durable photos, duplicate checks, recovery metadata, and linkage. An optional Hermes configuration workaround is:

```yaml
tools:
	tool_search:
		enabled: off
```

This keeps plugin/MCP schemas eager and can increase tool-schema context usage. The plugin does not edit this setting; restart Hermes and start a new session after changing it. No verified Desktop UI location for this setting is available.

### Desktop attachment rejected

Current versions accept explicit existing local paths below `%APPDATA%\Hermes\composer-images`. A remote backend cannot read a Windows Desktop-client path; use a local backend or a valid recent backend upload batch.

### HomeBox 401

The server was reached but rejected the credential. Current HomeBox static keys normally start with `hb_`. Run `hermes inventory setup --secrets` and enter a current key; the key is hidden and never echoed.

### Storage unavailable: WinError 67

The SMB share itself must already exist. Inventory may create its `inventory` child beneath an existing share, but it cannot create a missing SMB share.