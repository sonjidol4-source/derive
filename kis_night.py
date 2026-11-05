"""한투(KIS) KRX 야간선물 실행기.

  setup  : 앱키·앱시크릿을 화면에 보이지 않게 입력받아 키 파일에 저장하고 접속키 발급으로 확인
  check  : 근월물 체결·호가를 몇 분 받아 보고 data/한투_야간확인.txt에 결과를 남김
  record : 야간장이 끝나는 06:05까지 체결을 받아 1분봉(data/kis_night/)과 최신 요약을 저장

조회 전용이다. 키·접속키를 화면·파일·로그에 쓰지 않는다. 기존 레이더(app.py)와 따로 도는 별도 프로세스다.
"""
import argparse
import datetime as dt
import getpass
import importlib
import json
import subprocess
import sys
import time

import kis_api as kis
from common import ROOT, atomic_json, config, now_kst

DATA = ROOT / 'data'
REPORT = DATA / '한투_야간확인.txt'
LATEST = DATA / 'kis_night_latest.json'
BARS_DIR = DATA / 'kis_night'
MASTER_DIR = DATA / 'kis_master'


def night_hours(at):
    hm = at.strftime('%H:%M')
    return hm >= '18:00' or hm < '06:00'


def fmt(v, digits=2, sign=False):
    if v is None:
        return '-'
    if digits == 0:
        return format(round(v), '+,' if sign else ',')
    return ('%+.' + str(digits) + 'f' if sign else '%.' + str(digits) + 'f') % v


def live_line(v):
    return '%s:%s:%s  %s (%s, %s%%)  고 %s 저 %s · 거래 %s · 미결제 %s (%s) · 체결강도 %s' % (
        v['time'][:2], v['time'][2:4], v['time'][4:6], fmt(v.get('fut_price')), fmt(v.get('change'), sign=True),
        fmt(v.get('change_pct'), sign=True), fmt(v.get('fut_high')), fmt(v.get('fut_low')),
        fmt(v.get('volume'), 0), fmt(v.get('oi'), 0), fmt(v.get('oi_change'), 0, True), fmt(v.get('strength'), 1))


# ---------------------------------------------------------------- 준비

def ensure_websocket():
    """websocket-client가 없으면 이 파이썬에 설치한다(사용자에게 알리고 진행)."""
    try:
        return kis.import_websocket()
    except kis.KisError as e:
        if 'websocket-client 라이브러리가 없습니다' not in str(e):
            raise
    print('웹소켓 라이브러리(websocket-client)가 없어 설치합니다. 처음 한 번만 필요합니다.', flush=True)
    r = subprocess.run([sys.executable, '-m', 'pip', 'install', 'websocket-client'])
    if r.returncode:
        raise kis.KisError('websocket-client 설치 실패. 인터넷 연결을 확인하거나 "%s -m pip install websocket-client"를 직접 실행하세요'
                           % sys.executable)
    importlib.invalidate_caches()
    return kis.import_websocket()


def ask_secret(label, expected_len):
    while True:
        value = kis.clean_key(getpass.getpass(label))
        if not value:
            print('  비어 있습니다. 다시 붙여넣으세요.')
            continue
        note = '' if len(value) == expected_len else ' (보통 %d자입니다. 잘렸는지 확인하세요)' % expected_len
        print('  %d자 입력됨%s' % (len(value), note))
        return value


def verify(http, key, secret):
    try:
        kis.approval_key(http, key, secret)
        print('키 확인 완료: 웹소켓 접속키 발급에 성공했습니다.')
        return True
    except kis.KisError as e:
        print('키 확인 실패: %s' % e)
        print('앱키·앱시크릿이 바뀌었거나 잘못 붙여넣었을 수 있습니다. 한투_키등록.bat으로 다시 등록하세요.')
        return False


def setup(cfg, http=None, ask_existing=True):
    """키 등록. 이미 있으면 바꿀지 묻고, 그대로 두면 기존 키로 확인만 한다."""
    if http is None:
        import requests as http
    path = kis.credentials_path(cfg)
    if ask_existing:
        try:
            key, secret = kis.load_credentials(path)
            if input('이미 등록된 한투 키가 있습니다. 바꾸려면 y, 그대로 쓰려면 Enter: ').strip().lower() != 'y':
                verify(http, key, secret)
                return path
        except kis.KisError:
            pass
    print()
    print('한국투자증권 KIS Developers에서 받은 실전 앱키와 앱시크릿을 등록합니다.')
    print('입력한 글자는 화면에 보이지 않습니다. 붙여넣기(마우스 오른쪽 클릭 또는 Ctrl+V) 후 Enter를 누르세요.')
    key = ask_secret('앱키(App Key): ', 36)
    secret = ask_secret('앱시크릿(App Secret): ', 180)
    kis.save_credentials(path, key, secret)
    print('저장했습니다: %s' % path)
    print('이 파일은 비밀번호와 같습니다. 채팅·메일로 보내지 마세요.')
    verify(http, key, secret)
    return path


