"""LS증권 OPEN API로 키움 OCX(로그인 창 필요)의 파생 시세 역할을 대신한다. 조회 전용.

2026-10-21 1회씩 확인:
- t8467 지수선물 마스터 → 첫 행 'F 2612' A016C000이 근월물.
- t2111 선물 현재가: price, mgjv(미결제)·mgjvdiff, sbasis(=price−kospijisu)·ibasis(=theoryprice−kospijisu), kospijisu, jnilclose(전일 종가).
  응답에 체결 시각이 없어 수신 시각을 데이터 시각으로 쓴다.
- t2301 옵션 전광판(yyyymm, gubun=G): 콜(OutBlock1)·풋(OutBlock2) 각 343개 행사가 전부. delt는 소수(키움은 %).
- t1602 시장 5(업종 800)·6(업종 700): 옵션 투자자 합계.
  2026-10-22 한투 시장별 투자자매매동향(콜 OC01·풋 OP01, 공식 이름표)과 대조해 시장구분 5 = 풋, 6 = 콜로 확인했다
  (외국인·기관계·개인·금융투자 8개 값 모두 일치. 처음 추정 5=콜·6=풋은 반대였다).
  행사가별 투자자 수량(키움 opt50038)은 LS에 없어 델타 환산은 하지 않는다.
"""
import datetime as dt
from common import ROOT,atomic_json,last_trading_day,nearest_month,number

FO_PATH='/futureoption/market-data'
INVESTOR_PATH='/stock/investor'
INVESTORS=[('17','foreign','외국인'),('18','inst','기관계'),('08','individual','개인'),('01','financial','금융투자')]
# 2026-10-22 한투 이름표와 대조해 확인: 시장구분 5 = 풋, 6 = 콜.
OPTION_MARKETS=[{'key':'m5','market':'5','upcode':'800','guess':'풋'},{'key':'m6','market':'6','upcode':'700','guess':'콜'}]
MAPPING_CONFIRMED=True
SIDE_BY_MARKET={m['market']:m['guess'] for m in OPTION_MARKETS}
NOTE='옵션 당일 순매수 합계(계약, 금액은 억원 · 2026-10-22 한투 백만원 값과 대조). 행사가별 수량이 아니어서 델타 환산을 하지 않습니다.'


def market_label(m):
    return m['guess']+'옵션' if MAPPING_CONFIRMED else '시장구분 %s (%s 추정 · 콜·풋 미확정)'%(m['market'],m['guess'])


def relabel(text):
    """확인 전에 저장된 문구('시장구분 5 (콜 추정 · 콜·풋 미확정)')를 확인된 이름으로 바꾼다."""
    import re
    return re.sub(r'시장구분 ([56]) \([콜풋] 추정 · 콜·풋 미확정\)',lambda m:SIDE_BY_MARKET[m.group(1)]+'옵션',str(text))


def near_future(body):
    """t8467 행 중 'F YYMM' 첫 종목(근월물). 스프레드·미니 등은 건너뛴다."""
    for row in body.get('t8467OutBlock') or []:
        name=str(row.get('hname','')).strip()
        if name.startswith('F ') and name[2:].isdigit() and row.get('shcode'):return str(row['shcode']).strip()
    raise ValueError('LS t8467에서 근월 선물 코드를 찾지 못했습니다')


def futures_values(body):
    out=body.get('t2111OutBlock') or {}
    price=number(out.get('price'))
    if not price:raise ValueError('LS t2111 현재가가 비었습니다')
    values={'fut_price':abs(price)}
    for key,field in (('fut_oi','mgjv'),('fut_oi_prev','mgjvdiff'),('basis','sbasis'),('basis_theory','ibasis'),('k200','kospijisu'),('fut_prev_close','jnilclose')):
        v=number(out.get(field))
        if v is not None:values[key]=abs(v) if key in ('k200','fut_prev_close') else v
    return values


def futures_packet(body,code,received):
    return {'asof':received.isoformat(),'source':'LS t2111','fut_code':code,'values':futures_values(body),
            'quality':'장중 잠정 · 조회 수신 시각','scope':'market'}


