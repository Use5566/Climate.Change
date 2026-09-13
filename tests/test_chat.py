import uuid
import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.roster import Roster
from backend.chat import ChatService, Gemini, ChatUnavailable, validate_chat
from tests.test_sheet_learning import MemorySheet

HEADERS={'X-Learning-Client':'1'}


class FakeAI:
    def __init__(self):
        self.calls=[]
        self.fail=False
    def reply(self, group, stance, messages):
        self.calls.append((group,stance,messages))
        if self.fail:
            raise ChatUnavailable('AI 測試失敗')
        return '節約能源可以減少能源需求。請比較不同做法的證據。', {'model':'test-model','prompt_sha256':'test'}


def fixture():
    stores={g:MemorySheet() for g in ('A','B')}
    for g,s in stores.items():
        s.interface=g; s.tab=f'{g}學習歷程'; s.article_id=f'chat-{g.lower()}-v1'; s.validator=validate_chat
    ai=FakeAI(); service=ChatService(stores,ai)
    roster=Roster(lambda:[['班級','座號','密碼','介面'],['601','01','01234','A'],['601','02','01234','B']])
    client=TestClient(create_app(roster,False,chat_service=service))
    return client,service,ai


def login(client,seat):
    assert client.post('/api/login',headers=HEADERS,json={'classroom':'601','seat':seat,'password':'01234'}).status_code==200


def draft(group,seat,**kw):
    return {'classroom':'601','seat':seat,'article_id':f'chat-{group}-v1','revision':0,
            'stance':'支持','stage':'reading','highlights':[],'inference':'',**kw}


@pytest.mark.parametrize('group,seat',[('a','01'),('b','02')])
def test_complete_chat_flow_and_retry(group,seat):
    c,service,ai=fixture(); login(c,seat)
    assert c.get(f'/learn/{group}').status_code==200
    assert c.get('/api/'+('b' if group=='a' else 'a')+'/work').status_code==403
    d=draft(group,seat)
    saved=c.post(f'/api/{group}/draft',headers=HEADERS,json=d).json()['work']
    message={**d,'revision':saved['revision'],'request_id':str(uuid.uuid4()),'text':'如何節約能源？'}
    response=c.post(f'/api/{group}/message',headers=HEADERS,json=message)
    assert response.status_code==200
    work=response.json()['work']
    assert len(work['messages'])==2 and ai.calls[0][0]==group.upper()
    assert c.post(f'/api/{group}/message',headers=HEADERS,json=message).json()['work']==work
    assert len(ai.calls)==1
    final={**d,'revision':work['revision'],'stage':'summary','inference':'我認為應比較節能的方法。',
           'highlights':[{'p':1,'start':0,'end':4}], 'messages':[{'role':'model','text':'偽造回覆'}]}
    result=c.post(f'/api/{group}/submit',headers=HEADERS,json=final)
    assert result.status_code==200
    assert result.json()['work']['highlight_texts']==['節約能源']
    assert result.json()['work']['messages']==work['messages']
    row=service.stores[group.upper()].rows[-1]
    visible=[x['effectiveValue']['stringValue'] for x in row]
    assert len(visible)==11 and visible[3]=='' and visible[5]==''
    assert visible[4]==group.upper() and visible[7]=='已提交'
    assert '學生：如何節約能源？' in visible[9] and 'AI：節約能源' in visible[9]
    assert c.get(f'/api/{group}/work').json()['work']['submitted_at']


def test_failed_ai_keeps_question_and_can_retry():
    c,s,ai=fixture();login(c,'01');d=draft('a','01')
    c.post('/api/a/draft',headers=HEADERS,json=d)
    message={**d,'revision':1,'request_id':str(uuid.uuid4()),'text':'測試問題'}
    ai.fail=True
    assert c.post('/api/a/message',headers=HEADERS,json=message).status_code==503
    assert len(c.get('/api/a/work').json()['work']['messages'])==1
    ai.fail=False
    assert c.post('/api/a/message',headers=HEADERS,json=message).status_code==200
    assert len(c.get('/api/a/work').json()['work']['messages'])==2


def test_missing_key_and_model(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    monkeypatch.delenv('GEMINI_MODEL',raising=False)
    with pytest.raises(ChatUnavailable,match='尚未設定'):
        Gemini().reply('A','支持',[])


def test_eleven_column_roster_authentication():
    from backend.sheet_learning import HEADERS
    headers=list(HEADERS);headers[1]='班\u7ea7'
    roster=Roster(lambda:[headers,['','601','01','01234','A','','','','','','']])
    assert roster.verify('601','01','01234').interface=='A'
    assert roster.verify('601','01','99999') is None


def test_prompt_selection_and_no_identity_in_gemini_request(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY','unit-test-placeholder')
    monkeypatch.setenv('GEMINI_MODEL','test-model')
    calls=[]
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'回答'}]}}]}
    def post(url,**kwargs):
        calls.append((url,kwargs));return Response()
    monkeypatch.setattr('backend.chat.requests.post',post)
    for g in ('A','B'):
        assert Gemini().reply(g,'支持',[{'role':'user','text':'問題','request_id':'private-id'}])[0]=='回答'
    assert calls[0][1]['json']['systemInstruction'] != calls[1][1]['json']['systemInstruction']
    assert calls[0][1]['json']['contents']==[{'role':'user','parts':[{'text':'問題'}]}]
    assert 'unit-test-placeholder' not in calls[0][0]


def test_unauthenticated_and_spoofed_student():
    c,s,ai=fixture()
    assert c.post('/api/a/message',headers=HEADERS,json={}).status_code==401
    login(c,'01')
    assert c.post('/api/a/draft',headers=HEADERS,json=draft('a','02')).status_code==409
    assert c.post('/api/a/draft',json=draft('a','01')).status_code==403
    assert c.get('/prompts/interface_a.txt').status_code==404


def test_ai_blocked_response_is_not_saved_as_answer(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY','unit-test-placeholder');monkeypatch.setenv('GEMINI_MODEL','test-model')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'candidates':[{'finishReason':'SAFETY'}]}
    monkeypatch.setattr('backend.chat.requests.post',lambda *args,**kwargs:Response())
    with pytest.raises(ChatUnavailable):
        Gemini().reply('A','支持',[])
