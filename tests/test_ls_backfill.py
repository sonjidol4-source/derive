import datetime as dt
import unittest
from common import KST
from ls_backfill import bar_series,build_ticks,investor_series,program_series,quote_series,FUT_QTY

DAY=dt.date(2026,10,26)
T=lambda hms:dt.datetime.combine(DAY,dt.time(*map(int,hms.split(':'))),tzinfo=KST)


class BackfillTests(unittest.TestCase):
    def test_bars_are_end_labelled_and_other_days_dropped(self):
        rows=[{'date':'20261026','time':'084600','close':'858.95','openyak':148391},{'date':'20261023','time':'154500','close':'882.45','openyak':1}]
        self.assertEqual(bar_series(rows,DAY,'close'),[(T('08:46:00'),858.95)])

    def test_quote_series_basis_and_previous_day(self):
        fut={'close':[(T('09:01:00'),860.0)],'openyak':[(T('09:01:00'),149000)]}
        s=quote_series(fut,[(T('09:01:00'),857.5)],850.0,148000)
        self.assertEqual(s['basis'],[(T('09:01:00'),2.5)]);self.assertEqual(s['fut_oi_prev'],[(T('09:01:00'),1000)])
        self.assertEqual(s['fut_prev_close'],[(T('09:01:00'),850.0)])

    def test_program_in_eok_and_investor_series(self):
        p=program_series([{'time':'090100','tot3':114223,'cha3':-16258,'bcha3':130482}],DAY)
        self.assertAlmostEqual(p['program_amt'][0][1],1142.23)
        q=investor_series([{'time':'09013000','sv_17':-500,'sv_18':None}],DAY,FUT_QTY)
        self.assertEqual(q,{'foreign_fut_qty':[(T('09:01:30'),-500.0)]})

    def test_ticks_respect_freshness_and_skip_existing_minutes(self):
        series={'fut_price':[(T('09:01:00'),860.0)],'foreign_fut_qty':[(T('09:00:00'),-100.0)]}
        src={k:'s' for k in series}
        ticks=dict(build_ticks(DAY,series,src,'seg','A',{'2026-10-26T09:03'},start='09:00',end='09:04'))
        self.assertEqual(set(ticks['2026-10-26T09:01:30+09:00']['values']),{'fut_price','foreign_fut_qty'})
        self.assertNotIn('fut_price',ticks['2026-10-26T09:02:30+09:00']['values'])   # 90초 지난 시세는 넣지 않음
        self.assertIn('foreign_fut_qty',ticks['2026-10-26T09:02:30+09:00']['values'])  # 수급은 150초까지
        self.assertNotIn('2026-10-26T09:03:30+09:00',ticks)                           # 이미 있는 분은 건너뜀
        self.assertNotIn('2026-10-26T09:04:30+09:00',ticks)                           # 값이 모두 낡으면 기록 없음
        self.assertEqual(ticks['2026-10-26T09:01:30+09:00']['values']['fut_price']['quality'],'LS 사후 조회')


if __name__=='__main__':unittest.main()
