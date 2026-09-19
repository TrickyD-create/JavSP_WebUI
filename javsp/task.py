"""后台任务队列、状态库和增量扫描支持。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import requests

from javsp.config import Cfg
from javsp.datatype import Movie
from javsp.metadata import check_metadata_complete, info_from_dict, info_to_dict, load_info_from_nfo


logger = logging.getLogger(__name__)

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_SKIPPED = "skipped"
TASK_DEFERRED = "deferred"
TASK_TYPE_LEGACY_METADATA = "metadata_legacy"
METADATA_PATH_UNKNOWN = -1

RUNNING_STATES = {TASK_PENDING, TASK_RUNNING, TASK_DEFERRED}
TEMP_EXTENSIONS = (".part", ".aria2", ".!qb", ".download", ".tmp")
EVENT_LEVELS = {"info", "warning", "error"}
SOURCE_COMPARE_FIELDS = [
    "title",
    "ori_title",
    "plot",
    "actress",
    "genre",
    "publish_date",
    "publisher",
    "producer",
    "director",
    "duration",
    "score",
    "cover",
    "big_cover",
    "preview_pics",
]
SOURCE_FIELD_LABELS = {
    "title": "标题",
    "ori_title": "原始标题",
    "plot": "简介",
    "actress": "女演员",
    "genre": "类型",
    "publish_date": "发行日期",
    "publisher": "发行商",
    "producer": "制作商",
    "director": "导演",
    "duration": "时长",
    "score": "评分",
    "cover": "封面",
    "big_cover": "高清封面",
    "preview_pics": "预览图",
}

RESCRAPE_SESSION_SCRAPING = "scraping"
RESCRAPE_SESSION_AWAITING = "awaiting_selection"
RESCRAPE_SESSION_APPLYING = "applying"
RESCRAPE_SESSION_COMPLETED = "completed"
RESCRAPE_SESSION_CANCELLED = "cancelled"
RESCRAPE_SESSION_FAILED = "failed"
ACTIVE_RESCRAPE_SESSION_STATES = {
    RESCRAPE_SESSION_SCRAPING,
    RESCRAPE_SESSION_AWAITING,
    RESCRAPE_SESSION_APPLYING,
}

# 媒体字段刻意不出现在这里：候选阶段只抓取 URL，确认后才按自动汇总结果下载。
INTERACTIVE_RESCRAPE_FIELDS = [
    {"field": "title", "label": "标题", "editor": "text"},
    {"field": "ori_title", "label": "原始标题", "editor": "text"},
    {"field": "plot", "label": "简介", "editor": "textarea"},
    {"field": "actress", "label": "女演员", "editor": "tags"},
    {"field": "genre", "label": "类型", "editor": "tags"},
    {"field": "serial", "label": "系列", "editor": "text"},
    {"field": "director", "label": "导演", "editor": "text"},
    {"field": "producer", "label": "制作商", "editor": "text"},
    {"field": "publisher", "label": "发行商", "editor": "text"},
    {"field": "publish_date", "label": "发行日期", "editor": "date"},
    {"field": "duration", "label": "时长（分钟）", "editor": "number"},
    {"field": "score", "label": "评分", "editor": "number"},
]
INTERACTIVE_RESCRAPE_FIELD_NAMES = {item["field"] for item in INTERACTIVE_RESCRAPE_FIELDS}


def now_ts() -> float:
    return time.time()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).lower()


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    normalized = []
    for item in items:
        text = _normalize_scalar(item)
        if text:
            normalized.append(text)
    return normalized


def _field_match_score(final_value: Any, source_value: Any) -> float:
    if not _has_value(final_value) or not _has_value(source_value):
        return 0.0
    if isinstance(final_value, (list, tuple, set)) or isinstance(source_value, (list, tuple, set)):
        final_items = _normalize_list(final_value)
        source_items = _normalize_list(source_value)
        if not final_items or not source_items:
            return 0.0
        if final_items == source_items:
            return 1.0
        final_set = set(final_items)
        source_set = set(source_items)
        if final_set and final_set.issubset(source_set):
            return 0.95
        overlap = len(final_set & source_set)
        return overlap / max(len(final_set), 1)
    return 1.0 if _normalize_scalar(final_value) == _normalize_scalar(source_value) else 0.0


def build_metadata_source_comparison(
    final_info: dict[str, Any] | None,
    scrape_results: list[dict[str, Any]],
) -> dict[str, Any]:
    final_info = final_info or {}
    has_snapshots = any(isinstance(item.get("info"), dict) and item.get("info") for item in scrape_results)
    fields = []
    for field in SOURCE_COMPARE_FIELDS:
        final_value = final_info.get(field)
        source_items = []
        best_name = None
        best_score = 0.0
        non_empty_values: list[Any] = []
        for result in scrape_results:
            info = result.get("info") if isinstance(result.get("info"), dict) else None
            value = info.get(field) if info else None
            score = _field_match_score(final_value, value)
            if score > best_score:
                best_score = score
                best_name = result.get("crawler_name")
            if _has_value(value):
                non_empty_values.append(value)
            source_items.append(
                {
                    "crawler_name": result.get("crawler_name"),
                    "success": bool(result.get("success")),
                    "value": value,
                    "present": _has_value(value),
                    "matched": score >= 0.95,
                    "elapsed": result.get("elapsed"),
                    "error": result.get("error"),
                    "url": result.get("url"),
                    "title": result.get("title"),
                    "fields": result.get("fields") or [],
                }
            )
        distinct = {_normalize_scalar(value) for value in non_empty_values if not isinstance(value, (list, tuple, set))}
        if any(isinstance(value, (list, tuple, set)) for value in non_empty_values):
            distinct = {json_dumps(_normalize_list(value)) for value in non_empty_values}
        conflict = len({item for item in distinct if item}) > 1
        adopted_source = best_name if best_score >= 0.95 else None
        if not _has_value(final_value):
            status = "missing"
        elif adopted_source:
            status = "matched"
        elif conflict:
            status = "conflict"
        else:
            status = "unknown"
        fields.append(
            {
                "field": field,
                "label": SOURCE_FIELD_LABELS.get(field, field),
                "final_value": final_value,
                "adopted_source": adopted_source or "汇总生成/未知",
                "status": status,
                "conflict": conflict,
                "sources": source_items,
            }
        )
    return {
        "final_info": final_info,
        "fields": fields,
        "crawler_results": scrape_results,
        "has_snapshots": has_snapshots,
        "note": "" if has_snapshots else "旧记录无完整来源快照，仅能展示爬虫摘要字段。",
    }


def movie_id(movie: Movie) -> str:
    return movie.dvdid or movie.cid or "UNKNOWN"


def file_snapshot(files: Iterable[str]) -> list[dict[str, Any]]:
    snapshot = []
    for file in sorted(os.path.abspath(i) for i in files):
        try:
            stat = os.stat(file)
        except FileNotFoundError:
            continue
        snapshot.append({
            "path": file,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return snapshot


def fingerprint_files(files: Iterable[str]) -> str:
    raw = json_dumps(file_snapshot(files))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fingerprint_legacy_nfo(nfo_path: str) -> str:
    raw = "legacy-nfo:" + os.path.abspath(nfo_path)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def has_temp_download_marker(files: Iterable[str], stale_seconds: int = 0) -> bool:
    now = time.time()
    for file in files:
        lower = file.lower()
        if lower.endswith(TEMP_EXTENSIONS):
            if stale_seconds > 0 and now - os.path.getmtime(file) >= stale_seconds:
                continue
            return True
        folder = os.path.dirname(file)
        basename = os.path.basename(file)
        stem = os.path.splitext(basename)[0]
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            name_lower = name.lower()
            if name_lower.startswith(stem.lower()) and name_lower.endswith(TEMP_EXTENSIONS):
                temp_path = os.path.join(folder, name)
                try:
                    if stale_seconds > 0 and now - os.path.getmtime(temp_path) >= stale_seconds:
                        continue
                except OSError:
                    pass
                return True
    return False


def snapshot_by_path(snapshot: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in snapshot}


def _scan_extrafanart(save_dir: str | None) -> list[str] | None:
    """扫描 extrafanart 目录中已有的剧照文件，返回绝对路径列表"""
    if not save_dir:
        return None
    fanart_dir = os.path.join(save_dir, 'extrafanart')
    if not os.path.isdir(fanart_dir):
        return None
    pics = []
    try:
        for fname in sorted(os.listdir(fanart_dir)):
            if not fname.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                continue
            fpath = os.path.join(fanart_dir, fname)
            if os.path.isfile(fpath):
                pics.append(fpath)
    except OSError:
        return None
    return pics if pics else None


class TaskStore:
    """SQLite 封装：所有后台状态都通过这里读写。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @classmethod
    def from_config(cls, base_dir: str | Path | None = None) -> "TaskStore":
        db_path = Path(Cfg().daemon.state_db)
        if not db_path.is_absolute():
            base = Path(base_dir) if base_dir else Path.cwd()
            db_path = base / db_path
        return cls(db_path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    scan_total INTEGER DEFAULT 0,
                    enqueued INTEGER DEFAULT 0,
                    deferred INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    succeeded INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    message TEXT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    avid TEXT NOT NULL,
                    data_src TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    files_snapshot_json TEXT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    failure_stage TEXT,
                    failure_reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    next_retry_at REAL,
                    save_dir TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_run_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS file_index (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    fingerprint TEXT NOT NULL,
                    task_id INTEGER,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crawler_stats (
                    name TEXT PRIMARY KEY,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    consecutive_failures INTEGER DEFAULT 0,
                    total_elapsed REAL DEFAULT 0,
                    circuit_break_until REAL,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scrape_results (
                    task_id INTEGER NOT NULL,
                    crawler_name TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    fields_json TEXT,
                    url TEXT,
                    title TEXT,
                    info_json TEXT,
                    elapsed REAL DEFAULT 0,
                    error TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (task_id, crawler_name)
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    task_id INTEGER,
                    level TEXT NOT NULL DEFAULT 'info',
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS metadata_refresh_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    trigger_type TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    success INTEGER,
                    updated_fields_json TEXT,
                    reasons_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS rescrape_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    candidates_json TEXT,
                    auto_info_json TEXT,
                    final_values_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL
                );
                """
            )
            for column, definition in {
                "files_snapshot_json": "TEXT",
                "current_files_json": "TEXT",
                "current_save_dir": "TEXT",
                "current_nfo_path": "TEXT",
                "current_fanart_path": "TEXT",
                "current_poster_path": "TEXT",
                "info_json": "TEXT",
                "metadata_checked_at": "REAL",
                "metadata_complete": "INTEGER",
                "metadata_incomplete_reasons_json": "TEXT",
                "last_metadata_refresh_at": "REAL",
            }.items():
                self._ensure_column(conn, "tasks", column, definition)
            for column, definition in {
                "info_json": "TEXT",
                "elapsed": "REAL DEFAULT 0",
                "error": "TEXT",
            }.items():
                self._ensure_column(conn, "scrape_results", column, definition)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_run_id ON task_events(run_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_updated_at ON tasks(status, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_last_run_updated_at ON tasks(last_run_id, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rescrape_sessions_task_id ON rescrape_sessions(task_id, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rescrape_sessions_status ON rescrape_sessions(status, updated_at DESC)")
            try:
                conn.execute(
                    """
                    DELETE FROM crawler_stats
                    WHERE name IS NULL
                       OR name = ''
                       OR typeof(success_count) NOT IN ('integer', 'real')
                       OR typeof(failure_count) NOT IN ('integer', 'real')
                       OR typeof(consecutive_failures) NOT IN ('integer', 'real')
                    """
                )
            except sqlite3.DatabaseError:
                logger.warning("爬虫统计表损坏，已重建 crawler_stats")
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS crawler_stats;
                    CREATE TABLE crawler_stats (
                        name TEXT PRIMARY KEY,
                        success_count INTEGER DEFAULT 0,
                        failure_count INTEGER DEFAULT 0,
                        consecutive_failures INTEGER DEFAULT 0,
                        total_elapsed REAL DEFAULT 0,
                        circuit_break_until REAL,
                        last_error TEXT,
                        updated_at REAL NOT NULL
                    );
                    """
                )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def _record_event_conn(
        self,
        conn: sqlite3.Connection,
        run_id: int | None = None,
        task_id: int | None = None,
        level: str = "info",
        event_type: str = "event",
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        level = level if level in EVENT_LEVELS else "info"
        cur = conn.execute(
            """
            INSERT INTO task_events(run_id, task_id, level, event_type, message, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, level, event_type, message, json_dumps(payload or {}), now_ts()),
        )
        return int(cur.lastrowid)

    def record_event(
        self,
        run_id: int | None = None,
        task_id: int | None = None,
        level: str = "info",
        event_type: str = "event",
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            return self._record_event_conn(conn, run_id, task_id, level, event_type, message, payload)

    def import_legacy_metadata_dir(self, root: str) -> dict[str, Any]:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return {
                "root": root,
                "scanned": 0,
                "created": 0,
                "updated": 0,
                "complete": 0,
                "incomplete": 0,
                "failed": 1,
                "failures": [{"path": root, "reason": "路径不存在或不是目录"}],
            }

        nfo_paths = []
        for dirpath, _, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                if filename.lower().endswith(".nfo"):
                    nfo_paths.append(os.path.abspath(os.path.join(dirpath, filename)))

        summary: dict[str, Any] = {
            "root": root,
            "scanned": len(nfo_paths),
            "created": 0,
            "updated": 0,
            "complete": 0,
            "incomplete": 0,
            "failed": 0,
            "failures": [],
            "task_ids": [],
        }
        for nfo_path in sorted(nfo_paths):
            info = load_info_from_nfo(nfo_path)
            if not info:
                summary["failed"] += 1
                summary["failures"].append({"path": nfo_path, "reason": "无法解析 NFO 或识别番号"})
                continue
            task_id, created, complete = self.upsert_legacy_metadata_nfo(nfo_path, info)
            summary["task_ids"].append(task_id)
            if created:
                summary["created"] += 1
            else:
                summary["updated"] += 1
            if complete:
                summary["complete"] += 1
            else:
                summary["incomplete"] += 1
        return summary

    def upsert_legacy_metadata_nfo(self, nfo_path: str, info: Any) -> tuple[int, bool, bool]:
        nfo_path = os.path.abspath(nfo_path)
        save_dir = os.path.dirname(nfo_path)
        avid = info.dvdid or info.cid
        data_src = "cid" if info.cid and not info.dvdid else "normal"
        if info.dvdid:
            from javsp.avid import guess_av_type
            data_src = guess_av_type(info.dvdid)
        fingerprint = fingerprint_legacy_nfo(nfo_path)
        ts = now_ts()
        complete, reasons = check_metadata_complete(info)
        info_json = json_dumps(info_to_dict(info))
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM tasks WHERE fingerprint = ? OR current_nfo_path = ? ORDER BY id LIMIT 1",
                (fingerprint, nfo_path),
            ).fetchone()
            if existing:
                task_id = int(existing["id"])
                is_legacy_task = existing["task_type"] == TASK_TYPE_LEGACY_METADATA
                # 对已存在的任务，用 NFO 数据更新而不破坏历史数据
                old_info = info_from_dict(
                    json_loads(existing["info_json"]),
                    existing["avid"],
                    existing["data_src"] or "normal",
                )
                if old_info:
                    NFO_PROVIDED_FIELDS = [
                        "title", "ori_title", "score", "plot", "duration",
                        "publish_date", "director", "producer", "publisher",
                        "preview_video", "genre", "actress", "actress_pics",
                    ]
                    for field in NFO_PROVIDED_FIELDS:
                        nfo_val = getattr(info, field, None)
                        if nfo_val:
                            setattr(old_info, field, nfo_val)
                    info = old_info
                # 从磁盘扫描 preview_pics 作为权威来源（NFO 不存储此字段）
                fs_pics = _scan_extrafanart(save_dir)
                if fs_pics is not None:
                    info.preview_pics = fs_pics
                elif isinstance(info.preview_pics, list) and info.preview_pics:
                    # NFO 不存储剧照。磁盘目录不存在或为空时，仅保留仍有意义的远程 URL；
                    # 只要列表中包含本地路径，就说明原磁盘记录已经失效，应清空。
                    all_remote = all(
                        isinstance(pic, str) and pic.lower().startswith(("http://", "https://"))
                        for pic in info.preview_pics
                    )
                    if not all_remote:
                        info.preview_pics = None
                # 用合并后的数据重新计算完整性
                complete, reasons = check_metadata_complete(info)
                info_json = json_dumps(info_to_dict(info))
                conn.execute(
                    """
                    UPDATE tasks
                    SET task_type = ?, avid = ?, data_src = ?, files_json = ?, files_snapshot_json = ?,
                        fingerprint = ?, save_dir = ?, current_files_json = ?,
                        current_save_dir = ?, current_nfo_path = ?, info_json = ?,
                        metadata_checked_at = ?, metadata_complete = ?,
                        metadata_incomplete_reasons_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        existing["task_type"],
                        avid if is_legacy_task else existing["avid"],
                        data_src if is_legacy_task else existing["data_src"],
                        existing["files_json"] or "[]",
                        existing["files_snapshot_json"] or "[]",
                        fingerprint if is_legacy_task else existing["fingerprint"],
                        save_dir,
                        existing["current_files_json"] or "[]",
                        save_dir,
                        nfo_path,
                        info_json,
                        ts,
                        1 if complete else 0,
                        json_dumps(reasons),
                        ts,
                        task_id,
                    ),
                )
                return task_id, False, complete

            cur = conn.execute(
                """
                INSERT INTO tasks(
                    task_type, avid, data_src, files_json, files_snapshot_json, fingerprint,
                    status, retry_count, save_dir, created_at, updated_at, current_files_json,
                    current_save_dir, current_nfo_path, info_json, metadata_checked_at,
                    metadata_complete, metadata_incomplete_reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    TASK_TYPE_LEGACY_METADATA,
                    avid,
                    data_src,
                    "[]",
                    "[]",
                    fingerprint,
                    TASK_SUCCESS,
                    save_dir,
                    ts,
                    ts,
                    "[]",
                    save_dir,
                    nfo_path,
                    info_json,
                    ts,
                    1 if complete else 0,
                    json_dumps(reasons),
                ),
            )
            return int(cur.lastrowid), True, complete

    def start_run(self, trigger: str) -> int:
        ts = now_ts()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(trigger, status, started_at) VALUES (?, ?, ?)",
                (trigger, TASK_RUNNING, ts),
            )
            run_id = int(cur.lastrowid)
            self._record_event_conn(conn, run_id, None, "info", "run_started", f"运行开始: {trigger}", {"trigger": trigger})
            return run_id

    def finish_run(self, run_id: int, status: str = TASK_SUCCESS, message: str | None = None) -> None:
        with self.connect() as conn:
            counts = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS failed
                FROM tasks WHERE last_run_id = ?
                """,
                (TASK_SUCCESS, TASK_FAILED, run_id),
            ).fetchone()
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, succeeded = ?, failed = ?, message = ?
                WHERE id = ?
                """,
                (
                    status,
                    now_ts(),
                    int(counts["succeeded"] or 0),
                    int(counts["failed"] or 0),
                    message,
                    run_id,
                ),
            )
            level = "error" if status == TASK_FAILED else "info"
            self._record_event_conn(
                conn,
                run_id,
                None,
                level,
                "run_finished",
                f"运行结束: {status}",
                {
                    "status": status,
                    "message": message,
                    "succeeded": int(counts["succeeded"] or 0),
                    "failed": int(counts["failed"] or 0),
                },
            )

    def update_run_scan_counts(self, run_id: int, scan_total: int, enqueued: int, deferred: int, skipped: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET scan_total = ?, enqueued = ?, deferred = ?, skipped = ?
                WHERE id = ?
                """,
                (scan_total, enqueued, deferred, skipped, run_id),
            )

    def get_latest_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_run_tasks(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE last_run_id = ? ORDER BY updated_at DESC", (run_id,)).fetchall()
        return [self._decode_task(row) for row in rows]

    def _decode_event(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json_loads(data.pop("payload_json", None), {})
        return data

    def list_task_events(self, task_id: int, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM task_events
                    WHERE task_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                ) ORDER BY created_at ASC
                """,
                (task_id, limit),
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def list_run_events(self, run_id: int, task_id: int | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        params: list[Any] = [run_id]
        where = "run_id = ?"
        if task_id is not None:
            where += " AND task_id = ?"
            params.append(task_id)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM task_events
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                ) ORDER BY created_at ASC
                """,
                params,
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def finish_import_run(self, run_id: int, summary: dict[str, Any]) -> None:
        status = TASK_FAILED if summary.get("failed") and not summary.get("scanned") else TASK_SUCCESS
        message = (
            f"历史目录导入：扫描 {summary.get('scanned', 0)}，"
            f"新增 {summary.get('created', 0)}，更新 {summary.get('updated', 0)}，"
            f"完整 {summary.get('complete', 0)}，待优化 {summary.get('incomplete', 0)}，"
            f"失败 {summary.get('failed', 0)}"
        )
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, scan_total = ?, enqueued = ?,
                    skipped = ?, succeeded = ?, failed = ?, message = ?
                WHERE id = ?
                """,
                (
                    status,
                    now_ts(),
                    int(summary.get("scanned") or 0),
                    int(summary.get("created") or 0),
                    int(summary.get("updated") or 0),
                    int(summary.get("complete") or 0),
                    int(summary.get("failed") or 0),
                    message,
                    run_id,
                ),
            )
            self._record_event_conn(conn, run_id, None, "error" if status == TASK_FAILED else "info", "run_finished", message, summary)

    def finish_normalize_run(
        self,
        run_id: int,
        summary: dict[str, Any],
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        status = status or (TASK_FAILED if summary.get("failed") and not summary.get("changed") else TASK_SUCCESS)
        message = (
            f"女优名归一化：扫描 {summary.get('scanned', 0)}，"
            f"变更 {summary.get('changed', 0)}，移动 {summary.get('moved', 0)}，"
            f"跳过 {summary.get('skipped', 0)}，失败 {summary.get('failed', 0)}"
        )
        if error:
            message = f"{message}，错误: {error}"
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, scan_total = ?, enqueued = ?,
                    skipped = ?, succeeded = ?, failed = ?, message = ?
                WHERE id = ?
                """,
                (
                    status,
                    now_ts(),
                    int(summary.get("scanned") or 0),
                    int(summary.get("imported") or 0),
                    int(summary.get("skipped") or 0),
                    int(summary.get("changed") or 0),
                    int(summary.get("failed") or 0),
                    message,
                    run_id,
                ),
            )
            self._record_event_conn(
                conn,
                run_id,
                None,
                "error" if status == TASK_FAILED else "info",
                "run_finished",
                message,
                summary,
            )

    def update_normalized_metadata(
        self,
        task_id: int,
        info: Any,
        run_id: int | None = None,
        current_files: list[str] | None = None,
        current_save_dir: str | None = None,
        current_nfo_path: str | None = None,
        current_fanart_path: str | None = None,
        current_poster_path: str | None = None,
    ) -> None:
        ts = now_ts()
        complete, reasons = check_metadata_complete(info)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET info_json = ?, metadata_checked_at = ?, metadata_complete = ?,
                    metadata_incomplete_reasons_json = ?, current_files_json = COALESCE(?, current_files_json),
                    current_save_dir = COALESCE(?, current_save_dir),
                    current_nfo_path = COALESCE(?, current_nfo_path),
                    current_fanart_path = COALESCE(?, current_fanart_path),
                    current_poster_path = COALESCE(?, current_poster_path),
                    last_run_id = COALESCE(?, last_run_id), updated_at = ?
                WHERE id = ?
                """,
                (
                    json_dumps(info_to_dict(info)),
                    ts,
                    1 if complete else 0,
                    json_dumps(reasons),
                    json_dumps(current_files) if current_files is not None else None,
                    current_save_dir,
                    current_nfo_path,
                    current_fanart_path,
                    current_poster_path,
                    run_id,
                    ts,
                    task_id,
                ),
            )

    def list_activity_runs(self, limit: int = 80) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        with self.connect() as conn:
            for row in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall():
                data = dict(row)
                if data.get("trigger") == "metadata-import":
                    data["activity_type"] = "metadata_import"
                    data["label"] = "历史目录导入"
                elif data.get("trigger") == "normalize-actress":
                    data["activity_type"] = "actress_normalize"
                    data["label"] = "女优名归一化"
                else:
                    data["activity_type"] = "scrape_run"
                    data["label"] = str(data.get("trigger") or "run")
                activities.append(data)

            rows = conn.execute(
                """
                SELECT r.*, t.avid
                FROM metadata_refresh_runs r
                LEFT JOIN tasks t ON t.id = r.task_id
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                data = dict(row)
                success = data.get("success")
                if success is None:
                    status = TASK_RUNNING
                else:
                    status = TASK_SUCCESS if int(success) == 1 else TASK_FAILED
                data.update(
                    {
                        "activity_type": "metadata_refresh",
                        "trigger": data.get("trigger_type"),
                        "status": status,
                        "label": f"元数据优化 {data.get('avid') or data.get('task_id')}",
                        "message": data.get("error") or "",
                    }
                )
                activities.append(data)

        activities.sort(key=lambda item: float(item.get("started_at") or 0), reverse=True)
        return activities[:limit]

    def file_recently_changed(self, path: str, size: int, stable_seconds: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT size, updated_at FROM file_index WHERE path = ?", (os.path.abspath(path),)).fetchone()
        if not row:
            return False
        return int(row["size"]) != int(size) and (now_ts() - float(row["updated_at"])) < stable_seconds

    def enqueue_movie(self, movie: Movie, run_id: int, status: str = TASK_PENDING, reason: str | None = None) -> tuple[str, int | None]:
        files = [os.path.abspath(i) for i in movie.files]
        fingerprint = fingerprint_files(files)
        snapshot = file_snapshot(files)
        ts = now_ts()
        avid = movie_id(movie)

        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE fingerprint = ?", (fingerprint,)).fetchone()
            if existing and existing["status"] == TASK_SUCCESS:
                current_nfo_path = existing["current_nfo_path"] if "current_nfo_path" in existing.keys() else None
                if current_nfo_path and not os.path.exists(current_nfo_path):
                    logger.info(f"成功任务的 NFO 已不存在，重新加入刮削队列: {avid} ({current_nfo_path})")
                else:
                    self._upsert_file_index(conn, snapshot, fingerprint, existing["id"], TASK_SUCCESS, ts)
                    self._record_event_conn(
                        conn,
                        run_id,
                        int(existing["id"]),
                        "info",
                        "scan_skipped",
                        f"已存在成功任务，跳过: {avid}",
                        {"avid": avid, "reason": "success_task_exists"},
                    )
                    return TASK_SKIPPED, int(existing["id"])
                self._upsert_file_index(conn, snapshot, fingerprint, existing["id"], TASK_SUCCESS, ts)
            if existing and existing["status"] == TASK_FAILED and existing["next_retry_at"] and float(existing["next_retry_at"]) > ts:
                self._upsert_file_index(conn, snapshot, fingerprint, existing["id"], TASK_FAILED, ts)
                self._record_event_conn(
                    conn,
                    run_id,
                    int(existing["id"]),
                    "info",
                    "scan_skipped",
                    f"失败任务仍在冷却，跳过: {avid}",
                    {"avid": avid, "reason": "retry_cooldown", "next_retry_at": existing["next_retry_at"]},
                )
                return TASK_SKIPPED, int(existing["id"])
            if existing and existing["status"] == TASK_DEFERRED and status == TASK_PENDING:
                logger.info(f"延迟任务已满足入队条件，重新加入刮削队列: {avid}")
            elif existing and existing["status"] in RUNNING_STATES:
                self._upsert_file_index(conn, snapshot, fingerprint, existing["id"], existing["status"], ts)
                self._record_event_conn(
                    conn,
                    run_id,
                    int(existing["id"]),
                    "info",
                    "scan_skipped",
                    f"任务仍在队列中，跳过重复入队: {avid}",
                    {"avid": avid, "status": existing["status"]},
                )
                return existing["status"], int(existing["id"])

            next_retry_at = None
            if status == TASK_DEFERRED:
                next_retry_at = ts + int(Cfg().daemon.download_stable_seconds)

            if existing:
                task_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE tasks
                    SET task_type = ?, avid = ?, data_src = ?, files_json = ?, status = ?,
                        files_snapshot_json = ?, current_files_json = ?, failure_stage = ?, failure_reason = ?, next_retry_at = ?, updated_at = ?,
                        last_run_id = ?
                    WHERE id = ?
                    """,
                    (
                        "scrape_movie",
                        avid,
                        movie.data_src,
                        json_dumps(files),
                        status,
                        json_dumps(snapshot),
                        json_dumps(files),
                        "scan" if status == TASK_DEFERRED else None,
                        reason,
                        next_retry_at,
                        ts,
                        run_id,
                        task_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO tasks(
                        task_type, avid, data_src, files_json, fingerprint, status,
                        files_snapshot_json, current_files_json, failure_stage, failure_reason, next_retry_at, created_at, updated_at, last_run_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "scrape_movie",
                        avid,
                        movie.data_src,
                        json_dumps(files),
                        fingerprint,
                        status,
                        json_dumps(snapshot),
                        json_dumps(files),
                        "scan" if status == TASK_DEFERRED else None,
                        reason,
                        next_retry_at,
                        ts,
                        ts,
                        run_id,
                    ),
                )
                task_id = int(cur.lastrowid)
            self._upsert_file_index(conn, snapshot, fingerprint, task_id, status, ts)
            event_type = "task_deferred" if status == TASK_DEFERRED else "task_enqueued"
            level = "warning" if status == TASK_DEFERRED else "info"
            message = f"任务延迟入队: {avid}" if status == TASK_DEFERRED else f"任务入队: {avid}"
            self._record_event_conn(
                conn,
                run_id,
                task_id,
                level,
                event_type,
                message,
                {"avid": avid, "files": files, "reason": reason, "status": status},
            )
            return status, task_id

    def _upsert_file_index(self, conn: sqlite3.Connection, snapshot: list[dict[str, Any]], fingerprint: str, task_id: int, status: str, ts: float) -> None:
        for item in snapshot:
            conn.execute(
                """
                INSERT INTO file_index(path, size, mtime, fingerprint, task_id, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    fingerprint = excluded.fingerprint,
                    task_id = excluded.task_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (item["path"], item["size"], item["mtime"], fingerprint, task_id, status, ts),
            )

    def get_due_tasks(self, limit: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [TASK_PENDING, TASK_FAILED, int(Cfg().retry_policy.max_retry_count), now_ts(), TASK_DEFERRED, now_ts()]
        sql = """
            SELECT * FROM tasks
            WHERE status = ?
               OR (status = ? AND retry_count <= ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?)
               OR (status = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?)
            ORDER BY created_at ASC
        """
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def mark_running(self, task_id: int, run_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, failure_stage = NULL, failure_reason = NULL, updated_at = ?, last_run_id = COALESCE(?, last_run_id) WHERE id = ?",
                (TASK_RUNNING, now_ts(), run_id, task_id),
            )
            self._record_event_conn(conn, run_id, task_id, "info", "task_started", "开始处理任务", {"task_id": task_id})

    def mark_success(self, task_id: int, save_dir: str | None = None, movie: Movie | None = None) -> None:
        ts = now_ts()
        current_files = None
        current_save_dir = save_dir
        current_nfo_path = None
        current_fanart_path = None
        current_poster_path = None
        info_json = None
        metadata_complete = None
        metadata_reasons = None
        if movie:
            current_files = [os.path.abspath(i) for i in getattr(movie, "new_paths", None) or movie.files]
            current_save_dir = os.path.abspath(movie.save_dir) if movie.save_dir else save_dir
            current_nfo_path = os.path.abspath(movie.nfo_file) if movie.nfo_file else None
            current_fanart_path = os.path.abspath(movie.fanart_file) if movie.fanart_file else None
            current_poster_path = os.path.abspath(movie.poster_file) if movie.poster_file else None
            if movie.info:
                info_json = json_dumps(info_to_dict(movie.info))
                complete, reasons = check_metadata_complete(movie.info)
                metadata_complete = 1 if complete else 0
                metadata_reasons = json_dumps(reasons)
        with self.connect() as conn:
            row = conn.execute("SELECT fingerprint, last_run_id, avid FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, failure_stage = NULL, failure_reason = NULL, save_dir = ?,
                    current_files_json = COALESCE(?, current_files_json),
                    current_save_dir = COALESCE(?, current_save_dir),
                    current_nfo_path = COALESCE(?, current_nfo_path),
                    current_fanart_path = COALESCE(?, current_fanart_path),
                    current_poster_path = COALESCE(?, current_poster_path),
                    info_json = COALESCE(?, info_json),
                    metadata_checked_at = COALESCE(?, metadata_checked_at),
                    metadata_complete = COALESCE(?, metadata_complete),
                    metadata_incomplete_reasons_json = COALESCE(?, metadata_incomplete_reasons_json),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    TASK_SUCCESS,
                    current_save_dir,
                    json_dumps(current_files) if current_files is not None else None,
                    current_save_dir,
                    current_nfo_path,
                    current_fanart_path,
                    current_poster_path,
                    info_json,
                    ts if info_json is not None else None,
                    metadata_complete,
                    metadata_reasons,
                    ts,
                    task_id,
                ),
            )
            if row:
                conn.execute(
                    "UPDATE file_index SET status = ?, updated_at = ? WHERE fingerprint = ?",
                    (TASK_SUCCESS, ts, row["fingerprint"]),
                )
                self._record_event_conn(
                    conn,
                    row["last_run_id"],
                    task_id,
                    "info",
                    "task_success",
                    f"任务完成: {row['avid']}",
                    {
                        "save_dir": current_save_dir,
                        "nfo_path": current_nfo_path,
                        "fanart_path": current_fanart_path,
                        "poster_path": current_poster_path,
                        "metadata_complete": metadata_complete,
                    },
                )

    def mark_failure(self, task_id: int, stage: str, reason: str, retryable: bool = True) -> None:
        ts = now_ts()
        with self.connect() as conn:
            row = conn.execute("SELECT retry_count, last_run_id, avid FROM tasks WHERE id = ?", (task_id,)).fetchone()
            retry_count = int(row["retry_count"] or 0) + 1 if row else 1
            max_retry = int(Cfg().retry_policy.max_retry_count)
            next_retry_at = None
            status = TASK_FAILED
            if retryable and retry_count <= max_retry:
                delay = Cfg().retry_policy.network_retry_after
                if stage in {"summary", "metadata"}:
                    delay = Cfg().retry_policy.metadata_retry_after
                next_retry_at = ts + delay.total_seconds()
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, failure_stage = ?, failure_reason = ?, retry_count = ?,
                    next_retry_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, stage, reason, retry_count, next_retry_at, ts, task_id),
            )
            if row:
                self._record_event_conn(
                    conn,
                    row["last_run_id"],
                    task_id,
                    "error",
                    "task_failed",
                    f"任务失败: {row['avid']} - {reason}",
                    {
                        "stage": stage,
                        "reason": reason,
                        "retryable": retryable,
                        "retry_count": retry_count,
                        "next_retry_at": next_retry_at,
                    },
                )

    def mark_deferred(self, task_id: int, reason: str) -> None:
        ts = now_ts()
        with self.connect() as conn:
            row = conn.execute("SELECT last_run_id, avid FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, failure_stage = ?, failure_reason = ?,
                    next_retry_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    TASK_DEFERRED,
                    "file_pending",
                    reason,
                    ts + int(Cfg().daemon.download_stable_seconds),
                    ts,
                    task_id,
                ),
            )
            if row:
                self._record_event_conn(
                    conn,
                    row["last_run_id"],
                    task_id,
                    "warning",
                    "task_deferred",
                    f"任务延迟: {row['avid']} - {reason}",
                    {"reason": reason},
                )

    def retry_task(self, task_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return False
            if row["status"] not in {TASK_FAILED, TASK_DEFERRED}:
                return False
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, failure_stage = NULL, failure_reason = NULL,
                    next_retry_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (TASK_PENDING, now_ts(), task_id),
            )
        return True

    def delete_task(self, task_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT fingerprint FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM scrape_results WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM metadata_refresh_runs WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM rescrape_sessions WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM file_index WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return True

    def _task_filters(self, statuses: list[str] | None = None, updated_after: float | None = None,
                      last_run_id: int | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append("status IN ({})".format(",".join("?" for _ in statuses)))
            params.extend(statuses)
        if updated_after is not None:
            clauses.append("updated_at >= ?")
            params.append(updated_after)
        if last_run_id is not None:
            clauses.append("last_run_id = ?")
            params.append(last_run_id)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    def list_tasks(self, statuses: list[str] | None = None, limit: int = 200, offset: int = 0,
                   order_by: str = "updated_at", sort_dir: str = "DESC",
                   updated_after: float | None = None, last_run_id: int | None = None) -> list[dict[str, Any]]:
        allowed_columns = {"id", "avid", "status", "data_src", "updated_at", "created_at",
                           "metadata_status", "metadata_complete"}
        if order_by not in allowed_columns:
            order_by = "updated_at"
        sort_dir = "DESC" if sort_dir.upper() == "DESC" else "ASC"
        where, params = self._task_filters(statuses, updated_after, last_run_id)
        params.extend([limit, offset])
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY {order_by} {sort_dir} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._decode_task(row) for row in rows]

    def count_tasks(self, statuses: list[str] | None = None, updated_after: float | None = None,
                    last_run_id: int | None = None) -> int:
        where, params = self._task_filters(statuses, updated_after, last_run_id)
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM tasks {where}", params).fetchone()
        return row["cnt"] if row else 0

    def delete_tasks(self, task_ids: list[int]) -> int:
        if not task_ids:
            return 0
        with self.connect() as conn:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM scrape_results WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM metadata_refresh_runs WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM rescrape_sessions WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM file_index WHERE task_id IN ({placeholders})", task_ids)
            c = conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
        return c.rowcount

    def delete_tasks_by_status(self, status: str, older_than_days: int = 0) -> int:
        with self.connect() as conn:
            if older_than_days > 0:
                cutoff = now_ts() - older_than_days * 86400
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE status = ? AND updated_at < ?", (status, cutoff)
                ).fetchall()
            else:
                rows = conn.execute("SELECT id FROM tasks WHERE status = ?", (status,)).fetchall()
            task_ids = [r["id"] for r in rows]
        return self.delete_tasks(task_ids)

    def sync_file_inventory(self) -> dict[str, Any]:
        """同步数据库与文件系统：删除文件已不存在的任务记录。

        遍历除 running/deferred 外的所有任务，
        若可安全定位的影片文件中任一文件不存在 → 级联删除该任务。
        成功任务只使用 current_files_json 判断，避免升级旧库时用整理前路径误删。
        同时清理 file_index 中的孤立条目。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, avid, status, current_files_json, files_json FROM tasks WHERE status NOT IN (?, ?)",
                (TASK_RUNNING, TASK_DEFERRED),
            ).fetchall()

        def files_for_inventory(row: sqlite3.Row) -> list[str]:
            current_files = row["current_files_json"] if "current_files_json" in row.keys() else None
            files = json_loads(current_files, [])
            if not files and row["status"] != TASK_SUCCESS:
                files = json_loads(row["files_json"], [])
            return files if isinstance(files, list) else []

        purge_ids: list[int] = []
        details: list[dict[str, Any]] = []
        for row in rows:
            task_id = int(row["id"])
            files = files_for_inventory(row)
            if not files:
                continue
            missing = [f for f in files if not os.path.exists(f)]
            if not missing:
                continue
            purge_ids.append(task_id)
            details.append({
                "task_id": task_id,
                "avid": row["avid"],
                "status": row["status"],
                "missing_files": missing,
            })

        purged_task_ids: list[int] = []
        if purge_ids:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in purge_ids)
                rows = conn.execute(
                    f"""SELECT id, status, current_files_json, files_json
                        FROM tasks
                        WHERE id IN ({placeholders}) AND status NOT IN (?, ?)""",
                    purge_ids + [TASK_RUNNING, TASK_DEFERRED],
                ).fetchall()
                deletable = set()
                for row in rows:
                    files = files_for_inventory(row)
                    if files and any(not os.path.exists(f) for f in files):
                        deletable.add(int(row["id"]))
                purged_task_ids = [task_id for task_id in purge_ids if task_id in deletable]
                if purged_task_ids:
                    placeholders = ",".join("?" for _ in purged_task_ids)
                    conn.execute(f"DELETE FROM scrape_results WHERE task_id IN ({placeholders})", purged_task_ids)
                    conn.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", purged_task_ids)
                    conn.execute(f"DELETE FROM metadata_refresh_runs WHERE task_id IN ({placeholders})", purged_task_ids)
                    conn.execute(f"DELETE FROM rescrape_sessions WHERE task_id IN ({placeholders})", purged_task_ids)
                    conn.execute(f"DELETE FROM file_index WHERE task_id IN ({placeholders})", purged_task_ids)
                    conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", purged_task_ids)

            count = len(purged_task_ids)
            if count:
                logger.info("文件同步：清理了 %d 个失效任务（影片文件已不存在）", count)
            for d in details:
                if d["task_id"] not in purged_task_ids:
                    continue
                logger.debug(
                    "  任务 %d (%s, 原状态 %s): 缺失文件 %s",
                    d["task_id"], d["avid"], d["status"], d["missing_files"],
                )

        with self.connect() as conn:
            cur = conn.execute("DELETE FROM file_index WHERE task_id NOT IN (SELECT id FROM tasks)")
            orphaned = cur.rowcount
            if orphaned:
                logger.info("文件同步：清理了 %d 条 file_index 孤立记录", orphaned)

        return {"purged": len(purged_task_ids), "purged_task_ids": purged_task_ids}

    def batch_retry(self, task_ids: list[int]) -> int:
        if not task_ids:
            return 0
        with self.connect() as conn:
            placeholders = ",".join("?" for _ in task_ids)
            c = conn.execute(
                f"""UPDATE tasks SET status = ?, failure_stage = NULL, failure_reason = NULL,
                    next_retry_at = NULL, updated_at = ?
                    WHERE id IN ({placeholders}) AND status IN (?, ?)""",
                [TASK_PENDING, now_ts()] + task_ids + [TASK_FAILED, TASK_DEFERRED],
            )
        return c.rowcount

    def list_failures(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_tasks([TASK_FAILED], limit)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._decode_task(row) if row else None

    def list_scrape_results(self, task_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scrape_results WHERE task_id = ? ORDER BY updated_at DESC",
                (task_id,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["success"] = bool(item.get("success"))
            item["fields"] = json_loads(item.pop("fields_json", None), [])
            item["info"] = json_loads(item.pop("info_json", None), None)
            item["elapsed"] = float(item.get("elapsed") or 0)
            results.append(item)
        return results

    def list_scrape_results_for_tasks(self, task_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """一次查询多个任务的爬虫结果，避免任务列表接口产生 N+1 查询。"""
        grouped: dict[int, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
        if not task_ids:
            return grouped
        placeholders = ",".join("?" for _ in task_ids)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT task_id, crawler_name, success, fields_json, url, title, "
                f"elapsed, error, updated_at FROM scrape_results WHERE task_id IN ({placeholders}) "
                "ORDER BY task_id, updated_at DESC",
                task_ids,
            ).fetchall()
        for row in rows:
            item = dict(row)
            item["success"] = bool(item.get("success"))
            item["fields"] = json_loads(item.pop("fields_json", None), [])
            item["elapsed"] = float(item.get("elapsed") or 0)
            grouped.setdefault(int(item["task_id"]), []).append(item)
        return grouped

    def metadata_sources(self, task_id: int) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if not task:
            return None
        comparison = build_metadata_source_comparison(task.get("info"), self.list_scrape_results(task_id))
        comparison["task"] = task
        return comparison

    @staticmethod
    def _decode_rescrape_session(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        data["candidates"] = json_loads(data.pop("candidates_json", None), {})
        data["auto_info"] = json_loads(data.pop("auto_info_json", None), None)
        data["final_values"] = json_loads(data.pop("final_values_json", None), None)
        return data

    @staticmethod
    def rescrape_session_summary(session: dict[str, Any] | None) -> dict[str, Any] | None:
        if not session:
            return None
        return {
            "id": int(session["id"]),
            "task_id": int(session["task_id"]),
            "status": session["status"],
            "error": session.get("error"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "finished_at": session.get("finished_at"),
        }

    def get_rescrape_session(self, session_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rescrape_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._decode_rescrape_session(row)

    def active_rescrape_session(self, task_id: int) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_RESCRAPE_SESSION_STATES)
        params = [task_id, *sorted(ACTIVE_RESCRAPE_SESSION_STATES)]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM rescrape_sessions WHERE task_id = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return self._decode_rescrape_session(row)

    def active_rescrape_sessions_for_tasks(self, task_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not task_ids:
            return {}
        task_placeholders = ",".join("?" for _ in task_ids)
        state_placeholders = ",".join("?" for _ in ACTIVE_RESCRAPE_SESSION_STATES)
        params = [*task_ids, *sorted(ACTIVE_RESCRAPE_SESSION_STATES)]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rescrape_sessions "
                f"WHERE task_id IN ({task_placeholders}) AND status IN ({state_placeholders}) "
                "ORDER BY id DESC",
                params,
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            session = self._decode_rescrape_session(row)
            task_id = int(session["task_id"])
            result.setdefault(task_id, self.rescrape_session_summary(session))
        return result

    def create_rescrape_session(self, task_id: int) -> tuple[dict[str, Any], bool]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError("任务不存在")
        if task.get("status") != TASK_SUCCESS:
            raise ValueError("只有成功任务可以重新刮削")
        existing = self.active_rescrape_session(task_id)
        if existing:
            return existing, False
        ts = now_ts()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO rescrape_sessions(task_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (task_id, RESCRAPE_SESSION_SCRAPING, ts, ts),
            )
            session_id = int(cur.lastrowid)
            self._record_event_conn(
                conn, None, task_id, "info", "interactive_rescrape_started",
                "自由选择重新刮削开始抓取候选数据",
                {"session_id": session_id},
            )
        return self.get_rescrape_session(session_id), True

    def save_rescrape_candidates(
        self,
        session_id: int,
        candidates: dict[str, Any],
        auto_info: Any,
    ) -> dict[str, Any]:
        session = self.get_rescrape_session(session_id)
        if not session or session.get("status") != RESCRAPE_SESSION_SCRAPING:
            raise ValueError("重新刮削会话已失效")
        ts = now_ts()
        auto_data = info_to_dict(auto_info) if auto_info else None
        with self.connect() as conn:
            conn.execute(
                "UPDATE rescrape_sessions SET status = ?, candidates_json = ?, auto_info_json = ?, error = NULL, updated_at = ? WHERE id = ?",
                (RESCRAPE_SESSION_AWAITING, json_dumps(candidates), json_dumps(auto_data), ts, session_id),
            )
            self._record_event_conn(
                conn, None, int(session["task_id"]), "info", "interactive_rescrape_awaiting_selection",
                "候选数据抓取完成，等待人工选择",
                {"session_id": session_id, "sources": list(candidates)},
            )
        return self.get_rescrape_session(session_id)

    def begin_rescrape_apply(self, session_id: int, final_values: dict[str, Any]) -> dict[str, Any]:
        session = self.get_rescrape_session(session_id)
        if not session or session.get("status") != RESCRAPE_SESSION_AWAITING:
            raise ValueError("该会话当前不能提交")
        unknown = set(final_values) - INTERACTIVE_RESCRAPE_FIELD_NAMES
        if unknown:
            raise ValueError("包含不可编辑字段: " + ", ".join(sorted(unknown)))
        missing = INTERACTIVE_RESCRAPE_FIELD_NAMES - set(final_values)
        if missing:
            raise ValueError("缺少字段: " + ", ".join(sorted(missing)))
        ts = now_ts()
        with self.connect() as conn:
            conn.execute(
                "UPDATE rescrape_sessions SET status = ?, final_values_json = ?, error = NULL, updated_at = ? WHERE id = ?",
                (RESCRAPE_SESSION_APPLYING, json_dumps(final_values), ts, session_id),
            )
            self._record_event_conn(
                conn, None, int(session["task_id"]), "info", "interactive_rescrape_applying",
                "人工选择已确认，开始更新元数据和媒体文件",
                {"session_id": session_id},
            )
        return self.get_rescrape_session(session_id)

    def set_rescrape_session_result(self, session_id: int, success: bool, error: str | None = None) -> None:
        session = self.get_rescrape_session(session_id)
        if not session or session.get("status") in {
            RESCRAPE_SESSION_COMPLETED,
            RESCRAPE_SESSION_CANCELLED,
            RESCRAPE_SESSION_FAILED,
        }:
            return
        ts = now_ts()
        status = RESCRAPE_SESSION_COMPLETED if success else RESCRAPE_SESSION_FAILED
        with self.connect() as conn:
            conn.execute(
                "UPDATE rescrape_sessions SET status = ?, error = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                (status, error, ts, ts, session_id),
            )
            self._record_event_conn(
                conn, None, int(session["task_id"]), "info" if success else "error",
                "interactive_rescrape_completed" if success else "interactive_rescrape_failed",
                "自由选择重新刮削完成" if success else f"自由选择重新刮削失败: {error or '未知错误'}",
                {"session_id": session_id, "error": error},
            )

    def restore_rescrape_awaiting(self, session_id: int, error: str | None = None) -> None:
        session = self.get_rescrape_session(session_id)
        if not session or session.get("status") != RESCRAPE_SESSION_APPLYING:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE rescrape_sessions SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (RESCRAPE_SESSION_AWAITING, error, now_ts(), session_id),
            )

    def fail_rescrape_session(self, session_id: int, error: str) -> None:
        self.set_rescrape_session_result(session_id, False, error)

    def cancel_rescrape_session(self, session_id: int) -> bool:
        session = self.get_rescrape_session(session_id)
        if not session or session.get("status") != RESCRAPE_SESSION_AWAITING:
            return False
        ts = now_ts()
        with self.connect() as conn:
            conn.execute(
                "UPDATE rescrape_sessions SET status = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                (RESCRAPE_SESSION_CANCELLED, ts, ts, session_id),
            )
            self._record_event_conn(
                conn, None, int(session["task_id"]), "info", "interactive_rescrape_cancelled",
                "已取消自由选择重新刮削，未修改原有元数据和媒体文件",
                {"session_id": session_id},
            )
        return True

    def count_awaiting_rescrape_sessions(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM rescrape_sessions WHERE status = ?",
                (RESCRAPE_SESSION_AWAITING,),
            ).fetchone()
        return int(row["cnt"] or 0)

    def _decode_task(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["files"] = json_loads(data.pop("files_json", None), [])
        data["current_files"] = json_loads(data.pop("current_files_json", None), [])
        data["info"] = json_loads(data.pop("info_json", None), None)
        data["metadata_incomplete_reasons"] = json_loads(data.pop("metadata_incomplete_reasons_json", None), [])
        if data.get("status") != TASK_SUCCESS:
            data["metadata_status"] = "-"
        elif data.get("metadata_complete") == METADATA_PATH_UNKNOWN:
            data["metadata_status"] = "path_unknown"
        elif data.get("metadata_complete") is None:
            data["metadata_status"] = "not_checked"
        elif int(data.get("metadata_complete") or 0) == 1:
            data["metadata_status"] = "complete"
        else:
            data["metadata_status"] = "incomplete"
        return data

    def task_to_movie(self, task: dict[str, Any]) -> Movie:
        files = json_loads(task.get("files_json"), [])
        if task["data_src"] == "cid":
            movie = Movie(cid=task["avid"])
        else:
            movie = Movie(task["avid"])
        movie.data_src = task["data_src"]
        movie.files = files
        return movie

    def task_to_refresh_movie(self, task: dict[str, Any]) -> Movie:
        files = (
            task.get("current_files")
            or task.get("files")
            or json_loads(task.get("current_files_json"), [])
            or json_loads(task.get("files_json"), [])
        )
        if task["data_src"] == "cid":
            movie = Movie(cid=task["avid"])
        else:
            movie = Movie(task["avid"])
        movie.data_src = task["data_src"]
        movie.files = files
        return movie

    def _raw_task(self, conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def _locate_legacy_nfo(self, root: str, avid: str) -> tuple[str | None, str | None]:
        matches: list[str] = []
        avid_lower = avid.lower()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in filenames:
                if not filename.lower().endswith(".nfo"):
                    continue
                path = os.path.join(dirpath, filename)
                if avid_lower in filename.lower():
                    matches.append(path)
                    continue
                try:
                    with open(path, "rt", encoding="utf-8", errors="ignore") as f:
                        if avid_lower in f.read().lower():
                            matches.append(path)
                except OSError:
                    continue
        unique = sorted(set(matches))
        if len(unique) == 1:
            return unique[0], os.path.dirname(unique[0])
        return None, None

    def load_task_info(self, task_id: int, root: str | None = None) -> tuple[dict[str, Any] | None, Any | None]:
        task = self.get_task(task_id)
        if not task:
            return None, None
        info = info_from_dict(task.get("info"), task.get("avid"), task.get("data_src") or "normal") if task.get("info") else None
        if info:
            return task, info
        nfo_path = task.get("current_nfo_path")
        if nfo_path and not os.path.exists(nfo_path):
            nfo_path = None
        if not nfo_path and root:
            nfo_path, save_dir = self._locate_legacy_nfo(root, task["avid"])
            if nfo_path:
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET current_nfo_path = ?, current_save_dir = ?, updated_at = ? WHERE id = ?",
                        (nfo_path, save_dir, now_ts(), task_id),
                    )
                task["current_nfo_path"] = nfo_path
                task["current_save_dir"] = save_dir
        info = load_info_from_nfo(nfo_path, task.get("avid"), task.get("data_src") or "normal") if nfo_path else None
        return task, info

    def check_task_metadata(self, task_id: int, root: str | None = None) -> dict[str, Any] | None:
        task, info = self.load_task_info(task_id, root)
        if not task:
            return None
        ts = now_ts()
        if task.get("status") != TASK_SUCCESS:
            return task
        if not info:
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE tasks SET metadata_checked_at = ?, metadata_complete = ?,
                        metadata_incomplete_reasons_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ts,
                        METADATA_PATH_UNKNOWN,
                        json_dumps([{"field": "_path", "reason": "path_unknown", "message": "无法定位 NFO 或元数据"}]),
                        ts,
                        task_id,
                    ),
                )
            return self.get_task(task_id)
        save_dir = task.get("current_save_dir") or task.get("save_dir")
        if save_dir:
            fs_pics = _scan_extrafanart(save_dir)
            if fs_pics is not None:
                info.preview_pics = fs_pics
            elif isinstance(info.preview_pics, list) and info.preview_pics and \
                 isinstance(info.preview_pics[0], str) and info.preview_pics[0].startswith('/'):
                info.preview_pics = None
        complete, reasons = check_metadata_complete(info)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks SET info_json = ?, metadata_checked_at = ?,
                    metadata_complete = ?, metadata_incomplete_reasons_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json_dumps(info_to_dict(info)), ts, 1 if complete else 0, json_dumps(reasons), ts, task_id),
            )
        return self.get_task(task_id)

    def check_success_metadata(self, root: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        tasks = self.list_tasks([TASK_SUCCESS], limit or 1000)
        checked = []
        for task in tasks:
            checked_task = self.check_task_metadata(int(task["id"]), root)
            if checked_task:
                checked.append(checked_task)
        logger.info("check_success_metadata: %d tasks checked, %d complete, %d incomplete, %d not_checked",
                     len(checked),
                     sum(1 for t in checked if t.get("metadata_status") == "complete"),
                     sum(1 for t in checked if t.get("metadata_status") == "incomplete"),
                     sum(1 for t in checked if t.get("metadata_status") == "not_checked"))
        return checked

    def due_incomplete_metadata_tasks(self, root: str | None = None, manual: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
        checked = self.check_success_metadata(root, limit or 1000)
        ts = now_ts()
        due = []
        for task in checked:
            if task.get("metadata_status") != "incomplete":
                continue
            if not manual:
                last = task.get("last_metadata_refresh_at") or task.get("created_at") or 0
                if ts - float(last) < Cfg().metadata_complete.refresh_after.total_seconds():
                    continue
            due.append(task)
        return due

    def start_metadata_refresh(self, task_id: int, trigger_type: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO metadata_refresh_runs(task_id, trigger_type, started_at) VALUES (?, ?, ?)",
                (task_id, trigger_type, now_ts()),
            )
            refresh_id = int(cur.lastrowid)
            self._record_event_conn(
                conn,
                None,
                task_id,
                "info",
                "metadata_refresh_started",
                f"开始元数据优化: {trigger_type}",
                {"refresh_id": refresh_id, "trigger_type": trigger_type},
            )
            return refresh_id

    def finish_metadata_refresh(
        self,
        refresh_id: int,
        task_id: int,
        success: bool,
        updated_fields: list[str] | None = None,
        reasons: list[dict[str, Any]] | None = None,
        error: str | None = None,
        info: Any | None = None,
    ) -> None:
        ts = now_ts()
        complete = None
        reasons = reasons or []
        info_json = None
        if info:
            complete, reasons = check_metadata_complete(info)
            info_json = json_dumps(info_to_dict(info))
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE metadata_refresh_runs
                SET finished_at = ?, success = ?, updated_fields_json = ?, reasons_json = ?, error = ?
                WHERE id = ?
                """,
                (ts, 1 if success else 0, json_dumps(updated_fields or []), json_dumps(reasons), error, refresh_id),
            )
            if success:
                conn.execute(
                    """
                    UPDATE tasks
                    SET info_json = COALESCE(?, info_json), metadata_checked_at = ?,
                        metadata_complete = COALESCE(?, metadata_complete),
                        metadata_incomplete_reasons_json = ?, last_metadata_refresh_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (info_json, ts, 1 if complete else 0 if complete is not None else None, json_dumps(reasons), ts, ts, task_id),
                )
            self._record_event_conn(
                conn,
                None,
                task_id,
                "info" if success else "error",
                "metadata_refresh_finished",
                "元数据优化完成" if success else f"元数据优化失败: {error or '未知错误'}",
                {
                    "refresh_id": refresh_id,
                    "success": success,
                    "updated_fields": updated_fields or [],
                    "reasons": reasons,
                    "error": error,
                },
            )

    def validate_task_files(self, task: dict[str, Any]) -> tuple[str, str | None]:
        """Worker 执行前的文件安全检查。

        返回 (action, reason):
        - ok: 可以开始刮削
        - failed: 文件缺失或不再符合基本条件，不自动重试
        - deferred: 文件仍在变化或疑似下载中，下轮再试
        """
        files = json_loads(task.get("files_json"), [])
        if not files:
            return TASK_FAILED, "任务没有关联影片文件"

        missing = [i for i in files if not os.path.exists(i)]
        if missing:
            return TASK_FAILED, "影片文件不存在: " + ", ".join(missing)

        if has_temp_download_marker(files, int(Cfg().daemon.download_stable_seconds) * 2):
            return TASK_DEFERRED, "发现下载临时文件，等待下载完成"

        minimum_size = int(Cfg().scanner.minimum_size)
        too_small = []
        for file in files:
            try:
                if os.path.getsize(file) < minimum_size:
                    too_small.append(file)
            except OSError:
                return TASK_FAILED, f"无法读取影片文件: {file}"
        if too_small:
            return TASK_FAILED, "影片文件小于最小体积限制: " + ", ".join(too_small)

        original = snapshot_by_path(json_loads(task.get("files_snapshot_json"), []))
        current = snapshot_by_path(file_snapshot(files))
        changed = []
        for path, old_item in original.items():
            current_item = current.get(path)
            if not current_item:
                changed.append(path)
                continue
            if int(current_item["size"]) != int(old_item["size"]):
                changed.append(path)
        if changed:
            return TASK_DEFERRED, "影片文件大小发生变化，等待下轮重新扫描: " + ", ".join(changed)

        return "ok", None

    def record_crawler_result(
        self,
        task_id: int | None,
        crawler_name: str,
        success: bool,
        elapsed: float = 0,
        error: str | None = None,
        fields: list[str] | None = None,
        url: str | None = None,
        title: str | None = None,
        info: Any | None = None,
    ) -> None:
        ts = now_ts()
        info_json = json_dumps(info_to_dict(info)) if info else None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM crawler_stats WHERE name = ?", (crawler_name,)).fetchone()
            if success:
                success_count = int(row["success_count"] or 0) + 1 if row else 1
                failure_count = int(row["failure_count"] or 0) if row else 0
                total_elapsed = float(row["total_elapsed"] or 0) + elapsed if row else elapsed
                conn.execute(
                    """
                    INSERT INTO crawler_stats(name, success_count, failure_count, consecutive_failures, total_elapsed, circuit_break_until, last_error, updated_at)
                    VALUES (?, ?, ?, 0, ?, NULL, NULL, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        success_count = excluded.success_count,
                        failure_count = excluded.failure_count,
                        consecutive_failures = 0,
                        total_elapsed = excluded.total_elapsed,
                        circuit_break_until = NULL,
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (crawler_name, success_count, failure_count, total_elapsed, ts),
                )
            else:
                success_count = int(row["success_count"] or 0) if row else 0
                failure_count = int(row["failure_count"] or 0) + 1 if row else 1
                consecutive = int(row["consecutive_failures"] or 0) + 1 if row else 1
                total_elapsed = float(row["total_elapsed"] or 0) + elapsed if row else elapsed
                break_until = None
                if consecutive >= int(Cfg().retry_policy.crawler_circuit_break_threshold):
                    break_until = ts + Cfg().retry_policy.crawler_circuit_break_duration.total_seconds()
                conn.execute(
                    """
                    INSERT INTO crawler_stats(name, success_count, failure_count, consecutive_failures, total_elapsed, circuit_break_until, last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        success_count = excluded.success_count,
                        failure_count = excluded.failure_count,
                        consecutive_failures = excluded.consecutive_failures,
                        total_elapsed = excluded.total_elapsed,
                        circuit_break_until = excluded.circuit_break_until,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (crawler_name, success_count, failure_count, consecutive, total_elapsed, break_until, error, ts),
                )

            if task_id is not None:
                conn.execute(
                    """
                    INSERT INTO scrape_results(task_id, crawler_name, success, fields_json, url, title, info_json, elapsed, error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, crawler_name) DO UPDATE SET
                        success = excluded.success,
                        fields_json = excluded.fields_json,
                        url = excluded.url,
                        title = excluded.title,
                        info_json = excluded.info_json,
                        elapsed = excluded.elapsed,
                        error = excluded.error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        task_id,
                        crawler_name,
                        1 if success else 0,
                        json_dumps(fields or []),
                        url,
                        title,
                        info_json,
                        elapsed,
                        error,
                        ts,
                    ),
                )

    def is_crawler_available(self, crawler_name: str, force: bool = False) -> bool:
        if force:
            return True
        with self.connect() as conn:
            row = conn.execute("SELECT circuit_break_until FROM crawler_stats WHERE name = ?", (crawler_name,)).fetchone()
        return not row or not row["circuit_break_until"] or float(row["circuit_break_until"]) <= now_ts()

    def crawler_health(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM crawler_stats ORDER BY updated_at DESC").fetchall()
        result = []
        ts = now_ts()
        for row in rows:
            item = dict(row)
            try:
                success_count = int(item["success_count"] or 0)
                failure_count = int(item["failure_count"] or 0)
                circuit_break_until = float(item["circuit_break_until"] or 0)
            except (TypeError, ValueError):
                logger.warning("忽略异常的爬虫统计记录: %s", item)
                continue
            total = success_count + failure_count
            item["success_count"] = success_count
            item["failure_count"] = failure_count
            item["success_rate"] = (success_count / total) if total else None
            item["circuit_open"] = bool(circuit_break_until and circuit_break_until > ts)
            result.append(item)
        return result

    def summarize(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
        return {row["status"]: row["count"] for row in rows}


def enqueue_movies(store: TaskStore, movies: list[Movie], run_id: int) -> dict[str, int]:
    # 如果配置了 restrict_to_files，只入队匹配的影片
    target_files = getattr(Cfg().scanner, 'restrict_to_files', None)
    if target_files:
        target_set = {os.path.abspath(f) for f in target_files}
        movies = [m for m in movies if any(os.path.abspath(f) in target_set for f in m.files)]
    enqueued = deferred = skipped = 0
    stable_seconds = int(Cfg().daemon.download_stable_seconds)
    for movie in movies:
        defer_reason = None
        if has_temp_download_marker(movie.files, stable_seconds * 2):
            defer_reason = "发现下载临时文件，等待下载完成"
        else:
            for file in movie.files:
                try:
                    stat = os.stat(file)
                except FileNotFoundError:
                    defer_reason = "影片文件不存在"
                    break
                if store.file_recently_changed(file, stat.st_size, stable_seconds):
                    defer_reason = "文件大小仍在变化，等待稳定"
                    break
        if defer_reason:
            status, _ = store.enqueue_movie(movie, run_id, TASK_DEFERRED, defer_reason)
            deferred += 1 if status == TASK_DEFERRED else 0
            continue

        status, _ = store.enqueue_movie(movie, run_id, TASK_PENDING)
        if status == TASK_PENDING:
            enqueued += 1
        elif status == TASK_SKIPPED:
            skipped += 1
        elif status == TASK_DEFERRED:
            deferred += 1
    store.update_run_scan_counts(run_id, len(movies), enqueued, deferred, skipped)
    return {"scan_total": len(movies), "enqueued": enqueued, "deferred": deferred, "skipped": skipped}


def notify_run_summary(store: TaskStore, run_id: int) -> None:
    latest = store.get_latest_run()
    if not latest or int(latest["id"]) != run_id:
        return
    message = (
        f"JavSP 任务完成: 新增 {latest.get('enqueued', 0)}, 延迟 {latest.get('deferred', 0)}, "
        f"跳过 {latest.get('skipped', 0)}, 成功 {latest.get('succeeded', 0)}, 失败 {latest.get('failed', 0)}"
    )
    logger.info(message)
    if not Cfg().notifications.enabled or not Cfg().notifications.webhook_url:
        return
    try:
        requests.post(str(Cfg().notifications.webhook_url), json={"text": message, "run": latest}, timeout=10)
    except Exception as e:
        logger.warning(f"发送通知失败: {e}")
