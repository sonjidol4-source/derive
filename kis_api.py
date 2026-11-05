"""한국투자증권(KIS) Open API로 KRX 야간선물(KOSPI200 근월물) 실시간 시세를 받는다. 조회 전용(주문·계좌 없음).

근거(2026-10-22 확인):
- 한국투자증권 공식 저장소 koreainvestment/open-trading-api, examples_llm/domestic_futureoption
  · KRX야간선물 실시간종목체결 H0MFCNT0 [실시간-064], 실시간호가 H0MFASP0 [실시간-065]와 각 컬럼 순서
  · 웹소켓 접속키: POST /oauth2/Approval {grant_type, appkey, secretkey} → approval_key
  · 웹소켓 주소 ws://ops.koreainvestment.com:21000 (kis_devlp.yaml 'ops'), 예제 경로 /tryitout
  · PINGPONG을 받으면 같은 내용으로 pong을 돌려준다
- 지수선물옵션 종목정보파일 fo_idx_code_mts.mst: '|' 구분 9열
  (상품종류·단축코드·표준코드·한글종목명·ATM구분·행사가·월물구분코드·기초자산 단축코드·기초자산명)
- 외부 공개 프로젝트(codeyoma/krx-quant #264)가 2026-10-21 23:31 A01612(F 202612)로 두 TR 수신을 기록했다.
  이 PC에서의 실수신은 아직 확인 전이다.

키·접속키는 화면·파일·로그에 쓰지 않는다. 접속키는 실행마다 새로 받고 저장하지 않는다.
발급할 때마다 알림톡이 가는 접근토큰(/oauth2/tokenP)은 이 모듈에서 쓰지 않는다.
"""
import datetime as dt
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

from common import KST, atomic_json, monthly_expiry, now_kst, number, stamp

REST = 'https://openapi.koreainvestment.com:9443'
WS = 'ws://ops.koreainvestment.com:21000'
WS_PATHS = ('/tryitout', '')
MASTER_URL = 'https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip'
DEFAULT_CREDENTIALS = r'C:\Users\HP\Documents\Codex\secrets\kis_openapi.json'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) deriv-radar'

TR_TRADE = 'H0MFCNT0'
TR_QUOTE = 'H0MFASP0'

TRADE_COLUMNS = [
    'futs_shrn_iscd', 'bsop_hour', 'futs_prdy_vrss', 'prdy_vrss_sign', 'futs_prdy_ctrt', 'futs_prpr',
    'futs_oprc', 'futs_hgpr', 'futs_lwpr', 'last_cnqn', 'acml_vol', 'acml_tr_pbmn', 'hts_thpr',
    'mrkt_basis', 'dprt', 'nmsc_fctn_stpl_prc', 'fmsc_fctn_stpl_prc', 'spead_prc', 'hts_otst_stpl_qty',
    'otst_stpl_qty_icdc', 'oprc_hour', 'oprc_vrss_prpr_sign', 'oprc_vrss_nmix_prpr', 'hgpr_hour',
    'hgpr_vrss_prpr_sign', 'hgpr_vrss_nmix_prpr', 'lwpr_hour', 'lwpr_vrss_prpr_sign', 'lwpr_vrss_nmix_prpr',
    'shnu_rate', 'cttr', 'esdg', 'otst_stpl_rgbf_qty_icdc', 'thpr_basis', 'futs_askp1', 'futs_bidp1',
    'askp_rsqn1', 'bidp_rsqn1', 'seln_cntg_csnu', 'shnu_cntg_csnu', 'ntby_cntg_csnu', 'seln_cntg_smtn',
    'shnu_cntg_smtn', 'total_askp_rsqn', 'total_bidp_rsqn', 'prdy_vol_vrss_acml_vol_rate', 'dynm_mxpr',
    'dynm_llam', 'dynm_prc_limt_yn',
]
QUOTE_COLUMNS = (
    ['futs_shrn_iscd', 'bsop_hour']
    + ['futs_askp%d' % i for i in range(1, 6)] + ['futs_bidp%d' % i for i in range(1, 6)]
    + ['askp_csnu%d' % i for i in range(1, 6)] + ['bidp_csnu%d' % i for i in range(1, 6)]
    + ['askp_rsqn%d' % i for i in range(1, 6)] + ['bidp_rsqn%d' % i for i in range(1, 6)]
    + ['total_askp_csnu', 'total_bidp_csnu', 'total_askp_rsqn', 'total_bidp_rsqn',
       'total_askp_rsqn_icdc', 'total_bidp_rsqn_icdc']
)
COLUMNS = {TR_TRADE: TRADE_COLUMNS, TR_QUOTE: QUOTE_COLUMNS}

