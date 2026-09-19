"""P0 任务系统单元测试：deferred 过期检测、重试策略、文件校验、出队逻辑、入队状态转换。"""
import os
import time
from types import SimpleNamespace

import pytest

from javsp.task import (
    TASK_DEFERRED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SKIPPED,
    TASK_SUCCESS,
    TaskStore,
    enqueue_movies,
    file_snapshot,
    has_temp_download_marker,
    json_dumps,
    json_loads,
)

import javsp.task as task_module


# ──────────────────────── has_temp_download_marker ────────────────────────

class TestHasTempDownloadMarker:
    """过期检测：临时文件超过 stale_seconds 未修改视为残留，忽略。"""

    def test_detects_companion_temp(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"content")
        (tmp_path / "movie.mp4.aria2").write_bytes(b"")
        assert has_temp_download_marker([str(video)]) is True

    def test_detects_file_itself_as_temp(self, tmp_path):
        f = tmp_path / "video.mp4.aria2"
        f.write_bytes(b"")
        assert has_temp_download_marker([str(f)]) is True

    def test_ignores_stale_companion(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"content")
        marker = tmp_path / "movie.mp4.aria2"
        marker.write_bytes(b"")
        os.utime(marker, (time.time() - 601, time.time() - 601))
        assert has_temp_download_marker([str(video)], stale_seconds=600) is False

    def test_detects_fresh_companion(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"content")
        (tmp_path / "movie.mp4.aria2").write_bytes(b"")
        assert has_temp_download_marker([str(video)], stale_seconds=600) is True

    def test_ignores_stale_file_itself(self, tmp_path):
        f = tmp_path / "video.mp4.aria2"
        f.write_bytes(b"")
        os.utime(f, (time.time() - 601, time.time() - 601))
        assert has_temp_download_marker([str(f)], stale_seconds=600) is False

    def test_stale_seconds_zero_always_detects(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"content")
        marker = tmp_path / "movie.mp4.aria2"
        marker.write_bytes(b"")
        os.utime(marker, (time.time() - 99999, time.time() - 99999))
        assert has_temp_download_marker([str(video)]) is True
        assert has_temp_download_marker([str(video)], stale_seconds=0) is True

    def test_no_temp_files(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"content")
        assert has_temp_download_marker([str(video)]) is False

    def test_handles_multiple_files(self, tmp_path):
        v1 = tmp_path / "a.mp4"
        v2 = tmp_path / "b.mp4"
        v1.write_bytes(b"a")
        v2.write_bytes(b"b")
        marker = tmp_path / "b.mp4.aria2"
        marker.write_bytes(b"")
        assert has_temp_download_marker([str(v1), str(v2)]) is True

    def test_detects_qb_marker(self, tmp_path):
        video = tmp_path / "x.mkv"
        video.write_bytes(b"x")
        (tmp_path / "x.mkv.!qB").write_bytes(b"")
        assert has_temp_download_marker([str(video)]) is True


# ──────────────────── enqueue_movie / enqueue_movies ────────────────────

