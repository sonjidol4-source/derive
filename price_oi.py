"""선물 가격·시장 전체 미결제(OI) 4분면과, 외국인 선물·현물·기관계를 엮은 사실/추론/한계 해석.

네트워크·저장소에 의존하지 않는 순수 함수만 둔다(엔진의 장중 구간과 마감 요약이 같이 쓴다).
2026-07 카이로스 분석 방식을 따른다: 외국인 선물 순매수 자체보다, 그때 OI와 가격이 어떻게 움직였는지를 같이 본다.
  7/24 외국인 +12,701계약 · OI −1,921 · 현물 −3.28조 → 숏헤지 청산(숏커버) 가능성
  7/27 외국인 −3,434 · OI +3,489 · 기관계 +3,513 · 가격 상승 → 외국인 신규 숏·기관계 신규 롱 가능성
  7/28 외국인 −1,743 · OI +978 · 가격 하락 → 신규 숏 유입 가능성
OI는 시장 전체 값이라 증감폭을 외국인의 신규·청산 계약 수로 쓰지 않는다. 추론은 '가능성'으로만 말한다.
"""

DEFAULTS={'price_pct':{'m5':0.08,'m15':0.15,'day':0.3},'oi':{'m5':100,'m15':200,'day':500},
    'foreign':{'m5':300,'m15':600,'day':1000},'spot_eok':{'m5':100,'m15':200,'day':1000},'counterpart_ratio':0.5}

# 가격 방향 × 시장 전체 미결제 방향. 어느 투자자가 포지션을 늘리고 줄였는지는 알 수 없다.
# 2026-10-23 검토 반영: 시장 전체 모양만 이름 붙인다. '신규 매수 우위' 같은 이름은 특정 투자자의 신규 롱으로 읽혀 바꿨다.
QUADRANTS={(1,1):('가격↑·미결제↑','시장 전체로 새 계약이 늘며 가격이 오름 · 누가 신규로 샀는지는 알 수 없음',1),
    (1,-1):('가격↑·미결제↓','시장 전체로 청산이 많으며 가격이 오름 · 숏커버일 수 있으나 주체는 알 수 없음',1),
    (-1,1):('가격↓·미결제↑','시장 전체로 새 계약이 늘며 가격이 내림 · 누가 신규로 팔았는지는 알 수 없음',-1),
    (-1,-1):('가격↓·미결제↓','시장 전체로 청산이 많으며 가격이 내림 · 롱 정리일 수 있으나 주체는 알 수 없음',-1)}


def rules(cfg):
    """config의 rules.price_oi에 빠진 항목은 기본값으로 채운다."""
    got=(cfg.get('rules') or {}).get('price_oi') or {}
    return {k:({**v,**got.get(k,{})} if isinstance(v,dict) else got.get(k,v)) for k,v in DEFAULTS.items()}


def direction(v,th):
    return None if v is None else 1 if v>=th else -1 if v<=-th else 0


def price_oi_quadrant(price_pct, oi_change, price_th, oi_th):
    base={'price_pct':price_pct,'oi_change':oi_change,'price_dir':None,'oi_dir':None,'side':0}
    if price_pct is None or oi_change is None:
        return {**base,'label':'자료 부족','text':'가격 또는 미결제 관측이 끊겨 판정하지 않습니다.'}
    pd,od=direction(price_pct,price_th),direction(oi_change,oi_th)
    base.update(price_dir=pd,oi_dir=od)
    if pd and od:
        label,text,side=QUADRANTS[(pd,od)]
        return {**base,'label':label,'text':text,'side':side}
    if pd:
        return {**base,'label':'가격만 움직임','text':'미결제 변화가 작아 신규·청산을 구분하지 않습니다.'}
    if od:
        return {**base,'label':'미결제만 변화','text':'가격 변화가 작아 매수·매도 방향을 정하지 않습니다.'}
    return {**base,'label':'변화 작음','text':'가격·미결제 모두 기준보다 작게 움직였습니다.'}


def sgn(v,digits=0):
    return '—' if v is None else format(v,'+,.%df'%digits)


def eok(v):
    """억원 → 1조 이상은 조 단위로."""
    return sgn(v/10000,2)+'조원' if v is not None and abs(v)>=10000 else sgn(v)+'억원'


