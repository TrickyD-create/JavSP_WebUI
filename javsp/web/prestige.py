"""从蚊香社-prestige抓取数据"""
import re
import logging


from javsp.web.base import *
from javsp.web.exceptions import *
from javsp.datatype import MovieInfo


logger = logging.getLogger(__name__)
base_url = 'https://www.prestige-av.com'
# prestige要求访问者携带已通过R18认证的cookies才能够获得完整数据，否则会被重定向到认证页面
# （其他多数网站的R18认证只是在网页上遮了一层，完整数据已经传回，不影响爬虫爬取）
cookies = {'__age_auth__': 'true', 'isOverAge': 'true'}


def _normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def _normalize_label(text: str) -> str:
    return re.sub(r'\s+', '', text or '').rstrip(':：')


def _first(items, default=None):
    for item in items:
        if item:
            return item
    return default


def _first_xpath_text(root, xpaths):
    for xpath in xpaths:
        values = root.xpath(xpath)
        for value in values:
            if not isinstance(value, str):
                value = value.text_content()
            value = _normalize_text(value)
            if value:
                return value
    return None


def _first_xpath_attr(root, xpaths):
    value = _first_xpath_text(root, xpaths)
    if not value:
        return None
    value = value.split(',')[0].strip().split()[0]
    return value.split('?')[0]


def _text_with_breaks(node):
    parts = []
    for child in node.iter():
        if child is node:
            if child.text:
                parts.append(child.text)
        else:
            if child.tag == 'br':
                parts.append('\n')
            elif child.text:
                parts.append(child.text)
        if child.tail:
            parts.append(child.tail)
    text = ''.join(parts)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return text.strip()


def _label_sibling(root, label):
    for node in root.xpath(".//*[self::p or self::th or self::dt or self::span]"):
        if _normalize_label(node.text_content()) == label:
            sibling = node.getnext()
            if sibling is not None:
                return sibling
    return None


def _texts_after_label(root, label, links_only=False):
    sibling = _label_sibling(root, label)
    if sibling is None:
        return []

    xpath = ".//a/text()" if links_only else ".//text()"
    values = [_normalize_text(i) for i in sibling.xpath(xpath)]
    return [i for i in values if i]


def _first_after_label(root, label, links_only=False):
    return _first(_texts_after_label(root, label, links_only=links_only))


def _href_after_label(root, label):
    sibling = _label_sibling(root, label)
    if sibling is None:
        return None
    return _first(sibling.xpath(".//a/@href"))


def _strip_dvdid_prefix(title, dvdid):
    if not title:
        return title
    return re.sub(rf'^\s*{re.escape(dvdid)}\s*', '', title, flags=re.I).strip()


def _parse_date(text):
    if not text:
        return None
    match = re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', text)
    if match:
        return match.group(0).replace('/', '-')
    return None


def _parse_preview_pics(root):
    values = []
    for xpath in (
        "//h2[normalize-space()='サンプル画像']/following-sibling::*[1]//img/@src",
        "//img[contains(@src, 'sample')]/@src",
    ):
        values.extend(root.xpath(xpath))
    pics = []
    for value in values:
        if not value:
            continue
        value = value.split('?')[0]
        if value not in pics:
            pics.append(value)
    return pics


def _parse_plot(root):
    for xpath in (
        "//h2[normalize-space()='商品紹介']/following-sibling::*[1]//p",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' p-goods-detail__description ')]",
    ):
        nodes = root.xpath(xpath)
        for node in nodes:
            text = _text_with_breaks(node)
            if text:
                return text
    return None


def _product_container(root):
    for section in root.xpath("//section[contains(concat(' ', normalize-space(@class), ' '), ' px-4 ')]"):
        if section.xpath(".//p[normalize-space()='品番：' or normalize-space()='品番:']"):
            return section
    return None


def _parse_title(root, dvdid):
    h1_tags = root.xpath(".//h1")
    for h1 in h1_tags:
        text = _normalize_text(''.join(h1.xpath("./text()")))
        if text:
            return _strip_dvdid_prefix(text, dvdid)
    return _strip_dvdid_prefix(_first_xpath_text(root, [".//h1"]), dvdid)


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    avid = movie.dvdid.upper()
    url = f'{base_url}/goods/goods_detail.php?sku={avid}'
    resp = request_get(url, cookies=cookies, delay_raise=True)
    if resp.status_code == 500:
        # 500错误表明prestige没有这部影片的数据，不是网络问题，因此不再重试
        raise MovieNotFoundError(__name__, avid)
    elif resp.status_code == 403:
        raise SiteBlocked('prestige不允许从当前IP所在地区访问，请尝试更换为日本地区代理')
    resp.raise_for_status()
    html = resp2html(resp)
    container = _product_container(html)
    if container is None:
        raise MovieNotFoundError(__name__, avid)

    title = _parse_title(container, avid)
    cover = _first_xpath_attr(container, [
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' c-ratio-image ')]//img/@src",
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' c-ratio-image ')]//source/@srcset",
        "//meta[@property='og:image']/@content",
    ])
    actress = _texts_after_label(container, '出演者', links_only=True)
    # 移除女优名中的空格，使女优名与其他网站保持一致
    actress = [i.strip().replace(' ', '') for i in actress]
    duration_str = _first_after_label(container, '収録時間') or ''
    match = re.search(r'\d+', duration_str)
    if match:
        movie.duration = match.group(0)
    date_url = _href_after_label(container, '発売日')
    publish_date = _parse_date(date_url) or _parse_date(_first_after_label(container, '発売日'))
    producer = _first_after_label(container, 'メーカー', links_only=True)
    dvdid = _first_after_label(container, '品番') or avid
    genre = _texts_after_label(container, 'ジャンル', links_only=True)
    serial = _first_after_label(container, 'レーベル', links_only=True)
    plot = _parse_plot(container)
    preview_pics = _parse_preview_pics(container)

    if not title or not cover:
        raise MovieNotFoundError(__name__, avid)

    # prestige改版后已经无法获取高清封面，此前已经获取的高清封面地址也已失效
    movie.url = resp.url
    movie.dvdid = dvdid
    movie.title = title
    movie.cover = cover
    movie.actress = actress
    movie.publish_date = publish_date
    movie.producer = producer
    movie.genre = genre
    movie.serial = serial
    movie.plot = plot
    movie.preview_pics = preview_pics
    movie.uncensored = False    # prestige服务器在日本且面向日本国内公开发售，不会包含无码片


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('ABP-647')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
