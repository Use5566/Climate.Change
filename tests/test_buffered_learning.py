import copy
import pytest
from backend.buffered_learning import BufferedLearningStore, SyncCoordinator
from backend.learning import Conflict
from tests.test_learning import draft


class GoogleFake:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = False

    def request(self, method, suffix='', **kwargs):
        if method == 'GET':
            if 'ranges' not in kwargs.get('params', {}):
                return {'sheets': []}
            group = kwargs['params']['ranges'][1]
            rows = self.rows.get(group, {})
            return {'sheets': [{'data': [{'rowData': [
                {'values': copy.deepcopy(rows.get(i, []))} for i in range(1, max(rows, default=0) + 1)]}]}]}
        requests = kwargs['json']['requests']
        self.calls.append(copy.deepcopy(requests))
        for request in requests:
            change = request['updateCells']
            group = chr(change['start']['sheetId'])
            row = change['start']['rowIndex']
            self.rows.setdefault(group, {})[row] = [
                {'effectiveValue': c['userEnteredValue'], **({'note': c['note']} if 'note' in c else {})}
                for c in change['rows'][0]['values']]
        if self.fail:
            raise OSError('Lost acknowledgement')
        return {}


def store_for(google, coordinator=None, group='C'):
    store = BufferedLearningStore(coordinator or SyncCoordinator(), interface=group)
    store.request = google.request
    store._ready, store._sheet_id = True, ord(group)
    return store


def test_thirty_students_one_batch_fixed_rows_restore_and_noop():
    google = GoogleFake()
    store = store_for(google)
    for seat in range(1, 31):
        store.save('601', f'{seat:02}', draft())
        store.save('601', f'{seat:02}', draft(revision=1, inference='最新推論'))
    assert google.calls == []
    assert store.sync_status('601', '01')['pending']
    store.coordinator.flush()
    assert len(google.calls) == 1 and len(google.calls[0]) == 30
    assert len(google.rows['C']) == 30
    assert not store.sync_status('601', '01')['pending']
    store.coordinator.flush()
    assert len(google.calls) == 1
    restored = store_for(google)
    assert restored.read('601', '01')['inference'] == '最新推論'
    assert restored.read('601', '31') is None
    restored.save('601', '01', draft(revision=2, inference='再次更新'))
    restored.coordinator.flush()
    assert len(google.rows['C']) == 30
    values = [c['effectiveValue']['stringValue'] for c in google.rows['C'][1]]
    assert len(values) == 11 and values[3] == values[5] == values[9] == ''
    assert values[10] == '再次更新'


def test_failed_submission_never_reports_success_and_retry_is_idempotent():
    google = GoogleFake()
    store = store_for(google)
    store.save('601', '01', draft())
    final = draft(revision=1, stage='summary', inference='完成')
    google.fail = True
    with pytest.raises(OSError):
        store.save('601', '01', final, True)
    with pytest.raises(OSError):
        store.read('601', '01')
    assert store.sync_status('601', '01')['failed']
    google.fail = False
    result = store.save('601', '01', final, True)
    assert result['submitted_at']
    assert len(google.rows['C']) == 1
    assert store.read('601', '01') == result
    with pytest.raises(Conflict):
        store.save('601', '01', draft(revision=2))


def test_interrupted_process_restores_last_confirmed_snapshot():
    google = GoogleFake()
    store = store_for(google)
    store.save('601', '01', draft())
    store.coordinator.flush()
    store.save('601', '01', draft(revision=1, inference='尚未同步'))
    assert store_for(google).read('601', '01')['revision'] == 1
    assert store.read('601', '01')['revision'] == 2


def test_all_groups_share_batch_and_largest_student_row():
    google, coordinator = GoogleFake(), SyncCoordinator()
    from backend.chat import validate_chat
    for group in 'ABC':
        store = store_for(google, coordinator, group)
        data = draft()
        if group != 'C':
            store.validator = validate_chat
            data.update(article_id=store.article_id, messages=[], highlights=[])
        store.save('605', '32', data)
    coordinator.flush()
    assert len(google.calls) == 1 and len(google.calls[0]) == 3
    assert all(160 in rows for rows in google.rows.values())


def test_legacy_latest_migrates_without_changing_history():
    from tests.test_sheet_learning import MemorySheet
    old = MemorySheet()
    old.save('601', '01', draft())
    latest = old.save('601', '01', draft(revision=1, inference='舊紀錄'))
    original = copy.deepcopy(old.rows)
    google = GoogleFake()
    store = store_for(google)
    def request(method, suffix='', **kwargs):
        params = kwargs.get('params', {})
        if method == 'GET' and 'ranges' not in params:
            return {'sheets': [{'properties': {'title': 'C學習歷程', 'sheetId': 12}}]}
        if '學習歷程' in params.get('ranges', ''):
            return {'sheets': [{'data': [{'rowData': [{'values': row} for row in old.rows]}]}]}
        return google.request(method, suffix, **kwargs)
    store.request = request
    assert store.read('601', '01') == latest
    store.coordinator.flush()
    assert old.rows == original and len(google.rows['C']) == 1
    assert store_for(google).read('601', '01') == latest


def test_scheduler_uses_35_seconds_and_stops():
    coordinator = SyncCoordinator()
    waits, flushes = [], []
    class FakeEvent:
        def clear(self): pass
        def wait(self, interval):
            waits.append(interval)
            return len(waits) > 1
    coordinator.stop_event = FakeEvent()
    coordinator.flush = lambda: flushes.append(True)
    coordinator.start()
    coordinator.thread.join(timeout=2)
    assert not coordinator.thread.is_alive()
    assert waits == [35, 35] and flushes == [True]


def test_sync_route_requires_correct_student_group():
    from tests.test_chat import fixture
    client, _, _ = fixture()
    assert client.get('/api/c/sync').status_code == 401
    response = client.post('/api/login', json={'classroom': '601', 'seat': '01', 'password': '01234'},
                          headers={'X-Learning-Client': '1'})
    assert response.status_code == 200
    assert client.get('/api/c/sync').status_code == 403
    assert client.get('/api/a/sync').json()['pending'] is False
