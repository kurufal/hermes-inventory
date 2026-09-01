"""Authoritative image formats supported by attachment, ingest, and vision."""

from pathlib import Path


MIME_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".webp": "image/webp",
}
SUPPORTED_IMAGE_EXTENSIONS = frozenset(MIME_TYPES)


def is_supported_image(path: Path) -> bool:
	return path.suffix.casefold() in SUPPORTED_IMAGE_EXTENSIONS


def mime_type(path: Path) -> str:
	return MIME_TYPES[path.suffix.casefold()]