# 체결 메시지 → 저장·화면용 이름. 단위는 원문 그대로(가격 p, 수량 계약). 거래대금 단위는 미확인.
TRADE_FIELDS = {
    'fut_price': 'futs_prpr', 'fut_open': 'futs_oprc', 'fut_high': 'futs_hgpr', 'fut_low': 'futs_lwpr',
    'last_qty': 'last_cnqn', 'volume': 'acml_vol', 'trade_value_raw': 'acml_tr_pbmn',
    'theory': 'hts_thpr', 'basis': 'mrkt_basis', 'basis_theory': 'thpr_basis', 'disparity': 'dprt',
    'oi': 'hts_otst_stpl_qty', 'oi_change': 'otst_stpl_qty_icdc', 'oi_change_last': 'otst_stpl_rgbf_qty_icdc',
    'strength': 'cttr', 'buy_vol': 'shnu_cntg_smtn', 'sell_vol': 'seln_cntg_smtn',
    'buy_count': 'shnu_cntg_csnu', 'sell_count': 'seln_cntg_csnu', 'ask1': 'futs_askp1', 'bid1': 'futs_bidp1',
    'ask_qty1': 'askp_rsqn1', 'bid_qty1': 'bidp_rsqn1', 'total_ask_qty': 'total_askp_rsqn',
    'total_bid_qty': 'total_bidp_rsqn', 'limit_up': 'dynm_mxpr', 'limit_down': 'dynm_llam',
}
FUT_NAME = re.compile(r'^F\s*(\d{6})$')


