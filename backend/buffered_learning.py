"""Single-worker, bounded latest-state cache; one contiguous row per student.

Only Google-acknowledged submissions are exposed as complete. Progress tabs
are read for migration, while history tabs hold latest states. No passwords
enter this cache.
"""
import copy
import json
import logging
import threading
from .sheet_learning import SheetLearningStore


class SyncCoordinator:
    interval = 35

    def __init__(self):
        self.lock = threading.RLock()
        self.stores = []
        self.stop_event = threading.Event()
        self.thread = None

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
            store.confirmed[key] = copy.deepcopy(work)
            store.dirty.pop(key, None)
            store.failed = False

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
    def __init__(self, coordinator, **kwargs):
        super().__init__(**kwargs)
        self.tab = f'{self.interface}學習歷程'
        self.coordinator = coordinator
        self._lock = coordinator.lock
        self.cache, self.confirmed, self.dirty = {}, {}, {}
        self.row_map = {}
        self.loaded = False
        self.failed = False
        coordinator.stores.append(self)

    @staticmethod
    def validate_student(classroom, seat):
        if classroom not in ('601', '602', '603', '604', '605') or seat not in tuple(f'{i:02}' for i in range(1, 33)):
            raise ValueError('Invalid student')

    def _load_rows(self, tab):
        result = self.request('GET', params={'ranges': f"'{tab}'!A2:K",
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
            envelope = json.loads(cells[10]['note'])
            work = envelope['work']
            if envelope['schema'] != 'c-learning-v1' or values[4] != self.interface:
                raise ValueError()
            if work['article_id'] != self.article_id or type(work['revision']) is not int:
                raise ValueError()
            self.validator(work)
            return key, work
        except (KeyError, IndexError, TypeError, ValueError):
            raise OSError('Learning recovery metadata does not match') from None

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
        rows = [{'values': [{'userEnteredValue': c.get('effectiveValue', {'stringValue': ''}),
                             **({'note': c['note']} if 'note' in c else {})}
                            for c in current_cells[key]]} for key in current]
        end = max(len(history_rows), len(rows), 1) + 1
        requests = [{'updateCells': {
            'range': {'sheetId': self._sheet_id, 'startRowIndex': 1, 'endRowIndex': end,
                      'startColumnIndex': 0, 'endColumnIndex': 11},
            'rows': rows, 'fields': 'userEnteredValue,note'}},
            {'repeatCell': {
                'range': {'sheetId': self._sheet_id, 'startRowIndex': 1,
                          'startColumnIndex': 0, 'endColumnIndex': 11},
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
        self.cache = current
        self.confirmed = copy.deepcopy(self.cache)
        self.loaded = True

    def latest(self, classroom, seat):
        self.validate_student(classroom, seat)
        self._load()
        return copy.deepcopy(self.cache.get((classroom, seat)))

    def read(self, classroom, seat):
        with self._lock:
            work = self.latest(classroom, seat)
            # A lost acknowledgement cannot produce a false completion screen.
            if work and work.get('submitted_at') and (classroom, seat) in self.dirty:
                self.coordinator.flush()
            return work

    def persist(self, classroom, seat, work, cells):
        key = classroom, seat
        self.row_map.setdefault(key, len(self.row_map) + 1)
        self.cache[key] = copy.deepcopy(work)
        self.dirty[key] = (copy.deepcopy(work), {'updateCells': {
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
