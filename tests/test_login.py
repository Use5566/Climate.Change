import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.roster import Roster, RosterUnavailable

HEADER = ['班級', '座號', '密碼', '學習介面']
REQUEST_HEADERS = {'X-Learning-Client': '1'}


def client_for(rows):
    return TestClient(create_app(Roster(lambda: rows, ttl=0), secure_cookie=False))


def login(client, password='01234', **extra):
    return client.post('/api/login', json={'classroom': '601', 'seat': '01', 'password': password, **extra}, headers=REQUEST_HEADERS)


def test_login_session_logout_and_no_secret():
    client = client_for([HEADER, ['601', '01', '01234', '']])
    assert client.get('/api/session').status_code == 401
    response = login(client)
    assert response.status_code == 200
    assert response.json()['student'] == {'classroom': '601', 'seat': '01', 'interface': None}
    assert '01234' not in response.text and 'digest' not in response.text
    assert 'HttpOnly' in response.headers['set-cookie']
    assert client.get('/api/session').status_code == 200
    assert client.post('/api/logout', headers=REQUEST_HEADERS).status_code == 200
    assert client.get('/api/session').status_code == 401


@pytest.mark.parametrize('interface', ['A', 'B', 'C'])
def test_server_owns_group(interface):
    client = client_for([HEADER, [601, 1, '01234', interface]])
    assert login(client, interface='Z').json()['student']['interface'] == interface


def test_wrong_and_missing_student_indistinguishable():
    client = client_for([HEADER, ['601', '01', '01234', '']])
    wrong = login(client, '99999')
    absent = login(client, seat='02')
    assert wrong.status_code == absent.status_code == 401
    assert wrong.json() == absent.json()


@pytest.mark.parametrize('password', ['1234', '123456', '１２３４５', 'abcd5', 12345])
def test_invalid_password_is_not_echoed(password):
    client = client_for([HEADER, ['601', '01', '01234', '']])
    response = login(client, password)
    assert response.status_code == 400
    assert 'password' not in response.text


def test_sheet_error_and_missing_credentials_fail_closed():
    def failure():
        raise RosterUnavailable('MISSING_CREDENTIALS')
    client = TestClient(create_app(Roster(failure), secure_cookie=False))
    assert login(client).status_code == 503
    assert client.get('/api/session').status_code == 401


def test_duplicate_rows_fail_closed():
    client = client_for([HEADER, ['601','01','01234',''], ['601','1','54321','A']])
    assert login(client).status_code == 503


def test_interface_header_matches_live_sheet():
    client = client_for([['班級','座號','密碼','介面'], ['601','01','01234','']])
    assert login(client).status_code == 200


def test_password_change_revokes_session_and_group_refreshes():
    rows = [HEADER, ['601','01','01234','A']]
    client = client_for(rows)
    assert login(client).status_code == 200
    rows[1][3] = 'B'
    assert client.get('/api/session').json()['student']['interface'] == 'B'
    rows[1][2] = '56789'
    assert client.get('/api/session').status_code == 401


def test_rate_limit_and_private_files():
    client = client_for([HEADER, ['601','01','01234','']])
    for _ in range(8):
        assert login(client, '99999').status_code == 401
    assert login(client, '99999').status_code == 429
    for path in ['/基本密碼表.xlsx','/README.md','/.env','/backend/roster.py','/work/','/api/roster','/A']:
        assert client.get(path).status_code == 404


def test_cross_origin_form_and_tampered_cookie():
    client = client_for([HEADER, ['601','01','01234','']])
    assert client.post('/api/login', json={}).status_code == 403
    client.cookies.set('student_session', 'forged')
    assert client.get('/api/session').status_code == 401
