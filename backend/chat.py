"""Server-owned A/B chat history and Gemini adapter. No client supplied AI replies."""
import hashlib
import os
from pathlib import Path
import re
import threading
import uuid
import requests

from .learning import validated, InvalidWork, Conflict
from .sheet_learning import SheetLearningStore
from .knowledge_cache import KnowledgeCache, KnowledgeUnavailable

PROMPTS = Path(__file__).resolve().parent.parent / 'prompts'


class ChatUnavailable(OSError):
    pass


def validate_chat(data):
    messages = data.get('messages', [])
    if not isinstance(messages, list) or len(messages) > 40:
        raise InvalidWork('本次對話已達上限，請完成摘要與推論。')
    total = 0
    for index, message in enumerate(messages):
        if (not isinstance(message, dict) or message.get('role') != ('user' if index % 2 == 0 else 'model')
                or not isinstance(message.get('text'), str) or not message['text'].strip()):
            raise InvalidWork('對話資料格式不正確。')
        total += len(message['text'])
    if total > 12000:
        raise InvalidWork('本次對話內容已達上限，請完成摘要與推論。')
    if data.get('article_id') not in ('chat-a-v1', 'chat-b-v1'):
        raise InvalidWork('學習版本不正確。')
    clean = validated(data, data['article_id'], [{'text': m['text']} for m in messages])
    # Transcript is stored once in recovery metadata to fit Google's cell limits.
    clean.pop('article_snapshot')
    return {**clean, 'messages': messages}


