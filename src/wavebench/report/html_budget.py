"""Admission before report text copies; this is an estimate, not an RSS limit."""
from __future__ import annotations

import sys

from wavebench.data.analysis_resources import AnalysisLimits


def retained_size(value) -> int:
    """Count owned Python payloads conservatively, including repeated references."""
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(retained_size(k) + retained_size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return size + sum(retained_size(v) for v in value)
    if hasattr(value, "__dataclass_fields__"):
        return size + retained_size(vars(value))
    return size


def escaped_length(text: str) -> int:
    """Length after html.escape(quote=True), without allocating its copies."""
    return (len(text) + 4 * text.count("&") + 3 * (text.count("<") + text.count(">"))
            + 5 * (text.count('"') + text.count("'")))


def check_text_assembly(parts, limits, *, retained_bytes=0, stage="report HTML assembly"):
    """Reserve inputs, two Unicode output buffers, and a UTF-8 encoding buffer.

    format_map can grow/copy its writer; join needs only one result. Charging
    both buffers also covers a later text writer while the Unicode result lives.
    Character scanning allocates no encoded copy during admission.
    """
    limits = limits or AnalysisLimits()
    chars = sum(len(part) for part in parts)
    largest = max((max(map(ord, part), default=0) for part in parts), default=0)
    width = 1 if largest <= 255 else 2 if largest <= 65535 else 4
    utf8_width = 1 if largest <= 127 else 2 if largest <= 2047 else 3 if largest <= 65535 else 4
    output = 80 + width * chars
    requested = (retained_bytes + retained_size(parts) + 2 * output
                 + 33 + utf8_width * chars + 4096)
    limits.check("max_working_bytes", requested, stage)


def bounded_format(template, values, limits, *, retained_bytes=0):
    # Template placeholders and doubled CSS braces make this length estimate
    # larger than the actual result; all replacement values occur once.
    check_text_assembly(
        [template, *values.values()], limits,
        retained_bytes=retained_bytes, stage="final report HTML assembly/encoding",
    )
    return template.format_map(values)
