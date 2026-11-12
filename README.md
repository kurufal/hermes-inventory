# Hermes Inventory

Catalog physical items from photos, search local records, and sync with HomeBox. Original images and recovery metadata stay in durable Inventory storage, separate from Hermes' media caches.

## 1. Install

Choose your deployment. Commands below are terminal commands unless marked **Hermes chat**. All paths and container names are examples; substitute your deployment's values.

<details>
<summary><strong>1.1 Windows Hermes Desktop</strong></summary>

**1.1.1 Find the plugin folder (Desktop UI).** Open **Settings > Plugins > Agent plugins > Open plugins folder**. The usual location is `%LOCALAPPDATA%\hermes\plugins`; use the folder Hermes opens for your profile.

**1.1.2 Install (Windows host, PowerShell).** Change to that folder, then clone:

```powershell
cd "$env:LOCALAPPDATA\hermes\plugins"
git clone https://github.com/kurufal/hermes-inventory.git
```

**1.1.3 Enable (Desktop UI).** Restart Desktop, enable `hermes-inventory` under **Agent plugins**, restart again, and start a new chat. Continue to Step 2. No container shell is involved.

</details>

<details>
<summary><strong>1.2 Docker / Container Hermes Agent</strong></summary>

**1.2.1 Identify the Hermes home mount (Docker host).** Check your Compose file or container manager for the existing home volume and `HERMES_HOME`. This example assumes a bind mount `/srv/hermes:/opt/hermes` with `HERMES_HOME=/opt/hermes` inside the container. Keep your existing mount; do not change it just to match this example.

| Purpose | Docker host path | Container path |
| --- | --- | --- |
| Hermes home | `/srv/hermes` | `/opt/hermes` |
| Plugin checkout | `/srv/hermes/plugins/hermes-inventory` | `/opt/hermes/plugins/hermes-inventory` |
| Credentials | `/srv/hermes/.env` | `/opt/hermes/.env` |
| Hermes attachments | `/srv/hermes/images` | `/opt/hermes/images` |

**1.2.2 Install (Docker host, not inside the container).** For the bind mount above:

```sh
mkdir -p /srv/hermes/plugins
cd /srv/hermes/plugins
git clone https://github.com/kurufal/hermes-inventory.git
```

The container sees the same checkout through the mount. No `docker exec` is needed for this route. Ensure the container's Hermes user can read the checkout and write to its home and Inventory directories.

**1.2.3 Alternative: install inside the container.** Only use this route if the home is a persistent named volume or you cannot access its files from the host. Open its console in your container manager, or run this on the **Docker host**, replacing `your-hermes-container`:

```sh
docker exec -it your-hermes-container sh
```

Then run **inside the container**, as the Hermes user, with Git installed:

```sh
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
cd "${HERMES_HOME:-$HOME/.hermes}/plugins"
git clone https://github.com/kurufal/hermes-inventory.git
exit
```

Do not install into a container's disposable filesystem; the Hermes home must persist across recreation.

**1.2.4 Restart and enable (Docker host/container manager).** Restart or redeploy Hermes, enable the plugin if your Hermes version requires it, and start a new session. Continue to Step 2.

</details>

## 2. Configure

<details>
<summary><strong>2.1 HomeBox URL and API key</strong></summary>

**2.1.1 Generate the key (HomeBox UI).** Generate the key in HomeBox, not Hermes. Hermes' `API_SERVER_KEY` is unrelated.

**2.1.2 Edit Hermes' existing environment file.** Choose the location for your deployment:

- **Windows host:** edit the active Desktop profile's `.env`, commonly `%LOCALAPPDATA%\hermes\.env`.
- **Docker bind mount:** edit `/srv/hermes/.env` on the host; the example container sees it at `/opt/hermes/.env`. No container shell is needed.
- **Docker named volume:** edit `.env` in the persistent Hermes home using your volume manager or a container shell. Use the container's path, not `/srv/hermes`.

The general location is `$HERMES_HOME/.env` (default: `~/.hermes/.env`). Add:

```dotenv
HOMEBOX_URL=http://your-homebox-host:3100
HOMEBOX_API_KEY=your_homebox_generated_key
```

