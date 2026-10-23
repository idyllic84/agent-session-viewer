"""Docker bootstrap: persistent private secrets + Waitress, no source scanning."""
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .archive import CloudArchive
from .app import create_app


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database: Path
    owner_file: Path
    session_file: Path
    public_origin: str
    local_http: bool
    port: int
    threads: int
    max_request: int
    owner_external: bool
    session_external: bool


def settings(environ=None):
    env=os.environ if environ is None else environ
    origin=env.get('VIEWER_PUBLIC_ORIGIN','').strip().rstrip('/')
    parsed=urlsplit(origin);local=env.get('VIEWER_LOCAL_HTTP','0')=='1'
    if not origin or parsed.path or parsed.query or parsed.fragment or parsed.username or not parsed.netloc:
        raise ValueError('VIEWER_PUBLIC_ORIGIN must be an origin without credentials or a path')
    if local:
        if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1','::1'):
            raise ValueError('VIEWER_LOCAL_HTTP=1 requires a loopback-only http public origin')
    elif parsed.scheme!='https':raise ValueError('Production public origin must use HTTPS')
    data=Path(env.get('VIEWER_DATA_DIR','/data'))
    if not data.is_absolute() or data.resolve()==Path(data.anchor):raise ValueError('Use a dedicated absolute data directory')
    data=data.resolve();database=Path(env.get('VIEWER_DATABASE',str(data/'cloud.db'))).resolve()
    if not database.is_relative_to(data):raise ValueError('Database must stay in VIEWER_DATA_DIR')
    owner_external=bool(env.get('VIEWER_OWNER_KEY_FILE'));session_external=bool(env.get('VIEWER_SESSION_KEY_FILE'))
    owner=Path(env.get('VIEWER_OWNER_KEY_FILE',str(data/'owner.key')))
    session=Path(env.get('VIEWER_SESSION_KEY_FILE',str(data/'session.key')))
    port=int(env.get('VIEWER_PORT','8080'));threads=int(env.get('VIEWER_THREADS','8'))
    maximum=int(env.get('VIEWER_MAX_REQUEST_MIB','32'))*1024*1024
    if not 1<=port<=65535 or not 1<=threads<=64 or maximum<1024*1024:raise ValueError('Invalid port, threads or request limit')
    if owner.resolve()==session.resolve():raise ValueError('Owner and session key files must be different')
    return Settings(data,database,owner,session,origin,local,port,threads,maximum,owner_external,session_external)


def private_key(path,*,external=False):
    if not external:
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:pass
        else:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(48)+'\n');stream.flush();os.fsync(stream.fileno())
    value=path.read_text(encoding='utf-8').strip()
    if len(value)<32:raise ValueError('Key file must contain a random secret of at least 32 characters')
    return value


def application(environ=None):
    config=settings(environ)
    config.data_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    owner=private_key(config.owner_file,external=config.owner_external)
    session=private_key(config.session_file,external=config.session_external)
    archive=CloudArchive(config.database,max_request=config.max_request)
    app=create_app(archive,admin_key=owner,session_secret=session,
                   public_origin=config.public_origin,allow_http=config.local_http)
    app.config['CLOUD_ARCHIVE']=archive
    return app,config


def main():
    from waitress import serve
    os.umask(0o077)
    app,config=application()
    # No secret contents or environment dump. The public URL is not a token URL.
    print(f'[cloud] ready for {config.public_origin}; private data in {config.data_dir}',flush=True)
    serve(app,host='0.0.0.0',port=config.port,threads=config.threads,
          max_request_body_size=config.max_request,clear_untrusted_proxy_headers=True)


if __name__=='__main__':main()
