import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from lxml import etree

from javsp.datatype import Movie, MovieInfo
from javsp.actress_alias import normalize_movie_info_actress
from javsp.metadata import check_metadata_complete, load_info_from_nfo, merge_refresh_info, prepare_info_for_nfo
from javsp.metadata import info_to_dict, info_from_dict, field_reasons, has_value, cjk_ratio
from javsp.task import (
    TASK_DEFERRED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_SUCCESS,
    TASK_TYPE_LEGACY_METADATA,
    INTERACTIVE_RESCRAPE_FIELDS,
    TaskStore,
    build_metadata_source_comparison,
    enqueue_movies,
    json_dumps,
)
from javsp import nfo as nfo_module
from javsp import datatype as datatype_module
from javsp import file as file_module
from javsp import metadata as metadata_module


def write_test_nfo(path, avid="MIAD-533", title="Beautiful debut", plot="暂无简介"):
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>{title}</title>
  <plot>{plot}</plot>
  <uniqueid type="num" default="true">{avid}</uniqueid>
  <genre>Drama</genre>
  <actor><name>测试女优</name></actor>
</movie>
""",
        encoding="utf-8",
    )


def _minimal_nfo_cfg(include_actor_tmdbid=False):
    return SimpleNamespace(
        scanner=SimpleNamespace(filename_extensions=[".mp4"]),
        metadata_complete=SimpleNamespace(enabled=False),
        summarizer=SimpleNamespace(
            move_files=False,
            title=SimpleNamespace(remove_trailing_actor_name=True),
            path=SimpleNamespace(
                output_folder_pattern="JAV/{actress}/[{num}] {title}",
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
                include_actor_tmdbid=include_actor_tmdbid,
            ),
            fanart=SimpleNamespace(basename_pattern="fanart"),
            cover=SimpleNamespace(basename_pattern="poster"),
        )
    )


def test_normalize_actress_without_pics_and_deduplicates():
    info = MovieInfo("ABC-123")
    info.actress = ["旧名A", "旧名B", "其他女优"]

    _, changed, details = normalize_movie_info_actress(info, {"标准名": ["旧名A", "旧名B"]})

    assert changed is True
    assert info.actress == ["标准名", "其他女优"]
    assert details["before"]["actress"] == ["旧名A", "旧名B", "其他女优"]


def test_normalize_actress_pics_keys():
    info = MovieInfo("ABC-123")
    info.actress = ["旧名A"]
    info.actress_pics = {"旧名A": "https://example.test/a.jpg", "旧名B": "https://example.test/b.jpg"}

    normalize_movie_info_actress(info, {"标准名": ["旧名A", "旧名B"]})

    assert info.actress == ["标准名"]
    assert info.actress_pics == {"标准名": "https://example.test/a.jpg"}


def test_write_nfo_respects_actor_tmdbid_config_after_actress_normalize(tmp_path, monkeypatch):
    cfg_without_tmdb = _minimal_nfo_cfg(include_actor_tmdbid=False)
    monkeypatch.setattr(nfo_module, "Cfg", lambda: cfg_without_tmdb)
    monkeypatch.setattr(datatype_module, "Cfg", lambda: cfg_without_tmdb)
    info = MovieInfo("ABC-123")
    info.title = "测试标题"
    info.nfo_title = "ABC-123 测试标题"
    info.actress = ["旧名A"]
    normalize_movie_info_actress(info, {"标准名": ["旧名A"]})
    nfo_path = tmp_path / "movie.nfo"

    nfo_module.write_nfo(info, str(nfo_path))
    root = etree.parse(str(nfo_path)).getroot()

    assert root.findtext(".//actor/name") == "标准名"
    assert root.findtext(".//actor/tmdbid") is None

    cfg_with_tmdb = _minimal_nfo_cfg(include_actor_tmdbid=True)
    monkeypatch.setattr(nfo_module, "Cfg", lambda: cfg_with_tmdb)
    monkeypatch.setattr(datatype_module, "Cfg", lambda: cfg_with_tmdb)
    nfo_module.write_nfo(info, str(nfo_path))
    root = etree.parse(str(nfo_path)).getroot()

    assert root.findtext(".//actor/name") == "标准名"
    assert root.findtext(".//actor/tmdbid") == nfo_module.generate_tmdbid("标准名")


def test_metadata_complete_detects_language_and_missing_fields():
    info = MovieInfo("ABC-123")
    info.title = "Beautiful debut"
    info.plot = "暂无简介"
    complete, reasons = check_metadata_complete(info)
    reason_keys = {(item["field"], item["reason"]) for item in reasons}

    assert not complete
    assert ("title", "language_preference") in reason_keys
    assert ("plot", "reject_value") in reason_keys


def test_refresh_merge_overwrites_bad_chinese_preference_fields_only():
    old = MovieInfo("ABC-123")
    old.title = "Beautiful debut"
    old.plot = "暂无简介"
    old.score = "8.0"

    incoming = MovieInfo("ABC-123")
    incoming.title = "美丽出道"
    incoming.plot = "这是一段已经补全的中文剧情简介，长度足够用于完整性判断。"
    incoming.score = "9.0"

    merged, updated = merge_refresh_info(old, {"javdb": incoming})

    assert merged.title == incoming.title
    assert merged.plot == incoming.plot
    assert merged.score == "8.0"
    assert set(updated) >= {"title", "plot"}
    assert "score" not in updated


def test_refresh_merge_prefers_later_candidate_that_satisfies_complete_rule():
    old = MovieInfo("ABC-123")
    old.title = "Beautiful debut"
    old.plot = "暂无简介"

    early = MovieInfo("ABC-123")
    early.title = "Still not Chinese"
    early.plot = "No story yet"
    later = MovieInfo("ABC-123")
    later.title = "中文标题"
    later.plot = "这是一段来自后置爬虫的中文剧情简介，长度足够用于完整性判断。"

    merged, updated = merge_refresh_info(old, {"early": early, "later": later})

    assert merged.title == later.title
    assert merged.plot == later.plot
    assert set(updated) >= {"title", "plot"}


def _make_metadata_rule(**overrides):
    defaults = dict(
        required=True,
        prefer_language="zh",
        min_cjk_ratio=0.3,
        min_length=0,
        min_items=0,
        reject_values=[],
        allow_overwrite=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_refresh_merge_respects_field_priorities(monkeypatch):
    """field_priorities 指定 javrate 优先时，多源都满足规则 → 选 javrate"""
    cfg = SimpleNamespace(
        metadata_complete=SimpleNamespace(
            fields={"title": _make_metadata_rule(), "plot": _make_metadata_rule()},
        ),
        crawler=SimpleNamespace(
            field_priorities=SimpleNamespace(
                title=["javrate"],
                plot=["javrate"],
                actress=[],
                preview_pics=["javrate"],
            ),
        ),
    )
    monkeypatch.setattr(metadata_module, "Cfg", lambda: cfg)

    old = MovieInfo("ABC-123")
    old.title = "日本語のタイトルで漢字も多いからChinese扱いに"
    old.plot = "暂无简介"

    airav = MovieInfo("ABC-123")
    airav.title = "日本語のタイトルで漢字も多いからChinese扱いに"
    airav.plot = "日文简介内容"
    airav.preview_pics = ["https://airav.example/1.jpg"]
    javrate = MovieInfo("ABC-123")
    javrate.title = "这是一段真正的中文标题"
    javrate.plot = "这是一段来自javrate的中文剧情简介长度足够用于完整性判断"
    javrate.preview_pics = ["https://javrate.example/1.jpg", "https://javrate.example/2.jpg"]

    merged, updated = merge_refresh_info(old, {"airav": airav, "javrate": javrate})

    assert merged.title == javrate.title
    assert merged.plot == javrate.plot
    assert merged.preview_pics == javrate.preview_pics
    assert "title" in updated


def test_refresh_merge_skips_priority_source_when_not_satisfying_rule(monkeypatch):
    """优先级源不满足规则时自动跳过，选下一个满足的源"""
    cfg = SimpleNamespace(
        metadata_complete=SimpleNamespace(
            fields={"title": _make_metadata_rule(), "plot": _make_metadata_rule()},
        ),
        crawler=SimpleNamespace(
            field_priorities=SimpleNamespace(
                title=["javbus"],
                plot=["javbus"],
                actress=[],
                preview_pics=[],
            ),
        ),
    )
    monkeypatch.setattr(metadata_module, "Cfg", lambda: cfg)

    old = MovieInfo("ABC-123")
    old.title = "English title only"
    old.plot = "暂无简介"

    javbus = MovieInfo("ABC-123")
    javbus.title = "English title only"
    javbus.plot = "English plot"
    javrate = MovieInfo("ABC-123")
    javrate.title = "真正的中文标题满足规则"
    javrate.plot = "这是一段中文剧情简介长度足够用于完整性判断"

    merged, updated = merge_refresh_info(
        old, {"javbus": javbus, "javrate": javrate}
    )

    assert merged.title == javrate.title
    assert merged.plot == javrate.plot
    assert "title" in updated


def test_refresh_normalizes_nfo_title_before_rewriting(tmp_path):
    nfo = tmp_path / "SQTE-681.nfo"
    write_test_nfo(
        nfo,
        avid="SQTE-681",
        title="SQTE-681 SQTE-681 SQTE-681 パンツ脱がさずヤリまくりたい。 逢沢みゆ",
        plot="这是一段已经补全的中文剧情简介，长度足够用于完整性判断。",
    )
    info = load_info_from_nfo(str(nfo))

    assert info.title == "パンツ脱がさずヤリまくりたい。 逢沢みゆ"

    prepare_info_for_nfo(info)

    assert info.nfo_title.count("SQTE-681") == 1
    assert info.nfo_title.startswith("SQTE-681 ")


def test_refresh_merge_normalizes_crawler_title_with_id_prefix():
    old = MovieInfo("SQTE-681")
    old.title = "SQTE-681 SQTE-681 パンツ脱がさずヤリまくりたい。 逢沢みゆ"
    old.plot = "这是一段已经补全的中文剧情简介，长度足够用于完整性判断。"
    incoming = MovieInfo("SQTE-681")
    incoming.title = "SQTE-681 パンツ脱がさずヤリまくりたい。 逢沢みゆ"

    merged, _ = merge_refresh_info(old, {"javdb": incoming})
    prepare_info_for_nfo(merged)

    assert merged.title == "パンツ脱がさずヤリまくりたい。 逢沢みゆ"
    assert merged.nfo_title.count("SQTE-681") == 1


def test_info_summary_uses_field_priorities_with_selection_fallback(monkeypatch):
    from javsp import __main__ as main_module

    cfg = SimpleNamespace(
        summarizer=SimpleNamespace(title=SimpleNamespace(remove_trailing_actor_name=False)),
        crawler=SimpleNamespace(
            field_priorities=SimpleNamespace(
                title=["second"],
                plot=["missing", "first"],
                actress=["third"],
                preview_pics=["third"],
            ),
            respect_site_avid=False,
            use_javdb_cover=main_module.UseJavDBCover.fallback,
            normalize_actress_name=False,
            required_keys=[],
        ),
    )
    monkeypatch.setattr(main_module, "Cfg", lambda: cfg)

    movie = Movie("ABC-123")
    first = MovieInfo("ABC-123")
    first.title = "默认顺序标题"
    first.plot = "默认顺序简介"
    first.actress = ["默认女优"]
    first.preview_pics = ["https://first.example/1.jpg"]
    second = MovieInfo("ABC-123")
    second.title = "字段优先标题"
    third = MovieInfo("ABC-123")
    third.actress = ["字段优先女优"]
    third.preview_pics = ["https://third.example/1.jpg", "https://third.example/2.jpg"]

    assert main_module.info_summary(movie, {"first": first, "second": second, "third": third}) is True
    assert movie.info.title == "字段优先标题"
    assert movie.info.plot == "默认顺序简介"
    assert movie.info.actress == ["字段优先女优"]
    assert movie.info.preview_pics == third.preview_pics


def test_info_summary_normalizes_actress_even_without_pics(monkeypatch):
    from javsp import __main__ as main_module

    cfg = SimpleNamespace(
        summarizer=SimpleNamespace(title=SimpleNamespace(remove_trailing_actor_name=False)),
        crawler=SimpleNamespace(
            field_priorities=SimpleNamespace(title=[], plot=[], actress=[]),
            respect_site_avid=False,
            use_javdb_cover=main_module.UseJavDBCover.fallback,
            normalize_actress_name=True,
            required_keys=[],
        ),
    )
    monkeypatch.setattr(main_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(main_module, "actressAliasMap", {"标准名": ["旧名"]})

    movie = Movie("ABC-123")
    source = MovieInfo("ABC-123")
    source.title = "测试标题"
    source.actress = ["旧名"]

    assert main_module.info_summary(movie, {"source": source}) is True
    assert movie.info.actress == ["标准名"]


def test_rescrape_task_replaces_old_metadata(tmp_path, monkeypatch):
    from javsp import __main__ as main_module

    cfg = _minimal_nfo_cfg()
    cfg.summarizer.extra_fanarts = SimpleNamespace(enabled=False, scrap_interval=SimpleNamespace(total_seconds=lambda: 0))
    monkeypatch.setattr(main_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(metadata_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(datatype_module, "Cfg", lambda: cfg)

    store = TaskStore(tmp_path / "state.db")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie = Movie("ABC-123")
    movie.files = [str(video)]
    run_id = store.start_run("test")
    _, task_id = store.enqueue_movie(movie, run_id)

    old = MovieInfo("ABC-123")
    old.title = "旧标题"
    old.plot = "旧剧情"
    movie.info = old
    movie.save_dir = str(tmp_path)
    nfo_path = tmp_path / "ABC-123.nfo"
    movie.nfo_file = str(nfo_path)
    movie.fanart_file = str(tmp_path / "fanart.jpg")
    movie.poster_file = str(tmp_path / "poster.jpg")
    write_test_nfo(nfo_path, avid="ABC-123", title="旧标题", plot="旧剧情")
    store.mark_success(task_id, movie.save_dir, movie)

    incoming = MovieInfo("ABC-123")
    incoming.title = "新标题"
    incoming.plot = "新剧情"
    incoming.actress = ["新女优"]
    incoming.cover = "https://new.example/cover.jpg"
    incoming.covers = [incoming.cover]
    incoming.big_covers = []

    monkeypatch.setattr(main_module, "parallel_crawler", lambda *args, **kwargs: {"javdb": incoming})
    monkeypatch.setattr(
        main_module,
        "download_cover",
        lambda covers, fanart_path, big_covers=[]: (
            covers[0],
            str(Path(fanart_path).write_bytes(b"new-cover") and Path(fanart_path)),
        ),
    )
    monkeypatch.setattr(
        main_module,
        "process_poster",
        lambda movie_arg: Path(movie_arg.poster_file).write_bytes(b"new-poster"),
    )

    def fake_info_summary(movie_arg, all_info):
        movie_arg.info = incoming
        return True

    monkeypatch.setattr(main_module, "info_summary", fake_info_summary)

    ok = main_module.refresh_metadata_task(
        store,
        str(tmp_path),
        task_id,
        trigger_type="rescrape",
        force_crawlers=True,
        replace_all=True,
    )

    task = store.get_task(task_id)
    assert ok is True
    assert task["info"]["title"] == "新标题"
    assert task["info"]["plot"] == "新剧情"
    assert Path(task["current_fanart_path"]).read_bytes() == b"new-cover"
    assert Path(task["current_poster_path"]).read_bytes() == b"new-poster"
    assert "新标题" in nfo_path.read_text(encoding="utf-8")


def test_interactive_rescrape_prepare_does_not_download_or_modify_files(tmp_path, monkeypatch):
    from javsp import __main__ as main_module

    cfg = _minimal_nfo_cfg()
    cfg.summarizer.extra_fanarts = SimpleNamespace(enabled=True, scrap_interval=SimpleNamespace(total_seconds=lambda: 0))
    monkeypatch.setattr(main_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(metadata_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(datatype_module, "Cfg", lambda: cfg)

    store = TaskStore(tmp_path / "state.db")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie = Movie("ABC-123")
    movie.files = [str(video)]
    run_id = store.start_run("test")
    _, task_id = store.enqueue_movie(movie, run_id)
    old = MovieInfo("ABC-123")
    old.title = "旧标题"
    movie.info = old
    movie.save_dir = str(tmp_path)
    movie.nfo_file = str(tmp_path / "ABC-123.nfo")
    movie.fanart_file = str(tmp_path / "fanart.jpg")
    movie.poster_file = str(tmp_path / "poster.jpg")
    write_test_nfo(Path(movie.nfo_file), avid="ABC-123", title="旧标题")
    store.mark_success(task_id, movie.save_dir, movie)

    incoming = MovieInfo("ABC-123")
    incoming.title = "候选标题"
    incoming.plot = "候选简介"
    incoming.cover = "https://example.test/cover.jpg"
    incoming.preview_pics = ["https://example.test/preview-1.jpg"]
    monkeypatch.setattr(main_module, "parallel_crawler", lambda *args, **kwargs: {"javdb": incoming})

    def fake_summary(movie_arg, all_info):
        movie_arg.info = incoming
        return True

    monkeypatch.setattr(main_module, "info_summary", fake_summary)
    forbidden_calls = []
    monkeypatch.setattr(main_module, "download_cover", lambda *args, **kwargs: forbidden_calls.append("cover"))
    monkeypatch.setattr(main_module, "download", lambda *args, **kwargs: forbidden_calls.append("preview"))
    monkeypatch.setattr(main_module, "write_nfo", lambda *args, **kwargs: forbidden_calls.append("nfo"))

    session, created = store.create_rescrape_session(task_id)
    assert created is True
    ok = main_module.prepare_interactive_rescrape(store, str(tmp_path), session["id"])

    saved = store.get_rescrape_session(session["id"])
    task = store.get_task(task_id)
    assert ok is True
    assert saved["status"] == "awaiting_selection"
    assert saved["candidates"]["javdb"]["cover"] == incoming.cover
    assert saved["candidates"]["javdb"]["preview_pics"] == incoming.preview_pics
    assert forbidden_calls == []
    assert task["info"]["title"] == "旧标题"
    assert Path(movie.nfo_file).read_text(encoding="utf-8").find("旧标题") >= 0
    assert not Path(movie.fanart_file).exists()
    assert not Path(movie.poster_file).exists()


def test_interactive_rescrape_applies_manual_fields_then_downloads_media(tmp_path, monkeypatch):
    from javsp import __main__ as main_module

    cfg = _minimal_nfo_cfg()
    cfg.summarizer.extra_fanarts = SimpleNamespace(enabled=False, scrap_interval=SimpleNamespace(total_seconds=lambda: 0))
    monkeypatch.setattr(main_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(metadata_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(datatype_module, "Cfg", lambda: cfg)

    store = TaskStore(tmp_path / "state.db")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie = Movie("ABC-123")
    movie.files = [str(video)]
    run_id = store.start_run("test")
    _, task_id = store.enqueue_movie(movie, run_id)
    old = MovieInfo("ABC-123")
    old.title = "旧标题"
    movie.info = old
    movie.save_dir = str(tmp_path)
    movie.nfo_file = str(tmp_path / "ABC-123.nfo")
    movie.fanart_file = str(tmp_path / "fanart.jpg")
    movie.poster_file = str(tmp_path / "poster.jpg")
    write_test_nfo(Path(movie.nfo_file), avid="ABC-123", title="旧标题")
    store.mark_success(task_id, movie.save_dir, movie)

    incoming = MovieInfo("ABC-123")
    incoming.title = "候选标题"
    incoming.plot = "候选简介"
    incoming.cover = "https://example.test/cover.jpg"
    incoming.actress = ["演员甲"]
    # GGJAV 等爬虫会返回与女优顺序对应的头像列表，而不是 name -> url 字典。
    incoming.actress_pics = ["https://example.test/actress-a.jpg"]
    monkeypatch.setattr(main_module, "parallel_crawler", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应再次运行爬虫")))

    def fake_summary(movie_arg, all_info):
        selected = next(iter(all_info.values()))
        selected.covers = [selected.cover]
        selected.big_covers = []
        movie_arg.info = selected
        return True

    monkeypatch.setattr(main_module, "info_summary", fake_summary)
    monkeypatch.setattr(
        main_module,
        "download_cover",
        lambda covers, fanart_path, big_covers=[]: (
            covers[0],
            str(Path(fanart_path).write_bytes(b"confirmed-cover") and Path(fanart_path)),
        ),
    )
    monkeypatch.setattr(main_module, "process_poster", lambda movie_arg: Path(movie_arg.poster_file).write_bytes(b"confirmed-poster"))

    session, _ = store.create_rescrape_session(task_id)
    store.save_rescrape_candidates(session["id"], {"javdb": info_to_dict(incoming)}, incoming)
    values = {item["field"]: None for item in INTERACTIVE_RESCRAPE_FIELDS}
    values.update({"title": "人工最终标题", "plot": "人工最终简介", "actress": ["演员甲"], "genre": ["剧情"]})
    store.begin_rescrape_apply(session["id"], values)

    ok = main_module.apply_interactive_rescrape(store, str(tmp_path), session["id"])

    task = store.get_task(task_id)
    saved = store.get_rescrape_session(session["id"])
    assert ok is True
    assert saved["status"] == "completed"
    assert task["info"]["title"] == "人工最终标题"
    assert task["info"]["plot"] == "人工最终简介"
    assert task["info"]["actress_pics"] == {"演员甲": "https://example.test/actress-a.jpg"}
    assert "人工最终标题" in Path(movie.nfo_file).read_text(encoding="utf-8")
    assert "https://example.test/actress-a.jpg" in Path(movie.nfo_file).read_text(encoding="utf-8")
    assert Path(task["current_fanart_path"]).read_bytes() == b"confirmed-cover"
    assert Path(task["current_poster_path"]).read_bytes() == b"confirmed-poster"


def test_nfo_studio_falls_back_to_publisher(tmp_path, monkeypatch):
    cfg = _minimal_nfo_cfg()
    monkeypatch.setattr(datatype_module, "Cfg", lambda: cfg)
    monkeypatch.setattr(nfo_module, "Cfg", lambda: cfg)
    info = MovieInfo("ABC-123")
    info.title = "测试标题"
    info.nfo_title = "测试标题"
    info.publisher = "发行商名称"
    nfo_path = tmp_path / "publisher.nfo"

    from javsp.nfo import write_nfo
    write_nfo(info, nfo_path)

    assert "<studio>发行商名称</studio>" in nfo_path.read_text(encoding="utf-8")


def test_task_store_migrates_legacy_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
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
        """
    )
    conn.close()

    TaskStore(db_path)
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    scrape_columns = {row[1] for row in conn.execute("PRAGMA table_info(scrape_results)").fetchall()}
    refresh_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata_refresh_runs'"
    ).fetchone()
    events_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_events'"
    ).fetchone()
    conn.close()

    assert "current_nfo_path" in columns
    assert "info_json" in columns
    assert "metadata_complete" in columns
    assert {"info_json", "elapsed", "error"}.issubset(scrape_columns)
    assert refresh_table is not None
    assert events_table is not None