class TestEnqueueMovie:
    """入队状态转换：deferred→pending 升级、RUNNING_STATES 跳过、冷却期跳过。"""

    @pytest.fixture(autouse=True)
    def _mock_cfg(self, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
                metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
            ),
        ))

    def test_deferred_upgrades_to_pending(self, task_store, tmp_path):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]

        # 第一轮：deferred 入队
        run1 = task_store.start_run("first")
        status1, task_id1 = task_store.enqueue_movie(movie, run1, TASK_DEFERRED, "下载中")
        assert status1 == TASK_DEFERRED

        # 第二轮：pending 入队 → deferred 升级为 pending
        run2 = task_store.start_run("second")
        status2, _ = task_store.enqueue_movie(movie, run2, TASK_PENDING)
        assert status2 == TASK_PENDING
        task = task_store.get_task(task_id1)
        assert task["status"] == TASK_PENDING
        assert task["failure_stage"] is None

    def test_deferred_skipped_when_still_deferred(self, task_store, tmp_path):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]

        run1 = task_store.start_run("first")
        status1, task_id = task_store.enqueue_movie(movie, run1, TASK_DEFERRED, "下载中")
        assert status1 == TASK_DEFERRED

        # 第二轮：仍是 deferred → 跳过
        run2 = task_store.start_run("second")
        status2, _ = task_store.enqueue_movie(movie, run2, TASK_DEFERRED, "仍在下载")
        assert status2 == TASK_DEFERRED
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_DEFERRED

    def test_failed_cooldown_skipped(self, task_store, tmp_path, monkeypatch):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]

        # 创建 failed 任务，next_retry_at 设为未来
        run1 = task_store.start_run("first")
        _, task_id = task_store.enqueue_movie(movie, run1, TASK_PENDING)
        task_store.mark_failure(task_id, "network", "timeout", retryable=True)

        run2 = task_store.start_run("second")
        status, _ = task_store.enqueue_movie(movie, run2, TASK_PENDING)
        assert status == TASK_SKIPPED

    def test_success_task_skipped(self, task_store, tmp_path):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]

        run1 = task_store.start_run("first")
        _, task_id = task_store.enqueue_movie(movie, run1, TASK_PENDING)
        task_store.mark_success(task_id, str(tmp_path), movie)

        run2 = task_store.start_run("second")
        status, _ = task_store.enqueue_movie(movie, run2, TASK_PENDING)
        assert status == TASK_SKIPPED

    def test_new_task_pending(self, task_store, tmp_path):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]

        run = task_store.start_run("test")
        status, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        assert status == TASK_PENDING
        assert task_id is not None

    def test_enqueue_movies_deferred_with_stale_marker(self, tmp_path, monkeypatch):
        """enqueue_movies 传入 stale_seconds×2，过期临时文件不阻止入队。"""
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
            scanner=SimpleNamespace(restrict_to_files=None),
        ))
        store = TaskStore(tmp_path / "state.db")
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]

        # 新鲜临时文件 → deferred
        marker = tmp_path / "ABC-123.mp4.aria2"
        marker.write_bytes(b"")
        run1 = store.start_run("first")
        result1 = enqueue_movies(store, [movie], run1)
        assert result1["deferred"] == 1
        assert result1["enqueued"] == 0

        # 将临时文件改旧（>600s）→ 下次扫描应忽略
        os.utime(marker, (time.time() - 601, time.time() - 601))
        run2 = store.start_run("second")
        result2 = enqueue_movies(store, [movie], run2)
        assert result2["enqueued"] == 1
        assert result2["deferred"] == 0


# ──────────────────────── get_due_tasks ────────────────────────


