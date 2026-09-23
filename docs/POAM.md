# Hermes Inventory POAM

## Objective

Guide planned improvements while preserving canonical local Inventory records, cautious reconciliation, and non-destructive recovery practices.

## Current State

Achieved capabilities:

- Durable canonical items with original images, manifests, metadata, receipts, and backups.
- HomeBox synchronization for ingest and applicable updates.
- Local canonical search and targeted update, reanalysis, and resync flows.
- Read-only refresh previews, deterministic resolution, explicit ambiguity selection, backups, and retained historical evidence.
- Windows Desktop and container deployment guidance.

## Near-Term Milestones

### Cleanup Mode

- **Summary:** Design `/inventory refresh --clean` as a future cleanup mode for artifacts proven redundant or stale after successful reconciliation.
- **Rationale:** Operators need a controlled way to reduce obsolete local artifacts without weakening recovery.
- **Priority:** High.
- **Status:** Planned.
- **Guardrails:** Preview first, create and verify a backup before destructive cleanup, remove only artifacts proven redundant or stale, and never remove canonical live data by default.

### Historical Retry Reporting

- **Summary:** Improve reporting and operator guidance for historical retry groups already represented by canonical items.
- **Rationale:** Retained evidence should be understandable without appearing as an unresolved identity problem.
- **Priority:** Medium.
- **Status:** Planned.

### Refresh and Resolve Edge Cases

- **Summary:** Expand regression coverage for refresh, deterministic resolve, explicit candidate resolution, backup failure, and concurrent-state changes.
- **Rationale:** Reconciliation is safety-sensitive and benefits from focused boundary tests.
- **Priority:** High.
- **Status:** Ongoing.

## Mid-Term Milestones

### Deletion and Removal Workflow

- **Summary:** Design an explicit item removal workflow with confirmation, recovery records, and clear HomeBox coordination.
- **Rationale:** Removal needs a deliberate contract that does not conflict with durable local recovery.
- **Priority:** High.
- **Status:** Future design.

### Richer Update Flows

- **Summary:** Extend guided updates for purchase details, locations, condition, notes, identifiers, tags, and optional new images.
- **Rationale:** Operators should be able to maintain records through concise, predictable requests.
- **Priority:** Medium.
- **Status:** Planned.

### Duplicate-Image Improvements

- **Summary:** Improve reporting and operator decisions for exact duplicate images, mixed evidence, and re-encoded near-duplicates.
- **Rationale:** Better visibility can reduce unnecessary copies without creating unsafe automatic merges.
- **Priority:** Medium.
- **Status:** Future.

### Container and Web GUI Guidance

- **Summary:** Expand deployment guidance for mounted paths, environment configuration, upgrades, and common container management interfaces.
- **Rationale:** Clear operational documentation reduces path and secret-configuration errors.
- **Priority:** Medium.
- **Status:** Planned.

## Longer-Term Roadmap

### Alternative Inventory Backends

- **Summary:** Add compatibility with alternative inventory backends one at a time, prioritized by operator need and backend popularity.
- **Rationale:** The integration surface should remain understandable and supportable.
- **Priority:** Medium.
- **Status:** Future.
- **Notes:** Keep the abstraction clean and preserve canonical local Inventory records as the source of truth where practical. Do not attempt broad multi-backend support in a single release.

### Export and Reporting

- **Summary:** Add optional export and reporting formats for catalog summaries, inventory audits, and recovery-oriented records.
- **Rationale:** Operators need portable visibility without bypassing canonical storage.
- **Priority:** Low.
- **Status:** Future.

### Operational Reporting

- **Summary:** Provide clearer summaries for storage health, backup history, unresolved reconciliation findings, and safe maintenance candidates.
- **Rationale:** Regular operations should be easy to review before actions are taken.
- **Priority:** Low.
- **Status:** Future.

## Risks and Notes

- Canonical local records should remain durable and recoverable even when an external backend is unavailable.
- Destructive workflows require explicit user intent, preview, verified backup, and narrow proof of safety.
- HomeBox compatibility must remain non-destructive for existing items unless an explicit future workflow states otherwise.
- Roadmap items are not current commands or promised delivery dates.