def test_mark_success_records_current_paths_and_info(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
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
    task = store.get_task(task_id)

    assert task["current_nfo_path"] == movie.nfo_file
    assert task["info"]["title"] == "美丽出道"
    assert task["metadata_status"] in {"complete", "incomplete"}


def test_metadata_check_does_not_delay_due_refresh(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    old_ts = time.time() - 10 * 24 * 60 * 60
    info = MovieInfo("ABC-123")
    info.title = "Beautiful debut"
    info.plot = "暂无简介"
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(task_type, avid, data_src, files_json, fingerprint, status,
                created_at, updated_at, info_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("scrape_movie", "ABC-123", "normal", "[]", "fp-due", TASK_SUCCESS, old_ts, old_ts, "{}"),
        )
    task_id = store.list_tasks()[0]["id"]
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET info_json = ? WHERE id = ?",
            (json_dumps({"dvdid": "ABC-123", "title": info.title, "plot": info.plot}), task_id),
        )

    checked = store.check_task_metadata(task_id, str(tmp_path))
    due = store.due_incomplete_metadata_tasks(str(tmp_path), manual=False)

    assert checked["metadata_status"] == "incomplete"
    assert [task["id"] for task in due] == [task_id]


def test_missing_current_nfo_path_becomes_path_unknown(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(task_type, avid, data_src, files_json, fingerprint, status,
                created_at, updated_at, current_nfo_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("scrape_movie", "ABC-123", "normal", "[]", "fp-path", TASK_SUCCESS, time.time(), time.time(), str(tmp_path / "missing.nfo")),
        )
    task_id = store.list_tasks()[0]["id"]

    task = store.check_task_metadata(task_id, str(tmp_path))

    assert task["metadata_status"] == "path_unknown"


def test_retry_does_not_reset_success_task(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(task_type, avid, data_src, files_json, fingerprint, status,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("scrape_movie", "ABC-123", "normal", "[]", "fp-success", TASK_SUCCESS, time.time(), time.time()),
        )
    task_id = store.list_tasks()[0]["id"]

    assert store.retry_task(task_id) is False
    assert store.get_task(task_id)["status"] == TASK_SUCCESS


def test_success_task_with_missing_nfo_requeues_on_scan(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    movie = Movie("ABC-123")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie.files = [str(video)]
    run_id = store.start_run("first")
    _, task_id = store.enqueue_movie(movie, run_id)
    movie.info = MovieInfo("ABC-123")
    movie.info.title = "美丽出道"
    movie.info.plot = "这是一段已经补全的中文剧情简介，长度足够用于完整性判断。"
    movie.save_dir = str(tmp_path)
    movie.nfo_file = str(tmp_path / "missing.nfo")
    store.mark_success(task_id, movie.save_dir, movie)

    second_run = store.start_run("second")
    status, existing_id = store.enqueue_movie(movie, second_run)

    assert existing_id == task_id
    assert status == "pending"
    assert store.get_task(task_id)["status"] == "pending"


def test_deferred_task_requeues_when_temp_marker_is_gone(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    movie = Movie("ABC-123")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    marker = tmp_path / "ABC-123.mp4.!qB"
    marker.write_bytes(b"downloading")
    movie.files = [str(video)]

    first_run = store.start_run("first")
    first = enqueue_movies(store, [movie], first_run)
    task = store.list_tasks()[0]

    assert first["deferred"] == 1
    assert task["status"] == TASK_DEFERRED

    marker.unlink()
    second_run = store.start_run("second")
    second = enqueue_movies(store, [movie], second_run)
    task = store.get_task(task["id"])

    assert second["enqueued"] == 1
    assert task["status"] == TASK_PENDING
    assert task["failure_stage"] is None
    assert task["failure_reason"] is None


def test_delete_task_removes_related_records(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    movie = Movie("ABC-123")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie.files = [str(video)]
    run_id = store.start_run("test")
    _, task_id = store.enqueue_movie(movie, run_id)
    store.record_crawler_result(task_id, "javdb", True, fields=["title"])
    store.record_event(run_id, task_id, "info", "test_event", "test")
    refresh_id = store.start_metadata_refresh(task_id, "manual")
    store.finish_metadata_refresh(refresh_id, task_id, False, error="test")

    assert store.delete_task(task_id) is True
    assert store.get_task(task_id) is None
    assert store.list_scrape_results(task_id) == []
    assert store.list_task_events(task_id) == []


def test_record_crawler_result_saves_full_snapshot_elapsed_and_error(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    movie = Movie("ABC-123")
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    movie.files = [str(video)]
    run_id = store.start_run("test")
    _, task_id = store.enqueue_movie(movie, run_id)

    info = MovieInfo("ABC-123")
    info.title = "美丽出道"
    info.genre = ["剧情", "高清"]
    store.record_crawler_result(task_id, "javdb", True, elapsed=1.25, fields=["title", "genre"], url="https://example.test", title=info.title, info=info)
    store.record_crawler_result(task_id, "javbus", False, elapsed=0.5, error="blocked")

    results = {item["crawler_name"]: item for item in store.list_scrape_results(task_id)}

    assert results["javdb"]["info"]["title"] == "美丽出道"
    assert results["javdb"]["info"]["genre"] == ["剧情", "高清"]
    assert results["javdb"]["elapsed"] == 1.25
    assert results["javbus"]["success"] is False
    assert results["javbus"]["error"] == "blocked"


def test_metadata_source_comparison_handles_strings_lists_and_legacy_summary():
    final = {
        "title": "美丽出道",
        "genre": ["剧情", "高清"],
        "publisher": "Alice",
    }
    comparison = build_metadata_source_comparison(
        final,
        [
            {
                "crawler_name": "javdb",
                "success": True,
                "info": {"title": "美丽出道", "genre": ["剧情", "高清", "独占"]},
                "fields": ["title", "genre"],
            },
            {
                "crawler_name": "javbus",
                "success": True,
                "info": {"title": "Other", "genre": ["其他"]},
                "fields": ["title", "genre"],
            },
        ],
    )
    fields = {item["field"]: item for item in comparison["fields"]}

    assert comparison["has_snapshots"] is True
    assert fields["title"]["adopted_source"] == "javdb"
    assert fields["genre"]["adopted_source"] == "javdb"
    assert fields["title"]["conflict"] is True
    assert fields["publisher"]["status"] == "unknown"

    legacy = build_metadata_source_comparison(final, [{"crawler_name": "old", "success": True, "fields": ["title"], "title": "美丽出道"}])
    assert legacy["has_snapshots"] is False
    assert "旧记录" in legacy["note"]


def test_import_legacy_metadata_dir_registers_nfo_and_updates_without_duplicates(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    nfo_path = tmp_path / "MIAD-533.nfo"
    write_test_nfo(nfo_path)

    summary = store.import_legacy_metadata_dir(str(tmp_path))
    tasks = store.list_tasks()

    assert summary["scanned"] == 1
    assert summary["created"] == 1
    assert summary["incomplete"] == 1
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == TASK_TYPE_LEGACY_METADATA
    assert tasks[0]["avid"] == "MIAD-533"
    assert tasks[0]["status"] == TASK_SUCCESS
    assert tasks[0]["current_nfo_path"] == str(nfo_path)
    assert tasks[0]["metadata_status"] == "incomplete"

    write_test_nfo(
        nfo_path,
        title="美丽出道",
        plot="这是一段已经补全的中文剧情简介，长度足够用于完整性判断。",
    )
    second = store.import_legacy_metadata_dir(str(tmp_path))
    tasks = store.list_tasks()

    assert second["created"] == 0
    assert second["updated"] == 1
    assert len(tasks) == 1
    assert tasks[0]["info"]["title"] == "美丽出道"
    assert tasks[0]["metadata_status"] == "complete"


def test_import_nfo_preserves_normal_task_identity_and_failure_state(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    video = tmp_path / "MIAD-533.mp4"
    video.write_bytes(b"0" * 1024)
    nfo_path = tmp_path / "MIAD-533.nfo"
    write_test_nfo(nfo_path)

    movie = Movie("MIAD-533")
    movie.files = [str(video)]
    _, task_id = store.enqueue_movie(movie, run_id=1, status=TASK_PENDING)
    retry_at = time.time() - 1
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, failure_stage = ?, failure_reason = ?, retry_count = ?,
                next_retry_at = ?, current_nfo_path = ?, current_save_dir = ?
            WHERE id = ?
            """,
            (
                TASK_FAILED,
                "metadata",
                "测试失败",
                2,
                retry_at,
                str(nfo_path),
                str(tmp_path),
                task_id,
            ),
        )
    before = store.get_task(task_id)

    store.import_legacy_metadata_dir(str(tmp_path))
    after = store.get_task(task_id)

    assert after["task_type"] == before["task_type"] == "scrape_movie"
    assert after["fingerprint"] == before["fingerprint"]
    assert after["avid"] == before["avid"] == "MIAD-533"
    assert after["status"] == TASK_FAILED
    assert after["failure_stage"] == "metadata"
    assert after["failure_reason"] == "测试失败"
    assert after["retry_count"] == 2
    assert after["next_retry_at"] == retry_at

    # 再次扫描应命中并更新原任务，而不是因指纹被替换而创建重复任务。
    _, enqueued_task_id = store.enqueue_movie(movie, run_id=2, status=TASK_PENDING)
    assert enqueued_task_id == task_id
    assert len(store.list_tasks()) == 1


def test_import_nfo_clears_stale_local_preview_pics(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    nfo_path = tmp_path / "MIAD-533.nfo"
    write_test_nfo(nfo_path)
    store.import_legacy_metadata_dir(str(tmp_path))
    task = store.list_tasks()[0]
    info = task["info"]
    info["preview_pics"] = [str(tmp_path / "extrafanart" / "0.jpg")]
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET info_json = ? WHERE id = ?", (json_dumps(info), task["id"]))

    store.import_legacy_metadata_dir(str(tmp_path))

    assert store.get_task(task["id"])["info"]["preview_pics"] is None


def test_import_nfo_keeps_remote_preview_pics_without_local_directory(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    nfo_path = tmp_path / "MIAD-533.nfo"
    write_test_nfo(nfo_path)
    store.import_legacy_metadata_dir(str(tmp_path))
    task = store.list_tasks()[0]
    info = task["info"]
    info["preview_pics"] = ["https://img.example/0.jpg"]
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET info_json = ? WHERE id = ?", (json_dumps(info), task["id"]))

    store.import_legacy_metadata_dir(str(tmp_path))

    assert store.get_task(task["id"])["info"]["preview_pics"] == ["https://img.example/0.jpg"]


def test_import_nfo_does_not_turn_complete_normal_task_incomplete(tmp_path, monkeypatch):
    """归一化前导入 NFO 时，不应因 NFO 不存 preview_pics 批量产生待优化任务。"""
    cfg = SimpleNamespace(
        metadata_complete=SimpleNamespace(
            enabled=True,
            fields={"preview_pics": _make_metadata_rule(min_items=1)},
        )
    )
    monkeypatch.setattr(metadata_module, "Cfg", lambda: cfg)

    store = TaskStore(tmp_path / "state.db")
    video = tmp_path / "MIAD-533.mp4"
    video.write_bytes(b"0" * 1024)
    nfo_path = tmp_path / "MIAD-533.nfo"
    write_test_nfo(nfo_path)
    movie = Movie("MIAD-533")
    movie.files = [str(video)]
    _, task_id = store.enqueue_movie(movie, run_id=1, status=TASK_PENDING)

    old_info = MovieInfo("MIAD-533")
    old_info.title = "Beautiful debut"
    old_info.preview_pics = ["https://img.example/0.jpg"]
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, current_nfo_path = ?, current_save_dir = ?, info_json = ?,
                metadata_complete = 1, metadata_incomplete_reasons_json = '[]'
            WHERE id = ?
            """,
            (TASK_SUCCESS, str(nfo_path), str(tmp_path), json_dumps(info_to_dict(old_info)), task_id),
        )

    store.import_legacy_metadata_dir(str(tmp_path))
    task = store.get_task(task_id)

    assert task["status"] == TASK_SUCCESS
    assert task["metadata_status"] == "complete"
    assert task["info"]["preview_pics"] == ["https://img.example/0.jpg"]


def test_import_legacy_metadata_dir_bypasses_normal_scan_ignores(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    ignored_dir = tmp_path / "JAV" / "MIAD-533"
    ignored_dir.mkdir(parents=True)
    nfo_path = ignored_dir / "movie.nfo"
    write_test_nfo(nfo_path)

    summary = store.import_legacy_metadata_dir(str(tmp_path))
    task = store.list_tasks()[0]

    assert summary["scanned"] == 1
    assert summary["created"] == 1
    assert task["current_nfo_path"] == str(nfo_path)


def test_import_legacy_metadata_dir_reports_invalid_nfo(tmp_path):
    db_path = tmp_path / "state.db"
    store = TaskStore(db_path)
    bad_nfo = tmp_path / "MIAD-533.nfo"
    bad_nfo.write_text("<movie>", encoding="utf-8")

    summary = store.import_legacy_metadata_dir(str(tmp_path))

    assert summary["scanned"] == 1
    assert summary["failed"] == 1
    assert store.list_tasks() == []


def test_normalize_metadata_dir_cleans_title_with_new_aliases(tmp_path, monkeypatch):
    """归一化时 title 尾部有旧别名 → 展开全量别名匹配并清理"""
    from javsp import __main__ as main_module
    from javsp.func import remove_trail_actor_in_title

    cfg = _minimal_nfo_cfg(include_actor_tmdbid=False)
    for module in (main_module, nfo_module, datatype_module, file_module, metadata_module):
        monkeypatch.setattr(module, "Cfg", lambda: cfg)

    monkeypatch.setattr(main_module, "load_actress_alias_map", lambda: {"新女优": ["旧女优", "别名C"]})

    root = tmp_path / "library"
    import_root = root / "JAV"
    old_dir = import_root / "旧女优" / "[ABC-123] Beautiful debut - 别名C"
    old_dir.mkdir(parents=True)
    nfo_path = old_dir / "movie.nfo"
    write_test_nfo(nfo_path, avid="ABC-123", title="Beautiful debut - 别名C")
    nfo_path.write_text(
        nfo_path.read_text(encoding="utf-8").replace("测试女优", "旧女优"), encoding="utf-8"
    )
    video = old_dir / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)

    store = TaskStore(tmp_path / "state.db")
    summary = main_module.normalize_actress_metadata_dir(
        store, str(root), import_root=str(import_root), move=True
    )

    # 移动后女优归一化为标准名，title 尾部别名被清理
    new_dir = root / "JAV" / "新女优" / "[ABC-123] Beautiful debut"
    assert summary["changed"] == 1
    assert (new_dir / "ABC-123.mp4").exists()
    parsed = etree.parse(str(new_dir / "movie.nfo")).getroot()
    assert parsed.findtext(".//actor/name") == "新女优"
    assert parsed.findtext(".//title") == "ABC-123 Beautiful debut"


def test_normalize_metadata_dir_skips_when_no_change_and_title_clean(tmp_path, monkeypatch):
    """女优名已是标准名且 title 干净 → 跳过不处理"""
    from javsp import __main__ as main_module

    cfg = _minimal_nfo_cfg(include_actor_tmdbid=False)
    for module in (main_module, nfo_module, datatype_module, file_module, metadata_module):
        monkeypatch.setattr(module, "Cfg", lambda: cfg)

    monkeypatch.setattr(main_module, "load_actress_alias_map", lambda: {"新女优": ["旧女优"]})

    root = tmp_path / "library"
    import_root = root / "JAV"
    old_dir = import_root / "新女优" / "[ABC-123] 干净标题"
    old_dir.mkdir(parents=True)
    nfo_path = old_dir / "movie.nfo"
    write_test_nfo(nfo_path, avid="ABC-123", title="干净标题")
    nfo_path.write_text(
        nfo_path.read_text(encoding="utf-8").replace("测试女优", "新女优"), encoding="utf-8"
    )
    video = old_dir / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)

    store = TaskStore(tmp_path / "state.db")
    summary = main_module.normalize_actress_metadata_dir(
        store, str(root), import_root=str(import_root), move=True
    )

    assert summary["changed"] == 0
    assert summary["skipped"] == 1


def test_normalize_actress_metadata_dir_moves_by_current_naming_rule(tmp_path, monkeypatch):
    from javsp import __main__ as main_module

    cfg = _minimal_nfo_cfg(include_actor_tmdbid=False)
    for module in (main_module, nfo_module, datatype_module, file_module, metadata_module):
        monkeypatch.setattr(module, "Cfg", lambda: cfg)
    monkeypatch.setattr(main_module, "load_actress_alias_map", lambda: {"新女优": ["旧女优"]})

    root = tmp_path / "library"
    import_root = root / "JAV"
    old_dir = import_root / "旧女优" / "[ABC-123] 旧标题"
    old_dir.mkdir(parents=True)
    nfo_path = old_dir / "movie.nfo"
    write_test_nfo(nfo_path, avid="ABC-123", title="旧标题")
    nfo_path.write_text(nfo_path.read_text(encoding="utf-8").replace("测试女优", "旧女优"), encoding="utf-8")
    video = old_dir / "ABC-123.mp4"
    video.write_bytes(b"0" * 1024)
    (old_dir / "poster.jpg").write_bytes(b"poster")

    store = TaskStore(tmp_path / "state.db")
    summary = main_module.normalize_actress_metadata_dir(store, str(root), import_root=str(import_root), move=True)
    new_dir = root / "JAV" / "新女优" / "[ABC-123] 旧标题"
    new_nfo = new_dir / "movie.nfo"

    assert summary["changed"] == 1
    assert summary["moved"] == 1
    assert not nfo_path.exists()
    assert not (root / "JAV" / "JAV").exists()
    assert (new_dir / "ABC-123.mp4").exists()
    assert (new_dir / "poster.jpg").exists()
    parsed = etree.parse(str(new_nfo)).getroot()
    assert parsed.findtext(".//actor/name") == "新女优"
    task = store.list_tasks()[0]
    assert task["current_save_dir"] == str(new_dir)
    assert task["current_nfo_path"] == str(new_nfo)


# ──────────────────────── P1: info_to_dict / info_from_dict ────────────────────────


class TestInfoSerialization:
    """/dev/null ⨯ 序列化往返：CID/DVDID 构造、data_src 决策。"""

    def test_roundtrip_dvdid(self):
        original = MovieInfo("ABC-123")
        original.title = "美丽出道"
        original.plot = "剧情"
        original.actress = ["女优A"]
        d = info_to_dict(original)
        restored = info_from_dict(d, "ABC-123", "normal")
        assert restored is not None
        assert restored.title == "美丽出道"
        assert restored.plot == "剧情"
        assert restored.actress == ["女优A"]
        assert restored.dvdid == "ABC-123"

    def test_roundtrip_cid(self):
        original = MovieInfo(cid="cid00888")
        original.title = "CID标题"
        d = info_to_dict(original)
        restored = info_from_dict(d, "cid00888", "cid")
        assert restored is not None
        assert restored.title == "CID标题"
        assert restored.cid == "cid00888"
        assert restored.dvdid is None

    def test_info_to_dict_returns_empty_for_none(self):
        assert info_to_dict(None) == {}

    def test_info_from_dict_empty_data(self):
        assert info_from_dict({}, "ABC-123", "normal") is not None

    def test_info_from_dict_none_data(self):
        assert info_from_dict(None, "ABC-123", "normal") is not None

    def test_info_from_dict_no_ids(self):
        assert info_from_dict({}, None, "normal") is None

    def test_title_normalized_on_load(self):
        d = {"title": "ABC-123 美丽出道"}
        restored = info_from_dict(d, "ABC-123", "normal")
        assert restored.title == "美丽出道"


# ──────────────────────── P1: field_reasons ────────────────────────


class TestFieldReasons:
    """完整性检查：missing、reject_value、min_length、language_preference、min_items。"""

    RegularField = SimpleNamespace(
        required=True,
        min_items=None,
        reject_values=None,
        min_length=None,
        prefer_language=None,
        min_cjk_ratio=None,
    )

    def test_missing_none(self):
        reasons = field_reasons("title", None, self.RegularField)
        assert any(r["reason"] == "missing" for r in reasons)

    def test_missing_empty_string(self):
        reasons = field_reasons("title", "", self.RegularField)
        assert any(r["reason"] == "missing" for r in reasons)

    def test_ok_value(self):
        reasons = field_reasons("title", "有值", self.RegularField)
        assert not reasons

    def test_reject_value(self):
        rule = SimpleNamespace(
            required=True, reject_values={"暂无简介", "无"}, min_length=None,
            min_items=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("plot", "暂无简介", rule)
        assert any(r["reason"] == "reject_value" for r in reasons)

    def test_reject_value_no_match(self):
        rule = SimpleNamespace(
            required=True, reject_values={"暂无简介"}, min_length=None,
            min_items=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("plot", "有效内容", rule)
        assert not any(r["reason"] == "reject_value" for r in reasons)

    def test_min_length_fails(self):
        rule = SimpleNamespace(
            required=True, min_length=10, reject_values=None,
            min_items=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("plot", "短", rule)
        assert any(r["reason"] == "min_length" for r in reasons)

    def test_min_length_ok(self):
        rule = SimpleNamespace(
            required=True, min_length=5, reject_values=None,
            min_items=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("plot", "足够长的内容", rule)
        assert not any(r["reason"] == "min_length" for r in reasons)

    def test_language_preference_cjk_low(self):
        rule = SimpleNamespace(
            required=True, prefer_language="zh", min_cjk_ratio=0.5,
            reject_values=None, min_length=None, min_items=None,
        )
        reasons = field_reasons("title", "Hello World", rule)
        assert any(r["reason"] == "language_preference" for r in reasons)

    def test_language_preference_cjk_ok(self):
        rule = SimpleNamespace(
            required=True, prefer_language="zh", min_cjk_ratio=0.3,
            reject_values=None, min_length=None, min_items=None,
        )
        reasons = field_reasons("title", "这是中文标题也有English混合", rule)
        # 中文比例应足够
        assert not any(r["reason"] == "language_preference" for r in reasons)

    def test_min_items_fails(self):
        rule = SimpleNamespace(
            required=True, min_items=3,
            reject_values=None, min_length=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("genre", ["A"], rule)
        assert any(r["reason"] == "min_items" for r in reasons)

    def test_min_items_ok(self):
        rule = SimpleNamespace(
            required=True, min_items=2,
            reject_values=None, min_length=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("genre", ["A", "B", "C"], rule)
        assert not any(r["reason"] == "min_items" for r in reasons)

    def test_dict_min_items(self):
        rule = SimpleNamespace(
            required=True, min_items=2,
            reject_values=None, min_length=None, prefer_language=None, min_cjk_ratio=None,
        )
        reasons = field_reasons("actress_pics", {"a": "x"}, rule)
        assert any(r["reason"] == "min_items" for r in reasons)

    def test_multiple_reasons(self):
        rule = SimpleNamespace(
            required=True, min_length=10, prefer_language="zh", min_cjk_ratio=0.5,
            reject_values=None, min_items=None,
        )
        reasons = field_reasons("plot", "Hi", rule)
        assert any(r["reason"] == "min_length" for r in reasons)
        assert any(r["reason"] == "language_preference" for r in reasons)


# ──────────────────────── P1: has_value / cjk_ratio ────────────────────────


class TestHasValue:
    def test_none(self):
        assert has_value(None) is False

    def test_empty_string(self):
        assert has_value("") is False
        assert has_value("  ") is False

    def test_non_empty_string(self):
        assert has_value("hello") is True

    def test_empty_list(self):
        assert has_value([]) is False

    def test_non_empty_list(self):
        assert has_value(["a"]) is True

    def test_empty_dict(self):
        assert has_value({}) is False


class TestCjkRatio:
    def test_all_ascii(self):
        assert cjk_ratio("Hello World") == 0.0

    def test_all_cjk(self):
        assert cjk_ratio("中国电影标题") == 1.0

    def test_mixed(self):
        ratio = cjk_ratio("Hello 你好 World")
        assert ratio == 1.0

    def test_english_excluded_but_punctuation_retained(self):
        assert cjk_ratio("English中文!") == 2 / 3

    def test_empty(self):
        assert cjk_ratio("") == 0.0

    def test_only_spaces(self):
        assert cjk_ratio("   ") == 0.0