def reading(window, foreign, oi, price_pct, spot=None, inst=None, cfg=None, scope=''):
    """한 구간(m5·m15·day)의 사실/추론/한계. foreign·inst는 계약, spot은 외국인 현물 억원, oi는 시장 전체 계약 증감."""
    r=rules(cfg or {})
    fd=direction(foreign,r['foreign'][window]);od=direction(oi,r['oi'][window])
    pd=direction(price_pct,r['price_pct'][window]);sd=direction(spot,r['spot_eok'][window])
    facts,inferences,limits=[],[],[]
    if foreign is not None:
        facts.append('외국인 선물 %s계약 %s'%(sgn(foreign),'순매수' if foreign>0 else '순매도' if foreign<0 else ''))
    if oi is not None or price_pct is not None:
        facts.append('시장 전체 미결제 %s계약 · 선물가격 %s%%'%(sgn(oi),sgn(price_pct,2)))
    if spot is not None:
        facts.append('외국인 현물 %s'%eok(spot))
    if inst is not None:
        facts.append('기관계 선물 %s계약'%sgn(inst))
    side=0
    if fd is None:
        inferences.append('외국인 선물 수급이 없어 외국인 포지션은 해석하지 않습니다.')
    elif fd==0:
        inferences.append('외국인 선물 변화가 기준(%s계약)보다 작아 외국인 포지션 해석을 보류합니다.'%format(r['foreign'][window],','))
    elif od is None:
        inferences.append('미결제 관측이 없어 외국인 %s가 신규인지 청산인지 구분하지 않습니다.'%('매수' if fd>0 else '매도'))
    elif od==0:
        inferences.append('외국인 %s가 컸지만 미결제 변화가 작아 신규·청산을 구분하지 않습니다(기존 계약의 손바뀜일 수 있음).'%('매수' if fd>0 else '매도'))
    else:
        # 2026-10-23 검토 반영: 시장 전체 미결제와 투자자 집단 순매수로는 외국인의 신규·청산, 실제 거래 상대방, 가격 주도 주체를
        # 식별할 수 없다. 관측 조합만 말하고 귀속은 '구분 불가'로 남긴다. side는 외국인 순매매 방향(가설 검증용)이다.
        side=fd
        buy='순매수' if fd>0 else '순매도'
        market='증가(시장 전체로는 새 계약 우세)' if od>0 else '감소(시장 전체로는 청산 우세)'
        inferences.append('외국인 선물 %s · 시장 미결제 %s. 외국인 자신이 신규였는지 청산이었는지는 이 자료로 구분할 수 없습니다.'%(buy,market))
        if sd is not None and sd!=0:
            same=(sd==fd)
            inferences.append('같은 기간 외국인 현물은 %s로 선물과 %s 방향입니다. 같은 계좌의 헤지·차익인지는 집계 자료로 확인할 수 없습니다.'
                              %('순매수' if sd>0 else '순매도','같은' if same else '반대'))
        if inst is not None and inst*foreign<0 and abs(inst)>=r['counterpart_ratio']*abs(foreign):
            inferences.append('기관계 선물 %s계약으로 외국인과 반대 방향 순매매입니다(집계상 반대일 뿐 실제 거래 상대방은 알 수 없음).'%sgn(inst))
        if pd and pd!=fd:
            inferences.append('가격은 외국인 순매매와 반대로 움직였습니다. 가격을 누가 움직였는지는 이 자료로 식별할 수 없습니다.')
    limits.append('미결제는 시장 전체 값이라 증감폭이 외국인의 신규·청산 계약 수를 뜻하지 않습니다.')
    limits.append('순매매는 %s 거래이며 실제 보유 포지션이 아닙니다.'%(scope or '이 구간의'))
    if spot is None and fd:
        limits.append('외국인 현물 수급이 없어 헤지 여부를 교차 확인하지 못했습니다.')
    limits.append('해석 규칙은 과거 사례 몇 건에 맞춘 초기 규칙이며 검증 전입니다.')
    return {'window':window,'facts':facts,'inferences':inferences,'limits':limits,'side':side,
        'headline':inferences[0] if inferences else ''}
