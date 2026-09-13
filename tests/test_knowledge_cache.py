import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest
from backend.knowledge_cache import KnowledgeCache, KnowledgeUnavailable
from backend.chat import Gemini, ChatUnavailable


@pytest.fixture
def configured(monkeypatch, tmp_path):
    path = tmp_path / 'teacher.txt'
    path.write_text('SCI-001 教材測試：節約能源。', encoding='utf-8')
    monkeypatch.setenv('GEMINI_KNOWLEDGE_MODE', 'explicit')
    monkeypatch.setenv('GEMINI_KNOWLEDGE_PATH', str(path))
    monkeypatch.setenv('GEMINI_CACHE_TTL_SECONDS', '3600')
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-key')
    monkeypatch.setenv('GEMINI_MODEL', 'test-model')
    return path


class CacheAPI:
    def __init__(self):
        self.entries, self.calls = [], []
        self.count = 100000
    def request(self, key, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path.startswith('cachedContents?'):
            return {'cachedContents': copy.deepcopy(self.entries)}
        if path.endswith(':countTokens'):
            return {'totalTokens': self.count}
        if method == 'GET' and path.startswith('models/'):
            return {'inputTokenLimit': 1048576}
        assert path == 'cachedContents' and method == 'POST'
        entry = {'name': 'cachedContents/item' + str(len(self.entries)),
                 'model': payload['model'], 'displayName': payload['displayName'],
                 'expireTime': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        self.entries.append(entry)
        return copy.deepcopy(entry)


def test_concurrent_students_share_cache_and_restart_reuses_it(configured):
    api, cache = CacheAPI(), KnowledgeCache()
    cache.request = api.request
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.prepare('key', 'test-model', 'A', '指令A'), range(30)))
    assert len(api.entries) == 1
    assert len({r[0]['name'] for r in results}) == 1
    restarted = KnowledgeCache()
    restarted.request = api.request
    assert restarted.prepare('key', 'test-model', 'A', '指令A')[0]['name'] == results[0][0]['name']
    assert len(api.entries) == 1


def test_group_prompt_material_versions_and_expiry(configured):
    api, cache = CacheAPI(), KnowledgeCache()
    cache.request = api.request
    a, digest = cache.prepare('key', 'test-model', 'A', '指令A')
    b, _ = cache.prepare('key', 'test-model', 'B', '指令B')
    assert a['name'] != b['name']
    new, _ = cache.prepare('key', 'test-model', 'A', '修改指令A')
    assert new['name'] != a['name']
    configured.write_text('SCI-002 新教材內容', encoding='utf-8')
    changed, new_digest = cache.prepare('key', 'test-model', 'A', '修改指令A')
    assert digest != new_digest and changed['name'] != new['name']
    for entry in [*api.entries, *cache.entries.values()]:
        entry['expireTime'] = '2000-01-01T00:00:00Z'
    fresh, _ = cache.prepare('key', 'test-model', 'A', '修改指令A')
    assert fresh['name'] != changed['name']


def test_capacity_checked_before_paid_cache_creation(configured):
    api, cache = CacheAPI(), KnowledgeCache()
    cache.request = api.request
    api.count = 1040000
    with pytest.raises(KnowledgeUnavailable, match='容量'):
        cache.prepare('key', 'test-model', 'A', '指令')
    assert api.entries == []


def test_missing_material_does_not_fall_back_to_ungrounded_generation(configured, monkeypatch):
    configured.unlink()
    monkeypatch.setattr('backend.chat.requests.post', lambda *a, **k: pytest.fail('Must not generate without material'))
    with pytest.raises(ChatUnavailable, match='教材'):
        Gemini().reply('A', '支持', [{'role': 'user', 'text': '問題'}])


def test_student_data_excluded_from_cache_and_usage_recorded(configured, monkeypatch):
    api, ai, calls = CacheAPI(), Gemini(), []
    ai.knowledge.request = api.request
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '依 SCI-001，節約能源。'}]}}],
                    'usageMetadata': {'promptTokenCount': 101000, 'cachedContentTokenCount': 100000,
                                      'candidatesTokenCount': 20, 'thoughtsTokenCount': 10}}
    def post(url, **kwargs):
        calls.append(copy.deepcopy(kwargs['json']))
        return Response()
    monkeypatch.setattr('backend.chat.requests.post', post)
    text, meta = ai.reply('A', '反對', [{'role': 'user', 'text': '學生私有問題', 'request_id': 'secret-id'}])
    created = next(p for m, path, p in api.calls if path == 'cachedContents')
    assert '學生私有問題' not in str(created) and 'secret-id' not in str(created)
    assert 'systemInstruction' not in calls[0] and calls[0]['cachedContent']
    assert '反對' in str(calls[0]['contents']) and '學生私有問題' in str(calls[0]['contents'])
    assert meta['knowledge_sha256'] and meta['token_usage']['cachedContentTokenCount'] == 100000


def test_stale_cache_refresh_does_not_create_one_cache_per_student(configured):
    api, cache = CacheAPI(), KnowledgeCache()
    cache.request = api.request
    old, _ = cache.prepare('key', 'test-model', 'A', '指令')
    fresh, _ = cache.prepare('key', 'test-model', 'A', '指令', refresh=old['name'])
    again, _ = cache.prepare('key', 'test-model', 'A', '指令', refresh=old['name'])
    assert fresh['name'] == again['name'] != old['name']
    assert len(api.entries) == 2


def test_generate_missing_cache_rebuilds_once(configured, monkeypatch):
    api, ai, calls = CacheAPI(), Gemini(), []
    ai.knowledge.request = api.request
    class Response:
        def __init__(self, status): self.status_code = status
        def raise_for_status(self): assert self.status_code == 200
        def json(self):
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '回答'}]}}]}
    def post(url, **kwargs):
        calls.append(copy.deepcopy(kwargs['json']))
        return Response(404 if len(calls) == 1 else 200)
    monkeypatch.setattr('backend.chat.requests.post', post)
    assert ai.reply('A', '支持', [{'role': 'user', 'text': '問題'}])[0] == '回答'
    assert len(calls) == 2 and calls[0]['cachedContent'] != calls[1]['cachedContent']


def test_api_errors_are_redacted_and_cool_down(monkeypatch):
    calls, cache = [], KnowledgeCache()
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('sensitive-key-and-material')
    monkeypatch.setattr('backend.knowledge_cache.requests.request', fail)
    with pytest.raises(KnowledgeUnavailable) as error:
        cache.request('secret', 'GET', 'cachedContents')
    assert 'sensitive' not in str(error.value)
    with pytest.raises(KnowledgeUnavailable):
        cache.request('secret', 'GET', 'cachedContents')
    assert len(calls) == 1
