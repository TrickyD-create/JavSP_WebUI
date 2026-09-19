"""Check staged files or outgoing commits without printing secret values."""
import re
import subprocess
import sys
from pathlib import PurePosixPath


def git(*args):
    return subprocess.check_output(['git', *args])


def private_path(name):
    p = PurePosixPath(name)
    return (
        p.name in {'config.yml', 'web_config.yml', '.envrc', '.DS_Store', 'AGENTS.md', 'build_log.txt'}
        or (p.name.startswith('.env') and p.name != '.env.example')
        or any(part in {'.git', '.venv', '.trae', '.codex', '.agents', 'dist', 'private-backups'} for part in p.parts)
        or p.suffix in {'.db', '.sqlite', '.sqlite3', '.log', '.pem', '.key'}
        or bool(re.search(r'\.(?:db|sqlite3?)-(?:wal|shm|journal)$', p.name))
    )


SECRET = re.compile(
    rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
    rb'|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}'
    rb'|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
    rb'|/(?:Users|home)/[A-Za-z0-9_.-]+/)'
)


def check(revision=None):
    if revision:
        names = git('ls-tree', '-r', '--name-only', '-z', revision)
    else:
        names = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
    bad = []
    for raw in names.split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8', errors='surrogateescape')
        if private_path(name):
            bad.append(name + ': private file')
            continue
        content = git('show', (revision or '') + ':' + name)
        if b'\0' not in content and SECRET.search(content):
            bad.append(name + ': possible credential or personal path')
    if bad:
        print('Publication blocked; inspect these files locally:', file=sys.stderr)
        print('\n'.join(bad), file=sys.stderr)
    return bool(bad)


def main():
    if '--push' not in sys.argv:
        return int(check())
    revisions = set()
    for line in sys.stdin:
        _, local_sha, _, remote_sha = line.split()
        if set(local_sha) == {'0'}:
            continue
        args = ['rev-list', local_sha]
        if set(remote_sha) != {'0'}:
            args.append('^' + remote_sha)
        # If a remote base is unavailable locally, scan the entire outgoing history.
        try:
            commits = git(*args)
        except subprocess.CalledProcessError:
            commits = git('rev-list', local_sha)
        revisions.update(commits.decode().splitlines())
    failed = False
    for revision in revisions:
        failed = check(revision) or failed
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