def prepare(cfg, say, override=None):
    """키 확인 → 라이브러리 → 접속키 → 근월물. 반환: (requests, websocket, approval, contract, how, (key, secret))."""
    import requests
    path = kis.credentials_path(cfg)
    try:
        key, secret = kis.load_credentials(path)
    except kis.MissingKeys:
        say('한투 키가 아직 등록되지 않아 등록부터 합니다.')
        setup(cfg, requests, ask_existing=False)
        key, secret = kis.load_credentials(path)
    say('키 파일: 확인 (%s)' % path)
    websocket = ensure_websocket()
    approval = kis.approval_key(requests, key, secret)
    say('웹소켓 접속키: 발급 성공')
    contract, how = kis.choose_contract(requests, now_kst(), cfg, MASTER_DIR, override)
    say('근월물: %s %s (%s)' % (contract['code'], contract['name'], how))
    return requests, websocket, approval, contract, how, (key, secret)


def latest_payload(tape, how, status):
    return {'source': 'KIS %s (KRX 야간선물 실시간체결)' % kis.TR_TRADE, 'status': status, 'code_method': how,
            'updated_at': now_kst().isoformat(timespec='seconds'), **tape.summary(),
            'note': '야간장 기준일(session_day) 저녁 18:00에 시작한 장. session 값은 이 프로그램이 켜져 있던 구간의 체결만 반영한다.'}


# ---------------------------------------------------------------- check

def check(cfg, minutes, override=None):
    lines = []

    def say(text):
        print(text, flush=True)
        lines.append(text)

    started = now_kst()
    say('한투 KRX 야간선물 수신 확인 · 시작 %s · 최대 %g분' % (started.strftime('%Y-%m-%d %H:%M:%S'), minutes))
    if not night_hours(started):
        say('지금은 야간장(18:00~다음 날 06:00) 시간이 아니어서 체결이 들어오지 않을 수 있습니다.')
    tape = None
    how = ''
    try:
        _, websocket, approval, contract, how, _ = prepare(cfg, say, override)
        tape = kis.NightTape(kis.session_day(started), contract['code'])
        code = contract['code']
        end = time.monotonic() + minutes * 60
        state = {'printed': 0.0, 'status': time.monotonic()}
        watch = kis.SubscribeWatch(code, (kis.TR_TRADE, kis.TR_QUOTE))

        def on_message(msg, received):
            if msg['kind'] == 'system':
                text, retry = watch.on_system(msg)
                tape.subscribes.append(text)
                say(text)
                if retry:
                    tape.code = watch.alt
                    say('6자리 코드가 거절돼 %s 형식으로 한 번 더 구독합니다.' % watch.alt)
                    return retry
            elif msg['kind'] == 'data':
                values = tape.add(msg, received)
                if values and time.monotonic() - state['printed'] >= 1:
                    state['printed'] = time.monotonic()
                    print('  ' + live_line(values), flush=True)
            elif msg['kind'] == 'encrypted':
                tape.note('암호화된 메시지 %s 수신(이 확인에서는 쓰지 않음)' % msg['tr_id'])
            elif msg['kind'] == 'unknown':
                tape.note('형식을 모르는 메시지: %s' % msg['text'][:60])

        def idle():
            if time.monotonic() - state['status'] >= 15:
                state['status'] = time.monotonic()
                left = max(0, int(end - time.monotonic()))
                print('  … 체결 %d건 · 호가 %d건 수신 (남은 시간 %d초)' % (tape.trades, tape.quotes, left), flush=True)

        say('웹소켓 연결 후 %s(체결)·%s(호가) 구독. 창을 닫지 말고 기다리세요.' % (kis.TR_TRADE, kis.TR_QUOTE))
        path = kis.stream(websocket, approval, [(kis.TR_TRADE, code), (kis.TR_QUOTE, code)], on_message,
                          lambda: time.monotonic() >= end, idle=idle)
        say('웹소켓 경로: %s' % path)
    except kis.KisError as e:
        say('중단: %s' % e)
        if hint(e):
            say(hint(e))
    except KeyboardInterrupt:
        say('사용자가 중단했습니다.')
    except Exception as e:  # 예상 못 한 오류도 보고서는 남긴다(키 값은 오류 문구에 들어가지 않음)
        say('중단(예상 못 한 오류): %s %s' % (type(e).__name__, str(e)[:150]))
    finished = now_kst()
    for line in report(tape, started, finished):
        say(line)
    DATA.mkdir(parents=True, exist_ok=True)
    REPORT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if tape and tape.trades:
        atomic_json(LATEST, latest_payload(tape, how, '확인 실행(%s~%s)' % (started.strftime('%H:%M'), finished.strftime('%H:%M'))))
    print()
    print('결과 저장: %s' % REPORT)
    return tape


