import datetime as dt
import tempfile
import unittest
from pathlib import Path
from common import config,stamp
from close_summary import (add_extras,build_record,is_final,notes_for,parse_series,previous_business_day,save,load,status_of,
                           target_day,text_for,view)

CFG=config()


def series(points,end='18:10:30',step=30):
    """points: {'HH:MM:SS':value} 변화 시점. 최신 먼저인 30초 행(외국인 17만, 나머지는 반대 부호)을 만든다."""
    times=[]
    h,m,s=map(int,end.split(':'));t=h*3600+m*60+s;first=8*3600+46*60
    while t>=first:times.append(t);t-=step
    out=[];keys=sorted(points)
    for t in times:
        hhmmss='%02d:%02d:%02d'%(t//3600,t%3600//60,t%60)
        v=[points[k] for k in keys if k<=hhmmss][-1] if any(k<=hhmmss for k in keys) else 0
        out.append({'time':hhmmss.replace(':','')+'00','sv_17':v,'sv_18':-v,'sv_08':0,'sv_01':-v//2})
    return out


TIMELINE_1016={'08:46:00':-60,'15:35:00':-5104,'15:46:00':-6398,'16:07:30':-4200}


class SeriesTests(unittest.TestCase):
    def test_final_value_and_change_times(self):
        p=parse_series(series(TIMELINE_1016))
        self.assertEqual(p['final']['17'],-4200);self.assertEqual(p['last_change'],'16:07:30');self.assertEqual(p['at_1535']['17'],-5104)

    def test_confirmed_only_after_stable_for_twenty_minutes(self):
        confirmed=status_of(parse_series(series(TIMELINE_1016)),parse_series(series(TIMELINE_1016)))
        self.assertEqual(confirmed,'확정 추정')
        right_after=series(TIMELINE_1016,end='16:12:00')   # 마지막 변화 4분 뒤
        self.assertEqual(status_of(parse_series(right_after),parse_series(right_after)),'마감 직후 잠정')
        early=series({'08:46:00':-60,'15:35:00':-5104},end='15:36:30')
        self.assertEqual(status_of(parse_series(early),parse_series(early)),'마감 직전 값')
        mid=series({'08:46:00':-60},end='14:30:00')
        self.assertEqual(status_of(parse_series(mid),parse_series(mid)),'마감 전 값')

    def test_no_change_after_close_is_confirmed_once_stable(self):
        s=series({'08:46:00':-60,'15:46:00':-6398},end='16:30:00')
        self.assertEqual(status_of(parse_series(s),parse_series(s)),'확정 추정')

    def test_bad_rows_raise(self):
        with self.assertRaises(ValueError):parse_series([])
        with self.assertRaises(ValueError):parse_series([{'time':'xx','sv_17':1}])
        with self.assertRaises(ValueError):parse_series([{'time':'18103000','foo':1}])


class DayTests(unittest.TestCase):
    def test_target_day_by_calendar(self):
        self.assertIsNone(target_day(stamp('2026-10-16T10:00:00+09:00'),CFG))                        # 장중
        self.assertEqual(target_day(stamp('2026-10-16T16:00:00+09:00'),CFG).isoformat(),'2026-10-16')  # 마감 후
        self.assertEqual(target_day(stamp('2026-10-17T10:00:00+09:00'),CFG).isoformat(),'2026-10-16')  # 토요일
        self.assertEqual(target_day(stamp('2026-10-18T22:00:00+09:00'),CFG).isoformat(),'2026-10-16')  # 일요일
        self.assertEqual(previous_business_day(dt.date(2026,10,16),CFG).isoformat(),'2026-10-15')
        self.assertEqual(previous_business_day(dt.date(2026,10,12),CFG).isoformat(),'2026-10-08')  # 주말·10/9 휴장 건너뜀


class ReviewFixTests(unittest.TestCase):
    """2026-10-18 검토 보완 1~3."""
    def record(self,day='2026-10-16'):
        return build_record(dt.date.fromisoformat(day),series(TIMELINE_1016),series(TIMELINE_1016),'x')

    def test_early_spot_is_shown_with_time_but_not_used_for_hedge(self):
        r=self.record('2026-10-15')
        add_extras(r,{'foreign_spot_amt':-34817,'observed_at':{'foreign_spot_amt':'2026-10-15T13:58:58'},'asof':'2026-10-15T13:59:05'},None)
        text=text_for(r)
        self.assertIn('13:58 기록',text);self.assertIn('비교하지 않습니다',text);self.assertNotIn('헤지',text)
        self.assertIn('이른 값',view_rows(r)[0]['spot_note'])

    def test_rest_close_spot_allows_hedge_reading(self):
        r=self.record()
        add_extras(r,{'foreign_spot_amt':3214,'asof':'2026-10-18T10:00:00+09:00','source':'REST 마감 후 조회 ka10051'},None)
        text=text_for(r);self.assertIn('장 마감 후 조회',text);self.assertIn('헤지',text)

    def test_spot_from_other_day_is_ignored(self):
        r=self.record();add_extras(r,{'foreign_spot_amt':100,'asof':'2026-10-15T15:20:00+09:00'},None)
        self.assertNotIn('spot',r)

    def test_preopen_targets_previous_trading_day(self):
        self.assertEqual(target_day(stamp('2026-10-16T08:00:00+09:00'),CFG).isoformat(),'2026-10-15')
        self.assertIsNone(target_day(stamp('2026-10-16T08:50:00+09:00'),CFG))

    def test_confirmed_keeps_rechecking_until_1815_on_same_day(self):
        early=build_record(dt.date(2026,10,16),series(TIMELINE_1016,end='16:30:00'),series(TIMELINE_1016,end='16:30:00'),'x')
        self.assertEqual(early['status'],'확정 추정')
        self.assertFalse(is_final(early,stamp('2026-10-16T16:30:00+09:00'),CFG))
        self.assertTrue(is_final(early,stamp('2026-10-16T18:20:00+09:00'),CFG))
        self.assertTrue(is_final(early,stamp('2026-10-17T10:00:00+09:00'),CFG))
        self.assertTrue(is_final(self.record(),stamp('2026-10-16T17:00:00+09:00'),CFG))  # LS 시계열이 18:10까지 끝남

    def test_late_revision_after_confirmed_is_saved_and_explained(self):
        with tempfile.TemporaryDirectory() as d:
            first=build_record(dt.date(2026,10,16),series(TIMELINE_1016,end='16:30:00'),series(TIMELINE_1016,end='16:30:00'),'a')
            save(first,d)
            late={**TIMELINE_1016,'16:45:00':-3900}
            second=build_record(dt.date(2026,10,16),series(late,end='17:10:00'),series(late,end='17:10:00'),'b')
            self.assertTrue(save(second,d))
            saved=load(d)['2026-10-16'];self.assertEqual(saved['qty']['17'],-3900)
            self.assertTrue(any('다시 바뀌어' in n for n in notes_for(saved)))


def view_rows(record):
    with tempfile.TemporaryDirectory() as d:
        save(record,d);return view(d)['rows']


class DataDayTests(unittest.TestCase):
    def test_last_trading_day_for_option_query(self):
        from common import last_trading_day
        day=lambda s:last_trading_day(stamp(s),CFG).isoformat()
        self.assertEqual(day('2026-10-16T10:00:00+09:00'),'2026-10-16')   # 장중
        self.assertEqual(day('2026-10-16T08:50:00+09:00'),'2026-10-15')   # 장 시작 전
        self.assertEqual(day('2026-10-18T00:41:00+09:00'),'2026-10-16')   # 일요일 밤(0 응답으로 덮어쓰던 사례)
        self.assertEqual(day('2026-10-12T10:00:00+09:00'),'2026-10-12')

    def test_option_extras_match_by_data_day_not_query_time(self):
        r=build_record(dt.date(2026,10,16),series(TIMELINE_1016),series(TIMELINE_1016),'x')
        view={'status':'received','generated_at':'2026-10-18T00:41:00+09:00','data_day':'2026-10-16','structure_text':'s',
              'flows':[{'label':'외국인','available':True,'explain':{'side':-1,'text':'t'},'delta':{'kiwoom':{'total':-300},'bs':{'total':-200}}}]}
        add_extras(r,None,view)
        self.assertEqual(r['options']['investors'][0]['kiwoom'],-300)
        r2=build_record(dt.date(2026,10,15),series(TIMELINE_1016),series(TIMELINE_1016),'x');add_extras(r2,None,view)
        self.assertNotIn('options',r2)


class RecordTests(unittest.TestCase):
    def record(self):
        qty=series(TIMELINE_1016);amt=series({k:v*2 for k,v in TIMELINE_1016.items()})
        return build_record(dt.date(2026,10,16),qty,amt,'2026-10-18T10:00:00+09:00')

    def test_text_notes_and_hedge(self):
        r=self.record();add_extras(r,{'k200':866.4,'foreign_spot_amt':3415,'asof':'2026-10-16T15:20:00+09:00'},None)
        text=text_for(r)
        self.assertIn('10/16(금) 마감 선물',text);self.assertIn('외국인 −4,200계약(−8,400억) 순매도',text)
        self.assertIn('헤지·차익',text);self.assertIn('15:20 기록',text)
        self.assertTrue(any('15:35' in n and '−5,104' in n for n in notes_for(r)))

    def test_extras_skip_other_day_and_missing_spot(self):
        r=self.record()
        add_extras(r,None,{'status':'received','generated_at':'2026-10-15T23:23:00+09:00','flows':[]})
        self.assertNotIn('spot',r);self.assertNotIn('options',r)

    def test_confirmed_record_is_not_overwritten_by_provisional(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.record();self.assertTrue(save(r,d))
            worse=dict(r,status='마감 직후 잠정',qty={'17':-6398})
            self.assertFalse(save(worse,d));self.assertEqual(load(d)['2026-10-16']['qty']['17'],-4200)

    def test_extras_kept_when_requery_has_none(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.record();add_extras(r,{'foreign_spot_amt':100,'asof':'2026-10-16T15:20:00+09:00'},None);save(r,d)
            again=self.record();again['status']='확정 추정';save(again,d)
            self.assertIn('spot',load(d)['2026-10-16'])

    def test_view_orders_newest_first(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(view(d)['status'],'empty')
            a=self.record();save(a,d)
            b=build_record(dt.date(2026,10,15),series(TIMELINE_1016),series(TIMELINE_1016),'x');save(b,d)
            v=view(d);self.assertEqual([r['day'] for r in v['rows']],['2026-10-16','2026-10-15']);self.assertIn('10/16',v['latest']['text'])



def kis_day(d,day,at,mini,fut=None):
    """한투 기록 한 줄(미니선물 투자자 계약·억원)."""
    import json
    folder=Path(d)/'kis_flows';folder.mkdir(exist_ok=True)
    markets={'mini':{k:{'qty':q,'amt':a} for k,(q,a) in mini.items()}}
    if fut:markets['fut']={k:{'qty':q,'amt':a} for k,(q,a) in fut.items()}
    with open(folder/(day+'.jsonl'),'a',encoding='utf-8') as f:
        f.write(json.dumps({'at':day+'T'+at+'+09:00','day':day,'markets':markets})+'\n')


MINI_1029={'foreign':(-2745,-1293.09),'inst':(-52,-26.02),'individual':(2553,1202.21),'financial':(267,131.57)}


class MiniTests(unittest.TestCase):
    """2026-10-29 검토: 일반선물 +86만 보면 현물 −3,116억과 '반대'로 읽었으나 미니 −2,745를 더하면 −463계약으로 같은 방향."""
    def record(self):
        r={'day':'2026-10-29','status':'확정 추정','asked_at':'x','latest_time':'17:12:00','last_change':'16:08:00',
           'qty':{'17':86,'18':-805,'08':647,'01':1635},'amt':{'17':210,'18':-1874,'08':1495,'01':3832},
           'futures':{'price':935.0,'prev_close':945.8,'oi':146144.0,'oi_change':-1390.0,'price_pct':-1.14,'observed_at':'2026-10-29T16:11:52','source':'LS t2111 장 마감 후 조회'}}
        add_extras(r,{'foreign_spot_amt':-3116,'observed_at':{'foreign_spot_amt':'2026-10-29T15:49:36'}},None)
        return r

    def test_direction_includes_mini(self):
        with tempfile.TemporaryDirectory() as d:
            kis_day(d,'2026-10-29','16:15:05',MINI_1029)
            save(self.record(),d);v=view(d,cfg=CFG);text=v['latest']['text']
            self.assertIn('미니 포함(미니 0.2배 환산) 외국인 −463계약(−1,083억) 순매도',text)
            self.assertIn('같은 방향',text);self.assertNotIn('반대 방향',text)
            self.assertIn('외국인 선물(일반 +86 · 미니 −2,745 → 일반 환산) −463계약 순매도',v['latest']['reading']['facts'][0])
            self.assertEqual(v['rows'][0]['foreign_eq']['qty'],-463.0)

    def test_without_mini_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            save(self.record(),d);v=view(d,cfg=CFG)
            self.assertIn('미니선물 기록 없음',v['latest']['text']);self.assertIn('반대 방향',v['latest']['text'])
            self.assertTrue(any('미니선물 기록' in x for x in v['latest']['reading']['limits']))

    def test_flow_close_uses_close_values(self):
        import json
        from close_summary import flow_close
        with tempfile.TemporaryDirectory() as d:
            kis_day(d,'2026-10-29','16:15:05',MINI_1029)
            save(self.record(),d)
            board={'generated_at':'2026-10-29T17:22:50+09:00','markets':[{'key':'stock','label':'거래소','rows':[
                {'label':'개인','net':6722.0},{'label':'외국인','net':-3113.0},{'label':'기관계','net':-6061.0},{'label':'금융투자','net':-3909.0}]}]}
            (Path(d)/'ls_investor_board.json').write_text(json.dumps(board,ensure_ascii=False),encoding='utf-8')
            self.assertIsNone(flow_close(d,CFG,stamp('2026-10-29T14:00:00+09:00')))  # 장중에는 쓰지 않는다
            f=flow_close(d,CFG,stamp('2026-10-29T17:30:00+09:00'))
            self.assertEqual(f['rows']['외국인'],{'fut':86,'fut_amt':210,'fut_eq':-463.0,'fut_eq_amt':-1083,'spot':-3113.0})
            self.assertEqual(f['rows']['기관계']['spot'],-6061.0);self.assertEqual(f['fut']['status'],'확정 추정')
            # 다른 날 보드는 쓰지 않는다.
            board['generated_at']='2026-10-28T17:00:00+09:00'
            (Path(d)/'ls_investor_board.json').write_text(json.dumps(board,ensure_ascii=False),encoding='utf-8')
            self.assertIsNone(flow_close(d,CFG,stamp('2026-10-29T17:30:00+09:00'))['spot'])


if __name__=='__main__':unittest.main()
