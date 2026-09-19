"""Actress alias normalization helpers."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from javsp.datatype import MovieInfo
from javsp.lib import resource_path


def load_actress_alias_map(path: str | None = None) -> dict[str, list[str]]:
    alias_path = path or resource_path("data/actress_alias.json")
    with open(alias_path, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        return {}
    return {
        str(name): [str(alias) for alias in aliases]
        for name, aliases in data.items()
        if isinstance(aliases, list)
    }


def build_reverse_alias_map(alias_map: dict[str, list[str]]) -> dict[str, str]:
    reverse: dict[str, str] = {}
    for fixed_name, aliases in alias_map.items():
        reverse.setdefault(fixed_name, fixed_name)
        for alias in aliases:
            reverse.setdefault(alias, fixed_name)
    return reverse


def resolve_alias(name: str | None, alias_map: dict[str, list[str]] | None = None) -> str | None:
    if name is None:
        return None
    reverse = build_reverse_alias_map(alias_map or load_actress_alias_map())
    return reverse.get(name, name)


def _unique_keep_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def normalize_movie_info_actress(
    info: MovieInfo,
    alias_map: dict[str, list[str]] | None = None,
) -> tuple[MovieInfo, bool, dict[str, Any]]:
    """Normalize actress names and actress_pics keys in place.

    Returns ``(info, changed, details)``.
    """
    aliases = alias_map or load_actress_alias_map()
    reverse = build_reverse_alias_map(aliases)
    before = {
        "actress": deepcopy(getattr(info, "actress", None)),
        "actress_pics": deepcopy(getattr(info, "actress_pics", None)),
    }

    if info.actress:
        info.actress = _unique_keep_order([reverse.get(name, name) for name in info.actress])

    if isinstance(info.actress_pics, dict) and info.actress_pics:
        normalized_pics: dict[str, str] = {}
        for name, pic in info.actress_pics.items():
            fixed_name = reverse.get(name, name)
            normalized_pics.setdefault(fixed_name, pic)
        info.actress_pics = normalized_pics

    after = {
        "actress": deepcopy(getattr(info, "actress", None)),
        "actress_pics": deepcopy(getattr(info, "actress_pics", None)),
    }
    return info, before != after, {"before": before, "after": after}
