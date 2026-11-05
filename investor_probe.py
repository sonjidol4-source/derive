"""Two bounded, read-only probes using the installed official TR definition.

Probe responses are evidence. They never become a trading signal automatically.
"""
import json
from pathlib import Path
from common import ROOT,atomic_json,config,now_kst,number
from tr_schema import read_spec

INVESTORS=(('foreign','외국인','09'),('financial','금융투자','01'))
RECORD='투자자별만기손익차트'


def probe_spec(module):
    spec=read_spec(Path(module)/'data'/'opt50038.enc')
    expected={'일자구분','일자','투자자구분','수량금액구분'}
    required={'종목코드','투자자별순매수수량'}
    if set(spec['inputs'])!=expected or not required.issubset(spec['records'].get(RECORD,[])):
        raise ValueError('설치된 공식 투자자 조회 명세가 예상 구조와 다릅니다')
    return spec


def inspect_response(result,future_code,investor,label,received_at,message=''):
    rows=result.get(RECORD,[]) if isinstance(result,dict) else []
    populated=[r for r in rows if str(r.get('종목코드','')).strip() or str(r.get('풋_종목코드','')).strip()]
    matches=[r for r in populated if str(r.get('종목코드','')).strip()==future_code]
    quantity=number(matches[0].get('투자자별순매수수량')) if len(matches)==1 else None
    # A blank/missing future row must never be mistaken for zero contracts.
    return {'investor':investor,'label':label,'received_at':received_at,'future_code':future_code,
            'row_count':len(rows),'populated_rows':len(populated),'future_row_count':len(matches),
            'daily_net_contracts':quantity,'future_quantity_found':quantity is not None,
            'message':message,'scope':'daily_net_trades','inputs_date':received_at[:10],
            'sample_codes':[str(r.get('종목코드','')).strip() for r in populated[:8]],
            'response':result,'note':'공식 일자구분=1·수량금액구분=1로 조회한 진단입니다. 보유 포지션이 아니며 자동 판정에 사용하지 않습니다.'}


def save_report(reports,directory=None):
    directory=Path(directory or ROOT/'data')
    report={'generated_at':now_kst().isoformat(),'trcode':'opt50038','requests_limit':2,
            'results':reports,'verified_live_connection':False,
            'note':'필드 존재·값 확인을 위한 점검입니다. 실제 지원과 수량 의미를 검토한 후 수집 매핑을 적용해야 합니다.'}
    path=directory/'investor_probe.json'
    atomic_json(path,report)
    lines=['투자자별 파생 수급 연결 점검','최대 조회: 외국인·금융투자 각 1회 (총 2회)','']
    for r in reports:
        lines.extend([r['label']+': 응답 '+str(r['populated_rows'])+'행',
            '현재 선물 코드 일치: '+str(r['future_row_count'])+'행',
            '선물 당일 순매매 계약수: '+('미확인' if r['daily_net_contracts'] is None else str(r['daily_net_contracts'])),
            '서버 상태: '+r['message'],''])
    lines.append('이 점검 결과는 자동 매매 신호나 실제 보유 포지션이 아닙니다.')
    (directory/'투자자수급_연결점검.txt').write_text('\n'.join(lines),encoding='utf-8-sig')
    return path


def main():
    # Imports stay here so response validation can be tested without Windows/OCX.
    import sys
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer
    from kiwoom import Kiwoom,OpenApiBusy
    from version_guard import BUSY_EXIT
    app=QApplication(sys.argv)
    try:kw=Kiwoom(module_dir=config()['ocx']['openapi_dir'])
    except OpenApiBusy as e:
        print(str(e),flush=True);sys.exit(BUSY_EXIT)
    print('키움 실서버로 로그인하세요. 계좌 조회·주문은 하지 않습니다.',flush=True)
    if not kw.login():raise SystemExit('로그인을 완료하지 못했습니다')
    if str(kw.call('GetLoginInfo(QString)','GetServerGubun'))=='1':
        raise SystemExit('모의투자 접속입니다. 실서버로 다시 로그인하세요.')
    spec=probe_spec(kw.call('GetAPIModulePath()'))
    future=str(kw.call('GetFutureCodeByIndex(int)',0) or '').strip()
    if not future:raise SystemExit('현재 선물 코드를 받지 못했습니다')
    reports=[]
    def done(result,message,investor,label):
        reports.append(inspect_response(result,future,investor,label,now_kst().isoformat(),message))
        save_report(reports)
        if len(reports)==len(INVESTORS):app.quit()
    for investor,label,code in INVESTORS:
        inputs={'일자구분':'1','일자':now_kst().strftime('%Y%m%d'),'투자자구분':code,'수량금액구분':'1'}
        kw.request(spec,inputs,lambda result,message,investor=investor,label=label:done(result,message,investor,label))
    QTimer.singleShot(30000,app.quit)
    app.exec_()
    if len(reports)<len(INVESTORS):
        save_report(reports)
        print('일부 요청 응답을 받지 못했습니다. 결과 파일에서 확인하세요.',flush=True)
    print('점검 결과: data/투자자수급_연결점검.txt',flush=True)


if __name__=='__main__':main()
