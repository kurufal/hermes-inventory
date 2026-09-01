# hermes-inventory

`hermes-inventory` catalogs photographed physical items in HomeBox through Hermes.

## Quick Start: Windows Hermes Desktop

1. In Hermes Desktop, open **Settings** > **Plugins** > **Agent plugins** > **Open plugins folder**.
2. Clone this repository into that plugins folder.
3. Restart Hermes Desktop.
4. Enable `hermes-inventory`.
5. Start a new chat.
6. Run `/inventory setup`.
7. The default persistent location is `$HERMES_HOME/inventory`; leave it alone when it is suitable. To override settings, use `/inventory setup storage \\server\share\HermesInventory` and/or `/inventory setup homebox http://host:port`.
8. When the API key is missing, run `/inventory setup secrets`.
9. That helper displays the secure local-terminal command: `hermes inventory setup --secrets`.
10. Run that command locally, enter the key using hidden input, return to Desktop, and run `/inventory setup` again.
11. Attach one or more photos of one physical item and say `Add this to my inventory.`

The API key is never accepted in chat because chat arguments and output are not appropriate secret transport. It is stored only in Hermes' normal `.env` secret configuration, never in `inventory-config.json`, `item.json`, `catalog.json`, receipts, backup files, TOON, logs, or slash-command output.

## Setup Commands

Commands are case-insensitive; values are preserved exactly.

- `/inventory setup` summarizes persistent storage, HomeBox URL/key/authentication, upload directory, and runtime storage.
- `/inventory setup storage <absolute-path>` safely checks and saves a custom persistent path. It does not move existing inventory.
- `/inventory setup storage default` returns to `$HERMES_HOME/inventory`.
- `/inventory setup homebox <url>` saves the non-secret URL in plugin JSON. A non-empty `HOMEBOX_URL` environment value has higher precedence, preserving Docker compatibility.
- `/inventory setup secrets` only reports secret status and the secure CLI command. It never accepts an API key argument.
- `/inventory setup test` runs the same storage and authenticated HomeBox checks as setup.
- `/inventory setup help` shows setup syntax.

The secure CLI mode performs only secret configuration. It requires an existing HomeBox URL, permits Enter to retain an existing key, preserves unrelated `.env` entries, and immediately makes a non-destructive authenticated HomeBox request. On failure, it preserves the key and reports `hermes inventory setup --secrets` as the retry command.

## Storage and Docker

| Purpose | Default | Override |
| --- | --- | --- |
| Hermes uploads | `$HERMES_HOME/images` | Hermes-owned |
| Runtime state/staging | `$HERMES_HOME/inventory-runtime` | `INVENTORY_RUNTIME_DIR` |
| Persistent evidence | `$HERMES_HOME/inventory` | `INVENTORY_BASE_DIR` |
| Backups | `<persistent>/backups` | `INVENTORY_BACKUP_DIR` |

On Windows use a UNC location such as `\\truenas\Inventory\HermesInventory`. Docker containers cannot use a Windows UNC path directly; mount it on the host, bind it into the container, and set `INVENTORY_BASE_DIR` to the container path. Runtime state remains local.

## Updating

For Git-folder installs, run PowerShell:

```powershell
cd "$env:LOCALAPPDATA\hermes\plugins\hermes-inventory"
git pull
```

Restart Hermes Desktop and start a new chat.

## Evidence and Recovery

Each item is persisted before HomeBox mutation under `items/<inventory-id>/` with original images, `vision.json`, and canonical `item.json`. Failed HomeBox synchronization remains recoverable as `pending_homebox_sync`. Backups exclude runtime state and secrets; recovery does not destructively change files.

## Validation

```powershell
python -m unittest discover -s tests -q
python -m compileall -q .
```