# hermes-inventory

`hermes-inventory` is a Hermes Agent backend plugin for cataloging photographed physical items in HomeBox. It registers `inventory_ingest`, an Inventory skill, the `hermes_inventory_vision` auxiliary task, and (where supported by the installed public Hermes plugin context) one `/inventory` command namespace.

## Installation and Settings

Install with Hermes' normal plugin installation command, then install [requirements.txt](requirements.txt) in the Python environment that runs Hermes. Configure `HOMEBOX_URL` and `HOMEBOX_API_KEY` through Hermes' normal environment/secret mechanism. The API key is never stored in plugin YAML, manifests, receipts, backups, status output, or TOON.

Paths resolve in this order: non-empty environment value, `$HERMES_HOME/inventory-config.yaml`, then the portable default. `HERMES_HOME` is resolved with Hermes' public helper when available; otherwise `HERMES_HOME`, then `~/.hermes` is used. Existing `HERMES_HOME=/opt/data` and `INVENTORY_BASE_DIR` deployments remain supported.

| Purpose | Default | Override |
| --- | --- | --- |
| Hermes uploads | `$HERMES_HOME/images` | Hermes-owned |
| Runtime state/staging | `$HERMES_HOME/inventory-runtime` | `INVENTORY_RUNTIME_DIR` |
| Persistent evidence | `$HERMES_HOME/inventory` | `INVENTORY_BASE_DIR` |
| Backups | `<persistent>/backups` | `INVENTORY_BACKUP_DIR` |

The runtime directory stays local, so pending upload state and staging do not depend on NAS availability. Persistent storage is safely write/read/rename/delete probed before ingest; failure stops before HomeBox creation.

## Desktop, Network, Docker

The same Python plugin works with Hermes Agent Docker and native Hermes Desktop. It watches Hermes-managed images named `dashboard_`, `upload_`, or `clip_` with supported image extensions.

On Windows, use UNC storage such as `\\truenas\Inventory\HermesInventory`; it is more reliable than a mapped drive. Linux/macOS shares must be mounted by the operating system first. Docker containers cannot use Windows UNC paths directly: mount the share on the host, bind it into the container (for example `/inventory-data`), then set `INVENTORY_BASE_DIR=/inventory-data`. The plugin does not mount shares or store SMB credentials.

## Commands

`/inventory` is help. Command names are case-insensitive; path arguments retain their exact case/content.

- `/inventory setup`, `status`, `doctor`, `version`, `help`
- `/inventory storage [show|test|set <path>|reset]`
- `/inventory uploads [status]`
- `/inventory homebox [status|test|url]`
- `/inventory backup [create|list|verify [path]]`
- `/inventory recover [status|scan|plan]`

HomeBox URL/key updates remain Hermes configuration responsibilities. Native HomeBox export/import is not triggered because no verified public API was available in this development environment.

## Durable Evidence and Recovery

Each ingested item is persisted first under `items/<inventory-id>/` with immutable source images, `vision.json`, and versioned canonical `item.json`. The manifest records the item fields, IDs, attributes, image filenames/paths/MIME types/SHA-256 hashes, HomeBox linkage, timestamps, and sync state. A derived `catalog.json` is regenerated atomically. Existing `originals/`, `metadata/`, and `receipts/` are never removed or automatically moved.

If HomeBox sync fails after persistence, the manifest remains with `pending_homebox_sync` for recovery. `/inventory recover scan` is non-destructive and reports malformed manifests, missing images, checksum failures, duplicate IDs, and pending sync work. Automatic recovery apply is deliberately not implemented without a verified safe HomeBox API contract.

Backups are Zip64-capable ZIP archives created atomically. They include plugin-owned persistent data except existing backup archives, a canonical backup manifest, member SHA-256 hashes, and a companion archive SHA-256 file. `/inventory backup verify` rejects malformed/unsafe ZIP paths and validates manifest checksums without extraction. JSON is canonical; TOON is intentionally unavailable rather than hand-implementing an evolving specification.

## Security

Only regular supported image files directly below `$HERMES_HOME/images` are ingested; symlinks and arbitrary host paths are refused. Original images are copied byte-for-byte and never rewritten. Backup creation excludes runtime state and secrets, and recovery performs no destructive action.

## Validation

```sh
python -m unittest discover -s tests
python -m py_compile __init__.py inventory/*.py
```