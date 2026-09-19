import os
from types import SimpleNamespace

import pytest

from javsp.datatype import Movie, MovieInfo
from javsp.task import TaskStore, TASK_DEFERRED
from web_ui import web_server


def _seed_success_task(store, tmp_path):
    movie = Movie("ABC-123")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie.files = [str(video)]
    run_id = store.start_run("test")
    _, task_id = store.enqueue_movie(movie, run_id)

    info = MovieInfo("ABC-123")
    info.title = "美丽出道"
    info.plot = "这是一段已经补全的中文剧情简介，长度足够用于完整性判断。"
    movie.info = info
    movie.save_dir = str(tmp_path)
    movie.nfo_file = str(tmp_path / "ABC-123.nfo")
    movie.fanart_file = str(tmp_path / "fanart.jpg")
    movie.poster_file = str(tmp_path / "poster.jpg")
    store.mark_success(task_id, movie.save_dir, movie)
    store.record_crawler_result(task_id, "javdb", True, fields=["title", "plot"], title=info.title, info=info)
    store.record_event(run_id, task_id, "info", "test_event", "结构化事件")
    store.finish_run(run_id)
    return run_id, task_id


def test_task_events_and_metadata_sources_api(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "state.db")
    _, task_id = _seed_success_task(store, tmp_path)
    monkeypatch.setattr(web_server, "get_task_store", lambda: store)
    monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
    monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)

    client = web_server.app.test_client()

    events = client.get(f"/api/tasks/{task_id}/events")
    sources = client.get(f"/api/tasks/{task_id}/metadata/sources")

    assert events.status_code == 200
    assert any(event["event_type"] == "test_event" for event in events.get_json()["events"])
    assert sources.status_code == 200
    body = sources.get_json()
    assert body["final_info"]["title"] == "美丽出道"
    assert any(field["field"] == "title" and field["adopted_source"] == "javdb" for field in body["fields"])


def test_run_detail_and_events_api(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "state.db")
    run_id, task_id = _seed_success_task(store, tmp_path)
    monkeypatch.setattr(web_server, "get_task_store", lambda: store)

    client = web_server.app.test_client()

    detail = client.get(f"/api/runs/{run_id}")
    events = client.get(f"/api/runs/{run_id}/events?task_id={task_id}")

    assert detail.status_code == 200
    assert detail.get_json()["run"]["id"] == run_id
    assert [task["id"] for task in detail.get_json()["tasks"]] == [task_id]
    assert events.status_code == 200
    assert all(event["task_id"] == task_id for event in events.get_json()["events"])


def test_normalize_actress_api_starts_background_command(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))

    def fake_run(runtime_command, extra_args=None):
        captured["runtime_command"] = runtime_command
        captured["extra_args"] = extra_args
        return True, "任务已启动"

    monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
    client = web_server.app.test_client()

    res = client.post(
        "/api/metadata/normalize_actress",
        json={"path": "library", "move": True},
    )

    assert res.status_code == 202
    assert captured["runtime_command"] == "normalize-actress"
    assert captured["extra_args"][0] == "--move"
    assert captured["extra_args"][1] == str(tmp_path / "library")


def test_normalize_actress_api_requires_path():
    client = web_server.app.test_client()

    res = client.post("/api/metadata/normalize_actress", json={})

    assert res.status_code == 400


def test_metadata_rescrape_api_starts_replace_command(monkeypatch):
    captured = {}

    def fake_run(runtime_command, extra_args=None):
        captured["runtime_command"] = runtime_command
        captured["extra_args"] = extra_args
        return True, "任务已启动"

    monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
    client = web_server.app.test_client()

    res = client.post("/api/tasks/42/metadata/rescrape")

    assert res.status_code == 202
    assert captured["runtime_command"] == "rescrape-task"
    assert captured["extra_args"] == [42]


