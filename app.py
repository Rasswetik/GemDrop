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
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
BOT_TOKEN_FINGERPRINT = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:16] if BOT_TOKEN else ''
TONCENTER_API_KEY = (os.environ.get('TONCENTER_API_KEY') or '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '5257227756,8468542825').split(',') if x.strip().isdigit()}
GAME_RTP_DEFAULT = 0.97
PROMO_RTP_DEFAULT = 0.90
MIN_GAME_RTP = 0.97
MIN_PROMO_RTP = 0.89
MIN_BET_CENTS = 10
MAX_BET_CENTS = 30000  # 300 TON
MIN_MINES = 1
MAX_MINES = 20
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
    return db


def initialize():
    with connect() as db:
        # WAL is persistent for the SQLite database. Set it once at startup instead
        # of executing journal_mode on every API request/connection.
        if not DATABASE_URL:
            db.execute('PRAGMA journal_mode=WAL')
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
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, referrer_id INTEGER, referral_bonus INTEGER NOT NULL DEFAULT 0,
            admin_id INTEGER NOT NULL, request_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            inventory_id INTEGER NOT NULL, gift_id TEXT NOT NULL, gift_name TEXT NOT NULL, image_url TEXT NOT NULL DEFAULT '',
            floor_price INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'withdrawal',
            round_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', admin_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            kind TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0, balance_after INTEGER,
            reference_type TEXT NOT NULL DEFAULT '', reference_id TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bot_updates (
            update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY, reward_type TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0,
            gift_id TEXT NOT NULL DEFAULT '', gift_name TEXT NOT NULL DEFAULT '',
            gift_image_url TEXT NOT NULL DEFAULT '', gift_price INTEGER NOT NULL DEFAULT 0,
            wager_multiplier REAL NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 1, uses_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            code TEXT NOT NULL, user_id INTEGER NOT NULL, reward_type TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0, inventory_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(code,user_id)
        );
        CREATE TABLE IF NOT EXISTS user_wallets (
            user_id INTEGER PRIMARY KEY, address TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ton_deposit_orders (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, wallet_address TEXT NOT NULL,
            recipient_wallet TEXT NOT NULL, amount INTEGER NOT NULL, amount_nano BIGINT NOT NULL,
            created_unix INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            tx_hash TEXT UNIQUE, credited_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS roll_spins (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, roll_id TEXT NOT NULL,
            price INTEGER NOT NULL, outcome TEXT NOT NULL, gift_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS levels (
            level INTEGER PRIMARY KEY, required_turnover INTEGER NOT NULL,
            reward_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS level_claims (
            user_id INTEGER NOT NULL, level INTEGER NOT NULL, reward_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,level)
        );
        CREATE TABLE IF NOT EXISTS upgrade_spins (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,source_name TEXT NOT NULL,
            source_image TEXT NOT NULL DEFAULT '',source_price INTEGER NOT NULL,
            target_name TEXT NOT NULL,target_image TEXT NOT NULL DEFAULT '',target_price INTEGER NOT NULL,
            chance_bp INTEGER NOT NULL,won INTEGER NOT NULL,result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transfer_rates (
            level INTEGER PRIMARY KEY,fee_percent REAL NOT NULL DEFAULT 5,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id TEXT PRIMARY KEY,sender_id INTEGER NOT NULL,recipient_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,fee INTEGER NOT NULL,
            sender_before INTEGER NOT NULL,recipient_before INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            seen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        def ensure_columns(table, definitions):
            existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
            for name, definition in definitions:
                if name not in existing:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

        # Older Render disks may contain tables created by much earlier builds.
        # Keep migrations additive so an update cannot turn a working deployment into HTTP 500.
        ensure_columns('users', [
            ('username', "TEXT NOT NULL DEFAULT ''"),
            ('photo_url', "TEXT NOT NULL DEFAULT ''"),
            ('balance', 'INTEGER NOT NULL DEFAULT 0'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('roll_boost', 'REAL NOT NULL DEFAULT 1'),
            ('turnover_cents', 'INTEGER NOT NULL DEFAULT 0'),
        ])
        ensure_columns('rounds', [
            ('prize_inventory_id', 'INTEGER'), ('lost_cell', 'INTEGER'), ('win_total', 'INTEGER'),
            ('win_multiplier', 'REAL'), ('win_gift_name', "TEXT NOT NULL DEFAULT ''"),
            ('win_gift_image', "TEXT NOT NULL DEFAULT ''"), ('win_gift_price', 'INTEGER'),
            ('bet_type', "TEXT NOT NULL DEFAULT 'ton'"), ('bet_inventory_id', 'INTEGER'),
            ('bet_gift_id', "TEXT NOT NULL DEFAULT ''"), ('bet_gift_name', "TEXT NOT NULL DEFAULT ''"),
            ('bet_gift_image', "TEXT NOT NULL DEFAULT ''"), ('bet_gift_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_wager_multiplier', 'REAL NOT NULL DEFAULT 0'), ('promo_wager_target', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_wager_progress', 'INTEGER NOT NULL DEFAULT 0'), ('promo_progress_after', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_code', "TEXT NOT NULL DEFAULT ''"), ('rtp_snapshot', 'REAL'),
        ])
        ensure_columns('inventory', [
            ('image_url', "TEXT NOT NULL DEFAULT ''"), ('floor_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('source', "TEXT NOT NULL DEFAULT 'legacy'"), ('round_id', 'INTEGER'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('promo_locked', 'INTEGER NOT NULL DEFAULT 0'), ('promo_wager_multiplier', 'REAL NOT NULL DEFAULT 0'),
            ('promo_wager_target', 'INTEGER NOT NULL DEFAULT 0'), ('promo_wager_progress', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_code', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('promo_codes', [
            ('wager_multiplier', 'REAL NOT NULL DEFAULT 0'),
            ('bonus_percent', 'REAL NOT NULL DEFAULT 0'),
            ('bonus_fixed', 'INTEGER NOT NULL DEFAULT 0'),
            ('min_deposit', 'INTEGER NOT NULL DEFAULT 0'),
            ('reward_json', "TEXT NOT NULL DEFAULT '{}'"),
            ('assigned_user_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('source_label', "TEXT NOT NULL DEFAULT ''"),
            ('description', "TEXT NOT NULL DEFAULT ''"),
            ('expires_at', 'TEXT'),
        ])
        ensure_columns('promo_redemptions', [('consumed_at', 'TEXT'),('deactivated_at', 'TEXT')])
        ensure_columns('ton_deposit_orders', [('promo_code', "TEXT NOT NULL DEFAULT ''")])
        ensure_columns('withdrawals', [
            ('image_url', "TEXT NOT NULL DEFAULT ''"), ('floor_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('source', "TEXT NOT NULL DEFAULT 'withdrawal'"), ('round_id', 'INTEGER'),
            ('status', "TEXT NOT NULL DEFAULT 'pending'"), ('admin_id', 'INTEGER'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"), ('processed_at', 'TEXT'),
        ])
        ensure_columns('referrals', [
            ('referrer_id', 'INTEGER NOT NULL DEFAULT 0'), ('created_at', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('deposits', [
            ('amount', 'INTEGER NOT NULL DEFAULT 0'), ('referrer_id', 'INTEGER'),
            ('referral_bonus', 'INTEGER NOT NULL DEFAULT 0'), ('admin_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('request_key', "TEXT NOT NULL DEFAULT ''"), ('created_at', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('transactions', [
            ('kind', "TEXT NOT NULL DEFAULT 'legacy'"), ('amount', 'INTEGER NOT NULL DEFAULT 0'),
            ('balance_after', 'INTEGER'), ('reference_type', "TEXT NOT NULL DEFAULT ''"),
            ('reference_id', "TEXT NOT NULL DEFAULT ''"), ('details', "TEXT NOT NULL DEFAULT ''"),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
        ])
        # Indexes are intentionally created after additive migrations. Creating an index on a
        # column that did not exist on an older Render disk was the source of the HTTP 500 startup failure.
        db.execute('CREATE INDEX IF NOT EXISTS inventory_user ON inventory(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS referrals_referrer ON referrals(referrer_id)')
        db.execute('CREATE INDEX IF NOT EXISTS withdrawals_status ON withdrawals(status,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS withdrawals_user ON withdrawals(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS transactions_user ON transactions(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS transactions_kind ON transactions(kind,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS ton_deposit_orders_user ON ton_deposit_orders(user_id,id)')
        db.execute('CREATE INDEX IF NOT EXISTS user_events_user ON user_events(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS promo_codes_assigned_user ON promo_codes(assigned_user_id,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS transfers_recipient ON transfers(recipient_id,seen_at,id)')
        for level in range(1,21):
            db.execute('INSERT OR IGNORE INTO levels(level,required_turnover,reward_json) VALUES(?,?,?)',
                       (level, (level-1)*level*50, '{}'))
            db.execute('INSERT OR IGNORE INTO transfer_rates(level,fee_percent,enabled) VALUES(?,5,1)',(level,))



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
    referrer_id = data.get('referrer_id')
    # Also accept Telegram Mini App start_param when the app is opened from a startapp-style link.
    # The ordinary bot deep-link still uses /start ref_<id>; this is a second, signed fallback.
    if referrer_id in (None, ''):
        try:
            start_param = dict(parse_qsl(str(data.get('initData') or ''), keep_blank_values=True)).get('start_param', '')
            if re.fullmatch(r'ref_[0-9]{1,20}', start_param):
                referrer_id = start_param[4:]
        except (ValueError, TypeError):
            pass
    try:
        referrer_id = int(referrer_id) if referrer_id not in (None, '') else None
    except (TypeError, ValueError):
        referrer_id = None
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO users(id,name,username,photo_url,balance) VALUES(?,?,?,?,0)', (user_id, name, username, photo))
        db.execute('UPDATE users SET name=?,username=?,photo_url=? WHERE id=?', (name, username, photo, user_id))
        if referrer_id and referrer_id != user_id:
            # A referral is bound once. Existing users may still become referrals as
            # long as they have never been bound before; rewards are paid only from
            # future confirmed TON deposits in credit_ton_deposit().
            if db.execute('SELECT 1 FROM users WHERE id=?', (referrer_id,)).fetchone():
                db.execute('INSERT OR IGNORE INTO referrals(referred_id,referrer_id) VALUES(?,?)',
                           (user_id, referrer_id))
        log_event(db,user_id,'login',username=username)
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
        # Flask's signed session is the authentication proof after /api/auth.
        # Avoid an extra SELECT/open SQLite connection on every game click.
        if not session.get('uid'):
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
                balance=user['balance'] / 100, turnover=user['turnover_cents']/100,
                admin=user['id'] in ADMIN_IDS)


def level_number(db, turnover):
    row = db.execute('SELECT MAX(level) AS n FROM levels WHERE required_turnover<=?', (turnover,)).fetchone()
    return int(row['n'] or 1)


def increase_turnover(db, user_id, amount):
    if amount <= 0:
        return None
    user = db.execute('SELECT turnover_cents FROM users WHERE id=?', (user_id,)).fetchone()
    previous_turnover = int(user['turnover_cents'] or 0)
    previous = level_number(db, previous_turnover)
    new_turnover = previous_turnover + amount
    db.execute('UPDATE users SET turnover_cents=turnover_cents+? WHERE id=?', (amount, user_id))
    current = level_number(db, new_turnover)
    return current if current > previous else None


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


def game_rtp():
    """Long-run payout ratio used for standard Mines rounds.

    With uniformly sampled mines and a mandatory first-step multiplier >= 1.01x,
    a global RTP below ~97% is mathematically incompatible with the 1-mine mode.
    Keep standard play in the 97–99.9% range instead of secretly biasing outcomes.
    """
    try:
        doc = read_document('game_settings') or {}
        value = float(doc.get('rtp', GAME_RTP_DEFAULT))
    except (TypeError, ValueError, OSError, json.JSONDecodeError):
        value = GAME_RTP_DEFAULT
    return min(0.999, max(MIN_GAME_RTP, value))


def promo_game_rtp():
    """Separate, visibly lower payout curve for promo-wager gifts.

    Promo gifts can only be played with 3+ mines, so 89% still keeps the first
    visible cashout multiplier at or above 1.01x for the 3-mine mode.
    """
    try:
        doc = read_document('game_settings') or {}
        value = float(doc.get('promo_rtp', PROMO_RTP_DEFAULT))
    except (TypeError, ValueError, OSError, json.JSONDecodeError):
        value = PROMO_RTP_DEFAULT
    return min(max(MIN_PROMO_RTP, value), min(0.969, game_rtp() - 0.001))


def round_rtp(row):
    try:
        snap = row['rtp_snapshot']
        if snap is not None and float(snap) > 0:
            return float(snap)
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    try:
        bet_type = row['bet_type']
    except (KeyError, TypeError, IndexError):
        bet_type = 'ton'
    return promo_game_rtp() if bet_type == 'promo_gift' else game_rtp()


def record_transaction(db, user_id, kind, amount=0, reference_type='', reference_id='', details=''):
    row = db.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()
    balance_after = row['balance'] if row else None
    db.execute('''INSERT INTO transactions(user_id,kind,amount,balance_after,reference_type,reference_id,details)
                  VALUES(?,?,?,?,?,?,?)''',
               (user_id, str(kind)[:60], int(amount or 0), balance_after,
                str(reference_type)[:60], str(reference_id)[:120], str(details)[:500]))


def log_event(db,user_id,kind,**details):
    db.execute('INSERT INTO user_events(user_id,kind,payload) VALUES(?,?,?)',
               (user_id,kind,json.dumps(details,ensure_ascii=False)))


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


def repair_legacy_upgrade_wagers():
    """Restore wager gifts incorrectly changed into upgrade targets by older releases."""
    try:catalog=read_catalog().get('gifts',[])
    except (OSError,ValueError,TypeError):catalog=[]
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        items=db.execute("""SELECT * FROM inventory WHERE promo_locked=1
                            AND source IN ('upgrade','promo_wager')""").fetchall()
        for item in items:
            original=None
            if item['promo_code']:
                promo=db.execute("""SELECT gift_id,gift_name,gift_image_url,gift_price FROM promo_codes
                                    WHERE code=? AND reward_type='wager_gift'""",(item['promo_code'],)).fetchone()
                if promo and promo['gift_id']!=item['gift_id']:
                    original=(promo['gift_id'],promo['gift_name'],promo['gift_image_url'],int(promo['gift_price']))
            if original is None and item['source']=='upgrade':
                spins=db.execute('SELECT result_json,source_name,source_image,source_price FROM upgrade_spins WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(item['user_id'],)).fetchall()
                spin=None
                for candidate in spins:
                    try:awarded=json.loads(candidate['result_json']).get('awarded_inventory_id')
                    except (ValueError,TypeError,AttributeError):continue
                    if awarded==item['id']:
                        spin=candidate
                        break
                if spin:
                    matches=[]
                    for gift in catalog:
                        try:price=ton_to_cents(gift['price_ton'])
                        except (KeyError,ValueError,TypeError,InvalidOperation):continue
                        if gift.get('name')==spin['source_name'] and price==spin['source_price']:
                            matches.append(gift)
                    if len(matches)==1:
                        gift=matches[0]
                        original=(str(gift['id']),spin['source_name'],spin['source_image'],int(spin['source_price']))
            if not original or original[3]<1:continue
            target=round(original[3]*float(item['promo_wager_multiplier'] or 0))
            if target<1:continue
            progress=min(target,int(item['promo_wager_progress'] or 0))
            db.execute("""UPDATE inventory SET gift_id=?,gift_name=?,image_url=?,floor_price=?,
                          promo_wager_target=?,promo_wager_progress=?,source='upgrade_wager_repaired'
                          WHERE id=? AND user_id=?""",original+(target,progress,item['id'],item['user_id']))
            log_event(db,item['user_id'],'upgrade_wager_repaired',inventory_id=item['id'],gift_name=original[1])
        db.commit()
    finally:db.close()


def collection_key(name):
    return re.sub(r'[\W_]+', '', str(name).casefold(), flags=re.UNICODE)


def gift_id_map():
    """Load the authoritative Telegram gift ID/name map; reuse a disk copy on outage."""
    path = DATA / 'gift_id_to_name.json'
    try:
        response = requests.get('https://cdn.changes.tg/gifts/id-to-name.json', timeout=(4, 7))
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


def match_collection_image(gift, mapping, names=None):
    names = names or {collection_key(v): k for k, v in mapping.items()}
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
    names = {collection_key(v): k for k, v in mapping.items()}
    with connect() as db:
        for item in db.execute('SELECT id,gift_name,image_url FROM inventory').fetchall():
            matched = match_collection_image({'name': item['gift_name']}, mapping, names)
            if matched['image_match'] and matched['image_url'] != item['image_url']:
                db.execute('UPDATE inventory SET image_url=? WHERE id=?',
                           (matched['image_url'], item['id']))


def ton_to_cents(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid TON amount')
    if not amount.is_finite() or amount < 0:
        raise ValueError('Invalid TON amount')
    return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


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
            price = ton_to_cents(gift['price_ton'])
            if price > 0 and price <= amount:
                eligible.append((price, gift))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return max(eligible, key=lambda x: (x[0], str(x[1].get('id', ''))))[1] if eligible else None


def multiplier_for(mines, opened_count, rtp=None):
    if opened_count <= 0:
        return 1.0
    if mines < MIN_MINES or mines > MAX_MINES or opened_count > 25 - mines:
        raise ValueError('Invalid Mines step')
    rtp = game_rtp() if rtp is None else float(rtp)
    fair = Decimal(math.comb(25, opened_count)) / Decimal(math.comb(25 - mines, opened_count))
    raw = fair * Decimal(str(rtp))
    value = max(Decimal('1.01'), raw)
    return float(value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP))


def payout_for(row, opened_count, rtp=None):
    factor = Decimal(str(multiplier_for(row['mines'], opened_count, rtp)))
    return int((Decimal(int(row['bet'])) * factor).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def inventory_item(row):
    target = int(row['promo_wager_target'] or 0)
    progress = int(row['promo_wager_progress'] or 0)
    locked = bool(row['promo_locked'])
    return dict(id=row['id'], gift_id=row['gift_id'], name=row['gift_name'],
                image_url=row['image_url'], price_ton=row['floor_price']/100,
                source=row['source'], created_at=row['created_at'],
                promo_locked=locked, promo_code=row['promo_code'] or '',
                wager_multiplier=float(row['promo_wager_multiplier'] or 0),
                wager_target=target/100, wager_progress=progress/100,
                wager_complete=bool(locked and target > 0 and progress >= target),
                wager_percent=(min(100.0, progress * 100.0 / target) if target > 0 else 0.0))


def award_round(db, row, opened_count):
    """Settle once, atomically, including promo-wager gift bets."""
    rtp = round_rtp(row)
    factor = multiplier_for(row['mines'], opened_count, rtp)
    amount = payout_for(row, opened_count, rtp)

    if row['bet_type'] == 'promo_gift':
        target = max(0, int(row['promo_wager_target'] or 0))
        previous = max(0, int(row['promo_wager_progress'] or 0))
        progress = min(target, previous + amount) if target else previous + amount
        cursor = db.execute("""INSERT INTO inventory(
                                user_id,gift_id,gift_name,image_url,floor_price,source,round_id,
                                promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code)
                              VALUES(?,?,?,?,?,'promo_wager',?,1,?,?,?,?)""",
                            (row['user_id'], row['bet_gift_id'], row['bet_gift_name'], row['bet_gift_image'],
                             row['bet_gift_price'], row['id'], float(row['promo_wager_multiplier'] or 0),
                             target, progress, row['promo_code'] or ''))
        db.execute("""UPDATE rounds SET state='won',payout=0,prize_inventory_id=?,win_total=?,win_multiplier=?,
                      promo_progress_after=?,win_gift_name='',win_gift_image='',win_gift_price=NULL WHERE id=?""",
                   (cursor.lastrowid, amount, factor, progress, row['id']))
        record_transaction(db, row['user_id'], 'promo_wager_progress', amount, 'round', row['id'],
                           f'Отыгрыш {row["bet_gift_name"]}: {progress/100:.2f}/{target/100:.2f} TON')
        return

    prize = prize_for(amount)
    if prize:
        cents = ton_to_cents(prize['price_ton'])
        remainder = max(0, amount - cents)
        image_url = safe_image(prize.get('image_url'))
        cursor = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                               VALUES(?,?,?,?,?,'game',?)""",
                            (row['user_id'], str(prize['id']), str(prize['name']),
                             image_url, cents, row['id']))
        db.execute("""UPDATE rounds
                      SET state='won',payout=?,prize_inventory_id=?,win_total=?,win_multiplier=?,
                          win_gift_name=?,win_gift_image=?,win_gift_price=?
                      WHERE id=?""",
                   (remainder, cursor.lastrowid, amount, factor, str(prize['name'])[:140],
                    image_url, cents, row['id']))
        if remainder:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (remainder, row['user_id']))
            record_transaction(db, row['user_id'], 'game_win_ton', remainder, 'round', row['id'],
                               f'Остаток после выигрыша подарка: {prize["name"]}')
        record_transaction(db, row['user_id'], 'gift_win', 0, 'round', row['id'], str(prize['name']))
    else:
        db.execute("""UPDATE rounds SET state='won',payout=?,win_total=?,win_multiplier=?,
                      win_gift_name='',win_gift_image='',win_gift_price=NULL WHERE id=?""",
                   (amount, amount, factor, row['id']))
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, row['user_id']))
        record_transaction(db, row['user_id'], 'game_win_ton', amount, 'round', row['id'], 'Выигрыш Mines')


def round_view(row, reveal=False):
    if not row:
        return None
    opened = json.loads(row['opened'])
    rtp = round_rtp(row)
    if row['state'] == 'won' and row['win_multiplier'] is not None:
        factor = float(row['win_multiplier'])
        amount = int(row['win_total'] if row['win_total'] is not None else row['payout'])
    else:
        factor = multiplier_for(row['mines'], len(opened), rtp) if opened else 1
        amount = payout_for(row, len(opened), rtp) if opened else row['bet']
    is_promo = row['bet_type'] == 'promo_gift'
    prize = prize_for(amount) if opened and row['state'] == 'active' and not is_promo else None
    owned = None
    if row['prize_inventory_id']:
        with connect() as db:
            item = db.execute('SELECT * FROM inventory WHERE id=?', (row['prize_inventory_id'],)).fetchone()
            owned = inventory_item(item) if item else None
    bet_gift = None
    if row['bet_type'] in ('gift', 'promo_gift'):
        bet_gift = dict(id=row['bet_inventory_id'], gift_id=row['bet_gift_id'], name=row['bet_gift_name'],
                        image_url=row['bet_gift_image'], price_ton=row['bet_gift_price']/100,
                        promo_locked=is_promo, wager_multiplier=float(row['promo_wager_multiplier'] or 0),
                        wager_target=int(row['promo_wager_target'] or 0)/100,
                        wager_progress=int(row['promo_wager_progress'] or 0)/100)
    return dict(id=row['id'], bet=row['bet']/100, bet_type=row['bet_type'], bet_gift=bet_gift,
                mines=row['mines'], opened=opened, state=row['state'],
                multiplier=round(factor, 6), potential=amount/100,
                positions=json.loads(row['positions']) if reveal or row['state'] != 'active' else [],
                payout=row['payout']/100, prize=prize, awarded=owned, lost_cell=row['lost_cell'],
                promo_progress_after=int(row['promo_progress_after'] or 0)/100)


@app.get('/api/game/ladder')
@login_required
def ladder():
    try:
        mines = int(request.args.get('mines', '3'))
        bet = parse_amount(request.args.get('bet', '0.1'))
    except (ValueError, InvalidOperation, TypeError):
        return error('Неверные параметры.')
    if not (MIN_MINES <= mines <= MAX_MINES and MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
        return error('Неверные параметры.')
    try:
        gifts = read_catalog()['gifts']
    except (OSError, ValueError):
        gifts = []
    # Use the same RTP snapshot as an active round so the ladder cannot change mid-game.
    dummy = {'bet': bet, 'mines': mines}
    promo_mode = request.args.get('promo') == '1'
    current_rtp = promo_game_rtp() if promo_mode else game_rtp()
    with connect() as db:
        active = active_round(db, session['uid'])
    if active and int(active['mines']) == mines and int(active['bet']) == bet:
        current_rtp = round_rtp(active)
        promo_mode = active['bet_type'] == 'promo_gift'
    return jsonify(levels=[dict(step=step, multiplier=multiplier_for(mines, step, current_rtp),
                                amount=payout_for(dummy, step, current_rtp)/100,
                                prize=(None if promo_mode else prize_for(payout_for(dummy, step, current_rtp), gifts)))
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


def roll_config(db):
    row = db.execute("SELECT payload FROM app_documents WHERE name='roll_config'").fetchone()
    return json.loads(row['payload']) if row else []


def public_rolls(rolls):
    return [dict(id=r['id'], name=r['name'], price_ton=r['price']/100,
                 entries=[dict(id=e['id'], kind=e['kind'], name=e['name'],
                               image_url=e.get('image_url', ''), weight=e['weight'],
                               probability=round(100*e['weight']/sum(x['weight'] for x in r['entries']), 2),
                               boost=e.get('boost', 1), price_ton=e.get('price', 0)/100) for e in r['entries']]) for r in sorted(rolls, key=lambda r:r['price'])]


def normalize_level_reward(data):
    if not isinstance(data, dict):
        raise ValueError('Неверная настройка награды.')
    kind = str(data.get('type') or 'none')
    if kind not in ('none','balance','gift','wager_gift','personal_promo','deposit_promo','multi_promo','transfer_unlock'):
        raise ValueError('Неизвестный тип награды.')
    reward = {'type':kind}
    try:
        expires_days = int(data.get('expires_days') or 0)
    except (TypeError, ValueError):
        raise ValueError('Срок промокода должен быть указан в днях.')
    if not 0 <= expires_days <= 3650:
        raise ValueError('Срок промокода: от 0 до 3650 дней. 0 — без срока.')
    if kind in ('none','transfer_unlock'):
        return reward
    if kind=='multi_promo':
        components=data.get('components')
        if not isinstance(components,dict) or not components or len(components)>4:
            raise ValueError('Выберите хотя бы одну награду мультипромокода.')
        allowed={'balance','gift','wager_gift','deposit_bonus'}
        if not set(components)<=allowed:raise ValueError('Неизвестная награда мультипромокода.')
        resolved={}
        for name,config in components.items():
            if not isinstance(config,dict):raise ValueError('Проверьте настройки мультипромокода.')
            resolved[name]=normalize_level_reward({**config,'type':'deposit_promo' if name=='deposit_bonus' else name})
        return {'type':'multi_promo','components':resolved,'expires_days':expires_days}
    content = str(data.get('promo_reward_type') or 'balance') if kind=='personal_promo' else kind
    if content in ('balance','gift','wager_gift'):
        if content=='balance':
            try: amount=parse_amount(data.get('amount'))
            except (ValueError,InvalidOperation,TypeError): raise ValueError('Введите сумму награды в TON.')
            if not 1<=amount<=100000000: raise ValueError('Сумма награды вне допустимых пределов.')
            reward['amount']=amount
        else:
            gift_id=str(data.get('gift_id') or '')
            gift=next((x for x in read_catalog().get('gifts',[]) if str(x.get('id'))==gift_id),None)
            if not gift: raise ValueError('Выберите подарок из каталога Portal.')
            try: price=ton_to_cents(gift['price_ton'])
            except (KeyError,ValueError,TypeError,InvalidOperation): raise ValueError('У подарка нет цены Portal.')
            if price<1: raise ValueError('У подарка нет цены Portal.')
            reward.update(gift_id=gift_id,gift_name=str(gift.get('name') or 'Подарок')[:140],
                          image_url=safe_image(gift.get('image_url')),gift_price=price)
            if content=='wager_gift':
                try: multiplier=float(data.get('wager_multiplier'))
                except (TypeError,ValueError): raise ValueError('Укажите X отыгрыша.')
                if not math.isfinite(multiplier) or not 1<=multiplier<=1000: raise ValueError('X отыгрыша: 1–1000.')
                reward['wager_multiplier']=multiplier
    elif content=='deposit_promo':
        try:
            percent=float(data.get('bonus_percent') or 0)
            fixed=parse_amount(data.get('bonus_fixed') or '0')
            minimum=parse_amount(data.get('min_deposit') or '0')
        except (TypeError,ValueError,InvalidOperation): raise ValueError('Проверьте бонус к депозиту.')
        if not math.isfinite(percent) or not 0<=percent<=100 or not 0<=fixed<=1000000 or not 0<=minimum<=100000000 or not (percent or fixed):
            raise ValueError('Бонус: 1–100% или 0.01–10000 TON; минимальный депозит до 1 000 000 TON.')
        if percent and fixed: raise ValueError('Выберите один вид бонуса: процент или фиксированную сумму.')
        reward.update(bonus_percent=percent,bonus_fixed=fixed,min_deposit=minimum)
    else:
        raise ValueError('Выберите содержимое личного промокода.')
    if kind=='personal_promo': reward['promo_reward_type']=content
    if kind in ('personal_promo','deposit_promo'):
        reward['expires_days']=expires_days
    return reward


def public_level_reward(reward):
    result={'type':'none',**reward}
    if isinstance(result.get('components'),dict):
        result['components']={k:public_level_reward(v) for k,v in result['components'].items()}
    for key in ('amount','gift_price','bonus_fixed','min_deposit'):
        if key in result:result[key+'_ton']=result.pop(key)/100
    return result


@app.get('/api/levels')
@login_required
def user_levels():
    with connect() as db:
        turnover=int(db.execute('SELECT turnover_cents FROM users WHERE id=?',(session['uid'],)).fetchone()['turnover_cents'] or 0)
        rows=db.execute('SELECT * FROM levels ORDER BY level').fetchall()
        claims={r['level']:json.loads(r['reward_json']) for r in db.execute('SELECT level,reward_json FROM level_claims WHERE user_id=?',(session['uid'],)).fetchall()}
    level=max((int(r['level']) for r in rows if turnover>=r['required_turnover']),default=1)
    current=next(r for r in rows if r['level']==level)
    nxt=next((r for r in rows if r['level']>level),None)
    progress=100 if not nxt else max(0,min(100,(turnover-current['required_turnover'])*100/(nxt['required_turnover']-current['required_turnover'])))
    return jsonify(level=level,max_level=len(rows),turnover=turnover/100,
                   next_turnover=nxt['required_turnover']/100 if nxt else None,progress=round(progress,1),
                   pending=sum(1 for r in rows if r['required_turnover']<=turnover and r['level'] not in claims and json.loads(r['reward_json']).get('type','none')!='none'),
                   levels=[dict(level=r['level'],required_turnover=r['required_turnover']/100,
                                reward=public_level_reward(json.loads(r['reward_json'])),
                                unlocked=r['required_turnover']<=turnover,claimed=r['level'] in claims,
                                claim=claims.get(r['level'])) for r in rows])


@app.get('/api/admin/levels')
@admin_required
def admin_levels():
    with connect() as db:
        rows=db.execute('SELECT * FROM levels ORDER BY level').fetchall()
    return jsonify(levels=[dict(level=r['level'],required_turnover=r['required_turnover']/100,
                                reward=public_level_reward(json.loads(r['reward_json']))) for r in rows])


@app.post('/api/admin/levels/<int:level>')
@admin_required
def admin_save_level(level):
    if not 1<=level<=20:return error('Уровень: от 1 до 20.')
    data=request.get_json(silent=True) or {}
    try:
        threshold=parse_amount(data.get('required_turnover'))
        reward=normalize_level_reward(data.get('reward') or {'type':'none'})
    except (ValueError,InvalidOperation,TypeError) as exc:return error(str(exc))
    if threshold<0 or threshold>10000000000:return error('Слишком большой оборот.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        prev=db.execute('SELECT required_turnover FROM levels WHERE level=?',(level-1,)).fetchone() if level>1 else None
        nxt=db.execute('SELECT required_turnover FROM levels WHERE level=?',(level+1,)).fetchone() if level<20 else None
        if level==1 and threshold!=0:return error('Первый уровень начинается с нулевого оборота.')
        if prev and threshold<=prev['required_turnover'] or nxt and threshold>=nxt['required_turnover']:
            return error('Порог должен быть больше предыдущего и меньше следующего уровня.')
        db.execute('UPDATE levels SET required_turnover=?,reward_json=? WHERE level=?',
                   (threshold,json.dumps(reward,ensure_ascii=False),level))
        db.commit()
    finally:db.close()
    return jsonify(ok=True,level=level,reward=public_level_reward(reward))


@app.post('/api/admin/levels/bulk')
@admin_required
def admin_save_levels_bulk():
    data=request.get_json(silent=True) or {}
    items=data.get('levels')
    if not isinstance(items,list) or len(items)!=20:return error('Передайте все 20 уровней.')
    prepared=[]
    try:
        for index,item in enumerate(items,1):
            if not isinstance(item,dict) or int(item.get('level',0))!=index:
                return error('Уровни должны идти по порядку от 1 до 20.')
            threshold=parse_amount(item.get('required_turnover'))
            if threshold<0 or threshold>10000000000:return error('Слишком большой оборот.')
            reward=normalize_level_reward(item.get('reward') or {'type':'none'})
            prepared.append((index,threshold,json.dumps(reward,ensure_ascii=False)))
    except (ValueError,InvalidOperation,TypeError) as exc:return error(str(exc))
    if prepared[0][1]!=0:return error('Первый уровень начинается с нулевого оборота.')
    if any(right[1]<=left[1] for left,right in zip(prepared,prepared[1:])):
        return error('Пороги уровней должны возрастать.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        for level,threshold,reward_json in prepared:
            db.execute('UPDATE levels SET required_turnover=?,reward_json=? WHERE level=?',
                       (threshold,reward_json,level))
        db.commit()
    finally:db.close()
    return jsonify(ok=True)


LEVEL_PROMO_TYPES = ('personal_promo', 'deposit_promo', 'multi_promo')


def create_level_promo(db, user_id, level, reward):
    kind = reward.get('type', 'none')
    if kind not in LEVEL_PROMO_TYPES:
        return None
    promo_type = ('multi' if kind == 'multi_promo' else
                  reward.get('promo_reward_type', 'balance') if kind == 'personal_promo' else
                  'deposit_bonus')
    code = 'LV' + str(level) + '-' + secrets.token_hex(6).upper()
    deposit = reward.get('components', {}).get('deposit_bonus', {}) if kind == 'multi_promo' else reward
    expires_days = int(reward.get('expires_days') or 0)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat() if expires_days else None
    description = reward_description(reward)
    db.execute('''INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,assigned_user_id,source_label,description,expires_at)
                  VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)''',
               (code, promo_type, reward.get('amount', 0), reward.get('gift_id', ''), reward.get('gift_name', ''),
                reward.get('image_url', ''), reward.get('gift_price', 0), reward.get('wager_multiplier', 0), 0,
                deposit.get('bonus_percent', 0), deposit.get('bonus_fixed', 0), deposit.get('min_deposit', 0),
                json.dumps(reward, ensure_ascii=False) if kind == 'multi_promo' else '{}', user_id,
                f'Награда за уровень {level}', description, expires_at))
    return dict(type=kind, code=code, description=description, expires_at=expires_at)


def sync_unlocked_level_promos(db, user_id, turnover=None):
    if turnover is None:
        row = db.execute('SELECT turnover_cents FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return []
        turnover = int(row['turnover_cents'] or 0)
    rows = db.execute('SELECT level,reward_json FROM levels WHERE required_turnover<=? ORDER BY level',
                      (int(turnover),)).fetchall()
    issued = []
    for row in rows:
        level = int(row['level'])
        if db.execute('SELECT 1 FROM level_claims WHERE user_id=? AND level=?', (user_id, level)).fetchone():
            continue
        try:
            reward = json.loads(row['reward_json'] or '{}')
        except (ValueError, TypeError):
            continue
        if reward.get('type') not in LEVEL_PROMO_TYPES:
            continue
        result = create_level_promo(db, user_id, level, reward)
        if not result:
            continue
        db.execute('INSERT INTO level_claims(user_id,level,reward_json) VALUES(?,?,?)',
                   (user_id, level, json.dumps(result, ensure_ascii=False)))
        log_event(db, user_id, 'level_claim', level=level, reward=result, automatic=True)
        issued.append(result)
    return issued


@app.post('/api/levels/<int:level>/claim')
@login_required
def claim_level(level):
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM levels WHERE level=?',(level,)).fetchone()
        user=db.execute('SELECT turnover_cents FROM users WHERE id=?'+(' FOR UPDATE' if DATABASE_URL else ''),(session['uid'],)).fetchone()
        if not row or not user:return error('Уровень не найден.',404)
        if user['turnover_cents']<row['required_turnover']:return error('Достигните уровня, чтобы получить награду.',403)
        if db.execute('SELECT 1 FROM level_claims WHERE user_id=? AND level=?',(session['uid'],level)).fetchone():return error('Награда уже получена.',409)
        reward=json.loads(row['reward_json']);kind=reward.get('type','none')
        if kind=='none':return error('На этом уровне награда не назначена.')
        result={}
        if kind=='balance':
            amount=int(reward['amount']);db.execute('UPDATE users SET balance=balance+? WHERE id=?',(amount,session['uid']))
            record_transaction(db,session['uid'],'level_balance',amount,'level',level,f'Уровень {level}')
            result=dict(type='balance',amount=amount/100)
        elif kind in ('gift','wager_gift'):
            item=(session['uid'],reward['gift_id'],reward['gift_name'],reward['image_url'],reward['gift_price'])
            if kind=='gift':
                cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'level')",item)
            else:
                mult=reward['wager_multiplier'];target=round(reward['gift_price']*mult)
                cur=db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress)
                                  VALUES(?,?,?,?,?,'level_wager',1,?,?,0)""",item+(mult,target))
            result=dict(type=kind,gift=dict(id=cur.lastrowid,name=reward['gift_name'],image_url=reward['image_url'],
                        price_ton=reward['gift_price']/100,promo_locked=kind=='wager_gift',wager_multiplier=reward.get('wager_multiplier',0)))
        elif kind in LEVEL_PROMO_TYPES:
            result=create_level_promo(db,session['uid'],level,reward)
        elif kind=='transfer_unlock':
            result=dict(type='transfer_unlock',description='Переводы TON разблокированы')
        db.execute('INSERT INTO level_claims(user_id,level,reward_json) VALUES(?,?,?)',
                   (session['uid'],level,json.dumps(result,ensure_ascii=False)))
        log_event(db,session['uid'],'level_claim',level=level,reward=result)
        db.commit()
        return jsonify(ok=True,reward=result,user=profile())
    finally:db.close()


def reward_description(reward):
    if reward.get('type')=='none':return 'Без награды'
    if reward.get('type')=='transfer_unlock':return 'Доступ к переводам TON'
    if reward.get('type')=='multi_promo':return 'Мультипромокод · '+', '.join(reward.get('components',{}))
    if reward.get('type')=='balance' or reward.get('promo_reward_type')=='balance' and reward.get('type')=='personal_promo':
        return f"{reward.get('amount',0)/100:.2f} TON"
    if reward.get('type')=='deposit_promo':
        pct=reward.get('bonus_percent',0)
        return f'+{pct:g}% к депозиту' if pct else f"+{reward.get('bonus_fixed',0)/100:.2f} TON к депозиту от {reward.get('min_deposit',0)/100:.2f} TON"
    return reward.get('gift_name','Подарок')


@app.get('/api/rolls')
@login_required
def list_rolls():
    with connect() as db:
        rolls = roll_config(db)
        user = db.execute('SELECT roll_boost FROM users WHERE id=?', (session['uid'],)).fetchone()
    return jsonify(rolls=public_rolls(rolls), boost=float(user['roll_boost'] or 1))


@app.get('/api/admin/rolls')
@admin_required
def admin_rolls():
    with connect() as db:
        return jsonify(rolls=roll_config(db))


@app.post('/api/admin/rolls')
@admin_required
def admin_save_rolls():
    data = request.get_json(silent=True) or {}
    incoming = data.get('rolls')
    if not isinstance(incoming, list) or len(incoming)>30:
        return error('Допускается не более 30 роллов.')
    with connect() as db:
        catalog = {str(g.get('id')):g for g in read_catalog().get('gifts', [])}
        rolls = []
        ids = set()
        for raw in incoming:
            if not isinstance(raw, dict): return error('Неверный формат ролла.')
            rid = str(raw.get('id') or secrets.token_hex(8))
            name = str(raw.get('name') or '').strip()[:50]
            try:
                price = parse_amount(raw.get('price_ton'))
            except (ValueError, InvalidOperation, TypeError):
                return error('Укажите корректную цену Roll.')
            if not name or rid in ids or not 1 <= price <= MAX_BET_CENTS:
                return error('Название или цена Roll указаны неверно.')
            ids.add(rid)
            raw_entries = raw.get('entries')
            if not isinstance(raw_entries, list) or not 2 <= len(raw_entries) <= 24:
                return error('У Roll должно быть от 2 до 24 секторов.')
            entries=[]
            for item in raw_entries:
                if not isinstance(item,dict): return error('Неверный сектор.')
                kind = item.get('kind')
                try:
                    weight=int(item.get('weight'))
                    boost=float(item.get('boost') or 1)
                except (TypeError,ValueError,OverflowError):
                    return error('Проверьте шанс и Boost.')
                if kind not in ('gift','empty','boost') or not 1<=weight<=10000 or not math.isfinite(boost) or not 1<=boost<=3:
                    return error('Шанс: 1–10000; Boost: от 1 до 3.')
                eid=str(item.get('id') or secrets.token_hex(8))
                if kind=='gift':
                    gift=catalog.get(str(item.get('gift_id')))
                    if not gift or not gift.get('name') or not gift.get('image_match') or not gift.get('price_ton'):
                        return error('Подарок отсутствует в каталоге Portal или его PNG не найден.')
                    entries.append(dict(id=eid,kind=kind,weight=weight,gift_id=str(gift['id']),
                                        name=str(gift['name'])[:140],image_url=safe_image(gift.get('image_url')),
                                        price=ton_to_cents(gift['price_ton'])))
                else:
                    entries.append(dict(id=eid,kind=kind,weight=weight,
                                        name='Boost ×'+f'{boost:g}' if kind=='boost' else 'Без подарка',
                                        image_url='',boost=boost if kind=='boost' else 1))
            rolls.append(dict(id=rid,name=name,price=price,entries=entries))
        db.execute("INSERT INTO app_documents(name,payload) VALUES('roll_config',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                   (json.dumps(rolls,ensure_ascii=False),))
    return jsonify(rolls=rolls)


@app.post('/api/rolls/<roll_id>/spin')
@login_required
def spin_roll(roll_id):
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        rolls=roll_config(db)
        roll=next((r for r in rolls if r['id']==roll_id),None)
        if not roll: return error('Roll не найден.',404)
        lock=' FOR UPDATE' if DATABASE_URL else ''
        player=db.execute('SELECT balance,roll_boost FROM users WHERE id=?'+lock,(session['uid'],)).fetchone()
        if not player: return error('Пользователь не найден.',404)
        if player['balance']<roll['price']: return error('Недостаточно TON. Пополните баланс.')
        boost=max(1,min(3,float(player['roll_boost'] or 1)))
        weights=[max(1,round(e['weight']*(boost if e['kind']=='gift' else 1))) for e in roll['entries']]
        ticket=secrets.randbelow(sum(weights))
        index=0
        for index,weight in enumerate(weights):
            if ticket<weight: break
            ticket-=weight
        entry=roll['entries'][index]
        new_boost=entry['boost'] if entry['kind']=='boost' else 1
        updated=db.execute('UPDATE users SET balance=balance-?,roll_boost=? WHERE id=? AND balance>=?',
                           (roll['price'],new_boost,session['uid'],roll['price']))
        if not updated.rowcount: return error('Недостаточно TON.')
        spin_id=secrets.token_hex(16)
        if entry['kind']=='gift':
            db.execute('INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,?)',
                       (session['uid'],entry['gift_id'],entry['name'],entry['image_url'],entry['price'],'roll'))
        db.execute('INSERT INTO roll_spins(id,user_id,roll_id,price,outcome,gift_name) VALUES(?,?,?,?,?,?)',
                   (spin_id,session['uid'],roll_id,roll['price'],entry['kind'],entry['name'] if entry['kind']=='gift' else ''))
        record_transaction(db,session['uid'],'roll_spin',-roll['price'],'roll',spin_id,roll['name'])
        if entry['kind']=='gift':
            record_transaction(db,session['uid'],'roll_gift',0,'roll',spin_id,entry['name'])
        log_event(db,session['uid'],'roll',name=roll['name'],price=roll['price']/100,
                  outcome=entry['kind'],gift_name=entry['name'],gift_image=entry.get('image_url',''))
        new_level=increase_turnover(db,session['uid'],roll['price'])
        db.commit()
        return jsonify(spin_id=spin_id,entry_id=entry['id'],index=index,kind=entry['kind'],
                       name=entry['name'],image_url=entry.get('image_url',''),boost=new_boost,
                       applied_boost=boost,new_level=new_level,user=profile())
    finally:
        db.close()


def upgrade_target(gift_id):
    gift=next((g for g in read_catalog().get('gifts',[]) if str(g.get('id'))==str(gift_id)),None)
    if not gift:return None
    try:price=ton_to_cents(gift['price_ton'])
    except (ValueError,TypeError,KeyError,InvalidOperation):return None
    if price<1:return None
    image_url=safe_image(gift.get('image_url'))
    if not image_url:return None
    return dict(id=str(gift['id']),name=str(gift.get('name') or 'Подарок')[:140],
                image_url=image_url,price=price)


def upgrade_chance(source_price,target_price):
    if source_price<1 or target_price<=source_price:return 0
    numerator=upgrade_rtp_basis_points()*source_price
    if numerator<100*target_price or numerator>8000*target_price:return 0
    return round(numerator/target_price)


def upgrade_rtp_basis_points():
    try:return int((read_document('game_settings') or {}).get('upgrade_rtp_bp',9000))
    except (TypeError,ValueError):return 9000


@app.get('/api/upgrade/settings')
@login_required
def upgrade_settings():
    return jsonify(rtp=upgrade_rtp_basis_points()/100,min_chance=1,max_chance=80)


@app.get('/api/upgrade/preview')
@login_required
def upgrade_preview():
    amount_text=request.args.get('amount')
    item_text=request.args.get('inventory_id')
    if bool(amount_text)==bool(item_text):return error('Выберите TON или подарок для ставки.')
    if amount_text:
        try:source_price=parse_amount(amount_text)
        except (ValueError,InvalidOperation,TypeError):return error('Укажите ставку в TON с точностью до 0.01.')
        if not 10<=source_price<=100000000:return error('Ставка TON: от 0.10 до 1 000 000.')
        with connect() as db:
            balance=db.execute('SELECT balance FROM users WHERE id=?',(session['uid'],)).fetchone()['balance']
        if balance<source_price:return error('Недостаточно TON для ставки.')
        source_view=dict(type='ton',id=None,name='TON',image_url='/static/img/ton.png',price_ton=source_price/100)
    else:
        try:source_id=int(item_text)
        except (ValueError,TypeError):return error('Выберите свой подарок.')
        with connect() as db:
            source=db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?',(source_id,session['uid'])).fetchone()
        if not source:return error('Выберите доступный подарок из инвентаря.')
        if source['promo_locked'] and int(source['promo_wager_progress'] or 0)>=int(source['promo_wager_target'] or 0):
            return error('Отыгрыш завершён — сначала получите подарок.')
        source_price=int(source['floor_price'] or 0)
        source_view=dict(type='gift',**inventory_item(source))
    target=upgrade_target(request.args.get('gift_id'))
    if not target:return error('Целевой подарок не найден в каталоге Portal.')
    chance=upgrade_chance(source_price,target['price'])
    if not chance:return error('Цена цели должна давать шанс от 1% до 80%.')
    return jsonify(source=source_view,target=dict(id=target['id'],name=target['name'],
                   image_url=target['image_url'],price_ton=target['price']/100),chance=chance/100,
                   probability=chance/10000,rtp=upgrade_rtp_basis_points()/100)


@app.post('/api/upgrade/spin')
@login_required
def upgrade_spin():
    data=request.get_json(silent=True) or {}
    request_id=str(data.get('request_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',request_id):return error('Повторите попытку прокрутки.')
    amount_text=data.get('amount')
    item_text=data.get('inventory_id')
    if bool(amount_text)==bool(item_text):return error('Выберите TON или подарок для ставки.')
    if amount_text:
        try:ton_price=parse_amount(amount_text)
        except (ValueError,InvalidOperation,TypeError):return error('Укажите ставку в TON с точностью до 0.01.')
        if not 10<=ton_price<=100000000:return error('Ставка TON: от 0.10 до 1 000 000.')
    else:
        try:source_id=int(item_text)
        except (ValueError,TypeError):return error('Выберите свой подарок.')
    target=upgrade_target(data.get('gift_id'))
    if not target:return error('Целевой подарок не найден в каталоге Portal.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT user_id,result_json FROM upgrade_spins WHERE id=?',(request_id,)).fetchone()
        if previous:
            if previous['user_id']!=session['uid']:return error('Некорректная операция.',409)
            db.commit()
            return jsonify(**json.loads(previous['result_json']),user=profile())
        if amount_text:
            source_price=ton_price
            source=dict(gift_name='TON',image_url='/static/img/ton.png',floor_price=ton_price,promo_locked=0)
        else:
            source=db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?'+(' FOR UPDATE' if DATABASE_URL else ''),
                              (source_id,session['uid'])).fetchone()
            if not source:return error('Подарок недоступен для апгрейда.',409)
            source_price=int(source['floor_price'] or 0)
            if source['promo_locked'] and int(source['promo_wager_progress'] or 0)>=int(source['promo_wager_target'] or 0):
                return error('Отыгрыш завершён — сначала получите подарок.')
        chance=upgrade_chance(source_price,target['price'])
        if not chance:return error('Цена цели должна давать шанс от 1% до 80%.')
        if amount_text:
            if not db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?',
                              (source_price,session['uid'],source_price)).rowcount:
                return error('Недостаточно TON для ставки.',409)
        elif not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?',(source_id,session['uid'])).rowcount:
            return error('Подарок уже использован.',409)
        won=secrets.randbelow(10000)<chance
        awarded=None
        wager=bool(source['promo_locked'])
        wager_target=int(source['promo_wager_target'] or 0) if wager else 0
        wager_progress=min(wager_target,int(source['promo_wager_progress'] or 0)+target['price']) if wager and won else 0
        compensation=dict(cashback=0,cashback_percent=0,promo=None)
        if won:
            if wager:
                cur=db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                 promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code)
                                 VALUES(?,?,?,?,?,'upgrade_wager',1,?,?,?,?)''',
                               (session['uid'],source['gift_id'],source['gift_name'],source['image_url'],source_price,
                                float(source['promo_wager_multiplier'] or 0),wager_target,wager_progress,source['promo_code'] or ''))
            else:
                cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'upgrade')",
                               (session['uid'],target['id'],target['name'],target['image_url'],target['price']))
            awarded=cur.lastrowid
        else:
            compensation=apply_upgrade_loss_compensation(db,session['uid'],source_price)
        result=dict(ok=True,id=request_id,won=won,chance=chance/100,
                    source_type='ton' if amount_text else 'gift',reward_type='wager_progress' if wager else 'gift',
                    source=dict(name=source['gift_name'],image_url=source['image_url'],price_ton=source_price/100),
                    target=dict(name=target['name'],image_url=target['image_url'],price_ton=target['price']/100,
                                promo_locked=False),
                    wager_progress=wager_progress/100,wager_target=wager_target/100,
                    awarded_inventory_id=awarded,compensation=compensation)
        db.execute('''INSERT INTO upgrade_spins(id,user_id,source_name,source_image,source_price,target_name,target_image,target_price,chance_bp,won,result_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (request_id,session['uid'],source['gift_name'],source['image_url'],source_price,
                    target['name'],target['image_url'],target['price'],chance,int(won),json.dumps(result,ensure_ascii=False)))
        record_transaction(db,session['uid'],'upgrade_bet',-source_price if amount_text else 0,'upgrade',request_id,
                           f'{source["gift_name"]} → {target["name"]} · {chance/100:.2f}% · {"успех" if won else "проигрыш"}')
        log_event(db,session['uid'],'upgrade',source_name=source['gift_name'],source_image=source['image_url'],
                  source_price=source_price/100,target_name=target['name'],target_image=target['image_url'],
                  target_price=target['price']/100,chance=chance/100,won=won,promo_wager=wager,
                  wager_progress=wager_progress/100 if wager and won else None,source_type='ton' if amount_text else 'gift')
        result['new_level']=increase_turnover(db,session['uid'],source_price)
        db.execute('UPDATE upgrade_spins SET result_json=? WHERE id=?',(json.dumps(result,ensure_ascii=False),request_id))
        db.commit()
        return jsonify(**result,user=profile())
    finally:db.close()


def transfer_access(db,user_id):
    rows=db.execute('SELECT reward_json FROM level_claims WHERE user_id=?',(user_id,)).fetchall()
    return any(json.loads(r['reward_json']).get('type')=='transfer_unlock' for r in rows)


def transfer_rate(db,user_id):
    row=db.execute('SELECT turnover_cents FROM users WHERE id=?',(user_id,)).fetchone()
    level=level_number(db,int(row['turnover_cents'] or 0))
    rate=db.execute('SELECT fee_percent,enabled FROM transfer_rates WHERE level=?',(level,)).fetchone()
    return level,float(rate['fee_percent']) if rate else 5.0,bool(rate['enabled']) if rate else True


@app.get('/api/transfers/status')
@login_required
def transfers_status():
    with connect() as db:
        unlocked=transfer_access(db,session['uid'])
        level,fee,enabled=transfer_rate(db,session['uid'])
        incoming=db.execute('SELECT COUNT(*) AS n FROM transfers WHERE recipient_id=? AND seen_at IS NULL',(session['uid'],)).fetchone()['n']
    return jsonify(unlocked=unlocked,enabled=enabled,level=level,fee_percent=fee,
                   min_amount=0.1,unread=incoming)


@app.get('/api/transfers/recipient')
@login_required
def transfer_recipient():
    username=str(request.args.get('username') or '').strip().lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9_]{5,32}',username):return error('Введите Telegram username получателя.')
    with connect() as db:
        users=db.execute('SELECT id,name,username,photo_url FROM users WHERE LOWER(username)=? LIMIT 2',(username,)).fetchall()
    if len(users)!=1:return error('Зарегистрированный пользователь с таким username не найден.',404)
    u=users[0]
    if u['id']==session['uid']:return error('Себе перевести нельзя.')
    return jsonify(user=dict(id=u['id'],name=u['name'],username=u['username'],photo_url=u['photo_url']))


@app.post('/api/transfers/send')
@login_required
def transfer_send():
    data=request.get_json(silent=True) or {}
    request_id=str(data.get('request_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',request_id):return error('Обновите страницу и повторите перевод.')
    try:amount=parse_amount(data.get('amount'))
    except (ValueError,TypeError,InvalidOperation):return error('Введите сумму перевода с точностью до 0.01 TON.')
    if amount<10:return error('Минимальный перевод — 0.10 TON.')
    username=str(data.get('username') or '').strip().lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9_]{5,32}',username):return error('Введите username зарегистрированного получателя.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT * FROM transfers WHERE id=?',(request_id,)).fetchone()
        if previous:
            if previous['sender_id']!=session['uid']:return error('Некорректная операция.',409)
            recipient=db.execute('SELECT name,username,photo_url FROM users WHERE id=?',(previous['recipient_id'],)).fetchone()
            db.commit()
            return jsonify(ok=True,id=request_id,amount=previous['amount']/100,fee=previous['fee']/100,
                           old_balance=previous['sender_before']/100,new_balance=(previous['sender_before']-previous['amount']-previous['fee'])/100,
                           recipient=dict(name=recipient['name'],username=recipient['username'],photo_url=recipient['photo_url']),user=profile())
        users=db.execute('SELECT id,name,username,photo_url FROM users WHERE LOWER(username)=? LIMIT 2',(username,)).fetchall()
        if len(users)!=1 or users[0]['id']==session['uid']:return error('Получатель не найден или совпадает с отправителем.',404)
        recipient=users[0]
        if DATABASE_URL:
            for uid in sorted((session['uid'],recipient['id'])):
                db.execute('SELECT id FROM users WHERE id=? FOR UPDATE',(uid,))
        sender=db.execute('SELECT balance FROM users WHERE id=?',(session['uid'],)).fetchone()
        receiver=db.execute('SELECT balance FROM users WHERE id=?',(recipient['id'],)).fetchone()
        if not sender or not receiver:return error('Получатель не найден.',404)
        if not transfer_access(db,session['uid']):return error('Получите награду уровня с доступом к переводам.',403)
        _,fee_percent,enabled=transfer_rate(db,session['uid'])
        if not enabled:return error('Переводы для вашего уровня отключены.',403)
        fee=int((Decimal(amount)*Decimal(str(fee_percent))/100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        total=amount+fee
        if sender['balance']<total:return error(f'Недостаточно TON с учётом комиссии {fee_percent:g}%.')
        debited=db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?',(total,session['uid'],total))
        if not debited.rowcount:return error('Недостаточно TON.')
        db.execute('UPDATE users SET balance=balance+? WHERE id=?',(amount,recipient['id']))
        db.execute('INSERT INTO transfers(id,sender_id,recipient_id,amount,fee,sender_before,recipient_before) VALUES(?,?,?,?,?,?,?)',
                   (request_id,session['uid'],recipient['id'],amount,fee,sender['balance'],receiver['balance']))
        record_transaction(db,session['uid'],'transfer_sent',-total,'transfer',request_id,f'@{recipient["username"]} · комиссия {fee/100:.2f} TON')
        record_transaction(db,recipient['id'],'transfer_received',amount,'transfer',request_id,f'От пользователя {session["uid"]}')
        log_event(db,session['uid'],'transfer_sent',recipient_id=recipient['id'],username=recipient['username'],amount=amount/100,fee=fee/100)
        log_event(db,recipient['id'],'transfer_received',sender_id=session['uid'],amount=amount/100)
        db.commit()
        return jsonify(ok=True,id=request_id,amount=amount/100,fee=fee/100,
                       old_balance=sender['balance']/100,new_balance=(sender['balance']-total)/100,
                       recipient=dict(name=recipient['name'],username=recipient['username'],photo_url=recipient['photo_url']),user=profile())
    finally:db.close()


@app.get('/api/transfers/incoming')
@login_required
def incoming_transfer():
    with connect() as db:
        row=db.execute('''SELECT t.*,u.name,u.username,u.photo_url FROM transfers t JOIN users u ON u.id=t.sender_id
                          WHERE t.recipient_id=? AND t.seen_at IS NULL ORDER BY t.created_at,t.id LIMIT 1''',(session['uid'],)).fetchone()
    if not row:return jsonify(transfer=None)
    return jsonify(transfer=dict(id=row['id'],amount=row['amount']/100,
                  old_balance=row['recipient_before']/100,new_balance=(row['recipient_before']+row['amount'])/100,
                  sender=dict(name=row['name'],username=row['username'],photo_url=row['photo_url'])))


@app.post('/api/transfers/<transfer_id>/seen')
@login_required
def transfer_seen(transfer_id):
    with connect() as db:
        db.execute('UPDATE transfers SET seen_at=CURRENT_TIMESTAMP WHERE id=? AND recipient_id=? AND seen_at IS NULL',
                   (transfer_id,session['uid']))
    return jsonify(ok=True)


@app.get('/api/admin/transfers/settings')
@admin_required
def transfer_settings_get():
    with connect() as db:
        rows=db.execute('SELECT level,fee_percent,enabled FROM transfer_rates ORDER BY level').fetchall()
    return jsonify(levels=[dict(level=r['level'],fee_percent=float(r['fee_percent']),enabled=bool(r['enabled'])) for r in rows])


@app.post('/api/admin/transfers/settings/<int:level>')
@admin_required
def transfer_settings_set(level):
    if not 1<=level<=20:return error('Неверный уровень.')
    data=request.get_json(silent=True) or {}
    try:fee=float(data.get('fee_percent'))
    except (TypeError,ValueError):return error('Введите комиссию.')
    if not math.isfinite(fee) or not 0<=fee<=30:return error('Комиссия: от 0 до 30%.')
    enabled=int(bool(data.get('enabled',True)))
    with connect() as db:
        db.execute('UPDATE transfer_rates SET fee_percent=?,enabled=? WHERE level=?',(fee,enabled,level))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'transfer_rate',f'Уровень {level}: {fee:g}%, enabled={enabled}'))
    return jsonify(ok=True,level=level,fee_percent=fee,enabled=bool(enabled))


@app.post('/api/game/start')
@login_required
def start():
    data = request.get_json(silent=True) or {}
    try:
        mines = int(data.get('mines'))
    except (ValueError, TypeError):
        return error('Укажите корректное число мин.')
    if not (MIN_MINES <= mines <= MAX_MINES):
        return error('Количество мин: от 1 до 20.')

    inventory_id = data.get('inventory_id')
    try:
        inventory_id = int(inventory_id) if inventory_id not in (None, '') else None
    except (TypeError, ValueError):
        return error('Некорректный подарок для ставки.')

    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if active_round(db, session['uid']):
            return error('Сначала завершите текущую игру.')

        bet_type = 'ton'
        snapshot = dict(item_id=None, gift_id='', name='', image='', price=0,
                        multiplier=0.0, target=0, progress=0, code='')
        if inventory_id is not None:
            lock = ' FOR UPDATE' if DATABASE_URL else ''
            item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?' + lock,
                              (inventory_id, session['uid'])).fetchone()
            if not item:
                return error('Подарок не найден в инвентаре.', 404)
            bet = int(item['floor_price'] or 0)
            if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
                return error('Для ставки подходят подарки стоимостью от 0.10 до 300 TON.')
            target = int(item['promo_wager_target'] or 0)
            progress = int(item['promo_wager_progress'] or 0)
            if item['promo_locked'] and target > 0 and progress >= target:
                return error('Отыгрыш уже завершён. Сначала получите обычный подарок.')
            bet_type = 'promo_gift' if item['promo_locked'] else 'gift'
            if bet_type == 'promo_gift' and mines < 3:
                return error('Промо-отыгрыш доступен только при 3 или более минах.')
            snapshot = dict(item_id=item['id'], gift_id=item['gift_id'], name=item['gift_name'],
                            image=item['image_url'], price=bet,
                            multiplier=float(item['promo_wager_multiplier'] or 0),
                            target=target, progress=progress, code=item['promo_code'] or '')
            deleted = db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (inventory_id, session['uid']))
            if not deleted.rowcount:
                return error('Подарок уже используется.', 409)
        else:
            try:
                bet = parse_amount(data.get('bet'))
            except (ValueError, InvalidOperation, TypeError):
                return error('Укажите корректную ставку.')
            if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
                return error('Ставка от 0.10 до 300 TON.')
            updated = db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?',
                                 (bet, session['uid'], bet))
            if not updated.rowcount:
                return error('Недостаточно средств.')

        positions = sorted(secrets.SystemRandom().sample(range(25), mines))
        rtp_snapshot = promo_game_rtp() if bet_type == 'promo_gift' else game_rtp()
        db.execute("""INSERT INTO rounds(user_id,bet,mines,positions,bet_type,bet_inventory_id,
                       bet_gift_id,bet_gift_name,bet_gift_image,bet_gift_price,promo_wager_multiplier,
                       promo_wager_target,promo_wager_progress,promo_code,rtp_snapshot)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (session['uid'], bet, mines, json.dumps(positions), bet_type, snapshot['item_id'],
                    snapshot['gift_id'], snapshot['name'], snapshot['image'], snapshot['price'], snapshot['multiplier'],
                    snapshot['target'], snapshot['progress'], snapshot['code'], rtp_snapshot))
        row = active_round(db, session['uid'])
        if bet_type == 'ton':
            record_transaction(db, session['uid'], 'game_bet', -bet, 'round', row['id'], f'Mines: {mines}')
        elif bet_type == 'promo_gift':
            record_transaction(db, session['uid'], 'promo_wager_bet', 0, 'round', row['id'],
                               f'{snapshot["name"]} · X{snapshot["multiplier"]:g}')
        else:
            record_transaction(db, session['uid'], 'gift_bet', 0, 'round', row['id'], snapshot['name'])
        log_event(db,session['uid'],'mines_start',round_id=row['id'],mines=mines,bet=bet/100,
                  bet_type=bet_type,gift_name=snapshot['name'],gift_image=snapshot['image'])
        new_level=increase_turnover(db,session['uid'],bet)
        db.commit()
        return jsonify(round=round_view(row), user=profile(), new_level=new_level)
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
            if row['bet_type'] == 'promo_gift':
                record_transaction(db, row['user_id'], 'promo_wager_burn', 0, 'round', row['id'],
                                   f'Сгорел промо-подарок: {row["bet_gift_name"]}')
            elif row['bet_type'] == 'gift':
                record_transaction(db, row['user_id'], 'gift_bet_lost', 0, 'round', row['id'],
                                   f'Проигран подарок: {row["bet_gift_name"]}')
        else:
            opened.append(cell)
            db.execute('UPDATE rounds SET opened=? WHERE id=?', (json.dumps(opened), row['id']))
            if len(opened) == 25-row['mines']:
                award_round(db, row, len(opened))
        result = db.execute('SELECT * FROM rounds WHERE id=?', (row['id'],)).fetchone()
        log_event(db,session['uid'],'mines_cell',round_id=row['id'],cell=cell,
                  lost=cell in positions,opened=len(opened))
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
        log_event(db,session['uid'],'mines_cashout',round_id=row['id'],bet=row['bet']/100,
                  mines=row['mines'],opened=opened,payout=result['payout']/100,
                  gift_name=result['win_gift_name'],gift_image=result['win_gift_image'])
        db.commit()
        return jsonify(round=round_view(result), user=profile())
    finally:
        db.close()


@app.get('/api/game/recent-wins')
@login_required
def recent_wins():
    with connect() as db:
        rows = db.execute("""SELECT r.id,r.bet,r.mines,r.opened,r.payout,r.win_total,r.win_multiplier,
                                    r.win_gift_name,r.win_gift_image,r.win_gift_price,r.created_at,
                                    u.id AS user_id,u.name,u.username,u.photo_url
                             FROM rounds r JOIN users u ON u.id=r.user_id
                             WHERE r.state='won' AND COALESCE(r.bet_type,'ton')<>'promo_gift'
                             ORDER BY r.id DESC LIMIT 15""").fetchall()
    items = []
    for row in rows:
        opened_count = len(json.loads(row['opened'] or '[]'))
        factor = float(row['win_multiplier']) if row['win_multiplier'] is not None else multiplier_for(row['mines'], opened_count)
        total = row['win_total'] if row['win_total'] is not None else row['payout']
        items.append(dict(
            id=row['id'], user_id=row['user_id'], name=row['name'], username=row['username'],
            photo_url=row['photo_url'], bet=row['bet']/100, multiplier=round(max(1.01, factor), 6),
            amount=(total or 0)/100, gift=(dict(name=row['win_gift_name'], image_url=row['win_gift_image'],
                                               price_ton=(row['win_gift_price'] or 0)/100)
                                           if row['win_gift_name'] else None),
            created_at=row['created_at']))
    return jsonify(items=items)


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


@app.post('/api/inventory/<int:item_id>/sell')
@login_required
def sell_inventory(item_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?',
                          (item_id, session['uid'])).fetchone()
        if not item:
            return error('Подарок не найден.', 404)
        if item['promo_locked']:
            return error('Промо-подарок нельзя продать до завершения отыгрыша.', 409)
        amount = max(0, int(item['floor_price']))
        deleted = db.execute('DELETE FROM inventory WHERE id=? AND user_id=?',
                             (item_id, session['uid']))
        if not deleted.rowcount:
            return error('Подарок уже обработан.', 409)
        if amount:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, session['uid']))
        record_transaction(db, session['uid'], 'gift_sale', amount, 'inventory', item_id, item['gift_name'])
        db.commit()
        return jsonify(ok=True, sold_for=amount/100, user=profile())
    finally:
        db.close()


def send_user_notification(user_id, text, reply_markup=None, parse_mode=None):
    if not BOT_TOKEN:
        return
    payload = {'chat_id': int(user_id), 'text': str(text)}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    if parse_mode:
        payload['parse_mode'] = parse_mode
    for attempt in range(2):
        try:
            response = requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
                                     json=payload, timeout=(1.5, 3))
            response.raise_for_status()
            if response.json().get('ok'):
                return
        except (requests.RequestException, ValueError, TypeError):
            if attempt == 0:
                time.sleep(.08)
    app.logger.warning('Could not deliver notification to %s', user_id)


def notify_user_async(user_id, text, reply_markup=None, parse_mode=None):
    Thread(target=send_user_notification,
           args=(user_id, text, reply_markup, parse_mode), daemon=True).start()


@app.post('/api/inventory/<int:item_id>/withdraw')
@login_required
def request_withdrawal(item_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?',
                          (item_id, session['uid'])).fetchone()
        if not item:
            return error('Подарок не найден или уже отправлен на вывод.', 404)
        if item['promo_locked']:
            return error('Промо-подарок нельзя вывести до завершения отыгрыша.', 409)
        db.execute('''INSERT INTO withdrawals(user_id,inventory_id,gift_id,gift_name,image_url,floor_price,source,round_id,status)
                      VALUES(?,?,?,?,?,?,?,?,'pending')''',
                   (session['uid'], item['id'], item['gift_id'], item['gift_name'], item['image_url'],
                    item['floor_price'], item['source'], item['round_id']))
        deleted = db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (item_id, session['uid']))
        if not deleted.rowcount:
            return error('Не удалось зарезервировать подарок.', 409)
        record_transaction(db, session['uid'], 'withdrawal_request', 0, 'inventory', item_id, item['gift_name'])
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()


@app.post('/api/inventory/<int:item_id>/claim-promo')
@login_required
def claim_promo_gift(item_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        lock = ' FOR UPDATE' if DATABASE_URL else ''
        item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?' + lock,
                          (item_id, session['uid'])).fetchone()
        if not item:
            return error('Подарок не найден.', 404)
        if not item['promo_locked']:
            return error('Этот подарок уже обычный.', 409)
        target = int(item['promo_wager_target'] or 0)
        progress = int(item['promo_wager_progress'] or 0)
        if target <= 0 or progress < target:
            return error('Отыгрыш ещё не завершён.', 409)
        db.execute("""UPDATE inventory SET promo_locked=0,promo_wager_multiplier=0,promo_wager_target=0,
                      promo_wager_progress=0,promo_code='',source='promo_claimed'
                      WHERE id=? AND user_id=?""", (item_id, session['uid']))
        record_transaction(db, session['uid'], 'promo_wager_claim', 0,
                           'inventory', item_id, item['gift_name'])
        db.commit()
        updated = db.execute('SELECT * FROM inventory WHERE id=?', (item_id,)).fetchone()
        return jsonify(ok=True, item=inventory_item(updated), user=profile())
    finally:
        db.close()


def referral_percent():
    try:
        return min(50.0, max(0.0, float((read_document('ton_settings') or {}).get('referral_percent', 10))))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 10.0


def current_bot_username():
    # Never reuse a username cached for another BOT_TOKEN. This matters after a bot
    # replacement/deploy: otherwise referral links can silently point to the old bot.
    identity = read_document('bot_identity') or {}
    cached_ok = bool(BOT_TOKEN_FINGERPRINT and identity.get('token_fingerprint') == BOT_TOKEN_FINGERPRINT)
    username = str(identity.get('username') or '').strip().lstrip('@')[:64] if cached_ok else ''
    if username:
        return username
    if not BOT_TOKEN:
        return BOT_USERNAME[:64]
    try:
        response = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=(2, 4))
        response.raise_for_status()
        payload = response.json()
        if payload.get('ok'):
            username = str((payload.get('result') or {}).get('username') or '').strip().lstrip('@')[:64]
            if username:
                save_document('bot_identity', {'username': username,
                                               'token_fingerprint': BOT_TOKEN_FINGERPRINT,
                                               'updated_at': datetime.now(timezone.utc).isoformat()})
                return username
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
        pass
    return BOT_USERNAME[:64]


@app.get('/api/wallet/me')
@login_required
def wallet_me():
    with connect() as db:
        row = db.execute('SELECT address,updated_at FROM user_wallets WHERE user_id=?', (session['uid'],)).fetchone()
    return jsonify(address=(row['address'] if row else ''), updated_at=(row['updated_at'] if row else None))


@app.post('/api/wallet/me')
@login_required
def wallet_save():
    data = request.get_json(silent=True) or {}
    address = str(data.get('address') or '').strip()
    if not (20 <= len(address) <= 180 and re.fullmatch(r'[A-Za-z0-9_:\-+/=]+', address)):
        return error('Некорректный адрес TON-кошелька.')
    with connect() as db:
        db.execute('INSERT INTO user_wallets(user_id,address,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) '
                   'ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,updated_at=CURRENT_TIMESTAMP',
                   (session['uid'], address))
    return jsonify(ok=True, address=address)


@app.delete('/api/wallet/me')
@login_required
def wallet_forget():
    with connect() as db:
        db.execute('DELETE FROM user_wallets WHERE user_id=?', (session['uid'],))
    return jsonify(ok=True)


@app.get('/api/wallet/balance')
@login_required
def wallet_balance():
    address = str(request.args.get('address') or '').strip()
    if not address:
        with connect() as db:
            row = db.execute('SELECT address FROM user_wallets WHERE user_id=?', (session['uid'],)).fetchone()
        address = row['address'] if row else ''
    if not (20 <= len(address) <= 180 and re.fullmatch(r'[A-Za-z0-9_:\-+/=]+', address)):
        return error('Некорректный адрес TON-кошелька.')
    try:
        response = requests.get('https://toncenter.com/api/v3/accountStates',
                                params={'address': address, 'include_boc': 'false'},
                                headers=toncenter_headers(), timeout=(4, 8))
        response.raise_for_status()
        accounts = (response.json() or {}).get('accounts') or []
        if not accounts:
            return jsonify(ok=True, balance=0.0, balance_nano='0', status='uninitialized')
        account = accounts[0]
        nano = int(account.get('balance') or 0)
        balance = Decimal(nano) / Decimal(1_000_000_000)
        return jsonify(ok=True, balance=float(balance), balance_nano=str(nano),
                       status=str(account.get('status') or 'unknown'))
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
        app.logger.warning('TON wallet balance lookup failed')
        return error('Не удалось получить баланс кошелька из сети TON.', 502)


def loader_settings():
    doc = read_document('loader_settings') or {}
    path = str(doc.get('path') or '/static/gifs/shard.gif').strip()[:1000]
    if not (path.startswith('/static/') or path.startswith('https://')):
        path = '/static/gifs/shard.gif'
    return {'path': path}


def loader_catalog():
    folder = BASE / 'static' / 'gifs'
    items = []
    if folder.exists():
        for item in sorted(folder.iterdir(), key=lambda x: x.name.casefold()):
            if item.is_file() and item.suffix.lower() in {'.gif', '.webp', '.png', '.jpg', '.jpeg'}:
                items.append({'name': item.name, 'path': '/static/gifs/' + item.name})
    return items


@app.get('/api/ui/settings')
def public_ui_settings():
    # Intentionally public: the loading image is needed before Telegram auth finishes.
    return jsonify(loader_gif=loader_settings()['path'])


@app.get('/api/admin/loader-settings')
@admin_required
def admin_loader_settings():
    settings = loader_settings()
    return jsonify(path=settings['path'], catalog=loader_catalog())


@app.post('/api/admin/loader-settings')
@admin_required
def save_admin_loader_settings():
    data = request.get_json(silent=True) or {}
    path = str(data.get('path') or '').strip()[:1000]
    if not path:
        path = '/static/gifs/shard.gif'
    if path.startswith('/static/'):
        local = (BASE / path.lstrip('/')).resolve()
        static_root = (BASE / 'static').resolve()
        try:
            local.relative_to(static_root)
        except ValueError:
            return error('Путь должен находиться внутри /static или быть HTTPS URL.')
        if not local.is_file():
            return error('Файл по указанному пути не найден.')
    elif not path.startswith('https://'):
        return error('Укажите путь /static/... или полный HTTPS URL.')
    save_document('loader_settings', {'path': path, 'updated_at': datetime.now(timezone.utc).isoformat()})
    return jsonify(ok=True, path=path, catalog=loader_catalog())


@app.get('/api/referrals/me')
@login_required
def my_referrals():
    with connect() as db:
        count = db.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',
                           (session['uid'],)).fetchone()[0]
        total = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND kind='referral_bonus'",
                           (session['uid'],)).fetchone()[0]
    username = current_bot_username()
    return jsonify(count=count, earned=total/100, percent=referral_percent(), bot_username=username,
                   link=f'https://t.me/{username}?start=ref_{session["uid"]}' if username else '')



def parse_datetime_utc(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def promo_is_expired(promo):
    expires = parse_datetime_utc(promo['expires_at']) if promo and promo['expires_at'] else None
    return bool(expires and expires <= datetime.now(timezone.utc))


def promo_purpose(promo):
    custom = str(promo['description'] or '').strip() if 'description' in promo.keys() else ''
    if custom:
        return custom
    kind = promo['reward_type']
    if kind == 'balance':
        return f"Зачисляет {int(promo['amount'] or 0)/100:.2f} TON на игровой баланс."
    if kind == 'gift':
        return f"Выдаёт подарок «{promo['gift_name'] or 'Подарок'}»."
    if kind == 'wager_gift':
        return (f"Выдаёт отыгрышный подарок «{promo['gift_name'] or 'Подарок'}» "
                f"с условием X{float(promo['wager_multiplier'] or 0):g}.")
    if kind == 'deposit_bonus':
        pct = float(promo['bonus_percent'] or 0)
        fixed = int(promo['bonus_fixed'] or 0) / 100
        minimum = int(promo['min_deposit'] or 0) / 100
        value = f'+{pct:g}%' if pct else f'+{fixed:.2f} TON'
        suffix = f' при пополнении от {minimum:.2f} TON' if minimum else ''
        return f'Бонус {value} к следующему подтверждённому пополнению{suffix}.'
    if kind == 'multi':
        try:
            components = json.loads(promo['reward_json'] or '{}').get('components', {})
        except (ValueError, TypeError, AttributeError):
            components = {}
        labels = {'balance':'TON на баланс','gift':'подарок','wager_gift':'отыгрышный подарок','deposit_bonus':'бонус к пополнению'}
        parts = [labels.get(name, name) for name in components]
        return 'Мультипромокод: ' + ', '.join(parts) + '.' if parts else 'Мультипромокод с несколькими наградами.'
    return 'Бонусный промокод GemDrop.'


def promo_view(promo, redemption=None):
    reusable = bool(redemption and promo['reward_type'] in ('deposit_bonus','multi') and
                    redemption['reward_type'] == 'deposit_bonus' and redemption['deactivated_at'] and
                    not redemption['consumed_at'])
    used = bool(redemption and not reusable)
    expired = promo_is_expired(promo)
    exhausted = bool(int(promo['max_uses'] or 0) > 0 and int(promo['uses_count'] or 0) >= int(promo['max_uses'] or 0))
    if used:
        status = 'used'
    elif expired:
        status = 'expired'
    elif not promo['active'] or exhausted:
        status = 'disabled'
    else:
        status = 'active'
    expires = parse_datetime_utc(promo['expires_at']) if promo['expires_at'] else None
    return dict(
        code=promo['code'], reward_type=promo['reward_type'], purpose=promo_purpose(promo),
        source=(promo['source_label'] or 'Промокод GemDrop'), status=status,
        active=status == 'active', expires_at=expires.isoformat() if expires else None,
        used_at=redemption['created_at'] if redemption and status == 'used' else None,
        created_at=promo['created_at'],
    )


def unique_promo_code(db, prefix='GEM'):
    for _ in range(12):
        code = prefix + '-' + secrets.token_hex(4).upper()
        if not db.execute('SELECT 1 FROM promo_codes WHERE code=?', (code,)).fetchone():
            return code
    return generated_promo_code()


def create_upgrade_compensation_promo(db, user_id, source_price):
    source_ton = source_price / 100
    if source_ton < 50:
        return None
    # Small, occasional compensation; larger lost stakes gradually improve the odds.
    chance = min(0.30, 0.10 + max(0.0, source_ton - 50.0) * 0.20 / 950.0)
    if secrets.randbelow(10000) >= round(chance * 10000):
        return None
    code = unique_promo_code(db, 'UPG')
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    kind = secrets.choice(['deposit_bonus', 'gift', 'wager_gift', 'balance'])
    amount = 0
    gift_id = gift_name = gift_image = ''
    gift_price = 0
    wager_multiplier = 0.0
    bonus_percent = bonus_fixed = min_deposit = 0
    description = ''
    if kind in ('gift', 'wager_gift'):
        try:
            catalog = read_catalog().get('gifts', [])
        except (OSError, ValueError, json.JSONDecodeError):
            catalog = []
        budget = max(50, min(5000, round(source_price * (0.035 if kind == 'gift' else 0.05))))
        candidates = []
        for gift in catalog:
            try:
                price = ton_to_cents(gift.get('price_ton'))
            except (ValueError, TypeError, InvalidOperation):
                continue
            if 1 <= price <= budget and gift.get('id') and gift.get('name'):
                candidates.append((price, gift))
        candidates.sort(key=lambda x: x[0], reverse=True)
        if candidates:
            _, gift = secrets.choice(candidates[:min(12, len(candidates))])
            gift_id = str(gift['id'])
            gift_name = str(gift['name'])[:140]
            gift_image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
            gift_price = ton_to_cents(gift.get('price_ton'))
            if kind == 'wager_gift':
                wager_multiplier = 10.0
                description = f'Компенсационный отыгрышный подарок «{gift_name}» · X10.'
            else:
                description = f'Компенсационный подарок «{gift_name}».'
        else:
            kind = 'balance'
    if kind == 'deposit_bonus':
        bonus_percent = round(min(20.0, 5.0 + source_ton / 100.0), 1)
        min_deposit = max(100, round(source_price * 0.10))
        description = f'Компенсация Upgrade: +{bonus_percent:g}% к пополнению от {min_deposit/100:.2f} TON.'
    elif kind == 'balance':
        amount = max(10, round(source_price * 0.01))
        description = f'Компенсационный бонус {amount/100:.2f} TON на баланс.'
    db.execute("""INSERT INTO promo_codes(
        code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,
        max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,
        assigned_user_id,source_label,description,expires_at)
        VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",
        (code, kind, amount, gift_id, gift_name, gift_image, gift_price, wager_multiplier,
         0, bonus_percent, bonus_fixed, min_deposit, '{}', user_id,
         'Компенсация Upgrade', description, expires_at))
    row = db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone()
    return promo_view(row)


def apply_upgrade_loss_compensation(db, user_id, source_price):
    if source_price < 1000:
        return dict(cashback=0, cashback_percent=0, promo=None)
    source_ton = source_price / 100
    percent = min(5.0, 0.5 + max(0.0, source_ton - 10.0) * 4.5 / 490.0)
    cashback = max(1, round(source_price * percent / 100.0))
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (cashback, user_id))
    record_transaction(db, user_id, 'upgrade_cashback', cashback, 'upgrade', '',
                       f'Кэшбэк за неудачный Upgrade · {percent:.2f}%')
    promo = create_upgrade_compensation_promo(db, user_id, source_price)
    return dict(cashback=cashback/100, cashback_percent=round(percent, 2), promo=promo)


@app.get('/api/promocodes/mine')
@login_required
def my_promocodes():
    with connect() as db:
        claimed_codes = []
        for row in db.execute('SELECT reward_json FROM level_claims WHERE user_id=?', (session['uid'],)).fetchall():
            try:
                code = json.loads(row['reward_json'] or '{}').get('code')
            except (ValueError, TypeError, AttributeError):
                code = None
            if code:
                claimed_codes.append(str(code))
        rows = db.execute('SELECT * FROM promo_codes WHERE assigned_user_id=? ORDER BY created_at DESC',
                          (session['uid'],)).fetchall()
        by_code = {row['code']: row for row in rows}
        for code in claimed_codes:
            if code not in by_code:
                row = db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone()
                if row:
                    by_code[code] = row
        used_rows = db.execute("""SELECT p.* FROM promo_codes p JOIN promo_redemptions r ON r.code=p.code
                                  WHERE r.user_id=? ORDER BY r.created_at DESC""", (session['uid'],)).fetchall()
        for row in used_rows:
            by_code.setdefault(row['code'], row)
        items = []
        for promo in by_code.values():
            redemption = db.execute('SELECT * FROM promo_redemptions WHERE code=? AND user_id=?',
                                    (promo['code'], session['uid'])).fetchone()
            items.append(promo_view(promo, redemption))
    order = {'active':0, 'expired':1, 'disabled':2, 'used':3}
    items.sort(key=lambda x: x['created_at'] or '', reverse=True)
    items.sort(key=lambda x: order.get(x['status'], 9))
    return jsonify(items=items)


@app.post('/api/promocodes/redeem')
@login_required
def redeem_promocode():
    code = str((request.get_json(silent=True) or {}).get('code') or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Проверьте промокод.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        promo = db.execute('SELECT * FROM promo_codes WHERE code=?' + (' FOR UPDATE' if DATABASE_URL else ''), (code,)).fetchone()
        if not promo or not promo['active']:
            return error('Промокод не найден или отключён.', 404)
        if int(promo['assigned_user_id'] or 0) not in (0, int(session['uid'])):
            return error('Этот промокод предназначен другому пользователю.', 403)
        if promo_is_expired(promo):
            return error('Срок действия промокода истёк.', 409)
        prior=db.execute('SELECT * FROM promo_redemptions WHERE code=? AND user_id=?',(code,session['uid'])).fetchone()
        reusable=bool(prior and promo['reward_type'] in ('deposit_bonus','multi') and
                      prior['reward_type']=='deposit_bonus' and prior['deactivated_at'] and not prior['consumed_at'])
        if prior and not reusable:return error('Вы уже активировали этот промокод.',409)
        if not reusable and promo['max_uses'] > 0 and promo['uses_count'] >= promo['max_uses']:
            return error('Лимит активаций этого промокода исчерпан.', 409)
        if reusable:
            if db.execute("""SELECT 1 FROM promo_redemptions WHERE user_id=? AND reward_type='deposit_bonus'
                             AND consumed_at IS NULL AND deactivated_at IS NULL""",(session['uid'],)).fetchone():
                return error('У вас уже есть активный промокод на пополнение.',409)
            db.execute('UPDATE promo_redemptions SET deactivated_at=NULL WHERE code=? AND user_id=?',(code,session['uid']))
            db.commit()
            return jsonify(ok=True,reward=dict(type='deposit_bonus',code=code,
                           bonus_percent=float(promo['bonus_percent'] or 0),
                           bonus_fixed=int(promo['bonus_fixed'] or 0)/100,
                           min_deposit=int(promo['min_deposit'] or 0)/100),user=profile())
        inventory_id = None
        if promo['reward_type'] == 'balance':
            amount = max(0, int(promo['amount']))
            if amount <= 0:
                return error('Награда промокода настроена неверно.', 500)
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, session['uid']))
            reward = dict(type='balance', amount=amount/100)
            record_transaction(db, session['uid'], 'promo_balance', amount, 'promo', code, f'Промокод {code}')
        elif promo['reward_type'] == 'gift':
            cur = db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'promo')",
                             (session['uid'], promo['gift_id'], promo['gift_name'], promo['gift_image_url'], promo['gift_price']))
            inventory_id = cur.lastrowid
            reward = dict(type='gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'], name=promo['gift_name'],
                                                 image_url=promo['gift_image_url'], price_ton=promo['gift_price']/100))
            record_transaction(db, session['uid'], 'promo_gift', 0, 'promo', code, promo['gift_name'])
        elif promo['reward_type'] == 'wager_gift':
            multiplier = max(1.0, float(promo['wager_multiplier'] or 1))
            target = max(1, round(int(promo['gift_price']) * multiplier))
            cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                              promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code)
                              VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?)""",
                             (session['uid'], promo['gift_id'], promo['gift_name'], promo['gift_image_url'],
                              promo['gift_price'], multiplier, target, code))
            inventory_id = cur.lastrowid
            reward = dict(type='wager_gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'],
                                                       name=promo['gift_name'], image_url=promo['gift_image_url'],
                                                       price_ton=promo['gift_price']/100, promo_locked=True,
                                                       wager_multiplier=multiplier, wager_target=target/100,
                                                       wager_progress=0))
            record_transaction(db, session['uid'], 'promo_wager_gift', 0, 'promo', code,
                               f'{promo["gift_name"]} · X{multiplier:g}')
        elif promo['reward_type']=='multi':
            components=json.loads(promo['reward_json'] or '{}').get('components',{})
            if not components:return error('Мультипромокод настроен неверно.',500)
            if 'deposit_bonus' in components and db.execute("""SELECT 1 FROM promo_redemptions WHERE user_id=?
               AND reward_type='deposit_bonus' AND consumed_at IS NULL AND deactivated_at IS NULL""",(session['uid'],)).fetchone():
                return error('Сначала используйте или уберите активный промокод на пополнение.',409)
            rewards=[]
            if 'balance' in components:
                amount=int(components['balance']['amount'])
                db.execute('UPDATE users SET balance=balance+? WHERE id=?',(amount,session['uid']))
                record_transaction(db,session['uid'],'promo_balance',amount,'promo',code,f'Мультипромокод {code}')
                rewards.append(dict(type='balance',amount=amount/100))
            for kind in ('gift','wager_gift'):
                if kind not in components:continue
                comp=components[kind]
                if kind=='gift':
                    cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'promo')",
                                   (session['uid'],comp['gift_id'],comp['gift_name'],comp['image_url'],comp['gift_price']))
                else:
                    multiplier=float(comp['wager_multiplier'])
                    target=round(int(comp['gift_price'])*multiplier)
                    cur=db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                      promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code)
                                      VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?)""",
                                   (session['uid'],comp['gift_id'],comp['gift_name'],comp['image_url'],comp['gift_price'],multiplier,target,code))
                inventory_id=cur.lastrowid
                record_transaction(db,session['uid'],'promo_'+kind,0,'promo',code,comp['gift_name'])
                rewards.append(dict(type=kind,gift=dict(id=inventory_id,name=comp['gift_name'],
                   image_url=comp['image_url'],price_ton=comp['gift_price']/100,
                   wager_multiplier=comp.get('wager_multiplier',0))))
            if 'deposit_bonus' in components:
                comp=components['deposit_bonus']
                rewards.append(dict(type='deposit_bonus',code=code,
                  bonus_percent=float(comp.get('bonus_percent') or 0),
                  bonus_fixed=int(comp.get('bonus_fixed') or 0)/100,
                  min_deposit=int(comp.get('min_deposit') or 0)/100))
            reward=dict(type='multi',rewards=rewards)
        elif promo['reward_type'] == 'deposit_bonus':
            if db.execute("""SELECT 1 FROM promo_redemptions WHERE user_id=? AND reward_type='deposit_bonus'
                             AND consumed_at IS NULL AND deactivated_at IS NULL""",(session['uid'],)).fetchone():
                return error('У вас уже есть активный промокод на пополнение.',409)
            reward=dict(type='deposit_bonus',code=code,bonus_percent=float(promo['bonus_percent'] or 0),
                        bonus_fixed=int(promo['bonus_fixed'] or 0)/100,min_deposit=int(promo['min_deposit'] or 0)/100)
        else:
            return error('Награда промокода настроена неверно.', 500)
        redemption_type='deposit_bonus' if promo['reward_type']=='multi' and 'deposit_bonus' in components else promo['reward_type']
        db.execute('INSERT INTO promo_redemptions(code,user_id,reward_type,amount,inventory_id) VALUES(?,?,?,?,?)',
                   (code, session['uid'],redemption_type, int(promo['amount'] or 0), inventory_id))
        db.execute('UPDATE promo_codes SET uses_count=uses_count+1 WHERE code=?', (code,))
        log_event(db,session['uid'],'promo_redeem',code=code,reward_type=promo['reward_type'],reward=reward)
        db.commit()
        return jsonify(ok=True, reward=reward, user=profile())
    finally:
        db.close()


def generated_promo_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return 'GEM-' + ''.join(secrets.choice(alphabet) for _ in range(8))


@app.get('/api/deposit-bonus')
@login_required
def deposit_bonus_status():
    with connect() as db:
        row=db.execute("""SELECT p.code,p.bonus_percent,p.bonus_fixed,p.min_deposit FROM promo_redemptions r
                           JOIN promo_codes p ON p.code=r.code WHERE r.user_id=? AND r.reward_type='deposit_bonus'
                           AND r.consumed_at IS NULL AND r.deactivated_at IS NULL
                           ORDER BY r.created_at DESC LIMIT 1""",(session['uid'],)).fetchone()
    if not row:return jsonify(active=False)
    return jsonify(active=True,code=row['code'],bonus_percent=float(row['bonus_percent'] or 0),
                   bonus_fixed=int(row['bonus_fixed'] or 0)/100,min_deposit=int(row['min_deposit'] or 0)/100)


@app.post('/api/deposit-bonus/remove')
@login_required
def remove_deposit_bonus():
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("""SELECT code FROM promo_redemptions WHERE user_id=? AND reward_type='deposit_bonus'
                          AND consumed_at IS NULL AND deactivated_at IS NULL""",(session['uid'],)).fetchone()
        if row:
            db.execute("""UPDATE promo_redemptions SET deactivated_at=CURRENT_TIMESTAMP WHERE user_id=?
                          AND code=? AND consumed_at IS NULL AND deactivated_at IS NULL""",(session['uid'],row['code']))
            log_event(db,session['uid'],'deposit_promo_removed',code=row['code'])
        db.commit()
    finally:db.close()
    return jsonify(ok=True,active=False)


@app.get('/api/admin/promocodes')
@admin_required
def admin_promocodes():
    with connect() as db:
        rows = db.execute('SELECT * FROM promo_codes ORDER BY created_at DESC,code DESC LIMIT 300').fetchall()
    return jsonify(items=[dict(code=x['code'], reward_type=x['reward_type'], amount=x['amount']/100,
                               gift_id=x['gift_id'], gift_name=x['gift_name'], image_url=x['gift_image_url'],
                               gift_price=x['gift_price']/100, wager_multiplier=float(x['wager_multiplier'] or 0),
                               max_uses=x['max_uses'], uses_count=x['uses_count'],
                               active=bool(x['active']), created_at=x['created_at'],
                               assigned_user_id=int(x['assigned_user_id'] or 0),source=x['source_label'] or '',
                               description=x['description'] or '',expires_at=x['expires_at'],expired=promo_is_expired(x),
                               bonus_percent=float(x['bonus_percent'] or 0),bonus_fixed=x['bonus_fixed']/100,
                               min_deposit=x['min_deposit']/100,
                               components=public_level_reward(json.loads(x['reward_json'])).get('components',{})
                               if x['reward_type']=='multi' else {}) for x in rows])


@app.post('/api/admin/promocodes')
@admin_required
def admin_create_promocode():
    data = request.get_json(silent=True) or {}
    code = str(data.get('code') or '').strip().upper() or generated_promo_code()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Код: 3–32 символа, только A-Z, 0-9, _ и -.')
    reward_type = str(data.get('reward_type') or 'balance')
    try:
        max_uses = int(data.get('max_uses', 1))
    except (TypeError, ValueError):
        return error('Некорректный лимит активаций.')
    if not 0 <= max_uses <= 1000000:
        return error('Лимит активаций должен быть от 0 до 1 000 000. 0 — без лимита.')
    amount = 0; gift_id = ''; gift_name = ''; gift_image = ''; gift_price = 0; wager_multiplier = 0.0
    bonus_percent=0;bonus_fixed=0;min_deposit=0;multi_reward=None
    try:
        assigned_user_id=int(data.get('assigned_user_id') or 0)
        expires_days=int(data.get('expires_in_days') or 0)
    except (TypeError,ValueError):
        return error('Проверьте ID пользователя и срок действия.')
    if assigned_user_id < 0:return error('ID пользователя указан неверно.')
    if not 0 <= expires_days <= 3650:return error('Срок действия: от 0 до 3650 дней. 0 — без срока.')
    source_label=str(data.get('source_label') or 'Администрация').strip()[:80]
    description=str(data.get('description') or '').strip()[:300]
    expires_at=(datetime.now(timezone.utc)+timedelta(days=expires_days)).isoformat() if expires_days else None
    if reward_type == 'balance':
        try:
            amount = parse_amount(data.get('amount'))
        except (ValueError, InvalidOperation, TypeError):
            return error('Укажите сумму награды с точностью до 0.01 TON.')
        if not 1 <= amount <= 100000000:
            return error('Сумма промокода должна быть от 0.01 до 1 000 000 TON.')
    elif reward_type in ('gift', 'wager_gift'):
        gift_id = str(data.get('gift_id') or '')
        try:
            gift = next((g for g in read_catalog().get('gifts', []) if str(g.get('id')) == gift_id), None)
        except (OSError, ValueError, json.JSONDecodeError):
            gift = None
        if not gift:
            return error('Подарок не найден в каталоге Portal.')
        gift_name = str(gift.get('name') or 'Подарок')[:140]
        gift_image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
        try:
            gift_price = int(Decimal(str(gift.get('price_ton') or 0)) * 100)
        except (InvalidOperation, TypeError, ValueError):
            gift_price = 0
        if gift_price <= 0:
            return error('У подарка должна быть актуальная цена Portal.')
        if reward_type == 'wager_gift':
            try:
                wager_multiplier = float(data.get('wager_multiplier') or 0)
            except (TypeError, ValueError):
                return error('Укажите корректный X отыгрыша.')
            if not 1 <= wager_multiplier <= 1000:
                return error('X отыгрыша должен быть от 1 до 1000.')
    elif reward_type=='multi':
        try:multi_reward=normalize_level_reward({'type':'multi_promo','components':data.get('components')})
        except (ValueError,TypeError,InvalidOperation) as exc:return error(str(exc))
        deposit=multi_reward['components'].get('deposit_bonus',{})
        bonus_percent=deposit.get('bonus_percent',0)
        bonus_fixed=deposit.get('bonus_fixed',0)
        min_deposit=deposit.get('min_deposit',0)
    elif reward_type=='deposit_bonus':
        try:
            bonus_percent=float(data.get('bonus_percent') or 0)
            bonus_fixed=parse_amount(data.get('bonus_fixed') or 0)
            min_deposit=parse_amount(data.get('min_deposit') or 0)
        except (ValueError,TypeError,InvalidOperation):
            return error('Проверьте бонус и минимальную сумму пополнения.')
        if not math.isfinite(bonus_percent) or not 0<=bonus_percent<=100 or not 0<=bonus_fixed<=1000000 or not 0<=min_deposit<=100000000 or not (bonus_percent or bonus_fixed) or bonus_percent and bonus_fixed:
            return error('Укажите один бонус: процент до 100% или сумму в TON.')
    else:
        return error('Выберите тип промокода.')
    try:
        with connect() as db:
            if assigned_user_id and not db.execute('SELECT 1 FROM users WHERE id=?',(assigned_user_id,)).fetchone():
                return error('Пользователь с таким ID не найден.',404)
            if assigned_user_id:
                max_uses=1
            db.execute('INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,assigned_user_id,source_label,description,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (code, reward_type, amount, gift_id, gift_name, gift_image, gift_price,
                        wager_multiplier, max_uses, session['uid'],
                        bonus_percent,bonus_fixed,min_deposit,
                        json.dumps(multi_reward,ensure_ascii=False) if multi_reward else '{}',
                        assigned_user_id,source_label,description,expires_at))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'promo_create', code))
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return error('Такой промокод уже существует.', 409)
        raise
    return jsonify(ok=True, code=code)


