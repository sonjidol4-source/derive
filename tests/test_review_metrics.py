import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from review_metrics import aggregate_events, compare_close, event_metadata, provenance
from store import Store
from common import config, stamp
from engine import Engine


class CloseComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'kis_flows').mkdir()
        self.day = '2026-10-29'
        self.ls = {'day': self.day, 'source': 'LS t1602', 'status': '확정 추정',
                   'asked_at': self.day+'T17:00:00+09:00', 'latest_time': '17:00:00',
                   'qty': {'17': 0, '18': -10, '08': 5, '01': 3}}
        self.kis = {'day': self.day, 'at': self.day+'T16:15:05+09:00',
                    'started': self.day+'T16:15:02+09:00', 'markets': {'fut': {
                        'foreign': {'qty': 0}, 'inst': {'qty': -10},
                        'individual': {'qty': 5}, 'financial': {'qty': 3}},
                        'mini_fut': {'foreign': {'qty': 999}}}}

    def tearDown(self):
        self.tmp.cleanup()

    def compare(self):
        (self.root / 'close_summary.json').write_text(json.dumps({'days': {self.day: self.ls}}), encoding='utf-8')
        (self.root / 'kis_flows' / (self.day+'.jsonl')).write_text(json.dumps(self.kis)+'\n', encoding='utf-8')
        return compare_close(self.root, self.day)

    def test_general_only_zero_valid(self):
        result = self.compare()
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['rows'][0]['difference'], 0)

    def test_missing_is_not_zero(self):
        del self.kis['markets']['fut']['foreign']['qty']
        result = self.compare()
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['rows'][0]['difference'])

    def test_mismatched_day_not_compared(self):
        self.kis['day'] = '2026-10-28'
        self.assertEqual(self.compare()['status'], 'unavailable')

    def test_early_and_slow_not_final(self):
        self.kis['at'] = self.day+'T16:14:59+09:00'
        self.assertEqual(self.compare()['status'], 'unavailable')
        self.kis['at'] = self.day+'T16:16:00+09:00'
        self.assertEqual(self.compare()['status'], 'unavailable')

    def test_differences_have_direction_and_no_winner(self):
        self.kis['markets']['fut']['foreign']['qty'] = -12
        result = self.compare()
        self.assertEqual(result['status'], 'different')
        self.assertEqual(result['rows'][0]['difference'], -12)
        self.assertNotIn('winner', result)


