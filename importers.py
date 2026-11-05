import csv
import io
from common import LABELS,number,stamp

def flow_packet(text,source='HTS CSV · 사용자 확인',current_fut=''):
    rows=list(csv.DictReader(io.StringIO(text.lstrip('﻿'))))
    if not rows or len(rows)>2000:raise ValueError('수급 CSV 행 수를 확인하세요')
    for r in rows:stamp(r['asof'])
    r=max(rows,key=lambda x:stamp(x['asof']))
    values={k:number(v) for k,v in r.items() if k in LABELS and v and v.strip()}
    if any(values.get(k) is None for k,v in r.items() if k in LABELS and v and v.strip()):
        raise ValueError('숫자가 아닌 수급 값이 있습니다')
    if not values:raise ValueError('연결할 수급 열이 없습니다')
    code=(r.get('fut_code') or '').strip()
    # 다른 선물 코드는 엔진의 선물 구간을 초기화하므로, 현재 근월물과 다르면 받지 않는다.
    if code and current_fut and code!=current_fut:
        raise ValueError('CSV 선물 코드 '+code+'가 현재 근월물 '+current_fut+'와 다릅니다. 비워 두거나 현재 코드를 넣으세요')
    return {'source':source,'asof':stamp(r['asof']).isoformat(),
        'fut_code':code,'values':values,'quality':'사용자 입력 잠정','scope':'cumulative_day'}
