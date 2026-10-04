"""只查询原始演员名单；不执行文件整理或修改别名库。"""
from __future__ import annotations

import importlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from javsp.config import Cfg
from javsp.datatype import MovieInfo
from javsp.web.exceptions import (
    MovieNotFoundError, MovieDuplicateError, SiteBlocked,
    SitePermissionError, CredentialError,
)


def normalize_id(value: str) -> str:
    return re.sub(r"[-_\s]", "", value.upper().replace("FC2-PPV-", "FC2-")).upper()


def collect_source(name: str, dvdid: str, retry: int) -> dict:
    result = {"source": name, "names": []}
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("无效的爬虫名称")
        parser = importlib.import_module(f"javsp.web.{name}").parse_data
        for attempt in range(max(1, retry)):
            info = MovieInfo(dvdid)
            info._strict_dvdid_match = True
            try:
                parser(info)
                break
            except (MovieNotFoundError, MovieDuplicateError, SiteBlocked,
                    SitePermissionError, CredentialError):
                raise
            except Exception:
                if attempt == max(1, retry) - 1:
                    raise
        if not info.dvdid or normalize_id(info.dvdid) != normalize_id(dvdid):
            raise ValueError(f"返回番号不一致：{info.dvdid}")
        if info.actress is not None and not isinstance(info.actress, (list, tuple)):
            raise ValueError("演员名单格式错误")
        names = list(dict.fromkeys(n.strip() for n in (info.actress or []) if isinstance(n, str) and n.strip()))
        result.update(names=names, url=info.url)
        if not names:
            result["error"] = "未返回演员信息"
    except Exception as exc:
        result["error"] = str(exc) or type(exc).__name__
    return result


def collect_actress_sources(dvdid: str) -> list[dict]:
    dvdid = dvdid.upper().replace("FC2-PPV-", "FC2-")
    cfg = Cfg()
    kind = "fc2" if dvdid.upper().startswith("FC2") else "normal"
    crawlers = list(dict.fromkeys(getattr(cfg.crawler.selection, kind)))
    if not crawlers:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(crawlers))) as pool:
        return list(pool.map(lambda name: collect_source(name, dvdid, cfg.network.retry), crawlers))


if __name__ == "__main__":
    from argparse import ArgumentParser
    parser = ArgumentParser()
    parser.add_argument("-c", "--config")
    parser.add_argument("--dvdid", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as target:
        json.dump(collect_actress_sources(args.dvdid), target, ensure_ascii=False)
