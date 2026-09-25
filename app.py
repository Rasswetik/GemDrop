import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl

import requests
from flask import Flask, abort, g, jsonify, render_template, request, session


BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', str(BASE / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'gemdrop.sqlite3'
CATALOG = DATA / 'portal_gifts.json'
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
ADMIN_IDS = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '5257227756').split(',') if x.strip().isdigit()}
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('RENDER') == 'true')


def connect():
    db = sqlite3.connect(DB, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA journal_mode=WAL')
    return db


def initialize():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
            photo_url TEXT NOT NULL DEFAULT '', balance INTEGER NOT NULL DEFAULT 1000,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            bet INTEGER NOT NULL, mines INTEGER NOT NULL, positions TEXT NOT NULL,
            opened TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'active',
            payout INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')


initialize()


def error(message, code=400):
    return jsonify(error=message), code


def verified_user(init_data):
    if not BOT_TOKEN or not init_data or len(init_data) > 10000:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    signature = pairs.pop('hash', '')
    if not signature or not pairs.get('auth_date'):
        return None
    try:
        if abs(time.time() - int(pairs['auth_date'])) > 86400:
            return None
    except ValueError:
        return None
    check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
    secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        user = json.loads(pairs['user'])
        if not isinstance(user['id'], int) or user['id'] <= 0:
            return None
        return user
    except (KeyError, ValueError, TypeError):
        return None


@app.post('/api/auth')
def auth():
    data = request.get_json(silent=True) or {}
    user = verified_user(data.get('initData', ''))
    if not user:
        return error('Откройте приложение через Telegram-бота.', 401)
    user_id = user['id']
    name = (user.get('first_name') or 'Игрок')[:80]
    username = (user.get('username') or '')[:80]
    photo = user.get('photo_url') or ''
    photo = photo[:500] if photo.startswith('https://') else ''
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO users(id,name,username,photo_url) VALUES(?,?,?,?)', (user_id, name, username, photo))
        db.execute('UPDATE users SET name=?,username=?,photo_url=? WHERE id=?', (name, username, photo, user_id))
    session.clear()
    session['uid'] = user_id
    return jsonify(ok=True, user=profile())


def current_user():
    uid = session.get('uid')
    if not uid:
        return None
    with connect() as db:
        return db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        if not current_user():
            return error('Требуется вход через Telegram.', 401)
        return fn(*args, **kwargs)
    return decorated


def admin_required(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or user['id'] not in ADMIN_IDS:
            return error('Нет доступа.', 403)
        return fn(*args, **kwargs)
    return decorated


def profile():
    user = current_user()
    return dict(id=user['id'], name=user['name'], username=user['username'], photo_url=user['photo_url'],
                balance=user['balance'] / 100, admin=user['id'] in ADMIN_IDS)


def active_round(db, uid):
    return db.execute("SELECT * FROM rounds WHERE user_id=? AND state='active' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()


def round_view(row, reveal=False):
    if not row:
        return None
    opened = json.loads(row['opened'])
    factor = 0.97 * math.comb(25, len(opened)) / math.comb(25-row['mines'], len(opened)) if opened else 1
    return dict(id=row['id'], bet=row['bet']/100, mines=row['mines'], opened=opened, state=row['state'],
                multiplier=round(factor, 2), potential=round(row['bet']*factor/100, 2),
                positions=json.loads(row['positions']) if reveal or row['state'] != 'active' else [],
                payout=row['payout']/100)


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/health')
def health():
    return jsonify(status='ok')


@app.get('/api/me')
@login_required
def me():
    with connect() as db:
        row = active_round(db, session['uid'])
    return jsonify(user=profile(), round=round_view(row))


@app.post('/api/game/start')
@login_required
def start():
    data = request.get_json(silent=True) or {}
    try:
        mines = int(data.get('mines'))
        bet = int((Decimal(str(data.get('bet'))) * 100).to_integral_exact())
    except (ValueError, InvalidOperation, TypeError):
        return error('Укажите корректную ставку и число мин.')
    if not (1 <= mines <= 20 and 10 <= bet <= 100000):
        return error('Ставка от 0.10 до 1000, мин от 1 до 20.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if active_round(db, session['uid']):
            return error('Сначала завершите текущую игру.')
        updated = db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?', (bet, session['uid'], bet))
        if not updated.rowcount:
            return error('Недостаточно средств на демо-балансе.')
        positions = sorted(secrets.SystemRandom().sample(range(25), mines))
        db.execute('INSERT INTO rounds(user_id,bet,mines,positions) VALUES(?,?,?,?)', (session['uid'], bet, mines, json.dumps(positions)))
        row = active_round(db, session['uid'])
        db.commit()
        return jsonify(round=round_view(row), user=profile())
    finally:
        db.close()


@app.post('/api/game/open')
@login_required
def open_cell():
    data = request.get_json(silent=True) or {}
    try:
        cell = int(data.get('cell'))
    except (TypeError, ValueError):
        return error('Неверная клетка.')
    if cell not in range(25):
        return error('Неверная клетка.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = active_round(db, session['uid'])
        if not row:
            return error('Сначала начните игру.')
        opened = json.loads(row['opened'])
        if cell in opened:
            return error('Клетка уже открыта.')
        positions = json.loads(row['positions'])
        if cell in positions:
            db.execute("UPDATE rounds SET state='lost' WHERE id=?", (row['id'],))
        else:
            opened.append(cell)
            db.execute('UPDATE rounds SET opened=? WHERE id=?', (json.dumps(opened), row['id']))
            if len(opened) == 25-row['mines']:
                payout = round(row['bet'] * 0.97 * math.comb(25, len(opened))/math.comb(25-row['mines'], len(opened)))
                db.execute("UPDATE rounds SET state='won',payout=? WHERE id=?", (payout, row['id']))
                db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, session['uid']))
        result = db.execute('SELECT * FROM rounds WHERE id=?', (row['id'],)).fetchone()
        db.commit()
        return jsonify(round=round_view(result), user=profile())
    finally:
        db.close()


@app.post('/api/game/cashout')
@login_required
def cashout():
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = active_round(db, session['uid'])
        if not row or not json.loads(row['opened']):
            return error('Для вывода откройте хотя бы одну безопасную клетку.')
        opened = len(json.loads(row['opened']))
        payout = round(row['bet'] * 0.97 * math.comb(25, opened)/math.comb(25-row['mines'], opened))
        db.execute("UPDATE rounds SET state='won',payout=? WHERE id=?", (payout, row['id']))
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, session['uid']))
        result = db.execute('SELECT * FROM rounds WHERE id=?', (row['id'],)).fetchone()
        db.commit()
        return jsonify(round=round_view(result), user=profile())
    finally:
        db.close()


@app.get('/api/catalog')
@login_required
def catalog():
    if not CATALOG.exists():
        return jsonify(gifts=[], updated_at=None)
    try:
        content = json.loads(CATALOG.read_text(encoding='utf-8'))
        return jsonify(gifts=content.get('gifts', []), updated_at=content.get('updated_at'))
    except (OSError, ValueError):
        return error('Каталог повреждён.', 500)


@app.get('/api/admin/status')
@admin_required
def admin_status():
    if not CATALOG.exists():
        return jsonify(count=0, updated_at=None)
    content = json.loads(CATALOG.read_text(encoding='utf-8'))
    return jsonify(count=len(content.get('gifts', [])), updated_at=content.get('updated_at'))


def safe_image(value):
    if isinstance(value, str) and value.startswith('https://') and len(value) < 1000:
        return value
    return ''


@app.post('/api/admin/portal/import')
@admin_required
def portal_import():
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', '')).strip()
    if not key or len(key) > 8000:
        return error('Введите ключ Portal Market.')
    # Portals Mini App uses a tma Authorization header. Partner keys may use Bearer.
    authorization = key if key.startswith(('tma ', 'Bearer ')) else f'Bearer {key}'
    gifts = []
    try:
        for offset in range(0, 10000, 100):
            response = requests.get('https://portal-market.com/api/collections',
                                    params={'limit': 100, 'offset': offset},
                                    headers={'Authorization': authorization, 'Accept': 'application/json'}, timeout=18)
            response.raise_for_status()
            payload = response.json()
            items = payload.get('collections', payload.get('data', payload)) if isinstance(payload, dict) else payload
            if isinstance(items, dict):
                items = items.get('collections', items.get('items', []))
            if not isinstance(items, list):
                return error('Portal вернул неожиданный формат каталога.', 502)
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get('name') or item.get('title') or item.get('gift_name')
                if not name:
                    continue
                raw_price = item.get('floor_price') or item.get('floorPrice') or item.get('price')
                try:
                    price = str(Decimal(str(raw_price))) if raw_price is not None else None
                except InvalidOperation:
                    price = None
                # Prefer the collection's own PNG preview; preserve HTTPS fallback URL.
                img = next((safe_image(item.get(field)) for field in ('image_url', 'photo_url', 'preview_url', 'image', 'icon_url', 'png_url') if safe_image(item.get(field))), '')
                gifts.append(dict(id=str(item.get('id') or item.get('slug') or name), name=str(name)[:140],
                                  price_ton=price, image_url=img, image_format='png' if img.lower().split('?')[0].endswith('.png') else 'remote'))
            if len(items) < 100:
                break
        if not gifts:
            return error('Portal не вернул подарки. Проверьте ключ и его права.', 502)
        document = dict(source='Portal Market', updated_at=datetime.now(timezone.utc).isoformat(), gifts=gifts)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=DATA, delete=False, suffix='.tmp') as tmp:
            json.dump(document, tmp, ensure_ascii=False, indent=2)
            tmp_name = tmp.name
        os.replace(tmp_name, CATALOG)
        return jsonify(ok=True, count=len(gifts), updated_at=document['updated_at'])
    except requests.HTTPError as exc:
        status = exc.response.status_code
        return error('Portal отклонил ключ.' if status in (401, 403) else f'Portal вернул ошибку HTTP {status}.', 502)
    except (requests.RequestException, ValueError) as exc:
        app.logger.warning('Portal import failed: %s', type(exc).__name__)
        return error('Не удалось получить каталог Portal. Попробуйте позже.', 502)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
