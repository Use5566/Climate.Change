import copy
import pytest
from backend.sheet_learning import SheetLearningStore, HEADERS
from backend.learning import Conflict
from tests.test_learning import draft


class MemorySheet(SheetLearningStore):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.fail_after_append = False
        self._ready = True
        self._sheet_id = 12

    def request(self, method, suffix='', **kwargs):
        if method == 'GET':
            return {'sheets': [{'data': [{'rowData': [{'values': copy.deepcopy(row)} for row in self.rows]}]}]}
        assert method == 'POST' and suffix == ':batchUpdate'
        request = kwargs['json']['requests'][0]['appendCells']
        assert request['sheetId'] == 12
        for row in request['rows']:
            self.rows.append([{'effectiveValue': cell['userEnteredValue'],
                               **({'note': cell['note']} if 'note' in cell else {})} for cell in row['values']])
        if self.fail_after_append:
            self.fail_after_append = False
            raise OSError('Lost response')
        return {}


def test_history_restore_and_student_isolation():
    store = MemorySheet()
    first = store.save('601', '01', draft())
    second = store.save('601', '01', draft(revision=1, inference='=1+1', stage='summary'))
    assert len(store.rows) == 2
    values = [cell['effectiveValue']['stringValue'] for cell in store.rows[1]]
    assert len(values) == 11
    assert values[1:] == ['601', '01', '', 'C', '', '支持', '草稿', second['highlight_texts'][0], '', '=1+1']
    restored = MemorySheet()
    restored.rows = copy.deepcopy(store.rows)
    assert restored.read('601', '01') == second
    assert restored.read('601', '02') is None
    assert first['attempt_id'] == second['attempt_id']
    assert HEADERS == ['紀錄時間', '班級', '座號', '密碼', '介面', '成績統計', '立場',
                       '提交狀態', '劃記原文', '學生提問及AI回答內容', '推論']


def test_lost_response_retry_does_not_duplicate():
    store = MemorySheet()
    store.fail_after_append = True
    with pytest.raises(OSError):
        store.save('601', '01', draft())
    assert store.save('601', '01', draft())['revision'] == 1
    assert len(store.rows) == 1
    data = draft(revision=1, stage='summary', inference='我的推論')
    store.fail_after_append = True
    with pytest.raises(OSError):
        store.save('601', '01', data, True)
    assert store.save('601', '01', data, True)['submitted_at']
    assert len(store.rows) == 2
    assert store.rows[1][7]['effectiveValue']['stringValue'] == '已提交'
    with pytest.raises(Conflict):
        store.save('601', '01', draft(revision=2))


def test_never_overwrite_roster_tab():
    store = SheetLearningStore()
    calls = []
    def request(method, suffix='', **kwargs):
        calls.append(method)
        return {'sheets': [{'properties': {'sheetId': 0, 'title': 'C學習歷程'}}]}
    store.request = request
    with pytest.raises(OSError):
        store.ensure()
    assert calls == ['GET']


def test_unexpected_headers_are_preserved():
    store = SheetLearningStore()
    calls = []
    def request(method, suffix='', **kwargs):
        calls.append(method)
        if not suffix:
            return {'sheets': [{'properties': {'sheetId': 12, 'title': 'C學習歷程'}}]}
        return {'values': [['教師既有資料']]}
    store.request = request
    with pytest.raises(OSError):
        store.ensure()
    assert calls == ['GET', 'GET']
