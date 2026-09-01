"""Inventory-owned portable ZIP backups and integrity verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from pathlib import PurePosixPath

from inventory.config import get_settings, storage_health
from inventory.constants import BACKUP_SCHEMA, PLUGIN_VERSION, SCHEMA_VERSION
from inventory.hashing import sha256_file


_BACKUP_LOCK = threading.Lock()


def _sha256_stream(handle) -> str:
	digest = hashlib.sha256()
	for chunk in iter(lambda: handle.read(1024 * 1024), b""):
		digest.update(chunk)
	return digest.hexdigest()


def _safe_sources(root: Path, backup_dir: Path):
	try:
		backup_dir.relative_to(root)
		exclude_backup_subtree = True
	except ValueError:
		exclude_backup_subtree = False
	for path in root.rglob("*"):
		if not path.is_file() or path.is_symlink():
			continue
		if exclude_backup_subtree:
			try:
				path.relative_to(backup_dir)
				continue
			except ValueError:
				pass
		yield path


def _safe_member_name(name: str) -> bool:
	member = PurePosixPath(name.replace("\\", "/"))
	return not member.is_absolute() and ".." not in member.parts and not (member.parts and ":" in member.parts[0])


def create_backup() -> dict:
	if not _BACKUP_LOCK.acquire(blocking=False):
		raise RuntimeError("An Inventory backup is already in progress.")
	try:
		return _create_backup()
	finally:
		_BACKUP_LOCK.release()


def _create_backup() -> dict:
	settings = get_settings()
	ok, reason = storage_health(settings.persistent_data_dir)
	if not ok:
		raise RuntimeError(f"Persistent inventory storage is unavailable: {reason}")
	ok, reason = storage_health(settings.backup_dir)
	if not ok:
		raise RuntimeError(f"Backup storage is unavailable: {reason}")
	stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
	backup_id = f"{stamp}-{os.getpid()}-{next(tempfile._get_candidate_names())}"
	final_path = settings.backup_dir / f"hermes-inventory-backup-{backup_id}.zip"
	fd, temporary_name = tempfile.mkstemp(prefix=f".tmp-{backup_id}-", suffix=".zip", dir=settings.backup_dir)
	os.close(fd)
	checksums: dict[str, str] = {}
	try:
		with zipfile.ZipFile(temporary_name, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
			for source in _safe_sources(settings.persistent_data_dir, settings.backup_dir):
				arcname = Path("inventory") / source.relative_to(settings.persistent_data_dir)
				arcname = str(arcname).replace("\\", "/")
				archive.write(source, arcname)
				checksums[arcname] = sha256_file(source)
			manifest = {"schema": BACKUP_SCHEMA, "schema_version": SCHEMA_VERSION, "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "plugin_version": PLUGIN_VERSION, "source_inventory_path": str(settings.persistent_data_dir), "inventory_item_count": sum(1 for _ in settings.items_dir.glob("*/item.json")) if settings.items_dir.exists() else 0, "image_count": sum(1 for path in settings.items_dir.rglob("*") if path.is_file() and path.parent.name == "images") if settings.items_dir.exists() else 0, "included_sections": ["inventory"], "files": checksums, "native_homebox_export": {"included": False, "status": "unavailable", "reason": "No verified public HomeBox export API is configured."}}
			archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
		os.replace(temporary_name, final_path)
		archive_hash = sha256_file(final_path)
		final_path.with_suffix(".zip.sha256").write_text(f"{archive_hash}  {final_path.name}\n", encoding="ascii")
		return {"status": "WARN", "path": str(final_path), "file_count": len(checksums), "sha256": archive_hash, "native_homebox_export": "unavailable"}
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
			if any(not _safe_member_name(name) for name in names):
				return {"status": "FAIL", "error": "Archive contains unsafe member paths."}
			if "backup-manifest.json" not in names:
				return {"status": "FAIL", "error": "Archive has no backup manifest."}
			manifest = json.loads(archive.read("backup-manifest.json"))
			if manifest.get("schema") != BACKUP_SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
				return {"status": "FAIL", "error": "Unsupported backup manifest schema."}
			failures = []
			for name, digest in manifest.get("files", {}).items():
				if name not in names:
					failures.append(name)
					continue
				with archive.open(name) as member:
					if _sha256_stream(member) != digest:
						failures.append(name)
			sidecar = path.with_suffix(".zip.sha256")
			if sidecar.is_file() and sidecar.read_text(encoding="ascii").split()[0] != sha256_file(path):
				failures.append(sidecar.name)
			return {"status": "PASS" if not failures else "FAIL", "path": str(path), "checked_files": len(manifest.get("files", {})), "failures": failures}
	except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
		return {"status": "FAIL", "path": str(path), "error": str(exc)}


def list_backups() -> list[Path]:
	backup_dir = get_settings().backup_dir
	return sorted(backup_dir.glob("hermes-inventory-backup-*.zip"), reverse=True) if backup_dir.exists() else []