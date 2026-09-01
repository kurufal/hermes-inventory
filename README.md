# Hermes Inventory

Hermes Inventory catalogs photographs of physical items in HomeBox. It keeps durable original images, vision metadata, manifests, receipts, and backups separately from Hermes operational media.

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

Fully close and reopen Hermes Desktop. Open **Settings** > **Plugins** > **Agent plugins**, enable `hermes-inventory`, then start a new chat.

### Step 4: Setup

```text
/inventory setup
/inventory setup storage \\server\share\HermesInventory
/inventory setup homebox http://host:port
/inventory setup secrets
```

Storage defaults to `%LOCALAPPDATA%\hermes\inventory` (more precisely, `$HERMES_HOME\inventory`) and needs no acceptance step. The storage command checks access before saving and never moves existing Inventory data. When the API key is missing, `/inventory setup secrets` explains the secure terminal command:

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
PLUGIN PATH:    /opt/data/images
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

`$HERMES_HOME/media`, `$HERMES_HOME/image_cache`, and `$HERMES_HOME/user_media` are never automatically scanned. The internal Python `ingest(source_directory, ...)` API can use an explicitly supplied source directory for development/testing, but arbitrary manual directories are intentionally not accepted by the chat tool: it only accepts Hermes attachment paths under `$HERMES_HOME/images`.

## Updating the Plugin

Windows Desktop:

```powershell
cd "$env:LOCALAPPDATA\hermes\plugins\hermes-inventory"
git pull
```

Restart Hermes Desktop and start a new chat.

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

`/inventory setup`, `/inventory status`, `/inventory doctor`, `/inventory storage`, `/inventory uploads`, `/inventory homebox`, `/inventory backup`, and `/inventory recover` are available before HomeBox setup. `inventory_ingest` returns `not_configured` and directs to `/inventory setup` until HomeBox URL and API key exist.

## Backup and Recovery

Items are persisted before HomeBox mutation with original images, `vision.json`, and canonical `item.json`. A failed sync remains `pending_homebox_sync`. Backups exclude runtime state and secrets; recovery is non-destructive.

## Advanced Configuration

Paths resolve as environment variable, then `$HERMES_HOME/inventory-config.json`, then defaults. `INVENTORY_RUNTIME_DIR`, `INVENTORY_BASE_DIR`, and `INVENTORY_BACKUP_DIR` override their respective locations. `HOMEBOX_URL` environment configuration overrides plugin JSON.

## Development and Testing

The direct Python ingestion API accepts explicitly supplied local source directories for development/testing. Run:

```powershell
python -m unittest discover -s tests -q
python -m compileall -q .
```