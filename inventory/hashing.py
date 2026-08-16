"""SHA-256 hashing for source inventory images."""

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
	h = hashlib.sha256()

	with path.open("rb") as f:
		for chunk in iter(lambda: f.read(1024 * 1024), b""):
			h.update(chunk)

	return h.hexdigest()


def hash_images(base_dir: Path, filenames: list[str]) -> list[dict]:
	results = []

	for filename in filenames:
		path = base_dir / filename

		if not path.exists():
			results.append({
				"filename": filename,
				"exists": False,
				"sha256": "",
			})
			continue

		results.append({
			"filename": filename,
			"exists": True,
			"sha256": sha256_file(path),
		})

	return results
