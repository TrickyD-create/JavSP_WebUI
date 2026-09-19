import subprocess
import sys
import yaml
import os
import re
import json
import time
import logging
import threading
import traceback
import xml.etree.ElementTree as ET
from flask import Flask, render_template, Response, request, jsonify, send_from_directory
from waitress import serve
from apscheduler.schedulers.background import BackgroundScheduler
# 【关键改动】: 引入 IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from javsp.config import Cfg
from javsp.task import (
    INTERACTIVE_RESCRAPE_FIELDS,
    RESCRAPE_SESSION_AWAITING,
    RESCRAPE_SESSION_APPLYING,
    RESCRAPE_SESSION_SCRAPING,
    TaskStore,
)
from javsp.datatype import MovieInfo
from javsp.web.exceptions import CrawlerError, MovieNotFoundError

# --- 1. 基础配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
_current_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_current_dir)

# --- 2. 全局变量 ---
app = Flask(__name__, template_folder=_current_dir)
SCHEDULER_CONFIG_FILE = os.path.join(_current_dir, 'web_config.yml')
GENERAL_CONFIG_FILE = os.path.join(_root_dir, 'config.yml')
process_lock = threading.Lock()
_background_refresh_task = None
_background_refresh_lock = threading.Lock()
scheduler = BackgroundScheduler(daemon=True)


def ensure_scheduler_config():
    """首次启动从公开模板创建本地调度配置，保留已有个人配置。"""
    if os.path.exists(SCHEDULER_CONFIG_FILE):
        return
    template = os.path.join(_current_dir, 'web_config.example.yml')
    with open(template, 'r', encoding='utf-8') as source:
        content = source.read()
    try:
        with open(SCHEDULER_CONFIG_FILE, 'x', encoding='utf-8') as target:
            target.write(content)
    except FileExistsError:
        pass


ensure_scheduler_config()


def reload_javsp_config_cache():
    """ConfZ caches Cfg() in-process; reset it after WebUI config changes."""
    Cfg.confz_instance = None

# --- 2.5 影片刮削状态追踪 ---
class MovieScrapeStatus:
    def __init__(self):
        self.movies = {}  # {dvdid: {"title": "", "status": "pending"|"scraping"|"success"|"failed", "crawlers": {}, "save_dir": None, "num": ""}}
        self.current_dvdid = None
        self.total = 0
        self.completed = 0
        self.failed = 0

    def reset(self):
        self.movies = {}
        self.current_dvdid = None
        self.total = 0
        self.completed = 0
        self.failed = 0

    def set_total(self, count):
        self.total = count

    def start_movie(self, dvdid, num, data_src='normal', crawler_list=None):
        self.current_dvdid = dvdid
        crawlers = {}
        if crawler_list:
            for c in crawler_list:
                crawlers[c] = {"status": "pending", "fields": []}
        if dvdid not in self.movies:
            self.movies[dvdid] = {"title": "", "status": "scraping", "crawlers": crawlers, "save_dir": None, "num": num, "data_src": data_src}
        else:
            self.movies[dvdid]["status"] = "scraping"
            self.movies[dvdid]["crawlers"] = crawlers
            self.movies[dvdid]["data_src"] = data_src

    def add_crawler_result(self, crawler_name, fields):
        if self.current_dvdid and self.current_dvdid in self.movies:
            self.movies[self.current_dvdid]["crawlers"][crawler_name] = {"status": "success", "fields": fields}

    def mark_crawler_failed(self, crawler_name):
        if self.current_dvdid and self.current_dvdid in self.movies:
            self.movies[self.current_dvdid]["crawlers"][crawler_name] = {"status": "failed", "fields": []}

    def mark_crawler_skipped(self, crawler_name):
        if self.current_dvdid and self.current_dvdid in self.movies:
            self.movies[self.current_dvdid]["crawlers"][crawler_name] = {"status": "skipped", "fields": []}

    def set_save_dir(self, save_dir):
        if self.current_dvdid and self.current_dvdid in self.movies:
            self.movies[self.current_dvdid]["save_dir"] = save_dir

    def finish_movie(self, success, save_dir=None):
        if self.current_dvdid and self.current_dvdid in self.movies:
            movie = self.movies[self.current_dvdid]
            movie["status"] = "success" if success else "failed"
            if save_dir:
                movie["save_dir"] = save_dir
            # 影片失败时，将未标记为 success 的爬虫标记为 failed
            if not success:
                for c, data in movie["crawlers"].items():
                    if data["status"] == "pending":
                        data["status"] = "failed"
            else:
                # 影片成功时，将仍未确定的爬虫标记为 skipped（未参与或无需参与）
                for c, data in movie["crawlers"].items():
                    if data["status"] == "pending":
                        data["status"] = "skipped"
            if success:
                self.completed += 1
            else:
                self.failed += 1
        self.current_dvdid = None

    def set_movie_title(self, title):
        if self.current_dvdid and self.current_dvdid in self.movies:
            self.movies[self.current_dvdid]["title"] = title

    def get_summary(self):
        return {
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "movies": self.movies
        }

scrape_status = MovieScrapeStatus()

# ... (从这里开始，除了 update_scheduler 函数外，其他代码保持不变) ...

# --- 3. 核心功能：运行 JAVSP ---

def get_javsp_command(runtime_command='run-once', extra_args=None):
    """从主配置文件 config.yml 读取运行参数，并返回简洁的命令
    
    返回: (command_list, temp_config_path)
    temp_config_path 是临时配置文件路径，调用方负责在进程结束后删除
    """
    try:
        # 尝试读取主配置文件以获取额外的参数
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            general_config = yaml.safe_load(f) or {}
        args = general_config.get('javsp_args', [])
        
        # 创建临时配置文件，强制禁用 manual 模式（Web UI 无法提供 stdin 交互）
        import tempfile
        temp_config = dict(general_config)
        if 'scanner' not in temp_config:
            temp_config['scanner'] = {}
        temp_config['scanner']['manual'] = False
        
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False, encoding='utf-8')
        yaml.dump(temp_config, temp_file, allow_unicode=True, sort_keys=False)
        temp_file.close()
        
        command = [sys.executable, '-m', 'javsp', runtime_command, '-c', temp_file.name]
        if extra_args:
            command.extend(str(arg) for arg in extra_args)
        command.extend(str(arg) for arg in args)
        return command, temp_file.name
        
    except FileNotFoundError:
        # 如果配置文件不存在，使用不带额外参数的默认命令
        logging.warning(f"Main config file not found. Using default command.")
        return [sys.executable, '-m', 'javsp', runtime_command] + [str(arg) for arg in (extra_args or [])], None
    except Exception as e:
        # 如果读取文件时发生其他错误，也使用安全的默认命令
        logging.error(f"Error reading config.yml: {e}")
        return [sys.executable, '-m', 'javsp', runtime_command] + [str(arg) for arg in (extra_args or [])], None


def get_javsp_command_with_overrides(target_dir=None, move_files=None, target_file=None, runtime_command='run-once'):
    """构建 JAVSP 命令，支持覆盖扫描目录、移动选项和单文件限制。
    用于下载软件等外部系统下载完成后主动触发刮削。
    """
    try:
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            general_config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        general_config = {}

    temp_config = dict(general_config)
    if 'scanner' not in temp_config:
        temp_config['scanner'] = {}
    temp_config['scanner']['manual'] = False

    if target_dir:
        temp_config['scanner']['input_directory'] = str(target_dir)

    if move_files is not None:
        if 'summarizer' not in temp_config:
            temp_config['summarizer'] = {}
        temp_config['summarizer']['move_files'] = bool(move_files)

    if target_file:
        temp_config['scanner']['restrict_to_files'] = [str(target_file)]

    import tempfile
    temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False, encoding='utf-8')
    yaml.dump(temp_config, temp_file, allow_unicode=True, sort_keys=False)
    temp_file.close()

    args = general_config.get('javsp_args', [])
    command = [sys.executable, '-m', 'javsp', runtime_command, '-c', temp_file.name]
    command.extend(str(arg) for arg in args)
    return command, temp_file.name