class KisError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류(키·접속키 값을 담지 않는다)."""


class MissingKeys(KisError):
    pass


# ---------------------------------------------------------------- 키 파일

def credentials_path(cfg):
    """config의 kis.credentials_file → 없으면 LS 키 파일과 같은 폴더의 kis_openapi.json."""
    kis = (cfg or {}).get('kis') or {}
    if kis.get('credentials_file'):
        return Path(kis['credentials_file'])
    ls = (cfg or {}).get('ls') or {}
    if ls.get('credentials_file'):
        return Path(ls['credentials_file']).parent / 'kis_openapi.json'
    return Path(DEFAULT_CREDENTIALS)


def load_credentials(path):
    path = Path(path)
    if not path.exists():
        raise MissingKeys('한투 키 파일이 아직 없습니다: %s' % path)
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise KisError('한투 키 파일을 읽을 수 없습니다(형식 확인 필요): %s' % path)
    key = str(data.get('app_key') or '').strip()
    secret = str(data.get('app_secret') or '').strip()
    if not key or not secret:
        raise MissingKeys('한투 키 파일에 app_key/app_secret이 비어 있습니다: %s' % path)
    return key, secret


def clean_key(value):
    """붙여넣을 때 섞여 들어오는 공백·따옴표·줄바꿈을 걷어낸다."""
    return re.sub(r'\s+', '', str(value or '')).strip('"\'')


def save_credentials(path, key, secret):
    key, secret = clean_key(key), clean_key(secret)
    if not key or not secret:
        raise KisError('앱키와 앱시크릿이 모두 필요합니다')
    atomic_json(path, {'app_key': key, 'app_secret': secret, 'saved_at': now_kst().isoformat(timespec='seconds'),
                       'note': '한국투자증권 KIS Developers 실전 키. 다른 사람·채팅에 보내지 않는다.'})
    return Path(path)


# ---------------------------------------------------------------- 접속키

def approval_key(http, key, secret, timeout=10):
    """웹소켓 접속키 발급. 실패하면 HTTP 상태와 KIS 오류 코드·문구만 담아 KisError."""
    try:
        r = http.post(REST + '/oauth2/Approval', timeout=timeout,
                      headers={'content-type': 'application/json; charset=utf-8', 'User-Agent': USER_AGENT},
                      json={'grant_type': 'client_credentials', 'appkey': key, 'secretkey': secret})
    except Exception as e:  # 네트워크 오류 종류가 많아 메시지만 남긴다
        raise KisError('한투 서버에 접속하지 못했습니다: %s' % type(e).__name__)
    try:
        body = r.json()
    except ValueError:
        body = {}
    value = body.get('approval_key') if isinstance(body, dict) else None
    if r.status_code != 200 or not value:
        detail = ' '.join(str(body.get(k) or '') for k in ('error_code', 'error_description', 'msg_cd', 'msg1')
                          if isinstance(body, dict)).strip()
        raise KisError('웹소켓 접속키 발급 실패 (HTTP %s) %s' % (r.status_code, detail[:200]))
    return value


# ---------------------------------------------------------------- REST 접근토큰·조회

def _key_hash(key):
    import hashlib
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]


def access_token(http, key, secret, cache_path, force=False):
    """REST 접근토큰. 발급 때마다 알림톡이 가므로 만료 30분 전까지는 저장본을 다시 쓴다.

    저장본은 키 파일과 같은 폴더(secrets)의 kis_token.json. 토큰 값은 화면·로그에 쓰지 않는다."""
    cache_path = Path(cache_path)
    if not force:
        try:
            saved = json.loads(cache_path.read_text(encoding='utf-8'))
            expires = dt.datetime.fromisoformat(saved['expires_at'])
            if saved.get('key_hash') == _key_hash(key) and expires - now_kst() > dt.timedelta(minutes=30):
                return saved['access_token'], False
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        r = http.post(REST + '/oauth2/tokenP', timeout=10,
                      headers={'content-type': 'application/json; charset=utf-8', 'User-Agent': USER_AGENT},
                      json={'grant_type': 'client_credentials', 'appkey': key, 'appsecret': secret})
    except Exception as e:
        raise KisError('한투 서버에 접속하지 못했습니다: %s' % type(e).__name__)
    try:
        body = r.json()
    except ValueError:
        body = {}
    token = body.get('access_token') if isinstance(body, dict) else None
    if r.status_code != 200 or not token:
        detail = ' '.join(str(body.get(k) or '') for k in ('error_code', 'error_description', 'msg_cd', 'msg1')
                          if isinstance(body, dict)).strip()
        raise KisError('접근토큰 발급 실패 (HTTP %s) %s' % (r.status_code, detail[:200]))
    try:
        expires = dt.datetime.strptime(body['access_token_token_expired'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
    except (KeyError, ValueError):
        expires = now_kst() + dt.timedelta(seconds=int(body.get('expires_in') or 21600))
    atomic_json(cache_path, {'access_token': token, 'expires_at': expires.isoformat(), 'key_hash': _key_hash(key),
                             'issued_at': now_kst().isoformat(timespec='seconds'),
                             'note': '한투 REST 접근토큰. 다른 사람·채팅에 보내지 않는다.'})
    return token, True


class Rest:
    """조회 전용 REST 호출. 토큰 만료 응답이면 한 번만 새로 받아 다시 부른다."""

    def __init__(self, http, key, secret, cache_path, min_gap=0.4):
        self.http, self.key, self.secret, self.cache_path = http, key, secret, Path(cache_path)
        self.min_gap, self._last, self.last_tr_cont = min_gap, 0.0, ''
        self.token, self.issued = access_token(http, key, secret, cache_path)

    def get(self, path, tr_id, params, timeout=10, tr_cont=''):
        """조회 1건. 응답 머리의 tr_cont(M·F면 다음 쪽 있음)는 self.last_tr_cont에 둔다.

        초당 건수 초과(EGW00201)면 1.1초 쉬고 한 번 더, 토큰 만료면 새로 받아 한 번 더 부른다."""
        retried_rate = retried_token = False
        while True:
            self._pace()
            headers = {'content-type': 'application/json; charset=utf-8', 'authorization': 'Bearer ' + self.token,
                       'appkey': self.key, 'appsecret': self.secret, 'tr_id': tr_id, 'custtype': 'P',
                       'tr_cont': tr_cont, 'User-Agent': USER_AGENT}
            try:
                r = self.http.get(REST + path, headers=headers, params=params, timeout=timeout)
            except Exception as e:
                raise KisError('%s 조회 실패: %s' % (tr_id, type(e).__name__))
            try:
                body = r.json()
            except ValueError:
                raise KisError('%s 응답이 JSON이 아닙니다 (HTTP %s)' % (tr_id, r.status_code))
            code = str(body.get('msg_cd') or '')
            if code == 'EGW00201' and not retried_rate:
                retried_rate = True
                time.sleep(1.1)
                continue
            if not retried_token and (r.status_code in (401, 403) or code in ('EGW00123', 'EGW00121')):
                retried_token = True
                self.token, self.issued = access_token(self.http, self.key, self.secret, self.cache_path, force=True)
                continue
            if r.status_code != 200 or str(body.get('rt_cd')) != '0':
                raise KisError('%s 응답 오류 (HTTP %s) %s %s' % (tr_id, r.status_code, code, str(body.get('msg1') or '')[:120]))
            self.last_tr_cont = str((getattr(r, 'headers', None) or {}).get('tr_cont', '') or '')
            return body

    def get_all(self, path, tr_id, params, keys, pages=6):
        """다음 쪽이 있으면(tr_cont M·F) 이어 받아 keys 목록을 합친다. pages 쪽에서 멈춘다."""
        merged = {k: [] for k in keys}
        body, cont, n = {}, '', 0
        while n < pages:
            body = self.get(path, tr_id, params, tr_cont=cont)
            n += 1
            for k in keys:
                v = body.get(k)
                merged[k].extend(v if isinstance(v, list) else ([v] if v else []))
            if self.last_tr_cont not in ('M', 'F'):
                break
            cont = 'N'
        return {**body, **merged, '_pages': n}

    def _pace(self):
        wait = self.min_gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


# ---------------------------------------------------------------- 근월물 코드

def session_day(at):
    """야간장 기준일. 06:00 전이면 전날 저녁에 시작한 장, 그 외에는 오늘 저녁 장."""
    at = stamp(at)
    return (at - dt.timedelta(days=1)).date() if at.hour < 6 else at.date()


def read_master(raw):
    for enc in ('cp949', 'utf-8'):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return raw.decode('cp949', 'replace').splitlines()


def kospi200_futures(lines):
    """종목정보파일 행 중 KOSPI200 선물('F YYYYMM') 월물. 스프레드·미니·옵션은 이름 형식이 달라 빠진다."""
    rows = []
    for line in lines:
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 4:
            continue
        m = FUT_NAME.match(parts[3])
        if not m or not parts[1]:
            continue
        rows.append({'code': parts[1], 'ym': m.group(1), 'name': parts[3],
                     'month_div': parts[6] if len(parts) > 6 else '',
                     'underlying': parts[8] if len(parts) > 8 else ''})
    k200 = [r for r in rows if r['code'].startswith('A01')
            or any(t in r['underlying'].upper().replace(' ', '') for t in ('KOSPI200', '코스피200'))]
    return k200


def fetch_master(http, directory, today):
    """종목정보파일을 하루 한 번 받아 둔다. 실패하면 KisError."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    mst = directory / 'fo_idx_code_mts.mst'
    mark = directory / 'fo_idx_code_mts.date'
    try:
        if mst.exists() and mark.read_text(encoding='utf-8').strip() == today.isoformat():
            return read_master(mst.read_bytes()), '오늘 받아 둔 종목정보파일'
    except OSError:
        pass
    try:
        r = http.get(MASTER_URL, timeout=20, headers={'User-Agent': USER_AGENT})
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith('.mst'))
            raw = z.read(name)
    except StopIteration:
        raise KisError('종목정보파일 압축 안에 .mst가 없습니다')
    except Exception as e:
        raise KisError('종목정보파일 받기 실패: %s' % type(e).__name__)
    tmp = mst.with_suffix('.tmp')
    tmp.write_bytes(raw)
    os.replace(str(tmp), str(mst))
    mark.write_text(today.isoformat(), encoding='utf-8')
    return read_master(raw), '종목정보파일 새로 받음'


