"""C reading material, loaded once per process from an editable UTF-8 file."""
import copy
import hashlib
import json
from pathlib import Path
import re

ARTICLE_PATH = Path(__file__).resolve().parent.parent / 'prompts' / 'interface_c.txt'
TITLE = '閱讀文章'


def load_article(path):
    try:
        if path.stat().st_size > 120_000:
            raise ValueError('too large')
        source = path.read_text(encoding='utf-8-sig').strip()
        if not source:
            raise ValueError('empty article')
        paragraphs = []
        for block in re.split(r'\n[ \t]*\n+', source):
            parts = block.strip().split('**')
            paragraphs.append({'text': ''.join(parts), 'parts': [
                {'text': text, 'bold': bool(i % 2)} for i, text in enumerate(parts)]})
        # Leave room in Sheets recovery notes for inference and highlights.
        if sum(len(p['text'].encode('utf-16-le')) // 2 for p in paragraphs) > 20000:
            raise ValueError('article exceeds recovery capacity')
        digest = hashlib.sha256(json.dumps(paragraphs, ensure_ascii=False).encode()).hexdigest()
        return {'id': 'c-reading-' + digest, 'title': TITLE, 'paragraphs': paragraphs}
    except (OSError, UnicodeError, ValueError):
        raise OSError('C 文章載入失敗，請檢查 prompts/interface_c.txt：須為非空白 UTF-8 文字，內容不超過 20,000 字元。') from None


try:
    _ARTICLE = load_article(ARTICLE_PATH)
except OSError:
    _ARTICLE = None  # A/B and login remain available; C reports a configuration error.
ARTICLE_ID = _ARTICLE['id'] if _ARTICLE else 'c-reading-unavailable'


def article_data():
    if _ARTICLE is None:
        raise OSError('C 文章尚未設定完成，請檢查 prompts/interface_c.txt 後重新啟動。')
    return copy.deepcopy(_ARTICLE)


def article_for_work(work=None):
    if not work:
        return article_data()
    snapshot = work.get('article_snapshot')
    version = work.get('article_id', '')
    if (not isinstance(version, str) or not re.fullmatch(r'c-reading-(?:v1|[0-9a-f]{64})', version)
            or not isinstance(snapshot, list) or not snapshot
            or any(not isinstance(text, str) or not text for text in snapshot)):
        raise OSError('C 學習紀錄的文章備份不完整，請通知老師。')
    if _ARTICLE:
        if snapshot == [p['text'] for p in _ARTICLE['paragraphs']]:
            return {**article_data(), 'id': version}
        if version == _ARTICLE['id']:
            raise OSError('C 文章版本與備份不符。')
    # Keep existing students on their original text so highlight offsets survive edits.
    return {'id': version, 'title': TITLE, 'paragraphs': [
        {'text': text, 'parts': [{'text': text, 'bold': False}]} for text in snapshot]}