@app.post('/api/admin/promocodes/<code>/toggle')
@admin_required
def admin_toggle_promocode(code):
    code = str(code).upper()
    with connect() as db:
        row = db.execute('SELECT active FROM promo_codes WHERE code=?', (code,)).fetchone()
        if not row:
            return error('Промокод не найден.', 404)
        active = 0 if row['active'] else 1
        db.execute('UPDATE promo_codes SET active=? WHERE code=?', (active, code))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'promo_toggle', f'{code}:{active}'))
    return jsonify(ok=True, active=bool(active))


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
        level=level_number(db,int(user['turnover_cents'] or 0))
    return jsonify(user=dict(id=user['id'], name=user['name'], username=user['username'],
                             balance=user['balance']/100,level=level,
                             turnover=user['turnover_cents']/100),items=[inventory_item(x) for x in items])


@app.post('/api/admin/users/<int:user_id>/promocodes')
@admin_required
def admin_issue_user_promocode(user_id):
    code = str((request.get_json(silent=True) or {}).get('template_code') or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Выберите промокод из списка.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        template = db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone()
        if not template or int(template['assigned_user_id'] or 0) or not template['active'] or promo_is_expired(template):
            return error('Этот промокод недоступен для выдачи.', 409)
        issued_code = unique_promo_code(db, 'ADM')
        db.execute('''INSERT INTO promo_codes(
            code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,
            max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,
            assigned_user_id,source_label,description,expires_at)
            VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)''',
            (issued_code,template['reward_type'],template['amount'],template['gift_id'],
             template['gift_name'],template['gift_image_url'],template['gift_price'],
             template['wager_multiplier'],session['uid'],template['bonus_percent'],
             template['bonus_fixed'],template['min_deposit'],template['reward_json'],
             user_id,'Выдан администратором',promo_purpose(template),template['expires_at']))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'promo_issue',f'{code} → {issued_code}'))
        log_event(db,user_id,'promo_issued',code=issued_code,source='Администрация')
        db.commit()
        return jsonify(ok=True,code=issued_code)
    finally:
        db.close()


