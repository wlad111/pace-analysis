"""Parsing of Apex Timing result emails.

Public entry points (SPEC section 8.1)::

    from karting.parsing import parse_email_file, parse_email_bytes, parse_html
"""

from __future__ import annotations

from .apex_email import parse_email_bytes, parse_email_file, parse_html
from .timeparse import format_duration, parse_duration, parse_gap

__all__ = [
    "parse_email_bytes",
    "parse_email_file",
    "parse_html",
    "parse_duration",
    "format_duration",
    "parse_gap",
]
