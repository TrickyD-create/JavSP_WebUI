from types import SimpleNamespace

from requests import Response

from javsp.datatype import MovieInfo
from javsp.web import fc2, fc2cmadb, fc2ppvdb, javmenu, mgstage, prestige


def make_response(text, url, status_code=200, history=None):
    resp = Response()
    resp.status_code = status_code
    resp._content = text.encode("utf-8")
    resp.url = url
    resp.history = history or []
    return resp


def test_javmenu_parses_current_pred_330_markup(monkeypatch):
    url = "https://mrzyx.xyz/PRED-330"
    html = """
    <html>
      <body>
        <div class="col-md-9 px-1 px-md-0">
          <div class="mb-3 px-1">
            <h1 class="display-5"><strong>
              PRED-330 同學會NTR 妻子和前男友的出軌中出映像 香椎花乃 免費AV在線看
            </strong></h1>
          </div>
          <div class="single-video">
            <video poster="https://c0.jdbstatic.com/covers/d0/D0Y7k.jpg"></video>
          </div>
          <div class="card rounded">
            <div class="card-body">
              <div><span>番號:&nbsp;</span><a>PRED</a><span>-330</span></div>
              <div><span>發佈於:&nbsp;</span><span>2026-07-11</span></div>
              <div><span>時長:&nbsp;</span><span>121分鐘</span></div>
              <div>
                <span>類別:&nbsp;</span>
                <div>
                  <a class="genre" href="/censored/genre/18">中出</a>
                  <a class="genre" href="/censored/genre/51">出軌</a>
                </div>
              </div>
              <div><span>女優:&nbsp;</span><div><a>香椎花乃</a></div></div>
            </div>
          </div>
          <a data-fancybox="gallery" href="https://img.example/sample-1.jpg"></a>
          <a data-fancybox="gallery" href="https://img.example/sample-2.jpg"></a>
          <table class="magnet-table"><tbody><tr><td>
            <a href="magnet:?xt=urn:btih:abc&amp;dn=[javdb.com]PRED-330"></a>
          </td></tr></tbody></table>
        </div>
      </body>
    </html>
    """
    monkeypatch.setattr(
        javmenu.request,
        "get",
        lambda *args, **kwargs: make_response(html, url),
    )

    movie = MovieInfo("PRED-330")
    javmenu.parse_data(movie)

    assert movie.url == url
    assert movie.title == "同學會NTR 妻子和前男友的出軌中出映像 香椎花乃"
    assert movie.cover == "https://c0.jdbstatic.com/covers/d0/D0Y7k.jpg"
    assert movie.publish_date == "2026-07-11"
    assert movie.duration == "121"
    assert movie.genre == ["中出", "出軌"]
    assert movie.genre_id == ["censored/18", "censored/51"]
    assert movie.actress == ["香椎花乃"]
    assert movie.preview_pics == [
        "https://img.example/sample-1.jpg",
        "https://img.example/sample-2.jpg",
    ]
    assert movie.magnet == ["magnet:?xt=urn:btih:abc&dn=PRED-330"]


def test_javmenu_keeps_legacy_container_date_and_data_poster(monkeypatch):
    url = "https://mrzyx.xyz/ABC-123"
    html = """
    <html><body><div class="col-md-9 px-0">
      <div class="col-12 mb-3"><h1><strong>ABC-123 舊版標題 免費在線看</strong></h1></div>
      <div class="single-video"><video data-poster=" https://img.example/old.jpg "></video></div>
      <div class="card-body">
        <div><span>日期:</span><span>2020-01-02</span></div>
        <div><span>時長:</span><span>90分鐘</span></div>
      </div>
    </div></body></html>
    """
    monkeypatch.setattr(
        javmenu.request,
        "get",
        lambda *args, **kwargs: make_response(html, url),
    )

    movie = MovieInfo("ABC-123")
    javmenu.parse_data(movie)

    assert movie.title == "舊版標題"
    assert movie.cover == "https://img.example/old.jpg"
    assert movie.publish_date == "2020-01-02"
    assert movie.duration == "90"


