"""SQLite storage for parsed Apex Timing emails.

Public surface (see SPEC 8.2)::

    from karting.storage import open_db, Database, ImportReport

    with open_db("data/pace.db") as db:
        report = db.import_parsed(parsed, raw_bytes=raw)

Lap annotations come from two sources (SPEC 10.3): ``manual`` rows written by a
human and ``auto`` rows written by the joker/pit detector, which the importer
runs at the end of every successful import.  ``Database.lap_tags`` answers with
the *effective* tags (manual wins over auto, per lap), ``lap_annotations`` with
the raw rows of both sources.
"""

from __future__ import annotations

from karting.storage.db import (
    ANNOTATION_SOURCES,
    AUTO_SOURCE,
    DEFAULT_DB_PATH,
    KNOWN_LAP_TAGS,
    MANUAL_SOURCE,
    OVERRIDE_TAG,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    Database,
    DetectorUnavailableError,
    ImportReport,
    NoSessionIdentityError,
    StorageError,
    UnknownLapError,
    UnknownTagError,
    content_digest,
    open_db,
)

__all__ = [
    "ANNOTATION_SOURCES",
    "AUTO_SOURCE",
    "DEFAULT_DB_PATH",
    "KNOWN_LAP_TAGS",
    "MANUAL_SOURCE",
    "OVERRIDE_TAG",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "Database",
    "DetectorUnavailableError",
    "ImportReport",
    "NoSessionIdentityError",
    "StorageError",
    "UnknownLapError",
    "UnknownTagError",
    "content_digest",
    "open_db",
]
