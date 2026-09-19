"""Metadata completeness checks and conservative refresh merging."""
from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any

from lxml import etree

from javsp.avid import get_cid, get_id, guess_av_type
from javsp.config import Cfg, MetadataCompleteField, MovieInfoField
from javsp.datatype import MovieInfo


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TEXT_RE = re.compile(r"\S")
ENGLISH_RE = re.compile(r"[A-Za-z]")


def _field_names() -> set[str]:
    return {item.value for item in MovieInfoField}


def info_to_dict(info: MovieInfo | None) -> dict[str, Any]:
    if not info:
        return {}
    return {key: deepcopy(getattr(info, key, None)) for key in _field_names()}


def info_from_dict(data: dict[str, Any] | None, avid: str | None = None, data_src: str = "normal") -> MovieInfo | None:
    data = data or {}
    dvdid = data.get("dvdid") or (avid if data_src != "cid" else None)
    cid = data.get("cid") or (avid if data_src == "cid" else None)
    if cid:
        info = MovieInfo(cid=cid)
    elif dvdid:
        info = MovieInfo(dvdid)
    else:
        return None
    for key in _field_names():
        if key in data:
            setattr(info, key, data[key])
    _normalize_info_title(info)
    return info


def strip_leading_avid(title: str | None, avid: str | None) -> str | None:
    if not title or not avid:
        return title
    text = title.strip()
    prefix_re = re.compile(rf"^(?:{re.escape(avid)}(?:[\s:：/_|]+|$))+", re.IGNORECASE)
    stripped = prefix_re.sub("", text).lstrip(" -_/:：|").strip()
    return stripped or text


def _normalize_info_title(info: MovieInfo | None) -> None:
    if not info or not getattr(info, "title", None):
        return
    title = strip_leading_avid(info.title, info.dvdid or info.cid)
    if title:
        info.title = title


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def cjk_ratio(text: str) -> float:
    # 英文标题片段不应稀释中文比例；保留数字、标点等原有计数行为。
    chars = [ch for ch in text if TEXT_RE.match(ch) and not ENGLISH_RE.match(ch)]
    if not chars:
        return 0
    return len(CJK_RE.findall("".join(chars))) / len(chars)


def field_reasons(field: str, value: Any, rule: MetadataCompleteField) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if not has_value(value):
        reasons.append({"field": field, "reason": "missing", "message": f"{field} 缺失"})
        return reasons

    if isinstance(value, str):
        text = value.strip()
        if rule.reject_values and text in set(rule.reject_values):
            reasons.append({"field": field, "reason": "reject_value", "message": f"{field} 是拒绝值"})
        if rule.min_length and len(text) < int(rule.min_length):
            reasons.append({
                "field": field,
                "reason": "min_length",
                "expected": int(rule.min_length),
                "actual": len(text),
                "message": f"{field} 长度不足",
            })
        if rule.prefer_language == "zh" and rule.min_cjk_ratio:
            ratio = cjk_ratio(text)
            if ratio < float(rule.min_cjk_ratio):
                reasons.append({
                    "field": field,
                    "reason": "language_preference",
                    "expected": "zh",
                    "min_cjk_ratio": float(rule.min_cjk_ratio),
                    "actual_cjk_ratio": ratio,
                    "message": f"{field} 中文比例不足",
                })

    if rule.min_items and isinstance(value, (list, tuple, set, dict)):
        actual = len(value)
        if actual < int(rule.min_items):
            reasons.append({
                "field": field,
                "reason": "min_items",
                "expected": int(rule.min_items),
                "actual": actual,
                "message": f"{field} 数量不足",
            })
    return reasons


def check_metadata_complete(info: MovieInfo | None) -> tuple[bool, list[dict[str, Any]]]:
    cfg = Cfg().metadata_complete
    if not cfg.enabled:
        return True, []
    if not info:
        return False, [{"field": "_metadata", "reason": "missing", "message": "缺少可判断的元数据"}]

    reasons: list[dict[str, Any]] = []
    for field, rule in cfg.fields.items():
        if not rule.required:
            continue
        reasons.extend(field_reasons(field, getattr(info, field, None), rule))
    return not reasons, reasons


def _incoming_satisfies(field: str, value: Any, rule: MetadataCompleteField) -> bool:
    return has_value(value) and not field_reasons(field, value, rule)


_PRIORITY_FIELDS = {"title", "plot", "actress", "preview_pics"}


def _refresh_ordered_infos(field: str, crawler_infos: dict[str, MovieInfo]):
    """按 field_priorities 配置排序爬虫数据迭代器。未配置时回退到 dict 顺序。"""
    priorities = getattr(getattr(Cfg().crawler, "field_priorities", None), field, []) or []
    ordered = []
    for name in priorities:
        if name in crawler_infos and name not in ordered:
            ordered.append(name)
    ordered.extend(name for name in crawler_infos if name not in ordered)
    for name in ordered:
        yield crawler_infos[name]


