import uuid
import pytest
from backend.buffered_learning import conversation_tokens
from backend.chat import ChatService, validate_chat
from backend.learning import Conflict, InvalidWork
from tests.test_buffered_learning import GoogleFake, store_for
from tests.test_learning import draft

class AI:
    def reply(self, group, stance, messages):
        return '測試回覆', {'token_usage': {'totalTokenCount': 12}}

@pytest.mark.parametrize('group', ['A','B','C'])
def test_reopen_edit_resubmit_same_row_and_restore(group):
    google=GoogleFake();store=store_for(google,group=group)
    if group!='C': store.validator=validate_chat
    data=draft(stage='summary',inference='首次提交')
    if group!='C': data.update(article_id=f'chat-{group.lower()}-v1',messages=[],highlights=[])
    first=store.save('601','01',data,True)
    assert store.read('601','01')==first
    # Navigation alone preserves submitted status, also on legacy metadata.
    navigation=store.save('601','01',{**first,'stage':'reading'})
    assert navigation['submitted_at']==first['submitted_at']
    changed=store.save('601','01',{**navigation,'stage':'summary','inference':'修改內容'})
    assert changed['submitted_at'] is None
    assert changed['last_submitted_at']==first['submitted_at']
    store.coordinator.flush()
    assert google.rows[group][1][5]['effectiveValue']['stringValue']=='修改中（待重新提交）'
    restored=store_for(google,group=group)
    if group!='C': restored.validator=validate_chat
    assert restored.read('601','01')==changed
    final=restored.save('601','01',changed,True)
    assert final['submitted_at'] and final['submitted_at']>=first['submitted_at'] and final['revision']>first['revision']
    assert final['attempt_id']==first['attempt_id']
    assert len(google.rows[group])==1
    assert google.rows[group][1][5]['effectiveValue']['stringValue']=='已提交'
    assert restored.save('601','01',changed,True)==final
    with pytest.raises(Conflict):restored.save('601','01',{**first,'inference':'過期頁面修改'})

@pytest.mark.parametrize('group',['A','B'])
def test_chat_turns_tokens_and_context_survive_resubmission(group):
    google=GoogleFake();store=store_for(google,group=group);store.validator=validate_chat
    service=ChatService({group:store},AI())
    data=draft(article_id=f'chat-{group.lower()}-v1',highlights=[],messages=[])
    work=store.save('601','01',data)
    for turn in range(10):
        work=service.operate(group,'601','01',{**work,'request_id':str(uuid.uuid4()),'text':f'問題{turn}'},'message')
        if turn==0:
            work=store.save('601','01',{**work,'stage':'summary','inference':'初稿'},True)
            assert conversation_tokens(work)==12
            work=store.save('601','01',{**work,'stage':'reading'})
            assert work['submitted_at']
    assert conversation_tokens(work)==120
    assert len(work['messages'])==20
    work=store.save('601','01',{**work,'stage':'summary','inference':'完成'},True)
    work=store.save('601','01',{**work,'stage':'reading'})
    with pytest.raises(InvalidWork,match='10'):
        service.operate(group,'601','01',{**work,'request_id':str(uuid.uuid4()),'text':'第十一題'},'message')
    assert conversation_tokens(store.read('601','01'))==120
    assert len(google.rows[group])==1
import copy,json
import pytest
from tests.test_buffered_learning import GoogleFake,store_for
from tests.test_learning import draft
from backend.chat import validate_chat

@pytest.mark.parametrize('group',['A','B','C'])
def test_existing_603_submission_preserved_on_reload(group):
    google=GoogleFake();store=store_for(google,group=group)
    if group!='C':store.validator=validate_chat
    data=draft(stage='summary',inference='既有成果相容性測試（模擬資料）')
    if group!='C':data.update(article_id=f'chat-{group.lower()}-v1',highlights=[],messages=[])
    store.save('603','01',data,True)
    # Simulate an existing version's recovery note without the new optional field.
    cells=google.rows[group][1]
    envelope=json.loads(cells[8]['note']);envelope['work'].pop('last_submitted_at',None)
    cells[8]['note']=json.dumps(envelope,ensure_ascii=False,separators=(',',':'))
    before=copy.deepcopy(google.rows[group])
    restored=store_for(google,group=group)
    if group!='C':restored.validator=validate_chat
    assert restored.read('603','01')==envelope['work']
    assert google.rows[group]==before
    assert restored.read('603','01')['submitted_at']
