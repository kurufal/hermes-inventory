"""Inventory-owned portable ZIP backups and integrity verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import get_settings, storage_health


BACKUP_SCHEMA = "hermes-inventory-backup"
SCHEMA_VERSION = 1


def _sha256_bytes(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _safe_sources(root: Path, backup_dir: Path):
	for path in root.rglob("*"):
		if not path.is_file() or path.is_symlink():
			continue
		try:
			path.relative_to(backup_dir)
			continue
		except ValueError:
			yield path


def create_backup() -> dict:
	settings = get_settings()
	ok, reason = storage_health(settings.persistent_data_dir)
	if not ok:
		raise RuntimeError(f"Persistent inventory storage is unavailable: {reason}")
	ok, reason = storage_health(settings.backup_dir)
	if not ok:
		raise RuntimeError(f"Backup storage is unavailable: {reason}")
	stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
	final_path = settings.backup_dir / f"hermes-inventory-backup-{stamp}.zip"
	fd, temporary_name = tempfile.mkstemp(prefix=".inventory-backup-", suffix=".tmp", dir=settings.backup_dir)
	os.close(fd)
	checksums: dict[str, str] = {}
	try:
		with zipfile.ZipFile(temporary_name, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
			for source in _safe_sources(settings.persistent_data_dir, settings.backup_dir):
				arcname = Path("inventory") / source.relative_to(settings.persistent_data_dir)
				data = source.read_bytes()
				archive.writestr(str(arcname).replace("\\", "/"), data)
				checksums[str(arcname).replace("\\", "/")] = _sha256_bytes(data)
			manifest = {"schema": BACKUP_SCHEMA, "schema_version": SCHEMA_VERSION, "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "plugin_version": "0.2.0", "source_inventory_path": str(settings.persistent_data_dir), "files": checksums, "native_homebox_export": {"included": False, "status": "unavailable", "reason": "No verified public HomeBox export API is configured."}}
			archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
		os.replace(temporary_name, final_path)
		archive_hash = _sha256_bytes(final_path.read_bytes())
		final_path.with_suffix(".zip.sha256").write_text(f"{archive_hash}  {final_path.name}\n", encoding="ascii")
		return {"status": "PASS", "path": str(final_path), "file_count": len(checksums), "native_homebox_export": "unavailable"}
	except Exception:
		try:
			Path(temporary_name).unlink()
		except OSError:
			pass
		raise


def verify_backup(path: Path) -> dict:
	try:
		with zipfile.ZipFile(path) as archive:
			names = archive.namelist()
			if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
				return {"status": "FAIL", "error": "Archive contains unsafe member paths."}
			if "backup-manifest.json" not in names:
				return {"status": "FAIL", "error": "Archive has no backup manifest."}
			manifest = json.loads(archive.read("backup-manifest.json"))
			if manifest.get("schema") != BACKUP_SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
				return {"status": "FAIL", "error": "Unsupported backup manifest schema."}
			failures = [name for name, digest in manifest.get("files", {}).items() if name not in names or _sha256_bytes(archive.read(name)) != digest]
			return {"status": "PASS" if not failures else "FAIL", "path": str(path), "checked_files": len(manifest.get("files", {})), "failures": failures}
	except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
		return {"status": "FAIL", "path": str(path), "error": str(exc)}


def list_backups() -> list[Path]:
	backup_dir = get_settings().backup_dir
	return sorted(backup_dir.glob("hermes-inventory-backup-*.zip"), reverse=True) if backup_dir.exists() else []