def get_task_store():
    """按主配置文件定位后台状态库；避免 Web 进程依赖 Cfg 的缓存状态。"""
    db_path = os.path.join(_root_dir, 'data', 'javsp_state.db')
    try:
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        configured = config.get('daemon', {}).get('state_db')
        if configured:
            db_path = str(configured)
            if not os.path.isabs(db_path):
                db_path = os.path.join(_root_dir, db_path)
    except Exception:
        pass
    return TaskStore(db_path)


def _cleanup_temp_config(temp_path):
    """删除临时配置文件"""
    if temp_path and os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass

def get_crawlers_by_type(data_src='normal'):
    """读取主配置获取指定类型的爬虫列表"""
    try:
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        crawler_cfg = config.get('crawler', {})
        sel = crawler_cfg.get('selection', {})
        crawlers = list(sel.get(data_src, []))
        # 对于 cid 类型且配置了 dvdid 的影片，javsp 也会尝试 normal 类型的爬虫
        # 这里为了完整性，也把 normal 类型的爬虫加上（前端会动态处理）
        if data_src == 'cid':
            normal_crawlers = list(sel.get('normal', []))
            for c in normal_crawlers:
                if c not in crawlers:
                    crawlers.append(c)
        return crawlers
    except Exception:
        return []


def get_scan_directory():
    """读取主配置获取影片扫描目录（javsp 会 os.chdir 到此目录）"""
    try:
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        scan_dir = config.get('scanner', {}).get('input_directory')
        if scan_dir:
            scan_dir = str(scan_dir).strip()
            if not os.path.isabs(scan_dir):
                scan_dir = os.path.join(_root_dir, scan_dir)
            return os.path.normpath(scan_dir)
    except Exception:
        pass
    return _root_dir

# --- 4. 后台定时任务逻辑 (APScheduler) ---
def scheduled_run_javsp():
    if not process_lock.acquire(blocking=False):
        logging.warning("[Scheduler] JAVSP is already running. Skipping this scheduled execution.")
        return
    temp_config_path = None
    try:
        command, temp_config_path = get_javsp_command()
        logging.info(f"[Scheduler] Executing command: {' '.join(command)}")
        process = subprocess.run(
            command, cwd=_root_dir, capture_output=True, text=True, encoding='utf-8', check=False
        )
        if process.returncode == 0:
            logging.info(f"[Scheduler] JAVSP finished successfully.\nOutput:\n{process.stdout}")
        else:
            logging.error(f"[Scheduler] JAVSP failed with code {process.returncode}.\nError:\n{process.stderr}")
    except Exception as e:
        logging.error(f"[Scheduler] An unexpected error occurred: {e}")
    finally:
        process_lock.release()
        _cleanup_temp_config(temp_config_path)
        logging.info("[Scheduler] Run lock released.")


def scheduled_run_refresh():
    """定时触发元数据优化（refresh-incomplete）。"""
    ok, msg = _run_javsp_background('refresh-incomplete')
    if ok:
        logging.info(f"[RefreshScheduler] 元数据优化已触发: {msg}")
    else:
        logging.warning(f"[RefreshScheduler] 无法触发优化: {msg}")

# 【关键改动】: 这是全新升级的 update_scheduler 函数
def _build_trigger(section_config: dict, section_name: str):
    """从配置节构建 APScheduler trigger，返回 (trigger, log_prefix) 或抛异常。"""
    mode = section_config.get('mode', 'cron')
    if mode == 'cron':
        cron_params = section_config.get('cron', {})
        time_str = cron_params.get('time', '03:00')
        try:
            hour, minute = map(int, time_str.split(':'))
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError(f"Invalid time range: {hour}:{minute:02d}")
        except ValueError:
            raise ValueError(f"Invalid cron time format: '{time_str}', expected HH:MM")
        return CronTrigger(hour=hour, minute=minute), f"[{section_name}] cron: daily at {hour:02d}:{minute:02d}"

    elif mode == 'interval':
        interval_params = section_config.get('interval', {})
        valid = {k: v for k, v in interval_params.items() if k in ['days', 'hours', 'minutes', 'seconds'] and v > 0}
        if not valid:
            raise ValueError("Interval mode requires at least one of: days, hours, minutes, seconds")
        desc = ", ".join([f"{v} {k}" for k, v in valid.items()])
        return IntervalTrigger(**valid), f"[{section_name}] interval: every {desc}"

    else:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'cron' or 'interval'.")


