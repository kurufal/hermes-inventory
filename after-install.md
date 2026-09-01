# hermes-inventory installed

## Windows Desktop

1. Restart Hermes Desktop and enable `hermes-inventory` under **Agent plugins**.
2. Start a new chat and run `/inventory setup`.
3. Configure optional storage and HomeBox URL, then use `/inventory setup secrets` for the terminal-only API-key workflow.

## Docker

1. Restart or redeploy the Hermes container after placing the plugin under `$HERMES_HOME/plugins`.
2. Start a new session and run `/inventory setup`.
3. Configure container paths, `HOMEBOX_URL`, and `HOMEBOX_API_KEY` through the container environment.

The default persistent storage is `$HERMES_HOME/inventory`; custom storage is tested before saving and existing data is not moved. See [README.md](README.md) for host versus container paths, updates, and secrets.

The plugin defaults to `$HERMES_HOME/inventory-runtime` for local operational state and `$HERMES_HOME/inventory` for durable inventory evidence. Its non-secret configuration is `$HERMES_HOME/inventory-config.json`. See [README.md](README.md) for Desktop, Docker, UNC/NAS, recovery, and security guidance.