def futures_snapshot(body,code,received_at):
    return {'kind':'futures','code':code,'received_at':received_at,'market_asof':None,'values':futures_values(body),
            'source':'LS t2111','note':'조회 응답의 마지막 시세입니다. 체결 시각이 없어 수신 시각으로 표시합니다.'}


def option_months(at,cfg):
    """이번 달부터 석 달 중 만기가 남은 가장 가까운 월물."""
    y,m=at.year,at.month;months=[]
    for _ in range(3):
        months.append('%04d%02d'%(y,m));y,m=(y+1,1) if m==12 else (y,m+1)
    return nearest_month(months,at,cfg)


def option_snapshots(body,month,received_at):
    """t2301 → 키움 opt50021/22와 같은 형식의 calls·puts 스냅숏. delta_raw는 키움과 맞춰 % 단위로 둔다."""
    out=[]
    for kind,cp,block in (('calls','C','t2301OutBlock1'),('puts','P','t2301OutBlock2')):
        rows=[];seen=set()
        for row in body.get(block) or []:
            code=str(row.get('optcode','')).strip();strike=number(row.get('actprice'))
            if not code or strike is None or code in seen:continue
            seen.add(code);item={'code':code,'strike':strike,'cp':cp,'month':month}
            for key,field in (('price','price'),('oi','mgjv'),('iv','iv')):
                v=number(row.get(field))
                if v is not None:item[key]=abs(v) if key=='price' else v
            d=number(row.get('delt'))
            if d is not None:item['delta_raw']=d*100
            rows.append(item)
        if not rows:raise ValueError('LS t2301 %s 행이 비었습니다'%('콜' if cp=='C' else '풋'))
        out.append({'kind':kind,'month':month,'received_at':received_at,'market_asof':None,'rows':rows,
            'source':'LS t2301','pages':1,'listed_count':len(rows),'missing_count':0,
            'listed_range':[min(r['strike'] for r in rows),max(r['strike'] for r in rows)],'complete':True,
            'note':'LS 옵션 전광판은 상장 행사가 전부를 한 번에 준다. delta_raw = LS delt(소수) × 100.'})
    if out[0]['listed_count']!=out[1]['listed_count']:
        # 콜·풋 행사가 수가 다르면 일부가 빠졌을 수 있다 → max pain 보류.
        for s in out:s['complete']=False
    return out


def investor_row(body):
    rows=body.get('t1602OutBlock1') or []
    if not rows:raise ValueError('LS t1602 시간대 행이 없습니다')
    row=rows[0];values={}
    for code,key,_ in INVESTORS:
        v=row.get('sv_'+code)
        if isinstance(v,(int,float)) and not isinstance(v,bool):values[key]=float(v)
    return str(row.get('time',''))[:6],values


def option_investor_report(parts,received,cfg):
    """parts: [(market dict, qty body, amt body)] → 저장·화면용 합계."""
    groups=[]
    for m,qty,amt in parts:
        t,q=investor_row(qty);_,a=investor_row(amt) if amt else ('',{})
        groups.append({'key':m['key'],'market':m['market'],'upcode':m['upcode'],'label':market_label(m),
            'time':t,'qty':q,'amt':a})
    return {'generated_at':received.isoformat(),'data_day':last_trading_day(received,cfg).isoformat(),
        'source':'LS t1602 시장구분 5·6','mapping_confirmed':MAPPING_CONFIRMED,'groups':groups,
        'investors':[[key,label] for _,key,label in INVESTORS],
        'note':NOTE
              +('' if MAPPING_CONFIRMED else ' 콜·풋 구분 미확정: 어느 쪽이 콜·풋인지 영웅문 0780과 대조 전이라 방향 해석을 하지 않습니다.')}


def save_report(report,directory=None):
    atomic_json((directory or ROOT/'data')/'ls_option_investors.json',report)