def update_scheduler():
    """根据 web_config.yml 更新定时任务，分别支持刮削和优化两个独立调度器。"""
    scheduler.remove_all_jobs()
    result = {"success": True, "message": ""}
    try:
        with open(SCHEDULER_CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logging.info("Scheduler config file not found. Scheduler is disabled.")
        return result

    messages: list[str] = []
    has_error = False

    # ── 刮削调度器 ──
    scrape_cfg = config.get('scheduler', {})
    if scrape_cfg.get('enabled', False):
        try:
            trigger, msg = _build_trigger(scrape_cfg, 'scrape')
            scheduler.add_job(scheduled_run_javsp, trigger, id='javsp_run', replace_existing=True)
            logging.info(msg)
            messages.append(msg)
        except (ValueError, TypeError) as e:
            logging.error(f"Failed to configure scrape scheduler: {e}")
            messages.append(f"刮削调度器: {e}")
            has_error = True

    # ── 优化调度器 ──
    refresh_cfg = config.get('refresh_scheduler', {})
    if refresh_cfg.get('enabled', False):
        try:
            trigger, msg = _build_trigger(refresh_cfg, 'refresh')
            scheduler.add_job(scheduled_run_refresh, trigger, id='javsp_refresh', replace_existing=True)
            logging.info(msg)
            messages.append(msg)
        except (ValueError, TypeError) as e:
            logging.error(f"Failed to configure refresh scheduler: {e}")
            messages.append(f"优化调度器: {e}")
            has_error = True

    if has_error:
        result["success"] = False
    if messages:
        result["message"] = "; ".join(messages)
    elif not scheduler.get_jobs():
        logging.info("No scheduler jobs enabled in web_config.yml.")

    return result

# --- 5. Flask 路由 (Web UI 和 API) ---
# ... (这部分路由代码完全不需要修改，保持原样即可) ...
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/guide')
def guide():
    return render_template('guide.html')

@app.route('/js-yaml.min.js')
def serve_js_yaml():
    return send_from_directory(_current_dir, 'js-yaml.min.js', mimetype='application/javascript')

@app.route('/run-app')
def execute_and_stream_output():
    if not process_lock.acquire(blocking=False):
        def generator_locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '另一个进程已在运行中，请稍后重试。'})}\n\n"
            yield f"data: {json.dumps({'type': 'finish', 'return_code': -1, 'summary': scrape_status.get_summary()})}\n\n"
        return Response(generator_locked(), mimetype='text/event-stream')

    scrape_status.reset()

    def parse_log_line(line):
        """解析日志行，提取影片和爬虫状态"""

        # 扫描影片文件：共找到 X 部影片
        total_match = re.search(r'扫描影片文件[：:]\s*共找到\s*(\d+)\s*部', line)
        if total_match:
            count = int(total_match.group(1))
            logging.info(f"检测到扫描完成，共 {count} 部影片")
            scrape_status.set_total(count)
            return {"type": "scan_complete", "total": count}

        # 正在整理: filename1, filename2
        scraping_match = re.search(r'正在整理:\s*(.+)', line)
        if scraping_match:
            filenames = scraping_match.group(1)
            # 尝试提取番号
            dvdid_match = re.search(r'([A-Z]{2,}-\d+|\d{4,}-\d+|FC2[-\s]?\d+)', filenames)
            dvdid = dvdid_match.group(1) if dvdid_match else filenames[:20]
            num_match = re.search(r'\[([^\]]+)\]', filenames)
            num = num_match.group(1) if num_match else dvdid
            # 检测影片数据类型: fc2, cid, 或 normal
            data_src = 'normal'
            if 'FC2' in filenames.upper() or re.search(r'FC2[-\s]?\d+', filenames, re.IGNORECASE):
                data_src = 'fc2'
            elif re.search(r'cid[=:][a-z0-9]+', filenames, re.IGNORECASE):
                data_src = 'cid'
            logging.info(f"开始刮削影片: {dvdid} (类型: {data_src})")
            crawler_list = get_crawlers_by_type(data_src)
            scrape_status.start_movie(dvdid, num, data_src, crawler_list)
            return {"type": "movie_start", "dvdid": dvdid, "num": num, "filenames": filenames, "data_src": data_src, "crawlers": crawler_list}

        # 从'xxx'中获取了字段: xxx
        crawler_match = re.search(r"从'([^']+)'中获取了字段:\s*(.+)", line)
        if crawler_match:
            crawler_name = crawler_match.group(1)
            fields = crawler_match.group(2).split()
            scrape_status.add_crawler_result(crawler_name, fields)
            return {"type": "crawler_success", "dvdid": scrape_status.current_dvdid, "crawler": crawler_name, "fields": fields}

        # 【调试】检查是否有爬虫相关的日志行
        if '抓取' in line or '爬虫' in line or 'crawler' in line.lower():
            logging.info(f"[PARSE DEBUG] potential crawler log: {line}")

        # 所有抓取器均未获取到字段 - 抓取失败
        if '所有抓取器均未获取到字段' in line or '抓取失败' in line:
            dvdid = scrape_status.current_dvdid
            # 将所有 pending 状态的爬虫标记为 failed
            if dvdid and dvdid in scrape_status.movies:
                for c, data in scrape_status.movies[dvdid]["crawlers"].items():
                    if data["status"] == "pending":
                        scrape_status.mark_crawler_failed(c)
            scrape_status.finish_movie(False)
            return {"type": "movie_failed", "dvdid": dvdid, "reason": "crawler_failed"}

        # 整理完成，相关文件已保存到: path
        complete_match = re.search(r'整理完成.*?保存到:\s*(.+)', line)
        if complete_match:
            save_dir = complete_match.group(1).strip()
            # javsp 会 os.chdir 到扫描目录，相对路径基于此目录
            if not os.path.isabs(save_dir):
                save_dir = os.path.join(get_scan_directory(), save_dir)
            dvdid = scrape_status.current_dvdid
            scrape_status.set_save_dir(save_dir)
            scrape_status.finish_movie(True, save_dir)
            return {"type": "movie_success", "dvdid": dvdid, "save_dir": save_dir}

        # 刮削完成（不移动文件的情况）
        nfo_match = re.search(r'刮削完成.*?保存到:\s*(.+\.nfo)', line)
        if nfo_match:
            nfo_path = nfo_match.group(1).strip()
            save_dir = os.path.dirname(nfo_path)
            # javsp 会 os.chdir 到扫描目录，相对路径基于此目录
            if not os.path.isabs(save_dir):
                save_dir = os.path.join(get_scan_directory(), save_dir)
            dvdid = scrape_status.current_dvdid
            scrape_status.set_save_dir(save_dir)
            scrape_status.finish_movie(True, save_dir)
            return {"type": "movie_success", "dvdid": dvdid, "save_dir": save_dir}

        # 整理失败
        failed_match = re.search(r'整理失败:\s*(.+)', line)
        if failed_match:
            error_info = failed_match.group(1).strip()
            dvdid = scrape_status.current_dvdid
            scrape_status.finish_movie(False)
            return {"type": "movie_failed", "dvdid": dvdid, "reason": error_info}

        return None

    def generate_output():
        temp_config_path = None
        try:
            command, temp_config_path = get_javsp_command()
            logging.info(f"执行命令: {' '.join(command)}")

            # 发送初始状态
            yield f"data: {json.dumps({'type': 'start', 'command': ' '.join(command)})}\n\n"

            new_env = os.environ.copy()
            new_env["PYTHONUNBUFFERED"] = "1"

            process = subprocess.Popen(
                command,
                cwd=_root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                bufsize=1,
                env=new_env
            )

            for line in iter(process.stdout.readline, ''):
                if line:
                    line = line.rstrip()
                    # 解析日志行
                    parsed = parse_log_line(line)

                    if parsed:
                        # 发送结构化事件
                        yield f"data: {json.dumps(parsed)}\n\n"

                    # 同时发送原始行供调试用（可选择性显示）
                    yield f"data: {json.dumps({'type': 'log', 'content': line})}\n\n"

            process.stdout.close()
            return_code = process.wait()
            logging.info(f"JAVSP 进程结束，返回码: {return_code}")

            # 发送最终状态
            yield f"data: {json.dumps({'type': 'finish', 'return_code': return_code, 'summary': scrape_status.get_summary()})}\n\n"

        except Exception as e:
            logging.error(f"SSE 生成器异常: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            yield f"data: {json.dumps({'type': 'finish', 'return_code': -1})}\n\n"
        finally:
            process_lock.release()
            if temp_config_path:
                _cleanup_temp_config(temp_config_path)

    return Response(generate_output(), mimetype='text/event-stream')


@app.route('/api/scrape_status')
def get_scrape_status():
    """获取当前刮削状态"""
    return jsonify(scrape_status.get_summary())


@app.route('/api/clear_scrape_status', methods=['POST'])
def clear_scrape_status():
    """清除当前刮削状态"""
    scrape_status.reset()
    return jsonify({"message": "已清除影片列表"})


@app.route('/api/run-now', methods=['POST'])
def run_now_api():
    """手动触发一次后台任务周期（扫描 + 入队 + 消费队列）。"""
    if not process_lock.acquire(blocking=False):
        return jsonify({"message": "另一个任务已在运行中，请稍后重试。"}), 409

    def runner():
        temp_config_path = None
        try:
            command, temp_config_path = get_javsp_command()
            logging.info(f"[Manual] Executing command: {' '.join(command)}")
            process = subprocess.run(
                command, cwd=_root_dir, capture_output=True, text=True, encoding='utf-8', check=False
            )
            if process.returncode == 0:
                logging.info(f"[Manual] JAVSP finished successfully.\nOutput:\n{process.stdout}")
            else:
                logging.error(f"[Manual] JAVSP failed with code {process.returncode}.\nOutput:\n{process.stdout}\nError:\n{process.stderr}")
        except Exception as e:
            logging.error(f"[Manual] An unexpected error occurred: {e}")
        finally:
            _cleanup_temp_config(temp_config_path)
            process_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"message": "已触发手动运行。"}), 202


