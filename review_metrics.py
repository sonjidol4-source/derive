"""Read-only comparisons and descriptive observation statistics; no broker requests."""
import collections
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

INVESTORS = [('foreign', '17', '외국인'), ('inst', '18', '기관계'),
             ('individual', '08', '개인'), ('financial', '01', '금융투자')]
HORIZONS = (5, 15, 30, 60)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
        return result.astimezone(dt.timezone(dt.timedelta(hours=9)))
    except (ValueError, TypeError):
        return None


def compare_close(directory, day=None):
    """Compare general futures only. Difference means KIS minus LS, never truth."""
    root = Path(directory)
    try:
        days = json.loads((root / 'close_summary.json').read_text(encoding='utf-8')).get('days', {})
    except (OSError, ValueError):
        days = {}
    day = day or max(days, default=None)
    result = dict(day=day, status='unavailable', scope='일반선물 · 당일 순매매 · 계약 (미니 제외)',
                  note='', ls_at=None, kis_at=None, ls_data_at=None, rows=[])
    ls = days.get(day, {})
    ls_at = timestamp(ls.get('asked_at'))
    ls_data = timestamp(str(day) + 'T' + str(ls.get('latest_time', '')))
    result.update(ls_at=ls.get('asked_at'), ls_data_at=ls_data.isoformat() if ls_data else None)
    if (not day or ls.get('day') != day or ls.get('status') != '확정 추정'
            or not ls_data or ls_data.strftime('%H:%M') < '16:00'
            or not ls_at or ls_at < ls_data or ls.get('source') != 'LS t1602'):
        result['note'] = '해당 거래일의 LS 마감 일반선물 자료와 늦은 시각의 관측을 확인할 수 없습니다.'
        return result
    candidates = []
    try:
        with (root / 'kis_flows' / (day + '.jsonl')).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    at = timestamp(record.get('at'))
                    start = timestamp(record.get('started'))
                    if (record.get('day') == day and at and at.date().isoformat() == day
                            and at.strftime('%H:%M') >= '16:15' and start
                            and start.date().isoformat() == day and 0 <= (at-start).total_seconds() <= 30
                            and isinstance(record.get('markets', {}).get('fut'), dict)):
                        candidates.append((at, record))
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    if not candidates:
        result['note'] = '같은 거래일 16:15 이후의 한투 일반선물 자료가 없습니다. 날짜·수집 시각을 확인할 수 없는 자료는 대조하지 않습니다.'
        return result
    _, kis = max(candidates, key=lambda pair: pair[0])
    result['kis_at'] = kis['at']
    for investor, code, label in INVESTORS:
        a, b = ls.get('qty', {}).get(code), kis['markets']['fut'].get(investor, {}).get('qty')
        valid = finite(a) and finite(b)
        result['rows'].append(dict(investor=investor, label=label, ls_qty=a if finite(a) else None,
            kis_qty=b if finite(b) else None, difference=b-a if valid else None,
            status=('matched' if a == b else 'different') if valid else 'missing'))
    states = {r['status'] for r in result['rows']}
    result['status'] = 'partial' if 'missing' in states else 'different' if 'different' in states else 'matched'
    result['note'] = '차이 = 한투 − LS. 어느 쪽도 정답으로 지정하지 않습니다. LS 거래일은 저장된 달력 기준이며 조회 시각과 자료 시각을 함께 확인하세요.'
    return result


def provenance(measurements, mode='live'):
    if mode == 'demo':
        return 'demo'
    kinds = set()
    for m in measurements:
        explicit = m.get('provenance')
        if explicit in ('observed', 'recovered', 'manual', 'demo', 'unknown'):
            kinds.add(explicit)
            continue
        text = str(m.get('source', '')) + ' ' + str(m.get('quality', ''))
        if any(word in text for word in ('사후', '복구', 'recovered', 'backfill')):
            kinds.add('recovered')
        elif any(word in text for word in ('사용자 입력', '수동', 'manual')):
            kinds.add('manual')
        elif (m.get('source') and str(m['source']).lower() not in ('unknown', '미상')
              and m.get('at') and m.get('quality')):
            kinds.add('observed')
        else:
            kinds.add('unknown')
    return next(iter(kinds)) if len(kinds) == 1 else 'mixed' if kinds else 'unknown'


