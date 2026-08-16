"""Environment-backed configuration for the inventory backend."""

import math
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


def _non_negative_float(name: str, default: float) -> float:
	try:
		value = float(os.environ.get(name, str(default)))
	except (TypeError, ValueError):
		return default
	return value if math.isfinite(value) and value >= 0 else default


PENDING_UPLOAD_STATE_PATH = INVENTORY_BASE_DIR / "pending-uploads.json"
UPLOAD_WATCH_INTERVAL_SECONDS = _non_negative_float(
	"INVENTORY_UPLOAD_WATCH_INTERVAL_SECONDS",
	1.0,
)
UPLOAD_BATCH_WINDOW_SECONDS = _non_negative_float(
	"INVENTORY_UPLOAD_BATCH_WINDOW_SECONDS",
	10.0,
)
PENDING_UPLOAD_TTL_SECONDS = _non_negative_float(
	"INVENTORY_PENDING_UPLOAD_TTL_SECONDS",
	300.0,
)
UPLOAD_STATE_RETENTION_SECONDS = _non_negative_float(
	"INVENTORY_UPLOAD_STATE_RETENTION_SECONDS",
	86400.0,
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