@app.route('/api/trigger-scrape', methods=['POST'])
def trigger_scrape_api():
    """供下载软件下载完成后调用，触发指定目录的刮削。
    支持 JSON / form / query 参数:
      - target_dir: 可选，刮削目标目录（不传则使用 config.yml 默认目录）
      - target_file: 可选，只刮削指定文件（不传则扫描整个目录）
      - move_files: 可选，是否移动文件（不传则使用 config.yml 默认值）
    """
    data = request.get_json(silent=True) or {}
    target_dir = str(
        data.get('target_dir') or request.form.get('target_dir') or request.args.get('target_dir') or ''
    ).strip() or None
    target_file = str(
        data.get('target_file') or request.form.get('target_file') or request.args.get('target_file') or ''
    ).strip() or None
    raw_move = (
        data.get('move_files')
        or request.form.get('move_files')
        or request.args.get('move_files')
    )
    if raw_move is not None:
        raw_str = str(raw_move).strip().lower()
        move_files = raw_str in ('true', '1', 'yes')
    else:
        move_files = None

    # 优先尝试完整 run-once；若 busy 则降级为仅扫描入队，运行中的 worker 会处理
    if not process_lock.locked():
        def runner():
            temp_config_path = None
            try:
                command, temp_config_path = get_javsp_command_with_overrides(target_dir, move_files, target_file, 'run-once')
                logging.info(f"[Trigger] Executing command: {' '.join(command)}")
                process = subprocess.run(
                    command, cwd=_root_dir, capture_output=True, text=True, encoding='utf-8', check=False
                )
                if process.returncode == 0:
                    logging.info(f"[Trigger] 刮削完成。\nOutput:\n{process.stdout}")
                else:
                    logging.error(f"[Trigger] 刮削失败。\nOutput:\n{process.stdout}\nError:\n{process.stderr}")
            except Exception as e:
                logging.error(f"[Trigger] 异常: {e}")
            finally:
                _cleanup_temp_config(temp_config_path)
                try:
                    process_lock.release()
                except RuntimeError:
                    pass

        process_lock.acquire(blocking=False)
        threading.Thread(target=runner, daemon=True).start()
        return jsonify({
            "message": "刮削任务已触发",
            "status": "accepted",
            "target_dir": target_dir or "(使用默认目录)",
            "target_file": target_file or "(使用整个目录)",
            "move_files": "(使用默认配置)" if move_files is None else move_files,
        }), 202

    # 锁被占用：降级为快速扫描入队，不阻塞
    def scan_runner():
        temp_config_path = None
        try:
            command, temp_config_path = get_javsp_command_with_overrides(target_dir, move_files, target_file, 'scan')
            logging.info(f"[Trigger:queued] Running scan-only: {' '.join(command)}")
            process = subprocess.run(
                command, cwd=_root_dir, capture_output=True, text=True, encoding='utf-8', check=False
            )
            if process.returncode == 0:
                logging.info(f"[Trigger:queued] 入队完成。\nOutput:\n{process.stdout}")
            else:
                logging.error(f"[Trigger:queued] 入队失败。\nOutput:\n{process.stdout}\nError:\n{process.stderr}")
        except Exception as e:
            logging.error(f"[Trigger:queued] 异常: {e}")
        finally:
            _cleanup_temp_config(temp_config_path)

    threading.Thread(target=scan_runner, daemon=True).start()
    return jsonify({
        "message": "任务已加入队列，运行中的 Worker 将自动处理",
        "status": "queued",
        "target_dir": target_dir or "(使用默认目录)",
        "target_file": target_file or "(使用整个目录)",
        "move_files": "(使用默认配置)" if move_files is None else move_files,
    }), 202


@app.route('/api/tasks')
def tasks_api():
    statuses = request.args.get('status')
    status_list = [i.strip() for i in statuses.split(',') if i.strip()] if statuses else None
    limit = max(1, min(int(request.args.get('limit', 200)), 10000))
    offset = max(0, int(request.args.get('offset', 0)))
    updated_after = request.args.get('updated_after', type=float)
    last_run_id = request.args.get('last_run_id', type=int)
    order_by = request.args.get('order_by', 'updated_at')
    sort_dir = request.args.get('sort_dir', 'DESC')
    include_results = request.args.get('include_results') in {'1', 'true', 'yes'}
    store = get_task_store()
    if request.args.get('check_metadata') in {'1', 'true', 'yes'}:
        reload_javsp_config_cache()
        store.check_success_metadata(get_scan_directory())
    tasks = store.list_tasks(
        status_list, limit, offset, order_by, sort_dir,
        updated_after=updated_after, last_run_id=last_run_id,
    )
    total = store.count_tasks(status_list, updated_after=updated_after, last_run_id=last_run_id)
    sessions_by_task = store.active_rescrape_sessions_for_tasks([int(task["id"]) for task in tasks])
    for task in tasks:
        task["rescrape_session"] = sessions_by_task.get(int(task["id"]))
    if include_results:
        results_by_task = store.list_scrape_results_for_tasks([int(task["id"]) for task in tasks])
        crawlers_by_type = {}
        for task in tasks:
            data_src = task.get("data_src") or "normal"
            if data_src not in crawlers_by_type:
                crawlers_by_type[data_src] = get_crawlers_by_type(data_src)
            task["configured_crawlers"] = crawlers_by_type[data_src]
            task["crawler_results"] = results_by_task.get(int(task["id"]), [])
    response = jsonify({"tasks": tasks, "total": total})
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/api/runs/latest')
def latest_run_api():
    response = jsonify({"run": get_task_store().get_latest_run()})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/runs/activity')
def activity_runs_api():
    limit = int(request.args.get('limit', 80))
    response = jsonify({"runs": get_task_store().list_activity_runs(limit)})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/runs/<int:run_id>', methods=['GET'])
def run_detail_api(run_id):
    store = get_task_store()
    run = store.get_run(run_id)
    if not run:
        return jsonify({"message": "运行记录不存在"}), 404
    tasks = store.list_run_tasks(run_id)
    events = store.list_run_events(run_id, limit=int(request.args.get('event_limit', 1000)))
    response = jsonify({"run": run, "tasks": tasks, "events": events})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/runs/<int:run_id>/events', methods=['GET'])
def run_events_api(run_id):
    store = get_task_store()
    if not store.get_run(run_id):
        return jsonify({"message": "运行记录不存在"}), 404
    task_id_arg = request.args.get('task_id')
    task_id = int(task_id_arg) if task_id_arg else None
    limit = int(request.args.get('limit', 1000))
    response = jsonify({"events": store.list_run_events(run_id, task_id=task_id, limit=limit)})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/failures')
def failures_api():
    limit = int(request.args.get('limit', 200))
    return jsonify({"tasks": get_task_store().list_failures(limit)})


@app.route('/api/tasks/<int:task_id>', methods=['GET'])
def task_detail_api(task_id):
    store = get_task_store()
    reload_javsp_config_cache()
    store.check_task_metadata(task_id, get_scan_directory())
    task = store.get_task(task_id)
    if not task:
        return jsonify({"message": "任务不存在"}), 404
    task["configured_crawlers"] = get_crawlers_by_type(task.get("data_src") or "normal")
    task["crawler_results"] = store.list_scrape_results(task_id)
    task["rescrape_session"] = store.rescrape_session_summary(store.active_rescrape_session(task_id))
    return jsonify({"task": task})


@app.route('/api/tasks/<int:task_id>/events', methods=['GET'])
def task_events_api(task_id):
    store = get_task_store()
    if not store.get_task(task_id):
        return jsonify({"message": "任务不存在"}), 404
    limit = int(request.args.get('limit', 500))
    response = jsonify({"events": store.list_task_events(task_id, limit=limit)})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/tasks/<int:task_id>/metadata/sources', methods=['GET'])
def task_metadata_sources_api(task_id):
    store = get_task_store()
    reload_javsp_config_cache()
    store.check_task_metadata(task_id, get_scan_directory())
    sources = store.metadata_sources(task_id)
    if not sources:
        return jsonify({"message": "任务不存在"}), 404
    response = jsonify(sources)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/tasks/<int:task_id>/retry', methods=['POST'])
def retry_task_api(task_id):
    ok = get_task_store().retry_task(task_id)
    if not ok:
        return jsonify({"message": "任务不存在或当前状态不允许重试"}), 404
    # 重试成功后，如有待处理任务且无活跃任务则自动启动 Worker
    store = get_task_store()
    due = store.get_due_tasks(limit=1)
    worker_started = False
    if due and not process_lock.locked():
        worker_started, _ = _run_javsp_background("run-once")
    return jsonify({
        "message": "任务已重新加入队列" + ("，已自动启动刮削" if worker_started else ""),
        "worker_started": worker_started,
    })


