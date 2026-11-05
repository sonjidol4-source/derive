import unittest
from investor_view import summarize
from investor_probe import RECORD


class InvestorViewTests(unittest.TestCase):
    def report(self,rows):
        return {'generated_at':'2026-10-15T22:00:00+09:00','results':[{'scope':'daily_net_trades',
            'investor':'financial','label':'금융투자','received_at':'2026-10-15T22:00:00+09:00',
            'row_count':len(rows),'future_quantity_found':False,'response':{RECORD:rows}}]}

    def test_call_and_put_use_distinct_signed_quantity_fields(self):
        quotes={'B1':{'cp':'C','strike':850,'month':'202611'},'C1':{'cp':'P','strike':850,'month':'202611'}}
        view=summarize(self.report([{'종목코드':'B1','풋_종목코드':'C1','행사가':'850','투자자별순매수수량':'-5','풋_투자자별순매수수량':'+3'}]),quotes)
        group=view['groups'][0]
        self.assertEqual(group['call_net_qty'],-5)
        self.assertEqual(group['put_net_qty'],3)
        self.assertEqual(group['label'],'금융투자')
        self.assertFalse(group['future_quantity_found'])
        self.assertFalse(view['is_live'])

    def test_unknown_or_mismatched_codes_are_not_attributed(self):
        quotes={'B1':{'cp':'C','strike':860,'month':'202611'}}
        view=summarize(self.report([{'종목코드':'B1','행사가':'850','투자자별순매수수량':'100'}]),quotes)
        self.assertEqual(view['groups'][0]['matched_options'],0)
        self.assertIsNone(view['groups'][0]['call_net_qty'])

    def test_missing_quantity_is_not_zero(self):
        view=summarize(self.report([{'종목코드':'B1','행사가':'850','투자자별순매수수량':''}]),{'B1':{'cp':'C','strike':850,'month':'202611'}})
        self.assertIsNone(view['groups'][0]['call_net_qty'])
