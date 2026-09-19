"""从javrate抓取数据"""
import re
import logging

from javsp.web.base import Request, get_html
from javsp.web.exceptions import *
from javsp.config import Cfg
from javsp.datatype import MovieInfo

# 初始化Request实例。javrate有CloudFlare保护，需要使用scraper
request = Request(use_scraper=True)
request.headers['Accept-Language'] = 'zh-TW,zh;q=0.9'

logger = logging.getLogger(__name__)
base_url = 'https://www.javrate.com'


def search_movie(dvdid):
    """通过搜索番号获取指定的影片在网站上的ID（详情页链接）"""
    search_url = f'{base_url}/search/{dvdid}'
    html = get_html(search_url)
    
    logger.debug(f"当前搜索页面 URL: {html.base_url}")
    
    # javrate的搜索结果中，影片链接带有 data-movie-code 属性
    video_links = html.xpath("//a[@data-movie-code]")
    
    if not video_links:
        page_title = html.xpath('//title')[0].text_content() if html.xpath('//title') else '无标题'
        logger.debug(f"未找到搜索结果，页面标题: {page_title}")
        raise MovieNotFoundError(__name__, dvdid)
    
    target_link = None
    for link in video_links:
        code = link.get('data-movie-code', '')
        href = link.get('href', '')
        title = link.get('title', '')
        
        # 匹配番号（不区分大小写）
        if dvdid.lower() == code.lower():
            target_link = href
            logger.debug(f"找到匹配的结果: {title} - {href}")
            break
    
    if not target_link:
        raise MovieNotFoundError(__name__, dvdid)
    
    return target_link


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    # 搜索番号获取详情页链接
    detail_url = search_movie(movie.dvdid)
    
    # 进入详情页抓取信息
    html = get_html(detail_url)
    logger.debug(f"当前详情页 URL: {html.base_url}")
    
    # 抓取标题
    title_tags = html.xpath("//h1")
    if title_tags:
        full_title = title_tags[0].text_content().strip()
        # 移除标题中的番号部分
        if movie.dvdid:
            id_pattern = r'^\s*' + re.escape(movie.dvdid) + r'\s*'
            movie.title = re.sub(id_pattern, '', full_title, flags=re.IGNORECASE).strip()
            logger.debug(f"找到标题: {movie.title}")
    
    # 抓取封面
    cover_tags = html.xpath("//img[contains(@class, 'fixed-background-img')]/@src")
    if cover_tags:
        movie.cover = cover_tags[0]
    else:
        cover_tags = html.xpath("//img[contains(@class, 'player-skeleton-cover')]/@src")
        if cover_tags:
            movie.cover = cover_tags[0]
    
    # 抓取发行日期
    date_rows = html.xpath("//div[contains(@class, 'row') and contains(., '發片日期')]")
    if date_rows:
        date_cols = date_rows[0].xpath(".//div[contains(@class, 'col-auto')]")
        if len(date_cols) >= 2:
            date_text = date_cols[-1].text_content().strip()
            # 将 "2026年4月17日" 转换为 "2026-04-17"
            match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_text)
            if match:
                movie.publish_date = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
                logger.debug(f"找到发行日期: {movie.publish_date}")
    
    # 抓取剧情
    desc_div = html.xpath("//div[contains(@class, 'description-text')]")
    if desc_div:
        # description-text 下通常有多个p标签，第一个是前缀说明（如"SQTE-675为S-CUTE出品..."）
        # 后面的p标签是实际剧情
        paragraphs = desc_div[0].xpath(".//p")
        plot_parts = []
        for p in paragraphs:
            text = p.text_content().strip()
            if text:
                # 跳过前缀说明行（包含"出品"、"发行"、"成人影片"、"出演"）
                if re.search(r'为.*出品.*发行.*影片.*出演', text):
                    continue
                plot_parts.append(text)
        if plot_parts:
            movie.plot = '\n'.join(plot_parts)
            logger.debug(f"找到剧情: {movie.plot[:100]}...")
    
    # 抓取演员
    actress_section = html.xpath("//section[.//h2[contains(text(), '出演女優')]]")
    if actress_section:
        actress_names = []
        seen = set()
        for a in actress_section[0].xpath(".//a[@data-actress-name]"):
            name = a.get('data-actress-name', '').strip()
            if name and name not in seen:
                seen.add(name)
                actress_names.append(name)
        if actress_names:
            movie.actress = actress_names
            logger.debug(f"找到演员: {movie.actress}")
    
    # 抓取类型/标签
    keyword_section = html.xpath("//section[contains(@class, 'movie-keywords')]")
    if keyword_section:
        genres = []
        seen = set()
        for a in keyword_section[0].xpath(".//a[contains(@href, '/keywords/movie/')]"):
            genre = a.text_content().strip()
            # 去掉可能的前导 #
            genre = genre.lstrip('#')
            if genre and genre not in seen:
                seen.add(genre)
                genres.append(genre)
        if genres:
            movie.genre = genres
            logger.debug(f"找到类型: {movie.genre}")
    
    # 抓取片商/制作商
    issuer_links = html.xpath("//a[contains(@class, 'issuer-link')]")
    for link in issuer_links:
        issuer_text = link.text_content().strip()
        # 过滤掉像 "169部" 这样的统计文本
        if issuer_text and not re.match(r'^\d+部$', issuer_text):
            movie.producer = issuer_text
            logger.debug(f"找到片商: {movie.producer}")
            break
    
    # 抓取预览图（剧照）
    preview_imgs = html.xpath("//img[contains(@alt, '劇照') and contains(@class, 'img-fluid')]/@src")
    if preview_imgs:
        seen = set()
        previews = []
        for src in preview_imgs:
            if src not in seen:
                seen.add(src)
                previews.append(src)
        movie.preview_pics = previews
        logger.debug(f"找到预览图: {len(movie.preview_pics)} 张")
    
    # 确保至少抓到了标题或封面
    if not movie.title and not movie.cover:
        raise MovieNotFoundError(__name__, movie.dvdid)


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('SQTE-675')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