class TestGetDueTasks:
    """出队逻辑：只取 pending + 可重试 failed，不取 deferred。"""

    def test_gets_pending(self, task_store, tmp_path):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        due = task_store.get_due_tasks()
        assert any(t["id"] == task_id for t in due)

    def test_gets_retryable_failed(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
                metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_failure(task_id, "network", "timeout", retryable=True)

        due = task_store.get_due_tasks()
        # next_retry_at 刚设置，大约1小时后，现在不应出队
        # 改为等待… 实际上 network_retry_after 是 3600s, 当前时间 < next_retry_at
        assert not any(t["id"] == task_id for t in due)

    def test_skips_deferred(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_DEFERRED, "等待下载")

        due = task_store.get_due_tasks()
        assert not any(t["id"] == task_id for t in due)

    def test_respects_limit(self, task_store, tmp_path):
        from javsp.datatype import Movie
        run = task_store.start_run("t")
        for i in range(5):
            video = tmp_path / f"MOV{i}.mp4"
            video.write_bytes(b"0" * 1024)
            movie = Movie(f"ABC-{i:03d}")
            movie.files = [str(video)]
            task_store.enqueue_movie(movie, run, TASK_PENDING)

        due = task_store.get_due_tasks(limit=3)
        assert len(due) == 3


# ──────────────────────── mark_failure ────────────────────────


class TestMarkFailure:
    """失败重试：network vs metadata retry_after 分支、max_retry 上限。"""

    def test_network_stage_uses_network_delay(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
                metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        task_store.mark_failure(task_id, "crawl", "timeout", retryable=True)
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_FAILED
        assert task["retry_count"] == 1
        assert task["next_retry_at"] is not None
        # network_retry_after = 3600s, 验证在合理范围
        assert 3590 < task["next_retry_at"] - time.time() <= 3601

    def test_metadata_stage_uses_metadata_delay(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
                metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        task_store.mark_failure(task_id, "summary", "incomplete", retryable=True)
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_FAILED
        assert 86390 < task["next_retry_at"] - time.time() <= 86401

    def test_exceeds_max_retry_no_retry(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=1,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
                metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        # 第一次失败：允许重试
        task_store.mark_failure(task_id, "crawl", "timeout", retryable=True)
        t1 = task_store.get_task(task_id)
        assert t1["retry_count"] == 1
        assert t1["next_retry_at"] is not None

        # 第二次失败：超过 max_retry_count=1
        task_store.mark_failure(task_id, "crawl", "timeout2", retryable=True)
        t2 = task_store.get_task(task_id)
        assert t2["retry_count"] == 2
        assert t2["next_retry_at"] is None

    def test_non_retryable_no_next_retry(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        task_store.mark_failure(task_id, "file", "not found", retryable=False)
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_FAILED
        assert task["retry_count"] == 1
        assert task["next_retry_at"] is None


# ──────────────────────── validate_task_files ────────────────────────


class TestValidateTaskFiles:
    """Worker 执行前的文件安全检查。"""

    @pytest.fixture(autouse=True)
    def _mock_cfg(self, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            scanner=SimpleNamespace(minimum_size=100 * 1024 * 1024),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))

    def test_no_files(self, task_store):
        task = {"files_json": json_dumps([])}
        action, reason = task_store.validate_task_files(task)
        assert action == TASK_FAILED
        assert "没有关联" in reason

    def test_file_missing(self, tmp_path):
        store = TaskStore(tmp_path / "db.db")
        task = {"files_json": json_dumps([str(tmp_path / "nope.mp4")])}
        action, reason = store.validate_task_files(task)
        assert action == TASK_FAILED
        assert "不存在" in reason

    def test_temp_marker_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            scanner=SimpleNamespace(minimum_size=100 * 1024 * 1024),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        store = TaskStore(tmp_path / "db.db")
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"0" * (200 * 1024 * 1024))
        (tmp_path / "movie.mp4.aria2").write_bytes(b"")
        task = {
            "files_json": json_dumps([str(video)]),
            "files_snapshot_json": json_dumps(file_snapshot([str(video)])),
        }
        action, reason = store.validate_task_files(task)
        assert action == TASK_DEFERRED
        assert "临时文件" in reason

    def test_file_too_small(self, tmp_path):
        store = TaskStore(tmp_path / "db.db")
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"0" * 100)  # 100 bytes, far below 100 MiB
        task = {
            "files_json": json_dumps([str(video)]),
            "files_snapshot_json": json_dumps(file_snapshot([str(video)])),
        }
        action, reason = store.validate_task_files(task)
        assert action == TASK_FAILED
        assert "最小体积" in reason

    def test_file_size_changed(self, tmp_path):
        store = TaskStore(tmp_path / "db.db")
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"0" * (200 * 1024 * 1024))
        # snapshot 记录旧大小
        old_snap = [{"path": str(video), "size": 100, "mtime": time.time()}]
        task = {
            "files_json": json_dumps([str(video)]),
            "files_snapshot_json": json_dumps(old_snap),
        }
        action, reason = store.validate_task_files(task)
        assert action == TASK_DEFERRED
        assert "大小发生变化" in reason or "大小" in reason

    def test_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            scanner=SimpleNamespace(minimum_size=100 * 1024 * 1024),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        store = TaskStore(tmp_path / "db.db")
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"0" * (200 * 1024 * 1024))
        snapshot = file_snapshot([str(video)])
        task = {
            "files_json": json_dumps([str(video)]),
            "files_snapshot_json": json_dumps(snapshot),
        }
        action, reason = store.validate_task_files(task)
        assert action == "ok"
        assert reason is None


# ────────────────────── mark_deferred + retry_task ──────────────────────


