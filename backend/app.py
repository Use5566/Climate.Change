"""Single-worker student login and C learning workflow."""
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .roster import GoogleSheetLoader, Roster, RosterUnavailable
from .article import article_data
from .learning import LearningStore, InvalidWork, Conflict
from .sheet_learning import SheetLearningStore
from .chat import ChatService, ChatUnavailable
from .chat import validate_chat
from .buffered_learning import BufferedLearningStore, SyncCoordinator
import sqlite3

DIST = Path(__file__).resolve().parent.parent / 'dist'
COOKIE = 'student_session'
INVALID = '班級、座號或密碼不正確。'


def create_app(roster=None, secure_cookie=None, learning_store=None, chat_service=None):
    coordinator = SyncCoordinator()
    @asynccontextmanager
    async def lifespan(app):
        coordinator.start()
        yield
        await run_in_threadpool(coordinator.stop)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    roster = roster or Roster(GoogleSheetLoader())
    secure_cookie = os.getenv('COOKIE_SECURE', 'true').lower() != 'false' if secure_cookie is None else secure_cookie
    sessions = {}
    attempts = {}
    lock = threading.Lock()
    learning_store = learning_store or (LearningStore() if os.getenv('LEARNING_STORAGE') == 'sqlite' else BufferedLearningStore(coordinator))
    chat_service = chat_service or ChatService(stores={g: BufferedLearningStore(
        coordinator, interface=g, validator=validate_chat) for g in ('A', 'B')})

    def limited(keys):
        now = time.monotonic()
        with lock:
            for key in list(attempts):
                if attempts[key][1] <= now:
                    del attempts[key]
            for key, maximum in keys:
                if attempts.get(key, (0, 0))[0] >= maximum:
                    return True
            for key, maximum in keys:
                count, until = attempts.get(key, (0, now + 300))
                attempts[key] = (count + 1, until)
        return False

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def home():
        return FileResponse(DIST / 'index.html')

    @app.get('/app.js')
    def javascript():
        return FileResponse(DIST / 'app.js', media_type='text/javascript')

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}  # Liveness only; not evidence that Sheets is connected.

    @app.post('/api/login')
    async def login(request: Request):
        # JSON + custom header + no CORS prevent cross-origin form login/CSRF.
        if request.headers.get('x-learning-client') != '1' or request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'message': '請從登入頁面操作。'}, status_code=403)
        ip = request.client.host if request.client else 'unknown'
        if limited([('global', 3000), (('ip', ip), 300)]):
            return JSONResponse({'message': '嘗試次數過多，請五分鐘後再試。'}, status_code=429)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                return JSONResponse({'message': INVALID}, status_code=400)
        try:
            import json
            payload = json.loads(body)
            classroom, seat, password = (payload.get(k) for k in ('classroom', 'seat', 'password'))
            if not (isinstance(classroom, str) and re.fullmatch(r'60[1-5]', classroom)
                    and isinstance(seat, str) and re.fullmatch(r'0[1-9]|[12][0-9]|3[0-2]', seat)
                    and isinstance(password, str) and re.fullmatch(r'[0-9]{5}', password)):
                return JSONResponse({'message': INVALID}, status_code=400)
        except (ValueError, AttributeError, TypeError):
            return JSONResponse({'message': INVALID}, status_code=400)
        if limited([(('student', classroom, seat), 8)]):
            return JSONResponse({'message': '嘗試次數過多，請五分鐘後再試。'}, status_code=429)
        try:
            student = await run_in_threadpool(roster.verify, classroom, seat, password)
        except RosterUnavailable as error:
            logging.getLogger('learning').warning('Roster unavailable: %s', str(error))
            return JSONResponse({'message': '暫時無法讀取密碼表，請通知老師確認後台連線。'}, status_code=503)
        if student is None:
            return JSONResponse({'message': INVALID}, status_code=401)
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with lock:
            for key in list(sessions):
                if sessions[key][1] <= now:
                    del sessions[key]
            # One active session per student for shared classroom devices.
            for key in list(sessions):
                if sessions[key][0].classroom == classroom and sessions[key][0].seat == seat:
                    del sessions[key]
            sessions.pop(request.cookies.get(COOKIE), None)
            sessions[token] = (student, now + 7200)
        response = JSONResponse({'student': student.public()})
        response.set_cookie(COOKIE, token, max_age=7200, httponly=True,
                            secure=secure_cookie, samesite='strict', path='/')
        return response

    @app.get('/api/session')
    async def session(request: Request):
        token = request.cookies.get(COOKIE)
        with lock:
            entry = sessions.get(token)
        if entry is None or entry[1] <= time.monotonic():
            return JSONResponse({'message': '請先登入。'}, status_code=401)
        previous = entry[0]
        try:
            current = await run_in_threadpool(roster.current, previous.classroom, previous.seat)
        except RosterUnavailable:
            return JSONResponse({'message': '暫時無法確認登入狀態，請稍後重試。'}, status_code=503)
        if current is None or current.digest != previous.digest:
            with lock:
                sessions.pop(token, None)
            return JSONResponse({'message': '登入已失效，請重新登入。'}, status_code=401)
        return {'student': current.public()}

    @app.post('/api/logout')
    def logout(request: Request):
        if request.headers.get('x-learning-client') != '1':
            return JSONResponse({'message': '請從登入頁面操作。'}, status_code=403)
        with lock:
            sessions.pop(request.cookies.get(COOKIE), None)
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path='/', secure=secure_cookie, httponly=True, samesite='strict')
        return response

    async def c_identity(request):
        result = await session(request)
        if isinstance(result, JSONResponse):
            return result
        if result['student']['interface'] != 'C':
            return JSONResponse({'message': '此學習介面僅供 C 組學生使用。'}, status_code=403)
        return result['student']

    @app.get('/learn/c')
    async def learning_page(request: Request):
        identity = await c_identity(request)
        if isinstance(identity, JSONResponse):
            from fastapi.responses import RedirectResponse
            return RedirectResponse('/', status_code=303)
        return FileResponse(DIST / 'learning-c.html')

    @app.get('/learning-c.js')
    def learning_script():
        return FileResponse(DIST / 'learning-c.js', media_type='text/javascript')

    @app.get('/highlights.js')
    def highlight_script():
        return FileResponse(DIST / 'highlights.js', media_type='text/javascript')

    @app.get('/learning-c.css')
    def learning_style():
        return FileResponse(DIST / 'learning-c.css', media_type='text/css')

    @app.get('/api/c/work')
    async def get_work(request: Request):
        identity = await c_identity(request)
        if isinstance(identity, JSONResponse):
            return identity
        try:
            draft = await run_in_threadpool(learning_store.read, identity['classroom'], identity['seat'])
        except (OSError, sqlite3.Error):
            return JSONResponse({'message': '學習紀錄目前無法讀取，請通知老師檢查試算表授權與連線。'}, status_code=503)
        return {'student': identity, 'article': article_data(), 'work': draft}

    async def write_work(request, submit_work):
        identity = await c_identity(request)
        if isinstance(identity, JSONResponse):
            return identity
        if request.headers.get('x-learning-client') != '1' or request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'message': '請從學習頁面操作。'}, status_code=403)
        import json
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128000:
                return JSONResponse({'message': '內容過長，請縮短後再試。'}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or data.get('classroom') != identity['classroom'] or data.get('seat') != identity['seat']:
                return JSONResponse({'message': '登入學生已變更，請保留文字後重新登入。'}, status_code=409)
            saved = await run_in_threadpool(learning_store.save, identity['classroom'], identity['seat'], data, submit_work)
        except (ValueError, TypeError) as error:
            if isinstance(error, Conflict):
                return JSONResponse({'message': str(error)}, status_code=409)
            message = str(error) if isinstance(error, InvalidWork) else '學習資料格式不正確。'
            return JSONResponse({'message': message}, status_code=400)
        except (OSError, sqlite3.Error):
            return JSONResponse({'message': '儲存未成功，請保留此頁並稍後重試。'}, status_code=503)
        return {'work': saved}

    @app.post('/api/c/draft')
    async def save_draft(request: Request):
        return await write_work(request, False)

    @app.post('/api/c/submit')
    async def submit_work(request: Request):
        return await write_work(request, True)

    async def ab_identity(request, group):
        result = await session(request)
        if isinstance(result, JSONResponse):
            return result
        if group not in ('a', 'b') or result['student']['interface'] != group.upper():
            return JSONResponse({'message': '請使用老師指定的學習介面。'}, status_code=403)
        return result['student']

    @app.get('/learn/{group}')
    async def chat_page(request: Request, group: str):
        identity = await ab_identity(request, group)
        if isinstance(identity, JSONResponse):
            from fastapi.responses import RedirectResponse
            return RedirectResponse('/', status_code=303)
        return FileResponse(DIST / 'learning-ab.html')

    @app.get('/learning-ab.js')
    def chat_script():
        return FileResponse(DIST / 'learning-ab.js', media_type='text/javascript')

    @app.get('/learning-ab.css')
    def chat_style():
        return FileResponse(DIST / 'learning-ab.css', media_type='text/css')

    @app.get('/api/{group}/work')
    async def chat_work(request: Request, group: str):
        identity = await ab_identity(request, group)
        if isinstance(identity, JSONResponse):
            return identity
        try:
            work = await run_in_threadpool(chat_service.read, group.upper(), identity['classroom'], identity['seat'])
            return {'student': identity, 'work': work, 'article': {'id': f'chat-{group}-v1', 'paragraphs': []}}
        except OSError:
            return JSONResponse({'message': '無法讀取學習紀錄，請老師檢查試算表授權與連線。'}, status_code=503)

    @app.get('/api/{group}/sync')
    async def sync_status(request: Request, group: str):
        identity = await c_identity(request) if group == 'c' else await ab_identity(request, group)
        if isinstance(identity, JSONResponse):
            return identity
        store = learning_store if group == 'c' else chat_service.stores[group.upper()]
        if hasattr(store, 'sync_status'):
            return await run_in_threadpool(store.sync_status, identity['classroom'], identity['seat'])
        return {'pending': False, 'failed': False, 'saved_at': None}

    @app.post('/api/{group}/{action}')
    async def chat_action(request: Request, group: str, action: str):
        identity = await ab_identity(request, group)
        if isinstance(identity, JSONResponse):
            return identity
        if action not in ('draft', 'submit', 'message'):
            return JSONResponse({'message': '無此操作。'}, status_code=404)
        if request.headers.get('x-learning-client') != '1' or request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'message': '請從學習頁面操作。'}, status_code=403)
        import json
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128000:
                return JSONResponse({'message': '內容過長。'}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            if data.get('classroom') != identity['classroom'] or data.get('seat') != identity['seat']:
                raise Conflict('登入學生已變更，請保留文字後重新登入。')
            if action == 'message' and limited([(('chat', group, identity['classroom'], identity['seat']), 30)]):
                return JSONResponse({'message': '提問過於頻繁，請稍後再試。'}, status_code=429)
            work = await run_in_threadpool(chat_service.operate, group.upper(), identity['classroom'], identity['seat'], data, action)
            return {'work': work}
        except Conflict as e:
            return JSONResponse({'message': str(e)}, status_code=409)
        except (ValueError, TypeError) as e:
            return JSONResponse({'message': str(e) if isinstance(e, InvalidWork) else '資料格式不正確。'}, status_code=400)
        except ChatUnavailable as e:
            return JSONResponse({'message': str(e)}, status_code=503)
        except OSError:
            return JSONResponse({'message': '紀錄尚未確認儲存，請保留此頁並重試。'}, status_code=503)

    return app


app = create_app()
