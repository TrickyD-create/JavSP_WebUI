"""从Jav.Guru抓取数据"""
import re
import logging

from javsp.web.base import get_html
from javsp.web.exceptions import *
from javsp.datatype import MovieInfo


logger = logging.getLogger(__name__)
base_url = 'https://jav.guru'


def search_movie(dvdid):
    html = get_html(f'{base_url}/?s={dvdid}')
    dvdid_lower = dvdid.lower()
    all_links = html.xpath("//a[contains(@href, '/')]/@href")
    for link in all_links:
        skip_patterns = ['feed/rss', '?s=', '/search/', '/tag/', '/actress/',
                         '/studio/', '/director/', '/maker/', '/label/',
                         '/category/', '/page/', '/author/']
        if any(p in link for p in skip_patterns):
            continue
        if dvdid_lower in link.lower():
            if link.startswith('/'):
                link = f'{base_url}{link}'
            return link
    raise MovieNotFoundError(__name__, dvdid)


def parse_data(movie: MovieInfo):
    detail_url = search_movie(movie.dvdid)
    html = get_html(detail_url)

    title_tag = html.xpath("//title/text()")
    if title_tag:
        title = re.sub(r'\s*[-–|⋆]\s*Jav\s*Guru.*$', '', title_tag[0], flags=re.I).strip()
        title = re.sub(r'\s*⋆\s*Japanese porn Tube.*$', '', title).strip()
        title = re.sub(r'^\s*\[?' + re.escape(movie.dvdid) + r'\]?\s*', '', title, flags=re.I).strip()
        movie.title = title

    cover_imgs = html.xpath("//img[contains(@src, 'cdn.javmiku.com/wp-content/uploads')]/@src")
    exclude_keywords = ['logo', 'popular', 'icon', 'avatar']
    for img in cover_imgs:
        if not any(kw in img.lower() for kw in exclude_keywords):
            movie.cover = img
            break

    movie.url = detail_url

    # infoleft block has the core structured data
    infoleft = html.xpath("//div[contains(@class, 'infoleft')]")
    if infoleft:
        info_text = infoleft[0].text_content()

        # 使用标签边界来提取各字段
        labels_pattern = r'(Release\s*Date|Director|Studio|Label|Tags|Actor|Actress):\s*'
        parts = re.split(labels_pattern, info_text)
        current_label = None
        for part in parts:
            if part is None:
                continue
            part = part.strip()
            if re.match(r'^(Release\s*Date|Director|Studio|Label|Tags|Actor|Actress)$', part):
                current_label = part
                continue
            if current_label == 'Release Date':
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', part)
                if date_match:
                    movie.publish_date = date_match.group(1)
            elif current_label == 'Director' and part and not movie.director:
                movie.director = part.rstrip(',')
            elif current_label == 'Studio' and part and not movie.producer:
                movie.producer = part.rstrip(',')
            elif current_label == 'Label' and part and not movie.serial:
                movie.serial = part.rstrip(',')
            elif current_label == 'Tags' and part:
                raw_genres = [t.strip() for t in part.split(',') if t.strip()]
                cleaned = []
                for g in raw_genres:
                    g = re.sub(r'\s*\n\s*Series:.*$', '', g).strip()
                    cleaned.append(g)
                movie.genre = cleaned
            elif current_label == 'Actress' and part:
                actress = [a.strip() for a in part.split(',') if a.strip()]
                if actress:
                    movie.actress = actress

    # 如果 infoleft 没有提取到 actress，从链接中提取
    if not movie.actress:
        actress = []
        actress_links = html.xpath("//a[contains(@href,'/actress/')]/text()")
        seen = set()
        for a in actress_links:
            name = a.strip()
            if name and 'jav.guru' not in name and name not in seen:
                seen.add(name)
                actress.append(name)
        movie.actress = actress

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
