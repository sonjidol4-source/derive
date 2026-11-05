"""2026-10-29 LS 옵션 t2301 읽기 15초 초과 대응: 옵션 TR만 긴 대기, 실패 시 30·60·120초 재시도, 전광판·투자자 합계 분리."""
import tempfile
import threading
import unittest
from pathlib import Path
from common import config,stamp
from engine import Engine
from ls_feed import LsFeed,OptionSchedule,RETRY_DELAYS
from test_ls_deriv import T8467,T2111,board,investors

AT=stamp('2026-10-29T13:34:00+09:00')


class RequestException(Exception):pass
class Timeout(RequestException):pass


class FakeRequests:
    """requests 대역: t2301을 fail_board번 '읽기 시간 초과'로 실패시킨다."""
    RequestException,Timeout=RequestException,Timeout

    def __init__(self,fail_board=0):
        self.fail_board=fail_board;self.posts=[]

    def post(self,url,timeout,json,headers):
        tr=headers['tr_cd'];block=json[tr+'InBlock'];self.posts.append((tr,timeout))
        if tr=='t2301' and self.fail_board>0:
            self.fail_board-=1;raise Timeout("HTTPSConnectionPool(host='openapi.ls-sec.co.kr', port=8080): Read timed out. (read timeout=%g)"%timeout[1])
        body={'t8467':T8467,'t2111':T2111,'t2301':board()}.get(tr) or investors({'17':490 if block.get('market')=='5' else -377,'18':1,'08':2})
        return type('R',(),{'status_code':200,'json':lambda self:body})()


class ScheduleTests(unittest.TestCase):
    def test_backoff_then_back_to_poll_and_reset_on_success(self):
        s=OptionSchedule(300)
        self.assertEqual([s.failure(0,'x') for _ in range(5)],[30,60,120,300,30])  # 3회 재시도 뒤 정기 주기, 다음 실패부터 다시 셈
        s.success(10,AT);self.assertEqual((s.retry,s.next,s.last_error),(0,310,''))
        self.assertEqual(RETRY_DELAYS,(30,60,120))


class RetryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.dir=Path(self.temp.name)
        self.e=Engine(config(),self.dir)
        self.f=LsFeed(config(),self.e,threading.Event())
        self.f.keys=('k','s');self.f.access='token';self.f.expires=1e18
        self.f.stop.wait=lambda *_:None  # 테스트에서는 호출 간격을 기다리지 않음
    def tearDown(self):self.e.store.close();self.temp.cleanup()

    def run_at(self,req,now):
        self.f.options_due(req,AT,now=now,clock=lambda:AT)
        return self.e.connections

    def test_board_timeout_retries_while_investors_still_update(self):
        req=FakeRequests(fail_board=2)
        c=self.run_at(req,0)
        self.assertEqual(c['LS 옵션 전광판']['status'],'수신 오류');self.assertIn('재시도 1/3(30초 뒤)',c['LS 옵션 전광판']['detail'])
        self.assertIn('오늘 타임아웃 1회',c['LS 옵션 전광판']['detail'])
        self.assertEqual(c['LS 옵션 투자자']['status'],'정상')                     # 전광판 실패와 무관하게 갱신
        self.assertTrue((self.dir/'ls_option_investors.json').exists())
        self.assertTrue(all(t==(15,35.0) for tr,t in req.posts))                   # 옵션 TR만, 연결 15초·읽기 35초
        req.posts.clear();self.run_at(req,30)
        self.assertEqual([tr for tr,_ in req.posts],['t2301'])                     # 30초 뒤엔 전광판만 다시
        self.assertIn('재시도 2/3(60초 뒤)',self.e.connections['LS 옵션 전광판']['detail'])
        req.posts.clear();self.run_at(req,60);self.assertEqual(req.posts,[])       # 60초 백오프 중
        c=self.run_at(req,90)
        self.assertEqual(c['LS 옵션 전광판']['status'],'정상');self.assertIn('오늘 타임아웃 2회',c['LS 옵션 전광판']['detail'])
        self.assertTrue((self.dir/'snapshot_calls.json').exists())
        log=(self.dir/'ls_option.log').read_text(encoding='utf-8')
        for text in ('t2301 실패','30초 뒤 재시도 1/3','60초 뒤 재시도 2/3','[LS 옵션 전광판] t2301 정상(재시도 2/3)','복구','t1602 시장5·수량 정상'):
            self.assertIn(text,log)

    def test_exhausted_retries_return_to_poll(self):
        req=FakeRequests(fail_board=10)
        for now in (0,30,90,210):self.run_at(req,now)
        self.assertIn('5분 뒤 다시 조회',self.e.connections['LS 옵션 전광판']['detail'])
        self.assertEqual(self.f.opt['board'].next,210+300)
        self.assertIn('재시도 소진',(self.dir/'ls_option.log').read_text(encoding='utf-8'))

    def test_other_trs_keep_15s_read_timeout(self):
        req=FakeRequests();self.f.ensure_deriv(req);self.f.deriv.futures(AT,lambda:AT)
        self.assertEqual({t for _,t in req.posts},{(15,15)})


if __name__=='__main__':unittest.main()
