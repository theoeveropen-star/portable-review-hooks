"""Model egress: a fixed HTTP CONNECT proxy, or direct HTTPS.

COMPANION_MODEL_EGRESS selects the mode:
  proxy  - always through the proxy file; a missing or failed proxy never falls back direct;
  direct - straight to the destination, the proxy file is never read;
  unset  - proxy when the proxy file exists, otherwise direct (decided per call, no fallback
           after a proxy failure).
The proxy file is COMPANION_MODEL_EGRESS_FILE (default /var/lib/companion-model-egress/http-proxy.json).
Account auth and TLS stay between the native client and its original destination.
Only the proxy credential file is loaded here; its contents are never logged.
"""
import base64
import http.client
import ipaddress
import json
import os
from pathlib import Path
import ssl
import stat
from urllib.parse import quote

CREDENTIAL = Path(os.environ.get('COMPANION_MODEL_EGRESS_FILE') or '/var/lib/companion-model-egress/http-proxy.json')
MODE = (os.environ.get('COMPANION_MODEL_EGRESS') or '').strip().lower()
if MODE not in ('', 'proxy', 'direct'):
    raise ValueError('model_egress_mode_invalid')


def _direct():
    if MODE == 'direct':
        return True
    if MODE == 'proxy':
        return False
    try:
        os.lstat(CREDENTIAL)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
LOCAL_BYPASS = 'localhost,127.0.0.1,::1'


def _configuration():
    try:
        fd = os.open(CREDENTIAL, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096
                    or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
                raise ValueError()
            value = json.load(stream)
        if (set(value) != {'version', 'scheme', 'host', 'port', 'username', 'password'}
                or value['version'] != 1 or value['scheme'] != 'http'
                or type(value['port']) is not int or not 0 < value['port'] < 65536):
            raise ValueError()
        ipaddress.IPv4Address(value['host'])
        for key in ('username', 'password'):
            item = value[key]
            if not isinstance(item, str) or not item or any(ord(c) < 32 for c in item):
                raise ValueError()
        if ':' in value['username']:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise OSError('fixed_egress_configuration_unavailable') from None


def https_connection(host, port=443, *, timeout, context=None):
    """Preserve the caller's TLS context, timeout and destination; CONNECT only."""
    if not isinstance(host, str) or not host or any(c in host for c in '\r\n/@'):
        raise OSError('fixed_egress_destination_invalid')
    if _direct():
        connection = http.client.HTTPSConnection(
            host, port, timeout=timeout,
            context=context if context is not None else ssl.create_default_context(),
        )
        connection.set_debuglevel(0)
        return connection
    value = _configuration()
    token = base64.b64encode((value['username'] + ':' + value['password']).encode()).decode('ascii')
    connection = http.client.HTTPSConnection(
        value['host'], value['port'], timeout=timeout,
        context=context if context is not None else ssl.create_default_context(),
    )
    connection.set_debuglevel(0)
    connection.set_tunnel(host, port, {'Proxy-Authorization': 'Basic ' + token})
    return connection


def native_environment(environment):
    """Native HTTP(S) proxy settings; local Brain/relay endpoints bypass it."""
    if _direct():
        return dict(environment)
    value = _configuration()
    proxy = 'http://{}:{}@{}:{}'.format(
        quote(value['username'], safe=''), quote(value['password'], safe=''),
        value['host'], value['port'],
    )
    result = dict(environment)
    for key in ('ALL_PROXY', 'all_proxy'):
        result.pop(key, None)
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        result[key] = proxy
    result['NO_PROXY'] = result['no_proxy'] = LOCAL_BYPASS
    return result