For Docker, use a HomeBox address reachable **from the Hermes container**. `localhost` refers to that container, not the Docker host or another container. A Compose service name works when both services share a network.

Keys are never accepted as slash-command arguments: never paste them in chat or store them in `inventory-config.json`. Existing keys are detected automatically. The optional `hermes inventory setup --secrets` terminal command uses hidden input and saves to the same `.env`; run it on the Windows host for Desktop, or inside the Hermes container for Docker.

**2.1.3 Apply changes.** Process environment values take precedence over Hermes `.env`; `homebox.url` in `inventory-config.json` is the URL fallback. A Compose project `.env` is not automatically passed into the container: use `environment`/`env_file` if choosing that route. Restart Hermes after changing an already-exported value; recreate/redeploy the container when changing its environment configuration.

</details>

<details>
<summary><strong>2.2 Optional durable storage</strong></summary>

**2.2.1 Keep the default or choose a mounted location.** Durable data defaults to `$HERMES_HOME/inventory`; runtime state uses `$HERMES_HOME/inventory-runtime`. No storage command is required for the default.

For separate Docker storage, add a writable bind mount such as `/srv/inventory:/inventory-data` in your **host-side container configuration**, set `INVENTORY_BASE_DIR=/inventory-data` in the **container environment**, and redeploy. Inventory uses the container path, never `/srv/inventory`.

**2.2.2 Set a Desktop/NAS override (Hermes chat), if needed:**

```text
/inventory setup storage \\server\share\HermesInventory
/inventory setup storage default
```

A parent path gets an `inventory` child; a path ending in `inventory` is used directly. Setup tests access but never moves existing data. On Windows, prefer UNC paths over mapped drives. On Linux/Docker, mount NAS storage first; the container must see the mount. Inventory does not manage SMB credentials.

`INVENTORY_BASE_DIR`, `INVENTORY_RUNTIME_DIR`, and `INVENTORY_BACKUP_DIR` override storage paths; otherwise `$HERMES_HOME/inventory-config.json` and defaults apply.

</details>

## 3. Verify and Use

<details>
<summary><strong>3.1 Check setup and add your first item</strong></summary>

**3.1.1 Verify (Hermes chat).** Run `/inventory setup` for status, then `/inventory setup test` or `/inventory doctor` to check storage and make a non-destructive Bearer-authenticated HomeBox request. These are chat commands, not host or container shell commands.

**3.1.2 Add an item (Hermes chat).** Attach photos of one physical item and say **Add this to my inventory** in the same message. That is the entire add workflow: no watcher, upload queue, or separate import step. Inventory performs its own vision analysis. Attachments alone and generic conversation do not trigger it.

**3.1.3 Search or update (Hermes chat).** Try "What's item 000-011?", "Tag 000-011 as Cyberpunk", or "Resync 000-011." Use the prior result's Asset ID (`000-011`) or immutable Inventory ID to identify an item. Local search is read-only; user edits survive reanalysis.

</details>

## Photos and Inventory Behavior

Inventory uses only the images attached to the current request, through paths supplied by Hermes; users do not need to type filesystem paths. No background watcher or pending-upload queue is used. Inventory does not scan `$HERMES_HOME/images`, media caches, or Desktop attachment folders, and it never guesses the newest image.

Explicit attachments in `$HERMES_HOME/images` and local Desktop attachments under `%APPDATA%\Hermes\composer-images` remain supported regardless of filename. A remote backend cannot read client-local Windows paths. If Hermes does not provide accessible attachment paths, Inventory reports the attachment problem instead of ingesting an unrelated image. Old `pending-uploads.json` files are no longer used for ingestion and can be removed; the plugin does not create new ones.

The three tools are `inventory_ingest`, `inventory_search`, and `inventory_update`. HomeBox owns tag vocabulary and primary-photo selection; Inventory does not create tags or reorder attachments. Exact duplicate images do not create another item.

## Commands and Recovery

