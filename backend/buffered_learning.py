"""Single-worker, bounded latest-state cache; one contiguous row per student.

Only Google-acknowledged submissions are exposed as complete. Progress tabs
are read for migration, while history tabs hold latest states. No passwords
enter this cache.
"""
import copy
import json
import logging
import threading
import time
from .sheet_learning import SheetLearningStore, HEADERS

PROGRESS_HEADERS = ['紀錄時間', '班級', '座號', '介面', '立場', '提交狀態',
                    '劃記原文', '學生提問及AI回答內容', '推論', '對話總 tokens']


def conversation_tokens(work):
    total, missing = 0, 0
    for message in work.get('messages', []):
        if message.get('role') != 'model':
            continue
        usage = message.get('token_usage', {})
        value = usage.get('totalTokenCount')
        if type(value) is not int or value < 0:
            missing += 1
        else:
            total += value
    return f'資料不完整（已知 {total}）' if missing else total


class SyncCoordinator:
    interval = 35

    def __init__(self):
        self.lock = threading.RLock()
        self.stores = []
        self.stop_event = threading.Event()
        self.thread = None
        self.maintenance = []

    def flush(self):
        # One lock serializes snapshots, acknowledgements and updates, including
        # immediate submissions, so an older write can never overwrite a newer one.
        with self.lock:
            pending = [(s, key, entry) for s in self.stores for key, entry in s.dirty.items()]
            batch, size = [], 0
            for item in pending:
                encoded = len(json.dumps(item[2][1], ensure_ascii=False).encode('utf-8'))
                if batch and size + encoded > 1_500_000:
                    self._send(batch)
                    batch, size = [], 0
                batch.append(item)
                size += encoded
            if batch:
                self._send(batch)

    def _send(self, batch):
        try:
            batch[0][0].request('POST', ':batchUpdate', json={
                'requests': [entry[1] for _, _, entry in batch]})
        except OSError:
            for store, _, _ in batch:
                store.failed = True
            raise
        for store, key, (work, _) in batch:
            store.confirmed[key] = store.receipt(work)
            store.dirty.pop(key, None)
            store.failed = False
            if work.get('submitted_at') or key in store.release_pending:
                store.release(*key)

    def cleanup(self):
        # Run even when Google is unavailable. Never evict unacknowledged work.
        with self.lock:
            for store in self.stores:
                store.cleanup()
        for callback in self.maintenance:
            try:
                callback()
            except OSError:
                logging.getLogger('learning').warning('Learning maintenance deferred; unsaved data retained')

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def loop():
            while not self.stop_event.wait(self.interval):
                try:
                    self.flush()
                except OSError:
                    logging.getLogger('learning').warning('Learning sync failed; latest drafts retained for retry')
                finally:
                    self.cleanup()
        self.thread = threading.Thread(target=loop, daemon=True, name='learning-sync')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        try:
            self.flush()
        except OSError:
            logging.getLogger('learning').warning('Final learning sync failed')