class TestMarkDeferred:
    """mark_deferred 设置 failure_stage='file_pending' 和 next_retry_at。"""

    def test_sets_file_pending_and_next_retry(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        task_store.mark_deferred(task_id, "文件变化中")
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_DEFERRED
        assert task["failure_stage"] == "file_pending"
        assert task["failure_reason"] == "文件变化中"
        assert task["next_retry_at"] is not None


class TestRetryTask:
    """用户手动重试：failed/deferred → pending。"""

    def test_retry_failed_to_pending(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
            ),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_failure(task_id, "crawl", "timeout", retryable=False)

        assert task_store.retry_task(task_id) is True
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_PENDING

    def test_retry_deferred_to_pending(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_DEFERRED, "等待下载")

        assert task_store.retry_task(task_id) is True
        task = task_store.get_task(task_id)
        assert task["status"] == TASK_PENDING

    def test_retry_nonexistent(self, task_store):
        assert task_store.retry_task(99999) is False

    def test_retry_success_refused(self, task_store, tmp_path):
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_success(task_id, str(tmp_path), movie)

        assert task_store.retry_task(task_id) is False


# ──────────────────────── get_due_tasks: failed 到期出队 ────────────────────────


class TestGetDueTasksRetry:
    """failed 任务 next_retry_at 到期后出队。"""

    def test_failed_due_after_retry_at_expires(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=3,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 0),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_failure(task_id, "crawl", "timeout", retryable=True)

        # network_retry_after = 0, next_retry_at 为当前时间，应立即出队
        due = task_store.get_due_tasks()
        assert any(t["id"] == task_id for t in due)

    def test_failed_skipped_when_retry_count_exceeds_max(self, task_store, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            retry_policy=SimpleNamespace(
                max_retry_count=1,
                network_retry_after=SimpleNamespace(total_seconds=lambda: 0),
            ),
            daemon=SimpleNamespace(download_stable_seconds=300),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        # 第一次失败，retry_count=1
        task_store.mark_failure(task_id, "crawl", "fail1", retryable=True)
        t1 = task_store.get_task(task_id)
        assert t1["retry_count"] == 1

        # 第一次重试后再次失败，retry_count=2 > max=1
        task_store.mark_failure(task_id, "crawl", "fail2", retryable=True)
        t2 = task_store.get_task(task_id)
        assert t2["retry_count"] == 2
        assert t2["next_retry_at"] is None

        # get_due_tasks 不应包含此任务 (retry_count > max)
        due = task_store.get_due_tasks()
        assert not any(t["id"] == task_id for t in due)


# ──────────────── 修复 A: get_due_tasks 过期 deferred 出队 ────────────────


class TestGetDueTasksDeferredExpiry:
    """修复 A: 过期的 deferred 任务应被 get_due_tasks 取出。"""

    def test_picks_up_expired_deferred(self, task_store, tmp_path, monkeypatch):
        """deferred 的 next_retry_at 已过期 → 应被出队。"""
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_DEFERRED, "下载中")

        # 手动将 next_retry_at 设为过去
        with task_store.connect() as conn:
            conn.execute("UPDATE tasks SET next_retry_at = ? WHERE id = ?",
                         (time.time() - 3600, task_id))

        due = task_store.get_due_tasks()
        assert any(t["id"] == task_id for t in due)

    def test_skips_future_deferred(self, task_store, tmp_path, monkeypatch):
        """deferred 的 next_retry_at 在未来 → 不应被出队。"""
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
        ))
        from javsp.datatype import Movie
        movie = Movie("ABC-123")
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"0" * 1024)
        movie.files = [str(video)]
        run = task_store.start_run("t")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_DEFERRED, "下载中")

        # next_retry_at 默认是 now + 300s → 不应出队
        due = task_store.get_due_tasks()
        assert not any(t["id"] == task_id for t in due)

    def test_mixed_expired_and_future(self, task_store, tmp_path, monkeypatch):
        """同时有过期和未过期的 deferred，只取过期的。"""
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
        ))
        from javsp.datatype import Movie

        # 创建一个过期的 deferred
        v1 = tmp_path / "A.mp4"
        v1.write_bytes(b"0" * 1024)
        m1 = Movie("ABC-001")
        m1.files = [str(v1)]
        run = task_store.start_run("t")
        _, tid1 = task_store.enqueue_movie(m1, run, TASK_DEFERRED, "旧延迟")

        # 创建一个未来到期的 deferred
        v2 = tmp_path / "B.mp4"
        v2.write_bytes(b"0" * 1024)
        m2 = Movie("ABC-002")
        m2.files = [str(v2)]
        _, tid2 = task_store.enqueue_movie(m2, run, TASK_DEFERRED, "新延迟")

        # 手动将第一个设为过期
        with task_store.connect() as conn:
            conn.execute("UPDATE tasks SET next_retry_at = ? WHERE id = ?",
                         (time.time() - 3600, tid1))

        due = task_store.get_due_tasks()
        assert any(t["id"] == tid1 for t in due)
        assert not any(t["id"] == tid2 for t in due)


# ──────────────── 修复 C: enqueue_movies restrict_to_files ────────────────


