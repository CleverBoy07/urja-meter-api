"""Decoder for SvelteKit's ``__data.json`` payloads (the "devalue" flattened format).

A node's ``data`` is a flat array; element 0 is the root value and every object
property / array item is an *index* into that same array. Negative indices are
sentinels (undefined, holes, NaN, ±Infinity, -0).
"""

from __future__ import annotations

import math
from typing import Any

_SENTINELS: dict[int, Any] = {
    -1: None,  # undefined
    -2: None,  # array hole
    -3: math.nan,
    -4: math.inf,
    -5: -math.inf,
    -6: -0.0,
}


def unflatten(values: list[Any]) -> Any:
    cache: dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if index in _SENTINELS:
            return _SENTINELS[index]
        if index in cache:
            return cache[index]
        raw = values[index]
        if isinstance(raw, dict):
            obj: dict[str, Any] = {}
            cache[index] = obj
            for key, child in raw.items():
                obj[key] = hydrate(child)
            return obj
        if isinstance(raw, list):
            # Typed values look like ["Date", "..."]; we only need plain arrays here.
            if raw and isinstance(raw[0], str):
                cache[index] = raw[1] if len(raw) > 1 else None
                return cache[index]
            arr: list[Any] = []
            cache[index] = arr
            arr.extend(hydrate(child) for child in raw)
            return arr
        cache[index] = raw
        return raw

    return hydrate(0)


def page_data(payload: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    """Return (data, error) for the leaf node of a ``__data.json`` response."""
    nodes = payload.get("nodes") or []
    leaf = nodes[-1] if nodes else None
    if not leaf:
        return None, None
    if leaf.get("type") == "error":
        return None, {"status": leaf.get("status", 500), **(leaf.get("error") or {})}
    if leaf.get("type") == "data":
        return unflatten(leaf["data"]), None
    return None, None