class BufferedLearningStore(SheetLearningStore):
    idle_seconds = 300

    def __init__(self, coordinator, **kwargs):
        super().__init__(**kwargs)
        self.headers = PROGRESS_HEADERS
        self.accepted_headers = [HEADERS, PROGRESS_HEADERS]
        self.tab = f'{self.interface}學習歷程'
        self.coordinator = coordinator
        self._lock = coordinator.lock
        self.cache, self.confirmed, self.dirty = {}, {}, {}
        self.last_used = {}
        self.release_pending = set()
        self.row_map = {}
        self.loaded = False
        self.failed = False
        coordinator.stores.append(self)

    @staticmethod
    def validate_student(classroom, seat):
        if classroom not in ('601', '602', '603', '604', '605') or seat not in tuple(f'{i:02}' for i in range(1, 33)):
            raise ValueError('Invalid student')

    def _load_rows(self, tab, cells='A2:K'):
        result = self.request('GET', params={'ranges': f"'{tab}'!{cells}",
            'includeGridData': 'true', 'fields': 'sheets.data.rowData.values(effectiveValue,note)'})
        return [r.get('values', []) for s in result.get('sheets', [])
                for g in s.get('data', []) for r in g.get('rowData', [])]

    def _decode(self, cells):
        try:
            values = [c.get('effectiveValue', {}).get('stringValue',
                      c.get('effectiveValue', {}).get('numberValue', '')) for c in cells]
            if not any(values):
                return None
            key = (str(values[1]), str(values[2]).zfill(2))
            self.validate_student(*key)
            modern = len(values) > 3 and values[3] == self.interface
            envelope = json.loads(cells[8 if modern else 10]['note'])
            work = envelope['work']
            if envelope['schema'] != 'c-learning-v1' or values[3 if modern else 4] != self.interface:
                raise ValueError()
            if work['article_id'] != self.article_id or type(work['revision']) is not int:
                raise ValueError()
            self.validator(work)
            return key, work
        except (KeyError, IndexError, TypeError, ValueError):
            raise OSError('Learning recovery metadata does not match') from None

    def new_cells(self, cells, work):
        # Input may be old 11-column data or the already migrated 10-column row.
        entered = [{**c, 'userEnteredValue': c.get('userEnteredValue', c.get('effectiveValue', {'stringValue': ''}))}
                   for c in cells]
        modern = entered[3]['userEnteredValue'].get('stringValue') == self.interface
        indexes = range(9) if modern else (0, 1, 2, 4, 6, 7, 8, 9, 10)
        output = [{'userEnteredValue': entered[i]['userEnteredValue'],
                   **({'note': entered[i]['note']} if 'note' in entered[i] else {})} for i in indexes]
        tokens = conversation_tokens(work)
        output.append({'userEnteredValue': {'numberValue': tokens} if type(tokens) is int else {'stringValue': tokens}})
        return output

    def _load(self):
        if self.loaded:
            return
        self.ensure()
        history_rows = self._load_rows(self.tab)
        current, current_cells = {}, {}

        def merge(rows):
            for cells in rows:
                record = self._decode(cells)
                if not record:
                    continue
                key, work = record
                previous = current.get(key)
                if previous:
                    if previous['attempt_id'] != work['attempt_id']:
                        raise OSError('Multiple learning attempts require teacher reconciliation')
                    if previous['revision'] == work['revision'] and previous != work:
                        raise OSError('Conflicting learning revisions')
                    if previous['revision'] >= work['revision']:
                        continue
                current[key], current_cells[key] = work, cells

        merge(history_rows)
        metadata = self.request('GET', params={'fields': 'sheets.properties(sheetId,title)'})
        old_tab = f'{self.interface}學習進度'
        progress = next((s['properties'] for s in metadata.get('sheets', [])
                         if s['properties']['title'] == old_tab and s['properties']['sheetId'] != 0), None)
        if progress:
            merge(self._load_rows(old_tab))

        # First appearance determines row order. Compact old snapshots and holes
        # atomically with values + recovery notes; retries repeat the same result.
        rows = [{'values': self.new_cells(current_cells[key], current[key])} for key in current]
        end = max(len(history_rows), len(rows), 1) + 1
        requests = [{'updateCells': {
            'range': {'sheetId': self._sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
                      'startColumnIndex': 0, 'endColumnIndex': 11},
            'rows': [{'values': [{'userEnteredValue': {'stringValue': h}} for h in PROGRESS_HEADERS]}],
            'fields': 'userEnteredValue,note'}}, {'updateCells': {
            'range': {'sheetId': self._sheet_id, 'startRowIndex': 1, 'endRowIndex': end,
                      'startColumnIndex': 0, 'endColumnIndex': 11},
            'rows': rows, 'fields': 'userEnteredValue,note'}},
            {'repeatCell': {
                'range': {'sheetId': self._sheet_id, 'startRowIndex': 1,
                          'startColumnIndex': 0, 'endColumnIndex': 10},
                'cell': {'userEnteredFormat': {'verticalAlignment': 'TOP', 'wrapStrategy': 'CLIP'}},
                'fields': 'userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy'}},
            {'updateDimensionProperties': {
                'range': {'sheetId': self._sheet_id, 'dimension': 'ROWS', 'startIndex': 1},
                'properties': {'pixelSize': 60}, 'fields': 'pixelSize'}}]
        if progress:
            requests.append({'updateSheetProperties': {
                'properties': {'sheetId': progress['sheetId'], 'hidden': True}, 'fields': 'hidden'}})
        self.request('POST', ':batchUpdate', json={'requests': requests})
        self.row_map = {key: index for index, key in enumerate(current, 1)}
        # Keep only row addresses and tiny receipts after migration. Load full
        # records on demand, rather than retaining the entire class forever.
        self.confirmed = {key: self.receipt(work) for key, work in current.items()}
        self.loaded = True

    @staticmethod
    def receipt(work):
        return {'revision': work['revision'], 'updated_at': work['updated_at']}

    def release(self, classroom, seat):
        with self._lock:
            key = classroom, seat
            if key in self.dirty:
                self.release_pending.add(key)
                return
            self.cache.pop(key, None)
            self.last_used.pop(key, None)
            self.release_pending.discard(key)

    def cleanup(self):
        with self._lock:
            now = time.monotonic()
            for key, used in list(self.last_used.items()):
                if now - used >= self.idle_seconds:
                    self.release(*key)

    def latest(self, classroom, seat):
        self.validate_student(classroom, seat)
        self._load()
        key = classroom, seat
        if key not in self.cache and key in self.row_map:
            row = self.row_map[key] + 1
            rows = self._load_rows(self.tab, f'A{row}:J{row}')
            record = self._decode(rows[0]) if len(rows) == 1 else None
            if not record or record[0] != key:
                raise OSError('Learning row moved or missing')
            work = record[1]
            self.confirmed[key] = self.receipt(work)
            if work.get('submitted_at'):
                return work  # Completed records need no resident full copy.
            self.cache[key] = work
        if key in self.cache:
            self.last_used[key] = time.monotonic()
            self.release_pending.discard(key)
        return copy.deepcopy(self.cache.get(key))

    def read(self, classroom, seat):
        with self._lock:
            work = self.latest(classroom, seat)
            # A lost acknowledgement cannot produce a false completion screen.
            if work and work.get('submitted_at') and (classroom, seat) in self.dirty:
                self.coordinator.flush()
            return work

    def persist(self, classroom, seat, work, cells):
        cells = self.new_cells(cells, work)
        key = classroom, seat
        self.row_map.setdefault(key, len(self.row_map) + 1)
        self.cache[key] = copy.deepcopy(work)
        self.last_used[key] = time.monotonic()
        self.release_pending.discard(key)
        # Internal snapshots are replaced, never mutated or returned directly.
        self.dirty[key] = (self.cache[key], {'updateCells': {
            'start': {'sheetId': self._sheet_id, 'rowIndex': self.row_map[key], 'columnIndex': 0},
            'rows': [{'values': cells}], 'fields': 'userEnteredValue,note'}})

    def save(self, classroom, seat, data, submit=False):
        with self._lock:
            work = super().save(classroom, seat, data, submit)
            if submit:
                self.coordinator.flush()
            return work

    def sync_status(self, classroom, seat):
        with self._lock:
            key = classroom, seat
            confirmed = self.confirmed.get(key, {})
            return {'pending': key in self.dirty, 'failed': self.failed and key in self.dirty,
                    'revision': confirmed.get('revision', 0),
                    'saved_at': confirmed.get('updated_at'), 'interval_seconds': self.coordinator.interval}