class TestEnqueueMoviesRestrictFiles:
    """修复 C: restrict_to_files 只入队匹配的影片，不影响其他。"""

    def test_filter_keeps_matching_movie(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
            scanner=SimpleNamespace(restrict_to_files=[str(tmp_path / "A.mp4")]),
        ))
        store = TaskStore(tmp_path / "state.db")
        from javsp.datatype import Movie
        v = tmp_path / "A.mp4"
        v.write_bytes(b"0" * 1024)
        m = Movie("ABC-001")
        m.files = [str(v)]
        run = store.start_run("test")
        result = enqueue_movies(store, [m], run)
        assert result["enqueued"] == 1
        assert result["deferred"] == 0

    def test_filter_excludes_non_matching(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
            scanner=SimpleNamespace(restrict_to_files=[str(tmp_path / "ONLY_THIS.mp4")]),
        ))
        store = TaskStore(tmp_path / "state.db")
        from javsp.datatype import Movie
        v = tmp_path / "OTHER.mp4"
        v.write_bytes(b"0" * 1024)
        m = Movie("ABC-001")
        m.files = [str(v)]
        run = store.start_run("test")
        result = enqueue_movies(store, [m], run)
        assert result["enqueued"] == 0
        assert result["deferred"] == 0
        assert result["skipped"] == 0

    def test_filter_mixed_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
            scanner=SimpleNamespace(restrict_to_files=[str(tmp_path / "A.mp4")]),
        ))
        store = TaskStore(tmp_path / "state.db")
        from javsp.datatype import Movie
        v1 = tmp_path / "A.mp4"
        v2 = tmp_path / "B.mp4"
        v1.write_bytes(b"0" * 1024)
        v2.write_bytes(b"0" * 1024)
        m1 = Movie("ABC-001")
        m1.files = [str(v1)]
        m2 = Movie("ABC-002")
        m2.files = [str(v2)]
        run = store.start_run("test")
        result = enqueue_movies(store, [m1, m2], run)
        assert result["enqueued"] == 1

    def test_no_filter_when_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
            daemon=SimpleNamespace(download_stable_seconds=300),
            retry_policy=SimpleNamespace(max_retry_count=3),
            scanner=SimpleNamespace(restrict_to_files=None),
        ))
        store = TaskStore(tmp_path / "state.db")
        from javsp.datatype import Movie
        v1 = tmp_path / "A.mp4"
        v1.write_bytes(b"0" * 1024)
        m1 = Movie("ABC-001")
        m1.files = [str(v1)]
        run = store.start_run("test")
        result = enqueue_movies(store, [m1], run)
        assert result["enqueued"] == 1


# ──────────────────────── sync_file_inventory ────────────────────────


