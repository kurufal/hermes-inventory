# hermes-inventory installed

1. Install [requirements.txt](requirements.txt) in Hermes' Python environment.
2. Set `HOMEBOX_URL` and the secret `HOMEBOX_API_KEY` through Hermes configuration.
3. Run `/inventory setup`, then `/inventory doctor`.
4. Optionally set persistent storage with `/inventory storage set <path>` after the target share is mounted and accessible to Hermes.
5. Run `/inventory backup create` and `/inventory backup verify` after the first successful ingest.

The plugin defaults to `$HERMES_HOME/inventory-runtime` for local operational state and `$HERMES_HOME/inventory` for durable inventory evidence. Its non-secret configuration is `$HERMES_HOME/inventory-config.json`. See [README.md](README.md) for Desktop, Docker, UNC/NAS, recovery, and security guidance.