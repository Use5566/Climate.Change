import json
import pytest
from backend import article
from backend.learning import InvalidWork, LearningStore
from tests.test_learning import draft, login, HEADERS, ROWS
from tests.test_buffered_learning import GoogleFake, store_for


def test_utf8_bom_paragraphs_bold_literal_html_and_stable_newlines(tmp_path):
    path = tmp_path / 'article.txt'
    text = '第一段**重點**。\n段內換行。\n\n第二段<script>文字</script>。'
    path.write_text(text, encoding='utf-8-sig')
    first = article.load_article(path)
    assert len(first['paragraphs']) == 2
    assert first['paragraphs'][0]['text'] == '第一段重點。\n段內換行。'
    assert first['paragraphs'][0]['parts'][1] == {'text': '重點', 'bold': True}
    assert '<script>' in first['paragraphs'][1]['text']
    path.write_bytes(text.replace('\n', '\r\n').encode('utf-8'))
    assert article.load_article(path) == first
    path.write_text(text + '修改', encoding='utf-8')
    assert article.load_article(path)['id'] != first['id']


@pytest.mark.parametrize('content', [b'', b'  \n\n', b'\xff\xff', ('字' * 20001).encode()],
                         ids=['empty', 'whitespace', 'encoding', 'oversized'])
def test_invalid_material_rejected(tmp_path, content):
    path = tmp_path / 'bad.txt'
    path.write_bytes(content)
    with pytest.raises(OSError):
        article.load_article(path)
    with pytest.raises(OSError):
        article.load_article(tmp_path / 'missing.txt')


@pytest.mark.parametrize('storage', ['sqlite', 'buffered'])
def test_article_edit_keeps_previous_student_and_new_students_get_new_text(tmp_path, monkeypatch, storage):
    if storage == 'sqlite':
        store = LearningStore(tmp_path / 'learning.db')
    else:
        store = store_for(GoogleFake())
    old = store.save('601', '01', draft())
    path = tmp_path / 'new.txt'
    path.write_text('新版科學文章第一段。\n\n第二段包含新證據。', encoding='utf-8')
    updated = article.load_article(path)
    monkeypatch.setattr(article, '_ARTICLE', updated)
    if storage == 'buffered':
        store.coordinator.flush()
        store.release('601', '01')
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.roster import Roster
    client = TestClient(create_app(Roster(lambda: ROWS), False, learning_store=store))
    login(client)
    restored = client.get('/api/c/work').json()
    assert restored['article']['id'] == old['article_id']
    assert restored['article']['paragraphs'][0]['text'] == old['article_snapshot'][0]
    assert restored['work']['highlight_texts'] == old['highlight_texts']
    saved = client.post('/api/c/draft', headers=HEADERS, json=draft(revision=1, inference='繼續舊文章'))
    assert saved.status_code == 200
    assert saved.json()['work']['article_snapshot'] == old['article_snapshot']
    # A stale page cannot silently apply offsets to the changed text.
    with pytest.raises(InvalidWork):
        store.save('601', '02', draft())
    login(client, '02')
    assert client.get('/api/c/work').json()['article'] == updated
    saved = client.post('/api/c/draft', headers=HEADERS, json=draft(
        seat='02', article_id=updated['id'], highlights=[{'p': 1, 'start': 0, 'end': 3}]))
    assert saved.status_code == 200
    assert saved.json()['work']['highlight_texts'] == ['第二段']
    assert saved.json()['work']['article_snapshot'] == [p['text'] for p in updated['paragraphs']]
    assert client.get('/prompts/interface_c.txt').status_code == 404


def test_legacy_google_note_survives_restart_and_submission():
    google = GoogleFake()
    store = store_for(google)
    original = store.save('601', '01', draft())
    store.coordinator.flush()
    cells = google.rows['C'][1]
    envelope = json.loads(cells[8]['note'])
    envelope['work']['article_id'] = 'c-reading-v1'
    cells[8]['note'] = json.dumps(envelope, ensure_ascii=False)
    restarted = store_for(google)
    restored = restarted.read('601', '01')
    assert restored['article_id'] == 'c-reading-v1'
    assert restored['highlight_texts'] == original['highlight_texts']
    result = restarted.save('601', '01', draft(article_id='c-reading-v1', revision=1,
        stage='summary', inference='原文章的推論'), submit=True)
    assert result['submitted_at']
    assert restarted.read('601', '01') == result


def test_client_cannot_supply_own_article_snapshot():
    store = store_for(GoogleFake())
    result = store.save('601', '01', draft(article_snapshot=['偽造的文章內容']))
    assert result['article_snapshot'] == [p['text'] for p in article.article_data()['paragraphs']]


def test_unavailable_c_article_returns_503_but_login_and_health_work(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.roster import Roster
    monkeypatch.setattr(article, '_ARTICLE', None)
    client = TestClient(create_app(Roster(lambda: ROWS), False, LearningStore(tmp_path / 'db')))
    login(client)
    assert client.get('/api/c/work').status_code == 503
    assert client.get('/api/session').status_code == 200
    assert client.get('/healthz').status_code == 200
