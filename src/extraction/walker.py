"""Walk the corpus and normalize each file into an ExtractedDoc.

Metadata resolution order, most trusted first: embedded document properties,
folder path, filename, filesystem mtime.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from urllib.parse import quote

from extraction.adapters import ADAPTERS
from models.documents import ExtractedDoc

BASE_URL = "https://drive.halcyon.io/shared"


def slugify(text: str) -> str:
    """Shared by the path-derived document ID and the author's datasourceUserId."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def department_from_path(rel_path: str) -> str:
    return rel_path.split(os.sep)[0] if os.sep in rel_path else "Company"


def status_from_path(rel_path: str, filename: str) -> str | None:
    upper = f"{rel_path} {filename}".upper()
    if "ARCHIVE" in upper or "SUPERSEDED" in upper or "(OLD)" in upper:
        return "Archived"
    if "DRAFT" in upper:
        return "Draft"
    return None


def doc_type_from_filename(filename: str) -> str:
    lower = filename.lower()
    for kw, dtype in [
        ("runbook", "runbook"),
        ("policy", "policy"),
        ("process", "process"),
        ("guide", "guide"),
        ("checklist", "guide"),
        ("matrix", "reference"),
    ]:
        if kw in lower:
            return dtype
    return "reference"


def extract(path: str, root: str) -> ExtractedDoc | None:
    """Normalize one file. Returns None for unsupported types rather than raising."""
    rel = os.path.relpath(path, root)
    filename = os.path.basename(path)
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()

    adapter = ADAPTERS.get(ext)
    if adapter is None:
        return None

    raw = adapter(path)
    path_status = status_from_path(rel, filename)
    stat = os.stat(path)

    doc = ExtractedDoc(
        # Derived from path so re-runs upsert rather than duplicate.
        doc_id=raw.get("doc_id") or "PATH-" + slugify(rel),
        title=raw.get("title") or stem,
        body=raw.get("body", ""),
        source_path=rel,
        # Glean 400s the whole batch on one malformed viewURL, and every folder
        # in this corpus has a space in its name.
        view_url=f"{BASE_URL}/{quote(rel.replace(os.sep, '/'))}",
        department=raw.get("department") or department_from_path(rel),
        doc_type=raw.get("doc_type") or doc_type_from_filename(filename),
        classification=raw.get("classification", "Internal"),
        # Path and filename override embedded status: a file in Archive/ is
        # archived whatever its properties claim.
        status=path_status or raw.get("status", "Active"),
        author=raw.get("author"),
        created=raw.get("created"),
        modified=raw.get("modified") or datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        file_type=ext.lstrip("."),
        warnings=raw.get("warnings", []),
    )
    if not doc.body.strip():
        doc.warnings.append("Empty body after extraction.")
    return doc


def walk(root: str) -> list[ExtractedDoc]:
    docs = []
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            if fn.startswith(("~$", ".")):
                continue
            if doc := extract(os.path.join(dirpath, fn), root):
                docs.append(doc)
    return docs
