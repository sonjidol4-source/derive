"""영웅문 [0251] 투자자 매매현황처럼 시장 × 투자자 매도·매수·순매수 표(LS t1601 투자자별종합, 통합 거래소).

2026-10-29 14:44:30 영웅문 0251(통합·금액)과 14:45:12 LS t1601(exchgubun=U, 금액) 대조:
  블록1 거래소 개인 +8,116/+8,122 · 외국인 −2,439/−2,494 · 기관계 −8,033/−7,990 (40초 차이만큼 움직임)
  블록2 코스닥, 블록4 콜옵션(개인 −11/−10), 블록5 풋옵션(개인 −13/−13)도 같은 흐름.
  블록3은 KOSPI200 선물만(레이더 t1602 선물 값과 같음) — 0251의 '선물합산'(미니 등 포함)과 다르다.
  블록6은 0251 '주식선물'과 맞지 않고 정체를 확인하지 못해 표시하지 않는다.
조회만 하며 키·토큰은 저장하지 않는다.
"""
import datetime as dt
import json
from common import ROOT,atomic_json,in_session,now_kst,number,stamp

BLOCK={'gubun1':'2','gubun2':'2','gubun3':'','gubun4':'2','exchgubun':'U'}  # 주식·옵션·선물 모두 금액, 통합(KRX+NXT)
MARKETS=((1,'stock','거래소'),(2,'kosdaq','코스닥'),(3,'k200f','KOSPI200 선물'),(4,'call','콜옵션'),(5,'put','풋옵션'))
# 0251 열 순서. 코드는 LS 투자자 코드(05 종금 = 0251 '기타금융', 06 기금 = '연기금', 07 기타 = '기타법인').
INVESTORS=(('08','개인'),('17','외국인'),('18','기관계'),('01','금융투자'),('02','보험'),('03','투신'),('04','은행'),
           ('05','기타금융'),('06','연기금'),('00','사모펀드'),('07','기타법인'))
FILE='ls_investor_board.json'
STALE_SEC=90


def parse(body,received):
    if str(body.get('rsp_cd',''))!='00000':raise ValueError('LS t1601 응답 오류 %s %s'%(body.get('rsp_cd'),body.get('rsp_msg')))
    markets=[]
    for n,key,label in MARKETS:
        block=body.get('t1601OutBlock%d'%n)
        if not isinstance(block,dict) or not block:raise ValueError('LS t1601 %s(블록%d) 응답이 비었습니다'%(label,n))
        rows=[]
        for code,name in INVESTORS:
            sell,buy,net=(number(block.get(f+code)) for f in ('md_','ms_','svolume_'))
            rows.append({'code':code,'label':name,'sell':sell,'buy':buy,'net':net})
        markets.append({'key':key,'label':label,'rows':rows})
    return {'generated_at':received.isoformat(),'source':'LS t1601 투자자별종합','exchange':'통합(KRX+NXT)','unit':'억원','markets':markets,
            'note':'영웅문 0251(통합·금액)과 같은 구성. 선물은 KOSPI200 선물만(미니 등 포함 "선물합산"과 다름)이고 주식선물은 LS t1601에 없습니다.'}


def save(board,directory=None):
    atomic_json((directory or ROOT/'data')/FILE,board)


def view(directory=None,cfg=None,at=None):
    try:board=json.loads(((directory or ROOT/'data')/FILE).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {'status':'empty','note':'장중 30초마다 LS t1601(통합)을 조회하면 표시됩니다.'}
    at=stamp(at or now_kst());age=(at-stamp(board['generated_at'])).total_seconds()
    live=bool(cfg) and in_session(at,cfg)
    board['status']='received';board['age_sec']=round(age)
    board['stale']=live and age>STALE_SEC
    board['mode']='live' if live and not board['stale'] else 'stale' if board['stale'] else 'archive'
    return board
