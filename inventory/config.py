"""Portable, non-secret configuration and storage helpers."""

from __future__ import annotations

import math
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def resolve_hermes_home() -> Path:
	"""Resolve Hermes home without baking a Docker path into the plugin."""
	try:
		from hermes_constants import get_hermes_home
		resolved = get_hermes_home()
		if resolved:
			return Path(resolved).expanduser()
	except (ImportError, AttributeError, TypeError):
		pass
	configured = os.environ.get("HERMES_HOME", "").strip()
	return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _read_config(path: Path) -> dict[str, str]:
	"""Read the supported small YAML subset without making PyYAML mandatory."""
	if not path.is_file():
		return {}
	values: dict[str, str] = {}
	section = ""
	try:
		for raw_line in path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#"):
				continue
			if raw_line == raw_line.lstrip() and line.endswith(":"):
				section = line[:-1].strip()
				continue
			if ":" not in line or not section:
				continue
			key, value = line.split(":", 1)
			values[f"{section}.{key.strip()}"] = value.strip().strip("\"'")
	except OSError:
		return {}
	return values


def _non_negative_float(value: Any, default: float) -> float:
	try:
		parsed = float(value)
	except (TypeError, ValueError):
		return default
	return parsed if math.isfinite(parsed) and parsed >= 0 else default


@dataclass(frozen=True)
class InventorySettings:
	hermes_home: Path
	persistent_data_dir: Path
	runtime_dir: Path
	backup_dir: Path
	config_path: Path
	persistent_source: str
	runtime_source: str
	backup_source: str
	watch_interval_seconds: float
	batch_window_seconds: float
	pending_ttl_seconds: float
	state_retention_seconds: float
	toon_enabled: bool

	@property
	def hermes_images_dir(self) -> Path:
		return self.hermes_home / "images"

	@property
	def pending_upload_state_path(self) -> Path:
		return self.runtime_dir / "pending-uploads.json"

	@property
	def items_dir(self) -> Path:
		return self.persistent_data_dir / "items"


def get_settings() -> InventorySettings:
	hermes_home = resolve_hermes_home()
	config_path = hermes_home / "inventory-config.yaml"
	config = _read_config(config_path)

	def path_setting(env_name: str, config_key: str, default: Path) -> tuple[Path, str]:
		env_value = os.environ.get(env_name, "").strip()
		if env_value:
			return Path(env_value).expanduser(), f"{env_name} environment variable"
		config_value = config.get(config_key, "").strip()
		if config_value:
			return Path(config_value).expanduser(), "plugin config"
		return default, "default"

	persistent, persistent_source = path_setting("INVENTORY_BASE_DIR", "storage.persistent_data_dir", hermes_home / "inventory")
	runtime, runtime_source = path_setting("INVENTORY_RUNTIME_DIR", "storage.runtime_dir", hermes_home / "inventory-runtime")
	backup, backup_source = path_setting("INVENTORY_BACKUP_DIR", "storage.backup_dir", persistent / "backups")
	return InventorySettings(
		hermes_home=hermes_home, persistent_data_dir=persistent, runtime_dir=runtime,
		backup_dir=backup, config_path=config_path, persistent_source=persistent_source,
		runtime_source=runtime_source, backup_source=backup_source,
		watch_interval_seconds=_non_negative_float(os.environ.get("INVENTORY_UPLOAD_WATCH_INTERVAL_SECONDS", config.get("uploads.watch_interval_seconds", 1)), 1),
		batch_window_seconds=_non_negative_float(os.environ.get("INVENTORY_UPLOAD_BATCH_WINDOW_SECONDS", config.get("uploads.batch_window_seconds", 10)), 10),
		pending_ttl_seconds=_non_negative_float(os.environ.get("INVENTORY_PENDING_UPLOAD_TTL_SECONDS", config.get("uploads.pending_ttl_seconds", 300)), 300),
		state_retention_seconds=_non_negative_float(os.environ.get("INVENTORY_UPLOAD_STATE_RETENTION_SECONDS", config.get("uploads.state_retention_seconds", 86400)), 86400),
		toon_enabled=config.get("toon.enabled", "true").casefold() not in {"0", "false", "no"},
	)


def write_storage_config(persistent_data_dir: Path | None) -> None:
	"""Atomically save only the non-secret persistent-storage override."""
	settings = get_settings()
	settings.config_path.parent.mkdir(parents=True, exist_ok=True)
	value = "" if persistent_data_dir is None else str(persistent_data_dir)
	fd, temporary_name = tempfile.mkstemp(prefix=".inventory-config-", suffix=".tmp", dir=settings.config_path.parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			handle.write("storage:\n  persistent_data_dir: " + repr(value) + "\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary_name, settings.config_path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def storage_health(path: Path) -> tuple[bool, str]:
	"""Safely test create, flush, read, rename, and deletion in *path*."""
	probe = path / f".inventory-health-{uuid.uuid4().hex}.tmp"
	renamed = probe.with_suffix(".verified")
	try:
		path.mkdir(parents=True, exist_ok=True)
		list(path.iterdir())
		with probe.open("xb") as handle:
			handle.write(b"hermes-inventory-health-check\n")
			handle.flush()
			os.fsync(handle.fileno())
		if probe.read_bytes() != b"hermes-inventory-health-check\n":
			raise OSError("read-back mismatch")
		os.replace(probe, renamed)
		renamed.unlink()
		return True, "reachable"
	except OSError as exc:
		return False, str(exc)
	finally:
		for candidate in (probe, renamed):
			try:
				candidate.unlink()
			except OSError:
				pass


# Compatibility exports for existing callers. New operational code should use
# get_settings() so changed profiles/environments do not retain stale paths.
_DEFAULTS = get_settings()
HERMES_HOME = _DEFAULTS.hermes_home
INVENTORY_BASE_DIR = _DEFAULTS.persistent_data_dir
DASHBOARD_IMAGES_DIR = _DEFAULTS.hermes_images_dir
HERMES_IMAGES_DIR = DASHBOARD_IMAGES_DIR
PENDING_UPLOAD_STATE_PATH = _DEFAULTS.pending_upload_state_path
UPLOAD_WATCH_INTERVAL_SECONDS = _DEFAULTS.watch_interval_seconds
UPLOAD_BATCH_WINDOW_SECONDS = _DEFAULTS.batch_window_seconds
PENDING_UPLOAD_TTL_SECONDS = _DEFAULTS.pending_ttl_seconds
UPLOAD_STATE_RETENTION_SECONDS = _DEFAULTS.state_retention_seconds
BASE_DIR = INVENTORY_BASE_DIR
ORIGINALS_DIR = INVENTORY_BASE_DIR / "originals"
ITEMS_DIR = INVENTORY_BASE_DIR / "items"
METADATA_DIR = INVENTORY_BASE_DIR / "metadata"
RECEIPTS_DIR = INVENTORY_BASE_DIR / "receipts"
INBOX_DIR = INVENTORY_BASE_DIR / "inbox"
STAGING_DIR = _DEFAULTS.runtime_dir / "tool-staging"
HOMEBOX_URL = os.environ.get("HOMEBOX_URL", "").rstrip("/")
HOMEBOX_API_KEY = os.environ.get("HOMEBOX_API_KEY", "")
HOMEBOX_TIMEOUT_SECONDS = 30
HOMEBOX_ATTACHMENT_TIMEOUT_SECONDS = 120