@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
def delete_task_api(task_id):
    ok = get_task_store().delete_task(task_id)
    if not ok:
        return jsonify({"message": "任务不存在"}), 404
    return jsonify({"message": "任务记录已删除（不会删除影片或 NFO 文件）"})


@app.route('/api/tasks/batch-delete', methods=['POST'])
def batch_delete_api():
    data = request.json or {}
    task_ids = data.get('task_ids')
    status = (data.get('status') or '').strip()
    older_than_days = int(data.get('older_than_days') or 0)
    store = get_task_store()
    if task_ids is not None:
        count = store.delete_tasks(task_ids if isinstance(task_ids, list) else [])
    elif status:
        count = store.delete_tasks_by_status(status, older_than_days)
    else:
        return jsonify({"message": "请提供 task_ids 或 status 参数"}), 400
    return jsonify({"message": f"已删除 {count} 条任务记录（不会删除影片或 NFO 文件）", "deleted": count})


@app.route('/api/tasks/batch-retry', methods=['POST'])
def batch_retry_api():
    data = request.json or {}
    task_ids = data.get('task_ids')
    if task_ids is None:
        return jsonify({"message": "请提供 task_ids 列表"}), 400
    count = get_task_store().batch_retry(task_ids if isinstance(task_ids, list) else [])
    # 重试成功后，如有待处理任务且无活跃任务则自动启动 Worker
    if count > 0:
        store = get_task_store()
        due = store.get_due_tasks(limit=1)
        if due and not process_lock.locked():
            _run_javsp_background("run-once")
    return jsonify({"message": f"已重试 {count} 个任务", "retried": count})


@app.route('/api/tasks/export', methods=['GET'])
def export_tasks_api():
    import csv, io
    store = get_task_store()
    tasks = store.list_tasks(None, 10000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['ID', '番号', '状态', '数据源', '标题', '女优', '目录', 'NFO', '更新日期', '元数据完整性'])
    for t in tasks:
        info = t.get('info') or {}
        writer.writerow([
            t.get('id'), t.get('avid'), t.get('status'), t.get('data_src'),
            info.get('title', ''), ','.join(info.get('actress') or []),
            t.get('current_save_dir') or t.get('save_dir', ''),
            t.get('current_nfo_path', ''), t.get('updated_at'),
            t.get('metadata_status', '')
        ])
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=javsp_tasks.csv'}
    )


@app.route('/api/metadata/check_complete', methods=['POST'])
def metadata_check_complete_api():
    reload_javsp_config_cache()
    checked_count = len(get_task_store().check_success_metadata(get_scan_directory()))
    return jsonify({"message": f"已重新判断 {checked_count} 个成功任务的完整性", "checked": checked_count})


@app.route('/api/polling_active', methods=['GET'])
def polling_active_api():
    store = get_task_store()
    with store.connect() as conn:
        run = conn.execute("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        refreshes = conn.execute("SELECT COUNT(*) as cnt FROM metadata_refresh_runs WHERE finished_at IS NULL").fetchone()
        running_tasks = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status IN ('running', 'pending')").fetchone()
        pending_count = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'pending'").fetchone()
        running_count = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'running'").fetchone()
        incomplete_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'success' AND metadata_complete = 0"
        ).fetchone()
        not_checked_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'success' AND metadata_complete IS NULL"
        ).fetchone()
        path_unknown_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'success' AND metadata_complete = -1"
        ).fetchone()
    scraping_active = bool(run and run["status"] == "running")
    refresh_running = int(refreshes["cnt"])
    with _background_refresh_lock:
        bg = _background_refresh_task
    refresh_type = bg.get("type") if bg else None
    if bg and not refresh_running:
        refresh_running = 1
    refresh_active = refresh_running > 0
    awaiting_selection_count = store.count_awaiting_rescrape_sessions()
    tasks_pending = int(running_tasks["cnt"])
    active = scraping_active or refresh_active or tasks_pending > 0
    return jsonify({
        "active": active,
        "scraping_active": scraping_active,
        "refresh_active": refresh_active,
        "refresh_type": refresh_type,
        "refresh_running": refresh_running,
        "refresh_starting": bool(bg and not int(refreshes["cnt"])),
        "tasks_pending": int(pending_count["cnt"]),
        "tasks_running": int(running_count["cnt"]),
        "incomplete_count": int(incomplete_count["cnt"]),
        "not_checked_count": int(not_checked_count["cnt"]),
        "path_unknown_count": int(path_unknown_count["cnt"]),
        "awaiting_selection_count": awaiting_selection_count,
    })


@app.route('/api/metadata_complete/config', methods=['GET', 'POST'])
def metadata_complete_config_api():
    if request.method == 'POST':
        data = request.json or {}
        try:
            with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            config = {}
        config['metadata_complete'] = data.get('metadata_complete', data)
        try:
            with open(GENERAL_CONFIG_FILE, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, sort_keys=False)
            reload_javsp_config_cache()
            checked_count = len(get_task_store().check_success_metadata(get_scan_directory()))
            return jsonify({
                "message": f"元数据完整性配置已保存。已重新判断 {checked_count} 个成功任务的完整性。",
                "metadata_rechecked": checked_count,
            })
        except Exception as e:
            return jsonify({"message": f"保存失败: {e}"}), 500

    try:
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        config = {}
    return jsonify({"metadata_complete": config.get('metadata_complete', {})})


def _run_javsp_background(runtime_command, extra_args=None):
    if process_lock.locked():
        return False, "已有任务正在运行"

    is_refresh = runtime_command in ('refresh-incomplete', 'refresh-task', 'rescrape-task', 'rescrape-prepare', 'rescrape-apply', 'normalize-actress')
    if is_refresh:
        global _background_refresh_task
        with _background_refresh_lock:
            _background_refresh_task = {'type': runtime_command, 'started_at': time.time()}

    def runner():
        global _background_refresh_task
        temp_config_path = None
        with process_lock:
            try:
                command, temp_config_path = get_javsp_command(runtime_command, extra_args)
                logging.info(f"[Metadata] Executing command: {' '.join(command)}")
                process = subprocess.run(command, capture_output=True, text=True, cwd=_root_dir)
                if process.returncode == 0:
                    logging.info(f"[Metadata] JAVSP finished successfully.\nOutput:\n{process.stdout}")
                else:
                    logging.error(f"[Metadata] JAVSP failed.\nOutput:\n{process.stdout}\nError:\n{process.stderr}")
                    if runtime_command in ('rescrape-prepare', 'rescrape-apply') and extra_args:
                        get_task_store().fail_rescrape_session(int(extra_args[0]), process.stderr or process.stdout or "后台命令失败")
            except Exception as e:
                logging.exception(f"[Metadata] 后台命令异常: {e}")
                if runtime_command in ('rescrape-prepare', 'rescrape-apply') and extra_args:
                    get_task_store().fail_rescrape_session(int(extra_args[0]), repr(e))
            finally:
                _cleanup_temp_config(temp_config_path)
                if is_refresh:
                    with _background_refresh_lock:
                        _background_refresh_task = None

    threading.Thread(target=runner, daemon=True).start()
    return True, "任务已启动"


@app.route('/api/tasks/<int:task_id>/metadata/check', methods=['POST'])
def metadata_check_task_api(task_id):
    store = get_task_store()
    reload_javsp_config_cache()
    task = store.check_task_metadata(task_id, get_scan_directory())
    if not task:
        return jsonify({"message": "任务不存在"}), 404
    return jsonify({"message": "完整性已重新判断", "task": task})


@app.route('/api/tasks/<int:task_id>/metadata/refresh', methods=['POST'])
def metadata_refresh_task_api(task_id):
    ok, message = _run_javsp_background('refresh-task', [task_id])
    return jsonify({"message": message}), 202 if ok else 409


