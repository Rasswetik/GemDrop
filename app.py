import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, render_template, request, session


BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', str(BASE / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'gemdrop.sqlite3'
CATALOG = DATA / 'portal_gifts.json'
BOT_TOKEN = (os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('BOT_TOKEN') or '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '5257227756').split(',') if x.strip().isdigit()}
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME')))


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
            photo_url TEXT NOT NULL DEFAULT '', balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            bet INTEGER NOT NULL, mines INTEGER NOT NULL, positions TEXT NOT NULL,
            opened TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'active',
            payout INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            gift_id TEXT NOT NULL, gift_name TEXT NOT NULL, image_url TEXT NOT NULL DEFAULT '',
            floor_price INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
            round_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS inventory_user ON inventory(user_id,id DESC);
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        balance_default = next((row['dflt_value'] for row in db.execute('PRAGMA table_info(users)')
                                if row['name'] == 'balance'), None)
        # Only the old schema handed every new user 10 demo units. Remove that
        # legacy allocation once, without touching balances set after upgrading.
        if str(balance_default).strip("'\"()") == '1000' and not db.execute(
                "SELECT 1 FROM schema_migrations WHERE name='remove_legacy_demo_balances'").fetchone():
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE users SET balance=0')
            db.execute("UPDATE rounds SET state='lost' WHERE state='active'")
            db.execute("INSERT INTO schema_migrations(name) VALUES('remove_legacy_demo_balances')")
            db.commit()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(rounds)')}
        if 'prize_inventory_id' not in columns:
            db.execute('ALTER TABLE rounds ADD COLUMN prize_inventory_id INTEGER')
        if 'lost_cell' not in columns:
            db.execute('ALTER TABLE rounds ADD COLUMN lost_cell INTEGER')


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
    if not BOT_TOKEN:
        return error('На сервере не задан TELEGRAM_BOT_TOKEN. Добавьте токен именно бота, который открыл Mini App, в Environment на Render.', 503)
    user = verified_user(data.get('initData', ''))
    if not user:
        return error('Telegram не подтвердил вход. Проверьте TELEGRAM_BOT_TOKEN на Render: он должен принадлежать боту, через которого открыто приложение.', 401)
    user_id = user['id']
    name = (user.get('first_name') or 'Игрок')[:80]
    username = (user.get('username') or '')[:80]
    photo = user.get('photo_url') or ''
    photo = photo[:500] if photo.startswith('https://') else ''
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO users(id,name,username,photo_url,balance) VALUES(?,?,?,?,0)', (user_id, name, username, photo))
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


def read_catalog():
    if not CATALOG.exists():
        return {'gifts': [], 'updated_at': None}
    document = json.loads(CATALOG.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or not isinstance(document.get('gifts'), list):
        raise ValueError('Invalid catalog')
    return document


def prize_for(amount, gifts=None):
    if gifts is None:
        try:
            gifts = read_catalog()['gifts']
        except (OSError, ValueError):
            gifts = []
    eligible = []
    for gift in gifts:
        try:
            if not gift.get('id') or not gift.get('name'):
                continue
            price = int(Decimal(str(gift['price_ton'])) * 100)
            if price > 0 and price <= amount:
                eligible.append((price, gift))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return max(eligible, key=lambda x: (x[0], str(x[1].get('id', ''))))[1] if eligible else None


def payout_for(row, opened_count):
    return round(row['bet'] * 0.97 * math.comb(25, opened_count) /
                 math.comb(25-row['mines'], opened_count))


def inventory_item(row):
    return dict(id=row['id'], gift_id=row['gift_id'], name=row['gift_name'],
                image_url=row['image_url'], price_ton=row['floor_price']/100,
                source=row['source'], created_at=row['created_at'])


def award_round(db, row, opened_count):
    """Settle once, atomically, as a catalog gift or a balance payout."""
    amount = payout_for(row, opened_count)
    prize = prize_for(amount)
    if prize:
        cents = int(Decimal(str(prize['price_ton'])) * 100)
        cursor = db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                               VALUES(?,?,?,?,?,'game',?)''',
                            (row['user_id'], str(prize['id']), str(prize['name']),
                             safe_image(prize.get('image_url')), cents, row['id']))
        db.execute("UPDATE rounds SET state='won',payout=0,prize_inventory_id=? WHERE id=?",
                   (cursor.lastrowid, row['id']))
    else:
        db.execute("UPDATE rounds SET state='won',payout=? WHERE id=?", (amount, row['id']))
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, row['user_id']))


def round_view(row, reveal=False):
    if not row:
        return None
    opened = json.loads(row['opened'])
    factor = 0.97 * math.comb(25, len(opened)) / math.comb(25-row['mines'], len(opened)) if opened else 1
    amount = payout_for(row, len(opened)) if opened else row['bet']
    prize = prize_for(amount) if opened and row['state'] == 'active' else None
    owned = None
    if row['prize_inventory_id']:
        with connect() as db:
            item = db.execute('SELECT * FROM inventory WHERE id=?', (row['prize_inventory_id'],)).fetchone()
            owned = inventory_item(item) if item else None
    return dict(id=row['id'], bet=row['bet']/100, mines=row['mines'], opened=opened, state=row['state'],
                multiplier=round(factor, 2), potential=amount/100,
                positions=json.loads(row['positions']) if reveal or row['state'] != 'active' else [],
                payout=row['payout']/100, prize=prize, awarded=owned, lost_cell=row['lost_cell'])


@app.get('/api/game/ladder')
@login_required
def ladder():
    try:
        mines = int(request.args.get('mines', '3'))
        bet = parse_amount(request.args.get('bet', '0.1'))
    except (ValueError, InvalidOperation, TypeError):
        return error('Неверные параметры.')
    if not (1 <= mines <= 20 and 10 <= bet <= 100000):
        return error('Неверные параметры.')
    try:
        gifts = read_catalog()['gifts']
    except (OSError, ValueError):
        gifts = []
    # Snapshot for the UI. The award is always checked anew on the server.
    dummy = {'bet': bet, 'mines': mines}
    return jsonify(levels=[dict(step=step, multiplier=round(0.97*math.comb(25, step)/math.comb(25-mines, step), 2),
                                amount=payout_for(dummy, step)/100, prize=prize_for(payout_for(dummy, step), gifts))
                           for step in range(1, 26-mines)])


def parse_amount(value):
    value = Decimal(str(value)) * 100
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError('Invalid money amount')
    return int(value)


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
        bet = parse_amount(data.get('bet'))
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
            return error('Недостаточно средств. Баланс может пополнить администратор.')
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
            db.execute("UPDATE rounds SET state='lost',lost_cell=? WHERE id=?", (cell, row['id']))
        else:
            opened.append(cell)
            db.execute('UPDATE rounds SET opened=? WHERE id=?', (json.dumps(opened), row['id']))
            if len(opened) == 25-row['mines']:
                award_round(db, row, len(opened))
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
        award_round(db, row, opened)
        result = db.execute('SELECT * FROM rounds WHERE id=?', (row['id'],)).fetchone()
        db.commit()
        return jsonify(round=round_view(result), user=profile())
    finally:
        db.close()


@app.get('/api/catalog')
@login_required
def catalog():
    try:
        content = read_catalog()
        return jsonify(gifts=content.get('gifts', []), updated_at=content.get('updated_at'))
    except (OSError, ValueError):
        return error('Каталог повреждён.', 500)


@app.get('/api/admin/status')
@admin_required
def admin_status():
    content = read_catalog()
    return jsonify(count=len(content.get('gifts', [])), updated_at=content.get('updated_at'))


@app.get('/api/inventory')
@login_required
def inventory():
    with connect() as db:
        items = db.execute('SELECT * FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 200',
                           (session['uid'],)).fetchall()
    return jsonify(items=[inventory_item(item) for item in items])


@app.get('/api/admin/users')
@admin_required
def admin_users():
    term = request.args.get('q', '').strip()[:80]
    with connect() as db:
        if term:
            users = db.execute('''SELECT u.id,u.name,u.username,u.balance,COUNT(i.id) AS gifts
                                  FROM users u LEFT JOIN inventory i ON i.user_id=u.id
                                  WHERE CAST(u.id AS TEXT) LIKE ? OR u.username LIKE ? OR u.name LIKE ?
                                  GROUP BY u.id ORDER BY u.id DESC LIMIT 50''',
                               (f'%{term}%', f'%{term}%', f'%{term}%')).fetchall()
        else:
            users = db.execute('''SELECT u.id,u.name,u.username,u.balance,COUNT(i.id) AS gifts
                                  FROM users u LEFT JOIN inventory i ON i.user_id=u.id
                                  GROUP BY u.id ORDER BY u.id DESC LIMIT 50''').fetchall()
    return jsonify(users=[dict(id=u['id'], name=u['name'], username=u['username'],
                               balance=u['balance']/100, gifts=u['gifts']) for u in users])


@app.get('/api/admin/users/<int:user_id>')
@admin_required
def admin_user(user_id):
    with connect() as db:
        user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            return error('Пользователь не найден.', 404)
        items = db.execute('SELECT * FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 200',
                           (user_id,)).fetchall()
    return jsonify(user=dict(id=user['id'], name=user['name'], username=user['username'],
                             balance=user['balance']/100), items=[inventory_item(x) for x in items])


@app.post('/api/admin/users/<int:user_id>/balance')
@admin_required
def admin_balance(user_id):
    data = request.get_json(silent=True) or {}
    try:
        amount = parse_amount(data.get('balance'))
    except (ValueError, TypeError, InvalidOperation):
        return error('Введите сумму с точностью до 0.01.')
    if not 0 <= amount <= 100000000:
        return error('Баланс должен быть от 0 до 1 000 000.')
    with connect() as db:
        cursor = db.execute('UPDATE users SET balance=? WHERE id=?', (amount, user_id))
        if not cursor.rowcount:
            return error('Пользователь не найден.', 404)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'balance_set', str(amount)))
    return jsonify(ok=True, balance=amount/100)


@app.post('/api/admin/users/<int:user_id>/inventory')
@admin_required
def admin_add_inventory(user_id):
    data = request.get_json(silent=True) or {}
    gift_id = str(data.get('gift_id', ''))
    try:
        gift = next((gift for gift in read_catalog()['gifts'] if str(gift.get('id')) == gift_id), None)
    except (OSError, ValueError):
        gift = None
    if not gift:
        return error('Выберите подарок из каталога Portal.')
    try:
        price = parse_amount(gift['price_ton']) if gift.get('price_ton') is not None else 0
    except (KeyError, ValueError, TypeError, InvalidOperation):
        price = 0
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source)
                      VALUES(?,?,?,?,?,'admin')''',
                   (user_id, gift_id, str(gift['name']), safe_image(gift.get('image_url')), price))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'gift_add', gift_id))
    return jsonify(ok=True)


