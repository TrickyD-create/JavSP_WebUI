from javsp.avid import guess_av_type


def test_removed_store_types_fall_back_to_normal():
    assert guess_av_type("GETCHU-4041026") == "normal"
    assert guess_av_type("GYUTTO-266923") == "normal"
