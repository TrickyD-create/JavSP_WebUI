"""从airav抓取数据"""
import re
import logging
from html import unescape


from javsp.web.base import Request, get_html
from javsp.web.exceptions import *
from javsp.config import Cfg
from javsp.datatype import MovieInfo

# 初始化Request实例
request = Request(use_scraper=True)
request.headers['Accept-Language'] = 'zh-TW,zh;q=0.9'
# 近期airav服务器似乎不稳定，时好时坏，单次查询平均在17秒左右，timeout时间增加到20秒
request.timeout = 20


logger = logging.getLogger(__name__)
base_url = 'https://airav.io'


def search_movie(dvdid):
    """通过搜索番号获取指定的影片在网站上的ID"""
    search_url = f'{base_url}/search_result?kw={dvdid}'
    html = get_html(search_url)
    
    # 打印当前页面的 URL，以便了解是否被重定向
    logger.debug(f"当前搜索页面 URL: {html.base_url}")
    
    # 寻找搜索结果
    video_boxes = html.xpath("//div[@class='oneVideo']")
    if not video_boxes:
        video_boxes = html.xpath("//div[contains(@class, 'oneVideo')]")
    
    target_link = None
    
    if not video_boxes:
        page_title = html.xpath('//title')[0].text_content() if html.xpath('//title') else '无标题'
        logger.debug(f"未找到搜索结果，页面标题: {page_title}")
        raise MovieNotFoundError(__name__, dvdid)
    
    logger.debug(f"总共找到 {len(video_boxes)} 个可能的结果")
    
    for i, box in enumerate(video_boxes):
        try:
            # 尝试获取标题
            title = ""
            title_tags = box.xpath(".//h5 | .//h4 | .//h3 | .//h2 | .//h1 | .//div | .//span")
            
            for tag in title_tags:
                if tag.text_content().strip():
                    title = tag.text_content().strip()
                    break
            
            logger.debug(f"结果 {i+1} 标题: {title}")
            
            # 尝试获取链接
            link = ""
            link_tags = box.xpath(".//a")
            if link_tags:
                link = link_tags[0].get('href')
                logger.debug(f"结果 {i+1} 链接: {link}")
            
            # 简单匹配：只要标题里包含番号或者链接里包含番号
            if dvdid.lower() in title.lower() or dvdid.lower() in link.lower():
                if link:
                    target_link = link
                    logger.debug(f"找到匹配的结果: {title} - {link}")
                    break
                else:
                    logger.debug(f"结果 {i+1} 匹配标题但无有效链接，继续搜索")
        except Exception as e:
            logger.debug(f"处理结果 {i+1} 时出错: {e}")
            pass
    
    if not target_link:
        raise MovieNotFoundError(__name__, dvdid)
    
    return target_link


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    # 搜索番号获取详情页链接
    detail_url = search_movie(movie.dvdid)
    
    # 进入详情页抓取信息 - 添加重试机制
    html = None
    retries = 3
    for attempt in range(retries):
        try:
            html = get_html(detail_url)
            break
        except Exception as e:
            logger.warning(f"尝试 {attempt+1}/{retries} 访问详情页失败: {e}")
            if attempt == retries - 1:
                raise
            import time
            time.sleep(2)  # 等待 2 秒后重试
    
    # 打印当前页面的 URL，以便了解是否被重定向
    if html is not None and hasattr(html, 'base_url'):
        logger.debug(f"当前详情页 URL: {html.base_url}")
    
    # 抓取标题 - 尝试多种可能的选择器
    movie.title = None
    title_selectors = [
        "//div[@class='video-title']//h1",
        "//div[contains(@class, 'video-title')]//h1",
        "//h1[@class='video-title']",
        "//h1",
        "//h2",
        "//h3",
        "//div[@class='title']",
        "//div[contains(@class, 'title')]"
    ]
    
    for selector in title_selectors:
        try:
            title_tags = html.xpath(selector)
            if title_tags:
                movie.title = title_tags[0].text_content().strip()
                logger.debug(f"使用选择器 '{selector}' 找到标题: {movie.title}")
                # 移除标题中的ID部分，避免最终拼接时重复
                if movie.title and movie.dvdid:
                    id_pattern = r'\b' + re.escape(movie.dvdid) + r'\b'
                    cleaned_title = re.sub(id_pattern, '', movie.title, flags=re.IGNORECASE)
                    cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()
                    cleaned_title = re.sub(r'^[\s\-\_\,\.]+|[\s\-\_\,\.]+$', '', cleaned_title)
                    if cleaned_title:
                        movie.title = cleaned_title
                        logger.debug(f"移除ID后的标题: {movie.title}")
                break
        except Exception as e:
            logger.debug(f"使用选择器 '{selector}' 查找标题时出错: {e}")
            continue
    
    # 抓取剧情介绍 - 尝试多种可能的选择器
    movie.plot = None
    plot_selectors = [
        "//div[@class='video-info']//p",
        "//div[contains(@class, 'video-info')]//p",
        "//div[@class='info']//p",
        "//div[contains(@class, 'info')]//p",
        "//div[@class='plot']",
        "//div[contains(@class, 'plot')]",
        "//p[@class='plot']",
        "//p[contains(@class, 'plot')]",
        "//div[@id='description']",
        "//div[contains(@id, 'description')]"
    ]
    
    for selector in plot_selectors:
        try:
            plot_tags = html.xpath(selector)
            if plot_tags:
                movie.plot = plot_tags[0].text_content().strip()
                logger.debug(f"使用选择器 '{selector}' 找到剧情: {movie.plot}")
                break
        except Exception as e:
            logger.debug(f"使用选择器 '{selector}' 查找剧情时出错: {e}")
            continue

    # 抓取封面
    movie.cover = None
    try:
        og_image = html.xpath("//meta[@property='og:image']/@content")
        if og_image:
            movie.cover = og_image[0].strip()
            logger.debug(f"找到封面: {movie.cover}")
    except Exception as e:
        logger.debug(f"查找封面时出错: {e}")

    # 抓取演员
    movie.actress = []
    try:
        for a in html.xpath("//div[contains(@class, 'info-list')]//a[contains(@href, 'actor')]"):
            name = a.text_content().strip()
            if name:
                movie.actress.append(name)
        if movie.actress:
            logger.debug(f"找到演员: {movie.actress}")
    except Exception as e:
        logger.debug(f"查找演员时出错: {e}")

    # 抓取类型/标签
    movie.genre = []
    try:
        for a in html.xpath("//div[contains(@class, 'info-list')]//a[contains(@href, 'tag')]"):
            tag = a.text_content().strip()
            if tag:
                movie.genre.append(tag)
        if movie.genre:
            logger.debug(f"找到类型: {movie.genre}")
    except Exception as e:
        logger.debug(f"查找类型时出错: {e}")

    # 抓取发行商/厂商
    movie.producer = None
    movie.publisher = None
    try:
        for li in html.xpath("//div[contains(@class, 'info-list')]//li"):
            text = li.text_content().strip()
            if '廠商：' in text or '厂商：' in text or '發行商：' in text or '发行商：' in text:
                # 提取链接文本作为厂商名
                a_tags = li.xpath(".//a")
                if a_tags:
                    producer_name = a_tags[0].text_content().strip()
                    movie.producer = producer_name
                    movie.publisher = producer_name
                    logger.debug(f"找到厂商: {producer_name}")
                    break
    except Exception as e:
        logger.debug(f"查找厂商时出错: {e}")

    # 抓取发行日期
    movie.premiered = None
    try:
        # 从视频信息区域查找日期（fa-clock图标旁边）
        for elem in html.xpath("//i[contains(@class, 'fa-clock')]"):
            parent = elem.getparent()
            if parent is not None:
                text = parent.text_content().strip()
                match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                if match:
                    movie.premiered = match.group(1)
                    logger.debug(f"找到日期: {movie.premiered}")
                    break
    except Exception as e:
        logger.debug(f"查找日期时出错: {e}")
    # 如果未找到，尝试从info-list中查找
    if not movie.premiered:
        try:
            for li in html.xpath("//div[contains(@class, 'info-list')]//li"):
                text = li.text_content().strip()
                if '日期：' in text or '時間：' in text or '时间：' in text:
                    match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                    if match:
                        movie.premiered = match.group(1)
                        logger.debug(f"从info-list找到日期: {movie.premiered}")
                        break
        except Exception as e:
            logger.debug(f"从info-list查找日期时出错: {e}")

    # 确保至少有一个字段被抓取
    if not movie.title and not movie.plot and not movie.cover:
        try:
            page_title = html.xpath('//title')[0].text_content() if html.xpath('//title') else '无标题'
            logger.debug(f"未找到标题或剧情，页面标题: {page_title}")
        except Exception:
            pass
        raise MovieNotFoundError(__name__, movie.dvdid)


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('MIAD-533')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
