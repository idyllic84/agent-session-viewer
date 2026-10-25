"""Offline administration of the cloud database (run on the cloud host/container).

    python -m server.manage init-session-key --directory server/state
    python -m server.manage set-password --database server/state/cloud.db   # prompts, or --stdin
    python -m server.manage create-key --database server/state/cloud.db --name "work laptop"
    python -m server.manage create-key --database server/state/cloud.db --name "analysis" --kind query

Secrets are read from a prompt/stdin and are never echoed or logged, except a
newly generated API key, which is printed exactly once for you to copy.
"""
import argparse
import getpass
import os
from pathlib import Path
import secrets
import sys

from .archive import CloudArchive


def init_session_key(directory):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'session.key'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(secrets.token_urlsafe(48) + '\n'); stream.flush(); os.fsync(stream.fileno())
    print(f'Created private browser-session key: {path} (contents not logged)')


def read_password(use_stdin):
    if use_stdin:
        return sys.stdin.readline().rstrip('\r\n')
    first = getpass.getpass('New owner password (12+ characters): ')
    if first != getpass.getpass('Repeat: '):
        raise SystemExit('Passwords do not match')
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    keys = sub.add_parser('init-session-key'); keys.add_argument('--directory', default=str(Path(__file__).with_name('state')))
    password = sub.add_parser('set-password', help='Set/reset the owner password (signs out all browsers)')
    password.add_argument('--database', required=True); password.add_argument('--stdin', action='store_true')
    create = sub.add_parser('create-key', help='Generate an upload key (one computer) or a read-only query key')
    create.add_argument('--database', required=True); create.add_argument('--name', required=True)
    create.add_argument('--expires-days', type=int)
    create.add_argument('--kind', choices=('upload', 'query'), default='upload')
    args = parser.parse_args(argv)
    if args.command == 'init-session-key':
        init_session_key(args.directory)
    elif args.command == 'set-password':
        archive = CloudArchive(args.database)
        archive.set_password(read_password(args.stdin), require_current=False)
        Path(args.database).with_name('initial-admin-password').unlink(missing_ok=True)  # now obsolete
        print('Owner password updated; existing browser sessions are signed out.')
    else:
        value = CloudArchive(args.database).create_key(args.name, expires_days=args.expires_days, kind=args.kind)
        print(f"{value['kind']} API key for {value['name']} (shown once):\n{value['key']}")


if __name__ == '__main__':
    main()