@app.route('/api/tasks/<int:task_id>/metadata/rescrape', methods=['POST'])
def metadata_rescrape_task_api(task_id):
    ok, message = _run_javsp_background('rescrape-task', [task_id])
    return jsonify({"message": message}), 202 if ok else 409


def _rescrape_session_payload(store, session):
    task = store.get_task(int(session["task_id"]))
    current = (task or {}).get("info") or {}
    auto_info = session.get("auto_info") or {}
    candidates = session.get("candidates") or {}
    fields = []
    for definition in INTERACTIVE_RESCRAPE_FIELDS:
        field = definition["field"]
        sources = []
        for crawler_name, info in candidates.items():
            value = (info or {}).get(field)
            present = value is not None and value != "" and value != []
            sources.append({"crawler_name": crawler_name, "value": value, "present": present})
        fields.append({
            **definition,
            "current_value": current.get(field),
            "auto_value": auto_info.get(field),
            "sources": sources,
        })
    return {
        "session": store.rescrape_session_summary(session),
        "task": {
            "id": int(task["id"]),
            "avid": task.get("avid"),
            "title": current.get("title"),
        } if task else None,
        "fields": fields,
        "media_note": "媒体字段不参与人工选择；确认后才按自动汇总结果下载。",
    }


@app.route('/api/tasks/<int:task_id>/metadata/rescrape/interactive', methods=['POST'])
def metadata_interactive_rescrape_api(task_id):
    store = get_task_store()
    try:
        session, created = store.create_rescrape_session(task_id)
    except ValueError as e:
        return jsonify({"message": str(e)}), 404 if str(e) == "任务不存在" else 409
    if not created:
        status = session.get("status")
        message = "已有待人工选择结果" if status == RESCRAPE_SESSION_AWAITING else "自由选择重新刮削正在处理中"
        return jsonify({"message": message, "session": store.rescrape_session_summary(session)}), 200
    ok, message = _run_javsp_background('rescrape-prepare', [int(session["id"])])
    if not ok:
        store.fail_rescrape_session(int(session["id"]), message)
        return jsonify({"message": message}), 409
    return jsonify({
        "message": "候选数据抓取已启动；此阶段不会下载或修改媒体文件",
        "session": store.rescrape_session_summary(session),
    }), 202


@app.route('/api/rescrape-sessions/<int:session_id>', methods=['GET'])
def rescrape_session_api(session_id):
    store = get_task_store()
    session = store.get_rescrape_session(session_id)
    if not session:
        return jsonify({"message": "自由选择会话不存在"}), 404
    response = jsonify(_rescrape_session_payload(store, session))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/rescrape-sessions/<int:session_id>/apply', methods=['POST'])
def apply_rescrape_session_api(session_id):
    if process_lock.locked():
        return jsonify({"message": "已有任务正在运行，请稍后再确认"}), 409
    data = request.json or {}
    final_values = data.get("values")
    if not isinstance(final_values, dict):
        return jsonify({"message": "缺少最终字段值"}), 400
    store = get_task_store()
    try:
        store.begin_rescrape_apply(session_id, final_values)
    except ValueError as e:
        return jsonify({"message": str(e)}), 409
    ok, message = _run_javsp_background('rescrape-apply', [session_id])
    if not ok:
        store.restore_rescrape_awaiting(session_id, message)
        return jsonify({"message": message}), 409
    return jsonify({"message": "已确认最终字段，开始更新 NFO 和媒体文件"}), 202


@app.route('/api/rescrape-sessions/<int:session_id>/cancel', methods=['POST'])
def cancel_rescrape_session_api(session_id):
    ok = get_task_store().cancel_rescrape_session(session_id)
    if not ok:
        return jsonify({"message": "只有待人工选择的会话可以取消"}), 409
    return jsonify({"message": "已取消，本次未修改元数据、NFO 或媒体文件"})


@app.route('/api/metadata/refresh_incomplete', methods=['POST'])
def metadata_refresh_incomplete_api():
    ok, message = _run_javsp_background('refresh-incomplete')
    return jsonify({"message": message}), 202 if ok else 409


@app.route('/api/metadata/import_legacy', methods=['POST'])
def metadata_import_legacy_api():
    data = request.json or {}
    import_path = str(data.get('path') or '').strip()
    if not import_path:
        return jsonify({"message": "缺少历史目录路径"}), 400
    if not os.path.isabs(import_path):
        import_path = os.path.join(get_scan_directory(), import_path)
    reload_javsp_config_cache()
    store = get_task_store()
    run_id = store.start_run('metadata-import')
    summary = store.import_legacy_metadata_dir(import_path)
    store.finish_import_run(run_id, summary)
    if summary.get("failed") and not summary.get("scanned"):
        return jsonify({"message": summary["failures"][0]["reason"], "summary": summary}), 400
    message = (
        f"历史目录导入完成：扫描 {summary['scanned']}，新增 {summary['created']}，"
        f"更新 {summary['updated']}，完整 {summary['complete']}，"
        f"待优化 {summary['incomplete']}，失败 {summary['failed']}"
    )
    return jsonify({"message": message, "summary": summary})


@app.route('/api/metadata/normalize_actress', methods=['POST'])
def metadata_normalize_actress_api():
    data = request.json or {}
    import_path = str(data.get('path') or '').strip()
    move = bool(data.get('move'))
    if not import_path:
        return jsonify({"message": "缺少历史目录路径"}), 400
    if not os.path.isabs(import_path):
        import_path = os.path.join(get_scan_directory(), import_path)
    args = []
    if move:
        args.append('--move')
    args.append(import_path)
    ok, message = _run_javsp_background('normalize-actress', args)
    return jsonify({"message": message}), 202 if ok else 409


@app.route('/api/crawler_health')
def crawler_health_api():
    return jsonify({"crawlers": get_task_store().crawler_health()})


@app.route('/api/movie_detail')
def get_movie_detail():
    """从 nfo 和封面文件获取影片详情"""
    save_dir = request.args.get('path', '')
    num = request.args.get('num', '')

    if not save_dir:
        return jsonify({"error": "缺少路径参数"}), 400

    # 修复: javsp 运行时会 os.chdir 到扫描目录，相对路径基于扫描目录
    # 而 Flask 的 cwd 不同，需要重新基于扫描目录解析
    if not os.path.isabs(save_dir):
        save_dir = os.path.join(get_scan_directory(), save_dir)

    result = {
        "save_dir": save_dir,
        "num": num,
        "title": "",
        "original_title": "",
        "plot": "",
        "actress": [],
        "director": "",
        "publisher": "",
        "producer": "",
        "genre": [],
        "premiered": "",
        "poster": None,
        "fanart": None,
        "nfo_exists": False
    }

    # 读取 nfo 文件
    nfo_patterns = ['movie.nfo', f'{num}.nfo']
    for pattern in nfo_patterns:
        nfo_path = os.path.join(save_dir, pattern)
        if os.path.exists(nfo_path):
            result["nfo_exists"] = True
            try:
                tree = ET.parse(nfo_path)
                root = tree.getroot()
                result["title"] = root.findtext('title', '')
                result["original_title"] = root.findtext('originaltitle', '')
                result["plot"] = root.findtext('plot', '')
                result["director"] = root.findtext('director', '')
                result["publisher"] = root.findtext('studio', '')
                result["producer"] = root.findtext('studio', '')
                result["premiered"] = root.findtext('premiered', '')

                # 读取类型
                for genre in root.findall('genre'):
                    if genre.text:
                        result["genre"].append(genre.text)
                for tag in root.findall('tag'):
                    if tag.text:
                        result["genre"].append(tag.text)

                # 读取演员
                for actor in root.findall('.//actor'):
                    name = actor.findtext('name', '')
                    if name:
                        result["actress"].append(name)

            except Exception as e:
                logging.debug(f"解析 nfo 文件失败: {e}")
            break

    # 查找封面图片
    image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
    poster_patterns = ['poster', 'cover', 'thumb', f'{num}-poster']
    for pattern in poster_patterns:
        for ext in image_extensions:
            poster_path = os.path.join(save_dir, pattern + ext)
            if os.path.exists(poster_path):
                result["poster"] = f"/api/cover?path={poster_path}"
                break
        if result["poster"]:
            break

    # 查找 fanart 图片
    for ext in image_extensions:
        fanart_path = os.path.join(save_dir, 'fanart' + ext)
        if os.path.exists(fanart_path):
            result["fanart"] = f"/api/cover?path={fanart_path}"
            break

    return jsonify(result)