class ObservationTests(unittest.TestCase):
    def event(self, at='2026-10-29T10:00:00+09:00'):
        return {'id': 'test', 'at': at, 'kind': 'turn', 'side': -1, 'price': 100,
                'investor': 'foreign', 'rule_version': '1', 'mode': 'live',
                'outcomes': {'5': {'status': '관측', 'return_pct': -1}}}

    def test_legacy_never_inferred_live(self):
        old = self.event()
        new = {**self.event(), 'rule_hash': 'abc', 'data_provenance': 'observed'}
        result = aggregate_events([old, new], total_count=7)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['loaded'], 2)
        self.assertEqual({g['provenance'] for g in result['groups']}, {'unknown', 'observed'})
        self.assertEqual(result['audit']['missing_provenance'], 1)

    def test_missing_not_zero_and_overlap_within_day(self):
        events = [self.event(), self.event('2026-10-29T10:03:00+09:00'),
                  self.event('2026-10-30T10:00:00+09:00'), self.event()]
        events[-1]['outcomes'] = {'5': {'status': '가격 누락'}}
        stats = aggregate_events(events)['groups'][0]['horizons']['5']
        self.assertEqual(stats['valid'], 3)
        self.assertEqual(stats['missing'], 1)
        self.assertEqual(stats['days'], 2)
        self.assertEqual(stats['overlap_count'], 1)
        self.assertEqual(stats['mean_directional_pct'], 1)
        self.assertEqual(stats['positive_pct'], 100)

    def test_recovered_evaluation_separate(self):
        a, b = self.event(), self.event()
        a['outcomes']['5']['data_provenance'] = 'observed'
        b['outcomes']['5']['data_provenance'] = 'recovered'
        stats = aggregate_events([a, b])['groups'][0]['horizons']['5']['by_provenance']
        self.assertEqual(set(stats), {'observed', 'recovered'})
        self.assertEqual(stats['recovered']['valid'], 1)

    def test_snapshot_hash_provenance_and_no_credentials(self):
        cfg = {'version': '1', 'rules': {'x': 3}, 'session': {'start': '09:00'}, 'api_key': 'secret'}
        values = {'k200': {'value': 100, 'source': 'LS', 'at': '2026-10-29T10:00:00+09:00', 'quality': '장중 잠정'}}
        meta = event_metadata(cfg, 'live', values, {'thresholds': {'a': 5}, 'basis': {'a': 'prior median 5'},
                                                   'rule_context': {'method': 'deriv-today-1'}})
        self.assertEqual(meta['data_provenance'], 'observed')
        self.assertNotIn('secret', json.dumps(meta))
        changed = event_metadata(cfg, 'live', values, {'thresholds': {'a': 6}, 'basis': {'a': 'prior median 6'},
                                                      'rule_context': {'method': 'deriv-today-1'}})
        self.assertEqual(meta['rule_hash'], changed['rule_hash'])
        self.assertEqual(meta['rule_snapshot']['external_thresholds'], {'a': 5})
        self.assertEqual(changed['rule_snapshot']['external_thresholds'], {'a': 6})
        self.assertEqual(meta['rule_snapshot']['external_basis'], {'a': 'prior median 5'})
        cfg['rules']['x'] = 9
        self.assertEqual(meta['rule_snapshot']['rules']['x'], 3)
        self.assertNotEqual(meta['rule_hash'], event_metadata(cfg, 'live', values,
            {'rule_context': {'method': 'deriv-today-1'}})['rule_hash'])
        self.assertNotEqual(changed['rule_hash'], event_metadata(
            {'version': '1', 'rules': {'x': 3}, 'session': {'start': '09:00'}}, 'live', values,
            {'rule_context': {'method': 'deriv-today-2'}})['rule_hash'])
        self.assertEqual(provenance([{'source': 'LS 사후 조회'}]), 'recovered')
        self.assertEqual(provenance([{'quality': '사용자 입력'}]), 'manual')
        self.assertEqual(provenance([{'source':'unknown','quality':'provisional','at':'2026-10-29T10:00:00+09:00'}]),'unknown')
        self.assertEqual(event_metadata(cfg, 'live', values, {'source': 'external'})['data_provenance'], 'mixed')

    def test_count_and_no_rewrite_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            event = self.event()
            store.event(event)
            self.assertEqual(store.event_count(), 1)
            self.assertNotIn('data_provenance', store.events()[0])
            store.close()

    def test_engine_saves_new_input_and_evaluation_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(config(), directory)
            try:
                at = stamp('2026-10-14T10:00:00+09:00')
                engine.ingest({'asof': at.isoformat(), 'source': 'LS', 'quality': '장중 잠정',
                               'values': {'k200': 100}}, at)
                engine.values['inst_spot_amt']={'value':100,'at':(at-dt.timedelta(minutes=30)).isoformat(),
                                               'source':'LS 사후 조회','quality':'복구'}
                self.assertTrue(engine.observe('external', 1, at, [],
                    {'source': 'KIS', 'investor': 'foreign', 'input_observations': [
                        {'source': 'KIS', 'at': at.isoformat(), 'quality': '장중 잠정'}]}))
                event = engine.store.events()[0]
                self.assertEqual(event['data_provenance'], 'observed')
                self.assertNotIn('inst_spot_amt', {m.get('field') for m in event['input_observations']})
                self.assertTrue(event['rule_hash'])
                later = at+dt.timedelta(minutes=5)
                engine.history = [{'at': later.isoformat(), 'values': {'k200': {
                    'at': later.isoformat(), 'value': 101, 'source': 'LS 사후 조회', 'quality': '복구'}}}]
                engine.evaluate_events(later)
                outcome = engine.store.events()[0]['outcomes']['5']
                self.assertEqual(outcome['data_provenance'], 'recovered')
                self.assertEqual(outcome['return_pct'], 1)
            finally:
                engine.store.close()

    def test_internal_signal_includes_recovered_base_not_unused_values(self):
        for recovered_base in (False, True):
            with self.subTest(recovered_base=recovered_base), tempfile.TemporaryDirectory() as directory:
                cfg=config()
                cfg['rules'].update(confirm_min=0,foreign_fut_5m=1,program_5m=1,price_5m_pct=0.01)
                engine=Engine(cfg,directory)
                at=stamp('2026-10-14T10:00:00+09:00')
                try:
                    for minute in range(6):
                        clock=at+dt.timedelta(minutes=minute)
                        engine.ingest({'asof':clock.isoformat(),
                            'source':'LS 사후 조회' if recovered_base and minute==0 else 'LS',
                            'quality':'복구' if recovered_base and minute==0 else '장중 잠정',
                            'values':{'k200':500+minute, 'foreign_fut_qty':100*minute,
                                      'nonarb_amt':100*minute}},clock)
                        engine.values['inst_spot_amt']={'value':100,'at':at.isoformat(),
                            'source':'사용자 입력','quality':'사용자 입력'}
                        engine.compute(clock)
                    event=engine.store.events()[0]
                    self.assertEqual(event['data_provenance'],'mixed' if recovered_base else 'observed')
                    inputs=event['input_observations']
                    self.assertNotIn('inst_spot_amt',{m.get('field') for m in inputs})
                    bases=[m for m in inputs if m.get('role')=='window_base']
                    self.assertTrue(bases)
                    self.assertEqual(bases[0]['history_at'],at.isoformat())
                    if recovered_base:
                        self.assertTrue(any(m.get('source')=='LS 사후 조회' for m in bases))
                finally:
                    engine.store.close()

    def test_missing_derived_operand_never_certifies_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(config(),directory)
            try:
                at=stamp('2026-10-14T10:00:00+09:00')
                engine.values={'basis_residual':{'value':1,'at':at.isoformat(),'source':'계산','quality':'derived'}}
                inputs=engine.observation_inputs(at,[],['basis_residual'])
                self.assertEqual(provenance(inputs),'mixed')
                self.assertTrue(any(m.get('provenance')=='unknown' for m in inputs))
            finally:
                engine.store.close()


if __name__ == '__main__':
    unittest.main()
