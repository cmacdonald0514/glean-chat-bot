"""Document shapes: what the extractor produces and what retrieval returns."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExtractedDoc:
    doc_id: str
    title: str
    body: str
    source_path: str
    view_url: str
    department: str
    doc_type: str = "reference"
    classification: str = "Internal"
    status: str = "Active"
    author: str | None = None
    created: str | None = None
    modified: str | None = None
    file_type: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class Passage:
    """One retrieved chunk of one document, as returned by Search."""

    marker: int  # 1-based; the [n] the model is told to cite
    doc_id: str
    title: str
    url: str
    text: str