def test_fc2_parses_current_article_markup_without_hidden_title_noise(monkeypatch):
    url = "https://adult.contents.fc2.com/article/4921044/"
    article = """
    <html>
      <head>
        <meta property="og:title" content="FC2-PPV-4921044 すべてを手に入れた爆乳女社長の超絶フェラテクに悶絶。">
        <meta property="og:image" content="https://storage.example/cover.jpg">
      </head>
      <body>
        <div class="items_article_left">
          <div class="items_article_MainitemThumb">
            <span><img src="https://img.example/thumb.jpg"><p class="items_article_info">15:22</p></span>
          </div>
          <div class="items_article_headerInfo">
            <h3>すべてを手に入れた爆乳女社長の超絶フェラテクに悶絶。<span style="display:none">***noise</span></h3>
            <ul><li>by <a>イチャラブヘイタ</a></li></ul>
            <a class="tag tagTag">ハメ撮り</a><a class="tag tagTag">巨乳</a>
            <div class="items_article_softDevice"><p>販売日 : 2026/06/19</p></div>
          </div>
          <ul data-feed="sample-images">
            <li><a href="https://img.example/sample-1.jpg"></a></li>
            <li><a href="https://img.example/sample-2.jpg"></a></li>
          </ul>
          <a class="items_article_Stars"><p><span class="items_article_Star5"></span></p></a>
        </div>
      </body>
    </html>
    """
    monkeypatch.setattr(fc2, "request_get", lambda *args, **kwargs: make_response(article, url))
    monkeypatch.setattr(fc2, "Cfg", lambda: SimpleNamespace(crawler=SimpleNamespace(hardworking=False)))

    movie = MovieInfo("FC2-4921044")
    fc2.parse_data(movie)

    assert movie.url == url
    assert movie.dvdid == "FC2-4921044"
    assert movie.title == "すべてを手に入れた爆乳女社長の超絶フェラテクに悶絶。"
    assert "noise" not in movie.title
    assert movie.cover == "https://img.example/sample-1.jpg"
    assert movie.producer == "イチャラブヘイタ"
    assert movie.duration == "15"
    assert movie.publish_date == "2026-06-19"
    assert movie.genre == ["ハメ撮り", "巨乳"]
    assert movie.preview_pics == ["https://img.example/sample-1.jpg", "https://img.example/sample-2.jpg"]
    assert movie.score == "10.00"


def test_fc2cmadb_parses_inertia_article_data(monkeypatch):
    url = "https://fc2cmadb.com/articles/4921044"
    page = {
        "component": "Articles/Show",
        "props": {
            "article": {
                "title": "すべてを手に入れた爆乳女社長",
                "video_id": 4921044,
                "censored": "有",
                "not_found": None,
                "release_date": "2026-06-19",
                "duration": "15:22",
                "image_url": "https://contents-thumbnail2.fc2.com/w276/cover.jpg",
                "writer": {"name": "イチャラブヘイタ"},
                "tags": [{"name": "ハメ撮り"}, {"name": "巨乳"}],
            },
            "actresses": [{"id": 1, "name": "女优A"}],
        },
    }
    html = (
        '<html><script data-page="app" type="application/json">'
        + __import__("json").dumps(page, ensure_ascii=False)
        + "</script></html>"
    )
    monkeypatch.setattr(
        fc2cmadb,
        "request_get",
        lambda *args, **kwargs: make_response(html, url),
    )

    movie = MovieInfo("fc2-4921044")
    fc2cmadb.parse_data(movie)

    assert movie.dvdid == "FC2-4921044"
    assert movie.url == url
    assert movie.title == "すべてを手に入れた爆乳女社長"
    assert movie.cover == "https://contents-thumbnail2.fc2.com/w276/cover.jpg"
    assert movie.genre == ["ハメ撮り", "巨乳"]
    assert movie.actress == ["女优A"]
    assert movie.duration == "15"
    assert movie.publish_date == "2026-06-19"
    assert movie.publisher == "イチャラブヘイタ"
    assert movie.uncensored is False


def test_fc2ppvdb_old_name_uses_fc2cmadb_parser():
    assert fc2ppvdb.parse_data is fc2cmadb.parse_data


