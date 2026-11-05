import datetime as dt
import unittest
from rest_feed import stock_close,stock_intraday


def chart(*rows):
    return {'opmr_invsr_trde_chart':[{'tm':t,'frgnr_invsr':f,'orgn':o} for t,f,o in rows]}


class WatchlistTests(unittest.TestCase):
    """2026-10-29 삼성전자 실제 응답(백만원)."""
    def test_intraday_takes_last_aggregation(self):
        data=chart(('090000','0','0'),('091900','-45590','0'),('131100','-265302','-89454'),('141500','-352343','-76096'),('141500','-352343','-76096'))
        f=stock_intraday(data)
        self.assertEqual((f['agg_time'],f['foreign_amt'],f['inst_amt'],f['foreign_change']),('14:15',-3523.43,-760.96,-870.41))
        self.assertIn('가집계 14:15',f['basis'])

    def test_before_first_aggregation_is_empty_not_zero(self):
        f=stock_intraday(chart(('090000','0','0')))
        self.assertIsNone(f['foreign_amt']);self.assertIn('첫 가집계',f['basis'])

    def test_bad_response_raises(self):
        with self.assertRaises(ValueError):stock_intraday({})
        with self.assertRaises(ValueError):stock_close({},dt.date(2026,10,29),dt.datetime(2026,10,29,17))

    def test_close_row_of_that_day(self):
        data={'stk_invsr_orgn':[{'dt':'20261029','frgnr_invsr':'-139468','orgn':'52514'},{'dt':'20261028','frgnr_invsr':'364080','orgn':'383417'}]}
        f=stock_close(data,dt.date(2026,10,29),dt.datetime(2026,10,29,17,30))
        self.assertEqual((f['foreign_amt'],f['inst_amt'],f['closing']),(-1394.68,525.14,True));self.assertIn('NXT 반영 전',f['basis'])
        self.assertIn('통합',stock_close(data,dt.date(2026,10,29),dt.datetime(2026,10,29,20,10))['basis'])
        self.assertIsNone(stock_close(data,dt.date(2026,10,30),dt.datetime(2026,10,30,16)))  # 아직 집계 전


if __name__=='__main__':unittest.main()