def pick_contract(rows, at, cfg):
    """만기일이 야간장 기준일보다 뒤인 가장 가까운 월물. 만기일 저녁 장은 다음 월물로 넘어간다."""
    day = session_day(at)
    live = []
    for r in rows:
        try:
            if monthly_expiry(r['ym'], cfg) > day:
                live.append(r)
        except ValueError:
            continue
    return min(live, key=lambda r: r['ym']) if live else None


def rule_contract(at, cfg):
    """종목정보파일을 못 받을 때의 계산값: 분기월(3·6·9·12) 중 만기 전 첫 월물, 코드 'A01'+연도 끝자리+월."""
    day = session_day(at)
    y, m = day.year, day.month
    for _ in range(16):
        if m in (3, 6, 9, 12):
            ym = '%04d%02d' % (y, m)
            if monthly_expiry(ym, cfg) > day:
                return {'code': 'A01%d%02d' % (y % 10, m), 'ym': ym, 'name': 'F ' + ym, 'month_div': '', 'underlying': ''}
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise KisError('근월물을 계산하지 못했습니다')


def choose_contract(http, at, cfg, directory, override=None):
    """(계약 dict, 근거 문구). 종목정보파일 우선, 실패하면 규칙 계산값."""
    if override:
        return {'code': override, 'ym': '', 'name': '', 'month_div': '', 'underlying': ''}, '직접 지정한 코드'
    rule = rule_contract(at, cfg)
    try:
        lines, how = fetch_master(http, directory, stamp(at).date())
        picked = pick_contract(kospi200_futures(lines), at, cfg)
    except KisError as e:
        return rule, '규칙 계산(%s)' % e
    if not picked:
        return rule, '규칙 계산(종목정보파일에서 KOSPI200 선물 월물을 찾지 못함)'
    note = how
    if picked['code'] != rule['code']:
        note += ' · 규칙 계산값 %s와 다름' % rule['code']
    return picked, note


