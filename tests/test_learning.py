import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.roster import Roster
from backend.learning import LearningStore
from backend.article import ARTICLE_ID, article_data

HEADERS = {'X-Learning-Client':'1'}
ROWS = [['班級','座號','密碼','介面'],['601','01','01234','C'],['601','02','01234','C'],['601','03','01234','A']]


def client(db):
    return TestClient(create_app(Roster(lambda:ROWS),False,LearningStore(db)))


def login(c, seat='01'):
    assert c.post('/api/login',headers=HEADERS,json={'classroom':'601','seat':seat,'password':'01234'}).status_code==200


def draft(**updates):
    return {'classroom':'601','seat':'01','article_id':ARTICLE_ID,'revision':0,'stance':'支持',
            'stage':'reading','highlights':[{'p':0,'start':0,'end':8}], 'inference':'',**updates}


def test_permissions_and_spoofed_identity(tmp_path):
    c=client(tmp_path/'db.sqlite3')
    assert c.get('/api/c/work').status_code==401
    login(c,'03')
    assert c.get('/api/c/work').status_code==403
    assert c.post('/api/c/submit',headers=HEADERS,json=draft()).status_code==403
    login(c)
    assert c.post('/api/c/draft',headers=HEADERS,json=draft(seat='02')).status_code==409
    assert c.post('/api/c/draft',json=draft()).status_code==403


def test_durable_draft_submission_and_isolation(tmp_path):
    db=tmp_path/'db.sqlite3'; c=client(db); login(c)
    r=c.post('/api/c/draft',headers=HEADERS,json=draft())
    assert r.status_code==200
    assert r.json()['work']['highlight_texts']==[article_data()['paragraphs'][0]['text'][:8]]
    attempt=r.json()['work']['attempt_id']
    # A new app instance simulates process restart; login re-establishes its session.
    c=client(db); login(c)
    assert c.get('/api/c/work').json()['work']['attempt_id']==attempt
    payload=draft(revision=1,stage='summary',inference='這是我的推論。')
    submitted=c.post('/api/c/submit',headers=HEADERS,json=payload)
    assert submitted.status_code==200 and submitted.json()['work']['submitted_at']
    assert c.post('/api/c/submit',headers=HEADERS,json=payload).json()==submitted.json()
    assert c.post('/api/c/draft',headers=HEADERS,json=draft(revision=2)).status_code==409
    login(c,'02')
    assert c.get('/api/c/work').json()['work'] is None


@pytest.mark.parametrize('updates',[
    {'stance':'其他'},{'article_id':'tampered'},{'revision':True},
    {'highlights':[{'p':-1,'start':0,'end':1}]},
    {'highlights':[{'p':0,'start':0,'end':99999}]},
    {'highlights':[{'p':0,'start':8,'end':2}]},
])
def test_invalid_payload(tmp_path,updates):
    c=client(tmp_path/'db.sqlite3'); login(c)
    assert c.post('/api/c/draft',headers=HEADERS,json=draft(**updates)).status_code==400


def test_empty_inference_rejected_and_overlaps_merged(tmp_path):
    c=client(tmp_path/'db.sqlite3'); login(c)
    assert c.post('/api/c/submit',headers=HEADERS,json=draft(stage='summary',inference='  ')).status_code==400
    r=c.post('/api/c/draft',headers=HEADERS,json=draft(highlights=[{'p':0,'start':3,'end':10},{'p':0,'start':0,'end':5}]))
    assert r.json()['work']['highlights']==[{'p':0,'start':0,'end':10}]


def test_stale_draft_and_retry(tmp_path):
    c=client(tmp_path/'db.sqlite3'); login(c)
    r=c.post('/api/c/draft',headers=HEADERS,json=draft())
    assert c.post('/api/c/draft',headers=HEADERS,json=draft()).json()==r.json()
    assert c.post('/api/c/draft',headers=HEADERS,json=draft(inference='另一個分頁')).status_code==409


def test_render_requires_explicit_storage(monkeypatch):
    monkeypatch.setenv('RENDER','true'); monkeypatch.delenv('LEARNING_DB_PATH',raising=False)
    c=TestClient(create_app(Roster(lambda:ROWS),False))
    login(c)
    assert c.get('/api/c/work').status_code==503
