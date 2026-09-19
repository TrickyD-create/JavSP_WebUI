"""从SupJav抓取数据"""
import re
import logging


from javsp.web.base import Request, get_html
from javsp.web.exceptions import *
from javsp.func import *
from javsp.config import Cfg
from javsp.datatype import MovieInfo


logger = logging.getLogger(__name__)
base_url = 'https://supjav.com'

request = Request(use_scraper=True)
request.headers['Accept-Language'] = 'zh-CN,zh;q=0.9,en;q=0.8'


def search_movie(dvdid):
    html = get_html(f'{base_url}/?s={dvdid}')
    links = html.xpath("//article[contains(@class,'post')]//h2/a")
    for link in links:
        href = link.get('href', '')
        title = link.text_content().strip()
        if dvdid.lower() in title.lower() or dvdid.lower() in href.lower():
            return href
    raise MovieNotFoundError(__name__, dvdid)


def parse_data(movie: MovieInfo):
    detail_url = search_movie(movie.dvdid)
    html = get_html(detail_url)

    title_tag = html.xpath("//meta[@property='og:title']/@content")
    if title_tag:
        title = title_tag[0].strip()
        title = re.sub(r'\s*[-|]\s*SupJav\s*$', '', title, flags=re.IGNORECASE).strip()
        movie.title = title

    cover_tag = html.xpath("//meta[@property='og:image']/@content")
    if cover_tag:
        movie.cover = cover_tag[0].strip()

    plot_tag = html.xpath("//meta[@property='og:description']/@content")
    if plot_tag:
        plot = plot_tag[0].strip()
        movie.plot = plot if len(plot) > 10 else None

    movie.url = detail_url

    # Parse detail info from entry-content
    entry = html.xpath("//div[contains(@class,'entry-content')]")[0]
    info_text = entry.text_content() if entry is not None else ''

    actress_tags = entry.xpath(".//a[contains(@href,'/actress/')]")
    if actress_tags:
        movie.actress = [a.text_content().strip() for a in actress_tags if a.text_content().strip()]

    genre_tags = entry.xpath(".//a[contains(@href,'/genre/') or contains(@href,'/tag/')]")
    if genre_tags:
        movie.genre = [g.text_content().strip() for g in genre_tags if g.text_content().strip()]

    # 发布日期
    date_match = re.search(r'(?:発売日|配信日|Release)\s*[:：]\s*(\d{4}[-/]\d{2}[-/]\d{2})', info_text)
    if date_match:
        movie.publish_date = date_match.group(1).replace('/', '-')

    # 时长
    dur_match = re.search(r'(?:収録時間|Duration)\s*[:：]\s*(\d+)', info_text)
    if dur_match:
        movie.duration = dur_match.group(1)

    # 制作商
    maker_match = re.search(r'(?:メーカー|メーカー|制作商|Maker)\s*[:：]\s*([^\n]+)', info_text)
    if maker_match:
        movie.producer = maker_match.group(1).strip()

    # 系列
    series_tag = entry.xpath(".//a[contains(@href,'/series/')]")
    if series_tag:
        movie.serial = series_tag[0].text_content().strip()

    # 导演
    dir_tag = entry.xpath(".//a[contains(@href,'/director/')]")
    if dir_tag:
        movie.director = dir_tag[0].text_content().strip()

    # 预览图
    preview_pics = entry.xpath(".//a[contains(@class,'highslide')]/img/@src")
    if not preview_pics:
        preview_pics = entry.xpath(".//div[contains(@class,'gallery')]//img/@src")
    if preview_pics:
        movie.preview_pics = preview_pics

    if not movie.title and not movie.cover:
        raise MovieNotFoundError(__name__, movie.dvdid)


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('SSIS-950')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