def ls_code_to_kis(code):
    """LS·키움 단축코드 'A016C000'(KOSPI200 2026-12) → 한투 'A01612'. 형식이 다르면 None."""
    m = re.match(r'^A01(\d)([1-9A-C])000$', str(code or '').strip())
    return 'A01%s%02d' % (m.group(1), int(m.group(2), 16)) if m else None


def kis_code_to_ls(code):
    """한투 'A01612' → LS·키움 형식 'A016C000'. 한투가 6자리 코드를 거절할 때 두 번째로 시도하는 값."""
    m = re.match(r'^A01(\d)(0[1-9]|1[0-2])$', str(code or '').strip())
    return 'A01%s%X000' % (m.group(1), int(m.group(2))) if m else None


# ---------------------------------------------------------------- 실시간 메시지

class SubscribeWatch:
    """구독 응답을 문장으로 모으고, 6자리 코드가 거절되면 LS 형식 코드로 한 번만 다시 구독할 목록을 돌려준다.

    접속키·중복 접속·구독 한도처럼 코드와 상관없는 거절이면 다시 시도하지 않는다."""

    def __init__(self, code, trs):
        self.code, self.trs = code, list(trs)
        self.alt = kis_code_to_ls(code)
        self.retried = False

    def on_system(self, msg):
        line = '구독 %s %s: %s (%s)' % (msg['tr_id'], msg['tr_key'], msg['msg'], msg['msg_cd'])
        unrelated = any(w in msg['msg'].upper() for w in ('APPROVAL', 'ALREADY', 'MAX'))
        if not msg['ok'] and msg['tr_key'] == self.code and self.alt and not self.retried and not unrelated:
            self.retried = True
            return line, [(tr, self.alt) for tr in self.trs]
        return line, None