def test_interactive_rescrape_uses_new_api_without_changing_legacy_endpoint(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "state.db")
    _, task_id = _seed_success_task(store, tmp_path)
    captured = []

    def fake_run(runtime_command, extra_args=None):
        captured.append((runtime_command, extra_args))
        return True, "任务已启动"

    monkeypatch.setattr(web_server, "get_task_store", lambda: store)
    monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
    client = web_server.app.test_client()

    interactive = client.post(f"/api/tasks/{task_id}/metadata/rescrape/interactive")
    legacy = client.post(f"/api/tasks/{task_id}/metadata/rescrape")

    assert interactive.status_code == 202
    session = interactive.get_json()["session"]
    assert session["status"] == "scraping"
    assert captured[0] == ("rescrape-prepare", [session["id"]])
    assert legacy.status_code == 202
    assert captured[1] == ("rescrape-task", [task_id])


def test_interactive_rescrape_payload_excludes_media_and_can_apply(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "state.db")
    _, task_id = _seed_success_task(store, tmp_path)
    session, _ = store.create_rescrape_session(task_id)
    candidate = MovieInfo("ABC-123")
    candidate.title = "候选标题"
    candidate.plot = "候选简介"
    candidate.cover = "https://example.test/cover.jpg"
    candidate.preview_pics = ["https://example.test/1.jpg"]
    store.save_rescrape_candidates(session["id"], {"javdb": {
        "dvdid": "ABC-123",
        "title": candidate.title,
        "plot": candidate.plot,
        "cover": candidate.cover,
        "preview_pics": candidate.preview_pics,
    }}, candidate)
    captured = {}

    def fake_run(runtime_command, extra_args=None):
        captured["runtime_command"] = runtime_command
        captured["extra_args"] = extra_args
        return True, "任务已启动"

    monkeypatch.setattr(web_server, "get_task_store", lambda: store)
    monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
    client = web_server.app.test_client()

    detail = client.get(f"/api/rescrape-sessions/{session['id']}")
    assert detail.status_code == 200
    body = detail.get_json()
    field_names = {field["field"] for field in body["fields"]}
    assert "title" in field_names
    assert "cover" not in field_names
    assert "preview_pics" not in field_names

    values = {field["field"]: field["auto_value"] for field in body["fields"]}
    values["title"] = "人工编辑标题"
    applied = client.post(f"/api/rescrape-sessions/{session['id']}/apply", json={"values": values})
    assert applied.status_code == 202
    assert captured == {"runtime_command": "rescrape-apply", "extra_args": [session["id"]]}
    saved = store.get_rescrape_session(session["id"])
    assert saved["status"] == "applying"
    assert saved["final_values"]["title"] == "人工编辑标题"


def test_polling_active_reports_normalize_actress_background_type(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "state.db")
    monkeypatch.setattr(web_server, "get_task_store", lambda: store)
    with web_server._background_refresh_lock:
        web_server._background_refresh_task = {"type": "normalize-actress", "started_at": web_server.time.time()}

    try:
        client = web_server.app.test_client()
        res = client.get("/api/polling_active")
    finally:
        with web_server._background_refresh_lock:
            web_server._background_refresh_task = None

    assert res.status_code == 200
    body = res.get_json()
    assert body["refresh_type"] == "normalize-actress"
    assert body["refresh_active"] is True
    assert body["refresh_starting"] is True


# ──────────────────────── /api/cover 路径穿越防护 ────────────────────────


