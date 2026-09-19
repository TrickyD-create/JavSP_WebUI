import os
import shutil
import sys
import json
import time
import logging
from PIL import Image
from pydantic import ValidationError
from pydantic_extra_types.pendulum_dt import Duration
import requests
import threading
from typing import Dict, List
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

import colorama
import pretty_errors
from colorama import Fore, Style
from tqdm import tqdm


pretty_errors.configure(display_link=True)


from javsp.print import TqdmOut
from javsp.cropper import get_cropper


# 将StreamHandler的stream修改为TqdmOut，以与Tqdm协同工作
root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if type(handler) == logging.StreamHandler:
        handler.stream = TqdmOut

logger = logging.getLogger('main')


from javsp.lib import resource_path
from javsp.nfo import write_nfo
from javsp.metadata import merge_refresh_info, prepare_info_for_nfo, check_metadata_complete, info_from_dict, info_to_dict
from javsp.file import *
from javsp.func import *
from javsp.image import *
from javsp.datatype import Movie, MovieInfo
from javsp.actress_alias import load_actress_alias_map, normalize_movie_info_actress
from javsp.avid import get_id, get_cid
from javsp.web.base import download
from javsp.web.exceptions import *
from javsp.web.translate import translate_movie_info

from javsp.config import Cfg, UseJavDBCover
from javsp.prompt import prompt
from javsp.task import (
    TASK_DEFERRED,
    TASK_FAILED,
    TASK_SUCCESS,
    INTERACTIVE_RESCRAPE_FIELD_NAMES,
    RESCRAPE_SESSION_APPLYING,
    RESCRAPE_SESSION_SCRAPING,
    TaskStore,
    enqueue_movies,
    notify_run_summary,
)

actressAliasMap = {}


def import_crawlers():
    """按配置文件的抓取器顺序将该字段转换为抓取器的函数列表"""
    unknown_mods = []
    for _, mods in Cfg().crawler.selection.items():
        valid_mods = []
        for name in mods:
            try:
                # 导入fc2fan抓取器的前提: 配置了fc2fan的本地路径
                # if name == 'fc2fan' and (not os.path.isdir(Cfg().Crawler.fc2fan_local_path)):
                #     logger.debug('由于未配置有效的fc2fan路径，已跳过该抓取器')
                #     continue
                import_name = 'javsp.web.' + name
                __import__(import_name)
                valid_mods.append(import_name)  # 抓取器有效: 使用完整模块路径，便于程序实际使用
            except ModuleNotFoundError:
                unknown_mods.append(name)       # 抓取器无效: 仅使用模块名，便于显示
    if unknown_mods:
        logger.warning('配置的抓取器无效: ' + ', '.join(unknown_mods))


# 爬虫是IO密集型任务，可以通过多线程提升效率
def parallel_crawler(
    movie: Movie,
    tqdm_bar=None,
    task_store: TaskStore | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    force_crawlers: bool = False,
):
    """使用多线程抓取不同网站的数据"""
    def wrapper(parser, info: MovieInfo, retry, crawler_short: str):
        """对抓取器函数进行包装，便于更新提示信息和自动重试"""
        crawler_name = threading.current_thread().name
        task_info = f'Crawler: {crawler_name}: {info.dvdid}'
        started_at = time.time()
        if task_store:
            task_store.record_event(run_id, task_id, "info", "crawler_started", f"爬虫开始: {crawler_short}", {"crawler": crawler_short})
        for cnt in range(retry):
            try:
                parser(info)
                movie_id = info.dvdid or info.cid
                logger.debug(f"{crawler_name}: 抓取成功: '{movie_id}': '{info.url}'")
                setattr(info, 'success', True)
                if task_store:
                    fields = [k for k, v in vars(info).items() if v and k not in {'dvdid', 'cid'}]
                    elapsed = time.time() - started_at
                    task_store.record_crawler_result(
                        task_id, crawler_short, True, elapsed,
                        fields=fields, url=info.url, title=info.title, info=info
                    )
                    task_store.record_event(
                        run_id,
                        task_id,
                        "info",
                        "crawler_success",
                        f"爬虫成功: {crawler_short}",
                        {"crawler": crawler_short, "elapsed": elapsed, "fields": fields, "url": info.url, "title": info.title},
                    )
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'{crawler_name}: 抓取完成')
                break
            except MovieNotFoundError as e:
                logger.debug(e)
                if task_store:
                    elapsed = time.time() - started_at
                    task_store.record_crawler_result(task_id, crawler_short, False, elapsed, str(e))
                    task_store.record_event(run_id, task_id, "warning", "crawler_not_found", f"爬虫未找到影片: {crawler_short}", {"crawler": crawler_short, "elapsed": elapsed, "error": str(e)})
                break
            except MovieDuplicateError as e:
                logger.exception(e)
                if task_store:
                    elapsed = time.time() - started_at
                    task_store.record_crawler_result(task_id, crawler_short, False, elapsed, str(e))
                    task_store.record_event(run_id, task_id, "warning", "crawler_duplicate", f"爬虫返回重复结果: {crawler_short}", {"crawler": crawler_short, "elapsed": elapsed, "error": str(e)})
                break
            except (SiteBlocked, SitePermissionError, CredentialError) as e:
                logger.error(e)
                if task_store:
                    elapsed = time.time() - started_at
                    task_store.record_crawler_result(task_id, crawler_short, False, elapsed, str(e))
                    task_store.record_event(run_id, task_id, "error", "crawler_failed", f"爬虫失败: {crawler_short}", {"crawler": crawler_short, "elapsed": elapsed, "error": str(e)})
                break
            except requests.exceptions.RequestException as e:
                logger.debug(f'{crawler_name}: 网络错误，正在重试 ({cnt+1}/{retry}): \n{repr(e)}')
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'{crawler_name}: 网络错误，正在重试')
                if cnt == retry - 1 and task_store:
                    elapsed = time.time() - started_at
                    task_store.record_crawler_result(task_id, crawler_short, False, elapsed, repr(e))
                    task_store.record_event(run_id, task_id, "error", "crawler_failed", f"爬虫网络失败: {crawler_short}", {"crawler": crawler_short, "elapsed": elapsed, "error": repr(e)})
            except Exception as e:
                logger.exception(e)
                if cnt == retry - 1 and task_store:
                    elapsed = time.time() - started_at
                    task_store.record_crawler_result(task_id, crawler_short, False, elapsed, repr(e))
                    task_store.record_event(run_id, task_id, "error", "crawler_failed", f"爬虫异常: {crawler_short}", {"crawler": crawler_short, "elapsed": elapsed, "error": repr(e)})

    # 根据影片的数据源获取对应的抓取器
    crawler_mods: List[str] = list(Cfg().crawler.selection[movie.data_src])
    if task_store:
        skipped = [i for i in crawler_mods if not task_store.is_crawler_available(i, force_crawlers)]
        crawler_mods = [i for i in crawler_mods if task_store.is_crawler_available(i, force_crawlers)]
        if skipped:
            logger.warning('下列爬虫处于熔断状态，本轮跳过: ' + ', '.join(skipped))
            for crawler in skipped:
                task_store.record_event(run_id, task_id, "warning", "crawler_skipped", f"爬虫熔断跳过: {crawler}", {"crawler": crawler})

    all_info = {i: MovieInfo(movie) for i in crawler_mods}
    # 番号为cid但同时也有有效的dvdid时，也尝试使用普通模式进行抓取
    if movie.data_src == 'cid' and movie.dvdid:
        normal_mods = list(Cfg().crawler.selection.normal)
        if task_store:
            normal_mods = [i for i in normal_mods if task_store.is_crawler_available(i, force_crawlers)]
        crawler_mods = crawler_mods + normal_mods
        for i in all_info.values():
            i.dvdid = None
        for i in normal_mods:
            all_info[i] = MovieInfo(movie.dvdid)
    thread_pool = []
    for mod_partial, info in all_info.items():
        mod = f"javsp.web.{mod_partial}"
        parser = getattr(sys.modules[mod], 'parse_data')
        # 将all_info中的info实例传递给parser，parser抓取完成后，info实例的值已经完成更新
        # TODO: 抓取器如果带有parse_data_raw，说明它已经自行进行了重试处理，此时将重试次数设置为1
        if hasattr(sys.modules[mod], 'parse_data_raw'):
            th = threading.Thread(target=wrapper, name=mod_partial, args=(parser, info, 1, mod_partial))
        else:
            th = threading.Thread(target=wrapper, name=mod_partial, args=(parser, info, Cfg().network.retry, mod_partial))
        th.start()
        thread_pool.append(th)
    # 等待所有线程结束
    timeout = Cfg().network.retry * Cfg().network.timeout.total_seconds()
    for th in thread_pool:
        th: threading.Thread
        th.join(timeout=timeout)
    # 根据抓取结果更新影片类型判定
    if movie.data_src == 'cid' and movie.dvdid:
        titles = [all_info[i].title for i in Cfg().crawler.selection[movie.data_src]]
        if any(titles):
            movie.dvdid = None
            all_info = {k: v for k, v in all_info.items() if k in Cfg().crawler.selection['cid']}
        else:
            logger.debug(f'自动更正影片数据源类型: {movie.dvdid} ({movie.cid}): normal')
            movie.data_src = 'normal'
            movie.cid = None
            all_info = {k: v for k, v in all_info.items() if k not in Cfg().crawler.selection['cid']}
    # 删除抓取失败的站点对应的数据
    all_info = {k:v for k,v in all_info.items() if hasattr(v, 'success')}
    for info in all_info.values():
        del info.success
    # all_info 的键名已经是短名（如 javdb），无需再处理
    return all_info