def test_prestige_parses_ipzz_734_with_current_picture_markup(monkeypatch):
    url = "https://www.prestige-av.com/goods/goods_detail.php?sku=IPZZ-734"
    html = """
    <html>
      <head><meta property="og:image" content="https://img.example/og.jpg"></head>
      <body>
        <main>
          <section class="px-4 mb-4 md:px-8 md:mb-16 updated">
            <h1><span>IPZZ-734</span> めちゃかわ彼女と濃密デート</h1>
            <div class="c-ratio-image mr-8 extra">
              <picture>
                <source srcset="https://img.example/ipzz-734-big.jpg?token=1">
                <img src="https://img.example/ipzz-734.jpg?token=1">
              </picture>
            </div>
            <p>出演者：</p><div><p><a> 白浜 のぞみ </a><a>蒼乃 美月</a></p></div>
            <p>収録時間：</p><div><p>120分</p></div>
            <p>発売日：</p><div><a href="/goods/list.php?date=2026-05-02">2026/05/02</a></div>
            <p>メーカー：</p><div><a>プレステージ</a></div>
            <p>品番：</p><div><p>IPZZ-734</p></div>
            <p>ジャンル：</p><div><a>単体作品</a><a>ハイビジョン</a></div>
            <p>レーベル：</p><div><a>Prestige Premium</a></div>
          </section>
          <section><h2>商品紹介</h2><div><p>紹介文の一行目。<br>紹介文の二行目。</p></div></section>
          <section>
            <h2>サンプル画像</h2>
            <div><picture><img src="https://img.example/sample1.jpg?x=1"></picture></div>
          </section>
        </main>
      </body>
    </html>
    """
    monkeypatch.setattr(
        prestige,
        "request_get",
        lambda *args, **kwargs: make_response(html, url),
    )

    movie = MovieInfo("ipzz-734")
    prestige.parse_data(movie)

    assert movie.url == url
    assert movie.dvdid == "IPZZ-734"
    assert movie.title == "めちゃかわ彼女と濃密デート"
    assert movie.cover == "https://img.example/ipzz-734.jpg"
    assert movie.actress == ["白浜のぞみ", "蒼乃美月"]
    assert movie.duration == "120"
    assert movie.publish_date == "2026-05-02"
    assert movie.producer == "プレステージ"
    assert movie.genre == ["単体作品", "ハイビジョン"]
    assert movie.serial == "Prestige Premium"
    assert movie.plot == "紹介文の一行目。\n紹介文の二行目。"
    assert movie.preview_pics == ["https://img.example/sample1.jpg"]
    assert movie.uncensored is False


def test_mgstage_parses_ipzz_734_with_loose_classes(monkeypatch):
    url = "https://www.mgstage.com/product/product_detail/IPZZ-734/"
    html = """
    <html>
      <body>
        <div class="common_detail_cover renewed"><h1>IPZZ-734 めちゃかわ彼女と濃密デート</h1></div>
        <div class="detail_left renewed">
          <a id="EnlargeImage" href="https://image.mgstage.com/images/prestige/ipzz/734/pb_e_ipzz-734.jpg?cache=1">
            <img src="https://image.mgstage.com/thumb.jpg">
          </a>
          <table>
            <tr><th>出演：</th><td><a>白浜のぞみ</a><a>蒼乃美月</a></td></tr>
            <tr><th>メーカー：</th><td><a>プレステージ</a></td></tr>
            <tr><th>収録時間：</th><td>120min</td></tr>
            <tr><th>品番：</th><td>IPZZ-734</td></tr>
            <tr><th>配信開始日：</th><td>2026/05/02</td></tr>
            <tr><th>シリーズ：</th><td><a>Prestige Premium</a></td></tr>
            <tr><th>ジャンル：</th><td><a>単体作品</a><a>ハイビジョン</a></td></tr>
            <tr><td class="review"><span>レビュー</span>4.35点</td></tr>
          </table>
          <dl id="introduction"><dd><p>紹介文の一行目。<br>紹介文の二行目。</p><p class="more">more</p></dd></dl>
          <a class="sample_image updated" href="https://sample.example/1.jpg"></a>
          <a class="button_sample updated" href="/sampleplayer/player.php/IPZZ734"></a>
        </div>
      </body>
    </html>
    """

    monkeypatch.setattr(
        mgstage.request,
        "get",
        lambda *args, **kwargs: make_response(html, url),
    )

    class DummyCfg:
        def __init__(self):
            self.crawler = SimpleNamespace(hardworking=False)

    monkeypatch.setattr(mgstage, "Cfg", DummyCfg)

    movie = MovieInfo("ipzz-734")
    mgstage.parse_data(movie)

    assert movie.url == url
    assert movie.dvdid == "IPZZ-734"
    assert movie.title == "めちゃかわ彼女と濃密デート"
    assert movie.cover == "https://image.mgstage.com/images/prestige/ipzz/734/pb_e_ipzz-734.jpg"
    assert movie.actress == ["白浜のぞみ", "蒼乃美月"]
    assert movie.producer == "プレステージ"
    assert movie.duration == "120"
    assert movie.publish_date == "2026-05-02"
    assert movie.serial == "Prestige Premium"
    assert movie.genre == ["単体作品", "ハイビジョン"]
    assert movie.score == "8.70"
    assert movie.plot == "紹介文の一行目。\n紹介文の二行目。"
    assert movie.preview_pics == ["https://sample.example/1.jpg"]
    assert movie.uncensored is False