@app.post('/api/admin/users/<int:user_id>/level')
@admin_required
def admin_user_level(user_id):
    data=request.get_json(silent=True) or {}
    try:level=int(data.get('level'))
    except (ValueError,TypeError):return error('Уровень должен быть от 1 до 20.')
    if not 1<=level<=20:return error('Уровень должен быть от 1 до 20.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        user=db.execute('SELECT turnover_cents FROM users WHERE id=?'+(' FOR UPDATE' if DATABASE_URL else ''),(user_id,)).fetchone()
        row=db.execute('SELECT required_turnover FROM levels WHERE level=?',(level,)).fetchone()
        if not user:return error('Пользователь не найден.',404)
        if not row:return error('Уровень не найден.',404)
        old=level_number(db,int(user['turnover_cents'] or 0))
        db.execute('UPDATE users SET turnover_cents=? WHERE id=?',(row['required_turnover'],user_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'level_set',f'{old} → {level}'))
        log_event(db,user_id,'admin_level',previous_level=old,new_level=level,admin_id=session['uid'])
        db.commit()
    finally:db.close()
    return jsonify(ok=True,level=level,turnover=row['required_turnover']/100)


@app.get('/api/admin/users/<int:user_id>/activity')
@admin_required
def admin_user_activity(user_id):
    try:offset=max(0,min(100000,int(request.args.get('offset',0))))
    except (ValueError,TypeError):offset=0
    limit=offset+101
    events=[]
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?',(user_id,)).fetchone():return error('Пользователь не найден.',404)
        for r in db.execute('SELECT * FROM user_events WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall():
            payload=json.loads(r['payload'])
            image=payload.get('gift_image') or payload.get('target_image') or payload.get('source_image') or ''
            events.append(dict(id='e'+str(r['id']),date=str(r['created_at']),kind=r['kind'],
                               amount=None,image=image,details=payload))
        for r in db.execute('SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='t'+str(r['id']),date=str(r['created_at']),kind=r['kind'],
                               amount=r['amount']/100,image='',details=dict(text=r['details'],
                               balance_after=r['balance_after']/100 if r['balance_after'] is not None else None,
                               reference_type=r['reference_type'],reference_id=r['reference_id'])))
        for r in db.execute('SELECT * FROM rounds WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='m'+str(r['id']),date=str(r['created_at']),kind='mines_round',
                               amount=r['bet']/100,image=r['bet_gift_image'] or r['win_gift_image'],
                               details=dict(mines=r['mines'],bet=r['bet']/100,state=r['state'],
                                            bet_type=r['bet_type'],gift_name=r['bet_gift_name'] or r['win_gift_name'],
                                            opened=len(json.loads(r['opened'] or '[]')),payout=r['payout']/100)))
        for r in db.execute('SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='w'+str(r['id']),date=str(r['created_at']),kind='withdrawal',amount=0,
                               image=r['image_url'],details=dict(gift_name=r['gift_name'],status=r['status'])))
        for r in db.execute('SELECT * FROM promo_redemptions WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='p'+r['code'],date=str(r['created_at']),kind='promo_activation',amount=r['amount']/100,
                               image='',details=dict(code=r['code'],reward_type=r['reward_type'],consumed_at=r['consumed_at'])))
        for r in db.execute('SELECT * FROM roll_spins WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='r'+r['id'],date=str(r['created_at']),kind='roll_spin',amount=-r['price']/100,
                               image='',details=dict(roll_id=r['roll_id'],outcome=r['outcome'],gift_name=r['gift_name'])))
        for r in db.execute('SELECT * FROM ton_deposit_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='d'+r['id'],date=str(r['created_at']),kind='deposit_order',amount=r['amount']/100,
                               image='',details=dict(status=r['status'],promo_code=r['promo_code'],order_id=r['id'])))
        for r in db.execute('SELECT * FROM admin_log WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall():
            events.append(dict(id='a'+str(r['id']),date=str(r['created_at']),kind='admin_action',amount=None,
                               image='',details=dict(action=r['action'],text=r['details'],admin_id=r['admin_id'])))
        for r in db.execute('SELECT * FROM level_claims WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall():
            reward=json.loads(r['reward_json'])
            events.append(dict(id='l'+str(r['level']),date=str(r['created_at']),kind='level_claim',amount=None,
                               image=(reward.get('gift') or {}).get('image_url',''),details=dict(level=r['level'],reward=reward)))
    events.sort(key=lambda x:(x['date'],x['id']),reverse=True)
    return jsonify(items=events[offset:offset+100],has_more=len(events)>offset+100)


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
        old_row = db.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()
        if not old_row:
            return error('Пользователь не найден.', 404)
        cursor = db.execute('UPDATE users SET balance=? WHERE id=?', (amount, user_id))
        if not cursor.rowcount:
            return error('Пользователь не найден.', 404)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'balance_set', str(amount)))
        record_transaction(db, user_id, 'admin_balance', amount-int(old_row['balance']),
                           'admin', session['uid'], f'Баланс установлен: {amount/100:.2f} TON')
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
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
        db.execute('''INSERT INTO deposits(user_id,amount,referrer_id,referral_bonus,admin_id,request_key)
                      VALUES(?,?,?,?,?,?)''', (user_id, amount, None, 0, session['uid'], key))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'admin_deposit', str(amount)))
        record_transaction(db, user_id, 'deposit', amount, 'deposit', key, 'Пополнение администратором')
        db.commit()
        return jsonify(ok=True, balance_added=amount/100, referral_bonus=0)
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