| Command | Purpose |
| --- | --- |
| `/inventory setup` | Configuration status and optional storage/URL overrides |
| `/inventory doctor` | Storage and HomeBox connection checks |
| `/inventory status` | Paths, configuration, and local-state warnings |
| `/inventory uploads` | Request-only attachment behavior and trusted attachment directory |
| `/inventory homebox test` | HomeBox connection check |
| `/inventory refresh` | Read-only reconciliation preview; add `--verbose` for details |
| `/inventory refresh --resolve` | Apply deterministic local repairs after a verified backup |
| `/inventory sync` | Read-only three-way field synchronization preview for linked records |
| `/inventory sync --resolve` | Apply verified, nonconflicting changes after a verified backup |
| `/inventory sync --item 000-011` | Preview one Asset ID; add `--resolve` to apply safe changes |
| `/inventory sync --conflicts` | Read-only unresolved conflicts, including values and sources |
| `/inventory backup` | Create an Inventory backup |
| `/inventory recover` | Scan recovery state; add `plan` for a recovery plan |

`/inventory refresh --dry-run` is also read-only. For an ambiguous match, inspect `--verbose`, preview with `/inventory refresh --resolve --dry-run <ambiguity-number> <inventory-id>`, then remove `--dry-run` to apply your choice. Resolution revalidates evidence, never guesses a match, and does not modify existing HomeBox items or delete legacy evidence.

Items and originals are saved before HomeBox mutation. Failed syncs remain `pending_homebox_sync`. Backups exclude secrets and runtime state. HomeBox-only adoption keeps existing metadata locally but does not download original photos. Existing item schema versions 1-3 remain readable.

## Field Synchronization

`/inventory refresh` still handles reconciliation, adoption, and recovery. `/inventory sync` only compares records with existing Inventory/Homebox entity links. It never links by name, creates entities, deletes records, renames originals, or runs in the background. Use `/inventory help sync` for syntax. `--item` can also be combined with `--conflicts`; `--conflicts --resolve` is rejected.

Sync compares each current local and Homebox value with its last verified baseline. A change on only one side moves to the other side. Equal simultaneous changes advance the baseline without a remote write. Different simultaneous changes remain conflicts; safe changes to other fields may still apply. Modification timestamps never select a winner. Reports separate `planned`, `applied`, `conflicts`, and `skipped`, plus local and Homebox outcomes. A partial result is not complete synchronization.

### Supported Mappings

| Inventory field | Homebox field | Conditions |
| --- | --- | --- |
| `item.name`, `item.description`, `item.manufacturer` | `name`, `description`, `manufacturer` | Text; null and empty text compare equally; line endings normalize |
| `purchase_price` | `purchasePrice` | Finite decimal values, including numeric strings; null/empty clears are distinct from zero |
| `purchase_date` | `purchaseDate` | ISO date or midnight UTC date-time; time-bearing values are skipped rather than truncated |
| `purchase_from` | `purchaseFrom` | Text or cleared text |
| `location` | `location` / `locationId` | Verified location entity ID or unique catalog name/path; ambiguous or unverifiable references are skipped |
| `notes` | Freeform portion of `notes` | Plugin metadata is excluded; ambiguous legacy notes require review |
| `asset_id` | `assetId` | `NNN-NNN`, unique across local records, complete Homebox enumeration, and reservations |
| `item.category` | `entityType.id` / `entityTypeId` | Only an exact, unique matching non-location entity-type name; generic Homebox `Item` does not map to Inventory `Book` |
| User `tags` | `tags` / `tagIds` | Existing unambiguous Homebox vocabulary only; case/order differences normalize; system and category tags are protected |

Homebox tags remain Homebox-owned vocabulary: sync does not create them. Identifiers, attributes, condition observations, quantity, attachments, primary-photo selection, warranty/sale fields, and arbitrary custom fields have no bidirectional mapping in this release. They are not imported from generated notes. Remote imports are marked as manual field overrides so reanalysis does not replace them.

### Baseline and Migration

Each manifest may contain `homebox.sync_baseline`, independent of the item schema version:

