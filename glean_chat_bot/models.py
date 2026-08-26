from pydantic import BaseModel, Field

STATUS_PROPERTY = "halcyonStatus"
ACTIVE_STATUS = "Active"
ARCHIVED_STATUS = "Archived"
DRAFT_STATUS = "Draft"
STATUSES = frozenset({ACTIVE_STATUS, ARCHIVED_STATUS, DRAFT_STATUS})


class ExtractedDoc(BaseModel):
    """One local file, normalized into what the indexing API wants."""
    doc_id: str
    title: str
    body: str
    source_path: str
    view_url: str
    department: str
    doc_type: str = "reference"
    classification: str = "Internal"
    status: str = ACTIVE_STATUS
    author: str | None = None
    created: str | None = None
    modified: str | None = None
    file_type: str = ""
    warnings: list[str] = Field(default_factory=list)


class Passage(BaseModel):
    """One retrieved chunk of one document, as returned by Search."""
    marker: int  # 1-based; the [n] the model is told to cite
    doc_id: str
    title: str
    url: str
    text: str


class Source(BaseModel):
    """A citation the model emitted, after we tried to resolve it."""
    marker: int
    resolved: bool
    doc_id: str | None = None
    title: str | None = None
    url: str | None = None


class Answer(BaseModel):
    """The envelope the MCP tool returns."""
    answer: str
    sources: list[Source] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)
