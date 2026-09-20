"""Private, read-only Google Sheets roster. Never expose sheet values to clients."""
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

SPREADSHEET_ID = '1k53Yg6bu8__mEY0leZ2_da5TyEru2i3v9cwkmsnpO-A'
SERVICE_ACCOUNT = 'sci-video@sci-video-495014.iam.gserviceaccount.com'


class RosterUnavailable(Exception):
    """Safe operational error code, without upstream response or credentials."""


@dataclass(frozen=True)
class Student:
    classroom: str
    seat: str
    interface: str
    digest: bytes = field(repr=False)

    def public(self):
        return {'classroom': self.classroom, 'seat': self.seat,
                'interface': self.interface or None}


class Roster:
    def __init__(self, loader, ttl=30):
        self.loader = loader
        self.ttl = ttl
        self._pepper = secrets.token_bytes(32)
        self._students = {}
        self._expires = 0
        self._lock = threading.Lock()

    def _digest(self, classroom, seat, password):
        # Key exists only in process memory; this is not a persistent password DB.
        return hmac.digest(self._pepper, f'{classroom}:{seat}:{password}'.encode(), 'sha256')

    def _parse(self, rows):
        headers = [str(x).strip().replace('级', '級') for x in rows[0]] if rows else []
        offset = 1 if len(headers) in (5, 11) else 0
        if (len(headers) not in (4, 5, 11) or headers[offset:offset+3] != ['班級', '座號', '密碼']
                or headers[offset+3] not in ('介面', '學習介面')):
            raise RosterUnavailable('INVALID_HEADERS')
        result = {}
        for index, raw in enumerate(rows[1:], 2):
            if not any(str(x).strip() for x in raw):
                continue
            row = list(raw)[offset:offset+4]
            row += [''] * (4 - len(row))
            classroom, seat, password, interface = map(str, row[:4])
            classroom, seat, interface = classroom.strip(), seat.strip(), interface.strip()
            if not re.fullmatch(r'60[1-5]', classroom) or not re.fullmatch(r'0?[1-9]|[12][0-9]|3[0-2]', seat):
                raise RosterUnavailable(f'INVALID_ID_ROW_{index}')
            seat = seat.zfill(2)
            if not re.fullmatch(r'[0-9]{5}', password) or interface not in ('', 'A', 'B', 'C'):
                raise RosterUnavailable(f'INVALID_VALUE_ROW_{index}')
            key = (classroom, seat)
            if key in result:
                raise RosterUnavailable(f'DUPLICATE_ID_ROW_{index}')
            result[key] = Student(classroom, seat, interface, self._digest(classroom, seat, password))
        if not result:
            raise RosterUnavailable('EMPTY_ROSTER')
        return result

    def _refresh(self):
        if time.monotonic() >= self._expires:
            # Fail closed: never authenticate against an expired cache on API failure.
            self._students = self._parse(self.loader())
            self._expires = time.monotonic() + self.ttl

    def verify(self, classroom, seat, password):
        with self._lock:
            self._refresh()
            student = self._students.get((classroom, seat))
            expected = student.digest if student else b'\0' * 32
            valid = hmac.compare_digest(expected, self._digest(classroom, seat, password))
            return student if valid else None

    def current(self, classroom, seat):
        with self._lock:
            self._refresh()
            return self._students.get((classroom, seat))


class GoogleSheetLoader:
    def __init__(self):
        self._session = None

    def __call__(self):
        try:
            if self._session is None:
                from google.oauth2.service_account import Credentials
                from google.auth.transport.requests import AuthorizedSession
                raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
                path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
                if raw:
                    info = json.loads(raw)
                elif path:
                    with open(path, encoding='utf-8-sig') as stream:
                        info = json.load(stream)
                else:
                    raise RosterUnavailable('MISSING_CREDENTIALS')
                if info.get('client_email') != SERVICE_ACCOUNT or info.get('type') != 'service_account':
                    raise RosterUnavailable('WRONG_SERVICE_ACCOUNT')
                credentials = Credentials.from_service_account_info(
                    info, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
                self._session = AuthorizedSession(credentials, refresh_timeout=15)
            base = f'https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}'
            response = self._session.get(base, params={'fields': 'sheets.properties(sheetId,title)'}, timeout=15)
            response.raise_for_status()
            tabs = response.json().get('sheets', [])
            title = next((x['properties']['title'] for x in tabs if x['properties']['sheetId'] == 0), None)
            if title is None:
                raise RosterUnavailable('MISSING_SHEET_GID_0')
            address = "'" + title.replace("'", "''") + "'!A:K"
            response = self._session.get(base + '/values/' + quote(address, safe=''),
                                         params={'valueRenderOption': 'FORMATTED_VALUE'}, timeout=15)
            response.raise_for_status()
            return response.json().get('values', [])
        except RosterUnavailable:
            raise
        except Exception:
            # Deliberately do not log exception text: upstream errors can contain secrets.
            raise RosterUnavailable('GOOGLE_READ_FAILED') from None
