"""키움 REST API read-only adapter. No order endpoint is present."""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
import math
from typing import Any


class KiwoomAPIError(RuntimeError):
    pass


def kiwoom_number(value: Any) -> float:
    """Normalizes signed price/flow strings. Missing/broken is an ERROR, not zero."""
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('non-finite numeric field')
        return result
    if value is None or not str(value).strip():
        raise ValueError('missing numeric field')
    raw = str(value).strip().replace(',', '').replace(' ', '').replace('%', '')
    if raw.startswith('--'):  # some Kiwoom responses use --123 for negative
        raw = '-' + raw.lstrip('-')
    result = float(raw)
    if not math.isfinite(result):
        raise ValueError('non-finite numeric field')
    return result


class KiwoomClient:
    BASE_URLS = {"live": "https://api.kiwoom.com", "mock": "https://mockapi.kiwoom.com"}
    READ_PATHS = {'ka10001': '/api/dostk/stkinfo', 'ka10059': '/api/dostk/stkinfo',
                  'ka10080': '/api/dostk/chart', 'ka10081': '/api/dostk/chart',
                  'ka10131': '/api/dostk/frgnistt'}

    def __init__(self, appkey: str, secretkey: str, env: str = 'live', min_interval: float = 0.5,
                 response_hook=None):
        if env not in self.BASE_URLS:
            raise ValueError('env must be live or mock')
        if not appkey or not secretkey:
            raise ValueError('KIWOOM_APPKEY and KIWOOM_SECRETKEY are required')
        self.appkey, self.secretkey, self.base = appkey, secretkey, self.BASE_URLS[env]
        self.env, self.response_hook = env, response_hook
        self._token: str | None = None
        self._last_call = 0.0
        self._delay = max(min_interval, 0.25)  # local pacing, NOT a claimed API allowance

    @classmethod
    def from_env(cls) -> 'KiwoomClient':
        return cls(os.getenv('KIWOOM_APPKEY', ''), os.getenv('KIWOOM_SECRETKEY', ''), os.getenv('KIWOOM_ENV', 'live'))

    def _wait(self) -> None:
        remaining = self._delay - (time.monotonic() - self._last_call)
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()

    def _send(self, path: str, api_id: str, body: dict, token: bool = True, continuation: dict | None = None) -> tuple[dict, dict]:
        import requests
        if token:
            if self.READ_PATHS.get(api_id) != path:
                raise KiwoomAPIError('Only allowlisted market-data requests are supported')
        elif (api_id, path) != ('au10001', '/oauth2/token'):
            raise KiwoomAPIError('Unsupported authentication request')
        headers = {'Content-Type': 'application/json;charset=UTF-8', 'api-id': api_id}
        if token:
            if self._token is None:
                self.authenticate()
            headers['authorization'] = f'Bearer {self._token}'
        if continuation:
            headers.update(continuation)
        for attempt in range(3):
            self._wait()
            try:
                r = requests.post(self.base + path, headers=headers, json=body, timeout=12)
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise KiwoomAPIError(f'{api_id}: connection failed') from exc
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            try:
                r.raise_for_status()
                data = r.json()
            except (ValueError, requests.RequestException) as exc:
                raise KiwoomAPIError(f'{api_id}: HTTP {r.status_code} / invalid response') from exc
            if not isinstance(data, dict) or 'return_code' not in data:
                raise KiwoomAPIError(f'{api_id}: missing response status')
            response_headers = {k.lower(): v for k, v in r.headers.items()}
            if token and self.response_hook:
                # Authentication payloads and request Authorization headers never reach this hook.
                self.response_hook(api_id, path, body, data, response_headers,
                                   datetime.now(timezone.utc).isoformat())
            if str(data['return_code']) != '0':
                raise KiwoomAPIError(f"{api_id}: API error code {data['return_code']}")
            return data, response_headers
        raise KiwoomAPIError(f'{api_id}: server rate limit/server error after retries')

    def authenticate(self) -> None:
        data, _ = self._send('/oauth2/token', 'au10001', {
            'grant_type': 'client_credentials', 'appkey': self.appkey, 'secretkey': self.secretkey
        }, token=False)
        self._token = data.get('token')
        if not self._token:
            raise KiwoomAPIError('au10001: token not returned')

    def fetch_pages(self, api_id: str, path: str, body: dict, list_key: str, max_pages: int = 5) -> list[dict]:
        result: list[dict] = []
        cont: dict | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            data, resp_headers = self._send(path, api_id, body, continuation=cont)
            chunk = data.get(list_key)
            if chunk is None:
                raise KiwoomAPIError(f'{api_id}: expected list key {list_key} missing')
            if not isinstance(chunk, list):
                raise KiwoomAPIError(f'{api_id}: {list_key} must be list')
            result.extend(chunk)
            if resp_headers.get('cont-yn', '').upper() != 'Y':
                return result
            next_key = resp_headers.get('next-key')
            if not next_key or next_key in seen:
                raise KiwoomAPIError(f'{api_id}: invalid continuation key')
            seen.add(next_key)
            cont = {'cont-yn': 'Y', 'next-key': next_key}
        raise KiwoomAPIError(f'{api_id}: more than {max_pages} pages; dataset incomplete')

    def scan_institution_streaks(self, period: int = 5) -> list[dict]:
        """ka10131; KRX only, KOSPI+KOSDAQ, 0=amount (different from ka10059)."""
        if period not in (1, 3, 5, 10, 20, 120):
            raise ValueError('unsupported period')
        rows = []
        for market in ('001', '101'):
            body = {'dt': str(period), 'mrkt_tp': market, 'netslmt_tp': '2',
                    'stk_inds_tp': '0', 'amt_qty_tp': '0', 'stex_tp': '1'}
            rows.extend(self.fetch_pages('ka10131', '/api/dostk/frgnistt', body,
                                         'orgn_frgnr_cont_trde_prst', max_pages=8))
        return rows

    def investor_history(self, code: str, asof: date, max_pages: int = 2) -> list[dict]:
        return self.fetch_pages('ka10059', '/api/dostk/stkinfo', {
            'dt': asof.strftime('%Y%m%d'), 'stk_cd': code,
            'amt_qty_tp': '1', 'trde_tp': '0', 'unit_tp': '1'
        }, 'stk_invsr_orgn', max_pages=max_pages)

    def daily_prices(self, code: str, asof: date, max_pages: int = 1) -> list[dict]:
        return self.fetch_pages('ka10081', '/api/dostk/chart', {
            'stk_cd': code, 'base_dt': asof.strftime('%Y%m%d'), 'upd_stkpc_tp': '1'
        }, 'stk_dt_pole_chart_qry', max_pages=max_pages)

    def stock_info(self, code: str) -> dict:
        data, _ = self._send('/api/dostk/stkinfo', 'ka10001', {'stk_cd': code})
        return data
