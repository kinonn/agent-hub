"""Artifact path and content validation. Never echoes matched secret text."""

from __future__ import annotations

import re
import unicodedata

from src.errors import fail

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bghs_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bah_[A-Za-z0-9_-]{8}_[A-Za-z0-9_-]{32,}\b"),
)
DOCUMENT_KEY_RE = re.compile(r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,79}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
FRONT_MATTER_RE = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|$)", re.DOTALL | re.MULTILINE
)


def slugify_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("-._")
    slug = re.sub(r"\.{2,}", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = re.sub(r"\.md$", "", slug)
    return (slug or "artifact")[:80].rstrip("-._")


def validate_artifact(
    title: str,
    content: str,
    document_key: str | None,
    max_bytes: int,
) -> str:
    if not title.strip() or len(title) > 200 or CONTROL_RE.search(title):
        raise fail("validation_error", "Artifact title is invalid.", 422)
    try:
        encoded = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise fail("validation_error", "Artifact content must be valid UTF-8.", 422) from exc
    if b"\x00" in encoded:
        raise fail("validation_error", "Artifact content may not contain NUL bytes.", 422)
    if len(encoded) > max_bytes:
        raise fail("payload_too_large", "Artifact content exceeds MAX_ARTIFACT_BYTES.", 413)
    if FRONT_MATTER_RE.match(content):
        raise fail("content_policy_violation", "Agent-supplied front matter is not allowed.", 422)
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise fail("content_policy_violation", "Artifact content appears to contain a secret.", 422)
    key = document_key or slugify_title(title)
    if (
        not DOCUMENT_KEY_RE.fullmatch(key)
        or key.lower().endswith(".md")
        or "/" in key
        or "\\" in key
        or "%2f" in key.lower()
    ):
        raise fail("validation_error", "document_key is invalid.", 422)
    return key
