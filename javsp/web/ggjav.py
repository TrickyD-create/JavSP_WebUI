"""从GGJAV抓取数据"""
import re
import logging


from javsp.web.base import Request, get_html
from javsp.web.exceptions import *
from javsp.config import Cfg
from javsp.datatype import MovieInfo


logger = logging.getLogger(__name__)
base_url = 'https://ggjav.tv'

request = Request(use_scraper=True)
request.headers['Accept-Language'] = 'zh-CN,zh;q=0.9,en;q=0.8'


def search_movie(dvdid):
    """在GGJAV上搜索影片，返回匹配的详情页URL"""
    html = get_html(f'{base_url}/main/search?string={dvdid}')
    items = html.xpath("//div[contains(@class,'item') and not(contains(@class,'native_ads'))]")
    candidates = []
    for item in items:
        link = item.xpath(".//div[contains(@class,'item_title')]/a/@href")
        if not link:
            continue
        href = link[0]
        title_el = item.xpath(".//div[contains(@class,'item_title')]/a/text()")
        title = title_el[0].strip() if title_el else ''
        candidates.append((href, title))
        if dvdid.upper() in title.upper() or dvdid.upper() in href.upper():
            return href
    if len(candidates) == 1:
        return candidates[0][0]
    raise MovieNotFoundError(__name__, dvdid)


def parse_data(movie: MovieInfo):
    detail_url = search_movie(movie.dvdid)
    html = get_html(detail_url)
    movie.url = detail_url

    # 标题
    title_tag = html.xpath("//meta[@property='og:title']/@content")
    if title_tag:
        title = title_tag[0].strip()
        title = re.sub(r'\s*[-|]\s*GGJAV\s*\|.*$', '', title, flags=re.IGNORECASE).strip()
        movie.title = title

    # 封面
    cover_tag = html.xpath("//meta[@property='og:image']/@content")
    if cover_tag:
        movie.cover = cover_tag[0].strip()

    # 无码判断
    censored_btn = html.xpath("//a[contains(@class,'ctg_button') and contains(@href,'/main/censored')]")
    uncensored_btn = html.xpath("//a[contains(@class,'ctg_button') and contains(@href,'/main/uncensored')]")
    if uncensored_btn:
        movie.uncensored = True
    elif censored_btn:
        movie.uncensored = False

    # 类别标签 (排除有码/无码/中文字幕/素人/动漫/欧美等分类按钮)
    genre_tags = html.xpath("//a[contains(@class,'ctg_button') and contains(@href,'/main/ctg?')]")
    if genre_tags:
        genres = [g.text_content().strip() for g in genre_tags if g.text_content().strip()]
        if genres:
            movie.genre = genres

    # 女优
    actress_tags = html.xpath("//div[contains(@class,'model')]//div[contains(@class,'model_name')]")
    if actress_tags:
        movie.actress = [a.text_content().strip() for a in actress_tags if a.text_content().strip()]

    # 简介
    plot_match = re.search(r'簡介[：:]\s*([^\n]+)', html.text_content())
    if plot_match:
        plot = plot_match.group(1).strip()
        if len(plot) > 5:
            movie.plot = plot

    # 预览图
    preview_pics = html.xpath("//div[contains(@class,'previews')]//img/@src")
    if preview_pics:
        movie.preview_pics = list(preview_pics)

    # 女优头像 (actress_pics)
    actress_pics = html.xpath("//div[contains(@class,'model')]//img/@src")
    if actress_pics:
        movie.actress_pics = [p for p in actress_pics if '/model/' in p]

    if not movie.title and not movie.cover:
        raise MovieNotFoundError(__name__, movie.dvdid)


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logging.basicConfig(level=logging.DEBUG)

    movie = MovieInfo('DCX-105')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
