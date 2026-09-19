import os
from types import SimpleNamespace

import pytest

from javsp.task import TaskStore


def pytest_configure(config):
    config.addinivalue_line(
        "filterwarnings", "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )


@pytest.fixture
def task_store(tmp_path):
    """创建隔离的 TaskStore 实例（SQLite 在临时目录）。"""
    return TaskStore(tmp_path / "test_state.db")


def make_cfg(**overrides):
    """构造 pytest 用的 mock Cfg 配置 SimpleNamespace。"""
    cfg = dict(
        scanner=SimpleNamespace(
            filename_extensions=[".mp4", ".mkv"],
            minimum_size=100 * 1024 * 1024,  # 100 MiB
            restrict_to_files=None,
        ),
        daemon=SimpleNamespace(
            download_stable_seconds=300,
        ),
        retry_policy=SimpleNamespace(
            max_retry_count=3,
            network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
            metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
            crawler_circuit_break_threshold=5,
            crawler_circuit_break_duration=SimpleNamespace(total_seconds=lambda: 21600),
        ),
        metadata_complete=SimpleNamespace(enabled=False),
        summarizer=SimpleNamespace(
            move_files=False,
            path=SimpleNamespace(
                output_folder_pattern="{num}",
                basename_pattern="{num}",
                length_maximum=250,
                length_by_byte=False,
                max_actress_count=10,
                hard_link=False,
            ),
            default=SimpleNamespace(
                title="#未知标题",
                actress="#未知女优",
                series="#未知系列",
                director="#未知导演",
                producer="#未知制作商",
                publisher="#未知发行商",
            ),
            censor_options_representation=["无码", "有码", "打码情况未知"],
            nfo=SimpleNamespace(
                basename_pattern="movie",
                title_pattern="{num} {title}",
                custom_genres_fields=[],
                custom_tags_fields=[],
                include_actor_tmdbid=False,
            ),
            fanart=SimpleNamespace(basename_pattern="fanart"),
            cover=SimpleNamespace(basename_pattern="poster"),
            extra_fanarts=SimpleNamespace(enabled=False),
        ),
        network=SimpleNamespace(timeout=10, retry=3),
    )
    cfg.update(overrides)
    return SimpleNamespace(**cfg)


def make_movie(avid, tmp_path, filename=None, size=1024 * 1024):
    """创建带一个视频文件的 Movie 对象。"""
    from javsp.datatype import Movie

    fname = filename or f"{avid}.mp4"
    video = tmp_path / fname
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"0" * size)
    movie = Movie(avid)
    movie.files = [str(video)]
    movie.save_dir = str(video.parent)
    return movie


def _seed_pending_task(store, movie, trigger="test"):
    """创建 pending 状态任务，返回 (task_id, task_dict)。"""
    run_id = store.start_run(trigger)
    _, task_id = store.enqueue_movie(movie, run_id)
    store.finish_run(run_id)
    task = store.get_task(task_id)
    return task_id, task
