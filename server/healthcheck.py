"""Container readiness probe: no credentials, cookies or archive information."""
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler


def main():
    public=urlsplit(os.environ['VIEWER_PUBLIC_ORIGIN'])
    port=int(os.environ.get('VIEWER_PORT','8080'))
    request=Request(f'http://127.0.0.1:{port}/healthz',headers={'Host':public.netloc})
    with build_opener(ProxyHandler({})).open(request,timeout=4) as response:
        value=json.loads(response.read(4096))
        if response.status!=200 or value.get('ok') is not True:raise SystemExit(1)


if __name__=='__main__':main()
