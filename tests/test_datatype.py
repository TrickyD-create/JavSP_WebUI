"""P1: MovieInfo.get_info_dic 模板字典、Movie.rename_files 硬链接/移动/多分片。"""
from types import SimpleNamespace

import pytest

from javsp.datatype import Movie, MovieInfo
from javsp import datatype as datatype_module


def _cfg_dic():
    return SimpleNamespace(
        summarizer=SimpleNamespace(
            default=SimpleNamespace(
                title="#未知标题",
                actress="#未知女优",
                series="#未知系列",
                director="#未知导演",
                producer="#未知制作商",
                publisher="#未知发行商",
            ),
            censor_options_representation=["无码", "有码", "打码情况未知"],
        ),
    )


class TestGetInfoDic:
    """模板字典生成：字段默认值、genre_norm 回退、label 拆分。"""

    def test_basic_fields(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.title = "美丽出道"
        dic = info.get_info_dic()
        assert dic["num"] == "ABC-123"
        assert dic["title"] == "美丽出道"
        assert dic["label"] == "ABC"

    def test_title_default(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["title"] == "#未知标题"

    def test_rawtitle_falls_back_to_title(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.title = "标题"
        dic = info.get_info_dic()
        assert dic["rawtitle"] == "标题"

    def test_rawtitle_uses_ori_title_when_set(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.title = "标题"
        info.ori_title = "原始标题"
        dic = info.get_info_dic()
        assert dic["rawtitle"] == "原始标题"

    def test_actress_joined(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.actress = ["女优A", "女优B"]
        dic = info.get_info_dic()
        assert dic["actress"] == "女优A,女优B"

    def test_actress_default(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["actress"] == "#未知女优"

    def test_score_default_zero(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["score"] == "0"

    def test_censor_coded(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["censor"] == "无码"  # uncensored=None → 0

    def test_censor_uncensored_true(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.uncensored = True
        dic = info.get_info_dic()
        assert dic["censor"] == "有码"  # uncensored=True → 1

    def test_date_and_year(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.publish_date = "2024-03-15"
        dic = info.get_info_dic()
        assert dic["date"] == "2024-03-15"
        assert dic["year"] == "2024"

    def test_date_default(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["date"] == "0000-00-00"
        assert dic["year"] == "0000"

    def test_genre_norm_priority(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.genre = ["Raw"]
        info.genre_norm = ["规范化"]
        dic = info.get_info_dic()
        assert dic["genre"] == "规范化"

    def test_genre_fallback_to_raw(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        info.genre = ["Raw1", "Raw2"]
        dic = info.get_info_dic()
        assert dic["genre"] == "Raw1,Raw2"

    def test_genre_empty_default(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["genre"] == ""

    def test_label_no_dash(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo(cid="cid00888")
        dic = info.get_info_dic()
        assert dic["label"] == "---"

    def test_all_defaults(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo("ABC-123")
        dic = info.get_info_dic()
        assert dic["serial"] == "#未知系列"
        assert dic["director"] == "#未知导演"
        assert dic["producer"] == "#未知制作商"
        assert dic["publisher"] == "#未知发行商"

    def test_cid_as_num(self, monkeypatch):
        monkeypatch.setattr(datatype_module, "Cfg", lambda: _cfg_dic())
        info = MovieInfo(cid="cid00888")
        dic = info.get_info_dic()
        assert dic["num"] == "cid00888"