class TestServeCover:
    """三重防线：realpath 解析、commonpath 边界检查、扩展名白名单。"""

    def test_blocks_path_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        client = web_server.app.test_client()

        # 创建目录外的恶意文件
        outside = tmp_path.parent / "evil.jpg"
        outside.write_bytes(b"evil")
        rel = os.path.relpath(str(outside), str(tmp_path))

        res = client.get(f"/api/cover?path={rel}")
        assert res.status_code == 404

    def test_blocks_absolute_path_outside_scan_root(self, tmp_path, monkeypatch):
        scan = tmp_path / "library"
        scan.mkdir()
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(scan))

        # 在扫描根外创建文件
        outside = tmp_path / "evil.jpg"
        outside.write_bytes(b"evil")

        client = web_server.app.test_client()
        res = client.get(f"/api/cover?path={outside}")
        assert res.status_code == 404

    def test_blocks_non_image_extension(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        txt = tmp_path / "secret.txt"
        txt.write_text("secret")

        client = web_server.app.test_client()
        res = client.get(f"/api/cover?path={txt}")
        assert res.status_code == 404

    def test_serves_valid_image(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        img = tmp_path / "poster.jpg"
        img.write_bytes(b"\xff\xd8\xff")  # JPEG header

        client = web_server.app.test_client()
        res = client.get(f"/api/cover?path={img}")
        assert res.status_code == 200

    def test_returns_404_for_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        client = web_server.app.test_client()
        res = client.get("/api/cover?path=/nonexistent/poster.jpg")
        assert res.status_code == 404

    def test_returns_404_for_empty_path(self, monkeypatch):
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: "/video")
        client = web_server.app.test_client()
        res = client.get("/api/cover")
        assert res.status_code == 404


# ──────────────────────── P1: MovieScrapeStatus ────────────────────────


class TestMovieScrapeStatus:
    """状态机：pending→scraping→success/failed，爬虫状态、计数器。"""

    def test_initial_state(self):
        s = web_server.MovieScrapeStatus()
        assert s.total == 0
        assert s.completed == 0
        assert s.failed == 0
        assert s.movies == {}

    def test_reset(self):
        s = web_server.MovieScrapeStatus()
        s.total = 10
        s.completed = 5
        s.movies["X"] = {"title": "x"}
        s.reset()
        assert s.total == 0
        assert s.movies == {}

    def test_set_total(self):
        s = web_server.MovieScrapeStatus()
        s.set_total(42)
        assert s.total == 42

    def test_start_movie_creates_entry(self):
        s = web_server.MovieScrapeStatus()
        s.start_movie("ABC-123", "123", crawler_list=["javdb", "javbus"])
        assert "ABC-123" in s.movies
        m = s.movies["ABC-123"]
        assert m["status"] == "scraping"
        assert m["num"] == "123"
        assert "javdb" in m["crawlers"]
        assert "javbus" in m["crawlers"]
        assert m["crawlers"]["javdb"]["status"] == "pending"

    def test_add_crawler_result(self):
        s = web_server.MovieScrapeStatus()
        s.start_movie("X", "x", crawler_list=["javdb"])
        s.add_crawler_result("javdb", ["title", "plot"])
        c = s.movies["X"]["crawlers"]["javdb"]
        assert c["status"] == "success"
        assert c["fields"] == ["title", "plot"]

    def test_mark_crawler_failed(self):
        s = web_server.MovieScrapeStatus()
        s.start_movie("X", "x", crawler_list=["javdb"])
        s.mark_crawler_failed("javdb")
        assert s.movies["X"]["crawlers"]["javdb"]["status"] == "failed"

    def test_mark_crawler_skipped(self):
        s = web_server.MovieScrapeStatus()
        s.start_movie("X", "x", crawler_list=["javdb"])
        s.mark_crawler_skipped("javdb")
        assert s.movies["X"]["crawlers"]["javdb"]["status"] == "skipped"

    def test_finish_movie_success(self):
        s = web_server.MovieScrapeStatus()
        s.set_total(1)
        s.start_movie("X", "x", crawler_list=["javdb", "other"])
        s.add_crawler_result("javdb", ["title"])
        s.finish_movie(success=True, save_dir="/tmp/x")
        assert s.movies["X"]["status"] == "success"
        assert s.movies["X"]["save_dir"] == "/tmp/x"
        assert s.completed == 1
        assert s.failed == 0
        # 未完成的爬虫标记为 skipped
        assert s.movies["X"]["crawlers"]["other"]["status"] == "skipped"

    def test_finish_movie_failed(self):
        s = web_server.MovieScrapeStatus()
        s.set_total(1)
        s.start_movie("X", "x", crawler_list=["javdb"])
        s.finish_movie(success=False)
        assert s.movies["X"]["status"] == "failed"
        assert s.failed == 1
        assert s.completed == 0
        # 未完成爬虫标记为 failed
        assert s.movies["X"]["crawlers"]["javdb"]["status"] == "failed"

    def test_set_movie_title(self):
        s = web_server.MovieScrapeStatus()
        s.start_movie("X", "x")
        s.set_movie_title("美丽出道")
        assert s.movies["X"]["title"] == "美丽出道"

    def test_get_summary(self):
        s = web_server.MovieScrapeStatus()
        s.set_total(10)
        s.completed = 7
        s.failed = 2
        summary = s.get_summary()
        assert summary["total"] == 10
        assert summary["completed"] == 7
        assert summary["failed"] == 2
        assert isinstance(summary["movies"], dict)


# ──────────────────────── P1: /api/trigger-scrape ────────────────────────


class TestTriggerScrape:
    """Webhook 端点：JSON/form/query 三种传参、move_files 布尔解析。"""

    @pytest.fixture(autouse=True)
    def _release_lock(self):
        yield
        # 等待 daemon 线程完成，避免 monkeypatch 提前失效
        import time
        time.sleep(0.1)
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass

    def _setup(self, monkeypatch, tmp_path, locked=False):
        """设置 trigger-scrape 需要的 mock。"""
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)
        captured = {}

        def fake_run(command, extra_args=None):
            captured["cmd"] = command
            captured["args"] = extra_args
            return True, "started"

        monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
        # 确保锁释放
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass
        if locked:
            assert web_server.process_lock.acquire(blocking=False)
        # 让 daemon 线程在 monkeypatch 有效时执行
        import threading
        self._daemon_done = threading.Event()
        return captured

    def test_json_params(self, tmp_path, monkeypatch):
        captured = self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        res = client.post("/api/trigger-scrape", json={
            "target_dir": str(tmp_path / "movies"),
            "move_files": "true",
        })
        assert res.status_code == 202
        body = res.get_json()
        assert body["status"] == "accepted"
        assert body["move_files"] is True

    def test_form_params(self, tmp_path, monkeypatch):
        captured = self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        res = client.post("/api/trigger-scrape", data={
            "target_dir": str(tmp_path / "movies"),
            "move_files": "1",
        })
        assert res.status_code == 202

    def test_query_params(self, tmp_path, monkeypatch):
        captured = self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        res = client.post(f"/api/trigger-scrape?target_dir={tmp_path}&move_files=yes")
        assert res.status_code == 202

    def test_move_files_boolean_parsing(self, tmp_path, monkeypatch):
        for val, expect in [("true", True), ("1", True), ("yes", True),
                            ("false", False), ("0", False), ("no", False),
                            ("True", True), ("YES", True)]:
            captured = self._setup(monkeypatch, tmp_path)
            client = web_server.app.test_client()
            res = client.post("/api/trigger-scrape", json={"move_files": val})
            assert res.get_json()["move_files"] == expect, f"move_files={val!r}"

    def test_no_params_defaults(self, tmp_path, monkeypatch):
        captured = self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        res = client.post("/api/trigger-scrape", json={})
        assert res.status_code == 202
        body = res.get_json()
        assert "(使用默认配置)" in body["move_files"] or body["move_files"] is None

    def test_returns_queued_when_locked(self, tmp_path, monkeypatch):
        """锁被占用时不拒绝，返回 queued 状态让文件入队等待。"""
        captured = self._setup(monkeypatch, tmp_path, locked=True)
        client = web_server.app.test_client()
        res = client.post("/api/trigger-scrape", json={
            "target_file": str(tmp_path / "video.mp4"),
        })
        assert res.status_code == 202
        assert res.get_json()["status"] == "queued"
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass

    def test_concurrent_webhooks_not_rejected(self, tmp_path, monkeypatch):
        """并发 webhook 都应返回 202，不会出现 409 拒绝。"""
        self._setup(monkeypatch, tmp_path, locked=True)
        client = web_server.app.test_client()
        for i in range(3):
            res = client.post("/api/trigger-scrape", json={
                "target_file": str(tmp_path / f"video_{i}.mp4"),
            })
            assert res.status_code == 202
            assert res.get_json()["status"] in ("accepted", "queued")
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass


# ──────────────────────── P2: /api/tasks 过滤分页 ────────────────────────


class TestTasksApi:
    """任务列表：状态过滤、分页、排序、导出。"""

    def _seed_tasks(self, store, tmp_path, count=3):
        """创建多个不同状态的任务。"""
        run_id = store.start_run("test")
        task_ids = []
        for i in range(count):
            from javsp.datatype import Movie
            video = tmp_path / f"MOV-{i:03d}.mp4"
            video.write_bytes(b"0" * (200 * 1024 * 1024))
            movie = Movie(f"ABC-{i:03d}")
            movie.files = [str(video)]
            movie.save_dir = str(tmp_path)
            status = "pending" if i == 0 else ("success" if i == 1 else "failed")
            _, tid = store.enqueue_movie(movie, run_id, status)
            if status == "success":
                from javsp.datatype import MovieInfo
                movie.info = MovieInfo(f"ABC-{i:03d}")
                movie.info.title = f"Title {i}"
                store.mark_success(tid, str(tmp_path), movie)
            elif status == "failed":
                store.mark_failure(tid, "crawl", "error", retryable=False)
            task_ids.append(tid)
        store.finish_run(run_id)
        return task_ids

    def test_list_all(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        self._seed_tasks(store, tmp_path)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)

        client = web_server.app.test_client()
        res = client.get("/api/tasks")
        assert res.status_code == 200
        body = res.get_json()
        assert body["total"] == 3
        assert len(body["tasks"]) == 3

    def test_filter_by_status(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        self._seed_tasks(store, tmp_path)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)

        client = web_server.app.test_client()
        res = client.get("/api/tasks?status=pending")
        body = res.get_json()
        assert body["total"] == 1
        assert body["tasks"][0]["status"] == "pending"

    def test_filter_by_last_run(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        self._seed_tasks(store, tmp_path)
        latest_run = store.get_latest_run()
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        body = client.get(f"/api/tasks?last_run_id={latest_run['id']}").get_json()
        assert body["total"] == 3
        assert all(task["last_run_id"] == latest_run["id"] for task in body["tasks"])

    def test_filter_by_updated_after(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        task_ids = self._seed_tasks(store, tmp_path)
        with store.connect() as conn:
            conn.execute("UPDATE tasks SET updated_at = 100 WHERE id = ?", (task_ids[0],))
            conn.execute("UPDATE tasks SET updated_at = 200 WHERE id = ?", (task_ids[1],))
            conn.execute("UPDATE tasks SET updated_at = 300 WHERE id = ?", (task_ids[2],))
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        body = client.get("/api/tasks?updated_after=200").get_json()
        assert body["total"] == 2
        assert {task["id"] for task in body["tasks"]} == {task_ids[1], task_ids[2]}

    def test_include_results_uses_bulk_query(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        self._seed_tasks(store, tmp_path)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)
        monkeypatch.setattr(
            store,
            "list_scrape_results",
            lambda *_: (_ for _ in ()).throw(AssertionError("不应逐任务查询")),
        )

        client = web_server.app.test_client()
        body = client.get("/api/tasks?include_results=1").get_json()
        assert len(body["tasks"]) == 3
        assert all("crawler_results" in task for task in body["tasks"])

    def test_pagination(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        self._seed_tasks(store, tmp_path)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)

        client = web_server.app.test_client()
        res = client.get("/api/tasks?limit=1&offset=1")
        body = res.get_json()
        assert len(body["tasks"]) <= 1

    def test_export_csv(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        self._seed_tasks(store, tmp_path)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        res = client.get("/api/tasks/export")
        assert res.status_code == 200
        assert "text/csv" in res.content_type
        assert "ID,番号,状态" in res.get_data(as_text=True)


# ──────────────────────── P2: /api/general_config ────────────────────────


class TestConfigApi:
    """配置读写：YAML 保存、metadata_complete 变更检测。"""

    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        store = TaskStore(tmp_path / "state.db")
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)
        # 保存原始配置文件内容，测试结束后恢复
        self._restore_config(monkeypatch, tmp_path)
        return store

    def _restore_config(self, monkeypatch, tmp_path):
        """备份真实的 config.yml，测试结束后恢复，避免污染后续测试。"""
        import shutil
        try:
            if os.path.exists(web_server.GENERAL_CONFIG_FILE):
                backup = tmp_path / "config_backup.yml"
                shutil.copy(web_server.GENERAL_CONFIG_FILE, backup)
                # 重定向 GENERAL_CONFIG_FILE 到临时路径
                temp_config = tmp_path / "config_test.yml"
                shutil.copy(web_server.GENERAL_CONFIG_FILE, temp_config)
                monkeypatch.setattr(web_server, "GENERAL_CONFIG_FILE", str(temp_config))
        except Exception:
            pass

    def test_get_config(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        res = client.get("/api/general_config")
        assert res.status_code == 200
        assert "content" in res.get_json()

    def test_post_valid_yaml_does_not_crash(self, tmp_path, monkeypatch):
        store = self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        cfg = (
            "scanner:\n  input_directory: /video\n  minimum_size: 100MiB\n"
            "daemon:\n  download_stable_seconds: 300\n"
        )
        res = client.post("/api/general_config", json={"content": cfg})
        assert res.status_code == 200

    def test_post_invalid_yaml_returns_400(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        client = web_server.app.test_client()
        res = client.post("/api/general_config", json={"content": "\tbad: : damage"})
        assert res.status_code == 400
        assert "无效" in res.get_json()["message"]


# ──────────────────────── P2: /api/crawlers 字段优先级 ────────────────────────


class TestCrawlerConfigApi:
    """爬虫配置：字段优先级兼容旧配置，并支持剧照。"""

    def test_get_crawlers_defaults_preview_pics_for_old_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            """
crawler:
  selection:
    normal: [javdb]
    fc2: []
    cid: []
    getchu: []
    gyutto: []
  field_priorities:
    title: [javdb]
    plot: []
    actress: []
""".lstrip(),
            encoding="utf-8",
        )
        monkeypatch.setattr(web_server, "GENERAL_CONFIG_FILE", str(config_file))
        client = web_server.app.test_client()

        res = client.get("/api/crawlers")

        assert res.status_code == 200
        body = res.get_json()
        assert set(body["selection"]) == {"normal", "fc2", "cid"}
        assert "dl_getchu" not in body["available"]
        assert "gyutto" not in body["available"]
        assert body["field_priorities"]["title"] == ["javdb"]
        assert body["field_priorities"]["preview_pics"] == []

    def test_post_crawlers_accepts_preview_pics_priority(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            """
crawler:
  selection:
    normal: [javdb, javbus]
    fc2: []
    cid: []
    getchu: []
    gyutto: []
  field_priorities:
    title: []
    plot: []
    actress: []
""".lstrip(),
            encoding="utf-8",
        )
        monkeypatch.setattr(web_server, "GENERAL_CONFIG_FILE", str(config_file))
        client = web_server.app.test_client()

        res = client.post(
            "/api/crawlers",
            json={
                "selection": {"normal": ["javdb", "javbus"], "fc2": [], "cid": [], "getchu": [], "gyutto": []},
                "field_priorities": {"title": ["javdb"], "plot": [], "actress": [], "preview_pics": ["javbus"]},
            },
        )

        assert res.status_code == 200
        saved = config_file.read_text(encoding="utf-8")
        assert "preview_pics: [javbus]" in saved
        assert "getchu:" not in saved
        assert "gyutto:" not in saved


# ──────────────────────── P2: /api/tasks batch operations ────────────────────────


class TestBatchOperations:
    """批量删除、批量重试。"""

    def test_batch_delete_by_status(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        from test_web_api import TestTasksApi
        t = TestTasksApi()
        t._seed_tasks(store, tmp_path, 3)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        res = client.post("/api/tasks/batch-delete", json={"status": "failed"})
        assert res.status_code == 200
        assert "已删除" in res.get_json()["message"]

    def test_batch_delete_by_ids(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        from test_web_api import TestTasksApi
        t = TestTasksApi()
        tids = t._seed_tasks(store, tmp_path, 2)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        res = client.post("/api/tasks/batch-delete", json={"task_ids": tids})
        assert res.status_code == 200

    def test_batch_delete_no_params(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        res = client.post("/api/tasks/batch-delete", json={})
        assert res.status_code == 400

    def test_batch_retry(self, tmp_path, monkeypatch):
        store = TaskStore(tmp_path / "state.db")
        from test_web_api import TestTasksApi
        t = TestTasksApi()
        tids = t._seed_tasks(store, tmp_path, 2)
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)

        client = web_server.app.test_client()
        res = client.post("/api/tasks/batch-retry", json={"task_ids": tids})
        assert res.status_code == 200
        assert "重试" in res.get_json()["message"]


# ──────────────── 修复 B: 重试后自动启动 Worker ────────────────


class TestRetryAutoStart:
    """修复 B: 重试成功后如有 pending 任务且无活跃任务则自动启动 Worker。"""

    def _setup(self, monkeypatch, tmp_path, with_pending=False, locked=False):
        store = TaskStore(tmp_path / "state.db")
        monkeypatch.setattr(web_server, "get_task_store", lambda: store)
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)

        task_id = None
        if with_pending:
            from javsp.datatype import Movie
            movie = Movie("ABC-123")
            video = tmp_path / "ABC-123.mp4"
            video.write_bytes(b"0" * 1024)
            movie.files = [str(video)]
            run = store.start_run("test")
            _, task_id = store.enqueue_movie(movie, run, TASK_DEFERRED, "等待下载")

        captured = {}
        original_run = web_server._run_javsp_background

        def fake_run(command, extra_args=None):
            captured["command"] = command
            captured["extra_args"] = extra_args
            return True, "started"

        monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
        # 确保锁状态
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass
        if locked:
            assert web_server.process_lock.acquire(blocking=False)
        return store, task_id, captured

    def test_retry_starts_worker_when_pending_exists(self, tmp_path, monkeypatch):
        store, task_id, captured = self._setup(monkeypatch, tmp_path, with_pending=True)
        client = web_server.app.test_client()
        res = client.post(f"/api/tasks/{task_id}/retry")
        assert res.status_code == 200
        body = res.get_json()
        assert body["worker_started"] is True
        assert captured["command"] == "run-once"

    def test_retry_skips_worker_when_lock_held(self, tmp_path, monkeypatch):
        store, task_id, captured = self._setup(monkeypatch, tmp_path, with_pending=True, locked=True)
        client = web_server.app.test_client()
        res = client.post(f"/api/tasks/{task_id}/retry")
        assert res.status_code == 200
        body = res.get_json()
        assert body["worker_started"] is False
        assert "command" not in captured
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass

    def test_batch_retry_starts_worker(self, tmp_path, monkeypatch):
        store, task_id, captured = self._setup(monkeypatch, tmp_path, with_pending=True)
        client = web_server.app.test_client()
        res = client.post("/api/tasks/batch-retry", json={"task_ids": [task_id]})
        assert res.status_code == 200
        assert captured["command"] == "run-once"


# ──────────────── 修复 C: trigger-scrape target_file ────────────────


class TestTriggerScrapeTargetFile:
    """修复 C: trigger-scrape 支持单个文件刮削。"""

    def test_passes_target_file_to_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
        monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)
        captured = {}

        def fake_run(command, extra_args=None):
            captured["command"] = command
            captured["extra_args"] = extra_args
            return True, "started"

        monkeypatch.setattr(web_server, "_run_javsp_background", fake_run)
        try:
            web_server.process_lock.release()
        except RuntimeError:
            pass

        client = web_server.app.test_client()
        target = str(tmp_path / "myvideo.mp4")
        res = client.post("/api/trigger-scrape", json={
            "target_file": target,
            "target_dir": str(tmp_path),
        })
        assert res.status_code == 202
        body = res.get_json()
        assert body["target_file"] == target


def test_scheduler_config_created_from_template(tmp_path, monkeypatch):
    template = tmp_path / "web_config.example.yml"
    template.write_text("scheduler:\n  enabled: false\n", encoding="utf-8")
    target = tmp_path / "web_config.yml"
    monkeypatch.setattr(web_server, "_current_dir", str(tmp_path))
    monkeypatch.setattr(web_server, "SCHEDULER_CONFIG_FILE", str(target))
    web_server.ensure_scheduler_config()
    assert target.read_bytes() == template.read_bytes()
    target.write_text("personal settings", encoding="utf-8")
    web_server.ensure_scheduler_config()
    assert target.read_text(encoding="utf-8") == "personal settings"
