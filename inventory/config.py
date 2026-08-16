"""Environment-backed configuration for the inventory backend."""

import os
from pathlib import Path


HERMES_HOME = Path(
	os.environ.get(
		"HERMES_HOME",
		"/opt/data",
	)
).expanduser().resolve()

INVENTORY_BASE_DIR = Path(
	os.environ.get(
		"INVENTORY_BASE_DIR",
		str(HERMES_HOME / "inventory"),
	)
).expanduser().resolve()

DASHBOARD_IMAGES_DIR = HERMES_HOME / "images"
DASHBOARD_UPLOAD_WINDOW_SECONDS = int(
	os.environ.get(
		"INVENTORY_DASHBOARD_UPLOAD_WINDOW_SECONDS",
		"120",
	)
)
DASHBOARD_BURST_SECONDS = int(
	os.environ.get(
		"INVENTORY_DASHBOARD_BURST_SECONDS",
		"10",
	)
)
DASHBOARD_UPLOAD_STATE_PATH = (
	INVENTORY_BASE_DIR / "dashboard-upload-state.json"
)

# Backward-compatible name for callers that used the former backend module.
BASE_DIR = INVENTORY_BASE_DIR

ORIGINALS_DIR = INVENTORY_BASE_DIR / "originals"
METADATA_DIR = INVENTORY_BASE_DIR / "metadata"
RECEIPTS_DIR = INVENTORY_BASE_DIR / "receipts"
INBOX_DIR = INVENTORY_BASE_DIR / "inbox"
STAGING_DIR = INVENTORY_BASE_DIR / "tool-staging"

HOMEBOX_URL = os.environ.get(
	"HOMEBOX_URL",
	"",
).rstrip("/")

HOMEBOX_API_KEY = os.environ.get(
	"HOMEBOX_API_KEY",
	"",
)

HOMEBOX_TIMEOUT_SECONDS = 30
HOMEBOX_ATTACHMENT_TIMEOUT_SECONDS = 120