def hint(error):
    text = str(error)
    if '접속키 발급 실패' in text:
        return '앱키·앱시크릿이 틀렸거나 바뀌었을 수 있습니다. 한투_키등록.bat으로 다시 등록한 뒤 실행하세요.'
    if '접속하지 못했습니다' in text or '연결 실패' in text:
        return '인터넷 연결과 방화벽(한투 웹소켓 21000번 포트)을 확인하세요.'
    return None


def report(tape, started, finished):
    out = ['', '=== 결과 (%s ~ %s) ===' % (started.strftime('%H:%M:%S'), finished.strftime('%H:%M:%S'))]
    if tape is None:
        out.append('판정: 수신 전 단계에서 멈췄습니다. 위 "중단" 줄을 확인하세요.')
        return out
    ok_sub = any('SUCCESS' in s.upper() for s in tape.subscribes)
    if tape.trades and not tape.problems:
        verdict = '수신 성공 — 체결 %d건(레코드 %d)·호가 %d건' % (tape.trades, tape.trade_records, tape.quotes)
    elif tape.trades:
        verdict = '수신은 됐으나 형식 점검에 걸린 값이 있습니다 — 아래 점검 참고'
    elif ok_sub:
        verdict = '구독은 성공했지만 체결이 들어오지 않았습니다(야간장 시간·거래 여부 확인)'
    else:
        verdict = '구독 응답을 받지 못했거나 실패했습니다'
    out.append('판정: ' + verdict)
    out.append('종목: %s · 야간장 기준일 %s' % (tape.code, tape.day.isoformat()))
    out.append('구독 응답: ' + (' / '.join(tape.subscribes) if tape.subscribes else '없음'))
    if 'ALREADY IN USE' in ' '.join(tape.subscribes).upper():
        out.append('참고: 같은 앱키로 다른 창·프로그램이 한투 웹소켓에 접속 중이면 이렇게 나옵니다. 그 창을 닫고 다시 실행하세요.')
    if tape.first:
        out.append('체결 시각: 처음 %s · 마지막 %s' % (tape.first.strftime('%H:%M:%S'), tape.last.strftime('%H:%M:%S')))
    v = tape.latest
    if v:
        out.append('마지막 체결: %s' % live_line(v))
        out.append('  시가 %s · 시장 베이시스 %s · 이론 베이시스 %s · 이론가 %s' % (
            fmt(v.get('fut_open')), fmt(v.get('basis'), sign=True), fmt(v.get('basis_theory'), sign=True), fmt(v.get('theory'))))
        out.append('  매수 체결수량 %s · 매도 체결수량 %s · 매수1 %s(%s) · 매도1 %s(%s)' % (
            fmt(v.get('buy_vol'), 0), fmt(v.get('sell_vol'), 0), fmt(v.get('bid1')), fmt(v.get('bid_qty1'), 0),
            fmt(v.get('ask1')), fmt(v.get('ask_qty1'), 0)))
        out.append('  ' + kis.day_close_check(DATA, tape.day, tape.code, v.get('base_price')))
    if tape.latest_quote:
        q = tape.latest_quote
        out.append('마지막 호가(%s): 매도1 %s · 매수1 %s · 총잔량 매도 %s / 매수 %s' % (
            q.get('bsop_hour'), q.get('futs_askp1'), q.get('futs_bidp1'), q.get('total_askp_rsqn'), q.get('total_bidp_rsqn')))
    out.append('형식 점검: ' + ('문제 없음' if not tape.problems else ' / '.join(tape.problems)))
    for n in tape.notes:
        out.append('메모: ' + n)
    for s in tape.samples:
        out.append('샘플 %s %s' % (s['received'][11:19], json.dumps(s['values'], ensure_ascii=False)))
    out.append('이 결과는 접속·수신 확인입니다. 야간 수급(투자자별)은 이 TR에 없습니다.')
    return out


# ---------------------------------------------------------------- record