FIELD_PRIORITY_ATTRS = ("title", "plot", "actress", "preview_pics")


def _field_priority_order(field: str, all_info: Dict[str, MovieInfo]) -> list[str]:
    priorities = getattr(getattr(Cfg().crawler, "field_priorities", None), field, []) or []
    ordered = []
    for name in priorities:
        if name in all_info and name not in ordered:
            ordered.append(name)
    ordered.extend(name for name in all_info if name not in ordered)
    return ordered


def _select_field_by_priority(field: str, all_info: Dict[str, MovieInfo]):
    for name in _field_priority_order(field, all_info):
        value = getattr(all_info[name], field, None)
        if value:
            return name, value
    return None, None


def info_summary(movie: Movie, all_info: Dict[str, MovieInfo]):
    """汇总多个来源的在线数据生成最终数据"""
    final_info = MovieInfo(movie)
    ########## 部分字段配置了专门的选取逻辑，先处理这些字段 ##########
    # genre
    if 'javdb' in all_info and all_info['javdb'].genre:
        final_info.genre = all_info['javdb'].genre

    ########## 移除所有抓取器数据中，标题尾部的女优名 ##########
    if Cfg().summarizer.title.remove_trailing_actor_name:
        for name, data in all_info.items():
            data.title = remove_trail_actor_in_title(data.title, data.actress)
    selected_by_field: dict[str, list[str]] = {}
    for attr in FIELD_PRIORITY_ATTRS:
        source, value = _select_field_by_priority(attr, all_info)
        if source:
            setattr(final_info, attr, value)
            selected_by_field.setdefault(source, []).append(attr)
    ########## 然后检查所有字段，如果某个字段还是默认值，则按照优先级选取数据 ##########
    # parser直接更新了all_info中的项目，而初始all_info是按照优先级生成的，已经符合配置的优先级顺序了
    # 按照优先级取出各个爬虫获取到的信息
    attrs = [i for i in dir(final_info) if not i.startswith('_')]
    covers, big_covers = [], []
    for name, data in all_info.items():
        absorbed = selected_by_field.get(name, []).copy()
        # 遍历所有属性，如果某一属性当前值为空而爬取的数据中含有该属性，则采用爬虫的属性
        for attr in attrs:
            incoming = getattr(data, attr)
            current = getattr(final_info, attr)
            if attr == 'cover':
                if incoming and (incoming not in covers):
                    covers.append(incoming)
                    absorbed.append(attr)
            elif attr == 'big_cover':
                if incoming and (incoming not in big_covers):
                    big_covers.append(incoming)
                    absorbed.append(attr)
            elif attr == 'uncensored':
                if (current is None) and (incoming is not None):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
            else:
                if (not current) and (incoming):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
        if absorbed:
            logger.info(f"从'{name}'中获取了字段: " + ' '.join(absorbed))
    # 使用网站的番号作为番号
    if Cfg().crawler.respect_site_avid:
        id_weight = {}
        for name, data in all_info.items():
            if data.title:
                if movie.dvdid:
                    id_weight.setdefault(data.dvdid, []).append(name)
                else:
                    id_weight.setdefault(data.cid, []).append(name)
        # 根据权重选择最终番号
        if id_weight:
            id_weight = {k:v for k, v in sorted(id_weight.items(), key=lambda x:len(x[1]), reverse=True)}
            final_id = list(id_weight.keys())[0]
            if movie.dvdid:
                final_info.dvdid = final_id
            else:
                final_info.cid = final_id
    # javdb封面有水印，优先采用其他站点的封面
    javdb_cover = getattr(all_info.get('javdb'), 'cover', None)
    if javdb_cover is not None:
        match Cfg().crawler.use_javdb_cover:
            case UseJavDBCover.fallback:
                covers.remove(javdb_cover)
                covers.append(javdb_cover)
            case UseJavDBCover.no:
                covers.remove(javdb_cover)

    setattr(final_info, 'covers', covers)
    setattr(final_info, 'big_covers', big_covers)
    # 对cover和big_cover赋值，避免后续检查必须字段时出错
    if covers:
        final_info.cover = covers[0]
    if big_covers:
        final_info.big_cover = big_covers[0]
    ########## 部分字段放在最后进行检查 ##########
    # 特殊的 genre
    if final_info.genre is None:
        final_info.genre = []
    if movie.hard_sub:
        final_info.genre.append('内嵌字幕')
    if movie.uncensored:
        final_info.genre.append('无码流出/破解')

    # 女优别名固定
    if Cfg().crawler.normalize_actress_name and final_info.actress:
        normalize_movie_info_actress(final_info, actressAliasMap)

    # 检查是否所有必需的字段都已经获得了值
    for attr in Cfg().crawler.required_keys:
        if not getattr(final_info, attr, None):
            logger.error(f"所有抓取器均未获取到字段: '{attr}'，抓取失败")
            return False
    # 必需字段均已获得了值：将最终的数据附加到movie
    movie.info = final_info
    return True

def generate_names(movie: Movie, force_move_files: bool | None = None):
    """按照模板生成相关文件的文件名"""

    def legalize_path(path: str):
        """
            Windows下文件名中不能包含换行 #467
            所以这里对文件路径进行合法化
        """
        return ''.join(c for c in path if c not in {'\n'})

    info = movie.info
    move_files = Cfg().summarizer.move_files if force_move_files is None else force_move_files
    # 准备用来填充命名模板的字典
    d = info.get_info_dic()

    if info.actress and len(info.actress) > Cfg().summarizer.path.max_actress_count:
        logging.debug('女优人数过多，按配置保留了其中的前n个: ' + ','.join(info.actress))
        actress = info.actress[:Cfg().summarizer.path.max_actress_count] + ['…']
    else:
        actress = info.actress
    d['actress'] = ','.join(actress) if actress else Cfg().summarizer.default.actress

    # 保存label供后面判断裁剪图片的方式使用
    setattr(info, 'label', d['label'].upper())
    # 处理字段：替换不能作为文件名的字符，移除首尾的空字符
    for k, v in d.items():
        d[k] = replace_illegal_chars(v.strip())

    # 生成nfo文件中的影片标题
    nfo_title = Cfg().summarizer.nfo.title_pattern.format(**d)
    setattr(info, 'nfo_title', nfo_title)
    
    # 使用字典填充模板，生成相关文件的路径（多分片影片要考虑CD-x部分）
    cdx = '' if len(movie.files) <= 1 else '-CD1'
    if hasattr(info, 'title_break'):
        title_break = info.title_break
    else:
        title_break = split_by_punc(d['title'])
    if hasattr(info, 'ori_title_break'):
        ori_title_break = info.ori_title_break
    else:
        ori_title_break = split_by_punc(d['rawtitle'])
    copyd = d.copy()

    def legalize_info():
        if movie.save_dir != None:
            movie.save_dir = legalize_path(movie.save_dir)
        if movie.nfo_file != None:
            movie.nfo_file = legalize_path(movie.nfo_file)
        if movie.fanart_file != None:
            movie.fanart_file = legalize_path(movie.fanart_file)
        if movie.poster_file != None:
            movie.poster_file = legalize_path(movie.poster_file)
        if d['title'] != copyd['title']:
            logger.info(f"自动截短标题为:\n{copyd['title']}")
        if d['rawtitle'] != copyd['rawtitle']:
            logger.info(f"自动截短原始标题为:\n{copyd['rawtitle']}")
        return

    copyd['num'] = copyd['num'] + movie.attr_str
    longest_ext = max((os.path.splitext(i)[1] for i in movie.files), key=len)
    for end in range(len(ori_title_break), 0, -1):
        copyd['rawtitle'] = replace_illegal_chars(''.join(ori_title_break[:end]).strip())
        for sub_end in range(len(title_break), 0, -1):
            copyd['title'] = replace_illegal_chars(''.join(title_break[:sub_end]).strip())
            if move_files:
                save_dir = os.path.normpath(Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
                basename = os.path.normpath(Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
            else:
                # 如果不整理文件，则保存抓取的数据到当前目录
                save_dir = os.path.dirname(movie.files[0])
                filebasename = os.path.basename(movie.files[0])
                ext = os.path.splitext(filebasename)[1]
                basename = filebasename.replace(ext, '')
            long_path = os.path.join(save_dir, basename+longest_ext)
            remaining = get_remaining_path_len(os.path.abspath(long_path))
            if remaining > 0:
                movie.save_dir = save_dir
                movie.basename = basename
                movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + '.nfo')
                movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + '.jpg')
                movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + '.jpg')
                return legalize_info()
    else:
        # 以防万一，当整理路径非常深或者标题起始很长一段没有标点符号时，硬性截短生成的名称
        copyd['title'] = copyd['title'][:remaining]
        copyd['rawtitle'] = copyd['rawtitle'][:remaining]
        # 如果不整理文件，则保存抓取的数据到当前目录
        if not move_files:
            save_dir = os.path.dirname(movie.files[0])
            filebasename = os.path.basename(movie.files[0])
            ext = os.path.splitext(filebasename)[1]
            basename = filebasename.replace(ext, '')
        else:
            save_dir = os.path.normpath(Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
            basename = os.path.normpath(Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
        movie.save_dir = save_dir
        movie.basename = basename

        movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + '.nfo')
        movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + '.jpg')
        movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + '.jpg')

        return legalize_info()