@app.route('/api/cover')
def serve_cover():
    """提供封面图片服务"""
    path = request.args.get('path', '')
    if not path:
        return "Not found", 404
    if not os.path.isabs(path):
        path = os.path.join(get_scan_directory(), path)
    real_path = os.path.realpath(path)
    scan_root = os.path.realpath(get_scan_directory())
    if os.path.commonpath([real_path, scan_root]) != scan_root:
        return "Not found", 404
    if not os.path.isfile(real_path):
        return "Not found", 404
    ext = os.path.splitext(real_path)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        return "Not found", 404
    return send_from_directory(os.path.dirname(real_path), os.path.basename(real_path))

@app.route('/api/scheduler_config', methods=['GET', 'POST'])
def scheduler_config_api():
    if request.method == 'POST':
        content = request.json.get('content')
        try:
            yaml.safe_load(content)
            with open(SCHEDULER_CONFIG_FILE, 'w', encoding='utf-8') as f:
                f.write(content)
            result = update_scheduler()
            if result.get("success"):
                return jsonify({"message": "定时任务配置已保存并应用！"})
            else:
                return jsonify({"message": f"配置已保存但定时任务未启动：{result.get('message', '未知错误')}"}), 400
        except yaml.YAMLError as e:
            return jsonify({"message": f"错误：无效的YAML格式: {e}"}), 400
    else: # GET
        try:
            with open(SCHEDULER_CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            # 提供一个包含所有可能选项的默认模板，方便用户修改
            content = """# 定时任务配置
# ── 刮削调度器 ──
scheduler:
  # 是否启用定时刮削: true 或 false
  enabled: false
  
  # 模式: 'cron' (每日固定时间) 或 'interval' (按时间间隔)
  mode: 'cron'
  
  # 'cron' 模式配置:
  cron:
    # 24小时制时间
    time: '03:00'
    
  # 'interval' 模式配置:
  interval:
    # 可选单位: days, hours, minutes, seconds
    # 示例: 每隔 6 小时
    hours: 6

# ── 优化调度器 ──
refresh_scheduler:
  # 是否启用定时优化: true 或 false
  enabled: false
  
  # 模式: 'cron' (每日固定时间) 或 'interval' (按时间间隔)
  mode: 'interval'
  
  # 'interval' 模式配置:
  interval:
    hours: 24
    
  # 'cron' 模式配置 (使用此模式请注释掉 interval):
  # cron:
  #   time: '04:00'
"""
        return jsonify({"content": content})

@app.route('/api/general_config', methods=['GET', 'POST'])
def general_config_api():
    if request.method == 'POST':
        content = request.json.get('content')
        try:
            new_config = yaml.safe_load(content) or {}
            try:
                with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    old_config = yaml.safe_load(f) or {}
            except FileNotFoundError:
                old_config = {}
            metadata_changed = old_config.get('metadata_complete') != new_config.get('metadata_complete')
            with open(GENERAL_CONFIG_FILE, 'w', encoding='utf-8') as f:
                f.write(content)
            reload_javsp_config_cache()
            checked_count = None
            recheck_error = None
            if metadata_changed:
                try:
                    checked_count = len(get_task_store().check_success_metadata(get_scan_directory()))
                except Exception as e:
                    logging.warning(f"元数据完整性重新判断失败: {e}", exc_info=True)
                    recheck_error = str(e)
            message = "主程序配置已保存！"
            if recheck_error:
                message += f" 但完整性重新判断失败: {recheck_error}"
            elif checked_count is not None:
                message += f" 已重新判断 {checked_count} 个成功任务的完整性。"
            return jsonify({"message": message, "metadata_rechecked": checked_count})
        except yaml.YAMLError as e:
            return jsonify({"message": f"错误：无效的YAML格式: {e}"}), 400
    else: # GET
        try:
            with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            content = "# JAVSP 主配置文件\n# javsp_args: []"
        return jsonify({"content": content})

# --- 爬虫管理 API ---
CRAWLER_WEB_DIR = os.path.join(_root_dir, 'javsp', 'web')

def _replace_crawler_selection(text, new_selection):
    """只替换 config.yml 中 crawler.selection 的文本块，保留注释和其他格式"""
    lines = text.split('\n')
    result = []
    replaced = False
    i = 0
    valid_keys = {'normal', 'fc2', 'cid'}

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if not stripped.startswith('crawler:'):
            result.append(line)
            i += 1
            continue

        # 进入 crawler 块
        crawler_indent = len(line) - len(stripped)
        result.append(line)
        i += 1

        while i < len(lines):
            line2 = lines[i]
            stripped2 = line2.lstrip()

            if not stripped2:
                result.append(line2)
                i += 1
                continue

            indent2 = len(line2) - len(stripped2)
            # 退出 crawler 块
            if indent2 <= crawler_indent and not stripped2.startswith('#'):
                break

            # 找到 selection: 键
            if stripped2.startswith('selection:'):
                sel_indent = indent2
                result.append(line2)
                i += 1

                # 跳过旧的选择列表内容（根据缩进判断）
                while i < len(lines):
                    line3 = lines[i]
                    stripped3 = line3.lstrip()
                    if not stripped3:
                        i += 1
                        continue
                    indent3 = len(line3) - len(stripped3)
                    if indent3 <= sel_indent and not stripped3.startswith('#'):
                        break
                    i += 1

                # 插入新的 selection 内容
                child_indent = ' ' * (sel_indent + 2)
                for key in ['normal', 'fc2', 'cid']:
                    vals = new_selection.get(key, [])
                    if vals:
                        result.append(child_indent + f'{key}: [{", ".join(vals)}]')
                    else:
                        result.append(child_indent + f'{key}: []')
                replaced = True
                continue

            result.append(line2)
            i += 1

    if not replaced:
        # 如果没找到 selection 块，fallback 到 yaml.dump（文件结构异常时）
        config = yaml.safe_load(text) or {}
        if 'crawler' not in config:
            config['crawler'] = {}
        config['crawler']['selection'] = {k: v for k, v in new_selection.items() if k in valid_keys}
        return yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False)

    return '\n'.join(result)


def _replace_crawler_field_priorities(text, new_priorities):
    """只替换 config.yml 中 crawler.field_priorities 的文本块。"""
    lines = text.split('\n')
    result = []
    replaced = False
    inserted = False
    i = 0
    valid_fields = {'title', 'plot', 'actress', 'preview_pics'}

    def append_block(indent):
        child_indent = ' ' * (indent + 2)
        result.append(' ' * indent + 'field_priorities:')
        for key in ['title', 'plot', 'actress', 'preview_pics']:
            vals = [str(v) for v in new_priorities.get(key, [])]
            result.append(child_indent + f'{key}: [{", ".join(vals)}]' if vals else child_indent + f'{key}: []')

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if not stripped.startswith('crawler:'):
            result.append(line)
            i += 1
            continue

        crawler_indent = len(line) - len(stripped)
        result.append(line)
        i += 1
        block_insert_indent = crawler_indent + 2

        while i < len(lines):
            line2 = lines[i]
            stripped2 = line2.lstrip()

            if not stripped2:
                result.append(line2)
                i += 1
                continue

            indent2 = len(line2) - len(stripped2)
            if indent2 <= crawler_indent and not stripped2.startswith('#'):
                if not replaced and not inserted:
                    append_block(block_insert_indent)
                    inserted = True
                break

            if stripped2.startswith('field_priorities:'):
                priorities_indent = indent2
                append_block(priorities_indent)
                replaced = True
                i += 1

                while i < len(lines):
                    line3 = lines[i]
                    stripped3 = line3.lstrip()
                    if not stripped3:
                        i += 1
                        continue
                    indent3 = len(line3) - len(stripped3)
                    if indent3 <= priorities_indent and not stripped3.startswith('#'):
                        break
                    i += 1
                continue

            if not replaced and not inserted and stripped2.startswith('required_keys:'):
                append_block(indent2)
                inserted = True

            result.append(line2)
            i += 1

    if not replaced and not inserted:
        config = yaml.safe_load(text) or {}
        if 'crawler' not in config:
            config['crawler'] = {}
        config['crawler']['field_priorities'] = {k: v for k, v in new_priorities.items() if k in valid_fields}
        return yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False)

    return '\n'.join(result)


