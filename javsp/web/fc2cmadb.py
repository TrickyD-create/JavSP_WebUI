"""从 FC2CMADB 抓取 FC2 影片数据。"""

import json

import lxml.html
from lxml.etree import ParserError

from javsp.datatype import MovieInfo
from javsp.lib import strftime_to_minutes
from javsp.web.base import request_get
from javsp.web.exceptions import MovieNotFoundError, SiteBlocked, WebsiteError


base_url = "https://fc2cmadb.com"


def _extract_props(text: str) -> dict:
    """从 Inertia 页面中提取服务端直出的 props。"""
    try:
        html = lxml.html.fromstring(text)
        page_json = html.xpath("//script[@data-page and @type='application/json']/text()")
        if not page_json:
            raise ValueError("缺少 Inertia page data")
        page = json.loads(page_json[0])
        if page.get("component") != "Articles/Show":
            raise ValueError(f"非影片详情页: {page.get('component')!r}")
        return page["props"]
    except (ValueError, TypeError, KeyError, ParserError) as exc:
        raise WebsiteError(f"FC2CMADB: 无法解析页面数据: {exc}") from exc


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据。"""
    id_uc = movie.dvdid.upper()
    if not id_uc.startswith("FC2-"):
        raise ValueError("Invalid FC2 number: " + movie.dvdid)
    fc2_id = id_uc.removeprefix("FC2-")
    if not fc2_id.isdigit():
        raise ValueError("Invalid FC2 number: " + movie.dvdid)

    url = f"{base_url}/articles/{fc2_id}"
    resp = request_get(url, delay_raise=True)
    if resp.status_code == 404:
        raise MovieNotFoundError(__name__, movie.dvdid)
    if resp.status_code == 403:
        raise SiteBlocked("FC2CMADB: 访问被拒绝，可能触发了站点防护")
    if resp.status_code == 429:
        raise SiteBlocked("FC2CMADB: 请求过于频繁，请稍后重试")
    if not 200 <= resp.status_code < 300:
        raise WebsiteError(f"FC2CMADB: 站点不可用 (HTTP {resp.status_code}): {url}")

    props = _extract_props(resp.text)
    article = props.get("article")
    if not isinstance(article, dict) or article.get("not_found") or not article.get("title"):
        raise MovieNotFoundError(__name__, movie.dvdid)
    if str(article.get("video_id")) != fc2_id:
        raise WebsiteError(
            f"FC2CMADB: 页面番号不匹配: 请求 {fc2_id}, 返回 {article.get('video_id')}"
        )

    writer = article.get("writer") or {}
    tags = article.get("tags") or []
    actresses = props.get("actresses") or []
    duration = article.get("duration")
    censored = article.get("censored")

    movie.dvdid = id_uc
    movie.url = url
    movie.title = article.get("title")
    movie.cover = article.get("image_url")
    movie.genre = [
        tag["name"] for tag in tags if isinstance(tag, dict) and tag.get("name")
    ]
    movie.actress = [
        item["name"]
        for item in actresses
        if isinstance(item, dict) and item.get("name")
    ] or None
    movie.duration = str(strftime_to_minutes(duration)) if duration else None
    movie.publish_date = article.get("release_date")
    movie.publisher = writer.get("name")
    movie.uncensored = True if censored == "無" else False if censored == "有" else None