def load_report(directory=None):
    import json
    try:return json.loads(((directory or ROOT/'data')/'ls_option_investors.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return None


class Derivatives:
    """LsFeed가 토큰·호출 간격을 공유하며 부른다. call(path,tr,block)은 검증된 응답 body를 돌려준다."""

    def __init__(self,cfg,engine,call,wait,directory=None):
        self.cfg,self.engine,self.call,self.wait=cfg,engine,call,wait
        # 옵션 TR(t2301·옵션 t1602)만 응답 대기를 길게 쓰도록 LsFeed가 바꿔 끼운다. 기본은 공통 call.
        self.option_call=call
        self.directory=directory or engine.store.directory
        self.code=None;self.code_day=None;self.last_options=None;self.after_close_done=None

    def future_code(self,at):
        if self.code and self.code_day==at.date():return self.code
        body=self.call(FO_PATH,'t8467',{'gubun':''});self.wait()
        self.code=near_future(body);self.code_day=at.date()
        return self.code

    def futures(self,at,clock,ingest=True):
        code=self.future_code(at)
        body=self.call(FO_PATH,'t2111',{'focode':code});received=clock()
        snapshot=futures_snapshot(body,code,received.isoformat())
        atomic_json(self.directory/'snapshot_futures.json',{'snapshot':snapshot,'response':body})
        if ingest:
            self.engine.ingest(futures_packet(body,code,received),received)
            self.engine.ingest({'market_snapshot':snapshot},received)
        self.wait()
        return snapshot

    def options(self,at,clock,ingest=True):
        """전광판과 투자자 합계를 차례로(장외 저장·테스트용). 장중 LsFeed는 두 갈래를 따로 부르고 따로 재시도한다."""
        snaps=self.option_board(at,clock,ingest)
        return snaps,self.option_investors(clock)

    def option_board(self,at,clock,ingest=True):
        """t2301 옵션 전광판. 입력이 월물·구분뿐이라 행사가 범위나 콜·풋을 나눠 요청할 수 없다(2026-10-29 공식 명세 확인).
        한 번에 콜·풋 전 행사가가 온다. 그래서 줄일 수 있는 건 응답 대기·재시도뿐이다."""
        month=option_months(at,self.cfg)
        if not month:raise ValueError('만기가 남은 옵션 월물을 찾지 못했습니다')
        try:body=self.option_call(FO_PATH,'t2301',{'yyyymm':month,'gubun':'G'})
        finally:self.wait()  # 실패해도 다음 LS 호출과 간격을 둔다
        received=clock()
        snaps=option_snapshots(body,month,received.isoformat())
        for s in snaps:
            atomic_json(self.directory/('snapshot_'+s['kind']+'.json'),{'snapshot':s,'response':{'source':'LS t2301','rows':len(s['rows'])}})
            if ingest:self.engine.ingest({'market_snapshot':s},received)
        self.last_options=received
        return snaps

    def option_investors(self,clock):
        """t1602 시장구분 5·6 옵션 투자자 합계(수량·금액). 전광판이 실패해도 따로 갱신한다."""
        parts=[]
        for m in OPTION_MARKETS:
            bodies=[]
            for gubun1 in ('1','2'):
                try:bodies.append(self.option_call(INVESTOR_PATH,'t1602',{'market':m['market'],'upcode':m['upcode'],'gubun1':gubun1,'gubun2':'0',
                    'cts_time':'','cts_idx':0,'cnt':1,'gubun3':'','exchgubun':'K'}))
                finally:self.wait()
            parts.append((m,bodies[0],bodies[1]))
        report=option_investor_report(parts,clock(),self.cfg)
        save_report(report,self.directory)
        return report

    def session_tick(self,at,clock,option_due):
        self.futures(at,clock)
        if option_due:self.options(at,clock)

    def after_close(self,at,clock):
        """장 마감 뒤 저녁에 볼 수 있게 마지막 거래일 시세·옵션을 한 번 저장한다. 16:10 이후 1회."""
        day=last_trading_day(at,self.cfg)
        ready=at.date()!=day or at.strftime('%H:%M')>='16:10'
        if not ready or self.after_close_done==day:return False
        try:
            import json
            got=json.loads((self.directory/'snapshot_calls.json').read_text(encoding='utf-8'))['snapshot']
            # 이미 그 거래일 16:10 이후 LS로 받아 둔 게 있으면(재시작 등) 다시 부르지 않는다.
            if got.get('source')=='LS t2301' and got['received_at']>=day.isoformat()+'T16:10' and load_report(self.directory):
                self.after_close_done=day;return False
        except (OSError,ValueError,KeyError):pass
        self.futures(at,clock,ingest=False);self.options(at,clock,ingest=False)
        self.after_close_done=day
        return True
