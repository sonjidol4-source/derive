import json
import tempfile
import threading
import unittest
from pathlib import Path

import kis_api as kis
from common import config, stamp

CFG = config()


def trade_text(time='213005', price='857.45', change='0.45', sign='2', rate='0.05', oi='143900', vol='12345',
               last_qty='3', buy='6200', sell='6145', count=1, extra=0):
    rec = {c: '0' for c in kis.TRADE_COLUMNS}
    rec.update({'futs_shrn_iscd': 'A01612', 'bsop_hour': time, 'futs_prdy_vrss': change, 'prdy_vrss_sign': sign,
                'futs_prdy_ctrt': rate, 'futs_prpr': price, 'futs_oprc': '856.00', 'futs_hgpr': '858.10',
                'futs_lwpr': '855.90', 'last_cnqn': last_qty, 'acml_vol': vol, 'hts_otst_stpl_qty': oi,
                'otst_stpl_qty_icdc': '193', 'cttr': '103.2', 'mrkt_basis': '-1.02', 'thpr_basis': '3.20',
                'shnu_cntg_smtn': buy, 'seln_cntg_smtn': sell, 'futs_askp1': '857.50', 'futs_bidp1': '857.45'})
    one = '^'.join([rec[c] for c in kis.TRADE_COLUMNS] + ['x'] * extra)
    return '0|H0MFCNT0|%03d|%s' % (count, '^'.join([one] * count))


MASTER = [
    '1|A01612|KR4A01612005|F 202612|0|0|1|K2I|KOSPI200',
    '1|A01703|KR4A01703009|F 202703|0|0|2|K2I|KOSPI200',
    '1|A01706|KR4A01706002|F 202706|0|0|3|K2I|KOSPI200',
    '1|A05611|KR4A05611006|MF 202611|0|0|1|K2I|미니KOSPI200',
    '4|A01S12|KR4A01S12000|SP 2612-2703|0|0|1|K2I|KOSPI200',
    '2|B01611|KR4B01611W80|C 202611 850.0|1|850.00|1|K2I|KOSPI200',
    '1|A06612|KR4A06612000|F 202612|0|0|1|Q150|코스닥150',
]


class MessageTests(unittest.TestCase):
    def test_trade_fields_and_signed_change(self):
        msg = kis.parse_message(trade_text(change='1.20', sign='5', rate='0.14'))
        self.assertEqual((msg['kind'], msg['tr_id'], msg['count'], msg['width']), ('data', 'H0MFCNT0', 1, 49))
        v = kis.trade_values(msg['records'][0])
        self.assertEqual((v['code'], v['time'], v['fut_price'], v['oi'], v['volume']), ('A01612', '213005', 857.45, 143900, 12345))
        self.assertEqual((v['change'], v['change_pct'], v['base_price']), (-1.2, -0.14, 858.65))
        self.assertEqual((v['buy_vol'], v['sell_vol'], v['basis'], v['basis_theory']), (6200, 6145, -1.02, 3.2))
        self.assertEqual(kis.sane_trade(v), [])

    def test_already_signed_change_is_kept(self):
        v = kis.trade_values(kis.parse_message(trade_text(change='-1.20', sign='5'))['records'][0])
        self.assertEqual(v['change'], -1.2)

    def test_multiple_records_and_extra_fields(self):
        msg = kis.parse_message(trade_text(count=2))
        self.assertEqual(len(msg['records']), 2)
        self.assertEqual(msg['records'][1]['futs_prpr'], '857.45')
        wide = kis.parse_message(trade_text(extra=1))
        self.assertEqual((wide['width'], wide['expected']), (50, 49))
        self.assertEqual(wide['records'][0]['futs_prpr'], '857.45')

    def test_shifted_columns_fail_format_check(self):
        rec = dict(zip(kis.TRADE_COLUMNS, trade_text().split('|', 3)[3].split('^')[1:]))
        self.assertTrue(kis.sane_trade(kis.trade_values(rec)))

    def test_system_messages_drop_encryption_keys(self):
        text = json.dumps({'header': {'tr_id': 'H0MFCNT0', 'tr_key': 'A01612', 'encrypt': 'N'},
                           'body': {'rt_cd': '0', 'msg_cd': 'OPSP0000', 'msg1': 'SUBSCRIBE SUCCESS',
                                    'output': {'iv': 'secret-iv', 'key': 'secret-key'}}})
        msg = kis.parse_message(text)
        self.assertEqual((msg['kind'], msg['ok'], msg['msg'], msg['tr_key']), ('system', True, 'SUBSCRIBE SUCCESS', 'A01612'))
        self.assertNotIn('secret', json.dumps(msg))
        self.assertEqual(kis.parse_message('{"header":{"tr_id":"PINGPONG","datetime":"20261022213000"}}')['kind'], 'ping')
        self.assertEqual(kis.parse_message('1|H0MFCNI0|001|abc')['kind'], 'encrypted')
        self.assertEqual(kis.parse_message('not json')['kind'], 'unknown')

    def test_subscribe_message_shape(self):
        m = kis.subscribe_message('APPROVAL', 'H0MFCNT0', 'A01612')
        self.assertEqual(m['header']['tr_type'], '1')
        self.assertEqual(m['body']['input'], {'tr_id': 'H0MFCNT0', 'tr_key': 'A01612'})


