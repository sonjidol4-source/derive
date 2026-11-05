import json
import tempfile
import unittest
from pathlib import Path
from common import config,stamp
import investor_board as ib

FIX=Path(__file__).parent/'fixtures'/'ls_t1601_20261029_u.json'
AT=stamp('2026-10-29T14:45:12+09:00')


class BoardTests(unittest.TestCase):
    def setUp(self):self.body=json.loads(FIX.read_text(encoding='utf-8'))

    def test_matches_0251_layout_and_values(self):
        b=ib.parse(self.body,AT)
        self.assertEqual([m['label'] for m in b['markets']],['거래소','코스닥','KOSPI200 선물','콜옵션','풋옵션'])
        self.assertEqual([r['label'] for r in b['markets'][0]['rows']],['개인','외국인','기관계','금융투자','보험','투신','은행','기타금융','연기금','사모펀드','기타법인'])
        net=lambda m,label:next(r for r in b['markets'][m]['rows'] if r['label']==label)['net']
        # 2026-10-29 14:45:12 LS 통합 · 영웅문 0251 14:44:30 화면 +8,116 / −2,439 / −11 / −13 과 같은 흐름
        self.assertEqual((net(0,'개인'),net(0,'외국인'),net(0,'기관계')),(8122,-2494,-7990))
        self.assertEqual((net(3,'개인'),net(4,'개인')),(-10,-13))
        stock=b['markets'][0]['rows'][0];self.assertEqual((stock['sell'],stock['buy']),(164570,172692))
        self.assertEqual(ib.BLOCK['exchgubun'],'U')

    def test_errors_and_missing_block(self):
        with self.assertRaises(ValueError):ib.parse({**self.body,'rsp_cd':'IGW00121'},AT)
        with self.assertRaises(ValueError):ib.parse({k:v for k,v in self.body.items() if k!='t1601OutBlock4'},AT)

    def test_view_marks_stale_only_in_session(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ib.view(Path(d))['status'],'empty')
            ib.save(ib.parse(self.body,AT),Path(d))
            self.assertEqual(ib.view(Path(d),config(),stamp('2026-10-29T14:45:40+09:00'))['mode'],'live')
            self.assertEqual(ib.view(Path(d),config(),stamp('2026-10-29T14:50:00+09:00'))['mode'],'stale')
            self.assertEqual(ib.view(Path(d),config(),stamp('2026-10-29T19:00:00+09:00'))['mode'],'archive')


if __name__=='__main__':unittest.main()
