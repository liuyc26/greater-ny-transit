"""Decide whether a trip commutes to Manhattan and in which direction.

A trip's "core" stops are those in Manhattan or at a configured transfer hub.
"""

import re

from .models import Direction


def normalize_stop_name(name: str) -> str:
    """Canonical form for matching stop names across sources ("New York Penn Sta." == "NEW YORK PENN STA")."""
    return re.sub(r"[^A-Z0-9]+", " ", name.upper()).strip()


def classify_direction(
    first_seq: int, last_seq: int, min_core_seq: int | None, max_core_seq: int | None
) -> Direction | None:
    """Return the trip's direction relative to Manhattan, or None if it never reaches it.

    inbound:  the trip ends at a core stop (e.g. Ronkonkoma -> Penn Station)
    outbound: the trip starts at a core stop (e.g. Grand Central -> New Haven)
    through:  core stops only in the middle, or at both ends
    """
    if min_core_seq is None or max_core_seq is None:
        return None
    starts_in_core = min_core_seq == first_seq
    ends_in_core = max_core_seq == last_seq
    if ends_in_core and not starts_in_core:
        return "inbound"
    if starts_in_core and not ends_in_core:
        return "outbound"
    return "through"