@app.get('/api/admin/withdrawals')
@admin_required
def admin_withdrawals():
    view = request.args.get('view', 'pending').strip().lower()
    if view not in {'pending', 'completed'}:
        return error('Неизвестный раздел выводов.')
    where = "w.status='pending'" if view == 'pending' else "w.status IN ('approved','rejected')"
    with connect() as db:
        rows = db.execute(f'''SELECT w.*,u.name AS user_name,u.username,
                                     au.name AS admin_name,au.username AS admin_username
                              FROM withdrawals w
                              JOIN users u ON u.id=w.user_id
                              LEFT JOIN users au ON au.id=w.admin_id
                              WHERE {where}
                              ORDER BY w.id DESC LIMIT 300''').fetchall()
    return jsonify(items=[dict(id=x['id'], user_id=x['user_id'], user_name=x['user_name'],
                               username=x['username'], gift_name=x['gift_name'], image_url=x['image_url'],
                               price_ton=x['floor_price']/100, status=x['status'], admin_id=x['admin_id'],
                               admin_name=x['admin_name'], admin_username=x['admin_username'],
                               created_at=x['created_at'], processed_at=x['processed_at']) for x in rows])

@app.post('/api/admin/withdrawals/<int:withdrawal_id>/approve')
@admin_required
def approve_withdrawal(withdrawal_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'",
                         (withdrawal_id,)).fetchone()
        if not row:
            return error('Заявка не найдена или уже обработана.', 404)
        db.execute("UPDATE withdrawals SET status='approved',admin_id=?,processed_at=CURRENT_TIMESTAMP WHERE id=?",
                   (session['uid'], withdrawal_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], row['user_id'], 'withdrawal_approved', str(withdrawal_id)))
        record_transaction(db, row['user_id'], 'withdrawal_approved', 0, 'withdrawal', withdrawal_id, row['gift_name'])
        db.commit()
        notify_user_async(row['user_id'], f'✅ Вывод подарка «{row["gift_name"]}» завершён.')
        return jsonify(ok=True)
    finally:
        db.close()


