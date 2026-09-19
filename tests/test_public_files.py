import subprocess
import sys
from pathlib import Path

import pytest


CHECKER = Path(__file__).resolve().parents[1] / 'tools' / 'check_public_files.py'


def run_git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path):
    run_git(tmp_path, 'init')
    run_git(tmp_path, 'config', 'user.name', 'Test')
    run_git(tmp_path, 'config', 'user.email', 'test@example.com')
    return tmp_path


@pytest.mark.parametrize('name,content,blocked', [
    ('config.yml', 'network: {}', True),
    ('web_ui/web_config.yml', 'scheduler: {}', True),
    ('notes.txt', 'ghp_' + 'a' * 36, True),
    ('notes.txt', '/' + 'Users' + '/someone/project/', True),
    ('config.example.yml', 'network:\n  javdb_cookie: null\n', False),
])
def test_staged_privacy_check(repository, name, content, blocked):
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    run_git(repository, 'add', '.')
    result = subprocess.run([sys.executable, str(CHECKER)], cwd=repository, capture_output=True)
    assert bool(result.returncode) is blocked
    if blocked:
        assert content.encode() not in result.stderr


def test_push_checks_deleted_secrets_in_history(repository):
    path = repository / 'private.txt'
    path.write_text('ghp_' + 'a' * 36)
    run_git(repository, 'add', '.')
    run_git(repository, 'commit', '-m', 'First')
    run_git(repository, 'rm', 'private.txt')
    run_git(repository, 'commit', '-m', 'Remove')
    head = run_git(repository, 'rev-parse', 'HEAD').stdout.decode().strip()
    result = subprocess.run(
        [sys.executable, str(CHECKER), '--push'], cwd=repository,
        input=f'refs/heads/main {head} refs/heads/main {"0" * 40}\n',
        text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert 'private.txt' in result.stderr
    assert 'a' * 36 not in result.stderr
