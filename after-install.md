# hermes-inventory installed

1. Install the required Python dependency in the same environment that runs Hermes:

   `python -m pip install "requests>=2.31,<3"`

2. Configure `HOMEBOX_URL` and `HOMEBOX_API_KEY` in the Hermes runtime environment.

3. Run `hermes model`, then configure the **Inventory vision** auxiliary task. Hermes owns the provider, model, endpoint, credentials, timeout, and fallback settings for this task.

4. Validate the installed plugin with `hermes plugins doctor <plugin-path> --ci`.

See README.md for storage paths, duplicate behavior, and the required HomeBox API endpoints.
