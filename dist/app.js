const form = document.querySelector('#identity-form');
const classroom = document.querySelector('#classroom');
const seat = document.querySelector('#seat');
const password = document.querySelector('#student-password');
const status = document.querySelector('#status');
const submit = form.querySelector('button');
const authenticated = document.querySelector('#authenticated');
const logout = document.querySelector('#logout');
const numerals = ['一', '二', '三', '四', '五'];
numerals.forEach((number, index) => classroom.add(new Option(`六年${number}班（${601 + index}）`, String(601 + index))));
for (let number = 1; number <= 32; number++) {
  const value = String(number).padStart(2, '0');
  seat.add(new Option(`${value} 號`, value));
}
function message(text) {
  status.textContent = text;
  status.hidden = false;
}
function showStudent(student) {
  form.hidden = true;
  authenticated.hidden = false;
  password.value = '';
  document.querySelector('#student-info').textContent = `${student.classroom} 班・${student.seat} 號`;
  document.querySelector('#assignment-info').textContent = student.interface
    ? `你被分配到 ${student.interface} 學習介面，教學內容尚未開放。`
    : '尚未分配學習介面，請等候老師設定。';
  logout.focus();
}
async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Learning-Client': '1' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
    });
    return { response, data: await response.json() };
  } finally { clearTimeout(timeout); }
}
form.addEventListener('input', () => { status.hidden = true; });
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (location.protocol === 'file:') {
    message('請從 Python 網站網址開啟此頁，才能連接後台核對。');
    return;
  }
  submit.disabled = true;
  submit.textContent = '正在確認身分…';
  form.setAttribute('aria-busy', 'true');
  try {
    const { response, data } = await api('/api/login', {
      classroom: classroom.value, seat: seat.value, password: password.value,
    });
    if (response.ok) showStudent(data.student);
    else message(data.message || '無法登入，請稍後重試。');
  } catch { message('連線未完成，請確認網路後再試一次。'); }
  finally {
    password.value = '';
    submit.disabled = false;
    submit.textContent = '確認身分，進入學習';
    form.removeAttribute('aria-busy');
  }
});
logout.addEventListener('click', async () => {
  logout.disabled = true;
  try {
    const { response } = await api('/api/logout', {});
    if (!response.ok) throw new Error('logout');
    authenticated.hidden = true;
    form.hidden = false;
    form.reset();
    status.hidden = true;
    classroom.focus();
  } catch {
    document.querySelector('#assignment-info').textContent = '登出未完成，請確認網路後再試。';
  } finally { logout.disabled = false; }
});
if (location.protocol !== 'file:') {
  submit.disabled = true;
  api('/api/session').then(({response, data}) => {
    if (response.ok) showStudent(data.student);
    else if (response.status !== 401) message(data.message || '暫時無法確認登入狀態。');
  }).catch(() => message('無法連接後台，請確認網站已啟動。'))
    .finally(() => { submit.disabled = false; });
}