class ContractTests(unittest.TestCase):
    def test_master_keeps_only_kospi200_monthly_futures(self):
        rows = kis.kospi200_futures(MASTER)
        self.assertEqual([r['code'] for r in rows], ['A01612', 'A01703', 'A01706'])

    def test_session_day(self):
        self.assertEqual(str(kis.session_day(stamp('2026-10-22T21:00:00+09:00'))), '2026-10-22')
        self.assertEqual(str(kis.session_day(stamp('2026-10-23T03:00:00+09:00'))), '2026-10-22')
        self.assertEqual(str(kis.session_day(stamp('2026-10-23T10:00:00+09:00'))), '2026-10-23')

    def test_expiry_night_rolls_to_next_contract(self):
        rows = kis.kospi200_futures(MASTER)
        cases = {'2026-10-22T21:00:00+09:00': 'A01612', '2026-12-09T22:00:00+09:00': 'A01612',
                 '2026-12-10T19:00:00+09:00': 'A01703', '2026-12-11T03:00:00+09:00': 'A01703'}
        for at, code in cases.items():
            self.assertEqual(kis.pick_contract(rows, stamp(at), CFG)['code'], code, at)
            self.assertEqual(kis.rule_contract(stamp(at), CFG)['code'], code, at)

    def test_ls_code_conversion(self):
        self.assertEqual(kis.ls_code_to_kis('A016C000'), 'A01612')
        self.assertEqual(kis.ls_code_to_kis('A0173000'), 'A01703')
        self.assertIsNone(kis.ls_code_to_kis('101W9000'))
        self.assertEqual(kis.kis_code_to_ls('A01612'), 'A016C000')
        self.assertEqual(kis.kis_code_to_ls('A01703'), 'A0173000')
        self.assertIsNone(kis.kis_code_to_ls('A016C000'))

    def test_rejected_six_digit_code_retries_once_in_ls_format(self):
        w = kis.SubscribeWatch('A01612', ('H0MFCNT0', 'H0MFASP0'))
        bad = {'tr_id': 'H0MFCNT0', 'tr_key': 'A01612', 'ok': False, 'msg': 'invalid tr_key', 'msg_cd': 'OPSP9999'}
        line, retry = w.on_system(bad)
        self.assertIn('invalid tr_key', line)
        self.assertEqual(retry, [('H0MFCNT0', 'A016C000'), ('H0MFASP0', 'A016C000')])
        self.assertIsNone(w.on_system({**bad, 'tr_id': 'H0MFASP0'})[1])
        busy = kis.SubscribeWatch('A01612', ('H0MFCNT0',))
        self.assertIsNone(busy.on_system({**bad, 'msg': 'ALREADY IN USE appkey'})[1])

    def test_master_failure_falls_back_to_rule(self):
        class Down:
            def get(self, *a, **k):
                raise OSError('offline')
        with tempfile.TemporaryDirectory() as d:
            c, how = kis.choose_contract(Down(), stamp('2026-10-22T21:00:00+09:00'), CFG, Path(d))
        self.assertEqual(c['code'], 'A01612')
        self.assertIn('규칙 계산', how)


