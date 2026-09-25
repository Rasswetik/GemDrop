import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, render_template, request, session


BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', str(BASE / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'gemdrop.sqlite3'
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
CATALOG = DATA / 'portal_gifts.json'
BOT_TOKEN = (os.environ.get('BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
WEBAPP_URL = (os.environ.get('WEBAPP_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
BOT_USERNAME = (os.environ.get('BOT_USERNAME') or '').strip().lstrip('@')
ADMIN_IDS = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '5257227756').split(',') if x.strip().isdigit()}
app = Flask(__name__)
# A stable key avoids worker/restart-dependent Telegram sessions.
secret_path = DATA / '.session_secret'
if not os.environ.get('SECRET_KEY') and not BOT_TOKEN and not secret_path.exists():
    secret_path.write_text(secrets.token_hex(32), encoding='utf-8')
app.secret_key = os.environ.get('SECRET_KEY') or (
    hashlib.sha256(('gemdrop-session:' + BOT_TOKEN).encode()).hexdigest() if BOT_TOKEN
    else secret_path.read_text(encoding='utf-8'))
WEBHOOK_SECRET = hashlib.sha256((app.secret_key + BOT_TOKEN).encode()).hexdigest()[:48]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME')))


class DatabaseRow(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class PostgreSQL:
    """Small SQL compatibility layer for the existing parameterized SQLite queries."""
    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.connection = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10)

    def execute(self, sql, params=()):
        sql = sql.strip()
        if sql.startswith('PRAGMA table_info('):
            table = sql.split('(', 1)[1].rstrip(')')
            sql = 'SELECT column_name AS name, column_default AS dflt_value FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s'
            params = (table,)
        else:
            sql = sql.replace('BEGIN IMMEDIATE', 'BEGIN').replace('?', '%s')
            if 'INSERT OR IGNORE INTO' in sql:
                sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO') + ' ON CONFLICT DO NOTHING'
        returning = bool(re.match(r'INSERT INTO inventory\b', sql))
        if returning:
            sql += ' RETURNING id'
        cursor = self.connection.execute(sql, params)
        class Result:
            rowcount = cursor.rowcount
            lastrowid = cursor.fetchone()['id'] if returning else None
            def fetchone(self):
                row = cursor.fetchone()
                return DatabaseRow(row) if row else None
            def fetchall(self):
                return [DatabaseRow(row) for row in cursor.fetchall()]
            def __iter__(self):
                return iter(self.fetchall())
        return Result()

    def executescript(self, script):
        script = script.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
        script = re.sub(r'\bINTEGER\b', 'BIGINT', script)
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        try:
            if kind:
                self.connection.rollback()
            else:
                self.connection.commit()
        finally:
            self.close()


def connect():
    if DATABASE_URL:
        return PostgreSQL()
    db = sqlite3.connect(DB, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA journal_mode=WAL')
    return db


def initialize():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS app_documents (
            name TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
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
        CREATE TABLE IF NOT EXISTS referrals (
            referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS referrals_referrer ON referrals(referrer_id);
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, referrer_id INTEGER, referral_bonus INTEGER NOT NULL DEFAULT 0,
            admin_id INTEGER NOT NULL, request_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bot_updates (
            update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
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
        return error('На сервере не задан BOT_TOKEN. Добавьте токен именно бота, который открыл Mini App, в Environment на Render.', 503)
    user = verified_user(data.get('initData', ''))
    if not user:
        return error('Telegram не подтвердил вход. Проверьте BOT_TOKEN на Render: он должен принадлежать боту, через которого открыто приложение.', 401)
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
    if DATABASE_URL:
        db.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (uid,))
    return db.execute("SELECT * FROM rounds WHERE user_id=? AND state='active' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()


def save_document(name, document):
    with connect() as db:
        db.execute('INSERT INTO app_documents(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                   (name, json.dumps(document, ensure_ascii=False)))


def read_document(name):
    with connect() as db:
        row = db.execute('SELECT payload FROM app_documents WHERE name=?', (name,)).fetchone()
    return json.loads(row['payload']) if row else None


def read_catalog():
    stored = read_document('portal_catalog')
    if stored is not None:
        return stored
    if not CATALOG.exists():
        return {'gifts': [], 'updated_at': None}
    document = json.loads(CATALOG.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or not isinstance(document.get('gifts'), list):
        raise ValueError('Invalid catalog')
    return document


def collection_key(name):
    return re.sub(r'[\W_]+', '', str(name).casefold(), flags=re.UNICODE)


def gift_id_map():
    """Load the authoritative Telegram gift ID/name map; reuse a disk copy on outage."""
    path = DATA / 'gift_id_to_name.json'
    try:
        response = requests.get('https://cdn.changes.tg/gifts/id-to-name.json', timeout=12)
        response.raise_for_status()
        mapping = response.json()
        if not isinstance(mapping, dict) or not mapping or not all(
                str(k).isdigit() and isinstance(v, str) for k, v in mapping.items()):
            raise ValueError('Invalid gift mapping')
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(mapping, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)
        return mapping
    except (requests.RequestException, ValueError):
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        raise


def match_collection_image(gift, mapping):
    names = {collection_key(v): k for k, v in mapping.items()}
    match = None
    for field in ('telegram_gift_id', 'star_gift_id', 'gift_id'):
        candidate = str(gift.get(field) or '')
        if candidate in mapping and collection_key(mapping[candidate]) == collection_key(gift['name']):
            match = candidate
            break
    if match is None:
        match = names.get(collection_key(gift['name']))
    updated = dict(gift)
    if match:
        updated.update(telegram_gift_id=match,
                       image_url=f'https://cdn.changes.tg/gifts/originals/{match}/Original.png',
                       image_format='png', image_source='cdn.changes.tg', image_match=True)
    else:
        preview = safe_image(updated.get('portal_image_url') or updated.get('image_url'))
        updated.update(image_url=preview, image_format=None,
                       image_source='Portal Market' if preview else None, image_match=False)
    return updated


def save_catalog(document):
    save_document('portal_catalog', document)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=DATA, delete=False, suffix='.tmp') as tmp:
        json.dump(document, tmp, ensure_ascii=False, indent=2)
        tmp_name = tmp.name
    if CATALOG.exists():
        backups = DATA / 'catalog_backups'
        backups.mkdir(exist_ok=True)
        shutil.copy2(CATALOG, backups / f'portal_gifts_{time.time_ns()}.json')
        for stale in sorted(backups.glob('portal_gifts_*.json'))[:-5]:
            stale.unlink(missing_ok=True)
    os.replace(tmp_name, CATALOG)


def refresh_inventory_images(mapping):
    """Correct older inventory previews by exact collection name; keep price snapshots."""
    with connect() as db:
        for item in db.execute('SELECT id,gift_name,image_url FROM inventory').fetchall():
            matched = match_collection_image({'name': item['gift_name']}, mapping)
            if matched['image_match'] and matched['image_url'] != item['image_url']:
                db.execute('UPDATE inventory SET image_url=? WHERE id=?',
                           (matched['image_url'], item['id']))


def prize_for(amount, gifts=None):
    if gifts is None:
        try:
            gifts = read_catalog()['gifts']
        except (OSError, ValueError):
            gifts = []
    eligible = []
    for gift in gifts:
        try:
            if not gift.get('id') or not gift.get('name') or not gift.get('image_match'):
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
        remainder = max(0, amount - cents)
        cursor = db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                               VALUES(?,?,?,?,?,'game',?)''',
                            (row['user_id'], str(prize['id']), str(prize['name']),
                             safe_image(prize.get('image_url')), cents, row['id']))
        db.execute("UPDATE rounds SET state='won',payout=?,prize_inventory_id=? WHERE id=?",
                   (remainder, cursor.lastrowid, row['id']))
        if remainder:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (remainder, row['user_id']))
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


@app.get('/api/referrals/me')
@login_required
def my_referrals():
    with connect() as db:
        count = db.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',
                           (session['uid'],)).fetchone()[0]
        total = db.execute('SELECT COALESCE(SUM(referral_bonus),0) FROM deposits WHERE referrer_id=?',
                           (session['uid'],)).fetchone()[0]
    username = BOT_USERNAME
    return jsonify(count=count, earned=total/100,
                   link=f'https://t.me/{username}?start=ref_{session["uid"]}' if username else '')


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


@app.post('/api/admin/users/<int:user_id>/deposit')
@admin_required
def admin_deposit(user_id):
    data = request.get_json(silent=True) or {}
    try:
        amount = parse_amount(data.get('amount'))
    except (ValueError, TypeError, InvalidOperation):
        return error('Укажите депозит с точностью до 0.01.')
    key = str(data.get('request_key', ''))
    if not (1 <= amount <= 100000000 and re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key)):
        return error('Некорректная сумма или идентификатор операции.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT user_id FROM deposits WHERE request_key=?', (key,)).fetchone()
        if old:
            db.commit()
            return jsonify(ok=True, duplicate=True)
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        referral = db.execute('SELECT referrer_id FROM referrals WHERE referred_id=?',
                              (user_id,)).fetchone()
        referrer = referral['referrer_id'] if referral else None
        bonus = amount // 10 if referrer else 0
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
        if referrer:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (bonus, referrer))
        db.execute('''INSERT INTO deposits(user_id,amount,referrer_id,referral_bonus,admin_id,request_key)
                      VALUES(?,?,?,?,?,?)''', (user_id, amount, referrer, bonus, session['uid'], key))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'deposit', str(amount)))
        db.commit()
        return jsonify(ok=True, balance_added=amount/100, referral_bonus=bonus/100)
    finally:
        db.close()


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


def portal_headers(key):
    key = key.strip()
    headers = {'Accept': 'application/json', 'User-Agent': 'GemDrop/1.0'}
    if key:
        if not key.startswith(('tma ', 'Bearer ')):
            key = ('tma ' if 'hash=' in key and 'auth_date=' in key else 'Bearer ') + key
        headers['Authorization'] = key
    return headers


def fetch_portal_catalog(key, progress=None):
    previous = read_catalog()['gifts']
    previous_by_id = {str(gift.get('id')): gift for gift in previous}
    gifts, seen = [], set()
    offset = 0
    # Portals currently caps collections pages at 20 even when limit=100.
    # Advance by the number actually received, and stop on a repeated page.
    for page in range(100):
        for attempt in range(2):
            try:
                response = requests.get('https://portal-market.com/api/collections',
                                        params={'limit': 20, 'offset': offset},
                                        headers=portal_headers(key), timeout=(10, 25))
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if attempt or status in (400, 401, 403, 404):
                    raise
                time.sleep(1)
        payload = response.json()
        items = payload.get('collections', payload.get('data', payload)) if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = items.get('collections', items.get('items'))
        if not isinstance(items, list):
            raise ValueError('Portal вернул неожиданный формат коллекций.')
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get('name') or item.get('title') or item.get('gift_name')
            gift_id = str(item.get('id') or item.get('slug') or name or '')
            if not name or gift_id in seen:
                continue
            seen.add(gift_id)
            added += 1
            raw = next((item[k] for k in ('floor_price', 'floorPrice', 'price') if item.get(k) is not None), None)
            try:
                price = Decimal(str(raw))
                price = str(price) if price.is_finite() and price >= 0 else None
            except InvalidOperation:
                price = None
            img = next((safe_image(item.get(k)) for k in ('image_url', 'photo_url', 'preview_url', 'image', 'icon_url', 'png_url') if safe_image(item.get(k))), '')
            gift = dict(id=gift_id, name=str(name)[:140], price_ton=price, portal_image_url=img)
            old = previous_by_id.get(gift_id, {})
            gift.update(image_url=old.get('image_url') or img, image_match=bool(old.get('image_match')),
                        telegram_gift_id=old.get('telegram_gift_id', ''))
            gifts.append(gift)
        if progress:
            progress(len(gifts))
        if not items or not added:
            break
        offset += len(items)
        time.sleep(.15)
    else:
        raise ValueError('Portal не завершил список коллекций. Прежний каталог сохранён.')
    if not gifts:
        raise ValueError('Portal вернул пустой каталог. Прежний каталог сохранён.')
    try:
        mapping = gift_id_map()
    except (requests.RequestException, ValueError, OSError):
        mapping = {}
    if mapping:
        gifts = [match_collection_image(g, mapping) for g in gifts]
    retained = [g for g in previous if str(g.get('id')) not in seen]
    gifts.extend(retained)
    document = dict(source='Portal Market', updated_at=datetime.now(timezone.utc).isoformat(), gifts=gifts)
    save_catalog(document)
    if mapping:
        refresh_inventory_images(mapping)
    return dict(count=len(gifts), matched=sum(bool(g.get('image_match')) for g in gifts), retained=len(retained))


portal_job_lock = __import__('threading').Lock()


def portal_job(key):
    try:
        result = fetch_portal_catalog(key, lambda count: save_document('portal_job',
                     dict(state='running', count=count, updated=time.time())))
        save_document('portal_job', dict(state='done', updated=time.time(), **result))
    except requests.HTTPError as exc:
        status = exc.response.status_code
        message = ('Ключ Portal истёк или отклонён. Обновите Authorization из Portal либо очистите поле для публичного каталога.'
                   if status in (401, 403) else f'Portal вернул HTTP {status}. Каталог сохранён; повторите позже.')
        save_document('portal_job', dict(state='error', error=message, updated=time.time()))
    except (requests.RequestException, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Portal не ответил вовремя. Старые подарки сохранены. Повторите загрузку.'
        save_document('portal_job', dict(state='error', error=message, updated=time.time()))
    finally:
        portal_job_lock.release()


@app.post('/api/admin/portal/import')
@admin_required
def portal_import():
    key = str((request.get_json(silent=True) or {}).get('key', '')).strip()
    if len(key) > 8000 or '\n' in key or '\r' in key:
        return error('Некорректный ключ Portal.')
    if not portal_job_lock.acquire(blocking=False):
        return jsonify(ok=True, state='running'), 202
    save_document('portal_job', dict(state='running', count=0, updated=time.time()))
    Thread(target=portal_job, args=(key,), daemon=True).start()
    return jsonify(ok=True, state='running'), 202


@app.get('/api/admin/portal/job')
@admin_required
def portal_job_status():
    job = read_document('portal_job') or dict(state='idle')
    if job.get('state') == 'running' and time.time() - job.get('updated', 0) > 120:
        job = dict(state='error', error='Загрузка прервалась при перезапуске сервера. Повторите импорт.')
    return jsonify(job)


@app.post('/api/admin/portal/images/refresh')
@admin_required
def refresh_portal_images():
    try:
        document = read_catalog()
        if not document['gifts']:
            return error('Сначала загрузите каталог Portal.')
        mapping = gift_id_map()
        document['gifts'] = [match_collection_image(gift, mapping) for gift in document['gifts']]
        document['images_updated_at'] = datetime.now(timezone.utc).isoformat()
        save_catalog(document)
        refresh_inventory_images(mapping)
        return jsonify(ok=True, count=len(document['gifts']),
                       matched=sum(g['image_match'] for g in document['gifts']))
    except (requests.RequestException, ValueError, OSError) as exc:
        app.logger.warning('Gift image refresh failed: %s', type(exc).__name__)
        return error('Не удалось сопоставить изображения. Старый каталог сохранён.', 502)


WELCOME_TEXT = (
    '🎉 <b>Привет, Добро Пожаловать в GemDrop! 💎</b>\n\n'
    'Открывай кейсы и выигрывай лучшие NFT гифты!\n\n'
    '💰 Делись своей реферальной ссылкой с друзьями – и за каждого приведённого друга '
    'который сделает депозит ты получишь 10% от суммы их пополнений!'
)


@app.post('/telegram/webhook')
def telegram_webhook():
    received = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not BOT_TOKEN or not hmac.compare_digest(received, WEBHOOK_SECRET):
        return error('Нет доступа.', 403)
    if not WEBAPP_URL.startswith('https://'):
        return error('Укажите HTTPS URL приложения.', 503)
    update = request.get_json(silent=True) or {}
    message = update.get('message') or {}
    sender = message.get('from') or {}
    chat = message.get('chat') or {}
    command = str(message.get('text') or '').split(maxsplit=1)
    if (chat.get('type') != 'private' or not command or
            command[0].split('@')[0] != '/start' or not isinstance(sender.get('id'), int)):
        return jsonify(ok=True)
    try:
        update_id = int(update['update_id'])
        uid = sender['id']
        db = connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('INSERT OR IGNORE INTO bot_updates(update_id) VALUES(?)',
                              (update_id,)).rowcount:
                db.commit()
                return jsonify(ok=True)
            is_new = db.execute('''INSERT OR IGNORE INTO users(id,name,username,balance)
                                   VALUES(?,?,?,0)''',
                                (uid, str(sender.get('first_name') or 'Игрок')[:80],
                                 str(sender.get('username') or '')[:80])).rowcount
            if is_new and len(command) == 2 and re.fullmatch(r'ref_[0-9]{1,20}', command[1]):
                referrer = int(command[1][4:])
                if uid != referrer and db.execute('SELECT 1 FROM users WHERE id=?',
                                                 (referrer,)).fetchone():
                    db.execute('INSERT OR IGNORE INTO referrals(referred_id,referrer_id) VALUES(?,?)',
                               (uid, referrer))
            db.commit()
        finally:
            db.close()
        button = {'inline_keyboard': [[{'text': '🎮 Играть',
                                       'web_app': {'url': WEBAPP_URL + '/'}}]]}
        response = requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
                                 json={'chat_id': chat['id'], 'text': WELCOME_TEXT,
                                       'parse_mode': 'HTML', 'reply_markup': button}, timeout=12)
        response.raise_for_status()
        if not response.json().get('ok'):
            raise ValueError('Telegram rejected sendMessage')
        return jsonify(ok=True)
    except (ValueError, KeyError, requests.RequestException, sqlite3.Error) as exc:
        app.logger.warning('Telegram update failed: %s', type(exc).__name__)
        if isinstance(update.get('update_id'), int):
            with connect() as db:
                db.execute('DELETE FROM bot_updates WHERE update_id=?', (update['update_id'],))
        return error('Не удалось отправить сообщение.', 502)


def configure_bot():
    global BOT_USERNAME
    if not BOT_TOKEN or not WEBAPP_URL.startswith('https://'):
        return
    try:
        info = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=10)
        info.raise_for_status()
        if info.json().get('ok'):
            BOT_USERNAME = info.json()['result'].get('username') or BOT_USERNAME
        response = requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/setWebhook',
                                 json={'url': WEBAPP_URL + '/telegram/webhook',
                                       'secret_token': WEBHOOK_SECRET,
                                       'allowed_updates': ['message']}, timeout=12)
        response.raise_for_status()
        if not response.json().get('ok'):
            raise ValueError('setWebhook rejected')
        app.logger.info('Telegram webhook configured for %s', WEBAPP_URL)
    except (requests.RequestException, ValueError, KeyError) as exc:
        app.logger.warning('Telegram webhook setup failed: %s', type(exc).__name__)


if BOT_TOKEN and WEBAPP_URL.startswith('https://'):
    Thread(target=configure_bot, daemon=True).start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
