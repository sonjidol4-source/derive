"""opt10059(종목별투자자기관별)에 현재 근월물 선물 코드를 넣어 단 1회 조회하는 진단.

공식 도움말상 opt10059의 종목코드는 주식용이다. 선물 코드를 받는지 실제 응답으로만 판단하며,
빈 응답·오류를 0계약으로 바꾸지 않는다. 계좌조회·주문은 하지 않는다.
"""
import sys
from pathlib import Path
from common import ROOT,atomic_json,config,now_kst
from tr_schema import read_spec

RECORD='종목별투자자기관별'
INVESTOR_FIELDS=('개인투자자','외국인투자자','기관계','금융투자','보험','투신','기타금융','은행','연기금등','사모펀드','국가','기타법인')


def judge(result,day):
    rows=result.get(RECORD,[]) if isinstance(result,dict) else []
    filled=[r for r in rows if any(str(r.get(f,'')).strip() for f in INVESTOR_FIELDS)]
    today=[r for r in filled if str(r.get('일자','')).strip()==day]
    return {'row_count':len(rows),'rows_with_investor_values':len(filled),'rows_for_query_date':len(today),
            'first_row':rows[0] if rows else None,
            'investor_values_returned':bool(filled),
            'note':'값이 나와도 단위(계약/주)·선물 대상 여부는 HTS 0780 등과 대조해야 확정됩니다.'}


def main():
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer
    from kiwoom import Kiwoom,OpenApiBusy
    from version_guard import BUSY_EXIT
    app=QApplication(sys.argv)
    cfg=config()
    try:kw=Kiwoom(module_dir=cfg['ocx']['openapi_dir'])
    except OpenApiBusy as e:
        print(str(e),flush=True);sys.exit(BUSY_EXIT)
    messages=[]
    kw.on_message=lambda tr,msg:messages.append({'tr':tr,'msg':msg})
    print('키움 실서버로 로그인하세요. opt10059를 1회만 조회합니다. 계좌조회·주문은 하지 않습니다.',flush=True)
    if not kw.login():raise SystemExit('로그인을 완료하지 못했습니다')
    if str(kw.call('GetLoginInfo(QString)','GetServerGubun'))=='1':
        raise SystemExit('모의투자 접속입니다. 실서버로 다시 로그인하세요.')
    module=str(kw.call('GetAPIModulePath()') or cfg['ocx']['openapi_dir'])
    spec=read_spec(Path(module)/'data'/'opt10059.enc')
    future=str(kw.call('GetFutureCodeByIndex(int)',0) or '').strip()
    if not future:raise SystemExit('현재 선물 코드를 받지 못했습니다')
    day=now_kst().strftime('%Y%m%d')
    inputs={'일자':day,'종목코드':future,'금액수량구분':'2','매매구분':'0','단위구분':'1'}
    report={'trcode':'opt10059','requests_made':1,'inputs':inputs,'started_at':now_kst().isoformat()}
    def done(result,message):
        report.update({'received_at':now_kst().isoformat(),'status':message,'response':result,
                       'judgment':judge(result,day) if result is not None else None})
        app.quit()
    kw.request(spec,inputs,done)
    QTimer.singleShot(20000,app.quit)
    app.exec_()
    report['server_messages']=messages
    if 'received_at' not in report:report['status']='응답 없음(20초)'
    atomic_json(ROOT/'data'/'fut_investor_probe.json',report)
    j=report.get('judgment') or {}
    lines=['opt10059 선물 코드 1회 조회','선물 코드: '+future+' / 일자 '+day,
           '상태: '+str(report.get('status') or '정상 응답'),
           '서버 메시지: '+('; '.join(m['msg'] for m in messages) or '없음'),
           '응답 행: '+str(j.get('row_count','-'))+' / 투자자 값 있는 행: '+str(j.get('rows_with_investor_values','-'))+' / 조회일 행: '+str(j.get('rows_for_query_date','-')),
           '첫 행: '+str(j.get('first_row')),'',
           '값이 나와도 단위와 선물 대상 여부는 영웅문 화면과 대조해야 확정됩니다.']
    (ROOT/'data'/'선물투자자_opt10059점검.txt').write_text('\n'.join(lines),encoding='utf-8-sig')
    print('\n'.join(lines),flush=True)


if __name__=='__main__':main()