def _select_refresh_value(field: str, current: Any, rule: MetadataCompleteField | None, crawler_infos: dict[str, MovieInfo]) -> Any:
    candidates = []
    infos_iter = _refresh_ordered_infos(field, crawler_infos) if field in _PRIORITY_FIELDS else crawler_infos.values()
    for info in infos_iter:
        value = deepcopy(getattr(info, field, None))
        if field == "title":
            value = strip_leading_avid(value, info.dvdid or info.cid)
        if has_value(value):
            candidates.append(value)
    if not candidates:
        return None
    if not rule:
        return candidates[0]
    current_bad = has_value(current) and bool(field_reasons(field, current, rule))
    if (not has_value(current)) or current_bad or rule.allow_overwrite:
        for value in candidates:
            if _incoming_satisfies(field, value, rule):
                return value
    return candidates[0]


def merge_refresh_info(old_info: MovieInfo, crawler_infos: dict[str, MovieInfo]) -> tuple[MovieInfo, list[str]]:
    """Merge newly crawled fields into existing metadata with conservative overwrite rules."""
    merged = info_from_dict(info_to_dict(old_info), old_info.dvdid or old_info.cid, "cid" if old_info.cid else "normal")
    if merged is None:
        merged = old_info
    _normalize_info_title(merged)
    updated: list[str] = []
    rules = Cfg().metadata_complete.fields

    for field in _field_names():
        if field in {"dvdid", "cid"}:
            continue
        current = getattr(merged, field, None)
        rule = rules.get(field)
        incoming = _select_refresh_value(field, current, rule, crawler_infos)
        if not has_value(incoming):
            continue

        should_update = False
        if not has_value(current):
            should_update = True
        elif rule:
            current_bad = bool(field_reasons(field, current, rule))
            incoming_good = _incoming_satisfies(field, incoming, rule)
            should_update = bool(rule.allow_overwrite and incoming_good and incoming != current)
            if field in {"title", "plot"} and current_bad and incoming_good:
                should_update = True

        if should_update:
            setattr(merged, field, incoming)
            updated.append(field)

    return merged, updated


def _ids_from_nfo(root: etree._Element, nfo_path: str) -> tuple[str | None, str | None]:
    dvdid = None
    cid = None
    for node in root.findall("uniqueid"):
        text = (node.text or "").strip()
        if not text:
            continue
        node_type = (node.get("type") or "").lower()
        if node_type == "cid":
            cid = text
        elif node_type in {"num", "dvdid", "avid"}:
            dvdid = text

    for tag in ("num", "dvdid", "id"):
        if dvdid:
            break
        text = (root.findtext(tag) or "").strip()
        if text:
            dvdid = text

    if not dvdid:
        dvdid = get_id(nfo_path)
    if not cid:
        title = root.findtext("title") or ""
        cid = get_cid(title)
    if not dvdid and not cid:
        title = root.findtext("title") or ""
        dvdid = get_id(title)
    return dvdid, cid


def load_info_from_nfo(nfo_path: str, avid: str | None = None, data_src: str = "normal") -> MovieInfo | None:
    if not nfo_path or not os.path.exists(nfo_path):
        return None
    try:
        root = etree.parse(nfo_path).getroot()
    except Exception:
        return None

    if not avid:
        dvdid, cid = _ids_from_nfo(root, nfo_path)
        if data_src == "cid" or (cid and not dvdid):
            avid = cid
            data_src = "cid"
        else:
            avid = dvdid
            data_src = guess_av_type(avid) if avid else data_src

    info = info_from_dict({}, avid, data_src)
    if not info:
        return None
    info.title = strip_leading_avid(root.findtext("title") or None, info.dvdid or info.cid)
    info.ori_title = root.findtext("originaltitle") or None
    info.score = root.findtext("rating") or None
    info.plot = root.findtext("plot") or None
    info.duration = root.findtext("runtime") or None
    info.publish_date = root.findtext("premiered") or None
    info.director = root.findtext("director") or None
    studio = root.findtext("studio")
    info.producer = studio or None
    info.publisher = studio or None
    info.preview_video = root.findtext("trailer") or None
    genres = [node.text for node in root.findall("genre") if node.text]
    info.genre = genres or None
    actors = []
    actress_pics = {}
    for actor in root.findall(".//actor"):
        name = actor.findtext("name")
        if name:
            actors.append(name)
            thumb = actor.findtext("thumb")
            if thumb:
                actress_pics[name] = thumb
    info.actress = actors or None
    info.actress_pics = actress_pics or None
    for node in root.findall("uniqueid"):
        node_type = node.get("type")
        if node_type == "cid":
            info.cid = node.text
        elif node_type == "num" and node.text:
            info.dvdid = node.text
    return info


def prepare_info_for_nfo(info: MovieInfo) -> None:
    _normalize_info_title(info)
    if not getattr(info, "nfo_title", None):
        dic = info.get_info_dic()
        setattr(info, "nfo_title", Cfg().summarizer.nfo.title_pattern.format(**dic))
