"""Single-process Google Sheets learning history, separate from roster gid 0.

Every successful save appends a version. Retries reconcile against saved versions.
Never log Google responses or credentials. The teacher must not edit log rows.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from .article import ARTICLE_ID, article_for_work
from .learning import validated, InvalidWork, Conflict, check_highlight_limit, validated_c
from .roster import SERVICE_ACCOUNT, SPREADSHEET_ID

TAB = 'C學習歷程'
HEADERS = ['紀錄時間', '班級', '座號', '密碼', '介面', '成績統計', '立場',
           '提交狀態', '劃記原文', '學生提問及AI回答內容', '推論']


class SheetLearningStore:
    def __init__(self, session=None, interface='C', validator=validated):
        if interface not in ('A', 'B', 'C'):
            raise ValueError('Invalid interface')
        self.interface = interface
        self.headers = HEADERS
        self.accepted_headers = [HEADERS]
        self.tab = f'{interface}學習歷程'
        self.article_id = ARTICLE_ID if interface == 'C' else f'chat-{interface.lower()}-v1'
        self.validator = validator
        self._session = session
        self._lock = threading.Lock()
        self._ready = False
        self._sheet_id = None
        self.base = f'https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}'

    def request(self, method, suffix='', **kwargs):
        try:
            if self._session is None:
                from google.oauth2.service_account import Credentials
                from google.auth.transport.requests import AuthorizedSession
                raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
                if raw:
                    info = json.loads(raw)
                else:
                    with open(os.environ['GOOGLE_APPLICATION_CREDENTIALS'], encoding='utf-8-sig') as stream:
                        info = json.load(stream)
                if info.get('client_email') != SERVICE_ACCOUNT or info.get('type') != 'service_account':
                    raise ValueError('Wrong account')
                credentials = Credentials.from_service_account_info(
                    info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
                self._session = AuthorizedSession(credentials, refresh_timeout=15)
            response = self._session.request(method, self.base + suffix, timeout=15, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception:
            raise OSError('Google learning storage unavailable') from None

    def address(self, cells):
        return '/values/' + quote(f"'{self.tab}'!{cells}", safe='')

    def ensure(self):
        if self._ready:
            return
        metadata = self.request('GET', params={'fields': 'sheets.properties(sheetId,title)'})
        tab = next((s['properties'] for s in metadata.get('sheets', []) if s['properties']['title'] == self.tab), None)
        if tab and tab['sheetId'] == 0:
            raise OSError('Learning tab cannot be roster')
        if not tab:
            result = self.request('POST', ':batchUpdate', json={'requests': [
                {'addSheet': {'properties': {'title': self.tab, 'gridProperties': {'frozenRowCount': 1}}}}
            ]})
            tab = result['replies'][0]['addSheet']['properties']
        self._sheet_id = tab['sheetId']
        existing = self.request('GET', self.address('A1:1')).get('values', [])
        if not existing:
            # One contiguous RAW table; identifiers and student text remain literal.
            self.request('PUT', self.address('A1:K1'), params={'valueInputOption': 'RAW'}, json={'values': [self.headers]})
        elif len(existing) != 1 or [str(value).strip() for value in existing[0]] not in self.accepted_headers:
            raise OSError('Learning headers do not match')
        self._ready = True

    def latest(self, classroom, seat):
        self.ensure()
        result = self.request('GET', params={'ranges': f"'{self.tab}'!A2:K", 'includeGridData': 'true',
                              'fields': 'sheets.data.rowData.values(effectiveValue,note)'})
        rows = [row.get('values', []) for sheet in result.get('sheets', [])
                for grid in sheet.get('data', []) for row in grid.get('rowData', [])]
        latest = None
        for cells in rows:
            row = [cell.get('effectiveValue', {}).get('stringValue',
                   cell.get('effectiveValue', {}).get('numberValue', '')) for cell in cells]
            if len(row) < 5 or str(row[1]) != classroom or str(row[2]).zfill(2) != seat or row[4] != self.interface:
                continue
            try:
                # Recovery metadata lives in the inference cell's note, not extra columns.
                envelope = json.loads(cells[10]['note'])
                if envelope['schema'] != 'c-learning-v1':
                    raise ValueError()
                work = envelope['work']
                if not isinstance(work, dict) or type(work['revision']) is not int:
                    raise ValueError()
                if self.interface == 'C':
                    article_for_work(work)
                elif work['article_id'] != self.article_id:
                    continue
                if latest and work['revision'] == latest['revision'] and work != latest:
                    raise ValueError()
                if latest is None or work['revision'] > latest['revision']:
                    latest = work
            except (IndexError, KeyError, ValueError, TypeError):
                raise OSError('Learning history is damaged') from None
        return latest

    def read(self, classroom, seat):
        with self._lock:
            return self.latest(classroom, seat)

    def save(self, classroom, seat, data, submit=False):
        revision = data.get('revision')
        if type(revision) is not int or revision < 0:
            raise InvalidWork('草稿版本不正確。')
        with self._lock:
            old = self.latest(classroom, seat)
            clean = validated_c(data, old) if self.interface == 'C' else self.validator(data)
            check_highlight_limit(clean)
            if submit and (clean['stage'] != 'summary' or not clean['inference'].strip()):
                raise InvalidWork('請填寫推論後再提交。')
            same = old is not None and all(old.get(k) == v for k, v in clean.items())
            if old and old['submitted_at']:
                if submit and same:
                    return old
                raise Conflict('這份學習紀錄已提交，不能再修改。')
            if (old['revision'] if old else 0) != revision:
                if old and old['revision'] == revision + 1 and same and not submit:
                    return old
                raise Conflict('另一個頁面已更新草稿。請先保留推論文字，再重新整理。')
            now = datetime.now(timezone.utc)
            stamp = now.isoformat()
            work = {**clean, 'attempt_id': old['attempt_id'] if old else str(uuid.uuid4()),
                    'revision': revision + 1, 'started_at': old['started_at'] if old else stamp,
                    'updated_at': stamp, 'submitted_at': stamp if submit else None}
            row = [now.astimezone(timezone(timedelta(hours=8))).isoformat(), classroom, seat,
                   '', self.interface, '', clean['stance'], '已提交' if submit else '草稿',
                   '\n'.join(clean['highlight_texts']),
                   '\n\n'.join(('學生：' if m['role'] == 'user' else 'AI：') + m['text']
                               for m in clean.get('messages', [])), clean['inference']]
            note = json.dumps({'schema': 'c-learning-v1', 'work': work}, ensure_ascii=False, separators=(',', ':'))
            if any(len(value.encode('utf-16-le')) // 2 > 49000 for value in [*row, note]):
                raise InvalidWork('紀錄內容超過試算表單格容量，請減少劃記或推論。')
            cells = [{'userEnteredValue': {'stringValue': value}} for value in row]
            cells[10]['note'] = note
            # A single request writes both visible data and recovery metadata atomically.
            self.persist(classroom, seat, work, cells)
            return work

    def persist(self, classroom, seat, work, cells):
        self.request('POST', ':batchUpdate', json={'requests': [{'appendCells': {
            'sheetId': self._sheet_id, 'rows': [{'values': cells}], 'fields': 'userEnteredValue,note'
        }}]})
