# Inventory Workflows

An optional visual overview of current behavior. For installation, credentials, paths, and commands, use the [README](../README.md). Future work belongs in the [POAM](POAM.md).

## Setup

Configure HomeBox in Hermes' existing `.env`; interactive secret setup is optional. Credentials never belong in chat.

```mermaid
flowchart TD
    Install["Install and enable plugin"] --> Env["Set HomeBox URL and key in Hermes .env"]
    Env --> Setup["/inventory setup"]
    Setup --> Test["/inventory setup test"]
    Test --> Result{"Checks pass?"}
    Result -->|Yes| Ready["Ready"]
    Result -->|No| Fix["Correct the reported configuration or connection issue"]
    Fix --> Test
```

## Add, Search, and Update

Attach photos and ask to add the item in the same message. Only those attachments are processed; there is no background watcher or upload queue. Local records and originals are durable; user edits survive reanalysis and search never changes records.

```mermaid
flowchart TD
    Request["Explicit inventory request"] --> Action{"Action"}
    Action -->|Add| Ingest["Analyze photos and save local item with originals"]
    Action -->|Search| Search["Read local records"]
    Action -->|Update| Update["Edit or reanalyze selected local item"]
    Ingest --> Sync["Sync with HomeBox"]
    Update --> Sync
    Action -->|Resync| Sync
    Sync --> Result{"Sync succeeds?"}
    Result -->|Yes| Done["Return saved item"]
    Result -->|No| Pending["Keep durable item pending sync"]
```

## Reconcile

Refresh compares local, legacy, and HomeBox records. Repairs require a verified backup; ambiguous matches require your explicit choice. Existing HomeBox items and legacy evidence are not deleted or overwritten.

```mermaid
flowchart TD
    Preview["/inventory refresh"] --> Report["Read-only report; --verbose adds detail"]
    Report --> Choice{"Apply repairs?"}
    Choice -->|No| Stop["No changes"]
    Choice -->|Yes| Resolve["/inventory refresh --resolve"]
    Resolve --> Validate["Validate plan and any explicitly chosen match"]
    Validate --> Backup["Create and verify backup"]
    Backup --> Apply["Recheck and apply permitted local changes"]
    Apply --> Final["Final report"]
```

Add `--dry-run` to a resolution command to preview without applying changes.

## Synchronize Linked Fields

Sync is separate from refresh. Use `/inventory sync` or `/inventory sync --item 000-011` to preview without writes. `/inventory sync --conflicts` shows both values, the last verified baseline when known, and their sources. Add `--resolve` to a preview command to apply only safe changes; it cannot be combined with `--conflicts`.

```mermaid
flowchart TD
    Preview["/inventory sync"] --> Read["Read canonical manifests and complete Homebox catalogs"]
    Read --> Compare["Compare baseline, Inventory, and linked Homebox fields"]
    Compare --> Report["Separate planned changes, conflicts, and skipped mappings"]
    Report --> Choice{"Apply safe changes?"}
    Choice -->|No| Stop["No filesystem or remote mutations"]
    Choice -->|Yes| Backup["Create and verify backup"]
    Backup --> Check["Recheck local state and Homebox identities and revisions"]
    Check --> Apply["Import safe fields; conditionally export using strong ETag"]
    Apply --> Verify["Reread Homebox and verify each affected field"]
    Verify --> Save["Atomically save verified fields and their baselines"]
    Save --> Result["Report applied, conflicts, skipped, and partial outcomes"]
```

Missing baselines never authorize choosing a winner. Equal values can establish a baseline on resolve; differing values require deliberate reconciliation. Plugin-delimited generated notes are separate from Homebox user text; ambiguous legacy notes block notes synchronization. Remote exports are currently blocked because the full write contract and conditional enforcement remain unverified, even if a strong ETag is present. Verified imports and equal-field baseline establishment remain available. Setup diagnostics use GET only; no live write probe or force override is exposed. See the [README](../README.md#field-synchronization) for exact field mappings, baseline format, and compatibility limits.