class TapeTests(unittest.TestCase):
    def test_minute_bars_cross_midnight(self):
        tape = kis.NightTape(stamp('2026-10-22T21:00:00+09:00').date(), 'A01612')
        received = stamp('2026-10-22T23:59:30+09:00')
        for t, price, vol, buy, sell in (('235930', '857.00', '1000', '500', '480'),
                                         ('235950', '857.50', '1010', '508', '482'),
                                         ('000010', '857.25', '1013', '509', '484')):
            tape.add(kis.parse_message(trade_text(time=t, price=price, vol=vol, buy=buy, sell=sell, last_qty='2')), received)
        self.assertEqual(len(tape.bars), 2)
        first, second = tape.bars
        self.assertEqual(first['t'].isoformat(), '2026-10-22T23:59:00+09:00')
        self.assertEqual(second['t'].isoformat(), '2026-10-23T00:00:00+09:00')
        self.assertEqual((first['open'], first['high'], first['close'], first['volume']), (857.0, 857.5, 857.5, 12))
        self.assertEqual((second['volume'], second['buy_vol'], second['sell_vol']), (3, 1, 2))
        self.assertEqual(len(tape.closed_bars()), 1)
        s = tape.summary()
        self.assertEqual((s['session']['open'], s['session']['high'], s['trades']), (857.0, 857.5, 3))
        self.assertTrue(kis.bar_line(first).startswith('2026-10-22 23:59,857,857.5,857,857.5,12,'))

    def test_quotes_are_counted_not_barred(self):
        tape = kis.NightTape(stamp('2026-10-22T21:00:00+09:00').date(), 'A01612')
        q = '0|H0MFASP0|001|' + '^'.join(['A01612', '213000'] + ['1'] * (len(kis.QUOTE_COLUMNS) - 2))
        tape.add(kis.parse_message(q), stamp('2026-10-22T21:30:00+09:00'))
        self.assertEqual((tape.quotes, tape.trades, tape.bars), (1, 0, []))


class CredentialTests(unittest.TestCase):
    def test_save_load_and_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'kis_openapi.json'
            kis.save_credentials(p, ' "PSabc"\n', 'sec ret\r\n')
            self.assertEqual(kis.load_credentials(p), ('PSabc', 'secret'))
            p.write_text('{"app_key": ""}', encoding='utf-8')
            with self.assertRaises(kis.MissingKeys):
                kis.load_credentials(p)
        with self.assertRaises(kis.MissingKeys):
            kis.load_credentials(Path(d) / 'none.json')

    def test_credentials_next_to_ls_file(self):
        p = kis.credentials_path({'ls': {'credentials_file': r'C:\keys\ls_openapi.json'}})
        self.assertEqual(p.name, 'kis_openapi.json')
        self.assertEqual(kis.credentials_path({'kis': {'credentials_file': 'x.json'}}).name, 'x.json')

    def test_approval_errors_hide_keys(self):
        class Resp:
            def __init__(self, code, body):
                self.status_code, self._body = code, body

            def json(self):
                return self._body

        class Http:
            def __init__(self, resp):
                self.resp, self.sent = resp, None

            def post(self, url, **kw):
                self.sent = (url, kw)
                return self.resp

        ok = Http(Resp(200, {'approval_key': 'AK-1'}))
        self.assertEqual(kis.approval_key(ok, 'KEY123', 'SECRET456'), 'AK-1')
        self.assertTrue(ok.sent[0].endswith('/oauth2/Approval'))
        self.assertEqual(ok.sent[1]['json']['secretkey'], 'SECRET456')
        bad = Http(Resp(403, {'error_code': 'EGW00103', 'error_description': '유효하지 않은 AppKey입니다.'}))
        with self.assertRaises(kis.KisError) as cm:
            kis.approval_key(bad, 'KEY123', 'SECRET456')
        self.assertIn('EGW00103', str(cm.exception))
        self.assertNotIn('SECRET456', str(cm.exception))
        self.assertNotIn('KEY123', str(cm.exception))


