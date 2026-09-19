from javsp.config import CoverSummarize, CrawlerSelect


def test_removed_crawler_types_are_ignored_in_old_configs():
    selection = CrawlerSelect(
        normal=["javbus"],
        fc2=["fc2"],
        cid=["fanza"],
        getchu=["dl_getchu"],
        gyutto=["gyutto"],
    )

    assert selection.model_dump() == {
        "normal": ["javbus"],
        "fc2": ["fc2"],
        "cid": ["fanza"],
    }


def test_removed_cover_crop_config_is_ignored_in_old_configs():
    cover = CoverSummarize(
        basename_pattern="poster",
        highres=True,
        add_label=False,
        crop={
            "on_id_pattern": ["^FC2"],
            "engine": {"name": "slimeface"},
        },
    )

    assert cover.model_dump() == {
        "basename_pattern": "poster",
        "highres": True,
        "add_label": False,
    }