def reviewMovieID(all_movies, root):
    """人工检查每一部影片的番号"""
    count = len(all_movies)
    logger.info('进入手动模式检查番号: ')
    for i, movie in enumerate(all_movies, start=1):
        id = repr(movie)[7:-2]
        print(f'[{i}/{count}]\t{Fore.LIGHTMAGENTA_EX}{id}{Style.RESET_ALL}, 对应文件:')
        relpaths = [os.path.relpath(i, root) for i in movie.files]
        print('\n'.join(['  '+i for i in relpaths]))
        s = prompt("回车确认当前番号，或直接输入更正后的番号（如'ABC-123'或'cid:sqte00300'）", "更正后的番号")
        if not s:
            logger.info(f"已确认影片番号: {','.join(relpaths)}: {id}")
        else:
            s = s.strip()
            s_lc = s.lower()
            if s_lc.startswith(('cid:', 'cid=')):
                new_movie = Movie(cid=s_lc[4:])
                new_movie.data_src = 'cid'
                new_movie.files = movie.files
            elif s_lc.startswith('fc2'):
                new_movie = Movie(s)
                new_movie.data_src = 'fc2'
                new_movie.files = movie.files
            else:
                new_movie = Movie(s)
                new_movie.data_src = 'normal'
                new_movie.files = movie.files
            all_movies[i-1] = new_movie
            new_id = repr(new_movie)[7:-2]
            logger.info(f"已更正影片番号: {','.join(relpaths)}: {id} -> {new_id}")
        print()


SUBTITLE_MARK_FILE = Image.open(os.path.abspath(resource_path('image/sub_mark.png')))
UNCENSORED_MARK_FILE = Image.open(os.path.abspath(resource_path('image/unc_mark.png')))

def process_poster(movie: Movie):
    cropper = get_cropper()
    fanart_image = Image.open(movie.fanart_file)
    fanart_cropped = cropper.crop(fanart_image)

    if Cfg().summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPostion.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPostion.BOTTOM_LEFT)
    fanart_cropped.save(movie.poster_file)

