"""P1: NFO XML 写入完整字段输出、genre_norm 回退、自定义字段、generate_tmdbid。"""
from types import SimpleNamespace

import pytest
from lxml import etree

from javsp.datatype import MovieInfo
from javsp.nfo import generate_tmdbid, write_nfo
from javsp import nfo as nfo_module


def _nfo_cfg(include_actor_tmdbid=False, custom_genres=None, custom_tags=None):
    """构造 write_nfo 需要的 Cfg。"""
    return SimpleNamespace(
        summarizer=SimpleNamespace(
            nfo=SimpleNamespace(
                custom_genres_fields=custom_genres or [],
                custom_tags_fields=custom_tags or [],
                include_actor_tmdbid=include_actor_tmdbid,
                title_pattern="{num} {title}",
            ),
            cover=SimpleNamespace(basename_pattern="poster"),
            fanart=SimpleNamespace(basename_pattern="fanart"),
            path=SimpleNamespace(basename_pattern="{num}"),
            extra_fanarts=SimpleNamespace(enabled=False),
        ),
    )


class TestGenerateTmdbid:
    """确定性 SHA-1 哈希，固定长度 8 位数字。"""

    def test_deterministic(self):
        assert generate_tmdbid("测试女优") == generate_tmdbid("测试女优")

    def test_different_names_different_ids(self):
        assert generate_tmdbid("女优A") != generate_tmdbid("女优B")

    def test_output_is_8_chars(self):
        assert len(generate_tmdbid("test")) == 8

    def test_output_is_numeric(self):
        assert generate_tmdbid("hello").isdigit()


class TestWriteNfo:
    """NFO XML 全字段输出验证。"""

    def _write_and_parse(self, info, tmp_path, monkeypatch, cfg_overrides=None):
        """辅助：写入 NFO 并返回解析后的 XML root。"""
        cfg = cfg_overrides or _nfo_cfg()
        monkeypatch.setattr(nfo_module, "Cfg", lambda: cfg)
        nfo_path = tmp_path / "movie.nfo"
        # nfo_title 由 prepare_info_for_nfo() 动态设置，直接调用 write_nfo 时补充
        info.nfo_title = getattr(info, "nfo_title", None) or info.title or "测试"
        write_nfo(info, str(nfo_path))
        return etree.parse(str(nfo_path)).getroot()

    def test_title_written(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "美丽出道"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("title") == "美丽出道"

    def test_nfo_title_priority(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "美丽出道"
        info.nfo_title = "ABC-123 美丽出道"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("title") == "ABC-123 美丽出道"

    def test_original_title(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "标题"
        info.ori_title = "Original Title"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("originaltitle") == "Original Title"

    def test_score(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.score = "8.5"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("rating") == "8.5"

    def test_plot(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.plot = "这是一段剧情简介"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("plot") == "这是一段剧情简介"

    def test_duration(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.duration = "120分钟"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("runtime") == "120分钟"

    def test_mpaa_nc17(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("mpaa") == "NC-17"

    def test_dvdid_uniqueid(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        uniq = root.findall("uniqueid")
        assert len(uniq) == 1
        assert uniq[0].text == "ABC-123"
        assert uniq[0].get("type") == "num"
        assert uniq[0].get("default") == "true"

    def test_dvdid_and_cid_uniqueid(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.cid = "cid00888"
        info.title = "test"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        uniq = root.findall("uniqueid")
        types = {u.get("type"): u.text for u in uniq}
        assert types.get("num") == "ABC-123"
        assert types.get("cid") == "cid00888"

    def test_genre_written(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.genre = ["Drama", "Romance"]
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        genres = [g.text for g in root.findall("genre")]
        assert sorted(genres) == sorted(["Drama", "Romance"])

    def test_genre_norm_priority(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.genre = ["RawGenre"]
        info.genre_norm = ["剧情", "爱情"]
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        genres = [g.text for g in root.findall("genre")]
        assert "剧情" in genres
        assert "爱情" in genres
        assert "RawGenre" not in genres

    def test_genre_deduplication(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.genre = ["Drama", "Drama"]
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        genres = [g.text for g in root.findall("genre")]
        assert genres == ["Drama"]

    def test_custom_genre(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.genre = ["Base"]
        info.uncensored = True  # censor = 有码 (index 1)
        root = self._write_and_parse(
            info, tmp_path, monkeypatch,
            cfg_overrides=_nfo_cfg(custom_genres=["自定义-{num}", "标签-{censor}"],
                                  custom_tags=[]),
        )
        genres = [g.text for g in root.findall("genre")]
        assert "自定义-ABC-123" in genres
        assert "标签-有码" in genres

    def test_country(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("country") == "日本"

    def test_serial(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.serial = "超级系列"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        set_el = root.find("set")
        assert set_el is not None
        assert set_el.findtext("name") == "超级系列"

    def test_director(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.director = "某导演"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("director") == "某导演"

    def test_premiered(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.publish_date = "2024-01-15"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("premiered") == "2024-01-15"

    def test_studio(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.producer = "某制作商"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("studio") == "某制作商"

    def test_trailer(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.preview_video = "https://example.test/trailer.mp4"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        assert root.findtext("trailer") == "https://example.test/trailer.mp4"

    def test_actress_plain(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.actress = ["女优一号", "女优二号"]
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        actors = root.findall(".//actor")
        assert len(actors) == 2
        names = [a.findtext("name") for a in actors]
        assert names == ["女优一号", "女优二号"]

    def test_actress_with_pics(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.actress = ["女优一号"]
        info.actress_pics = {"女优一号": "https://example.test/1.jpg"}
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        actor = root.find(".//actor")
        assert actor.findtext("name") == "女优一号"
        assert actor.findtext("thumb") == "https://example.test/1.jpg"

    def test_actress_with_tmdbid(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.actress = ["女优一号"]
        info.actress_pics = {"女优一号": "https://example.test/1.jpg"}
        root = self._write_and_parse(
            info, tmp_path, monkeypatch,
            cfg_overrides=_nfo_cfg(include_actor_tmdbid=True),
        )
        actor = root.find(".//actor")
        assert actor.findtext("name") == "女优一号"
        assert actor.findtext("tmdbid") is not None
        assert len(actor.findtext("tmdbid")) == 8

    def test_optional_fields_omitted_when_none(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "minimal"
        root = self._write_and_parse(info, tmp_path, monkeypatch)
        # 这些字段不应出现
        assert root.find("originaltitle") is None
        assert root.find("rating") is None
        assert root.find("plot") is None
        assert root.find("runtime") is None
        assert root.find("director") is None
        assert root.find("premiered") is None
        assert root.find("trailer") is None
        assert root.find("studio") is None
        assert root.find("set") is None

    def test_tag_written_from_custom_tags(self, tmp_path, monkeypatch):
        info = MovieInfo("ABC-123")
        info.title = "test"
        info.genre_norm = ["剧情"]
        root = self._write_and_parse(
            info, tmp_path, monkeypatch,
            cfg_overrides=_nfo_cfg(custom_tags=["{label}", "{year}"]),
        )
        tags = [t.text for t in root.findall("tag")]
        assert "ABC" in tags
        assert "0000" in tags
