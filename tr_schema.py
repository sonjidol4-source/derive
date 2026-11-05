"""설치된 키움 공식 .enc에서 명세 확인. 미확인 enum·TR 번호 자동 추정 금지."""
import re
import zipfile
from pathlib import Path
from common import LABELS, number

def read_spec(path):
    with zipfile.ZipFile(path) as z:
        if sum(x.file_size for x in z.infolist())>2000000:
            raise ValueError('TR 정의 파일 크기가 비정상입니다')
        pieces=[]
        for name in z.namelist():
            b=z.read(name)
            try:pieces.append(b.decode('cp949'))
            except UnicodeDecodeError:pieces.append(b.decode('utf-8-sig'))
    return parse_spec('\n'.join(pieces), Path(path).stem.lower())

def parse_spec(text,code):
    result={'trcode':code,'title':'','inputs':[],'records':{}}
    section,record=None,None
    for raw in text.splitlines():
        line=raw.strip();upper=line.upper()
        if upper.startswith('[INPUT'):
            section='in';continue
        if upper.startswith('[OUTPUT'):
            section='out';continue
        if line.startswith('['):
            section=None;continue
        start=re.match(r'@START_?([^=\s]+)',line,re.I)
        if start:
            record=start.group(1)
            if section=='out':result['records'].setdefault(record,[])
            continue
        if upper.startswith('@END'):record=None;continue
        field=re.match(r'([^=@;]+?)\s*=',line)
        if not field:continue
        key=field.group(1).strip()
        if section is None and key.lower() in ('trname','name'):
            result['title']=line.split('=',1)[1].strip()
        elif section=='in' and key not in result['inputs']:
            result['inputs'].append(key)
        elif section=='out' and record:
            result['records'].setdefault(record,[]).append(key)
    if not result['title']:
        result['title']=next(iter(result['records']),code)
    elif not re.search('[가-힣]',result['title']):
        # Installed files use internal TRName values such as STOCK/FOSTOCK.
        # The Korean output record carries the user-facing query name.
        result['internal_name']=result['title']
        result['title']=next((name for name in result['records'] if re.search('[가-힣]',name)),result['title'])
    return result

def catalog(directory):
    root=Path(directory)
    out=[]
    data=root/'data'
    if not data.is_dir():data=root/'Data'
    for p in sorted(data.glob('*')):
        if p.suffix.lower()!='.enc' or not p.stem.lower().startswith('opt'):
            continue
        try:
            spec=read_spec(p)
            if re.search('투자자|프로그램|만기손익|포지션|선물.*현재|선물.*동향',spec['title']):
                out.append(spec)
        except (ValueError,zipfile.BadZipFile,UnicodeError,OSError):
            continue
    return out

def validate_source(src,spec):
    if src.get('trcode','').lower()!=spec['trcode'] or not re.fullmatch(r'opt\d{5}',spec['trcode']):
        raise ValueError('확인되지 않은 조회 TR입니다')
    if re.search('주문|잔고|계좌|체결요청',spec['title']):
        raise ValueError('시장정보 조회 TR만 사용합니다')
    if set(src.get('inputs',{}))!=set(spec['inputs']):
        raise ValueError('공식 명세의 모든 입력 이름을 정확히 지정하세요: '+', '.join(spec['inputs']))
    if not src.get('fields'):
        raise ValueError('출력 필드 연결이 비었습니다')
    if any('fut_qty' in k or k.endswith('_amt') for k in src['fields']) and src.get('scope')!='cumulative_day':
        raise ValueError('투자자 수급은 scope=cumulative_day인 당일 순매매 누적만 연결하세요. 만기 누적·추정 보유량은 연결할 수 없습니다')
    allfields={f for values in spec['records'].values() for f in values}
    for target,m in src['fields'].items():
        if target not in LABELS:raise ValueError('허용되지 않은 출력 키: '+target)
        if m.get('record') not in spec['records']:raise ValueError('출력 레코드 이름 확인 필요')
        if m.get('field') not in spec['records'][m['record']]:raise ValueError('출력 필드 이름 확인 필요')
        if m.get('subtract') and m['subtract'] not in spec['records'][m['record']]:raise ValueError('차감 필드 이름 확인 필요')
        if number(m.get('scale',1)) is None:raise ValueError('단위 환산 scale 확인 필요')
        where=m.get('where')
        if where and where.get('field') not in allfields:raise ValueError('선택 기준 필드 확인 필요')

def extract_source(src,result):
    out={}
    for target,m in src['fields'].items():
        rows=result.get(m['record'],[])
        if m.get('where'):
            where=m['where']
            rows=[r for r in rows if str(r.get(where['field'],'')).strip()==str(where['equals'])]
            if len(rows)!=1:continue
            row=rows[0]
        else:
            index=int(m.get('row',0))
            if not 0<=index<len(rows):continue
            row=rows[index]
        value=number(row.get(m['field']))
        if m.get('subtract'):
            other=number(row.get(m['subtract']))
            value=None if value is None or other is None else value-other
        if value is not None:out[target]=value*float(m.get('scale',1))
    return out

def normalize_strikes(raw,reference):
    """PDF/KOA의 행사가 표현 차이를 실제 기초지수와 대조하여 해결."""
    values=[number(s) for s in raw]
    values=[v for v in values if v is not None and v>0]
    if not values or not reference:
        raise ValueError('행사가와 기초지수 대조가 필요합니다')
    candidates=[]
    for scale in (1,.01):
        distance=min(abs(v*scale/reference-1) for v in values)
        candidates.append((distance,scale))
    distance,scale=min(candidates)
    if distance>.25:
        raise ValueError('행사가 단위를 기초지수와 대조하지 못했습니다')
    return sorted(set(round(v*scale,2) for v in values)),scale
