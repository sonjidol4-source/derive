"""키움 실시간 '투자자별매매'가 공개 OCX로 들어오는지 확인하는 점검기(장중 전용, 서버 조회·주문 없음).

근거: D:\\OpenAPI\\data\\nkrealtime.dat에는 주석이 아닌 '투자자별매매'(FID 20·217·202~213·561·562,
선물용 'Fd' 정의는 50401~50423 추가)와 '투자자ticker'(6028 등)가 있다. 반면 OCX용 system\\koarealtime.dat에서는
같은 항목이 주석 처리돼 있다. 파일만으로는 사용 가능 여부를 알 수 없어, 후보 코드에 실시간 등록만 하고 일정 시간 들어오는
모든 실시간 이벤트를 원문 그대로 기록한다. 들어온 값의 의미(FID별 투자자·단위)는 같은 시각 영웅문 0780과 대조해야 확정된다.
"""
import argparse
import json
import sys
from collections import Counter
from common import ROOT,atomic_json,config,now_kst,in_session

FIDS=[20,216,217]+list(range(202,214))+[561,562,50401,50420,50403,50421,50404,50422,50423,6028,6029,6034,886,887,888]
# 업종(코스피·코스닥·KOSPI200)과 선물 시장 후보 코드. 근월물 코드는 로그인 후 추가.
BASE_CODES=['001','101','201','900','K2I']
SCREEN='7201'


def summarize(events,codes,started,ended,duration):
    kinds=Counter((e['code'],e['kind']) for e in events)
    investor=[e for e in events if '투자자' in e['kind']]
    lines=['키움 실시간 투자자별매매 등록 점검','등록 코드: '+', '.join(codes),
           '수집 시간: %s ~ %s (%d초)'%(started[11:19],ended[11:19],duration),'',
           '받은 실시간 종류(코드 / 종류 / 건수):']
    lines+=['  %s / %s / %d건'%(c,k,n) for (c,k),n in kinds.most_common()] or ['  없음']
    lines.append('')
    if investor:
        lines.append('투자자 관련 실시간 %d건 수신 → 첫 사례의 FID 원문:'%len(investor))
        first=investor[0];lines.append('  %s %s %s'%(first['at'][11:19],first['code'],first['kind']))
        lines+=['    FID %s = %s'%(k,v) for k,v in first['values'].items() if v]
        lines.append('다음 단계: 같은 시각 영웅문 0780(선물·순매수·수량)을 새로 조회해 값·단위를 대조해야 연결할 수 있습니다.')
    else:
        lines.append('투자자 관련 실시간은 들어오지 않았습니다. 장중(09:00~15:30)에 실행했는지 확인하세요.')
        lines.append('장중에도 없으면 공개 OCX로는 이 경로를 쓸 수 없다고 보고 LS t1602 경로를 검증합니다.')
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=180);p.add_argument('--codes',default='')
    args=p.parse_args()
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer
    from kiwoom import Kiwoom,OpenApiBusy
    from version_guard import BUSY_EXIT
    cfg=config()
    if not in_session(now_kst(),cfg):
        print('지금은 장중이 아닙니다. 실시간 투자자 데이터는 장중에만 들어오므로 결과가 비어 있을 수 있습니다.',flush=True)
    app=QApplication(sys.argv)
    try:kw=Kiwoom(module_dir=cfg['ocx']['openapi_dir'])
    except OpenApiBusy as e:
        print(str(e)+' (실시간실행 창을 닫고 3분 점검 후 다시 켜세요)',flush=True);sys.exit(BUSY_EXIT)
    print('키움 실서버로 로그인하세요. 실시간 등록만 하며 조회 요청·주문은 하지 않습니다.',flush=True)
    if not kw.login():raise SystemExit('로그인을 완료하지 못했습니다')
    if str(kw.call('GetLoginInfo(QString)','GetServerGubun'))=='1':
        raise SystemExit('모의투자 접속입니다. 실서버로 다시 로그인하세요.')
    future=str(kw.call('GetFutureCodeByIndex(int)',0) or '').strip()
    codes=[c for c in (args.codes.split(',') if args.codes else BASE_CODES+([future] if future else [])) if c]
    events=[];limit=5000
    def on_real(code,kind):
        if len(events)>=limit:return
        values={str(f):kw.real(code,f) for f in FIDS}
        events.append({'at':now_kst().isoformat(),'code':code,'kind':kind,'values':{k:v for k,v in values.items() if v}})
    kw.on_real=on_real
    started=now_kst().isoformat()
    kw.register(SCREEN,codes,FIDS)
    print('등록 코드 %s · %d초 동안 수신 기록 중...'%(','.join(codes),args.seconds),flush=True)
    QTimer.singleShot(args.seconds*1000,app.quit)
    app.exec_()
    kw.remove(SCREEN)
    ended=now_kst().isoformat()
    report={'started_at':started,'ended_at':ended,'seconds':args.seconds,'codes':codes,'fids':FIDS,
            'in_session':in_session(now_kst(),cfg),'event_count':len(events),'truncated':len(events)>=limit,
            'events':events,'note':'등록만 한 원문 기록입니다. FID 의미·단위는 0780 대조 전 미확정이며 수집기에 연결하지 않았습니다.'}
    atomic_json(ROOT/'data'/'realtime_investor_probe.json',report)
    text=summarize(events,codes,started,ended,args.seconds)
    (ROOT/'data'/'실시간투자자_등록점검.txt').write_text(text,encoding='utf-8-sig')
    print(text,flush=True)


if __name__=='__main__':main()
