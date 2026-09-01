"""Portable, non-secret configuration and storage helpers."""

from __future__ import annotations

import math
import os
import json
import re
import sys
import tempfile
import uuid
from urllib.parse import urlparse
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


class ConfigurationError(ValueError):
	"""Raised when plugin-owned non-secret configuration is invalid."""


def _read_json_config(path: Path) -> dict[str, Any]:
	if not path.is_file():
		return {}
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise ConfigurationError(f"Malformed inventory configuration {path}: {exc}") from exc
	if not isinstance(payload, dict):
		raise ConfigurationError(f"Inventory configuration {path} must be a JSON object.")
	return payload


def _read_legacy_yaml(path: Path) -> dict[str, Any]:
	"""Read legacy config only with a real YAML parser; never emulate YAML."""
	if not path.is_file():
		return {}
	try:
		import yaml
	except ImportError as exc:
		raise ConfigurationError(
			f"Legacy configuration {path} requires PyYAML for one-time migration; "
			"create inventory-config.json instead."
		) from exc
	try:
		payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
	except (OSError, yaml.YAMLError) as exc:
		raise ConfigurationError(f"Malformed legacy inventory configuration {path}: {exc}") from exc
	if not isinstance(payload, dict):
		raise ConfigurationError(f"Legacy configuration {path} must be a mapping.")
	return payload


def _nested_value(payload: dict[str, Any], section: str, key: str, default: Any = "") -> Any:
	value = payload.get(section, {})
	return value.get(key, default) if isinstance(value, dict) else default


def _load_config_document(hermes_home: Path) -> tuple[dict[str, Any], Path]:
	json_path = hermes_home / "inventory-config.json"
	if json_path.is_file():
		return _read_json_config(json_path), json_path
	return _read_legacy_yaml(hermes_home / "inventory-config.yaml"), json_path


def environment_value(name: str) -> str:
	"""Read a Hermes-managed environment value without import-time caching."""
	try:
		from hermes_cli.config import get_env_value
		value = get_env_value(name)
		if value is not None:
			return str(value)
	except (ImportError, AttributeError, TypeError):
		try:
			from hermes_constants import get_environment_value
			value = get_environment_value(name)
			if value is not None:
				return str(value)
		except (ImportError, AttributeError, TypeError):
			pass
	return os.environ.get(name, "")


def _boolean(value: Any, name: str, default: bool = False) -> bool:
	if value is None or value == "":
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, str):
		if value.casefold() in {"true", "1", "yes"}:
			return True
		if value.casefold() in {"false", "0", "no"}:
			return False
	raise ConfigurationError(f"{name} must be a boolean")


def _is_absolute_storage_path(value: str, path: Path) -> bool:
	return path.is_absolute() or value.startswith("\\\\") or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _configured_path(value: Any, name: str) -> Path | None:
	if not isinstance(value, str) or not value.strip():
		return None
	path = Path(value)
	if not _is_absolute_storage_path(value, path):
		raise ConfigurationError(f"{name} must be an absolute path: {value}")
	return path.expanduser()


def trusted_attachment_roots(settings: "InventorySettings") -> tuple[Path, ...]:
	"""Return local Hermes-owned attachment directories accepted by the tool."""
	roots = [settings.hermes_images_dir]
	if sys.platform == "win32":
		appdata = os.environ.get("APPDATA", "").strip()
		if appdata:
			roots.append(Path(appdata) / "Hermes" / "composer-images")
	elif sys.platform == "darwin":
		roots.append(Path.home() / "Library" / "Application Support" / "Hermes" / "composer-images")
	else:
		roots.append(Path.home() / ".config" / "Hermes" / "composer-images")
	return tuple(roots)


def normalize_homebox_url(value: str) -> str:
	"""Accept a plain URL or Hermes @url Markdown wrapper, never arbitrary text."""
	if not isinstance(value, str):
		raise ConfigurationError("HomeBox URL must be an HTTP or HTTPS URL")
	text = value.strip()
	match = re.fullmatch(r"@url:`?\[([^\]]+)\]\((https?://[^)]+)\)`?", text)
	if match:
		label, target = match.groups()
		if label != target:
			raise ConfigurationError("HomeBox URL link text and target must match")
		text = target
	parsed = urlparse(text)
	if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
		raise ConfigurationError("HomeBox URL must be an HTTP or HTTPS URL")
	return text.rstrip("/")


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
	config, config_path = _load_config_document(hermes_home)

	def path_setting(env_name: str, config_key: str, default: Path) -> tuple[Path, str]:
		env_value = _configured_path(os.environ.get(env_name, ""), env_name)
		if env_value is not None:
			return env_value, f"{env_name} environment variable"
		section, key = config_key.split(".", 1)
		config_value = _configured_path(_nested_value(config, section, key), config_key)
		if config_value is not None:
			return config_value, "plugin config"
		return default, "default"

	persistent, persistent_source = path_setting("INVENTORY_BASE_DIR", "storage.persistent_data_dir", hermes_home / "inventory")
	runtime, runtime_source = path_setting("INVENTORY_RUNTIME_DIR", "storage.runtime_dir", hermes_home / "inventory-runtime")
	backup, backup_source = path_setting("INVENTORY_BACKUP_DIR", "storage.backup_dir", persistent / "backups")
	return InventorySettings(
		hermes_home=hermes_home, persistent_data_dir=persistent, runtime_dir=runtime,
		backup_dir=backup, config_path=config_path, persistent_source=persistent_source,
		runtime_source=runtime_source, backup_source=backup_source,
		watch_interval_seconds=_non_negative_float(os.environ.get("INVENTORY_UPLOAD_WATCH_INTERVAL_SECONDS", _nested_value(config, "uploads", "watch_interval_seconds", 1)), 1),
		batch_window_seconds=_non_negative_float(os.environ.get("INVENTORY_UPLOAD_BATCH_WINDOW_SECONDS", _nested_value(config, "uploads", "batch_window_seconds", 10)), 10),
		pending_ttl_seconds=_non_negative_float(os.environ.get("INVENTORY_PENDING_UPLOAD_TTL_SECONDS", _nested_value(config, "uploads", "pending_ttl_seconds", 300)), 300),
		state_retention_seconds=_non_negative_float(os.environ.get("INVENTORY_UPLOAD_STATE_RETENTION_SECONDS", _nested_value(config, "uploads", "state_retention_seconds", 86400)), 86400),
		toon_enabled=_boolean(_nested_value(config, "toon", "enabled", False), "toon.enabled"),
	)


