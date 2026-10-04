"""覆盖只读查询、人工确认、冲突保护及词库文件更新。"""
import json
import subprocess
from types import SimpleNamespace

import pytest

from javsp import actress_lookup
from javsp.actress_alias import (
    AliasConflict, analyze_actress_sources, read_alias_document,
    save_alias_change, normalize_movie_info_actress,
)
from javsp.datatype import MovieInfo
from javsp.web.exceptions import MovieNotFoundError
from web_ui import web_server


@pytest.fixture
def alias_file(tmp_path, monkeypatch):
    path = tmp_path / 'actress_alias.json'
    path.write_text(json.dumps({'日文名甲': ['日文名甲', '旧名甲'], '乙': ['乙', '旧名乙']}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(web_server, 'ACTRESS_ALIAS_FILE', str(path))
    return path


def source(name, names, **kwargs):
    return dict(source=name, names=names, **kwargs)


def test_query_preserves_sources_and_does_not_write(alias_file, monkeypatch):
    raw = alias_file.read_bytes()
    monkeypatch.setattr(web_server, 'query_actress_sources', lambda dvdid: [
        source('a', ['旧名甲']), source('b', ['新译名']), source('c', ['新译名']),
        source('d', [], error='超时'),
    ])
    result = web_server.app.test_client().post('/api/actress-aliases/query', json={'dvdid': 'ABC-123'})
    body = result.get_json()
    assert result.status_code == 200
    assert body['name'] == body['original_name'] == '日文名甲'
    assert body['candidates'][1] == {'name': '新译名', 'sources': ['b', 'c']}
    assert len(body['sources']) == 4
    assert alias_file.read_bytes() == raw
    assert list(alias_file.parent.glob('*.bak')) == []


@pytest.mark.parametrize('sources,reason', [
    ([source('a', ['甲']), source('b', ['甲', '乙'])], '多位演员'),
    ([source('a', [])], '没有获取'),
    ([source('a', ['旧名甲']), source('b', ['旧名乙'])], '归属冲突'),
])
def test_query_failures_never_modify(alias_file, monkeypatch, sources, reason):
    raw = alias_file.read_bytes()
    monkeypatch.setattr(web_server, 'query_actress_sources', lambda _: sources)
    result = web_server.app.test_client().post('/api/actress-aliases/query', json={'dvdid': 'ABC-123'})
    assert result.status_code == 422
    assert reason in result.get_json()['error']
    assert alias_file.read_bytes() == raw


@pytest.mark.parametrize('confirmed', [None, False, 'true', 1])
def test_save_requires_explicit_confirmation(alias_file, confirmed):
    raw = alias_file.read_bytes()
    _, revision = read_alias_document(str(alias_file))
    result = web_server.app.test_client().post('/api/actress-aliases', json={
        'name': '日文名甲', 'original_name': '日文名甲', 'aliases': ['新译名'],
        'revision': revision, 'confirmed': confirmed,
    })
    assert result.status_code == 400
    assert alias_file.read_bytes() == raw


def test_confirm_append_backup_and_normalization(alias_file):
    before = alias_file.read_bytes()
    _, revision = read_alias_document(str(alias_file))
    result = web_server.app.test_client().post('/api/actress-aliases', json={
        'name': '日文名甲', 'original_name': '日文名甲',
        'aliases': [' 新译名 ', '新译名', ''], 'mode': 'append',
        'confirmed': True, 'revision': revision,
    })
    assert result.status_code == 200
    data, new_revision = read_alias_document(str(alias_file))
    assert set(data['日文名甲']) == {'日文名甲', '旧名甲', '新译名'}
    assert data['乙'] == ['乙', '旧名乙']
    assert new_revision != revision
    assert (alias_file.parent / result.get_json()['backup']).read_bytes() == before
    info = MovieInfo('ABC-123')
    info.actress = ['新译名']
    normalize_movie_info_actress(info, data)
    assert info.actress == ['日文名甲']


def test_manual_rename_delete_and_create(alias_file):
    _, revision = read_alias_document(str(alias_file))
    saved = save_alias_change(str(alias_file), revision=revision, confirmed=True,
                              name='新统一名', original_name='日文名甲', aliases=['新译名'], mode='edit')
    data, _ = read_alias_document(str(alias_file))
    assert '日文名甲' not in data
    assert data['新统一名'] == ['新统一名', '新译名', '日文名甲']
    assert saved['changes']['removed'] == ['旧名甲']
    saved = save_alias_change(str(alias_file), revision=saved['revision'], confirmed=True,
                              name='新女优', aliases=[], mode='edit')
    assert read_alias_document(str(alias_file))[0]['新女优'] == ['新女优']


@pytest.mark.parametrize('change', [
    {'name': '日文名甲', 'aliases': ['旧名乙'], 'original_name': '日文名甲'},
    {'name': '旧名乙', 'aliases': []},
    {'name': '日文名甲', 'aliases': []},
    {'name': '乙', 'aliases': [], 'original_name': '日文名甲'},
])
def test_conflict_does_not_overwrite(alias_file, change):
    raw = alias_file.read_bytes()
    _, revision = read_alias_document(str(alias_file))
    result = web_server.app.test_client().post('/api/actress-aliases', json={
        **change, 'revision': revision, 'confirmed': True,
    })
    assert result.status_code == 409
    assert alias_file.read_bytes() == raw


def test_stale_revision_rejected(alias_file):
    _, revision = read_alias_document(str(alias_file))
    save_alias_change(str(alias_file), revision=revision, confirmed=True, name='丙', aliases=[])
    raw = alias_file.read_bytes()
    with pytest.raises(AliasConflict, match='发生变化'):
        save_alias_change(str(alias_file), revision=revision, confirmed=True, name='丁', aliases=[])
    assert alias_file.read_bytes() == raw


def test_search_by_alias_and_record(alias_file):
    client = web_server.app.test_client()
    result = client.get('/api/actress-aliases?q=旧名甲')
    assert result.get_json()['records'][0]['name'] == '日文名甲'
    assert result.headers['Cache-Control'] == 'no-store'
    assert client.get('/api/actress-aliases?name=乙').get_json()['aliases'] == ['乙', '旧名乙']


def test_query_invalid_and_timeout(alias_file, monkeypatch):
    client = web_server.app.test_client()
    assert client.post('/api/actress-aliases/query', json={'dvdid': '../abc'}).status_code == 400
    def timeout(_):
        raise subprocess.TimeoutExpired('query', 180)
    monkeypatch.setattr(web_server, 'query_actress_sources', timeout)
    assert client.post('/api/actress-aliases/query', json={'dvdid': 'ABC-123'}).status_code == 504


def test_collect_raw_names_retry_and_mismatched_id(monkeypatch):
    calls = []
    def parse(info):
        calls.append(info)
        if len(calls) == 1:
            info.actress = ['不应保留的失败数据']
            raise ConnectionError('temporary')
        info.actress = [' 原始中文名 ', '原始中文名']
    monkeypatch.setattr(actress_lookup.importlib, 'import_module', lambda _: SimpleNamespace(parse_data=parse))
    result = actress_lookup.collect_source('fake', 'ABC-123', 3)
    assert result['names'] == ['原始中文名']
    assert len(calls) == 2
    def wrong(info):
        info.dvdid = 'XYZ-999'
        info.actress = ['错误女优']
    monkeypatch.setattr(actress_lookup.importlib, 'import_module', lambda _: SimpleNamespace(parse_data=wrong))
    result = actress_lookup.collect_source('fake', 'ABC-123', 3)
    assert result['names'] == []
    assert '不一致' in result['error']


def test_not_found_is_not_retried(monkeypatch):
    calls = []
    def parse(info):
        calls.append(info)
        raise MovieNotFoundError('fake', info.dvdid)
    monkeypatch.setattr(actress_lookup.importlib, 'import_module', lambda _: SimpleNamespace(parse_data=parse))
    assert actress_lookup.collect_source('fake', 'ABC-123', 3)['error']
    assert len(calls) == 1


def test_uses_enabled_crawlers_and_fc2_selection(monkeypatch):
    cfg = SimpleNamespace(crawler=SimpleNamespace(selection=SimpleNamespace(normal=['a', 'a', 'b'], fc2=['c'])),
                          network=SimpleNamespace(retry=3))
    monkeypatch.setattr(actress_lookup, 'Cfg', lambda: cfg)
    monkeypatch.setattr(actress_lookup, 'collect_source', lambda name, dvdid, retry: source(name, [dvdid]))
    assert [s['source'] for s in actress_lookup.collect_actress_sources('ABC-123')] == ['a', 'b']
    assert [s['source'] for s in actress_lookup.collect_actress_sources('FC2-123')] == ['c']


def test_atomic_replace_failure_keeps_original(alias_file, monkeypatch):
    from javsp import actress_alias
    raw = alias_file.read_bytes()
    _, revision = read_alias_document(str(alias_file))
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(actress_alias.os, 'replace', fail)
    with pytest.raises(OSError):
        save_alias_change(str(alias_file), revision=revision, confirmed=True, name='丙', aliases=[])
    assert alias_file.read_bytes() == raw
    assert not list(alias_file.parent.glob('.actress_alias-*'))


@pytest.mark.parametrize('site,markup', [
    ('airav', '<div class="oneVideo"><h5>ABC-1234</h5><a href="/ABC-1234">作品</a></div>'),
    ('ggjav', '<div class="item"><div class="item_title"><a href="/ABC-1234">ABC-1234</a></div></div>'),
    ('supjav', '<article class="post"><h2><a href="/ABC-1234">ABC-1234</a></h2></article>'),
    ('javguru', '<a href="/ABC-1234/">ABC-1234</a>'),
])
def test_alias_query_strict_search_rejects_partial_number(monkeypatch, site, markup):
    import importlib
    from lxml import html
    mod = importlib.import_module('javsp.web.' + site)
    monkeypatch.setattr(mod, 'get_html', lambda *_args, **_kwargs: html.fromstring(markup))
    with pytest.raises(MovieNotFoundError):
        mod.search_movie('ABC-123', strict=True)
    assert mod.search_movie('ABC-1234', strict=True)


def test_fc2_ppv_is_same_number():
    assert actress_lookup.normalize_id('FC2-PPV-123') == actress_lookup.normalize_id('FC2-123')


def test_query_subprocess_uses_supplied_configuration(tmp_path, monkeypatch):
    from pathlib import Path
    import yaml
    config = yaml.safe_load(Path('config.example.yml').read_text(encoding='utf-8'))
    config['crawler']['selection']['normal'] = []
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
    monkeypatch.setattr(web_server, 'GENERAL_CONFIG_FILE', str(path))
    assert web_server.query_actress_sources('ABC-123') == []


def test_new_draft_revision_does_not_return_dictionary(alias_file):
    response = web_server.app.test_client().get('/api/actress-aliases?revision_only=1')
    assert response.status_code == 200
    assert set(response.get_json()) == {'revision'}
    assert response.headers['Cache-Control'] == 'no-store'
