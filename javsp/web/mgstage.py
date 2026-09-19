"""从蚊香社-mgstage抓取数据"""
import re
import logging


from javsp.web.base import Request, resp2html
from javsp.web.exceptions import *
from javsp.config import Cfg
from javsp.datatype import MovieInfo


logger = logging.getLogger(__name__)
base_url = 'https://www.mgstage.com'
# 初始化Request实例（要求携带已通过R18认证的cookies，否则会被重定向到认证页面）
request = Request()
request.cookies = {'adc': '1'}


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
    for node in root.xpath(".//*[self::th or self::dt or self::p or self::span]"):
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


def _parse_date(text):
    if not text:
        return None
    match = re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', text)
    if match:
        return match.group(0).replace('/', '-')
    return None


def _parse_plot(root):
    plots = []
    plot_p_tags = root.xpath("//dl[@id='introduction']/dd/p[not(contains(concat(' ', normalize-space(@class), ' '), ' more '))]")
    for p in plot_p_tags:
        text = _text_with_breaks(p)
        if text:
            plots.append(text)
        if plots and plots[-1] != '\n':
            plots.append('\n')
    return ''.join(plots).strip()


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    avid = movie.dvdid.upper()
    url = f'{base_url}/product/product_detail/{avid}/'
    resp = request.get(url, delay_raise=True)
    if resp.status_code == 403:
        raise SiteBlocked('mgstage不允许从当前IP所在地区访问，请尝试更换为日本地区代理')
    # url不存在时会被重定向至主页。history非空时说明发生了重定向
    elif resp.history:
        raise MovieNotFoundError(__name__, avid)
    resp.raise_for_status()

    html = resp2html(resp)
    # mgstage的文本中含有大量的空白字符（'\n \t'），需要使用strip去除
    title = _first_xpath_text(html, [
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' common_detail_cover ')]/h1",
        "//h1",
    ])
    title = re.sub(rf'^\s*{re.escape(avid)}\s*', '', title or '', flags=re.I).strip()
    cover = _first_xpath_attr(html, [
        "//a[@id='EnlargeImage']/@href",
        "//meta[@property='og:image']/@content",
    ])
    # 有链接的女优和仅有文本的女优匹配方法不同，因此分别匹配以后合并列表
    actress = _texts_after_label(html, '出演', links_only=True) or _texts_after_label(html, '出演')
    actress = [i for i in actress if i]     # 移除空字符串
    producer = _first_after_label(html, 'メーカー', links_only=True) or _first_after_label(html, 'メーカー')
    duration_str = _first_after_label(html, '収録時間') or ''
    match = re.search(r'\d+', duration_str)
    if match:
        movie.duration = match.group(0)
    dvdid = _first_after_label(html, '品番') or avid
    publish_date = _parse_date(_first_after_label(html, '配信開始日'))
    serial = _first_after_label(html, 'シリーズ', links_only=True)
    if serial:
        movie.serial = serial
    # label: 大意是某个系列策划用同样的番号，例如ABS打头的番号label是'ABSOLUTELY PERFECT'，暂时用不到
    # label = container.xpath("//th[text()='レーベル：']/following-sibling::td/text()")[0].strip()
    genre = _texts_after_label(html, 'ジャンル', links_only=True)
    score_str = _first_xpath_text(html, [
        "//td[contains(concat(' ', normalize-space(@class), ' '), ' review ')]/text()[normalize-space()]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' review ')]/text()[normalize-space()]",
    ]) or ''
    match = re.search(r'^[\.\d]+', score_str)
    if match:
        score = float(match.group()) * 2
        movie.score = f'{score:.2f}'
    plot = _parse_plot(html)
    preview_pics = html.xpath("//a[contains(concat(' ', normalize-space(@class), ' '), ' sample_image ')]/@href")

    if not title or not cover:
        raise MovieNotFoundError(__name__, avid)

    if Cfg().crawler.hardworking:
        # 预览视频是点击按钮后再加载的，不在静态网页中
        btn_url = _first(html.xpath("//a[contains(concat(' ', normalize-space(@class), ' '), ' button_sample ')]/@href"))
        if btn_url:
            video_pid = btn_url.split('/')[-1]
            req_url = f'{base_url}/sampleplayer/sampleRespons.php?pid={video_pid}'
            resp = request.get(req_url).json()
            video_url = resp.get('url')
            if video_url:
                # /sample/shirouto/siro/3093/SIRO-3093_sample.ism/request?uid=XXX&amp;pid=XXX
                preview_video = video_url.split('.ism/')[0] + '.mp4'
                movie.preview_video = preview_video

    movie.dvdid = dvdid
    movie.url = url
    movie.title = title
    movie.cover = cover
    movie.actress = actress
    movie.producer = producer
    movie.publish_date = publish_date
    movie.genre = genre
    movie.plot = plot
    movie.preview_pics = preview_pics
    movie.uncensored = False    # 服务器在日本且面向日本国内公开发售，不会包含无码片


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('HRV-045')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
