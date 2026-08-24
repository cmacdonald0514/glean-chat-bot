"""Custom datasource definition: object type and the properties documents carry."""

from __future__ import annotations

import logging
import re

from glean.api_client import Glean, models

from config import Settings
from extraction.walker import BASE_URL
from logs import log_call

log = logging.getLogger("glean_chat_bot.indexing")

OBJECT_TYPE = "Document"

# (property name, display label, ExtractedDoc attribute, show as facet).
# Names are prefixed because Glean reserves a set of operator names and rejects
# any custom property that collides - "department" does.
CUSTOM_PROPERTIES = [
    ("halcyonDepartment", "Department", "department", True),
    ("halcyonDocType", "Document type", "doc_type", True),
    ("halcyonClassification", "Classification", "classification", True),
    ("halcyonStatus", "Status", "status", True),
    ("halcyonFileType", "File type", "file_type", True),
    ("halcyonSourcePath", "Source path", "source_path", False),
]


def ensure_datasource(client: Glean, settings: Settings) -> None:
    """Create or update the datasource definition. /adddatasource is an upsert, so this re-runs."""
    with log_call("indexing.datasources.add", datasource=settings.datasource):
        client.indexing.datasources.add(
            name=settings.datasource,
            display_name="Halcyon Shared Drive",
            # Must not be UNCATEGORIZED - Glean treats category as a relevance signal.
            datasource_category=models.DatasourceCategory.PUBLISHED_CONTENT,
            # How Glean recognises URLs as belonging to this datasource. Derived
            # from the extractor's BASE_URL; if the two drift, documents index
            # fine but attribution silently breaks.
            url_regex=re.escape(BASE_URL) + "/.*",
            # Every object_type a document uses must be declared here first, or
            # the whole batch fails with "Object definitions not found".
            object_definitions=[
                models.ObjectDefinition(
                    name=OBJECT_TYPE,
                    display_label="Document",
                    doc_category=models.DocCategory.PUBLISHED_CONTENT,
                    property_definitions=[
                        models.PropertyDefinition(
                            name=name,
                            display_label=label,
                            property_type=models.PropertyDefinitionPropertyType.TEXT,
                            ui_options=models.UIOptions.SEARCH_RESULT,
                            hide_ui_facet=not facet,
                        )
                        for name, label, _attr, facet in CUSTOM_PROPERTIES
                    ],
                    summarizable=True,
                )
            ],
            is_test_datasource=False,
        )
    log.info("datasource %s ensured", settings.datasource)
