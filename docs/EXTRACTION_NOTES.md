# Ingestion Notes: Real File Formats

Notes to fold into DESIGN.md. The corpus is now a folder tree of .docx, .pdf,
and .xlsx laid out like a shared drive, not markdown.

## Why this matters architecturally

The Glean Indexing API takes text or HTML in the document body. It does not take
a Word file. So something has to convert binary formats into text plus metadata
before anything reaches Glean, and that conversion layer is where most of the
real work in a custom ingestion lives. Markdown hides that entirely, which is
what made the original corpus unrealistic.

The pipeline doesn't change shape. It gains one stage at the front:

```
source files → extract (text + metadata) → index → verify → search → chat
                   ^
                   one adapter per file type
```

Format is an input-adapter concern, not an architecture concern. That's the line
worth saying out loud in the walkthrough.

## What I would not do

Do not build a Google Drive, SharePoint, or Box integration. Glean ships native
connectors for all three. The Indexing API exists precisely for content that has
no connector, which is the stated scenario in the exercise, "documents not yet
available in Glean." Reading a local folder of real file formats is a faithful
simulation of that. Building an OAuth flow against Drive would burn two days
demonstrating something Glean already sells, and it would blow the "keep the
scope small" instruction.

If asked in Part 2, the answer is that a real deployment picks the connector
when one exists and uses the Indexing API when one doesn't. Knowing which lever
to pull is the actual SA skill.

## Where metadata comes from now

This is the interesting part, and it's a real customer conversation. Markdown
front matter was a cheat. Word files don't have it. Metadata comes from four
places, in descending order of trust:

1. **Embedded document properties.** docx core properties carry title, author,
   category, keywords, created, modified, revision, and last-modified-by. Most
   companies populate maybe half of these. Some populate none.
2. **Folder path.** `Finance/Archive/` tells you department and lifecycle state.
   Often more reliable than anything inside the file.
3. **Filename.** `Expense Policy 2023 (SUPERSEDED).docx` carries status that
   exists nowhere else.
4. **Filesystem stat.** Modified time as a last resort, and a poor one, because
   a bulk migration rewrites every mtime to the same day.

`extraction/walker.py` implements exactly this precedence. Note one deliberate inversion:
**path and filename override embedded status**. A file sitting in `Archive/`
is archived regardless of what its properties claim, because people move files
and forget to update properties far more often than the reverse.

That precedence chain is a defensible design decision with a stated rationale,
which is the kind of thing they said they'd probe.

## Per-format notes

**docx** is the easy case. `python-docx` gives clean paragraph text plus real
core properties. Tables need flattening. I join cells with a pipe so row
association survives into the retrieved snippet, since a table row that becomes
loose words loses the mapping between threshold and approver.

**PDF** is where extraction fidelity becomes a real failure mode. `pdftotext
-layout` handles text-based PDFs fine. It returns almost nothing for scanned
ones, so the extractor flags any PDF under 200 characters as probably needing
OCR rather than indexing an empty document silently. Multi-column layouts and
tables degrade unpredictably. Worth stating in the design note as a known
limitation: PDF text extraction quality directly caps retrieval quality, and
in a real deployment you'd sample-check extraction output before trusting it.

**xlsx** raises a genuine design question with no obvious answer. Is a
spreadsheet one document, or one document per row, or one per sheet? For a
reference table like the approval matrix, one document per workbook with sheets
flattened to labeled sections retrieves well. For something like a customer list
or a ticket export, row-per-document is clearly right. The rule of thumb is
whether a single row is independently meaningful to a reader. Say this out loud,
because most candidates won't have thought about it.

## Why this helps in the live session

"Adding support for a new document type" is listed verbatim in the instructions
as a likely live change. With an adapter registry, adding .pptx is a new
function and one dictionary entry. You'll be able to do it in about three
minutes while narrating, instead of refactoring under pressure.

Same for "a new metadata field." `classification`, `department`, and
`file_type` are already extracted and sitting in the record. Pushing one of them
into the index and surfacing it in citations is a small, clean change with a
sensible business reason behind it.

## Corpus inventory

16 documents, 6 departments, 3 formats.

| Format | Count | Where |
|---|---|---|
| .docx | 13 | People Ops, Finance, Engineering, IT, Customer Success |
| .pdf | 2 | Security (converted from Word, as signed policies usually are) |
| .xlsx | 1 | Finance, the procurement approval matrix |

The superseded expense policy now lives at
`Finance/Archive/Expense Policy 2023 (SUPERSEDED).docx`. Its status comes from
the folder and the filename, not from inside the file, which makes the
retrieval-precision demo more realistic than the markdown version was.
