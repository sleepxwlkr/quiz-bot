import json
import threading
import unittest
from unittest.mock import patch

from quizbot.telegram import TelegramClient, TelegramError


class Response:
    status=200
    will_close=False

    def __init__(self,payload=None):
        self.payload=payload or {'ok':True,'result':{'id':1}}

    def read(self):
        return json.dumps(self.payload).encode()


class Connection:
    def __init__(self,*args,**kwargs):
        self.sock=None
        self.calls=[]
        self.response=Response()
        self.closed=False

    def request(self,*args,**kwargs):
        self.calls.append((args,kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed=True


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.proxy=patch('quizbot.telegram.urllib.request.getproxies',return_value={})
        self.proxy.start()
        self.factory=patch('quizbot.telegram.http.client.HTTPSConnection',side_effect=Connection)
        self.create=self.factory.start()
        self.client=TelegramClient('123:private-token')

    def tearDown(self):
        self.client.close()
        self.factory.stop()
        self.proxy.stop()

    def test_connection_is_reused_across_requests_and_worker_threads(self):
        self.client.call('getMe')
        results=[]
        worker=threading.Thread(target=lambda:results.append(self.client.call('getMe')))
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results,[{'id':1}])
        self.assertEqual(self.create.call_count,1)

    def test_polling_does_not_take_the_warmed_message_connection(self):
        self.client.call('getMe')
        ready=threading.Event()
        release=threading.Event()
        def poll():
            self.client.call('getUpdates',timeout=20)
            ready.set()
            release.wait(1)
            self.client.close()
        worker=threading.Thread(target=poll)
        worker.start()
        try:
            self.assertTrue(ready.wait(1))
            self.client.call('sendMessage',chat_id=1,text='Hello')
            self.assertEqual(self.create.call_count,2)
        finally:
            release.set()
            worker.join(timeout=1)

    def test_api_error_redacts_token_and_preserves_retry_after(self):
        self.client.call('getMe')
        connection=self.client._idle.get_nowait()
        connection.response=Response({'ok':False,'error_code':429,
            'description':'123:private-token rate limited','parameters':{'retry_after':3}})
        self.client._idle.put(connection)
        with self.assertRaises(TelegramError) as raised:
            self.client.call('sendMessage',chat_id=1,text='test')
        self.assertEqual(raised.exception.retry_after,3)
        self.assertNotIn('private-token',str(raised.exception))

    def test_network_error_discards_socket_without_automatic_resend(self):
        self.client.call('getMe')
        connection=self.client._idle.get_nowait()
        connection.request=lambda *args,**kwargs: (_ for _ in ()).throw(OSError('private-token'))
        self.client._idle.put(connection)
        with self.assertRaises(TelegramError) as raised:
            self.client.call('sendMessage',chat_id=1,text='test')
        self.assertEqual(raised.exception.code,0)
        self.assertNotIn('private-token',str(raised.exception))
        self.assertTrue(connection.closed)
        self.assertEqual(self.create.call_count,1)
        self.client.call('getMe')
        self.assertEqual(self.create.call_count,2)
