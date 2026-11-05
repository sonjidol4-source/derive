import json
import shutil
import subprocess
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from app import handler_for


class LocalSessionTests(unittest.TestCase):
    def setUp(self):
        self.queued=[]
        self.app=SimpleNamespace(mode='live',cfg={'port':0},token='server-test-session',
            recovery=SimpleNamespace(request=lambda ranges:self.queued.append(ranges) or True))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.app))
        self.app.cfg['port']=self.server.server_port
        self.app.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()

    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close()

    def post(self,session=None,origin=None):
        headers={'Content-Type':'application/json'}
        if session:headers['Authorization']='Bearer '+session
        if origin:headers['Origin']=origin
        request=Request(self.app.url+'/api/recovery',data=b'{}',headers=headers,method='POST')
        try:
            with urlopen(request) as response:return response.status,json.load(response)
        except HTTPError as error:
            with error:return error.code,json.load(error)

    def test_rejected_old_session_does_not_enqueue_and_bootstrap_restores_the_request(self):
        status,body=self.post('previous-test-session',self.app.url)
        self.assertEqual((status,body['code']),(401,'session_expired'));self.assertEqual(self.queued,[])
        with urlopen(self.app.url+'/api/bootstrap') as response:fresh=json.load(response)['token']
        status,body=self.post(fresh,self.app.url)
        self.assertEqual(status,200);self.assertTrue(body['accepted']);self.assertEqual(self.queued,[None])

    def test_missing_session_is_rejected_before_the_operation(self):
        status,body=self.post()
        self.assertEqual((status,body['code']),(401,'session_expired'));self.assertEqual(self.queued,[])

    def test_foreign_origin_stays_blocked_even_with_a_valid_session(self):
        status,body=self.post(self.app.token,'https://unrelated.example')
        self.assertEqual((status,body['code']),(403,'origin_rejected'));self.assertEqual(self.queued,[])

    def test_preview_stays_read_only(self):
        self.app.mode='preview';status,body=self.post(self.app.token,self.app.url)
        self.assertEqual(status,403);self.assertIn('미리보기',body['error']);self.assertEqual(self.queued,[])


class BrowserSessionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node is required for browser request checks')
    def test_request_recovery_scenarios(self):
        result=subprocess.run([shutil.which('node'),'--test',str(Path(__file__).with_name('session_recovery.test.js'))],
            capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)


if __name__=='__main__':unittest.main()
