"""从FC2官网抓取数据"""
import logging
import re


from javsp.web.base import get_html, request_get, resp2html
from javsp.web.exceptions import *
from javsp.config import Cfg
from javsp.lib import strftime_to_minutes
from javsp.datatype import MovieInfo


logger = logging.getLogger(__name__)
base_url = 'https://adult.contents.fc2.com'


def _first(items, default=None):
    """返回 XPath 结果的第一项，避免可选字段变更导致整个爬虫崩溃。"""
    return items[0] if items else default


def _clean_title(html, container, fc2_id):
    """优先使用无反爬噪声的 OpenGraph 标题。"""
    title = _first(html.xpath("//meta[@property='og:title']/@content"))
    if title:
        return re.sub(rf'^FC2(?:-PPV)?-{re.escape(fc2_id)}\s*', '', title, flags=re.I).strip()

    # 页面会在 h3 内插入视觉隐藏的随机文本，不能直接 text_content()。
    title_parts = container.xpath(
        ".//div[contains(concat(' ', normalize-space(@class), ' '), "
        "' items_article_headerInfo ')]/h3/text()"
    )
    return ''.join(title_parts).strip() or None


def get_movie_score(fc2_id):
    """通过评论数据来计算FC2的影片评分（10分制），无法获得评分时返回None"""
    html = get_html(f'{base_url}/article/{fc2_id}/review')
    review_tags = html.xpath("//ul[@class='items_comment_headerReviewInArea']/li")
    reviews = {}
    for tag in review_tags:
        score = int(tag.xpath("div/span/text()")[0])
        vote = int(tag.xpath("span")[0].text_content())
        reviews[score] = vote
    total_votes = sum(reviews.values())
    if (total_votes >= 2):   # 至少也该有两个人评价才有参考意义一点吧
        summary = sum([k*v for k, v in reviews.items()])
        final_score = summary / total_votes * 2   # 乘以2转换为10分制
        return final_score


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    # 去除番号中的'FC2'字样
    id_uc = movie.dvdid.upper()
    if not id_uc.startswith('FC2-'):
        raise ValueError('Invalid FC2 number: ' + movie.dvdid)
    fc2_id = id_uc.replace('FC2-', '')
    # 抓取网页
    url = f'{base_url}/article/{fc2_id}/'
    resp = request_get(url)
    if '/id.fc2.com/' in resp.url:
        raise SiteBlocked('FC2要求当前IP登录账号才可访问，请尝试更换为日本IP')
    html = resp2html(resp)
    container = html.xpath("//div[@class='items_article_left']")
    if len(container) > 0:
        container = container[0]
    else:
        raise MovieNotFoundError(__name__, movie.dvdid)
    title = _clean_title(html, container, fc2_id)
    thumb_tag = _first(container.xpath(".//div[@class='items_article_MainitemThumb']"))
    # og:image 是原图；旧版页面没有该字段时再回退到缩略图。
    thumb_pic = _first(html.xpath("//meta[@property='og:image']/@content"))
    duration_str = None
    if thumb_tag is not None:
        thumb_pic = thumb_pic or _first(thumb_tag.xpath(".//span/img/@src"))
        duration_str = _first(thumb_tag.xpath(".//p[@class='items_article_info']/text()"))
    # FC2没有制作商和发行商的区分，作为个人市场，影片页面的'by'更接近于制作商
    producer = _first(container.xpath(".//li[starts-with(normalize-space(.), 'by ')]/a[1]/text()"))
    genre = container.xpath(".//a[contains(concat(' ', normalize-space(@class), ' '), ' tagTag ')]/text()")
    # 旧页面使用 items_article_Releasedate，新页面移入 items_article_softDevice。
    date_str = _first(container.xpath(
        ".//div[@class='items_article_Releasedate']/p/text() | "
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' items_article_softDevice ')]"
        "/p[contains(., '販売日')]/text()"
    ))
    date_match = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', date_str or '')
    publish_date = '-'.join(part.zfill(2) if i else part for i, part in enumerate(date_match.groups())) if date_match else None
    preview_pics = container.xpath(".//ul[@data-feed='sample-images']/li/a/@href")

    if Cfg().crawler.hardworking:
        # 通过评论数据来计算准确的评分
        score = get_movie_score(fc2_id)
        if score:
            movie.score = f'{score:.2f}'
        # 预览视频是动态加载的，不在静态网页中
        desc_frame_url = _first(container.xpath(".//section[@class='items_article_Contents']/iframe/@src"))
        if desc_frame_url and 'ac=' in desc_frame_url:
            key = desc_frame_url.split('ac=', 1)[-1].split('&', 1)[0]
            api_url = f'{base_url}/api/v2/videos/{fc2_id}/sample?key={key}'
            movie.preview_video = request_get(api_url).json().get('path')
    else:
        # 获取影片评分。影片页面的评分只能粗略到星级，且没有分数，要通过类名来判断，如'items_article_Star5'表示5星
        score_tag_attr = _first(container.xpath(".//a[@class='items_article_Stars']/p/span/@class"))
        score_match = re.search(r'(\d+)$', score_tag_attr or '')
        if score_match:
            movie.score = f'{int(score_match.group(1)) * 2:.2f}'

    movie.dvdid = id_uc
    movie.url = url
    movie.title = title
    movie.genre = genre
    movie.producer = producer
    movie.duration = str(strftime_to_minutes(duration_str)) if duration_str else None
    movie.publish_date = publish_date
    movie.preview_pics = preview_pics
    # FC2的封面是220x220的，和正常封面尺寸、比例都差太多。如果有预览图片，则使用第一张预览图作为封面
    if movie.preview_pics:
        movie.cover = preview_pics[0]
    else:
        movie.cover = thumb_pic


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
