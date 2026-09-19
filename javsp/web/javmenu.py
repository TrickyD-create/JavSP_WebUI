"""从JavMenu抓取数据"""
import logging
import re

from javsp.web.base import Request, resp2html
from javsp.web.exceptions import *
from javsp.datatype import MovieInfo


request = Request()

logger = logging.getLogger(__name__)
base_url = 'https://mrzyx.xyz'


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    # JavMenu网页做得很不走心，将就了
    url = f'{base_url}/{movie.dvdid}'
    r = request.get(url)
    if r.history:
        # 被重定向到主页说明找不到影片资源
        raise MovieNotFoundError(__name__, movie.dvdid)

    html = resp2html(r)
    # 站点会不定期调整Bootstrap间距类，不能依赖完整的class字符串。
    containers = html.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' col-md-9 ')]"
    )
    if not containers:
        raise WebsiteError(f"JavMenu: 无法识别影片页面结构: {url}")
    container = containers[0]

    title = container.xpath("normalize-space(.//h1/strong)")
    if not title:
        raise WebsiteError(f"JavMenu: 影片页面缺少标题: {url}")
    # 竟然还在标题里插广告，真的疯了。要不是我已经写了抓取器，才懒得维护这个破站
    title = title.replace('| JAV目錄大全 | 每日更新', '')
    title = title.replace(' 免費在線看', '').replace(' 免費AV在線看', '')

    # 新页面首个video使用标准poster属性，旧播放器使用data-poster。
    for video_tag in container.xpath(
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' single-video ')]/video"
    ):
        cover = video_tag.get('data-poster') or video_tag.get('poster')
        if cover:
            movie.cover = cover.strip()
            break
    if not movie.cover:
        cover_img_tag = container.xpath(
            ".//img[contains(concat(' ', normalize-space(@class), ' '), ' lazy ')"
            " and contains(concat(' ', normalize-space(@class), ' '), ' rounded ')]/@data-src"
        )
        if cover_img_tag:
            movie.cover = cover_img_tag[0].strip()

    info_tags = container.xpath(
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' card-body ')]["
        ".//span[contains(normalize-space(.), '時長:')]]"
    )
    if not info_tags:
        raise WebsiteError(f"JavMenu: 影片页面缺少资料区域: {url}")
    info = info_tags[0]

    publish_date_tags = info.xpath(
        ".//div/span[contains(normalize-space(.), '日期:')"
        " or contains(normalize-space(.), '發佈於:')]"
    )
    publish_date = None
    if publish_date_tags and publish_date_tags[0].getnext() is not None:
        publish_date = publish_date_tags[0].getnext().text_content().strip()

    duration_tags = info.xpath(".//div/span[contains(normalize-space(.), '時長:')]")
    duration = None
    if duration_tags and duration_tags[0].getnext() is not None:
        duration = duration_tags[0].getnext().text_content().replace('分鐘', '').strip()

    producer = info.xpath(
        ".//div/span[contains(normalize-space(.), '製作:')]/following-sibling::a/span/text()"
    )
    if producer:
        movie.producer = producer[0].strip()
    genre_tags = info.xpath(
        ".//a[contains(concat(' ', normalize-space(@class), ' '), ' genre ')]"
    )
    genre, genre_id = [], []
    for tag in genre_tags:
        items = tag.get('href').split('/')
        pre_id = items[-3] + '/' + items[-1]
        genre.append(tag.text_content().strip())
        genre_id.append(pre_id)
        # genre的链接中含有censored字段，但是无法用来判断影片是否有码，因为完全不可靠……
    actress = [
        item.strip()
        for item in info.xpath(
            ".//div/span[contains(normalize-space(.), '女優:')]/following-sibling::*//a/text()"
        )
        if item.strip()
    ] or None
    magnet_table = container.xpath(".//table[contains(@class, 'magnet-table')]/tbody")
    if magnet_table:
        magnet_links = magnet_table[0].xpath("tr/td/a/@href")
        # 它的FC2数据是从JavDB抓的，JavDB更换图片服务器后它也跟上了，似乎数据更新频率还可以
        movie.magnet = [i.replace('[javdb.com]', '') for i in magnet_links]
    preview_pics = container.xpath(".//a[@data-fancybox='gallery']/@href")

    if (not movie.cover) and preview_pics:
        movie.cover = preview_pics[0]
    movie.url = url
    movie.title = re.sub(re.escape(movie.dvdid), '', title, count=1, flags=re.IGNORECASE).strip()
    movie.preview_pics = preview_pics
    movie.publish_date = publish_date
    movie.duration = duration
    movie.genre = genre
    movie.genre_id = genre_id
    movie.actress = actress


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('FC2-718323')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
