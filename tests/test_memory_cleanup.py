"""Recovery and failure tests for releasing resident student snapshots."""
import uuid
import pytest
from backend.chat import ChatService, validate_chat
from tests.test_buffered_learning import GoogleFake, store_for
from tests.test_learning import draft
from tests.test_chat import FakeAI


def test_thirty_saved_students_expire_and_resume_without_new_rows(monkeypatch):
    now = [0]
    monkeypatch.setattr('backend.buffered_learning.time.monotonic', lambda: now[0])
    google = GoogleFake()
    store = store_for(google)
    for seat in range(1, 31):
        store.save('601', f'{seat:02}', draft(inference=f'進度{seat}'))
    store.coordinator.flush()
    assert len(store.cache) == 30
    assert all(set(value) == {'revision', 'updated_at'} for value in store.confirmed.values())
    now[0] = 301
    store.coordinator.cleanup()
    assert not store.cache and not store.last_used and not store.dirty
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return google.request(*args, **kwargs)
    store.request = request
    restored = store.read('601', '17')
    assert restored['inference'] == '進度17'
    assert len(store.cache) == 1
    assert calls[0]['params']['ranges'] == "'C學習歷程'!A18:J18"
    store.save('601', '17', draft(revision=1, inference='繼續學習'))
    store.coordinator.flush()
    assert len(google.rows['C']) == 30
    assert store.read('601', '17')['revision'] == 2


def test_dirty_data_survives_idle_cleanup_and_lost_ack(monkeypatch):
    google = GoogleFake()
    store = store_for(google)
    saved = store.save('601', '01', draft(inference='不可遺失'))
    monkeypatch.setattr('backend.buffered_learning.time.monotonic', lambda: 10**12)
    google.fail = True
    with pytest.raises(OSError):
        store.coordinator.flush()
    store.coordinator.cleanup()
    assert store.cache[('601', '01')] == saved
    assert store.dirty and store.release_pending
    google.fail = False
    store.coordinator.flush()
    assert not store.cache and not store.dirty and not store.release_pending
    assert store.read('601', '01') == saved


def test_active_read_resets_idle_timeout_and_returns_isolated_copy(monkeypatch):
    now = [0]
    monkeypatch.setattr('backend.buffered_learning.time.monotonic', lambda: now[0])
    store = store_for(GoogleFake())
    saved = store.save('601', '01', draft())
    store.coordinator.flush()
    now[0] = 299
    copy = store.read('601', '01')
    copy['inference'] = '不可影響後端'
    now[0] = 301
    store.coordinator.cleanup()
    assert store.cache[('601', '01')] == saved


def test_completed_student_is_released_and_retry_restores_same_submission():
    store = store_for(GoogleFake())
    final = draft(stage='summary', inference='完成推論')
    saved = store.save('601', '01', final, submit=True)
    assert not store.cache and not store.dirty
    assert store.read('601', '01') == saved
    assert store.save('601', '01', final, submit=True) == saved
    assert not store.cache


@pytest.mark.parametrize('dirty', [False, True])
@pytest.mark.parametrize('group', ['A', 'B', 'C'])
def test_logout_releases_only_confirmed_data_and_login_resumes(group, dirty):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.roster import Roster
    from tests.test_chat import HEADERS
    store = store_for(GoogleFake(), group=group)
    data = draft(inference='登出前的紀錄')
    if group != 'C':
        store.validator = validate_chat
        data.update(article_id=store.article_id, messages=[], highlights=[])
    saved = store.save('601', '01', data)
    if not dirty:
        store.coordinator.flush()
    service = ChatService(stores={group: store})
    roster = Roster(lambda: [['班級', '座號', '密碼', '介面'], ['601', '01', '01234', group]])
    client = TestClient(create_app(roster, False, learning_store=store, chat_service=service))
    credentials = {'classroom': '601', 'seat': '01', 'password': '01234'}
    assert client.post('/api/login', headers=HEADERS, json=credentials).status_code == 200
    assert client.post('/api/logout', headers=HEADERS).status_code == 200
    assert bool(store.cache) == dirty
    store.coordinator.flush()
    assert not store.cache
    assert client.post('/api/login', headers=HEADERS, json=credentials).status_code == 200
    assert client.get(f'/api/{group.lower()}/work').json()['work'] == saved


def test_missing_recovery_row_is_an_error_not_a_new_empty_attempt():
    google = GoogleFake()
    store = store_for(google)
    store.save('601', '01', draft())
    store.coordinator.flush()
    store.release('601', '01')
    google.rows['C'].clear()
    with pytest.raises(OSError):
        store.read('601', '01')


def test_generated_answer_retried_without_new_ai_call_and_unsaved_answer_retained():
    google = GoogleFake()
    store = store_for(google, group='A')
    store.validator = validate_chat
    data = draft(article_id=store.article_id, messages=[], highlights=[])
    store.save('601', '01', data)
    ai = FakeAI()
    service = ChatService(stores={'A': store}, ai=ai)
    original = store.save
    def fail_answer(classroom, seat, data, *args):
        if data.get('messages') and data['messages'][-1]['role'] == 'model':
            raise OSError('Temporary failure')
        return original(classroom, seat, data, *args)
    store.save = fail_answer
    question = {**data, 'revision': 1, 'request_id': str(uuid.uuid4()), 'text': '為什麼？'}
    with pytest.raises(OSError):
        service.operate('A', '601', '01', question, 'message')
    service.cleanup()
    assert len(service._answers) == 1 and len(ai.calls) == 1
    store.save = original
    service.cleanup()
    assert not service._answers and store.dirty
    store.coordinator.flush()
    store.release('601', '01')
    restored = service.operate('A', '601', '01', question, 'message')
    assert len(restored['messages']) == 2 and len(ai.calls) == 1


def test_maintenance_does_not_block_an_inflight_ai_reply():
    service = ChatService(stores={'A': object()}, ai=FakeAI())
    key = ('A', '601', '01', str(uuid.uuid4()))
    service._answers[key] = ('回答', {})
    with service.lock('A', '601', '01'):
        service.cleanup()
    assert key in service._answers


def test_cleanup_runs_even_when_background_sync_fails():
    from backend.buffered_learning import SyncCoordinator
    coordinator = SyncCoordinator()
    class StopAfterOne:
        calls = 0
        def clear(self): pass
        def wait(self, interval):
            self.calls += 1
            return self.calls > 1
    coordinator.stop_event = StopAfterOne()
    coordinator.flush = lambda: (_ for _ in ()).throw(OSError('offline'))
    cleaned = []
    coordinator.cleanup = lambda: cleaned.append(True)
    coordinator.start()
    coordinator.thread.join(timeout=2)
    assert not coordinator.thread.is_alive() and cleaned == [True]
