import json
import pytest
from backend import article
from backend.learning import InvalidWork, LearningStore
from tests.test_learning import draft, login, HEADERS, ROWS
from tests.test_buffered_learning import GoogleFake, store_for


def test_two_heading_levels_preserve_saved_highlight_positions(tmp_path):
    path = tmp_path / 'article.txt'
    original = '文章標題\n第一個小標題\n\n內文**證據**與數字 32°C。\n\n第二個小標題\n\n第二段。'
    path.write_text(original, encoding='utf-8')
    before = article.load_article(path)
    formatted = original.replace('文章標題', '# 文章標題').replace('第一個小標題', '## 第一個小標題').replace('第二個小標題', '## 第二個小標題')
    path.write_text(formatted, encoding='utf-8')
    after = article.load_article(path)
    assert [p['text'] for p in after['paragraphs']] == [p['text'] for p in before['paragraphs']]
    assert after['paragraphs'][0]['parts'][0]['heading'] == 1
    assert after['paragraphs'][0]['parts'][1]['heading'] == 2
    assert after['paragraphs'][2]['parts'][0]['heading'] == 2
    assert after['paragraphs'][1] == before['paragraphs'][1]


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
    assert article.load_article(path)['id'] == first['id'] == 'c-reading-v1'
    assert article.load_article(path)['paragraphs'] != first['paragraphs']


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
def test_all_students_read_same_file_even_with_saved_snapshot(tmp_path, monkeypatch, storage):
    store = LearningStore(tmp_path / 'learning.db') if storage == 'sqlite' else store_for(GoogleFake())
    store.save('601', '01', draft())
    path = tmp_path / 'article.txt'
    path.write_text('固定科學文章第一段。\n\n第二段包含科學證據。', encoding='utf-8')
    current = article.load_article(path)
    monkeypatch.setattr(article, '_ARTICLE', current)
    if storage == 'buffered':
        store.coordinator.flush()
        store.release('601', '01')
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.roster import Roster
    client = TestClient(create_app(Roster(lambda: ROWS), False, learning_store=store))
    for seat in ('01', '02'):
        login(client, seat)
        response = client.get('/api/c/work')
        assert response.status_code == 200
        assert response.json()['article'] == current
        saved = client.post('/api/c/draft', headers=HEADERS, json=draft(
            seat=seat, revision=1 if seat == '01' else 0,
            highlights=[{'p': 1, 'start': 0, 'end': 3}]))
        assert saved.status_code == 200
        assert saved.json()['work']['highlight_texts'] == ['第二段']
        assert saved.json()['work']['article_snapshot'] == [p['text'] for p in current['paragraphs']]
    assert client.get('/prompts/interface_c.txt').status_code == 404


def test_legacy_google_note_survives_restart_and_submission():
    google = GoogleFake()
    store = store_for(google)
    original = store.save('601', '01', draft())
    store.coordinator.flush()
    cells = google.rows['C'][1]
    envelope = json.loads(cells[8]['note'])
    legacy_id = 'c-reading-' + 'a' * 64
    envelope['work']['article_id'] = legacy_id
    cells[8]['note'] = json.dumps(envelope, ensure_ascii=False)
    restarted = store_for(google)
    restored = restarted.read('601', '01')
    assert restored['article_id'] == legacy_id
    assert article.article_for_work(restored) == article.article_data()
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