@app.route('/api/crawlers', methods=['GET', 'POST'])
def crawlers_api():
    if request.method == 'POST':
        selection = request.json.get('selection', {})
        field_priorities = request.json.get('field_priorities', {})
        valid_keys = {'normal', 'fc2', 'cid'}
        # 兼容旧版客户端：已移除的类型静默忽略，并在保存时从配置中清除。
        removed_keys = {'getchu', 'gyutto'}
        valid_fields = {'title', 'plot', 'actress', 'preview_pics'}
        # 校验键名
        for key in selection:
            if key not in valid_keys and key not in removed_keys:
                return jsonify({"message": f"错误：无效的爬虫类型 '{key}'"}), 400
        selection = {key: value for key, value in selection.items() if key in valid_keys}
        for key in field_priorities:
            if key not in valid_fields:
                return jsonify({"message": f"错误：无效的字段优先级 '{key}'"}), 400
        # 读取原始文本并只替换 selection 块
        try:
            with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                original_text = f.read()
        except FileNotFoundError:
            original_text = ''
        new_text = _replace_crawler_selection(original_text, selection)
        new_text = _replace_crawler_field_priorities(new_text, field_priorities)
        try:
            with open(GENERAL_CONFIG_FILE, 'w', encoding='utf-8') as f:
                f.write(new_text)
            return jsonify({"message": "爬虫配置已保存！"})
        except Exception as e:
            return jsonify({"message": f"错误：保存配置失败: {e}"}), 500
    else:  # GET
        # 扫描可用爬虫
        available = []
        exclude_files = {'__init__.py', 'base.py', 'exceptions.py', 'translate.py'}
        try:
            for fname in sorted(os.listdir(CRAWLER_WEB_DIR)):
                if not fname.endswith('.py') or fname in exclude_files:
                    continue
                mod_name = fname[:-3]
                fpath = os.path.join(CRAWLER_WEB_DIR, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    if 'def parse_data' in content:
                        available.append(mod_name)
                except Exception:
                    pass
        except FileNotFoundError:
            pass
        # 读取当前配置
        selection = {"normal": [], "fc2": [], "cid": []}
        field_priorities = {"title": [], "plot": [], "actress": [], "preview_pics": []}
        try:
            with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            crawler_cfg = config.get('crawler', {})
            sel = crawler_cfg.get('selection', {})
            for key in selection:
                if key in sel and isinstance(sel[key], list):
                    selection[key] = sel[key]
            priorities = crawler_cfg.get('field_priorities', {})
            for key in field_priorities:
                if key in priorities and isinstance(priorities[key], list):
                    field_priorities[key] = priorities[key]
        except FileNotFoundError:
            pass
        return jsonify({"available": available, "selection": selection, "field_priorities": field_priorities})


@app.route('/api/crawlers_by_type', methods=['GET'])
def crawlers_by_type_api():
    """返回指定类型的爬虫列表"""
    data_src = request.args.get('type', 'normal')
    selection = {"normal": [], "fc2": [], "cid": []}
    try:
        with open(GENERAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        crawler_cfg = config.get('crawler', {})
        sel = crawler_cfg.get('selection', {})
        for key in selection:
            if key in sel and isinstance(sel[key], list):
                selection[key] = sel[key]
    except FileNotFoundError:
        pass
    crawlers = selection.get(data_src, [])
    return jsonify({"crawlers": crawlers})


# --- 爬虫调试 API ---
@app.route('/api/crawlers/debug', methods=['POST'])
def crawler_debug_api():
    data = request.json or {}
    crawler_name = (data.get('crawler_name') or '').strip()
    dvdid = (data.get('dvdid') or '').strip()
    cid = (data.get('cid') or '').strip()

    if not crawler_name:
        return jsonify({"error": "请指定爬虫名称"}), 400
    if not dvdid and not cid:
        return jsonify({"error": "请指定 dvdid 或 cid"}), 400

    crawler_file = os.path.join(CRAWLER_WEB_DIR, f'{crawler_name}.py')
    if not os.path.exists(crawler_file):
        return jsonify({"error": f"爬虫 '{crawler_name}' 不存在"}), 404

    # 验证爬虫包含 parse_data 函数
    try:
        with open(crawler_file, 'r', encoding='utf-8') as f:
            content = f.read()
        if 'def parse_data' not in content:
            return jsonify({"error": f"爬虫 '{crawler_name}' 不包含 parse_data 函数"}), 400
    except Exception:
        return jsonify({"error": f"无法读取爬虫文件 '{crawler_name}'"}), 500

    # 确保配置已刷新
    reload_javsp_config_cache()

    started_at = time.time()
    try:
        # 动态导入爬虫模块
        import_name = f'javsp.web.{crawler_name}'
        try:
            __import__(import_name)
        except ModuleNotFoundError as e:
            return jsonify({"success": False, "error": f"导入爬虫模块失败: {e}", "type": "import_error", "elapsed": round(time.time() - started_at, 3)})

        mod = sys.modules[import_name]

        # 创建 MovieInfo 实例
        if cid:
            movie = MovieInfo(cid=cid)
        else:
            movie = MovieInfo(dvdid)

        # 执行抓取
        mod.parse_data(movie)
        elapsed = round(time.time() - started_at, 3)

        # 收集所有非空字段
        movie_fields = ['dvdid', 'cid', 'url', 'plot', 'cover', 'big_cover', 'genre', 'genre_id',
                        'genre_norm', 'score', 'title', 'ori_title', 'magnet', 'serial',
                        'actress', 'actress_pics', 'director', 'duration', 'producer',
                        'publisher', 'uncensored', 'publish_date', 'preview_pics', 'preview_video']
        fields = {}
        for f in movie_fields:
            val = getattr(movie, f, None)
            if val is not None:
                fields[f] = val

        return jsonify({
            "success": True,
            "elapsed": elapsed,
            "dvdid": movie.dvdid,
            "cid": movie.cid,
            "url": movie.url,
            "fields": fields,
            "field_count": len(fields)
        })
    except MovieNotFoundError as e:
        elapsed = round(time.time() - started_at, 3)
        return jsonify({"success": False, "error": str(e), "type": "not_found", "elapsed": elapsed})
    except CrawlerError as e:
        elapsed = round(time.time() - started_at, 3)
        return jsonify({"success": False, "error": str(e), "type": f"crawler_error", "elapsed": elapsed})
    except Exception as e:
        elapsed = round(time.time() - started_at, 3)
        tb = traceback.format_exc()
        logging.error(f"爬虫调试异常: {tb}")
        return jsonify({"success": False, "error": str(e), "type": "unknown_error", "elapsed": elapsed, "traceback": tb[:8000]})


# --- 6. 主程序入口 ---
if __name__ == '__main__':
    update_scheduler()
    scheduler.start()
    logging.info(f"Starting Waitress web server on http://0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000)
