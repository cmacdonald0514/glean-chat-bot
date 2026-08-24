"""Answer shapes: a resolved citation and the envelope the MCP tool returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Source:
    """A citation the model emitted, after we tried to resolve it."""

    marker: int
    resolved: bool
    doc_id: str | None = None
    title: str | None = None
    url: str | None = None


@dataclass
class Answer:
    answer: str
    sources: list[Source] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
