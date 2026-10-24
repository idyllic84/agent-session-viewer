"""HTTPS client for the cloud ingest API (protocol 6).

Verified TLS, no redirects, no proxy/environment credential injection, no
cookies; the API key is sent only to the configured origin as a Bearer token.
Transient failures retry the SAME body/key with exponential backoff + jitter.
"""
import random
import time
from threading import Lock, local as thread_local
from urllib.parse import urlsplit

import requests

from server.contracts.protocol import Problem, INGEST_VERSION
from .credentials import validate_key

LOOPBACK = ('localhost', '127.0.0.1', '::1')


def normalize_origin(url):
    if not isinstance(url, str):
        raise ValueError('Cloud address is required')
    url = url.strip().rstrip('/'); parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or not parsed.hostname:
        raise ValueError('Use only the cloud origin, e.g. https://history.example.com (no path, query or credentials)')
    if parsed.scheme == 'https':
        return url
    if parsed.scheme == 'http' and parsed.hostname in LOOPBACK:
        return url  # same-machine Docker deployment only; traffic never leaves this computer
    raise ValueError('Cloud address must use HTTPS (plain HTTP is allowed only for localhost)')


class CloudClient:
    def __init__(self, url, key, *, budget_seconds=120, attempts=5):
        self.url = normalize_origin(url); self.key = validate_key(key)
        self.budget = budget_seconds; self.attempts = attempts
        self._thread = thread_local(); self._sessions = []; self._lock = Lock()

    def session(self):
        if not hasattr(self._thread, 'http'):
            http = requests.Session(); http.trust_env = False; self._thread.http = http
            with self._lock:
                self._sessions.append(http)
        return self._thread.http

    def close(self):
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for http in sessions:
            http.close()

    def call(self, method, path, *, retry=True, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.key, 'Accept': 'application/json', **kwargs.pop('headers', {})}
        deadline = time.monotonic() + self.budget; attempts = self.attempts if retry else 1
        for attempt in range(attempts):
            delay = 0
            try:
                left = max(1, deadline - time.monotonic())
                response = self.session().request(method, self.url + path, headers=headers, timeout=(min(10, left), min(90, left)),
                                                  allow_redirects=False, **kwargs)
                try:
                    if 300 <= response.status_code < 400:
                        raise Problem('REDIRECT_REFUSED', 'The cloud address redirected; enter the final HTTPS origin', 400)
                    try:
                        value = response.json()
                    except ValueError:
                        raise Problem('INVALID_RESPONSE', f'Cloud returned a non-JSON response (HTTP {response.status_code})', 502)
                    if response.ok:
                        return value
                    error = value.get('error', {}) if isinstance(value, dict) else {}
                    code = error.get('code', 'HTTP_ERROR') if isinstance(error, dict) else 'HTTP_ERROR'
                    message = error.get('message', 'Cloud request failed') if isinstance(error, dict) else 'Cloud request failed'
                    if response.status_code not in (429, 500, 502, 503, 504) or attempt == attempts - 1 or time.monotonic() >= deadline:
                        problem = Problem(code, message, response.status_code)
                        problem.details = error if isinstance(error, dict) else {}
                        raise problem
                    try:
                        delay = max(0, float(response.headers.get('Retry-After', 0)))
                    except ValueError:
                        delay = 0
                finally:
                    response.close()
            except requests.exceptions.SSLError as exc:
                raise Problem('TLS_FAILED', 'TLS certificate verification failed for the cloud address', 495) from exc
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == attempts - 1 or time.monotonic() >= deadline:
                    raise Problem('NETWORK_UNAVAILABLE', 'Cloud unreachable; nothing local was lost, retry later', 503) from exc
            delay = max(delay, random.uniform(0, 2 ** attempt))
            if time.monotonic() + delay >= deadline:
                raise Problem('NETWORK_UNAVAILABLE', 'Retry budget exhausted; the batch will be resent unchanged next time', 503)
            time.sleep(delay)

    def status(self, dataset):
        value = self.call('GET', '/ingest/v1/status', params={'datasetId': dataset})
        if value.get('protocolVersion') != INGEST_VERSION:
            raise Problem('PROTOCOL_VERSION', 'Cloud ingest protocol is not compatible; upgrade both servers', 426)
        return value

    def bind(self, dataset):
        return self.call('POST', '/ingest/v1/bind', json={'datasetId': dataset})

    def send(self, body, key):
        return self.call('POST', '/ingest/v1/batches', data=body,
                         headers={'Content-Type': 'application/json', 'Idempotency-Key': key})