def subscribe_message(approval, tr_id, code, tr_type='1'):
    return {'header': {'approval_key': approval, 'custtype': 'P', 'tr_type': tr_type, 'content-type': 'utf-8'},
            'body': {'input': {'tr_id': tr_id, 'tr_key': code}}}


def parse_message(text):
    """KIS 웹소켓 문자열 → dict(kind=data|ping|system|encrypted|unknown). 시스템 응답의 암호화 키(iv·key)는 버린다."""
    if text[:1] in ('0', '1'):
        parts = text.split('|', 3)
        if len(parts) < 4:
            return {'kind': 'unknown', 'text': text[:120]}
        enc, tr_id, count, payload = parts
        if enc == '1':
            return {'kind': 'encrypted', 'tr_id': tr_id}
        values = payload.split('^')
        n = int(count) if count.isdigit() and int(count) > 0 else 1
        cols = COLUMNS.get(tr_id)
        if not cols:
            return {'kind': 'data', 'tr_id': tr_id, 'records': [], 'count': n, 'width': len(values), 'unknown_tr': True}
        width = len(values) // n if len(values) % n == 0 else len(cols)
        records = [dict(zip(cols, values[i * width:i * width + len(cols)])) for i in range(n)]
        return {'kind': 'data', 'tr_id': tr_id, 'records': records, 'count': n,
                'width': width, 'expected': len(cols)}
    try:
        body = json.loads(text)
    except ValueError:
        return {'kind': 'unknown', 'text': text[:120]}
    header = body.get('header') or {}
    tr_id = str(header.get('tr_id') or '')
    if tr_id == 'PINGPONG':
        return {'kind': 'ping', 'tr_id': tr_id}
    b = body.get('body') or {}
    return {'kind': 'system', 'tr_id': tr_id, 'tr_key': str(header.get('tr_key') or ''),
            'ok': str(b.get('rt_cd', '')) == '0', 'msg_cd': str(b.get('msg_cd') or ''), 'msg': str(b.get('msg1') or '')}


def signed(value, sign):
    """KIS 대비 부호(1 상한·2 상승·3 보합·4 하한·5 하락). 값에 부호가 없어도 4·5면 음수로."""
    v = number(value)
    if v is not None and str(sign).strip() in ('4', '5') and v > 0:
        v = -v
    return v


def trade_values(rec):
    out = {'code': str(rec.get('futs_shrn_iscd', '')).strip(), 'time': str(rec.get('bsop_hour', '')).strip()[:6]}
    for key, field in TRADE_FIELDS.items():
        v = number(rec.get(field))
        if v is not None:
            out[key] = v
    for key in ('fut_price', 'fut_open', 'fut_high', 'fut_low', 'ask1', 'bid1', 'theory', 'limit_up', 'limit_down'):
        if key in out:
            out[key] = abs(out[key])
    sign = rec.get('prdy_vrss_sign', '')
    change, rate = signed(rec.get('futs_prdy_vrss'), sign), signed(rec.get('futs_prdy_ctrt'), sign)
    if change is not None:
        out['change'] = change
        if 'fut_price' in out:
            out['base_price'] = round(out['fut_price'] - change, 4)
    if rate is not None:
        out['change_pct'] = rate
    return out


def sane_trade(v):
    """컬럼이 밀렸는지 보는 최소 점검. 값의 옳고 그름이 아니라 형식만 본다."""
    problems = []
    p = v.get('fut_price')
    if p is None or not 50 <= p <= 5000:
        problems.append('현재가 %r' % p)
    if v.get('fut_high') is not None and v.get('fut_low') is not None and v['fut_high'] < v['fut_low']:
        problems.append('고가<저가')
    if p is not None and v.get('fut_high') and v.get('fut_low') and not v['fut_low'] - 0.01 <= p <= v['fut_high'] + 0.01:
        problems.append('현재가가 고저 범위 밖')
    if v.get('oi') is not None and v['oi'] < 1000:
        problems.append('미결제 %r' % v['oi'])
    if not re.match(r'^\d{6}$', v.get('time', '')):
        problems.append('시각 %r' % v.get('time'))
    return problems


