"""Environment-backed configuration for the inventory backend."""

import os
from pathlib import Path


INVENTORY_BASE_DIR = Path(
	os.environ.get(
		"INVENTORY_BASE_DIR",
		"/opt/data/inventory",
	)
).expanduser()

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
