"""Telegram API with pooled HTTPS connections and a separate polling socket."""
import base64
import http.client
import json
import logging
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger('quizbot')


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        self.code = code
        self.description = description
        self.retry_after = retry_after
        super().__init__('Telegram API error %s: %s'%(code,description))


class TelegramClient:
    def __init__(self, token):
        self._token = token
        self._local = threading.local()
        self._idle = queue.LifoQueue()
        self._context = ssl.create_default_context()
        proxies = urllib.request.getproxies()
        self._proxy = proxies.get('https') if not urllib.request.proxy_bypass('api.telegram.org') else None
        # urllib retains compatibility with HTTPS proxies (nested TLS).
        self._urllib_proxy = bool(self._proxy and
            urllib.parse.urlsplit(self._proxy if '://' in self._proxy else 'http://'+self._proxy).scheme!='http')

    def close(self):
        self._discard_connection()
        self._discard_connection(polling=True)
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return

    def _discard_connection(self, polling=False):
        attribute = 'poll_connection' if polling else 'connection'
        connection = getattr(self._local,attribute,None)
        if connection is not None:
            connection.close()
            setattr(self._local,attribute,None)

    def _connection(self, timeout, polling=False):
        attribute = 'poll_connection' if polling else 'connection'
        connection = getattr(self._local,attribute,None)
        if connection is None and not polling:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                pass
        if connection is None:
            if self._proxy:
                proxy = urllib.parse.urlsplit(self._proxy if '://' in self._proxy else 'http://'+self._proxy)
                connection = http.client.HTTPSConnection(proxy.hostname,proxy.port or 80,timeout=timeout,context=self._context)
                headers = {}
                if proxy.username is not None:
                    credentials = '%s:%s'%(urllib.parse.unquote(proxy.username),urllib.parse.unquote(proxy.password or ''))
                    headers['Proxy-Authorization'] = 'Basic '+base64.b64encode(credentials.encode()).decode('ascii')
                connection.set_tunnel('api.telegram.org',443,headers)
            else:
                connection = http.client.HTTPSConnection('api.telegram.org',timeout=timeout,context=self._context)
        setattr(self._local,attribute,connection)
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
        return connection

    def _request(self, method, params, timeout):
        connection = self._connection(timeout,polling=method=='getUpdates')
        connection.request('POST','/bot%s/%s'%(self._token,method),
                           body=json.dumps(params).encode('utf-8'),headers={'Content-Type':'application/json'})
        response = connection.getresponse()
        body = response.read()  # Read to the end so the next request can reuse the connection.
        if response.will_close:
            self._discard_connection(polling=method=='getUpdates')
        try:
            return json.loads(body)
        except ValueError:
            raise TelegramError(response.status,'Invalid API response') from None

    def _request_via_urllib(self, method, params, timeout):
        request = urllib.request.Request('https://api.telegram.org/bot%s/%s'%(self._token,method),
            data=json.dumps(params).encode('utf-8'),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode('utf-8'))
            except ValueError:
                raise TelegramError(exc.code,'HTTP request failed') from None

    def call(self, method, **params):
        started = time.perf_counter()
        timeout = max(12,int(params.get('timeout',0))+5)
        try:
            result = self._request_via_urllib(method,params,timeout) if self._urllib_proxy else self._request(method,params,timeout)
        except (OSError,ValueError,http.client.HTTPException):
            # Do not silently resend sendMessage after an ambiguous network failure.
            self._discard_connection(polling=method=='getUpdates')
            raise TelegramError(0,'Connection failed; retrying') from None
        finally:
            log.debug('Telegram %s: %.0f ms',method,(time.perf_counter()-started)*1000)
            if method!='getUpdates':
                connection = getattr(self._local,'connection',None)
                if connection is not None:
                    self._local.connection = None
                    self._idle.put(connection)
        if not result.get('ok'):
            description = str(result.get('description','Unknown error')).replace(self._token,'[redacted]')
            raise TelegramError(result.get('error_code',0),description,result.get('parameters',{}).get('retry_after',0))
        return result['result']
