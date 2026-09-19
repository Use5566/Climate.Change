"""Local durable C drafts/submissions. Configure a persistent disk before cloud use."""
import json
import os
from pathlib import Path
import sqlite3
import uuid
import unicodedata
from contextlib import closing
from datetime import datetime, timezone

from .article import ARTICLE_ID, article_data, article_for_work


class InvalidWork(ValueError):
    pass


class Conflict(ValueError):
    pass


def check_highlight_limit(clean):
    for text in clean['highlight_texts']:
        count = sum(not ch.isspace() and not unicodedata.category(ch).startswith('P') for ch in text)
        if count > 100:
            raise InvalidWork('每段劃記最多 100 字（不含標點與空白），請縮短劃記後再儲存。')


def validated(data, article_id=ARTICLE_ID, paragraphs=None):
    if not isinstance(data, dict) or data.get('article_id') != article_id:
        raise InvalidWork('文章版本不符，請重新整理頁面。')
    stance = data.get('stance')
    stage = data.get('stage')
    inference = data.get('inference')
    if stance not in ('支持', '反對') or stage not in ('reading', 'summary'):
        raise InvalidWork('請先選擇立場。')
    if not isinstance(inference, str) or len(inference) > 10000:
        raise InvalidWork('推論請勿超過 10,000 字。')
    highlights = data.get('highlights')
    if not isinstance(highlights, list) or len(highlights) > 500:
        raise InvalidWork('劃記資料格式不正確。')
    paragraphs = article_data()['paragraphs'] if paragraphs is None else paragraphs
    clean = []
    for mark in highlights:
        if not isinstance(mark, dict):
            raise InvalidWork('劃記資料格式不正確。')
        p, start, end = (mark.get(k) for k in ('p', 'start', 'end'))
        if any(type(x) is not int for x in (p, start, end)) or not 0 <= p < len(paragraphs):
            raise InvalidWork('劃記範圍不正確。')
        if not 0 <= start < end <= len(paragraphs[p]['text']):
            raise InvalidWork('劃記範圍不正確。')
        clean.append({'p': p, 'start': start, 'end': end})
    merged = []
    for mark in sorted(clean, key=lambda h: (h['p'], h['start'])):
        if merged and merged[-1]['p'] == mark['p'] and mark['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], mark['end'])
        else:
            merged.append(mark.copy())
    return {'article_id': article_id, 'stance': stance, 'stage': stage,
            'highlights': merged, 'inference': inference,
            'highlight_texts': [paragraphs[h['p']]['text'][h['start']:h['end']] for h in merged],
            'article_snapshot': [p['text'] for p in paragraphs]}


def validated_c(data, previous=None):
    article = article_data()
    payload = data
    # Compatibility with already saved hash IDs; never select an old article.
    if previous and data.get('article_id') == previous.get('article_id'):
        payload = {**data, 'article_id': article['id']}
    clean = validated(payload, article['id'], article['paragraphs'])
    if previous:
        # Keep the existing storage key so SQLite does not create a second row.
        clean['article_id'] = previous['article_id']
    return clean


class LearningStore:
    def __init__(self, path=None):
        configured = path or os.getenv('LEARNING_DB_PATH')
        # Never silently store real cloud submissions on Render's ephemeral filesystem.
        self.path = Path(configured) if configured else (
            None if os.getenv('RENDER') else Path(__file__).resolve().parent.parent / 'private/learning.sqlite3')

    def connect(self):
        if self.path is None:
            raise OSError('Persistent learning storage is not configured')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS c_work (
            classroom TEXT NOT NULL, seat TEXT NOT NULL, article_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT,
            PRIMARY KEY(classroom, seat, article_id))''')
        db.commit()
        return db

    def read(self, classroom, seat):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM c_work WHERE classroom=? AND seat=? ORDER BY updated_at DESC LIMIT 1',
                             (classroom, seat)).fetchone()
            return self.result(row) if row else None

    @staticmethod
    def result(row):
        data = json.loads(row['payload'])
        return {**data, 'attempt_id': row['attempt_id'], 'revision': row['revision'],
                'started_at': row['started_at'], 'updated_at': row['updated_at'],
                'submitted_at': row['submitted_at']}

    def save(self, classroom, seat, data, submit=False):
        revision = data.get('revision')
        if type(revision) is not int or revision < 0:
            raise InvalidWork('草稿版本不正確。')
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM c_work WHERE classroom=? AND seat=? ORDER BY updated_at DESC LIMIT 1',
                             (classroom, seat)).fetchone()
            clean = validated_c(data, self.result(row) if row else None)
            check_highlight_limit(clean)
            if submit and (clean['stage'] != 'summary' or not clean['inference'].strip()):
                raise InvalidWork('請填寫推論後再提交。')
            if row and row['submitted_at']:
                if submit and json.loads(row['payload']) == clean:
                    return self.result(row)  # A retry after a lost response is idempotent.
                raise Conflict('這份學習紀錄已提交，不能再修改。')
            if (row['revision'] if row else 0) != revision:
                if row and row['revision'] == revision + 1 and json.loads(row['payload']) == clean and not submit:
                    return self.result(row)
                raise Conflict('另一個頁面已更新草稿。請先保留推論文字，再重新整理。')
            db.execute('''INSERT INTO c_work VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(classroom, seat, article_id) DO UPDATE SET
                payload=excluded.payload, revision=excluded.revision,
                updated_at=excluded.updated_at, submitted_at=excluded.submitted_at''',
                (classroom, seat, clean['article_id'], row['attempt_id'] if row else str(uuid.uuid4()),
                 json.dumps(clean, ensure_ascii=False), revision + 1,
                 row['started_at'] if row else now, now, now if submit else None))
            saved = db.execute('SELECT * FROM c_work WHERE classroom=? AND seat=? AND article_id=?',
                               (classroom, seat, clean['article_id'])).fetchone()
            return self.result(saved)