def event_metadata(cfg, mode, values, meta=None):
    """Snapshot only non-secret rule parameters and actual input measurements."""
    meta = meta or {}
    rule = {'version': cfg.get('version'), 'rules': cfg.get('rules', {}),
            'session': cfg.get('session', {}), 'external_rule': meta.get('rule_context', {})}
    # An adaptive threshold is an observation-time input, not a new algorithm.
    # Keep it for audit while grouping observations by the stable rule definition.
    rule_hash = hashlib.sha256(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    frozen = json.loads(json.dumps({**rule, 'external_thresholds': meta.get('thresholds', {}),
                                   'external_basis': meta.get('basis', {})}, ensure_ascii=False, sort_keys=True))
    observations = [{k: m.get(k) for k in ('value', 'at', 'source', 'quality', 'scope', 'provenance')}
                    | {'field': key} for key, m in values.items() if isinstance(m, dict)]
    observations.extend(json.loads(json.dumps(meta.get('input_observations', []), ensure_ascii=False)))
    # External observations without declared inputs cannot be certified as observed.
    if meta.get('source') and not meta.get('input_observations'):
        observations.append({'field': 'external', 'source': meta['source'],
                             'at': meta.get('data_at'), 'quality': None, 'provenance': 'unknown'})
    return {'metadata_version': 1, 'rule_hash': rule_hash, 'rule_snapshot': frozen,
            'data_provenance': provenance(observations, mode), 'input_observations': observations,
            'provenance_note': '출처 분류는 기록된 신호 입력과 진입 기준 지수에 대한 것입니다. '
                '적응형 임계값 산출에 쓰인 과거 표본 전체의 출처를 인증하지 않습니다. '
                '당시 임계값과 산출 근거는 rule_snapshot에서 별도로 확인합니다.'}


def _stats(events, mins):
    valid, missing = [], collections.Counter()
    for event in events:
        outcome = event.get('outcomes', {}).get(str(mins), {})
        at = timestamp(event.get('at'))
        if outcome.get('status') != '관측':
            missing[outcome.get('status', '미평가')] += 1
        elif not finite(outcome.get('return_pct')) or event.get('side') not in (-1, 1) or not at:
            missing['평가값/방향/시각 누락'] += 1
        else:
            valid.append((at, event['side'] * outcome['return_pct']))
    valid.sort(key=lambda pair: pair[0])
    previous_end, overlaps = {}, 0
    for at, _ in valid:
        day = at.date().isoformat()
        if previous_end.get(day) and at < previous_end[day]:
            overlaps += 1
        previous_end[day] = max(previous_end.get(day, at), at+dt.timedelta(minutes=mins))
    return {'valid': len(valid), 'missing': sum(missing.values()), 'missing_reasons': dict(missing),
            'days': len({at.date() for at, _ in valid}), 'overlap_count': overlaps,
            'mean_directional_pct': sum(v for _, v in valid)/len(valid) if valid else None,
            'positive_pct': sum(v > 0 for _, v in valid)/len(valid)*100 if valid else None}


def aggregate_events(events, total_count=None):
    events = list(events)
    buckets = collections.defaultdict(list)
    for event in events:
        key = (event.get('kind', 'unknown'), event.get('investor', 'unspecified'),
               event.get('rule_version', 'unknown'), event.get('rule_hash', 'unknown'),
               event.get('data_provenance', 'unknown'), event.get('mode', 'unknown'))
        buckets[key].append(event)
    groups = []
    for key, subset in sorted(buckets.items(), key=lambda pair: str(pair[0])):
        group = dict(zip(('kind', 'investor', 'rule_version', 'rule_hash', 'provenance', 'mode'), key))
        group.update(count=len(subset), days=len({e.get('at', '')[:10] for e in subset}), horizons={})
        for mins in HORIZONS:
            stats = _stats(subset, mins)
            strata = collections.defaultdict(list)
            for event in subset:
                outcome = event.get('outcomes', {}).get(str(mins), {})
                strata[outcome.get('data_provenance', 'unknown')].append(event)
            stats['by_provenance'] = {label: _stats(rows, mins) for label, rows in sorted(strata.items())}
            group['horizons'][str(mins)] = stats
        groups.append(group)
    total = max(len(events), total_count) if total_count is not None else len(events)
    return {'loaded': len(events), 'total': total, 'truncated': total > len(events),
            'note': '로드된 %d / 전체 %d건. %s 방향수익률은 관측 방향×지수 등락률이며 거래비용을 반영한 수익률이 아닙니다. 같은 날 평가 구간이 앞선 표본과 겹치는 건수를 따로 표시하며 독립 표본으로 보지 않습니다. 과거 출처 미기록은 unknown으로 분리합니다.' % (len(events), total, '최신 일부만 집계합니다.' if total > len(events) else '전체 저장 관측을 집계합니다.'),
            'audit': {'missing_rule_hash': sum(not e.get('rule_hash') for e in events),
                      'missing_provenance': sum(not e.get('data_provenance') for e in events),
                      'missing_investor': sum(not e.get('investor') for e in events),
                      'missing_outcome_provenance': sum(o.get('status') == '관측' and not o.get('data_provenance') for e in events for o in e.get('outcomes', {}).values())},
            'groups': groups}