@app.delete('/api/admin/users/<int:user_id>/inventory/<int:item_id>')
@admin_required
def admin_remove_inventory(user_id, item_id):
    with connect() as db:
        result = db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (item_id, user_id))
        if not result.rowcount:
            return error('Подарок не найден.', 404)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'gift_remove', str(item_id)))
    return jsonify(ok=True)


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
    seen = set()
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
                gift_id = str(item.get('id') or item.get('slug') or name)
                if gift_id in seen:
                    continue
                seen.add(gift_id)
                gifts.append(dict(id=gift_id, name=str(name)[:140],
                                  price_ton=price, image_url=img,
                                  image_format='png' if img.lower().split('?')[0].endswith('.png') else 'remote'))
            if len(items) < 100:
                break
        if not gifts:
            return error('Portal не вернул подарки. Проверьте ключ и его права.', 502)
        old_count = len(read_catalog()['gifts'])
        if old_count > 10 and len(gifts) < old_count // 2:
            return error('Новый ответ Portal содержит менее половины прежнего каталога. Старые подарки сохранены; проверьте права ключа.', 502)
        document = dict(source='Portal Market', updated_at=datetime.now(timezone.utc).isoformat(), gifts=gifts)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=DATA, delete=False, suffix='.tmp') as tmp:
            json.dump(document, tmp, ensure_ascii=False, indent=2)
            tmp_name = tmp.name
        if CATALOG.exists():
            backups = DATA / 'catalog_backups'
            backups.mkdir(exist_ok=True)
            shutil.copy2(CATALOG, backups / f'portal_gifts_{int(time.time())}.json')
            for stale in sorted(backups.glob('portal_gifts_*.json'))[:-5]:
                stale.unlink(missing_ok=True)
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
