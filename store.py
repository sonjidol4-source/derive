import csv
import json
import sqlite3
from pathlib import Path

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # 다른 프로그램(미리보기·점검 스크립트)이 잠깐 잠가도 바로 실패하지 않게 30초까지 기다린다(기본 5초).
        self.db = sqlite3.connect(str(self.directory / 'radar.sqlite3'), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS ticks(at TEXT PRIMARY KEY, day TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at TEXT, kind TEXT, side INTEGER, price REAL, payload TEXT, outcomes TEXT);
        CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS raw(at TEXT, source TEXT, payload TEXT);
        ''')

    def tick(self, at, payload):
        self.db.execute('INSERT OR REPLACE INTO ticks VALUES(?,?,?)', (at, at[:10], json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def ticks(self, day):
        rows = self.db.execute('SELECT at,payload FROM ticks WHERE day=? ORDER BY at', (day,))
        return [{'at':r['at'], **json.loads(r['payload'])} for r in rows]

    def event(self, event):
        self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)',
            (event['id'], event['at'], event['kind'], event['side'], event['price'], json.dumps(event, ensure_ascii=False), '{}'))
        self.db.commit()

    def events(self, limit=300):
        rows = self.db.execute('SELECT payload,outcomes FROM events ORDER BY at DESC LIMIT ?', (limit,))
        return [{**json.loads(r['payload']), 'outcomes':json.loads(r['outcomes'])} for r in rows]

    def outcomes(self, event_id, values):
        self.db.execute('UPDATE events SET outcomes=? WHERE id=?', (json.dumps(values), event_id))
        self.db.commit()

    def event_count(self):
        return self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    def save_daily(self, day, values):
        self.db.execute('INSERT OR REPLACE INTO daily VALUES(?,?)', (day, json.dumps(values, ensure_ascii=False)))
        self.db.commit()
        with (self.directory / 'daily.csv').open('w', encoding='utf-8-sig', newline='') as f:
            cols = ['date','k200','foreign_fut_qty','inst_fut_qty','foreign_spot_amt','nonarb_amt','basis','basis_residual','fut_code','quality','finalized',
                    'foreign_fut_qty_intraday','inst_fut_qty_intraday','confirmed_source','confirmed_at']
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for row in self.db.execute('SELECT day,payload FROM daily ORDER BY day'):
                d = json.loads(row['payload'])
                writer.writerow({'date':row['day'], **{k:d.get(k,'') for k in cols if k!='date'}})

    def daily_row(self, day):
        r = self.db.execute('SELECT payload FROM daily WHERE day=?', (day,)).fetchone()
        return json.loads(r['payload']) if r else None

    def daily(self):
        return {r['day']:json.loads(r['payload']) for r in self.db.execute('SELECT * FROM daily ORDER BY day')}

    def raw(self, at, source, payload):
        self.db.execute('INSERT INTO raw VALUES(?,?,?)', (at, source, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def close(self):
        self.db.close()