class DayCloseTests(unittest.TestCase):
    def test_matches_ls_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            snap = {'snapshot': {'code': 'A016C000', 'received_at': '2026-10-22T16:23:20+09:00', 'values': {'fut_price': 857.0}}}
            (Path(d) / 'snapshot_futures.json').write_text(json.dumps(snap), encoding='utf-8')
            day = stamp('2026-10-22T21:00:00+09:00').date()
            self.assertIn('일치', kis.day_close_check(d, day, 'A01612', 857.0))
            self.assertIn('차이 +0.45', kis.day_close_check(d, day, 'A01612', 857.45))
            self.assertIn('종목', kis.day_close_check(d, day, 'A01703', 857.0))


class FakeSocketModule:
    """websocket-client와 같은 이름의 속성만 흉내 낸 가짜(네트워크 없음)."""

    class WebSocketTimeoutException(Exception):
        pass

    class WebSocketConnectionClosedException(Exception):
        pass

    class ABNF:
        OPCODE_TEXT, OPCODE_CLOSE, OPCODE_PING = 1, 8, 9

    def __init__(self, frames, fail_paths=()):
        self.frames, self.fail_paths, self.urls, self.conn = list(frames), fail_paths, [], None

    def create_connection(self, url, timeout=10):
        self.urls.append(url)
        if any(url.endswith(p) for p in self.fail_paths):
            raise OSError('404')
        module = self

        class Conn:
            sent, pongs, closed = [], [], False

            def send(self, text):
                self.sent.append(json.loads(text))

            def settimeout(self, t):
                pass

            def recv_data(self, control_frame=False):
                if not module.frames:
                    raise module.WebSocketTimeoutException()
                item = module.frames.pop(0)
                if item is None:
                    raise module.WebSocketTimeoutException()
                return item

            def pong(self, payload):
                self.pongs.append(payload)

            def close(self):
                self.closed = True

        self.conn = Conn()
        return self.conn


class StreamTests(unittest.TestCase):
    def test_subscribe_pingpong_and_data(self):
        ping = '{"header":{"tr_id":"PINGPONG","datetime":"20261022213000"}}'
        ok = json.dumps({'header': {'tr_id': 'H0MFCNT0', 'tr_key': 'A01612'}, 'body': {'rt_cd': '0', 'msg_cd': 'OPSP0000', 'msg1': 'SUBSCRIBE SUCCESS'}})
        mod = FakeSocketModule([(1, ok.encode()), None, (1, ping.encode()), (9, b''), (1, trade_text().encode())], fail_paths=('/tryitout',))
        got = []
        path = kis.stream(mod, 'APPROVAL', [('H0MFCNT0', 'A01612'), ('H0MFASP0', 'A01612')],
                          lambda m, r: got.append(m), lambda: len(got) >= 3)
        self.assertEqual(path, '/')
        self.assertEqual(mod.urls, [kis.WS + '/tryitout', kis.WS])
        self.assertEqual([s['body']['input']['tr_id'] for s in mod.conn.sent], ['H0MFCNT0', 'H0MFASP0'])
        self.assertEqual(mod.conn.pongs, [ping])
        self.assertEqual([m['kind'] for m in got], ['system', 'ping', 'data'])
        self.assertTrue(mod.conn.closed)

    def test_extra_subscriptions_from_handler_are_sent(self):
        bad = json.dumps({'header': {'tr_id': 'H0MFCNT0', 'tr_key': 'A01612'}, 'body': {'rt_cd': '1', 'msg_cd': 'X', 'msg1': 'invalid tr_key'}})
        mod = FakeSocketModule([(1, bad.encode())])
        watch = kis.SubscribeWatch('A01612', ('H0MFCNT0',))
        seen = []

        def on_message(m, r):
            seen.append(m)
            return watch.on_system(m)[1] if m['kind'] == 'system' else None

        kis.stream(mod, 'A', [('H0MFCNT0', 'A01612')], on_message, lambda: len(seen) >= 1 and not mod.frames)
        self.assertEqual([s['body']['input']['tr_key'] for s in mod.conn.sent], ['A01612', 'A016C000'])

    def test_server_close_raises(self):
        mod = FakeSocketModule([(8, b'')])
        with self.assertRaises(kis.KisError):
            kis.stream(mod, 'A', [('H0MFCNT0', 'A01612')], lambda m, r: None, lambda: False)


