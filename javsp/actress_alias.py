"""Actress alias normalization helpers."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
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


# 仅管理入口调用写入函数；加载和归一化始终只读。

_alias_write_lock = threading.Lock()


class AliasConflict(ValueError):
    pass


def read_alias_document(path: str | None = None) -> tuple[dict, str]:
    raw = Path(path or resource_path("data/actress_alias.json")).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or any(
        not isinstance(k, str) or not isinstance(v, list) or
        any(not isinstance(a, str) for a in v) for k, v in data.items()
    ):
        raise ValueError("别名库格式错误，未修改文件")
    return data, hashlib.sha256(raw).hexdigest()


def alias_owners(data: dict, names: list[str]) -> dict[str, list[str]]:
    wanted = set(names)
    owners = {n: [] for n in names}
    for fixed, aliases in data.items():
        for name in wanted.intersection([fixed, *aliases]):
            owners[name].append(fixed)
    return owners


def prepare_alias_change(data: dict, name: str, aliases: list[str],
                         original_name: str | None = None, mode: str = "append") -> tuple[dict, dict]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("请填写统一名字")
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise ValueError("别名必须是名字列表")
    if mode not in {"append", "edit"}:
        raise ValueError("无效的保存方式")
    name = name.strip()
    aliases = _unique_keep_order([a.strip() for a in aliases if a.strip()])
    if original_name is not None and original_name not in data:
        raise AliasConflict("原记录不存在，请重新加载")
    if original_name is None and name in data:
        raise AliasConflict("统一名字已有记录，请先选择该记录再维护")
    target = original_name or name
    old = data.get(target, [])
    names = [name, *aliases]
    if mode == "append":
        names.extend(old)
    if original_name and original_name != name:
        names.append(original_name)
    names = _unique_keep_order(names)
    conflicts = {n: [o for o in owners if o != target]
                 for n, owners in alias_owners(data, names).items()}
    conflicts = {n: owners for n, owners in conflicts.items() if owners}
    if conflicts:
        raise AliasConflict("名字归属冲突：" + "；".join(f"{n} → {', '.join(o)}" for n, o in conflicts.items()))
    updated = deepcopy(data)
    if original_name and original_name != name:
        del updated[original_name]
    updated[name] = names
    changes = {"original_name": original_name, "name": name, "created": target not in data,
               "added": [n for n in names if n not in [target, *old]],
               "removed": [n for n in old if n not in names], "aliases": names}
    return updated, changes


def save_alias_change(path: str, *, revision: str, confirmed: bool, **change) -> dict:
    if confirmed is not True:
        raise ValueError("必须人工确认后才能保存")
    with _alias_write_lock:
        data, current = read_alias_document(path)
        if revision != current:
            raise AliasConflict("别名库已发生变化，请重新加载后确认")
        updated, changes = prepare_alias_change(data, **change)
        if updated == data:
            return {"changes": changes, "revision": current, "backup": None}
        # 备份原始字节，然后原子替换，避免写入中断损坏词库。
        backup = path + "." + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak"
        Path(backup).write_bytes(Path(path).read_bytes())
        fd, temporary = tempfile.mkstemp(prefix=".actress_alias-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(updated, target, ensure_ascii=False, indent=2)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, os.stat(path).st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _, current = read_alias_document(path)
        return {"changes": changes, "revision": current, "backup": os.path.basename(backup)}


def analyze_actress_sources(sources: list[dict], data: dict) -> dict:
    multi = [s for s in sources if len(s.get("names", [])) > 1]
    if multi:
        return {"success": False, "sources": sources, "error": "目前仅支持单演员作品：" +
                "；".join(f"{s['source']} 返回多位演员：{', '.join(s['names'])}" for s in multi)}
    candidates = {}
    for source in sources:
        if source.get("error"):
            continue
        for name in source.get("names", []):
            candidates.setdefault(name, []).append(source["source"])
    if not candidates:
        return {"success": False, "sources": sources, "error": "没有获取到有效演员信息，请检查爬虫配置或手动维护"}
    owners = alias_owners(data, list(candidates))
    matched = set(o for values in owners.values() for o in values)
    if len(matched) > 1:
        return {"success": False, "sources": sources, "error": "名字归属冲突：" +
                "；".join(f"{n} → {', '.join(o)}" for n, o in owners.items() if o)}
    fixed = next(iter(matched), None)
    return {"success": True, "sources": sources, "name": fixed or next(iter(candidates)),
            "original_name": fixed, "existing_aliases": data.get(fixed, []) if fixed else [],
            "candidates": [{"name": n, "sources": s} for n, s in candidates.items()]}
