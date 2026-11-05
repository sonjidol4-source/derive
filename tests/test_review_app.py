import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

import app
import close_summary as cs
from common import atomic_json,config,stamp
from store import Store
from option_analytics import build,HISTORY
from test_option_analytics import Fixture,DAY


class ReviewAppTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name);self.data=self.root/'data';self.data.mkdir()
        self.cfg=config()

    def tearDown(self):self.temp.cleanup()

    def record(self,day='2026-10-29'):
        return dict(day=day,status='확정 추정',asked_at=day+'T17:00:00+09:00',
                    latest_time='17:00:00',last_change='16:08:00',qty={'17':86},amt={'17':210},
                    options={'generated_at':day+'T16:10:00+09:00','structure_text':'old 950'})

    def options(self,day='2026-10-29'):
        return dict(status='received',data_day=day,generated_at=day+'T16:20:00+09:00',
                    structure_text='new 1000',methodology={'id':'oi-structure-2','near_band_pct':7})

    def test_pending_updates_only_same_day_and_preserves_other_days(self):
        old=self.record('2026-10-28');today=self.record()
        atomic_json(self.data/cs.FILE,{'days':{old['day']:old,today['day']:today}})
        self.assertTrue(cs.refresh_pending_options(self.data,self.options(),stamp('2026-10-29T17:30:00+09:00'),self.cfg))
        saved=cs.load(self.data)
        self.assertEqual(saved['2026-10-28'],old)
        self.assertEqual(saved['2026-10-29']['options']['methodology']['near_band_pct'],7)
        self.assertFalse(cs.refresh_pending_options(self.data,self.options(),stamp('2026-10-29T17:31:00+09:00'),self.cfg))

    def test_final_and_past_records_unchanged(self):
        cs.save(self.record(),self.data);before=(self.data/cs.FILE).read_bytes()
        for now in ('2026-10-29T18:16:00+09:00','2026-10-30T08:00:00+09:00'):
            self.assertFalse(cs.refresh_pending_options(self.data,self.options(),stamp(now),self.cfg))
        self.assertEqual((self.data/cs.FILE).read_bytes(),before)

    def test_wrong_day_and_older_options_do_not_replace(self):
        cs.save(self.record(),self.data);before=(self.data/cs.FILE).read_bytes()
        stale=self.options();stale['generated_at']='2026-10-29T15:00:00+09:00'
        for options in (self.options('2026-10-28'),stale):
            self.assertFalse(cs.refresh_pending_options(self.data,options,stamp('2026-10-29T17:30:00+09:00'),self.cfg))
        self.assertEqual((self.data/cs.FILE).read_bytes(),before)

    def test_view_recalculation_is_labeled_without_writing(self):
        cs.save(self.record(),self.data);before=(self.data/cs.FILE).read_bytes()
        v=cs.view(self.data,cfg=self.cfg,current_options=self.options())
        self.assertEqual(v['latest']['structure_text'],'old 950')
        self.assertEqual(v['latest']['recalculated_options']['text'],'new 1000')
        self.assertIn('미기록',v['latest']['option_method_note'])
        self.assertEqual((self.data/cs.FILE).read_bytes(),before)
        self.assertIsNone(cs.view(self.data,cfg=self.cfg,current_options=self.options('2026-10-28'))['latest']['recalculated_options'])

    def test_option_preview_never_appends_history(self):
        f=Fixture(self.data);f.investors(DAY)
        result=build(self.data,860,'test',{},self.cfg,DAY,persist_history=False)
        self.assertEqual(result['status'],'received')
        self.assertFalse((self.data/HISTORY).exists())
        self.assertEqual(result['methodology']['near_band_pct'],7)

    def test_preview_no_engine_no_feeds_and_post_rejected(self):
        store=Store(self.data);store.close()
        atomic_json(self.data/'latest.json',{'day':'2026-10-29','session':True,'mode':'live'})
        before=(self.data/'latest.json').read_bytes()
        with patch.object(app,'ROOT',self.root),patch.object(app,'Engine',side_effect=AssertionError('Engine must not start')),patch.object(app,'RestFeed',side_effect=AssertionError('No feed')):
            a=app.Application('preview');a.run()
            self.assertFalse(a.snapshot['session'])
            self.assertEqual(a.mode,'preview')
            with self.assertRaises(Exception):a.engine.store.db.execute('DELETE FROM events')
            server=ThreadingHTTPServer(('127.0.0.1',0),app.handler_for(a))
            a.cfg=copy.deepcopy(a.cfg);a.cfg['port']=server.server_port;a.url='http://127.0.0.1:'+str(server.server_port)
            worker=threading.Thread(target=server.serve_forever);worker.start()
            try:
                req=Request(a.url+'/api/import',data=b'{}',method='POST')
                with self.assertRaises(HTTPError) as e:urlopen(req)
                self.assertEqual(e.exception.code,403)
                e.exception.close()
            finally:
                server.shutdown();worker.join();server.server_close();a.engine.store.close()
        self.assertEqual((self.data/'latest.json').read_bytes(),before)


if __name__=='__main__':unittest.main()