# ---------------------------------------------------------------- 1분봉·요약

def trade_stamp(day, hhmmss):
    """야간장 기준일 + 체결시각(자정을 넘으면 다음 날)."""
    t = dt.time(int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))
    d = day + dt.timedelta(days=1) if t.hour < 12 else day
    return dt.datetime.combine(d, t, tzinfo=KST)


class NightTape:
    """체결·호가 수신 기록과 1분봉. 호가는 개수와 마지막 값만 둔다."""

    def __init__(self, day, code):
        self.day, self.code = day, code
        self.trades = self.quotes = self.trade_records = 0
        self.first = self.last = None
        self.latest = None
        self.latest_quote = None
        self.subscribes = []
        self.notes = []
        self.samples = []
        self.problems = []
        self.bars = []
        self._cum = None  # 직전 봉 마감 때 누적 (거래량, 매수, 매도)

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def add(self, msg, received):
        if msg['tr_id'] == TR_QUOTE:
            self.quotes += 1
            if msg['records']:
                self.latest_quote = {k: msg['records'][-1].get(k) for k in
                                     ('bsop_hour', 'futs_askp1', 'futs_bidp1', 'total_askp_rsqn', 'total_bidp_rsqn')}
            return None
        if msg['tr_id'] != TR_TRADE:
            return None
        self.trades += 1
        if msg.get('width') != msg.get('expected'):
            self.note('체결 메시지 필드 수 %s개(공식 예제 %s개) — 앞쪽 %s개만 씀' % (msg.get('width'), msg.get('expected'), msg.get('expected')))
        values = None
        for rec in msg['records']:
            values = trade_values(rec)
            self.trade_records += 1
            if len(self.samples) < 3:
                self.samples.append({'received': received.isoformat(timespec='seconds'), 'values': values})
            bad = sane_trade(values)
            if bad and len(self.problems) < 5:
                self.problems.append('%s %s' % (values.get('time'), ', '.join(bad)))
            if bad:
                continue
            at = trade_stamp(self.day, values['time'])
            self.first = self.first or at
            self.last = at
            self.latest = values
            self._bar(at, values)
        return values

    def _bar(self, at, v):
        minute = at.replace(second=0, microsecond=0)
        cum = (v.get('volume'), v.get('buy_vol'), v.get('sell_vol'))
        if self.bars and self.bars[-1]['t'] == minute:
            b = self.bars[-1]
            b['high'], b['low'], b['close'] = max(b['high'], v['fut_price']), min(b['low'], v['fut_price']), v['fut_price']
        else:
            if self.bars:
                self._cum = self.bars[-1]['cum']
            elif cum[0] is not None and v.get('last_qty') is not None:
                self._cum = (cum[0] - v['last_qty'], None, None)
            b = {'t': minute, 'open': v['fut_price'], 'high': v['fut_price'], 'low': v['fut_price'], 'close': v['fut_price']}
            self.bars.append(b)
        b['cum'] = cum
        b['oi'] = v.get('oi')
        b['strength'] = v.get('strength')
        prev = self._cum or (None, None, None)
        b['volume'] = cum[0] - prev[0] if None not in (cum[0], prev[0]) else None
        b['buy_vol'] = cum[1] - prev[1] if None not in (cum[1], prev[1]) else None
        b['sell_vol'] = cum[2] - prev[2] if None not in (cum[2], prev[2]) else None

    def closed_bars(self):
        return self.bars[:-1]

    def summary(self):
        v = self.latest or {}
        out = {'session_day': self.day.isoformat(), 'code': self.code, 'trades': self.trades, 'quotes': self.quotes,
               'first_trade': self.first.isoformat() if self.first else None,
               'last_trade': self.last.isoformat() if self.last else None, 'values': v}
        if self.bars:
            out['session'] = {'open': self.bars[0]['open'], 'high': max(b['high'] for b in self.bars),
                              'low': min(b['low'] for b in self.bars), 'close': self.bars[-1]['close'],
                              'bars': len(self.bars), 'note': '이 프로그램이 받은 체결만으로 만든 값(켜기 전 구간 제외)'}
        return out


