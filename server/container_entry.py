"""Docker bootstrap: persistent session secret, owner password, Waitress.

Owner password bootstrap (first start only, never overwrites an existing one):
1. VIEWER_ADMIN_PASSWORD_FILE  -> a private file you provide (Docker secret), or
2. otherwise a random password is generated into <data>/initial-admin-password
   (mode 0600). Sign in with it, change it in the console; the file is then
   deleted. Its contents are never logged.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .archive import CloudArchive
from .app import create_app

INITIAL_PASSWORD = 'initial-admin-password'


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database: Path
    session_file: Path
    password_file: Path | None
    public_origin: str
    local_http: bool
    port: int
    threads: int
    max_request: int
    session_external: bool


def settings(environ=None):
    env = os.environ if environ is None else environ
    origin = env.get('VIEWER_PUBLIC_ORIGIN', '').strip().rstrip('/')
    parsed = urlsplit(origin); local = env.get('VIEWER_LOCAL_HTTP', '0') == '1'
    if not origin or parsed.path or parsed.query or parsed.fragment or parsed.username or not parsed.netloc:
        raise ValueError('VIEWER_PUBLIC_ORIGIN must be an origin without credentials or a path')
    if local:
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('VIEWER_LOCAL_HTTP=1 requires a loopback-only http public origin')
    elif parsed.scheme != 'https':
        raise ValueError('Production public origin must use HTTPS')
    data = Path(env.get('VIEWER_DATA_DIR', '/data'))
    if not data.is_absolute() or data.resolve() == Path(data.anchor):
        raise ValueError('Use a dedicated absolute data directory')
    data = data.resolve(); database = Path(env.get('VIEWER_DATABASE', str(data / 'cloud-v6.db'))).resolve()
    if not database.is_relative_to(data):
        raise ValueError('Database must stay in VIEWER_DATA_DIR')
    session_external = bool(env.get('VIEWER_SESSION_KEY_FILE'))
    session = Path(env.get('VIEWER_SESSION_KEY_FILE', str(data / 'session.key')))
    password = Path(env['VIEWER_ADMIN_PASSWORD_FILE']) if env.get('VIEWER_ADMIN_PASSWORD_FILE') else None
    port = int(env.get('VIEWER_PORT', '8080')); threads = int(env.get('VIEWER_THREADS', '8'))
    maximum = int(env.get('VIEWER_MAX_REQUEST_MIB', '32')) * 1024 * 1024
    if not 1 <= port <= 65535 or not 1 <= threads <= 64 or maximum < 1024 * 1024:
        raise ValueError('Invalid port, threads or request limit')
    return Settings(data, database, session, password, origin, local, port, threads, maximum, session_external)


def private_secret(path, *, external=False):
    if not external:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(48) + '\n'); stream.flush(); os.fsync(stream.fileno())
    value = path.read_text(encoding='utf-8').strip()
    if len(value) < 32:
        raise ValueError('Secret file must contain a random value of at least 32 characters')
    return value


def bootstrap_password(archive, config, log=print):
    initial = config.data_dir / INITIAL_PASSWORD
    if archive.has_password():
        if initial.exists() and archive.password_version() > 1:
            initial.unlink()  # owner already replaced the generated password
        return
    if config.password_file is not None:
        archive.set_password(config.password_file.read_text(encoding='utf-8').strip(), require_current=False)
        log('[cloud] owner password initialised from VIEWER_ADMIN_PASSWORD_FILE')
        return
    password = secrets.token_urlsafe(18)
    fd = os.open(initial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(password + '\n'); stream.flush(); os.fsync(stream.fileno())
    archive.set_password(password, require_current=False)
    log(f'[cloud] generated an initial owner password in {initial} (contents not logged); change it after signing in')


def application(environ=None, log=print):
    config = settings(environ)
    config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = private_secret(config.session_file, external=config.session_external)
    archive = CloudArchive(config.database, max_request=config.max_request)
    bootstrap_password(archive, config, log)
    app = create_app(archive, session_secret=session, public_origin=config.public_origin, allow_http=config.local_http)
    app.config['INITIAL_PASSWORD_FILE'] = config.data_dir / INITIAL_PASSWORD
    return app, config


def main():
    from waitress import serve
    os.umask(0o077)
    app, config = application()
    print(f'[cloud] ready for {config.public_origin}; private data in {config.data_dir}', flush=True)
    serve(app, host='0.0.0.0', port=config.port, threads=config.threads,
          max_request_body_size=config.max_request, clear_untrusted_proxy_headers=True)


if __name__ == '__main__':
    main()
