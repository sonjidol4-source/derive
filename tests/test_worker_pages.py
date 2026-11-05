"""검토 7번: 콜 연속 조회 응답이 풋 처리기로 넘어가지 않는지. OCX·서버 없이 가짜 객체로만 확인."""
import unittest
from unittest import mock

try:
    import ocx_worker
except ImportError:  # PyQt5가 없는 일반 Python에서는 건너뜀(32비트 runtime32에서 실행)
    ocx_worker=None


@unittest.skipIf(ocx_worker is None,'PyQt5/OCX 환경 필요')
class OptionPagesTests(unittest.TestCase):
    def test_each_side_keeps_its_own_handler_through_continuation(self):
        calls=[];saved={}
        fake=mock.Mock(listed_strikes=[860.0,862.5])
        fake.request_quote=lambda code,inputs,records,done,prev_next=0:calls.append((code,prev_next,done))
        fake.option_pages_handler=lambda *a:ocx_worker.Worker.option_pages_handler(fake,*a)
        with mock.patch.object(ocx_worker,'atomic_json',lambda path,data:saved.__setitem__(path.name,data)):
            fake.month='202611'
            ocx_worker.Worker.snapshot_options(fake)
            (c_code,_,c_done),(p_code,_,p_done)=calls
            self.assertEqual((c_code,p_code),('opt50021','opt50022'))
            row=lambda code,strike:{'종목코드':code,'행사가':str(strike),'현재가':'1','미결제약정':'5','내재변동성':'50','델타':'50'}
            c_done({'콜종목결제월별시세':[row('B1',860)]},'연속 조회 잔여')
            code,prev,next_done=calls[-1]
            self.assertEqual((code,prev),('opt50021',2))
            self.assertIs(next_done,c_done)  # 다음 쪽도 콜 처리기
            next_done({'콜종목결제월별시세':[row('B2',862.5)]},'')
            p_done({'풋종목결제월별시세':[row('C1',860)]},'')
        calls_snap=saved['snapshot_calls.json']['snapshot']
        self.assertEqual(calls_snap['kind'],'calls');self.assertEqual(calls_snap['pages'],2)
        self.assertEqual(sorted(r['code'] for r in calls_snap['rows']),['B1','B2'])
        self.assertTrue(calls_snap['complete'])
        self.assertFalse(saved['snapshot_puts.json']['snapshot']['complete'])  # 862.5 풋 미수신


if __name__=='__main__':unittest.main()