def homebox_url(settings: InventorySettings | None = None) -> str:
	"""Resolve the non-secret HomeBox URL: environment then plugin config."""
	settings = settings or get_settings()
	configured = environment_value("HOMEBOX_URL") or _nested_value(
		_load_config_document(settings.hermes_home)[0], "homebox", "url", ""
	)
	return normalize_homebox_url(configured) if configured else ""


def homebox_api_key() -> str:
	value = environment_value("HOMEBOX_API_KEY")
	if value:
		return value
	try:
		for line in (resolve_hermes_home() / ".env").read_text(encoding="utf-8").splitlines():
			if line.startswith("HOMEBOX_API_KEY="):
				return line.partition("=")[2]
	except OSError:
		pass
	return ""


def _write_config_document(settings: InventorySettings, payload: dict[str, Any]) -> None:
	settings.config_path.parent.mkdir(parents=True, exist_ok=True)
	fd, temporary_name = tempfile.mkstemp(prefix=".inventory-config-", suffix=".tmp", dir=settings.config_path.parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary_name, settings.config_path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def write_storage_config(persistent_data_dir: Path | None) -> None:
	"""Atomically merge the non-secret persistent-storage override."""
	settings = get_settings()
	payload, _ = _load_config_document(settings.hermes_home)
	storage = payload.setdefault("storage", {})
	if not isinstance(storage, dict):
		raise ConfigurationError("storage must be an object in inventory-config.json")
	storage["persistent_data_dir"] = "" if persistent_data_dir is None else str(_configured_path(str(persistent_data_dir), "storage.persistent_data_dir"))
	_write_config_document(settings, payload)


def storage_root_for_setup(value: str) -> Path:
	"""Treat a setup storage value as a parent unless it is already an Inventory root."""
	path = _configured_path(value, "storage.persistent_data_dir")
	if path is None:
		raise ConfigurationError("Storage path must not be empty")
	return path if path.name.casefold().endswith("inventory") else path / "inventory"


def write_homebox_url(url: str) -> None:
	"""Atomically save the normal, non-secret HomeBox URL unchanged."""
	if not isinstance(url, str) or not url.strip():
		raise ConfigurationError("HomeBox URL must not be empty")
	settings = get_settings()
	payload, _ = _load_config_document(settings.hermes_home)
	homebox = payload.setdefault("homebox", {})
	if not isinstance(homebox, dict):
		raise ConfigurationError("homebox must be an object in inventory-config.json")
	homebox["url"] = normalize_homebox_url(url)
	_write_config_document(settings, payload)


def _dotenv_path(settings: InventorySettings) -> Path:
	return settings.hermes_home / ".env"


def _write_dotenv_secret(name: str, value: str, settings: InventorySettings) -> None:
	"""Compatibility writer for Hermes' normal environment file."""
	path = _dotenv_path(settings)
	lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
	prefix = f"{name}="
	updated = False
	output = []
	for line in lines:
		if line.startswith(prefix):
			output.append(prefix + value)
			updated = True
		else:
			output.append(line)
	if not updated:
		output.append(prefix + value)
	path.parent.mkdir(parents=True, exist_ok=True)
	fd, temporary_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			handle.write("\n".join(output) + "\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary_name, path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def write_homebox_api_key(value: str, settings: InventorySettings | None = None) -> None:
	if not value:
		return
	settings = settings or get_settings()
	try:
		from hermes_cli.config import save_env_value
		save_env_value("HOMEBOX_API_KEY", value)
		return
	except (ImportError, AttributeError, TypeError):
		try:
			from hermes_constants import set_environment_value
			set_environment_value("HOMEBOX_API_KEY", value)
			return
		except (ImportError, AttributeError, TypeError):
			_write_dotenv_secret("HOMEBOX_API_KEY", value, settings)
			os.environ["HOMEBOX_API_KEY"] = value


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