BAR_HEADER = 'time,open,high,low,close,volume,buy_vol,sell_vol,oi,strength\n'


def bar_line(b):
    def f(x):
        return '' if x is None else ('%g' % x)
    return ','.join([b['t'].strftime('%Y-%m-%d %H:%M')] + [f(b.get(k)) for k in
                    ('open', 'high', 'low', 'close', 'volume', 'buy_vol', 'sell_vol', 'oi', 'strength')]) + '\n'


def day_close_check(directory, day, code, base_price):
    """LS 수집기가 저장한 주간 선물 마지막 조회(snapshot_futures.json)와 한투 '전일 대비' 기준가를 대조."""
    try:
        snap = json.loads((Path(directory) / 'snapshot_futures.json').read_text(encoding='utf-8'))['snapshot']
    except (OSError, ValueError, KeyError):
        return '주간 종가 대조: LS 저장본 없음'
    same = code in (snap.get('code'), ls_code_to_kis(snap.get('code')))
    received = str(snap.get('received_at', ''))
    price = (snap.get('values') or {}).get('fut_price')
    if not same:
        return '주간 종가 대조: LS 저장본 종목(%s)이 다름' % snap.get('code')
    if received[:10] != day.isoformat() or received[11:16] < '15:46':
        return '주간 종가 대조: LS 저장본이 %s 마감 뒤 값이 아님(%s)' % (day.isoformat(), received[:16])
    if base_price is None or price is None:
        return '주간 종가 대조: 비교할 값 없음'
    diff = base_price - price
    verdict = '일치' if abs(diff) < 0.005 else '차이 %+.2f' % diff
    return '주간 종가 대조: 한투 기준가(현재가−전일대비) %.2f vs LS 주간 마지막 %.2f(%s 조회) → %s' % (
        base_price, price, received[11:16], verdict)


# ---------------------------------------------------------------- 웹소켓

def import_websocket():
    try:
        import websocket
    except ImportError:
        raise KisError('websocket-client 라이브러리가 없습니다')
    if not hasattr(websocket, 'create_connection'):
        raise KisError("'websocket' 이름의 다른 패키지가 설치돼 있습니다. websocket-client가 필요합니다")
    return websocket


def connect(websocket, url=None, timeout=10):
    url = url or WS
    last = None
    for path in WS_PATHS:
        try:
            return websocket.create_connection(url + path, timeout=timeout), path or '/'
        except Exception as e:
            last = e
    raise KisError('웹소켓 연결 실패: %s %s' % (type(last).__name__, str(last)[:120]))


def stream(websocket, approval, subs, on_message, until, url=None, recv_timeout=2.0, idle=None):
    """구독을 보내고 until()이 참이 될 때까지 메시지를 on_message(msg, received)로 넘긴다.

    on_message가 [(tr_id, code), ...]를 돌려주면 그 구독을 추가로 보낸다(코드 형식 재시도용).
    PINGPONG은 공식 예제처럼 같은 내용을 pong으로 돌려준다. 서버가 닫으면 KisError."""
    ws, path = connect(websocket, url)

    def subscribe(items):
        for tr_id, code in items:
            ws.send(json.dumps(subscribe_message(approval, tr_id, code)))
            time.sleep(0.2)

    try:
        subscribe(subs)
        ws.settimeout(recv_timeout)
        while not until():
            try:
                opcode, data = ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                if idle:
                    idle()
                continue
            except websocket.WebSocketConnectionClosedException:
                raise KisError('서버와 연결이 끊겼습니다')
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                raise KisError('서버가 연결을 닫았습니다')
            if opcode != websocket.ABNF.OPCODE_TEXT:
                continue
            text = data.decode('utf-8', 'replace') if isinstance(data, (bytes, bytearray)) else str(data)
            msg = parse_message(text)
            if msg['kind'] == 'ping':
                ws.pong(text)
            extra = on_message(msg, now_kst())
            if extra:
                subscribe(extra)
        return path
    finally:
        try:
            ws.close()
        except Exception:
            pass
