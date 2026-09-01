# hermes-inventory installed

1. Restart Hermes Desktop, enable `hermes-inventory`, start a new chat, and run `/inventory setup`.
2. The default persistent storage is `$HERMES_HOME/inventory`. Override it only when needed with `/inventory setup storage <absolute-path>`.
3. Save the non-secret server address with `/inventory setup homebox <url>`.
4. Run `/inventory setup secrets`. Never enter an API key in chat; it provides `hermes inventory setup --secrets` for secure hidden terminal input into Hermes' normal `.env`.
5. Return to Desktop, run `/inventory setup`, then attach an image and say `Add this to my inventory.`
6. Run `/inventory backup create` and `/inventory backup verify` after the first successful ingest.

The plugin defaults to `$HERMES_HOME/inventory-runtime` for local operational state and `$HERMES_HOME/inventory` for durable inventory evidence. Its non-secret configuration is `$HERMES_HOME/inventory-config.json`. See [README.md](README.md) for Desktop, Docker, UNC/NAS, recovery, and security guidance.