def keep_awake(on):
    """기록하는 동안 Windows가 절전으로 들어가지 않게 요청한다(프로그램이 끝나면 자동 해제)."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def record_end(at):
    end = at.replace(hour=6, minute=5, second=0, microsecond=0)
    return end if at < end else end + dt.timedelta(days=1)


def record(cfg, override=None):
    started = now_kst()
    end_at = record_end(started)

    def say(text):
        print(text, flush=True)

    say('한투 KRX 야간선물 기록 · 시작 %s · %s에 자동 종료 · 창을 닫으면 그 자리에서 멈춥니다'
        % (started.strftime('%m-%d %H:%M'), end_at.strftime('%m-%d %H:%M')))
    if not night_hours(started):
        say('지금은 야간장 시간이 아닙니다. 연결은 해 두지만 체결은 18:00부터 들어옵니다.')
    keep_awake(True)
    tape = None
    how = ''
    written = 0
    bars_path = None
    backoff = 5
    key_secret = None
    requests = websocket = None
    contract = None
    try:
        while now_kst() < end_at:
            try:
                if tape is None:
                    requests, websocket, approval, contract, how, key_secret = prepare(cfg, say, override)
                    tape = kis.NightTape(kis.session_day(started), contract['code'])
                    BARS_DIR.mkdir(parents=True, exist_ok=True)
                    bars_path = BARS_DIR / ('%s_%s_1m.csv' % (tape.day.isoformat(), tape.code))
                    if not bars_path.exists():
                        bars_path.write_text(kis.BAR_HEADER, encoding='utf-8')
                else:
                    approval = kis.approval_key(requests, *key_secret)
                watch = kis.SubscribeWatch(tape.code, (kis.TR_TRADE,))

                def on_message(msg, received):
                    nonlocal written
                    if msg['kind'] == 'system':
                        text, retry = watch.on_system(msg)
                        tape.subscribes.append(text)
                        say(text)
                        if retry:
                            tape.code = watch.alt
                            say('6자리 코드가 거절돼 %s 형식으로 한 번 더 구독합니다.' % watch.alt)
                            return retry
                    elif msg['kind'] == 'data':
                        tape.add(msg, received)
                        closed = tape.closed_bars()
                        if len(closed) > written:
                            with open(bars_path, 'a', encoding='utf-8') as f:
                                for b in closed[written:]:
                                    f.write(kis.bar_line(b))
                            written = len(closed)
                            b = closed[-1]
                            say('%s  종가 %s · 1분 거래 %s · 매수-매도 %s · 미결제 %s · 누적 체결 %d건' % (
                                b['t'].strftime('%H:%M'), fmt(b['close']), fmt(b.get('volume'), 0),
                                fmt((b.get('buy_vol') or 0) - (b.get('sell_vol') or 0), 0, True)
                                if b.get('buy_vol') is not None and b.get('sell_vol') is not None else '-',
                                fmt(b.get('oi'), 0), tape.trades))
                            atomic_json(LATEST, latest_payload(tape, how, '기록 중'))

                kis.stream(websocket, approval, [(kis.TR_TRADE, tape.code)], on_message,
                           lambda: now_kst() >= end_at)
                backoff = 5
            except kis.KisError as e:
                if tape is None:
                    raise  # 시작 단계(키·라이브러리·첫 접속키) 실패는 바로 알린다
                say('%s 연결 문제: %s · %d초 뒤 다시 연결' % (now_kst().strftime('%H:%M:%S'), e, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception as e:  # 네트워크·소켓 오류: 기록 중이면 다시 연결, 시작 전이면 알린다
                if tape is None and not isinstance(e, OSError):
                    raise
                say('%s 네트워크 오류: %s · %d초 뒤 다시 연결' % (now_kst().strftime('%H:%M:%S'), type(e).__name__, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
    except KeyboardInterrupt:
        say('사용자가 중단했습니다.')
    except kis.KisError as e:
        say('중단: %s' % e)
        if hint(e):
            say(hint(e))
    finally:
        keep_awake(False)
        if tape and tape.bars and bars_path:
            with open(bars_path, 'a', encoding='utf-8') as f:
                for b in tape.bars[written:]:
                    f.write(kis.bar_line(b))
        if tape and tape.trades:
            atomic_json(LATEST, latest_payload(tape, how, '기록 종료 %s' % now_kst().strftime('%m-%d %H:%M')))
            say('저장: %s · %s' % (bars_path, LATEST))
    return tape


def main(argv=None):
    p = argparse.ArgumentParser(description='한투 KRX 야간선물 확인·기록(조회 전용)')
    p.add_argument('command', choices=['setup', 'check', 'record'])
    p.add_argument('--minutes', type=float, default=3, help='check 실행 시간(분)')
    p.add_argument('--code', help='종목코드를 직접 지정(예: A01612). 보통은 비워 둔다')
    args = p.parse_args(argv)
    cfg = config()
    try:
        if args.command == 'setup':
            setup(cfg)
        elif args.command == 'check':
            check(cfg, args.minutes, args.code)
        else:
            record(cfg, args.code)
    except kis.KisError as e:
        print('중단: %s' % e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