class Gemini:
    def __init__(self):
        self.knowledge = KnowledgeCache()

    def reply(self, group, stance, messages):
        key = os.getenv('GEMINI_API_KEY', '').strip()
        model = os.getenv('GEMINI_MODEL', '').strip()
        if not key or not model:
            raise ChatUnavailable('AI 尚未設定完成，請老師設定 GEMINI_API_KEY 與 GEMINI_MODEL。')
        if not re.fullmatch(r'[A-Za-z0-9._-]+', model):
            raise ChatUnavailable('AI 模型名稱設定不正確。')
        try:
            prompt = (PROMPTS / f'interface_{group.lower()}.txt').read_text(encoding='utf-8-sig').strip()
            if not prompt:
                raise ChatUnavailable('AI 教學提示尚未填寫，請通知老師。')
            contents = [{'role': m['role'], 'parts': [{'text': m['text']}]} for m in messages]
            body = {'systemInstruction': {'parts': [{'text': prompt + '\n學生目前選擇的立場：' + stance}]},
                    'contents': contents, 'generationConfig': {'maxOutputTokens': 1200}}
            cache, digest = None, None
            if self.knowledge.enabled():
                cache, digest = self.knowledge.prepare(key, model, group, prompt)
                body.pop('systemInstruction')
                body['cachedContent'] = cache['name']
                # Dynamic stance belongs to this student's request, never the shared cache.
                contents = [{'role': m['role'], 'parts': [{'text': m['text']}]} for m in messages]
                if contents:
                    contents[0]['parts'].insert(0, {'text': '學生目前選擇的立場：' + stance})
                body['contents'] = contents
            response = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                headers={'x-goog-api-key': key}, timeout=(10, 45),
                json=body)
            if cache and getattr(response, 'status_code', None) == 404:
                cache, digest = self.knowledge.prepare(key, model, group, prompt, refresh=cache['name'])
                body['cachedContent'] = cache['name']
                response = requests.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                    headers={'x-goog-api-key': key}, timeout=(10, 45), json=body)
            response.raise_for_status()
            result = response.json()
            candidates = result.get('candidates', [])
            if not candidates or candidates[0].get('finishReason') != 'STOP':
                raise ChatUnavailable('AI 這次未能提供完整回覆，請稍後重試或調整問題。')
            text = '\n'.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', [])
                             if not p.get('thought')).strip()
            if not text or len(text) > 4000:
                raise ChatUnavailable('AI 這次未能提供合適長度的回覆，請調整問題後重試。')
            metadata = {'model': model, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
            if cache:
                metadata.update(knowledge_sha256=digest, cache_fingerprint=cache['displayName'],
                                knowledge_mode='explicit')
            usage = result.get('usageMetadata', {})
            metadata['token_usage'] = {k: usage[k] for k in (
                'promptTokenCount', 'cachedContentTokenCount', 'candidatesTokenCount',
                'thoughtsTokenCount', 'totalTokenCount') if type(usage.get(k)) is int}
            return text, metadata
        except KnowledgeUnavailable as error:
            raise ChatUnavailable(str(error)) from None
        except ChatUnavailable:
            raise
        except Exception:
            raise ChatUnavailable('AI 連線未完成，請稍後重試；若持續發生，請老師檢查模型、金鑰與配額。') from None


class ChatService:
    def __init__(self, stores=None, ai=None):
        self.stores = stores or {g: SheetLearningStore(interface=g, validator=validate_chat) for g in ('A', 'B')}
        self.ai = ai or Gemini()
        self._locks = {}
        self._guard = threading.Lock()
        self._answers = {}  # Recover a generated reply if Sheets temporarily fails.

    def lock(self, group, classroom, seat):
        with self._guard:
            return self._locks.setdefault((group, classroom, seat), threading.Lock())

    def read(self, group, classroom, seat):
        return self.stores[group].read(classroom, seat)

    def cleanup(self):
        # Failed saves retain generated answers. Retry storing them without
        # calling Gemini again; release only after the store accepts the reply.
        with self._guard:
            keys = list(self._answers)
        for cache_key in keys:
            group, classroom, seat, request_id = cache_key
            student_lock = self.lock(group, classroom, seat)
            if not student_lock.acquire(blocking=False):
                continue
            try:
                with self._guard:
                    answer = self._answers.get(cache_key)
                if answer is None:
                    continue
                store = self.stores[group]
                old = store.read(classroom, seat)
                messages = old.get('messages', []) if old else []
                if any(m.get('request_id') == request_id and m['role'] == 'model' for m in messages):
                    pass  # A previous save succeeded but its acknowledgement was lost.
                elif (old and not old.get('submitted_at') and messages
                      and messages[-1]['role'] == 'user'
                      and messages[-1].get('request_id') == request_id):
                    text, metadata = answer
                    store.save(classroom, seat, {**old, 'messages': [*messages, {
                        'id': str(uuid.uuid4()), 'role': 'model', 'text': text,
                        'request_id': request_id, **metadata}]})
                else:
                    continue  # Keep unresolved data; do not discard an unsaved answer.
                with self._guard:
                    self._answers.pop(cache_key, None)
            except (OSError, ValueError):
                continue
            finally:
                student_lock.release()

    def operate(self, group, classroom, seat, data, action):
        with self.lock(group, classroom, seat):
            store = self.stores[group]
            old = store.read(classroom, seat)
            if data.get('article_id') != f'chat-{group.lower()}-v1':
                raise InvalidWork('學習版本不正確。')
            if action != 'message':
                # The browser may edit highlights/inference, never conversation history.
                clean = {k: data.get(k) for k in ('article_id', 'revision', 'stance', 'stage', 'highlights', 'inference')}
                clean['messages'] = old.get('messages', []) if old else []
                return store.save(classroom, seat, clean, action == 'submit')
            if not old or old.get('submitted_at') or old['stage'] != 'reading':
                raise Conflict('請先選擇立場並進入對話頁面。')
            request_id, text = data.get('request_id'), data.get('text')
            if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9-]{36}', request_id):
                raise InvalidWork('對話請求格式不正確。')
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
                raise InvalidWork('問題請輸入 1 至 1,000 字。')
            text = text.strip()
            messages = old['messages']
            previous = next((m for m in messages if m.get('request_id') == request_id and m['role'] == 'user'), None)
            if previous and previous['text'] != text:
                raise Conflict('重試時請保留原本問題。')
            if previous and any(m.get('request_id') == request_id and m['role'] == 'model' for m in messages):
                return old
            if not previous:
                if type(data.get('revision')) is not int or data['revision'] != old['revision']:
                    raise Conflict('對話進度已更新，請重新整理後再試。')
                if messages and messages[-1]['role'] == 'user':
                    raise Conflict('上一個問題仍在等待回覆，請先重試。')
                if len(messages) >= 40 or sum(len(m['text']) for m in messages) + len(text) > 8000:
                    raise InvalidWork('本次對話已達上限，請完成摘要與推論。')
                messages = [*messages, {'id': str(uuid.uuid4()), 'role': 'user', 'text': text, 'request_id': request_id}]
                old = store.save(classroom, seat, {**old, 'messages': messages})
            cache_key = (group, classroom, seat, request_id)
            if cache_key not in self._answers:
                generated = self.ai.reply(group, old['stance'], old['messages'])
                with self._guard:
                    self._answers[cache_key] = generated
            answer, metadata = self._answers[cache_key]
            result = store.save(classroom, seat, {**old, 'messages': [*old['messages'], {
                'id': str(uuid.uuid4()), 'role': 'model', 'text': answer, 'request_id': request_id, **metadata}]})
            with self._guard:
                self._answers.pop(cache_key, None)
            return result