@app.post('/api/admin/withdrawals/<int:withdrawal_id>/reject')
@admin_required
def reject_withdrawal(withdrawal_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'",
                         (withdrawal_id,)).fetchone()
        if not row:
            return error('Заявка не найдена или уже обработана.', 404)
        db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                      VALUES(?,?,?,?,?,?,?)''',
                   (row['user_id'], row['gift_id'], row['gift_name'], row['image_url'],
                    row['floor_price'], row['source'], row['round_id']))
        db.execute("UPDATE withdrawals SET status='rejected',admin_id=?,processed_at=CURRENT_TIMESTAMP WHERE id=?",
                   (session['uid'], withdrawal_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], row['user_id'], 'withdrawal_rejected', str(withdrawal_id)))
        record_transaction(db, row['user_id'], 'withdrawal_rejected', 0, 'withdrawal', withdrawal_id, row['gift_name'])
        db.commit()
        notify_user_async(row['user_id'], f'↩️ Вывод подарка «{row["gift_name"]}» отклонён. Подарок возвращён в инвентарь.')
        return jsonify(ok=True)
    finally:
        db.close()


@app.get('/api/admin/transactions')
@admin_required
def admin_transactions():
    # Funding/balance adjustment history for the admin UI. Game audit rows stay in DB.
    term = request.args.get('q', '').strip()[:80]
    params = []
    where = ["t.kind IN ('deposit','admin_balance','referral_bonus','ton_deposit','promo_balance')"]
    if term:
        where.append('(CAST(t.user_id AS TEXT) LIKE ? OR u.username LIKE ? OR u.name LIKE ?)')
        params.extend([f'%{term}%', f'%{term}%', f'%{term}%'])
    clause = ' WHERE ' + ' AND '.join(where)
    with connect() as db:
        rows = db.execute('''SELECT t.*,u.name,u.username FROM transactions t
                             JOIN users u ON u.id=t.user_id''' + clause +
                          ' ORDER BY t.id DESC LIMIT 500', tuple(params)).fetchall()
    return jsonify(items=[dict(id=x['id'], user_id=x['user_id'], name=x['name'], username=x['username'],
                               kind=x['kind'], amount=x['amount']/100,
                               balance_after=None if x['balance_after'] is None else x['balance_after']/100,
                               reference_type=x['reference_type'], reference_id=x['reference_id'],
                               details=x['details'], created_at=x['created_at']) for x in rows])

@app.get('/api/admin/rtp')
@admin_required
def admin_rtp_get():
    return jsonify(rtp=round(game_rtp()*100, 2), promo_rtp=round(promo_game_rtp()*100, 2),
                   upgrade_rtp=upgrade_rtp_basis_points()/100,mode='global')


@app.post('/api/admin/rtp')
@admin_required
def admin_rtp_set():
    data = request.get_json(silent=True) or {}
    try:
        percent = float(data.get('rtp'))
        promo_percent = float(data.get('promo_rtp', promo_game_rtp()*100))
        upgrade_percent = float(data.get('upgrade_rtp',upgrade_rtp_basis_points()/100))
    except (TypeError, ValueError):
        return error('Введите RTP в процентах.')
    if not math.isfinite(percent) or not 97 <= percent <= 99.9:
        return error('Для честной сетки Mines с минимумом 1.01x общий RTP должен быть от 97 до 99.9%.')
    if not math.isfinite(promo_percent) or not 89 <= promo_percent <= 96.9:
        return error('RTP промо-отыгрыша должен быть от 89 до 96.9%.')
    if promo_percent >= percent:
        return error('RTP промо-отыгрыша должен быть ниже обычного RTP.')
    if not math.isfinite(upgrade_percent) or not 1<=upgrade_percent<=100:
        return error('RTP апгрейда должен быть от 1 до 100%.')
    save_document('game_settings', {'rtp': percent/100, 'promo_rtp': promo_percent/100,
                                    'upgrade_rtp_bp':round(upgrade_percent*100),
                                    'updated_at': datetime.now(timezone.utc).isoformat(),
                                    'admin_id': session['uid']})
    return jsonify(ok=True, rtp=round(game_rtp()*100, 2), promo_rtp=round(promo_game_rtp()*100, 2),
                   upgrade_rtp=upgrade_rtp_basis_points()/100)


def ton_settings():
    doc = read_document('ton_settings') or {}
    try:
        ref_percent = min(50.0, max(0.0, float(doc.get('referral_percent', 10) or 0)))
    except (TypeError, ValueError):
        ref_percent = 10.0
    return dict(
        enabled=bool(doc.get('enabled', True)),
        recipient_wallet=str(doc.get('recipient_wallet') or '').strip()[:180],
        site_name=str(doc.get('site_name') or 'GemDrop').strip()[:48] or 'GemDrop',
        site_url=str(doc.get('site_url') or WEBAPP_URL or '').strip()[:500],
        icon_url=str(doc.get('icon_url') or '').strip()[:500],
        referral_percent=ref_percent,
    )


@app.get('/api/ton/settings')
@login_required
def ton_settings_public():
    settings = ton_settings()
    return jsonify(enabled=settings['enabled'], recipient_wallet=settings['recipient_wallet'],
                   site_name=settings['site_name'], referral_percent=settings['referral_percent'])


@app.get('/api/admin/ton-settings')
@admin_required
def admin_ton_settings_get():
    return jsonify(**ton_settings())


@app.post('/api/admin/ton-settings')
@admin_required
def admin_ton_settings_set():
    data = request.get_json(silent=True) or {}
    recipient = str(data.get('recipient_wallet') or '').strip()
    site_name = str(data.get('site_name') or 'GemDrop').strip()
    site_url = str(data.get('site_url') or '').strip()
    icon_url = str(data.get('icon_url') or '').strip()
    enabled = bool(data.get('enabled', True))
    try:
        referral_pct = float(data.get('referral_percent', 10))
    except (TypeError, ValueError):
        return error('Реферальный процент указан неверно.')
    if not 0 <= referral_pct <= 50:
        return error('Реферальный процент должен быть от 0 до 50%.')
    if recipient and not (20 <= len(recipient) <= 180 and re.fullmatch(r'[A-Za-z0-9_:\-+/=]+', recipient)):
        return error('Проверьте адрес TON-кошелька получателя.')
    if not (1 <= len(site_name) <= 48):
        return error('Название сайта должно быть от 1 до 48 символов.')
    if site_url and not site_url.startswith('https://'):
        return error('URL сайта должен начинаться с https://')
    if icon_url and not icon_url.startswith('https://'):
        return error('URL иконки должен начинаться с https://')
    save_document('ton_settings', dict(enabled=enabled, recipient_wallet=recipient, site_name=site_name,
                                       site_url=site_url, icon_url=icon_url, referral_percent=referral_pct,
                                       updated_at=datetime.now(timezone.utc).isoformat(),
                                       admin_id=session['uid']))
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'ton_settings',
                    json.dumps({'enabled': enabled, 'site_name': site_name, 'recipient_wallet': recipient, 'referral_percent': referral_pct}, ensure_ascii=False)))
    return jsonify(ok=True, **ton_settings())


def toncenter_headers():
    headers = {'Accept': 'application/json'}
    if TONCENTER_API_KEY:
        headers['X-API-Key'] = TONCENTER_API_KEY
    return headers


def credit_verified_ton_deposit(db, order, tx_hash):
    """Credit an on-chain TON deposit exactly once; referral rewards are paid only here."""
    user_id = int(order['user_id'])
    amount = int(order['amount'])
    referral = db.execute('SELECT referrer_id FROM referrals WHERE referred_id=?', (user_id,)).fetchone()
    referrer = referral['referrer_id'] if referral else None
    bonus = int(amount * referral_percent() / 100) if referrer else 0
    active=db.execute("""SELECT p.code,p.bonus_percent,p.bonus_fixed,p.min_deposit FROM promo_redemptions r
                         JOIN promo_codes p ON p.code=r.code WHERE r.user_id=? AND r.reward_type='deposit_bonus'
                         AND r.code=? AND r.consumed_at IS NULL""" + (' FOR UPDATE OF r' if DATABASE_URL else ''),
                      (user_id,order['promo_code'] or '')).fetchone()
    deposit_bonus=0
    if active and amount>=int(active['min_deposit'] or 0):
        deposit_bonus=round(amount*float(active['bonus_percent'] or 0)/100)+int(active['bonus_fixed'] or 0)
        consumed=db.execute('UPDATE promo_redemptions SET consumed_at=CURRENT_TIMESTAMP WHERE code=? AND user_id=? AND consumed_at IS NULL',
                            (active['code'],user_id))
        if not consumed.rowcount:deposit_bonus=0
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount+deposit_bonus, user_id))
    if referrer and bonus:
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (bonus, referrer))
    request_key = 'ton:' + str(order['id'])
    db.execute('INSERT INTO deposits(user_id,amount,referrer_id,referral_bonus,admin_id,request_key) VALUES(?,?,?,?,0,?)',
               (user_id, amount, referrer, bonus, request_key))
    db.execute("UPDATE ton_deposit_orders SET status='credited',tx_hash=?,credited_at=CURRENT_TIMESTAMP WHERE id=?",
               (tx_hash, order['id']))
    record_transaction(db, user_id, 'ton_deposit', amount, 'ton_tx', tx_hash, 'Подтверждённое пополнение TON')
    if deposit_bonus:
        record_transaction(db,user_id,'deposit_promo_bonus',deposit_bonus,'ton_tx',tx_hash,
                           f'Бонус промокода {active["code"]}')
    log_event(db,user_id,'deposit_confirmed',amount=amount/100,bonus=deposit_bonus/100,
              promo_code=active['code'] if deposit_bonus else '',transaction=tx_hash)
    if referrer and bonus:
        record_transaction(db, referrer, 'referral_bonus', bonus, 'ton_tx', tx_hash,
                           f'Реферальный бонус {referral_percent():g}% от TON-пополнения пользователя {user_id}')
    return bonus


@app.post('/api/ton/deposit/create')
@login_required
def create_ton_deposit():
    settings = ton_settings()
    if not settings['enabled']:
        return error('Пополнение TON временно отключено.', 503)
    if not settings['recipient_wallet']:
        return error('Администратор ещё не настроил кошелёк получателя.', 503)
    data = request.get_json(silent=True) or {}
    try:
        amount = parse_amount(data.get('amount'))
    except (ValueError, InvalidOperation, TypeError):
        return error('Введите сумму с точностью до 0.01 TON.')
    if not 10 <= amount <= 100000000:
        return error('Сумма пополнения должна быть от 0.10 до 1 000 000 TON.')
    wallet_address = str(data.get('wallet_address') or '').strip()
    if not (20 <= len(wallet_address) <= 180 and re.fullmatch(r'[A-Za-z0-9_:\-+/=]+', wallet_address)):
        return error('Сначала подключите TON-кошелёк.')
    order_id = secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]
    created = int(time.time())
    amount_nano = amount * 10_000_000
    with connect() as db:
        db.execute("UPDATE ton_deposit_orders SET status='expired' WHERE user_id=? AND status='pending'", (session['uid'],))
        promo=db.execute("""SELECT r.code,p.bonus_percent,p.bonus_fixed,p.min_deposit FROM promo_redemptions r
                            JOIN promo_codes p ON p.code=r.code WHERE r.user_id=? AND r.reward_type='deposit_bonus'
                            AND r.consumed_at IS NULL AND r.deactivated_at IS NULL
                            ORDER BY r.created_at DESC LIMIT 1""",(session['uid'],)).fetchone()
        promo_code=promo['code'] if promo else ''
        db.execute('INSERT INTO ton_deposit_orders(id,user_id,wallet_address,recipient_wallet,amount,amount_nano,created_unix,promo_code) VALUES(?,?,?,?,?,?,?,?)',
                   (order_id, session['uid'], wallet_address, settings['recipient_wallet'], amount, amount_nano, created,promo_code))
        db.execute('INSERT INTO user_wallets(user_id,address,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,updated_at=CURRENT_TIMESTAMP',
                   (session['uid'], wallet_address))
        log_event(db,session['uid'],'deposit_created',amount=amount/100,promo_code=promo_code,order_id=order_id)
    return jsonify(ok=True, order_id=order_id, amount=amount/100,
                   deposit_bonus=(round(amount*float(promo['bonus_percent'] or 0)/100)+int(promo['bonus_fixed'] or 0))/100
                   if promo and amount>=int(promo['min_deposit'] or 0) else 0,
                   transaction=dict(validUntil=created + 300,
                                    messages=[dict(address=settings['recipient_wallet'], amount=str(amount_nano))]))


@app.post('/api/ton/deposit/<order_id>/verify')
@login_required
def verify_ton_deposit(order_id):
    if not re.fullmatch(r'[A-Za-z0-9]{8,40}', order_id):
        return error('Некорректная операция.')
    with connect() as db:
        order = db.execute('SELECT * FROM ton_deposit_orders WHERE id=? AND user_id=?',
                           (order_id, session['uid'])).fetchone()
    if not order:
        return error('Операция пополнения не найдена.', 404)
    if order['status'] == 'credited':
        return jsonify(ok=True, status='credited', user=profile())
    if order['status'] == 'expired':
        return error('Эта операция уже устарела. Создайте новое пополнение.', 409)
    if int(time.time()) - int(order['created_unix']) > 900:
        with connect() as db:
            db.execute("UPDATE ton_deposit_orders SET status='expired' WHERE id=? AND status='pending'", (order_id,))
        return error('Транзакция не найдена вовремя. Если TON уже отправлены — обратитесь к администратору.', 409)
    try:
        response = requests.get('https://toncenter.com/api/v3/messages', params={
            'source': order['wallet_address'], 'destination': order['recipient_wallet'],
            'start_utime': max(0, int(order['created_unix']) - 8), 'limit': 50, 'sort': 'desc'
        }, headers=toncenter_headers(), timeout=(5, 12))
        response.raise_for_status()
        messages = response.json().get('messages') or []
        match = None
        for msg in messages:
            try:
                if int(msg.get('value') or 0) != int(order['amount_nano']):
                    continue
            except (TypeError, ValueError):
                continue
            if msg.get('bounced') is True or not msg.get('hash'):
                continue
            check = requests.get('https://toncenter.com/api/v3/transactionsByMessage',
                                 params={'msg_hash': msg['hash'], 'direction': 'in', 'limit': 5},
                                 headers=toncenter_headers(), timeout=(5, 12))
            check.raise_for_status()
            txs = check.json().get('transactions') or []
            good = next((tx for tx in txs if not (tx.get('description') or {}).get('aborted', False)), None)
            if good:
                match = (msg, good)
                break
        if not match:
            return jsonify(ok=True, status='pending')
        msg, tx = match
        tx_hash = str(tx.get('hash') or msg.get('in_msg_tx_hash') or msg['hash'])[:180]
        db = connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            fresh = db.execute('SELECT * FROM ton_deposit_orders WHERE id=?' + (' FOR UPDATE' if DATABASE_URL else ''),
                               (order_id,)).fetchone()
            if fresh['status'] == 'credited':
                db.commit()
                return jsonify(ok=True, status='credited', user=profile())
            used = db.execute("SELECT id FROM ton_deposit_orders WHERE tx_hash=? AND status='credited'", (tx_hash,)).fetchone()
            if used:
                return error('Эта блокчейн-транзакция уже была зачислена.', 409)
            bonus = credit_verified_ton_deposit(db, fresh, tx_hash)
            db.commit()
        finally:
            db.close()
        return jsonify(ok=True, status='credited', referral_bonus=bonus/100, user=profile())
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
        app.logger.exception('TON deposit verification failed')
        return error('Сеть TON пока не подтвердила пополнение. Повторите проверку через несколько секунд.', 502)



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


def saved_portal_key():
    """Return the last admin-supplied Portal Authorization without exposing it to the client."""
    try:
        doc = read_document('portal_auth') or {}
        value = str(doc.get('authorization') or '').strip()
        return value if len(value) <= 8000 and '\n' not in value and '\r' not in value else ''
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ''


def store_portal_key(key):
    key = str(key or '').strip()
    if not key:
        return
    save_document('portal_auth', {
        'authorization': key,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })


def portal_get_collections(session_http, key, params):
    """Fetch one collections page with bounded retries and stale-auth fallback.

    Portals TMA auth can expire. If an old saved token is rejected, retry the
    public collections endpoint before failing, while never deleting the last
    successfully cached catalog.
    """
    auth_candidates = [str(key or '').strip()]
    if auth_candidates[0]:
        auth_candidates.append('')
    last_error = None
    for auth_index, auth_key in enumerate(auth_candidates):
        for attempt in range(3):
            try:
                response = session_http.get(
                    'https://portal-market.com/api/collections',
                    params=params,
                    headers=portal_headers(auth_key),
                    timeout=(5, 10),
                )
                if response.status_code == 429 and attempt < 2:
                    retry_after = response.headers.get('Retry-After', '1')
                    try:
                        delay = min(3.0, max(0.5, float(retry_after)))
                    except ValueError:
                        delay = 1.0
                    time.sleep(delay)
                    continue
                if response.status_code in (401, 403) and auth_key and auth_index == 0:
                    append_portal_log('Сохранённый Authorization Portal отклонён; пробуем публичный каталог без него.', 'error')
                    break
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status in (400, 401, 403, 404) or attempt >= 2:
                    break
                time.sleep(0.7 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise requests.RequestException('Portal did not return a response')


def fetch_portal_catalog(key, progress=None):
    """Fetch Portal collections without blocking the admin UI for the whole import."""
    previous = read_catalog()['gifts']
    previous_by_id = {str(gift.get('id')): gift for gift in previous}
    gifts, seen = [], set()
    offset = 0
    session_http = requests.Session()
    page_signatures = set()
    request_limit = 100
    deadline = time.monotonic() + 55

    # The public collections endpoint is also used by the Portals web app. Some
    # deployments cap a page below the requested limit, so advance by the real
    # number of rows instead of assuming a fixed page size.
    for page in range(30):
        if time.monotonic() >= deadline:
            append_portal_log('Импорт остановлен по защитному лимиту времени; уже полученные коллекции сохранены.', 'error')
            break
        response = portal_get_collections(
            session_http, key, {'limit': request_limit, 'offset': offset}
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError('Portal вернул некорректный JSON.') from exc
        items = payload.get('collections', payload.get('data', payload)) if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = items.get('collections', items.get('items'))
        if not isinstance(items, list):
            raise ValueError('Portal вернул неожиданный формат коллекций.')
        if not items:
            break

        page_ids = tuple(str(item.get('id') or item.get('slug') or item.get('name') or '')
                         for item in items if isinstance(item, dict))
        signature = hashlib.sha1(json.dumps(page_ids, ensure_ascii=False).encode()).hexdigest()
        if signature in page_signatures:
            append_portal_log('Portal повторил уже полученную страницу; импорт завершён без зацикливания.')
            break
        page_signatures.add(signature)

        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get('name') or item.get('title') or item.get('gift_name')
            gift_id = str(item.get('id') or item.get('slug') or name or '')
            if not name or not gift_id or gift_id in seen:
                continue
            seen.add(gift_id)
            added += 1
            raw = next((item[k] for k in ('floor_price', 'floorPrice', 'price') if item.get(k) is not None), None)
            try:
                price = Decimal(str(raw))
                price = (format(price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
                         if price.is_finite() and price >= 0 else None)
            except (InvalidOperation, TypeError, ValueError):
                price = None
            img = next((safe_image(item.get(k)) for k in
                        ('image_url', 'photo_url', 'preview_url', 'image', 'icon_url', 'png_url')
                        if safe_image(item.get(k))), '')
            gift = dict(id=gift_id, name=str(name)[:140], price_ton=price, portal_image_url=img)
            old = previous_by_id.get(gift_id, {})
            gift.update(image_url=old.get('image_url') or img,
                        image_match=bool(old.get('image_match')),
                        telegram_gift_id=old.get('telegram_gift_id', ''))
            gifts.append(gift)

        if not added:
            break
        offset += len(items)

        # Publish a partial catalog after every page. The admin panel can show
        # gifts immediately instead of appearing frozen until PNG matching ends.
        partial = gifts + [g for g in previous if str(g.get('id')) not in seen]
        save_document('portal_catalog', dict(
            source='Portal Market', updated_at=datetime.now(timezone.utc).isoformat(),
            partial=True, gifts=partial))
        if progress:
            progress(len(gifts), 'collections')
        if page == 0 or (page + 1) % 5 == 0:
            append_portal_log(f'Portal: получено {len(gifts)} коллекций (страница {page + 1}).')

        total = None
        if isinstance(payload, dict):
            for key_name in ('total', 'count', 'total_count'):
                try:
                    value = int(payload.get(key_name))
                    if value >= 0:
                        total = value
                        break
                except (TypeError, ValueError):
                    pass
        if total is not None and offset >= total:
            break
    else:
        append_portal_log('Достигнут защитный лимит страниц Portal; полученные коллекции сохранены.', 'error')

    if not gifts:
        raise ValueError('Portal вернул пустой каталог. Прежний каталог сохранён.')

    retained = [g for g in previous if str(g.get('id')) not in seen]
    gifts.extend(retained)
    # Save usable Portal data before optional external PNG matching.
    document = dict(source='Portal Market', updated_at=datetime.now(timezone.utc).isoformat(), gifts=gifts)
    save_catalog(document)
    if progress:
        progress(len(gifts), 'images')

    try:
        mapping = gift_id_map()
    except (requests.RequestException, ValueError, OSError, json.JSONDecodeError):
        mapping = {}
        append_portal_log('Каталог Portal загружен; CDN сопоставления PNG временно недоступен.')
    if mapping:
        names = {collection_key(v): k for k, v in mapping.items()}
        gifts = [match_collection_image(g, mapping, names) for g in gifts]
        document = dict(source='Portal Market', updated_at=datetime.now(timezone.utc).isoformat(), gifts=gifts)
        save_catalog(document)
        refresh_inventory_images(mapping)

    return dict(count=len(gifts), matched=sum(bool(g.get('image_match')) for g in gifts), retained=len(retained))


def append_portal_log(message, level='info'):
    try:
        logs = read_document('portal_logs') or []
        if not isinstance(logs, list):
            logs = []
        logs.append({'ts': datetime.now(timezone.utc).isoformat(), 'level': level, 'message': str(message)[:500]})
        save_document('portal_logs', logs[-100:])
    except Exception:
        app.logger.exception('Could not persist Portal log')


portal_job_lock = __import__('threading').Lock()


def portal_job(key):
    try:
        append_portal_log('Начата загрузка каталога Portal Market.')
        def progress(count, stage='collections'):
            save_document('portal_job', dict(state='running', count=count, stage=stage, updated=time.time()))
        result = fetch_portal_catalog(key, progress)
        save_document('portal_job', dict(state='done', updated=time.time(), **result))
        append_portal_log(f"Каталог сохранён: {result.get('count', 0)} коллекций, PNG: {result.get('matched', 0)}.")
    except requests.HTTPError as exc:
        status = exc.response.status_code
        message = ('Ключ Portal истёк или отклонён. Обновите Authorization из Portal либо очистите поле для публичного каталога.'
                   if status in (401, 403) else f'Portal вернул HTTP {status}. Каталог сохранён; повторите позже.')
        save_document('portal_job', dict(state='error', error=message, updated=time.time()))
        append_portal_log(message, 'error')
    except (requests.RequestException, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Portal не ответил вовремя. Старые подарки сохранены. Повторите загрузку.'
        save_document('portal_job', dict(state='error', error=message, updated=time.time()))
        append_portal_log(message, 'error')
    finally:
        try:
            auto = portal_auto_settings()
            if auto.get('enabled'):
                auto['last_run_at'] = time.time()
                save_portal_auto_settings(auto)
        except Exception:
            app.logger.exception('Could not update Portal auto-refresh timestamp')
        portal_job_lock.release()


def portal_auto_settings():
    doc = read_document('portal_auto_refresh') or {}
    try:
        interval = int(doc.get('interval_minutes', 60))
    except (TypeError, ValueError):
        interval = 60
    interval = min(1440, max(15, interval))
    try:
        next_run_at = float(doc.get('next_run_at') or 0)
    except (TypeError, ValueError):
        next_run_at = 0
    try:
        last_run_at = float(doc.get('last_run_at') or 0)
    except (TypeError, ValueError):
        last_run_at = 0
    return dict(enabled=bool(doc.get('enabled', False)), interval_minutes=interval,
                next_run_at=next_run_at, last_run_at=last_run_at)


def save_portal_auto_settings(settings):
    save_document('portal_auto_refresh', settings)


@app.get('/api/admin/portal/auto')
@admin_required
def portal_auto_get():
    return jsonify(**portal_auto_settings())


@app.post('/api/admin/portal/auto')
@admin_required
def portal_auto_set():
    data = request.get_json(silent=True) or {}
    try:
        interval = int(data.get('interval_minutes', 60))
    except (TypeError, ValueError):
        return error('Интервал автообновления указан неверно.')
    if not 15 <= interval <= 1440:
        return error('Интервал автообновления: от 15 до 1440 минут.')
    enabled = bool(data.get('enabled', False))
    now = time.time()
    previous = portal_auto_settings()
    settings = dict(enabled=enabled, interval_minutes=interval,
                    next_run_at=(now + interval * 60 if enabled else 0),
                    last_run_at=previous.get('last_run_at', 0),
                    updated_at=datetime.now(timezone.utc).isoformat(), admin_id=session['uid'])
    save_portal_auto_settings(settings)
    append_portal_log(f'Автообновление цен: {"включено" if enabled else "выключено"}; интервал {interval} мин.')
    return jsonify(ok=True, **portal_auto_settings())


@app.post('/api/admin/portal/import')
@admin_required
def portal_import():
    entered_key = str((request.get_json(silent=True) or {}).get('key', '')).strip()
    if len(entered_key) > 8000 or '\n' in entered_key or '\r' in entered_key:
        return error('Некорректный ключ Portal.')
    if entered_key:
        store_portal_key(entered_key)
    key = entered_key or saved_portal_key()
    if not portal_job_lock.acquire(blocking=False):
        return jsonify(ok=True, state='running'), 202
    save_document('portal_job', dict(state='running', count=0, stage='collections', updated=time.time()))
    append_portal_log('Используется сохранённый Authorization Portal.' if key and not entered_key else
                      ('Authorization Portal сохранён для следующих обновлений.' if entered_key else
                       'Authorization не задан; пробуем публичный каталог Portal.'))
    Thread(target=portal_job, args=(key,), daemon=True).start()
    return jsonify(ok=True, state='running'), 202


@app.get('/api/admin/portal/job')
@admin_required
def portal_job_status():
    job = read_document('portal_job') or dict(state='idle')
    if job.get('state') == 'running' and time.time() - job.get('updated', 0) > 120:
        job = dict(state='error', error='Загрузка прервалась при перезапуске сервера. Повторите импорт.')
    return jsonify(job)


@app.get('/api/admin/portal/logs')
@admin_required
def portal_logs():
    logs = read_document('portal_logs') or []
    return jsonify(logs=logs[-100:] if isinstance(logs, list) else [])


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


@app.get('/tonconnect-manifest.json')
def tonconnect_manifest():
    base = WEBAPP_URL or request.url_root.rstrip('/')
    settings = ton_settings()
    site_url = settings['site_url'] if settings['site_url'].startswith('https://') else base
    icon_url = settings['icon_url'] if settings['icon_url'].startswith('https://') else base + '/static/img/ton.png'
    return jsonify(url=site_url, name=settings['site_name'], iconUrl=icon_url)


def welcome_text():
    pct = referral_percent()
    pct_text = f'{pct:.1f}'.rstrip('0').rstrip('.')
    return (
        '🎉 <b>Привет, добро пожаловать в GemDrop! 💎</b>\n\n'
        'Открывай Mines и собирай подарки.\n\n'
        f'💰 Делись своей реферальной ссылкой: {pct_text}% начисляется только с подтверждённых TON-пополнений приглашённых друзей.'
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
        referrer = None
        if len(command) == 2 and re.fullmatch(r'ref_[0-9]{1,20}', command[1]):
            referrer = int(command[1][4:])
        db = connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('INSERT OR IGNORE INTO bot_updates(update_id) VALUES(?)', (update_id,)).rowcount:
                db.commit()
                return jsonify(ok=True)
            db.execute("""INSERT OR IGNORE INTO users(id,name,username,balance) VALUES(?,?,?,0)""",
                       (uid, str(sender.get('first_name') or 'Игрок')[:80], str(sender.get('username') or '')[:80]))
            db.execute('UPDATE users SET name=?,username=? WHERE id=?',
                       (str(sender.get('first_name') or 'Игрок')[:80], str(sender.get('username') or '')[:80], uid))
            if referrer and uid != referrer:
                if db.execute('SELECT 1 FROM users WHERE id=?', (referrer,)).fetchone():
                    db.execute('INSERT OR IGNORE INTO referrals(referred_id,referrer_id) VALUES(?,?)', (uid, referrer))
            db.commit()
        finally:
            db.close()
        play_url = WEBAPP_URL + ('/?ref=' + str(referrer) if referrer else '/')
        button = {'inline_keyboard': [[{'text': '🎮 Играть', 'web_app': {'url': play_url}}]]}
        # Telegram can execute a Bot API method directly from the webhook response.
        # This removes one extra outbound HTTP request and makes /start visibly faster.
        return jsonify(method='sendMessage', chat_id=chat['id'], text=welcome_text(),
                       reply_markup=button, parse_mode='HTML')
    except (ValueError, KeyError, sqlite3.Error) as exc:
        app.logger.warning('Telegram update failed: %s', type(exc).__name__)
        if isinstance(update.get('update_id'), int):
            with connect() as db:
                db.execute('DELETE FROM bot_updates WHERE update_id=?', (update['update_id'],))
        return error('Не удалось обработать команду.', 500)


@app.errorhandler(500)
def internal_error_handler(exc):
    app.logger.exception('Unhandled server error: %s', exc)
    if request.path.startswith('/api/') or request.path.startswith('/telegram/'):
        return error('Внутренняя ошибка сервера. Ошибка записана в лог.', 500)
    return 'Internal Server Error', 500



def portal_auto_loop():
    # Best-effort scheduler for a continuously running Render web service.
    # The schedule itself lives in the persistent DB, so deploys do not erase it.
    time.sleep(8)
    while True:
        try:
            settings = portal_auto_settings()
            now = time.time()
            if settings.get('enabled') and (not settings.get('next_run_at') or now >= settings['next_run_at']):
                settings['next_run_at'] = now + settings['interval_minutes'] * 60
                save_portal_auto_settings(settings)
                if portal_job_lock.acquire(blocking=False):
                    append_portal_log('Автообновление: запускаем обновление цен Portal.')
                    Thread(target=portal_job, args=(saved_portal_key(),), daemon=True).start()
        except Exception:
            app.logger.exception('Portal auto-refresh loop failed')
        time.sleep(30)


Thread(target=portal_auto_loop, daemon=True).start()


def configure_bot():
    global BOT_USERNAME
    if not BOT_TOKEN or not WEBAPP_URL.startswith('https://'):
        return
    last_error = None
    for attempt in range(5):
        try:
            info = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=(2, 4))
            info.raise_for_status()
            if info.json().get('ok'):
                BOT_USERNAME = info.json()['result'].get('username') or BOT_USERNAME
                save_document('bot_identity', {'username': BOT_USERNAME,
                                                'token_fingerprint': BOT_TOKEN_FINGERPRINT,
                                                'updated_at': datetime.now(timezone.utc).isoformat()})
            response = requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/setWebhook',
                                     json={'url': WEBAPP_URL + '/telegram/webhook',
                                           'secret_token': WEBHOOK_SECRET,
                                           'allowed_updates': ['message'],
                                           'max_connections': 40,
                                           'drop_pending_updates': False}, timeout=(3, 6))
            response.raise_for_status()
            if not response.json().get('ok'):
                raise ValueError('setWebhook rejected')
            app.logger.info('Telegram webhook configured for %s', WEBAPP_URL)
            return
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_error = exc
            time.sleep(min(8, 1.5 ** attempt))
    app.logger.warning('Telegram webhook setup failed after retries: %s',
                       type(last_error).__name__ if last_error else 'unknown')


if BOT_TOKEN and WEBAPP_URL.startswith('https://'):
    Thread(target=configure_bot, daemon=True).start()


repair_legacy_upgrade_wagers()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
