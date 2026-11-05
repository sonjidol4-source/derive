import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from common import config,stamp
from engine import Engine
from gap_recovery import recover,read_view,targets,normalize_ranges,FILE


class GapRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.e=Engine(config(),self.path)
        self.day=dt.date(2026,10,30)
        self.at=stamp('2026-10-30T10:00:00+09:00')
        self.e.ingest({'asof':self.at.isoformat(),'source':'live','fut_code':'F1','values':{'k200':500,'foreign_fut_qty':0,'foreign_spot_amt':100}},self.at)
        self.e.compute(self.at)

    def tearDown(self):self.e.store.close();self.temp.cleanup()

    def pack(self):
        ts=[self.at+dt.timedelta(minutes=i) for i in range(4)]
        series={'k200':[(t,600+i) for i,t in enumerate(ts)],'foreign_fut_qty':[(t,300+i) for i,t in enumerate(ts)],
                'fut_price':[(t,601+i) for i,t in enumerate(ts)],'fut_oi':[(t,100000+i) for i,t in enumerate(ts)],
                'nonarb_amt':[(t,-100+i) for i,t in enumerate(ts)],'foreign_spot_amt':[(t,200+i) for i,t in enumerate(ts)]}
        return {'day':self.day,'code':'F1','series':series,'sources':{k:'LS historical' for k in series},'report':{'day':str(self.day),'ls_calls':0}}

    def rows(self):return [tuple(r) for r in self.e.store.db.execute('SELECT * FROM ticks ORDER BY at')]

    def test_missing_fields_are_archived_without_overwriting_live_or_creating_events(self):
        before=self.rows();events=self.e.store.events()
        r=recover(self.day,config(),self.path,[('10:00','10:03')],pack=self.pack())
        self.assertGreater(r['added_fields'],0);self.assertEqual(self.rows(),before)
        self.assertEqual(self.e.store.events(),events)
        self.assertTrue(Path(r['backup']).exists())
        v=read_view(self.path,self.day)
        original_minute=next(h for h in v['history'] if h['at'].startswith('2026-10-30T10:00'))
        self.assertNotIn('k200',original_minute['values'])
        self.assertNotIn('foreign_fut_qty',original_minute['values'])  # valid zero is preserved
        self.assertIn('fut_price',original_minute['values'])  # fill a partial minute
        self.assertIn('foreign_spot_krx_amt',original_minute['values'])
        self.assertNotIn('foreign_spot_amt',original_minute['values'])

    def test_repeat_recovery_is_idempotent_and_old_archive_values_are_preserved(self):
        first=recover(self.day,config(),self.path,[('10:01','10:03')],pack=self.pack())
        second=recover(self.day,config(),self.path,[('10:01','10:03')],pack=self.pack())
        self.assertGreater(first['added_fields'],0);self.assertEqual(second['added_fields'],0)
        self.assertEqual(second['added_minutes'],0)

    def test_requested_ranges_only_and_no_interpolation(self):
        pack=self.pack();pack['series']['k200']=[(self.at+dt.timedelta(minutes=3),603)]
        recover(self.day,config(),self.path,[('10:01','10:02')],pack=pack)
        view=read_view(self.path,self.day)
        self.assertTrue(all('10:01' in h['at'] or '10:02' in h['at'] for h in view['history']))
        self.assertTrue(all('k200' not in h['values'] for h in view['history']))
        self.assertNotIn('basis_theory',view['counts'])

    def test_dry_run_does_not_create_archive_or_touch_ticks(self):
        before=self.rows()
        r=recover(self.day,config(),self.path,[('10:01','10:03')],pack=self.pack(),dry=True)
        self.assertGreater(r['proposed_fields'],0)
        self.assertFalse((self.path/FILE).exists());self.assertEqual(self.rows(),before)

    def test_wrong_day_and_contract_are_not_silently_merged(self):
        pack=self.pack();pack['day']=dt.date(2026,10,29)
        with self.assertRaises(ValueError):recover(self.day,config(),self.path,[('10:01','10:03')],pack=pack)
        pack=self.pack();pack['code']='F2'
        r=recover(self.day,config(),self.path,[('10:01','10:03')],pack=pack)
        self.assertNotIn('fut_price',r['counts']);self.assertNotIn('foreign_fut_qty',r['counts'])
        self.assertIn('k200',r['counts'])

    def test_live_signal_history_never_loads_recovered_ticks(self):
        recover(self.day,config(),self.path,[('10:01','10:03')],pack=self.pack())
        self.e.store.close();self.e=Engine(config(),self.path)
        self.e.new_day(self.at+dt.timedelta(minutes=4))
        self.assertEqual(len(self.e.history),1)
        self.assertIsNone(self.e.delta('k200',5,self.at+dt.timedelta(minutes=4)))

    def test_day_with_multiple_futures_contracts_does_not_merge_ambiguous_futures(self):
        at=self.at+dt.timedelta(minutes=4)
        self.e.ingest({'asof':at.isoformat(),'source':'live','fut_code':'F2','values':{'k200':501}},at)
        self.e.compute(at)
        r=recover(self.day,config(),self.path,[('10:01','10:03')],pack=self.pack())
        self.assertNotIn('fut_price',r['counts'])
        self.assertNotIn('foreign_fut_qty',r['counts'])
        self.assertIn('k200',r['counts'])

    def test_midday_start_and_partial_minutes_are_targets(self):
        ranges=targets(self.path,self.day,self.at+dt.timedelta(minutes=5))
        self.assertTrue(any(a<='09:00'<=b for a,b in ranges))
        self.assertTrue(any(a<='10:00'<=b for a,b in ranges))

    def test_already_archived_fields_do_not_trigger_the_same_gap_again(self):
        recover(self.day,config(),self.path,[('10:00','10:03')],pack=self.pack())
        ranges=targets(self.path,self.day,self.at+dt.timedelta(minutes=5))
        self.assertFalse(any(a<='10:01'<=b for a,b in ranges))

    def test_stale_original_field_is_preserved_but_analysis_gets_a_recovered_value(self):
        self.e.values['k200']['at']=(self.at-dt.timedelta(minutes=2)).isoformat()
        self.e.last_tick_minute=None;self.e.compute(self.at)
        before=self.rows()
        recover(self.day,config(),self.path,[('10:00','10:00')],pack=self.pack())
        self.assertEqual(self.rows(),before)
        row=next(h for h in read_view(self.path,self.day)['history'] if h['at'].startswith('2026-10-30T10:00'))
        self.assertEqual(row['values']['k200'],600)

    def test_bad_ranges_are_rejected_before_any_fetch(self):
        for value in ([('10:10','10:00')],[('07:00','10:00')],[('10:00',1)],'bad'):
            with self.assertRaises(ValueError):normalize_ranges(self.day,value)

    def test_archive_damage_does_not_break_live_view(self):
        (self.path/FILE).write_bytes(b'not a database')
        result=read_view(self.path,self.day)
        self.assertEqual(result['status'],'복구 자료 읽기 오류')
        self.assertEqual(result['history'],[])
        self.assertEqual(len(self.rows()),1)


if __name__=='__main__':unittest.main()