```json
{
	"schema": "hermes-inventory-sync",
	"version": 1,
	"inventory_id": "INV-example",
	"entity_id": "homebox-entity-id",
	"verified_at": "2026-11-11T00:00:00Z",
	"fields": {"name": "Example", "purchase_price": "10"}
}
```

Existing manifests need no bulk migration. Preview never creates a baseline. On `--resolve`, equal, verified values can establish a per-field baseline; differences without a reliable baseline are explicit conflicts, not an assumed local or remote win. Review and reconcile those values deliberately, then rerun sync. Unknown baseline formats are not overwritten or downgraded; changed identity links are not trusted. Failed or unverified fields retain their previous baseline; successful independent fields can advance.

Newly ingested items automatically initialize matching baseline fields after Homebox completion and a separate identity-checked reread. No seed sync is needed before detecting a subsequent manual Homebox edit. Successful `inventory_update` operations maintain verified baseline fields using the same comparisons and a pre-write snapshot; a response alone never advances them. Detected preexisting conflicts or Homebox-only changes block the push while keeping local edits durable. Legacy differences without a baseline remain protected. Results include `sync_tracking` with `verified`, `partial`, `unavailable`, or `blocked` status and recovery guidance. Tracking failures do not undo a created item, its receipt, or its completed synchronization state; retry `/inventory sync --resolve` to establish safe equal fields. Unsupported or differing fields remain uninitialized rather than guessed.

### Notes and Apply Safety

New generated notes use a plugin-owned block delimited by `<!-- hermes-inventory:metadata:v1:start -->` and `<!-- hermes-inventory:metadata:v1:end -->`. Text outside it is user-owned and can be imported. Legacy notes are only separated when their generated content can be reconstructed exactly; unverifiable legacy content is reported as `notes_migration_required` and is not rewritten. Existing freeform text is preserved. Ordinary `inventory_update` also refuses to overwrite divergent Homebox user notes.

Resolve creates and verifies an Inventory backup, saves a local pre-apply Homebox snapshot in item history, rechecks identities and both states, and rereads Homebox after any write. Baselines advance only for verified agreeing fields. Incomplete entity/tag pagination or unavailable catalogs blocks application. Originals, receipts, provenance, existing reservations, and backup metadata are not overwritten. Existing `pending_homebox_sync` status is not cleared by field-only sync, since attachment/custom-metadata retries still belong to `inventory_update` with `operation="resync"`. That existing tool remains an explicit local-to-Homebox workflow; do not use resync to choose a winner for unresolved field conflicts. Use sync previews before pushing older local state.

**Remote export requires a strong Homebox ETag and server enforcement of `If-Match`.** Without that capability, exports are reported as `conditional_write_unavailable`; verified imports and baseline establishment can still proceed. No timestamp-only or unchecked overwrite fallback is used. API/HTTP errors are sanitized. A timed-out write may have reached Homebox; a reread must verify its value before its baseline advances. Retry sync to recover. Atomic cross-process local editing is not provided: avoid editing the same manifest concurrently; optimistic checks refuse detected local changes.

## Development and Testing

Run from a development checkout in a terminal, not Hermes chat:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -q
python -m compileall -q .
```

## Updates and Troubleshooting

Run `git pull` in the same checkout used for installation: on the Windows host for Desktop, on the Docker host for a bind mount, or inside the container for the named-volume route. Restart Desktop or redeploy the container, confirm the plugin is enabled, and start a new session.

- **HTTP 401/403:** check the HomeBox-generated `HOMEBOX_API_KEY` in Hermes' `.env`, then run `/inventory setup test`. Network and invalid-URL failures are reported separately.
- **WinError 67:** the SMB share must already exist; Inventory can create a child directory, not the share itself.
- **Desktop attachment unavailable:** use a local backend or upload to the remote backend. Client-local paths are not portable.
- **`tool_call requires a 'name' argument`:** this may be Hermes' Tool Search bridge. Do not bypass Inventory with raw HomeBox calls. An optional Hermes setting is `tools.tool_search.enabled: off`; restart and start a new session. This increases tool-schema context usage.

See [workflow diagrams](docs/flows.md) for a visual overview and [planned work](docs/POAM.md) for future milestones, not current features.