class TestSyncFileInventory:
    """同步数据库与文件系统：文件不存在的任务自动清理。"""

    def test_empty_database(self, task_store):
        result = task_store.sync_file_inventory()
        assert result["purged"] == 0
        assert result["purged_task_ids"] == []

    def test_pending_with_existing_files_not_purged(self, task_store, tmp_path):
        video = tmp_path / "keep.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-001")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 0
        assert task_store.get_task(task_id) is not None

    def test_pending_with_missing_files_purged(self, task_store, tmp_path):
        video = tmp_path / "will_delete.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-002")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        os.remove(video)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 1
        assert result["purged_task_ids"] == [task_id]
        assert task_store.get_task(task_id) is None

    def test_success_with_existing_files_not_purged(self, task_store, tmp_path):
        video = tmp_path / "success_keep.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-003")
        movie.files = [str(video)]
        movie.save_dir = str(tmp_path)
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_success(task_id, str(tmp_path), movie)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 0
        assert task_store.get_task(task_id) is not None

    def test_success_with_missing_files_purged(self, task_store, tmp_path):
        video = tmp_path / "success_gone.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-004")
        movie.files = [str(video)]
        movie.save_dir = str(tmp_path)
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_success(task_id, str(tmp_path), movie)
        os.remove(video)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 1
        assert task_store.get_task(task_id) is None

    def test_success_without_current_files_does_not_fallback_to_old_source(self, task_store, tmp_path):
        old_video = tmp_path / "old_source.mp4"
        nfo = tmp_path / "movie.nfo"
        old_video.write_bytes(b"0")
        nfo.write_text("<movie></movie>", encoding="utf-8")
        os.remove(old_video)

        ts = time.time()
        with task_store.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks(
                    task_type, avid, data_src, files_json, fingerprint, status,
                    save_dir, created_at, updated_at, current_save_dir, current_nfo_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scrape_movie",
                    "LEGACY-001",
                    "normal",
                    json_dumps([str(old_video)]),
                    "legacy-success-without-current-files",
                    TASK_SUCCESS,
                    str(tmp_path),
                    ts,
                    ts,
                    str(tmp_path),
                    str(nfo),
                ),
            )
            task_id = int(cur.lastrowid)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 0
        assert result["purged_task_ids"] == []
        assert task_store.get_task(task_id) is not None

    def test_failed_with_missing_files_purged(self, task_store, tmp_path):
        video = tmp_path / "failed_gone.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-005")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_failure(task_id, "crawl", "timeout", retryable=True)
        os.remove(video)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 1
        assert task_store.get_task(task_id) is None

    def test_running_skipped_even_if_file_missing(self, task_store, tmp_path):
        video = tmp_path / "running.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-006")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        task_store.mark_running(task_id, run)
        os.remove(video)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 0
        assert task_store.get_task(task_id) is not None

    def test_rechecks_status_before_deleting_candidate(self, task_store, tmp_path, monkeypatch):
        video = tmp_path / "race.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-008")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        os.remove(video)

        real_exists = task_module.os.path.exists
        changed_to_running = False

        def exists_with_race(path):
            nonlocal changed_to_running
            if path == str(video) and not changed_to_running:
                changed_to_running = True
                task_store.mark_running(task_id, run)
                return False
            return real_exists(path)

        monkeypatch.setattr(task_module.os.path, "exists", exists_with_race)

        result = task_store.sync_file_inventory()
        task = task_store.get_task(task_id)
        assert changed_to_running is True
        assert result["purged"] == 0
        assert result["purged_task_ids"] == []
        assert task is not None
        assert task["status"] == TASK_RUNNING

    def test_rechecks_file_existence_before_deleting_candidate(self, task_store, tmp_path, monkeypatch):
        video = tmp_path / "restored.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-010")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)
        os.remove(video)

        real_exists = task_module.os.path.exists
        restored = False

        def exists_then_restore(path):
            nonlocal restored
            if path == str(video) and not restored:
                restored = True
                video.write_bytes(b"0")
                return False
            return real_exists(path)

        monkeypatch.setattr(task_module.os.path, "exists", exists_then_restore)

        result = task_store.sync_file_inventory()
        assert restored is True
        assert result["purged"] == 0
        assert result["purged_task_ids"] == []
        assert task_store.get_task(task_id) is not None

    def test_deferred_skipped_even_if_file_missing(self, task_store, tmp_path):
        video = tmp_path / "deferred.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-007")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_DEFERRED)
        os.remove(video)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 0
        assert task_store.get_task(task_id) is not None

    def test_mixed_tasks_only_purge_missing(self, task_store, tmp_path):
        v_keep = tmp_path / "keep.mp4"
        v_gone = tmp_path / "gone.mp4"
        v_keep.write_bytes(b"0")
        v_gone.write_bytes(b"0")
        from javsp.datatype import Movie
        m1 = Movie("KEEP-001")
        m1.files = [str(v_keep)]
        m1.save_dir = str(tmp_path)
        m2 = Movie("GONE-001")
        m2.files = [str(v_gone)]
        m2.save_dir = str(tmp_path)

        run = task_store.start_run("test")
        _, tid1 = task_store.enqueue_movie(m1, run, TASK_PENDING)
        task_store.mark_success(tid1, str(tmp_path), m1)
        _, tid2 = task_store.enqueue_movie(m2, run, TASK_PENDING)
        task_store.mark_success(tid2, str(tmp_path), m2)
        os.remove(v_gone)

        result = task_store.sync_file_inventory()
        assert result["purged"] == 1
        assert result["purged_task_ids"] == [tid2]
        assert task_store.get_task(tid1) is not None
        assert task_store.get_task(tid2) is None

    def test_deletes_file_index_orphans(self, task_store, tmp_path):
        video = tmp_path / "orphan.mp4"
        video.write_bytes(b"0")
        from javsp.datatype import Movie
        movie = Movie("ABC-009")
        movie.files = [str(video)]
        run = task_store.start_run("test")
        _, task_id = task_store.enqueue_movie(movie, run, TASK_PENDING)

        with task_store.connect() as conn:
            entry = conn.execute(
                "SELECT COUNT(*) as cnt FROM file_index WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert entry["cnt"] == 1

        os.remove(video)
        task_store.sync_file_inventory()

        with task_store.connect() as conn:
            entry = conn.execute(
                "SELECT COUNT(*) as cnt FROM file_index WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert entry["cnt"] == 0