def RunNormalMode(
    all_movies,
    task_store: TaskStore | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    force_crawlers: bool = False,
):
    """普通整理模式"""
    def check_step(result, msg='步骤错误'):
        """检查一个整理步骤的结果，并负责更新tqdm的进度"""
        if result:
            inner_bar.update()
        else:
            raise Exception(msg + '\n')

    outer_bar = tqdm(all_movies, desc='整理影片', ascii=True, leave=False)
    total_step = 6
    if Cfg().translator.engine:
        total_step += 1
    if Cfg().summarizer.extra_fanarts.enabled:
        total_step += 1

    return_movies = []
    for movie in outer_bar:
        try:
            # 初始化本次循环要整理影片任务
            filenames = [os.path.split(i)[1] for i in movie.files]
            print('正在整理: ' + ', '.join(filenames), flush=True)
            if task_store:
                task_store.record_event(run_id, task_id, "info", "scrape_started", f"开始刮削: {movie.dvdid or movie.cid}", {"files": movie.files})
            inner_bar = tqdm(total=total_step, desc='步骤', ascii=True, leave=False)
            # 依次执行各个步骤
            inner_bar.set_description(f'启动并发任务')
            all_info = parallel_crawler(movie, inner_bar, task_store=task_store, task_id=task_id, run_id=run_id, force_crawlers=force_crawlers)
            msg = f'为其配置的{len(Cfg().crawler.selection[movie.data_src])}个抓取器均未获取到影片信息'
            check_step(all_info, msg)

            inner_bar.set_description('汇总数据')
            has_required_keys = info_summary(movie, all_info)
            check_step(has_required_keys)
            if task_store:
                task_store.record_event(
                    run_id,
                    task_id,
                    "info",
                    "metadata_summary",
                    "汇总爬虫元数据",
                    {"sources": list(all_info.keys()), "title": getattr(movie.info, "title", None), "url": getattr(movie.info, "url", None)},
                )

            if Cfg().translator.engine:
                inner_bar.set_description('翻译影片信息')
                success = translate_movie_info(movie.info)
                check_step(success)
                if task_store:
                    task_store.record_event(run_id, task_id, "info", "metadata_translated", "翻译影片信息", {"success": bool(success)})

            generate_names(movie)
            check_step(movie.save_dir, '无法按命名规则生成目标文件夹')
            if not os.path.exists(movie.save_dir):
                os.makedirs(movie.save_dir)
            if task_store:
                task_store.record_event(
                    run_id,
                    task_id,
                    "info",
                    "paths_generated",
                    "生成保存路径",
                    {"save_dir": movie.save_dir, "nfo_file": movie.nfo_file, "fanart_file": movie.fanart_file, "poster_file": movie.poster_file},
                )

            inner_bar.set_description('下载封面图片')
            if Cfg().summarizer.cover.highres:
                cover_dl = download_cover(movie.info.covers, movie.fanart_file, movie.info.big_covers)
            else:
                cover_dl = download_cover(movie.info.covers, movie.fanart_file)
            check_step(cover_dl, '下载封面图片失败')
            cover, pic_path = cover_dl
            if task_store:
                task_store.record_event(run_id, task_id, "info", "cover_downloaded", "下载封面图片", {"cover": cover, "path": pic_path})
            # 确保实际下载的封面的url与即将写入到movie.info中的一致
            if cover != movie.info.cover:
                movie.info.cover = cover
            # 根据实际下载的封面的格式更新fanart/poster等图片的文件名
            if pic_path != movie.fanart_file:
                movie.fanart_file = pic_path
                actual_ext = os.path.splitext(pic_path)[1]
                movie.poster_file = os.path.splitext(movie.poster_file)[0] + actual_ext

            process_poster(movie)
            if task_store:
                task_store.record_event(run_id, task_id, "info", "poster_generated", "生成 poster", {"poster_file": movie.poster_file})

            check_step(True)

            if Cfg().summarizer.extra_fanarts.enabled:
                scrape_interval = Cfg().summarizer.extra_fanarts.scrap_interval.total_seconds()
                inner_bar.set_description('下载剧照')
                if movie.info.preview_pics:
                    extrafanartdir = movie.save_dir + '/extrafanart'
                    os.makedirs(extrafanartdir, exist_ok=True)
                    total = len(movie.info.preview_pics)
                    for i, pic_url in enumerate(movie.info.preview_pics):
                        inner_bar.set_description(f'下载剧照 {i+1}/{total}')
                        fanart_destination = f"{extrafanartdir}/{i}.png"
                        success = False
                        for attempt in range(Cfg().network.retry):
                            try:
                                info = download(pic_url, fanart_destination)
                                if valid_pic(fanart_destination):
                                    filesize = get_fmt_size(fanart_destination)
                                    width, height = get_pic_size(fanart_destination)
                                    elapsed = time.strftime("%M:%S", time.gmtime(info['elapsed']))
                                    speed = get_fmt_size(info['rate']) + '/s'
                                    logger.info(f"已下载剧照{i}.png: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                                    success = True
                                    break
                            except Exception as e:
                                logger.debug(f"下载剧照{i}.png 第{attempt+1}/{Cfg().network.retry}次尝试失败: {e}")
                                if os.path.exists(fanart_destination):
                                    try:
                                        os.remove(fanart_destination)
                                    except:
                                        pass
                            if attempt < Cfg().network.retry - 1:
                                time.sleep(1)
                        if not success:
                            logger.warning(f"下载剧照{i}.png全部{Cfg().network.retry}次尝试均失败，已跳过")
                        time.sleep(scrape_interval)
                    successful = [f"{extrafanartdir}/{i}.png" for i in range(total)
                                  if os.path.exists(f"{extrafanartdir}/{i}.png") and valid_pic(f"{extrafanartdir}/{i}.png")]
                    if successful:
                        movie.info.preview_pics = successful
                    if task_store:
                        task_store.record_event(
                            run_id,
                            task_id,
                            "info" if successful else "warning",
                            "preview_pics_downloaded",
                            f"下载剧照: {len(successful)}/{total}",
                            {"successful": successful, "total": total, "save_dir": extrafanartdir},
                        )
                check_step(True)

            inner_bar.set_description('写入NFO')
            write_nfo(movie.info, movie.nfo_file)
            check_step(True)
            if task_store:
                task_store.record_event(run_id, task_id, "info", "nfo_written", "写入 NFO", {"nfo_file": movie.nfo_file})
            if Cfg().summarizer.move_files:
                inner_bar.set_description('移动影片文件')
                movie.rename_files(Cfg().summarizer.path.hard_link)
                check_step(True)
                if task_store:
                    task_store.record_event(run_id, task_id, "info", "files_moved", "移动或硬链接影片文件", {"new_paths": getattr(movie, "new_paths", None), "hard_link": Cfg().summarizer.path.hard_link})
                print(f'整理完成，相关文件已保存到: {movie.save_dir}', flush=True)
            else:
                print(f'刮削完成，相关文件已保存到: {movie.nfo_file}', flush=True)

            if movie != all_movies[-1] and Cfg().crawler.sleep_after_scraping > Duration(0):
                time.sleep(Cfg().crawler.sleep_after_scraping.total_seconds())
            return_movies.append(movie)
        except Exception as e:
            logger.debug(e, exc_info=True)
            logger.error(f'整理失败: {movie.dvdid} - {e}')
            if task_store:
                task_store.record_event(run_id, task_id, "error", "scrape_failed", f"整理失败: {movie.dvdid or movie.cid} - {e}", {"error": repr(e)})
            print(f'整理失败: {movie.dvdid}', flush=True)
        finally:
            inner_bar.close()
    return return_movies


def download_cover(covers, fanart_path, big_covers=[]):
    """下载封面图片"""
    # 优先下载高清封面
    for url in big_covers:
        pic_path = get_pic_path(fanart_path, url)
        for _ in range(Cfg().network.retry):
            try:
                info = download(url, pic_path)
                if valid_pic(pic_path):
                    filesize = get_fmt_size(pic_path)
                    width, height = get_pic_size(pic_path)
                    elapsed = time.strftime("%M:%S", time.gmtime(info['elapsed']))
                    speed = get_fmt_size(info['rate']) + '/s'
                    logger.info(f"已下载高清封面: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                    return (url, pic_path)
            except requests.exceptions.HTTPError:
                # HTTPError通常说明猜测的高清封面地址实际不可用，因此不再重试
                break
    # 如果没有高清封面或高清封面下载失败
    for url in covers:
        pic_path = get_pic_path(fanart_path, url)
        for _ in range(Cfg().network.retry):
            try:
                download(url, pic_path)
                if valid_pic(pic_path):
                    logger.debug(f"已下载封面: '{url}'")
                    return (url, pic_path)
                else:
                    logger.debug(f"图片无效或已损坏: '{url}'，尝试更换下载地址")
                    break
            except Exception as e:
                logger.debug(e, exc_info=True)
    logger.error(f"下载封面图片失败")
    logger.debug('big_covers:'+str(big_covers) + ', covers'+str(covers))
    return None

def get_pic_path(fanart_path, url):
    fanart_base = os.path.splitext(fanart_path)[0]
    pic_extend = url.split('.')[-1]
    # 判断 url 是否带？后面的参数
    if '?' in pic_extend:
        pic_extend = pic_extend.split('?')[0]
        
    pic_path = fanart_base + "." + pic_extend
    return pic_path


def _default_sidecar_path(save_dir: str, pattern: str, info: MovieInfo) -> str:
    try:
        name = pattern.format(**info.get_info_dic())
    except Exception:
        name = pattern
    return os.path.abspath(os.path.join(save_dir, name + ".jpg"))


def _replace_rescrape_cover(store: TaskStore, task_id: int, task: dict, movie: Movie, info: MovieInfo, save_dir: str) -> tuple[str | None, str | None]:
    covers = getattr(info, "covers", None) or ([info.cover] if getattr(info, "cover", None) else [])
    big_covers = getattr(info, "big_covers", None) or ([info.big_cover] if getattr(info, "big_cover", None) else [])
    if not covers and not big_covers:
        return task.get("current_fanart_path"), task.get("current_poster_path")

    os.makedirs(save_dir, exist_ok=True)
    fanart_base = (
        task.get("current_fanart_path")
        or _default_sidecar_path(save_dir, Cfg().summarizer.fanart.basename_pattern, info)
    )
    poster_base = (
        task.get("current_poster_path")
        or _default_sidecar_path(save_dir, Cfg().summarizer.cover.basename_pattern, info)
    )
    temp_fanart = os.path.join(save_dir, f".javsp_rescrape_fanart_{task_id}.jpg")
    cover_dl = download_cover(covers, temp_fanart, big_covers if getattr(Cfg().summarizer.cover, "highres", False) else [])
    if not cover_dl:
        raise RuntimeError("重新下载封面失败")

    cover, downloaded_path = cover_dl
    actual_ext = os.path.splitext(downloaded_path)[1] or os.path.splitext(fanart_base)[1] or ".jpg"
    final_fanart = os.path.splitext(fanart_base)[0] + actual_ext
    final_poster = os.path.splitext(poster_base)[0] + actual_ext
    os.replace(downloaded_path, final_fanart)
    if cover != info.cover:
        info.cover = cover

    movie.info = info
    movie.save_dir = save_dir
    movie.fanart_file = final_fanart
    movie.poster_file = final_poster
    process_poster(movie)
    store.record_event(None, task_id, "info", "cover_downloaded", "重新刮削下载封面图片", {"cover": cover, "path": final_fanart})
    store.record_event(None, task_id, "info", "poster_generated", "重新刮削生成 poster", {"poster_file": final_poster})
    return final_fanart, final_poster


def _session_candidate_infos(session: dict, task: dict) -> dict[str, MovieInfo]:
    infos: dict[str, MovieInfo] = {}
    for name, data in (session.get("candidates") or {}).items():
        info = info_from_dict(data, task.get("avid"), task.get("data_src") or "normal")
        if info:
            infos[name] = info
    return infos


def _iter_actress_pic_pairs(info: MovieInfo):
    """统一爬虫返回的女优头像格式，不影响爬虫和旧重新刮削流程。"""
    pics = getattr(info, "actress_pics", None)
    if isinstance(pics, dict):
        return pics.items()
    if isinstance(pics, (list, tuple)):
        actresses = getattr(info, "actress", None)
        if isinstance(actresses, (list, tuple)) and len(actresses) == len(pics):
            # GGJAV 等爬虫按页面女优顺序返回头像列表，两者按位置对应。
            return zip(actresses, pics)
    return ()


def prepare_interactive_rescrape(store: TaskStore, root: str, session_id: int) -> bool:
    """只抓取并持久化候选元数据；这里禁止下载或改写任何媒体/NFO。"""
    session = store.get_rescrape_session(session_id)
    if not session or session.get("status") != RESCRAPE_SESSION_SCRAPING:
        return False
    task_id = int(session["task_id"])
    os.chdir(root)
    task, old_info = store.load_task_info(task_id, root)
    if not task or task.get("status") != TASK_SUCCESS or not old_info:
        store.fail_rescrape_session(session_id, "无法定位成功任务的原始元数据")
        return False
    try:
        movie = store.task_to_refresh_movie(task)
        all_info = parallel_crawler(movie, task_store=store, task_id=task_id, force_crawlers=True)
        if not all_info:
            store.fail_rescrape_session(session_id, "没有爬虫返回可用信息")
            return False
        if not info_summary(movie, all_info):
            store.fail_rescrape_session(session_id, "重新刮削未获取到必需字段")
            return False
        candidates = {name: info_to_dict(info) for name, info in all_info.items()}
        store.save_rescrape_candidates(session_id, candidates, movie.info)
        print(f"自由选择候选抓取完成：任务 {task_id}，来源 {len(candidates)} 个，等待人工选择", flush=True)
        return True
    except Exception as e:
        logger.debug(e, exc_info=True)
        store.fail_rescrape_session(session_id, repr(e))
        return False


def apply_interactive_rescrape(store: TaskStore, root: str, session_id: int) -> bool:
    """使用已持久化的候选和人工最终值落盘；不会再次运行爬虫。"""
    session = store.get_rescrape_session(session_id)
    if not session or session.get("status") != RESCRAPE_SESSION_APPLYING:
        return False
    task_id = int(session["task_id"])
    task = store.get_task(task_id)
    if not task:
        store.fail_rescrape_session(session_id, "任务不存在")
        return False
    all_info = _session_candidate_infos(session, task)
    if not all_info:
        store.fail_rescrape_session(session_id, "候选数据已丢失")
        return False
    try:
        ok = refresh_metadata_task(
            store,
            root,
            task_id,
            trigger_type="interactive-rescrape",
            force_crawlers=True,
            replace_all=True,
            prepared_crawler_infos=all_info,
            field_overrides=session.get("final_values") or {},
        )
        store.set_rescrape_session_result(session_id, ok, None if ok else "应用最终元数据失败")
        return ok
    except Exception as e:
        logger.debug(e, exc_info=True)
        store.fail_rescrape_session(session_id, repr(e))
        return False

def refresh_metadata_task(
    store: TaskStore,
    root: str,
    task_id: int,
    trigger_type: str = "manual",
    force_crawlers: bool = False,
    replace_all: bool = False,
    prepared_crawler_infos: dict[str, MovieInfo] | None = None,
    field_overrides: dict | None = None,
) -> bool:
    """Refresh metadata for a successful historical task without running filesystem scan rules."""
    os.chdir(root)
    task, old_info = store.load_task_info(task_id, root)
    refresh_id = store.start_metadata_refresh(task_id, trigger_type)
    if not task:
        store.finish_metadata_refresh(refresh_id, task_id, False, error="任务不存在")
        return False
    if task.get("status") != TASK_SUCCESS:
        store.finish_metadata_refresh(refresh_id, task_id, False, error="只有成功任务可以进行元数据优化")
        return False
    if not old_info:
        checked = store.check_task_metadata(task_id, root)
        reasons = checked.get("metadata_incomplete_reasons", []) if checked else []
        store.finish_metadata_refresh(refresh_id, task_id, False, reasons=reasons, error="无法定位原始元数据")
        return False

    movie = store.task_to_refresh_movie(task)
    try:
        all_info = prepared_crawler_infos
        if all_info is None:
            all_info = parallel_crawler(movie, task_store=store, task_id=task_id, force_crawlers=force_crawlers)
        if not all_info:
            complete, reasons = check_metadata_complete(old_info)
            store.finish_metadata_refresh(refresh_id, task_id, False, reasons=reasons, error="没有爬虫返回可用信息", info=old_info)
            return False
        if replace_all:
            if not info_summary(movie, all_info):
                complete, reasons = check_metadata_complete(old_info)
                store.finish_metadata_refresh(
                    refresh_id,
                    task_id,
                    False,
                    reasons=reasons,
                    error="重新刮削未获取到必需字段",
                    info=old_info,
                )
                return False
            merged = movie.info
            if field_overrides is not None:
                unknown = set(field_overrides) - INTERACTIVE_RESCRAPE_FIELD_NAMES
                if unknown:
                    raise ValueError("包含不可编辑字段: " + ", ".join(sorted(unknown)))
                for field in INTERACTIVE_RESCRAPE_FIELD_NAMES:
                    value = field_overrides.get(field)
                    if field in {"actress", "genre"}:
                        if value is None:
                            normalized = None
                        elif isinstance(value, list):
                            normalized = [str(item).strip() for item in value if str(item).strip()]
                            normalized = normalized or None
                        else:
                            raise ValueError(f"字段 {field} 必须是列表")
                    else:
                        if value is None:
                            normalized = None
                        elif isinstance(value, (str, int, float)):
                            normalized = str(value).strip() or None
                        else:
                            raise ValueError(f"字段 {field} 格式无效")
                    setattr(merged, field, normalized)
                # genre_norm 优先于 genre 写入 NFO；人工改过类型后必须重新使用最终 genre。
                merged.genre_norm = None
                # 演员头像是媒体字段，按最终演员名单从各爬虫结果中自动匹配。
                actress_pics = {}
                for source_info in all_info.values():
                    for name, url in _iter_actress_pic_pairs(source_info):
                        if name in (merged.actress or []) and name not in actress_pics:
                            actress_pics[name] = url
                merged.actress_pics = actress_pics or None
            old_data = info_to_dict(old_info)
            new_data = info_to_dict(merged)
            updated_fields = [
                field for field, value in new_data.items()
                if field not in {"dvdid", "cid"} and value != old_data.get(field)
            ]
        else:
            merged, updated_fields = merge_refresh_info(old_info, all_info)
        store.record_event(
            None,
            task_id,
            "info",
            "metadata_fields_merged",
            f"{'重新刮削并替换' if replace_all else '元数据字段合并'}: {', '.join(updated_fields) if updated_fields else '无变化'}",
            {"updated_fields": updated_fields, "sources": list(all_info.keys()), "replace_all": replace_all},
        )
        save_dir = task.get("current_save_dir") or task.get("save_dir") or movie.save_dir
        fanart_path = task.get("current_fanart_path")
        poster_path = task.get("current_poster_path")
        if replace_all and save_dir:
            fanart_path, poster_path = _replace_rescrape_cover(store, task_id, task, movie, merged, save_dir)
        should_download = Cfg().summarizer.extra_fanarts.enabled or replace_all or (not old_info.preview_pics)
        if should_download and merged.preview_pics and save_dir:
            scrape_interval = Cfg().summarizer.extra_fanarts.scrap_interval.total_seconds()
            extrafanartdir = os.path.join(save_dir, 'extrafanart')
            os.makedirs(extrafanartdir, exist_ok=True)
            successful = []
            total = len(merged.preview_pics)
            for i, pic_url in enumerate(merged.preview_pics):
                fanart_destination = os.path.join(extrafanartdir, f"{i}.png")
                # 已是目标目录内的有效本地文件，直接复用
                if not pic_url.startswith('http'):
                    real_src = os.path.realpath(pic_url)
                    real_dst = os.path.realpath(fanart_destination)
                    if real_src == real_dst:
                        if valid_pic(fanart_destination):
                            successful.append(fanart_destination)
                            store.record_event(None, task_id, "info", "preview_pic_reused", f"复用剧照: {i}.png", {"path": fanart_destination})
                    else:
                        try:
                            shutil.copyfile(real_src, fanart_destination)
                            if valid_pic(fanart_destination):
                                successful.append(fanart_destination)
                                store.record_event(None, task_id, "info", "preview_pic_reused", f"复制复用剧照: {i}.png", {"source": real_src, "path": fanart_destination})
                        except Exception:
                            pass
                    continue
                # 远程URL → 下载
                dl_success = False
                for attempt in range(Cfg().network.retry):
                    try:
                        info_data = download(pic_url, fanart_destination)
                        if valid_pic(fanart_destination):
                            filesize = get_fmt_size(fanart_destination)
                            width, height = get_pic_size(fanart_destination)
                            elapsed = time.strftime("%M:%S", time.gmtime(info_data['elapsed']))
                            speed = get_fmt_size(info_data['rate']) + '/s'
                            logger.info(f"优化-下载剧照{i}.png: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                            dl_success = True
                            break
                    except Exception as e:
                        logger.debug(f"优化-下载剧照{i}.png 第{attempt+1}/{Cfg().network.retry}次尝试失败: {e}")
                        if os.path.exists(fanart_destination) and not valid_pic(fanart_destination):
                            try:
                                os.remove(fanart_destination)
                            except Exception:
                                pass
                    if attempt < Cfg().network.retry - 1:
                        time.sleep(1)
                if dl_success:
                    successful.append(fanart_destination)
                    store.record_event(None, task_id, "info", "preview_pic_downloaded", f"下载剧照: {i}.png", {"url": pic_url, "path": fanart_destination})
                time.sleep(scrape_interval)
            if successful:
                merged.preview_pics = successful
                if 'preview_pics' not in updated_fields:
                    updated_fields.append('preview_pics')
                logger.info(f"优化-剧照下载完成: {len(successful)}/{total} 张, 保存至 {extrafanartdir}")
            else:
                merged.preview_pics = None
                logger.warning(f"优化-剧照下载全部失败: {total} 张均无法下载")
                store.record_event(None, task_id, "warning", "preview_pics_failed", f"剧照下载全部失败: {total} 张", {"total": total})
        prepare_info_for_nfo(merged)
        nfo_path = task.get("current_nfo_path")
        if nfo_path and not os.path.exists(nfo_path):
            nfo_path = None
        if not nfo_path:
            checked = store.check_task_metadata(task_id, root)
            nfo_path = checked.get("current_nfo_path") if checked else None
            if nfo_path and not os.path.exists(nfo_path):
                nfo_path = None
        if not nfo_path:
            complete, reasons = check_metadata_complete(merged)
            store.finish_metadata_refresh(refresh_id, task_id, False, updated_fields, reasons, "无法定位 NFO 路径", merged)
            return False
        write_nfo(merged, nfo_path)
        store.record_event(None, task_id, "info", "nfo_written", "优化后写入 NFO", {"nfo_path": nfo_path})
        complete, reasons = check_metadata_complete(merged)
        store.record_event(None, task_id, "info", "metadata_complete_checked", "完整性检查完成", {"complete": complete, "reasons": reasons})
        if replace_all:
            store.update_normalized_metadata(
                task_id,
                merged,
                current_save_dir=os.path.abspath(os.path.dirname(nfo_path)),
                current_nfo_path=os.path.abspath(nfo_path),
                current_fanart_path=os.path.abspath(fanart_path) if fanart_path else None,
                current_poster_path=os.path.abspath(poster_path) if poster_path else None,
            )
        store.finish_metadata_refresh(refresh_id, task_id, True, updated_fields, reasons, info=merged)
        logger.info(f"{'重新刮削' if replace_all else '元数据优化'}完成: {task.get('avid')} 更新字段: {', '.join(updated_fields) if updated_fields else '无'}")
        return True
    except Exception as e:
        logger.debug(e, exc_info=True)
        store.finish_metadata_refresh(refresh_id, task_id, False, error=repr(e), info=old_info)
        return False


def refresh_incomplete_metadata(
    store: TaskStore,
    root: str,
    trigger_type: str = "manual",
    manual: bool = True,
    limit: int | None = None,
) -> dict[str, int]:
    if not Cfg().metadata_complete.enabled:
        print("元数据完整性判断未启用", flush=True)
        return {"total": 0, "success": 0, "failed": 0}
    tasks = store.due_incomplete_metadata_tasks(root, manual=manual, limit=limit)
    success = failed = 0
    for task in tasks:
        ok = refresh_metadata_task(store, root, int(task["id"]), trigger_type=trigger_type, force_crawlers=(trigger_type == "manual"))
        if ok:
            success += 1
        else:
            failed += 1
    print(f"元数据优化完成：成功 {success}，失败 {failed}，共 {len(tasks)} 个任务", flush=True)
    return {"total": len(tasks), "success": success, "failed": failed}


def _find_history_movie_files(nfo_path: str, info: MovieInfo) -> list[str]:
    nfo_dir = os.path.dirname(os.path.abspath(nfo_path))
    candidates = []
    for filename in sorted(os.listdir(nfo_dir)):
        path = os.path.join(nfo_dir, filename)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(filename)[1].lower() in Cfg().scanner.filename_extensions:
            candidates.append(path)
    if not candidates:
        return []

    avid = info.dvdid or info.cid
    matched = []
    for path in candidates:
        if info.cid and get_cid(path) == info.cid:
            matched.append(path)
        elif info.dvdid and get_id(path) == info.dvdid:
            matched.append(path)
        elif avid and avid.lower() in os.path.basename(path).lower():
            matched.append(path)
    if matched:
        return matched
    if len(candidates) == 1:
        return candidates
    return []


def _planned_video_paths(movie: Movie) -> list[str]:
    if len(movie.files) == 1:
        fullpath = movie.files[0]
        ext = os.path.splitext(fullpath)[1]
        return [os.path.abspath(os.path.join(movie.save_dir, movie.basename + ext))]
    planned = []
    for i, fullpath in enumerate(movie.files, start=1):
        ext = os.path.splitext(fullpath)[1]
        planned.append(os.path.abspath(os.path.join(movie.save_dir, movie.basename + f'-CD{i}' + ext)))
    return planned


def _same_realpath(left: str | None, right: str | None) -> bool:
    return bool(left and right) and os.path.realpath(left) == os.path.realpath(right)


def _existing_pic_by_stem(directory: str, path: str | None) -> str | None:
    if not path:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        candidate = os.path.join(directory, stem + ext)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(path)


def _move_history_sidecars(old_dir: str, old_nfo_path: str, movie: Movie) -> None:
    old_dir = os.path.abspath(old_dir)
    target_dir = os.path.abspath(movie.save_dir)
    if _same_realpath(old_dir, target_dir):
        return
    video_paths = {os.path.realpath(path) for path in movie.files}
    old_nfo_real = os.path.realpath(old_nfo_path)
    for name in sorted(os.listdir(old_dir)):
        src = os.path.join(old_dir, name)
        src_real = os.path.realpath(src)
        if src_real in video_paths or src_real == old_nfo_real or src_real == os.path.realpath(target_dir):
            continue
        dst = os.path.join(target_dir, name)
        if os.path.exists(dst):
            raise FileExistsError(f"目标文件已存在: {dst}")
        shutil.move(src, dst)


def _rewrite_normalized_nfo(
    store: TaskStore,
    task_id: int,
    info: MovieInfo,
    nfo_path: str,
    run_id: int | None = None,
) -> None:
    prepare_info_for_nfo(info)
    write_nfo(info, nfo_path)
    store.update_normalized_metadata(
        task_id,
        info,
        run_id=run_id,
        current_nfo_path=os.path.abspath(nfo_path),
        current_save_dir=os.path.abspath(os.path.dirname(nfo_path)),
    )


def _move_and_rewrite_normalized_movie(
    store: TaskStore,
    naming_root: str,
    task_id: int,
    task: dict,
    info: MovieInfo,
    run_id: int | None = None,
) -> None:
    old_nfo_path = task.get("current_nfo_path")
    if not old_nfo_path or not os.path.exists(old_nfo_path):
        raise FileNotFoundError("无法定位历史 NFO")
    old_dir = os.path.abspath(os.path.dirname(old_nfo_path))
    movie_files = _find_history_movie_files(old_nfo_path, info)
    if not movie_files:
        raise FileNotFoundError("开启移动但未找到影片文件")

    if task.get("data_src") == "cid":
        movie = Movie(cid=task.get("avid"))
    else:
        movie = Movie(task.get("avid"))
    movie.data_src = task.get("data_src") or "normal"
    movie.files = movie_files
    movie.info = info

    previous_cwd = os.getcwd()
    try:
        os.chdir(naming_root)
        generate_names(movie, force_move_files=True)
        movie.save_dir = os.path.abspath(movie.save_dir)
        movie.nfo_file = os.path.abspath(movie.nfo_file)
        movie.fanart_file = os.path.abspath(movie.fanart_file)
        movie.poster_file = os.path.abspath(movie.poster_file)
    finally:
        os.chdir(previous_cwd)

    target_dir = os.path.abspath(movie.save_dir)
    same_dir = _same_realpath(old_dir, target_dir)
    if not same_dir and os.path.exists(target_dir):
        raise FileExistsError(f"目标目录已存在: {target_dir}")

    planned_videos = _planned_video_paths(movie)
    for src, dst in zip(movie.files, planned_videos):
        if os.path.exists(dst) and not _same_realpath(src, dst):
            raise FileExistsError(f"目标影片文件已存在: {dst}")
    if os.path.exists(movie.nfo_file) and not _same_realpath(old_nfo_path, movie.nfo_file):
        raise FileExistsError(f"目标 NFO 已存在: {movie.nfo_file}")

    os.makedirs(target_dir, exist_ok=same_dir)
    if not all(_same_realpath(src, dst) for src, dst in zip(movie.files, planned_videos)):
        movie.rename_files(Cfg().summarizer.path.hard_link)
    else:
        movie.new_paths = movie.files
    _move_history_sidecars(old_dir, old_nfo_path, movie)

    prepare_info_for_nfo(info)
    write_nfo(info, movie.nfo_file)
    if not _same_realpath(old_nfo_path, movie.nfo_file) and os.path.exists(old_nfo_path):
        os.remove(old_nfo_path)
    if os.path.isdir(old_dir) and not os.listdir(old_dir):
        os.rmdir(old_dir)

    fanart_path = _existing_pic_by_stem(target_dir, movie.fanart_file)
    poster_path = _existing_pic_by_stem(target_dir, movie.poster_file)
    store.update_normalized_metadata(
        task_id,
        info,
        run_id=run_id,
        current_files=[os.path.abspath(path) for path in getattr(movie, "new_paths", None) or movie.files],
        current_save_dir=target_dir,
        current_nfo_path=os.path.abspath(movie.nfo_file),
        current_fanart_path=fanart_path,
        current_poster_path=poster_path,
    )


def normalize_actress_metadata_dir(
    store: TaskStore,
    naming_root: str,
    import_root: str | None = None,
    move: bool = False,
) -> dict[str, int | dict]:
    naming_root = os.path.abspath(naming_root)
    import_root = os.path.abspath(import_root or naming_root)
    import_summary = store.import_legacy_metadata_dir(import_root)
    alias_map = load_actress_alias_map()
    run_id = store.start_run("normalize-actress")
    summary = {
        "scanned": int(import_summary.get("scanned") or 0),
        "imported": int(import_summary.get("created") or 0) + int(import_summary.get("updated") or 0),
        "changed": 0,
        "skipped": 0,
        "failed": int(import_summary.get("failed") or 0),
        "moved": 0,
        "failures": list(import_summary.get("failures") or []),
    }
    try:
        for task_id in import_summary.get("task_ids", []):
            task, info = store.load_task_info(int(task_id), import_root)
            if not task or not info:
                summary["failed"] += 1
                summary["failures"].append({"task_id": task_id, "reason": "无法载入任务元数据"})
                continue
            _, changed, details = normalize_movie_info_actress(info, alias_map)
            # 清理 title 中残留的女优名（包括别名映射表更新后新发现的名字）
            # 原始刮削时 remove_trail_actor_in_title 用的是爬虫返回的原始名，
            # 后续补充别名映射后，title 可能残留旧别名，需用展开的全量名字重新清理
            if info.title and info.actress and Cfg().summarizer.title.remove_trailing_actor_name:
                expanded_names = list(info.actress)
                for name in info.actress:
                    expanded_names.extend(alias_map.get(name, []))
                cleaned_title = remove_trail_actor_in_title(info.title, expanded_names)
                if cleaned_title != info.title:
                    logger.info(f"归一化时从标题清理女优名: '{info.title}' -> '{cleaned_title}'")
                    details["title_cleaned"] = {"before": info.title, "after": cleaned_title}
                    info.title = cleaned_title
                    changed = True
            if not changed:
                summary["skipped"] += 1
                continue
            try:
                nfo_path = task.get("current_nfo_path")
                if move:
                    _move_and_rewrite_normalized_movie(store, naming_root, int(task_id), task, info, run_id=run_id)
                    summary["moved"] += 1
                else:
                    if not nfo_path or not os.path.exists(nfo_path):
                        raise FileNotFoundError("无法定位历史 NFO")
                    _rewrite_normalized_nfo(store, int(task_id), info, nfo_path, run_id=run_id)
                summary["changed"] += 1
                store.record_event(
                    run_id,
                    int(task_id),
                    "info",
                    "actress_normalized",
                    "女优名归一化完成",
                    {"move": move, **details},
                )
            except Exception as e:
                logger.debug(e, exc_info=True)
                summary["failed"] += 1
                summary["failures"].append({"task_id": task_id, "path": task.get("current_nfo_path"), "reason": str(e)})
                store.record_event(
                    run_id,
                    int(task_id),
                    "error",
                    "actress_normalize_failed",
                    f"女优名归一化失败: {e}",
                    {"move": move, "error": repr(e)},
                )
        store.finish_normalize_run(run_id, summary)
    except Exception as e:
        store.finish_normalize_run(run_id, summary, status=TASK_FAILED, error=repr(e))
        raise

    print(
        "女优名归一化完成："
        f"扫描 {summary['scanned']}，变更 {summary['changed']}，移动 {summary['moved']}，"
        f"跳过 {summary['skipped']}，失败 {summary['failed']}",
        flush=True,
    )
    for item in summary.get("failures", [])[:20]:
        print(f"归一化失败: {item.get('path') or item.get('task_id')}: {item.get('reason')}", flush=True)
    return summary


def _has_flag(argv: list[str], *flags: str) -> bool:
    return any(arg in flags for arg in argv[1:])

def error_exit(success, err_info):
    """检查业务逻辑是否成功完成，如果失败则报错退出程序"""
    if not success:
        logger.error(err_info)
        sys.exit(1)


def _runtime_command(argv: list[str]) -> str:
    """从命令行中找出运行模式；未指定时保持旧的单次运行语义。"""
    commands = {'run-once', 'scan', 'work', 'daemon', 'status', 'metadata-check', 'metadata-import', 'refresh-incomplete', 'refresh-task', 'rescrape-task', 'rescrape-prepare', 'rescrape-apply', 'normalize-actress'}
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in {'-c', '--config', '-i', '--input'}:
            skip_next = True
            continue
        if arg.startswith('-'):
            continue
        if arg in commands:
            return arg
    return 'run-once'


def _strip_runtime_command(argv: list[str]) -> list[str]:
    commands = {'run-once', 'scan', 'work', 'daemon', 'status', 'metadata-check', 'metadata-import', 'refresh-incomplete', 'refresh-task', 'rescrape-task', 'rescrape-prepare', 'rescrape-apply', 'normalize-actress'}
    stripped = [argv[0]]
    removed = False
    for arg in argv[1:]:
        if not removed and arg in commands:
            removed = True
            continue
        stripped.append(arg)
    return stripped


def _positional_args(argv: list[str]) -> list[str]:
    values = []
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-c", "--config"}:
            skip_next = True
            continue
        if arg.startswith("--config="):
            continue
        if arg.startswith("-"):
            continue
        values.append(arg)
    return values


def init_config():
    try:
        # 配置 logging 输出到 stdout，以便 Web UI 捕获
        logging.basicConfig(
            level=logging.INFO,
            format='%(message)s',
            stream=sys.stdout
        )

        Cfg()
    except ValidationError as e:
        print(e.errors())
        exit(1)

    global actressAliasMap
    if Cfg().crawler.normalize_actress_name:
        actressAliasMap = load_actress_alias_map()

    colorama.init(autoreset=True)


def init_runtime():
    init_config()

    root = get_scan_dir(Cfg().scanner.input_directory)
    error_exit(root, '未选择要扫描的文件夹')
    # 导入抓取器，必须在chdir之前
    import_crawlers()
    return root


def run_legacy_once(root: str):
    """旧的单次运行流程，用于兼容和回归对照。"""
    os.chdir(root)

    print(f'扫描影片文件...')
    recognized = scan_movies(root)
    movie_count = len(recognized)
    recognize_fail = []
    error_exit(movie_count, '未找到影片文件')
    print(f'扫描影片文件：共找到 {movie_count} 部影片', flush=True)
    if Cfg().scanner.manual:
        reviewMovieID(recognized, root)
    try:
        RunNormalMode(recognized + recognize_fail)
    except Exception as e:
        logger.error(f'整理过程中发生未预期的错误: {e}')
        logger.debug(e, exc_info=True)

    sys.exit(0)


def scan_to_queue(store: TaskStore, root: str, run_id: int):
    os.chdir(root)
    sync_result = store.sync_file_inventory()
    if sync_result['purged'] > 0:
        print(f'文件同步：清理了 {sync_result["purged"]} 个失效任务（影片文件已不存在）', flush=True)
        store.record_event(run_id, None, "info", "file_sync",
                          f"文件同步：清理了 {sync_result['purged']} 个失效任务", sync_result)
    print(f'扫描影片文件...')
    store.record_event(run_id, None, "info", "scan_started", "开始扫描影片文件", {"root": os.path.abspath(root)})
    recognized = scan_movies(root)
    movie_count = len(recognized)
    if not movie_count:
        print('未找到影片文件', flush=True)
        store.update_run_scan_counts(run_id, 0, 0, 0, 0)
        store.record_event(run_id, None, "info", "scan_finished", "扫描完成：未找到影片文件", {"scan_total": 0})
        return {'scan_total': 0, 'enqueued': 0, 'deferred': 0, 'skipped': 0}
    print(f'扫描影片文件：共找到 {movie_count} 部影片', flush=True)
    store.record_event(run_id, None, "info", "scan_found_movies", f"发现影片: {movie_count}", {"scan_total": movie_count})
    if Cfg().scanner.manual:
        reviewMovieID(recognized, root)
    summary = enqueue_movies(store, recognized, run_id)
    print(
        f"任务入队完成：新增 {summary['enqueued']}，延迟 {summary['deferred']}，跳过 {summary['skipped']}",
        flush=True,
    )
    store.record_event(run_id, None, "info", "queue_summary", "任务入队完成", summary)
    return summary


def work_queue(store: TaskStore, root: str, run_id: int | None = None, limit: int | None = None):
    os.chdir(root)
    limit = limit if limit is not None else int(Cfg().daemon.max_movies_per_run or 0)
    success = failed = 0
    first_batch = True

    while True:
        tasks = store.get_due_tasks(limit or None)
        if not tasks:
            if first_batch:
                print('任务队列为空，没有需要处理的影片', flush=True)
                if run_id is not None:
                    store.record_event(run_id, None, "info", "work_queue_empty", "任务队列为空", {})
            break

        batch_total = len(tasks)
        if first_batch and run_id is not None:
            store.record_event(run_id, None, "info", "work_queue_started", f"开始处理队列: {batch_total} 个任务", {"total": batch_total})
        elif not first_batch:
            print(f'继续处理新入队任务: {batch_total} 个', flush=True)

        for task in tasks:
            task_id = int(task['id'])
            store.mark_running(task_id, run_id)
            file_action, file_reason = store.validate_task_files(task)
            if file_action == TASK_FAILED:
                store.mark_failure(task_id, 'file_missing', file_reason or '影片文件不可用', retryable=False)
                failed += 1
                print(f"任务跳过: {task['avid']} - {file_reason}", flush=True)
                continue
            if file_action == TASK_DEFERRED:
                store.mark_deferred(task_id, file_reason or '影片文件暂不可用')
                print(f"任务延迟: {task['avid']} - {file_reason}", flush=True)
                continue
            movie = store.task_to_movie(task)
            try:
                result = RunNormalMode([movie], task_store=store, task_id=task_id, run_id=run_id)
                if result:
                    save_dir = result[0].save_dir if result[0].save_dir else None
                    store.mark_success(task_id, save_dir, result[0])
                    success += 1
                else:
                    store.mark_failure(task_id, 'scrape', '刮削流程未返回成功结果', retryable=True)
                    failed += 1
            except Exception as e:
                logger.debug(e, exc_info=True)
                store.mark_failure(task_id, 'scrape', repr(e), retryable=True)
                failed += 1

        first_batch = False
        if limit and success + failed >= limit:
            break

    print(f'队列处理完成：成功 {success}，失败 {failed}', flush=True)
    if run_id is not None:
        store.record_event(run_id, None, "info", "work_queue_finished", "队列处理完成", {"success": success, "failed": failed, "total": success + failed})
    return {'success': success, 'failed': failed, 'total': success + failed}


def run_once_tasked(root: str, store: TaskStore, trigger: str = 'manual'):
    run_id = store.start_run(trigger)
    status = TASK_SUCCESS
    message = None
    try:
        scan_to_queue(store, root, run_id)
        work_queue(store, root, run_id)
    except SystemExit:
        status = TASK_FAILED
        message = '运行提前退出'
        raise
    except Exception as e:
        status = TASK_FAILED
        message = repr(e)
        logger.error(f'后台任务运行失败: {e}')
        logger.debug(e, exc_info=True)
    finally:
        store.finish_run(run_id, status, message)
        notify_run_summary(store, run_id)
    return run_id


def run_daemon(root: str, store: TaskStore):
    logger.info('JavSP daemon 已启动')
    while True:
        run_once_tasked(root, store, trigger='daemon')
        if Cfg().metadata_complete.enabled and Cfg().metadata_complete.auto_refresh:
            refresh_incomplete_metadata(store, root, trigger_type='auto', manual=False, limit=int(Cfg().daemon.max_movies_per_run or 0) or None)
        time.sleep(Cfg().daemon.scan_interval.total_seconds())


def print_status(store: TaskStore):
    latest = store.get_latest_run()
    counts = store.summarize()
    print('任务队列状态:')
    for key in ('pending', 'running', 'success', 'failed', 'skipped', 'deferred'):
        print(f'  {key}: {counts.get(key, 0)}')
    if latest:
        print('最近运行:')
        print(json.dumps(latest, ensure_ascii=False, indent=2))


def entry():
    command = _runtime_command(sys.argv)
    sys.argv = _strip_runtime_command(sys.argv)
    if command == 'status':
        init_config()
        store = TaskStore.from_config(Path.cwd())
        print_status(store)
        sys.exit(0)

    root = init_runtime()
    store = TaskStore.from_config(Path.cwd())

    match command:
        case 'run-once':
            run_once_tasked(root, store, trigger='manual')
        case 'scan':
            run_id = store.start_run('scan')
            try:
                scan_to_queue(store, root, run_id)
                store.finish_run(run_id, TASK_SUCCESS)
            except Exception as e:
                store.finish_run(run_id, TASK_FAILED, repr(e))
                raise
        case 'work':
            run_id = store.start_run('work')
            try:
                work_queue(store, root, run_id)
                store.finish_run(run_id, TASK_SUCCESS)
                notify_run_summary(store, run_id)
            except Exception as e:
                store.finish_run(run_id, TASK_FAILED, repr(e))
                raise
        case 'daemon':
            run_daemon(root, store)
        case 'metadata-check':
            checked = store.check_success_metadata(root)
            incomplete = len([i for i in checked if i.get('metadata_status') == 'incomplete'])
            path_unknown = len([i for i in checked if i.get('metadata_status') == 'path_unknown'])
            print(f"元数据完整性检查完成：不完整 {incomplete}，路径未知 {path_unknown}，共 {len(checked)} 个成功任务", flush=True)
        case 'metadata-import':
            args = _positional_args(sys.argv)
            import_root = args[-1] if args else None
            error_exit(import_root, '缺少历史目录路径')
            if not os.path.isabs(import_root):
                import_root = os.path.join(root, import_root)
            run_id = store.start_run('metadata-import')
            summary = store.import_legacy_metadata_dir(import_root)
            store.finish_import_run(run_id, summary)
            print(
                "历史目录导入完成："
                f"扫描 {summary['scanned']}，新增 {summary['created']}，更新 {summary['updated']}，"
                f"完整 {summary['complete']}，不完整 {summary['incomplete']}，失败 {summary['failed']}",
                flush=True,
            )
            for item in summary.get("failures", [])[:20]:
                print(f"导入失败: {item.get('path')}: {item.get('reason')}", flush=True)
        case 'refresh-incomplete':
            refresh_incomplete_metadata(store, root, trigger_type='manual', manual=True)
        case 'refresh-task':
            task_id = None
            for arg in sys.argv[1:]:
                if arg.isdigit():
                    task_id = int(arg)
                    break
            error_exit(task_id, '缺少任务 ID')
            ok = refresh_metadata_task(store, root, task_id, trigger_type='manual', force_crawlers=True)
            error_exit(ok, f'任务 {task_id} 元数据优化失败')
        case 'rescrape-task':
            task_id = None
            for arg in sys.argv[1:]:
                if arg.isdigit():
                    task_id = int(arg)
                    break
            error_exit(task_id, '缺少任务 ID')
            ok = refresh_metadata_task(store, root, task_id, trigger_type='rescrape', force_crawlers=True, replace_all=True)
            error_exit(ok, f'任务 {task_id} 重新刮削失败')
        case 'rescrape-prepare':
            session_ids = [int(arg) for arg in sys.argv[1:] if arg.isdigit()]
            session_id = session_ids[0] if session_ids else None
            error_exit(session_id, '缺少自由选择会话 ID')
            ok = prepare_interactive_rescrape(store, root, session_id)
            error_exit(ok, f'自由选择会话 {session_id} 候选抓取失败')
        case 'rescrape-apply':
            session_ids = [int(arg) for arg in sys.argv[1:] if arg.isdigit()]
            session_id = session_ids[0] if session_ids else None
            error_exit(session_id, '缺少自由选择会话 ID')
            ok = apply_interactive_rescrape(store, root, session_id)
            error_exit(ok, f'自由选择会话 {session_id} 应用失败')
        case 'normalize-actress':
            args = _positional_args(sys.argv)
            import_root = args[-1] if args else root
            if not os.path.isabs(import_root):
                import_root = os.path.join(root, import_root)
            normalize_actress_metadata_dir(store, root, import_root=import_root, move=_has_flag(sys.argv, '--move'))

    sys.exit(0)

if __name__ == "__main__":
    entry()