try:
    import websocket as _wsclient
    import websockets as _wsserver
    _real_libs = hasattr(_wsclient, 'create_connection') and hasattr(_wsserver, 'serve')
except ImportError:
    _real_libs = False


@unittest.skipUnless(_real_libs, 'websocket-client·websockets 둘 다 있을 때만 실제 소켓으로 확인')
class LocalServerTests(unittest.TestCase):
    """실제 웹소켓 라이브러리끼리: 경로 대체, 구독 응답, 서버 ping, 다건 체결, 호가."""

    def test_round_trip_with_local_server(self):
        import asyncio
        from http import HTTPStatus
        received_subs = []
        ready = threading.Event()
        state = {}

        async def handler(ws):
            for _ in range(2):
                received_subs.append(json.loads(await ws.recv()))
            for sub in received_subs:
                inp = sub['body']['input']
                await ws.send(json.dumps({'header': {'tr_id': inp['tr_id'], 'tr_key': inp['tr_key'], 'encrypt': 'N'},
                                          'body': {'rt_cd': '0', 'msg_cd': 'OPSP0000', 'msg1': 'SUBSCRIBE SUCCESS',
                                                   'output': {'iv': 'i', 'key': 'k'}}}))
            await ws.ping(b'hb')
            await ws.send('{"header":{"tr_id":"PINGPONG","datetime":"20261022213000"}}')
            await ws.send(trade_text(count=2))
            await ws.send('0|H0MFASP0|001|' + '^'.join(['A01612', '213000'] + ['1'] * (len(kis.QUOTE_COLUMNS) - 2)))
            await asyncio.sleep(1)

        def process_request(connection, request):
            if request.path == '/tryitout':
                return connection.respond(HTTPStatus.NOT_FOUND, 'no\n')
            return None

        def run():
            async def main():
                async with _wsserver.serve(handler, '127.0.0.1', 0, process_request=process_request) as server:
                    state['port'] = server.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.sleep(5)
            asyncio.run(main())

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(ready.wait(5))
        tape = kis.NightTape(stamp('2026-10-22T21:00:00+09:00').date(), 'A01612')
        kinds = []

        def on_message(msg, received):
            kinds.append(msg['kind'])
            if msg['kind'] == 'data':
                tape.add(msg, received)

        path = kis.stream(_wsclient, 'APPROVAL', [('H0MFCNT0', 'A01612'), ('H0MFASP0', 'A01612')], on_message,
                          lambda: tape.quotes >= 1, url='ws://127.0.0.1:%d' % state['port'], recv_timeout=0.5)
        self.assertEqual(path, '/')
        self.assertEqual([s['header']['approval_key'] for s in received_subs], ['APPROVAL', 'APPROVAL'])
        self.assertEqual(kinds, ['system', 'system', 'ping', 'data', 'data'])
        self.assertEqual((tape.trades, tape.trade_records, tape.quotes, tape.latest['fut_price']), (1, 2, 1, 857.45))


if __name__ == '__main__':
    unittest.main()
