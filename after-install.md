# hermes-inventory installed

1. Install the required Python dependency in the same environment that runs Hermes:

   `python -m pip install "requests>=2.31,<3"`

2. Configure `HOMEBOX_URL` and `HOMEBOX_API_KEY` in the Hermes runtime environment.

3. The plugin includes the tested **Inventory vision** route for
   `qwen3-vl:8b-instruct-q8_0` at `http://192.168.1.160:30068/v1` with the
   `ollama` key. If this deployment uses a different endpoint or model, run
   `hermes model` and override `auxiliary.hermes_inventory_vision.base_url`,
   `.api_key`, `.model`, and `.timeout` in Hermes' `config.yaml`. Hermes
   operator configuration takes precedence over the plugin defaults.

4. Validate the installed plugin with `hermes plugins doctor <plugin-path> --ci`.

5. The plugin watches `$HERMES_HOME/images` for complete `dashboard_` image
   files and stores pending batches in
   `$INVENTORY_BASE_DIR/pending-uploads.json`. Optional settings are
   `INVENTORY_UPLOAD_WATCH_INTERVAL_SECONDS` (default `1`),
   `INVENTORY_UPLOAD_BATCH_WINDOW_SECONDS` (default `10`),
   `INVENTORY_PENDING_UPLOAD_TTL_SECONDS` (default `300`), and
   `INVENTORY_UPLOAD_STATE_RETENTION_SECONDS` (default `86400`).

See README.md for storage paths, duplicate behavior, and the required HomeBox API endpoints.
