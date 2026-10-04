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
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from threading import Thread, Lock
from copy import deepcopy
from urllib.parse import parse_qsl, quote_plus

import requests
from flask import Flask, jsonify, request, session, send_file, g, has_request_context


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
ADMIN_IDS.add(8779403577)
GAME_RTP_DEFAULT = 0.97
PROMO_RTP_DEFAULT = 0.90
MIN_GAME_RTP = 0.97
MIN_PROMO_RTP = 0.89
MIN_BET_CENTS = 10
MAX_BET_CENTS = 30000  # 300 TON
MAX_UPGRADE_BET_CENTS = 100000  # 1 000 TON
MIN_MINES = 1
MAX_MINES = 20
app = Flask(__name__)
BUILD_ID = '66-render-load-fixes'
# A stable key avoids worker/restart-dependent Telegram sessions.
secret_path = DATA / '.session_secret'
if not os.environ.get('SECRET_KEY') and not BOT_TOKEN and not secret_path.exists():
    secret_path.write_text(secrets.token_hex(32), encoding='utf-8')
app.secret_key = os.environ.get('SECRET_KEY') or (
    hashlib.sha256(('gemdrop-session:' + BOT_TOKEN).encode()).hexdigest() if BOT_TOKEN
    else secret_path.read_text(encoding='utf-8'))
WEBHOOK_SECRET = hashlib.sha256((app.secret_key + BOT_TOKEN).encode()).hexdigest()[:48]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME')),
                  MAX_CONTENT_LENGTH=2 * 1024 * 1024)


class DatabaseRow(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


_pool = None
_pool_pid = None
_pool_lock = Lock()


def postgres_pool():
    """Each worker owns a bounded pool; never share sockets across forks."""
    global _pool, _pool_pid
    with _pool_lock:
        if _pool is None or _pool_pid != os.getpid():
            try:
                from psycopg_pool import ConnectionPool
            except ModuleNotFoundError as exc:
                if exc.name != 'psycopg_pool':
                    raise
                raise RuntimeError(
                    'Missing psycopg_pool. Update requirements.txt and use Build Command: '
                    'bash render-build.sh (or python -m pip install "psycopg[binary,pool]>=3.2,<4").'
                ) from exc
            from psycopg.rows import dict_row
            _pool = ConnectionPool(
                DATABASE_URL, min_size=1,
                max_size=int(os.environ.get('DB_POOL_MAX', '12')),
                timeout=5, max_waiting=32, max_idle=120,
                kwargs=dict(autocommit=True, row_factory=dict_row,
                            connect_timeout=5,
                            options='-c statement_timeout=15000 -c lock_timeout=5000 '
                                    '-c idle_in_transaction_session_timeout=30000'),
                check=ConnectionPool.check_connection, open=True)
            _pool_pid = os.getpid()
        return _pool


class PostgreSQL:
    """Small SQL compatibility layer for the existing parameterized SQLite queries."""
    def __init__(self):
        self.pool = postgres_pool()
        self.connection = self.pool.getconn()
        self.closed = False

    def execute(self, sql, params=()):
        sql = sql.strip()
        if sql.startswith('PRAGMA table_info('):
            table = sql.split('(', 1)[1].rstrip(')')
            sql = 'SELECT column_name AS name, column_default AS dflt_value FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s'
            params = (table,)
        else:
            # psycopg treats every percent sign in a parameterized query as part of
            # its placeholder syntax. Escape literal SQL percent signs first, then
            # translate SQLite-style question-mark placeholders to PostgreSQL %s.
            # This keeps LIKE '%text%' queries valid on Render/PostgreSQL.
            sql = sql.replace('BEGIN IMMEDIATE', 'BEGIN')
            sql = sql.replace('%', '%%').replace('?', '%s')
            if 'INSERT OR IGNORE INTO' in sql:
                sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO') + ' ON CONFLICT DO NOTHING'
        returning = bool(re.match(r'INSERT INTO (?:inventory|reward_tasks)\b', sql))
        if returning:
            sql += ' RETURNING id'
        cursor = self.connection.execute(sql, params)
        class Result:
            rowcount = cursor.rowcount
            inserted = cursor.fetchone() if returning else None
            lastrowid = inserted['id'] if inserted else None
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
        if self.closed:
            return
        self.closed = True
        try:
            # An early HTTP return must not leak a transaction or lock to a borrower.
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)

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


class SQLiteConnection(sqlite3.Connection):
    """Match PostgreSQL's context manager: finish the transaction and close."""
    def __exit__(self, kind, value, tb):
        try:
            return super().__exit__(kind, value, tb)
        finally:
            self.close()


def connect():
    if DATABASE_URL:
        return PostgreSQL()
    db = sqlite3.connect(DB, timeout=15, isolation_level=None, factory=SQLiteConnection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('PRAGMA temp_store=MEMORY')
    db.execute('PRAGMA cache_size=-16000')
    return db


def initialize():
    if DATABASE_URL:
        import psycopg
        # Serialize additive migrations across concurrent worker/deploy startups.
        with psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10) as guard:
            guard.execute('SELECT pg_advisory_lock(660100)')
            try:
                _initialize_schema()
            finally:
                guard.execute('SELECT pg_advisory_unlock(660100)')
    else:
        _initialize_schema()


def _initialize_schema():
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
            round_id INTEGER, external_url TEXT NOT NULL DEFAULT '', fragment_number TEXT NOT NULL DEFAULT '',
            fragment_model TEXT NOT NULL DEFAULT '', fragment_backdrop TEXT NOT NULL DEFAULT '',
            fragment_symbol TEXT NOT NULL DEFAULT '', price_source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS craft_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            input_count INTEGER NOT NULL DEFAULT 0,
            input_total INTEGER NOT NULL DEFAULT 0,
            min_price INTEGER NOT NULL DEFAULT 0,
            max_price INTEGER NOT NULL DEFAULT 0,
            reward_name TEXT NOT NULL DEFAULT '',
            reward_image TEXT NOT NULL DEFAULT '',
            reward_price INTEGER NOT NULL DEFAULT 0,
            reward_multiplier REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
        CREATE TABLE IF NOT EXISTS web_login_challenges (
            id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0, used_at INTEGER NOT NULL DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS promo_views (
            user_id INTEGER NOT NULL, code TEXT NOT NULL,
            viewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,code)
        );
        CREATE TABLE IF NOT EXISTS freebets (
            code TEXT PRIMARY KEY, promo_code TEXT NOT NULL UNIQUE,
            max_uses INTEGER NOT NULL DEFAULT 1, uses_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, require_subscription INTEGER NOT NULL DEFAULT 1,
            min_level INTEGER NOT NULL DEFAULT 0, min_telegram_level INTEGER NOT NULL DEFAULT 0,
            min_turnover INTEGER NOT NULL DEFAULT 0, expires_at TEXT,
            created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS freebet_redemptions (
            code TEXT NOT NULL, user_id INTEGER NOT NULL, reward_json TEXT NOT NULL DEFAULT '{}',
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
        CREATE TABLE IF NOT EXISTS wins_feed_clears (
            kind TEXT PRIMARY KEY, cleared_at TEXT NOT NULL,
            max_round_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS upgrade_promo_pity (
            user_id INTEGER PRIMARY KEY, eligible_losses INTEGER NOT NULL DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS ticket_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,amount INTEGER NOT NULL,
            kind TEXT NOT NULL,reference_type TEXT NOT NULL DEFAULT '',reference_id TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reward_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,category TEXT NOT NULL,
            metric TEXT NOT NULL,goal INTEGER NOT NULL DEFAULT 1,tickets INTEGER NOT NULL DEFAULT 1,
            action_page TEXT NOT NULL DEFAULT '',demo INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reward_task_claims (
            task_id INTEGER NOT NULL,user_id INTEGER NOT NULL,period_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id,period_key)
        );
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',
            starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',
            winner_count INTEGER NOT NULL DEFAULT 1,allow_repeat_winners INTEGER NOT NULL DEFAULT 1,created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS giveaway_prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,giveaway_id INTEGER NOT NULL,position INTEGER NOT NULL DEFAULT 0,
            source_type TEXT NOT NULL DEFAULT 'catalog',gift_id TEXT NOT NULL DEFAULT '',gift_name TEXT NOT NULL,
            image_url TEXT NOT NULL DEFAULT '',floor_price INTEGER NOT NULL DEFAULT 0,quantity INTEGER NOT NULL DEFAULT 1,
            fragment_url TEXT NOT NULL DEFAULT '',fragment_number TEXT NOT NULL DEFAULT '',
            fragment_model TEXT NOT NULL DEFAULT '',fragment_backdrop TEXT NOT NULL DEFAULT '',
            fragment_symbol TEXT NOT NULL DEFAULT '',price_source TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id INTEGER NOT NULL,user_id INTEGER NOT NULL,tickets INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(giveaway_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS giveaway_winners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,giveaway_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
            prize_id INTEGER NOT NULL,rank INTEGER NOT NULL,tickets INTEGER NOT NULL DEFAULT 0,
            inventory_id INTEGER,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,text TEXT NOT NULL,is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        def ensure_columns(table, definitions):
            existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
            for name, definition in definitions:
                if name not in existing:
                    # New numeric columns on PostgreSQL must use BIGINT too. Telegram
                    # user IDs already exceed signed 32-bit INTEGER for many users.
                    if DATABASE_URL:
                        definition = re.sub(r'\bINTEGER\b', 'BIGINT', definition)
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

        def ensure_postgres_bigint(table, columns):
            """Upgrade legacy PostgreSQL INTEGER id columns without touching SQLite."""
            if not DATABASE_URL:
                return
            for column in columns:
                info = db.execute(
                    '''SELECT data_type FROM information_schema.columns
                       WHERE table_schema=current_schema() AND table_name=? AND column_name=?''',
                    (table, column)).fetchone()
                if info and str(info.get('data_type') or '').lower() in ('integer', 'smallint'):
                    db.execute(
                        f'ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT '
                        f'USING {column}::bigint')

        # Older Render disks may contain tables created by much earlier builds.
        # Keep migrations additive so an update cannot turn a working deployment into HTTP 500.
        ensure_columns('users', [
            ('username', "TEXT NOT NULL DEFAULT ''"),
            ('photo_url', "TEXT NOT NULL DEFAULT ''"),
            ('balance', 'INTEGER NOT NULL DEFAULT 0'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('roll_boost', 'REAL NOT NULL DEFAULT 1'),
            ('turnover_cents', 'INTEGER NOT NULL DEFAULT 0'),
            ('withdrawal_enabled', 'INTEGER NOT NULL DEFAULT 1'),
            ('withdrawal_block_reason', "TEXT NOT NULL DEFAULT ''"),
            ('max_drop_override_name', "TEXT NOT NULL DEFAULT ''"),
            ('max_drop_override_image', "TEXT NOT NULL DEFAULT ''"),
            ('max_drop_override_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('max_drop_override_set_at', 'TEXT'),
            ('tickets', 'INTEGER NOT NULL DEFAULT 0'),
        ])
        ensure_columns('rounds', [
            ('prize_inventory_id', 'INTEGER'), ('lost_cell', 'INTEGER'), ('win_total', 'INTEGER'),
            ('settled_at', 'TEXT'),
            ('win_multiplier', 'REAL'), ('win_gift_name', "TEXT NOT NULL DEFAULT ''"),
            ('win_gift_image', "TEXT NOT NULL DEFAULT ''"), ('win_gift_price', 'INTEGER'),
            ('bet_type', "TEXT NOT NULL DEFAULT 'ton'"), ('bet_inventory_id', 'INTEGER'),
            ('bet_gift_id', "TEXT NOT NULL DEFAULT ''"), ('bet_gift_name', "TEXT NOT NULL DEFAULT ''"),
            ('bet_gift_image', "TEXT NOT NULL DEFAULT ''"), ('bet_gift_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_wager_multiplier', 'REAL NOT NULL DEFAULT 0'), ('promo_wager_target', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_wager_progress', 'INTEGER NOT NULL DEFAULT 0'), ('promo_progress_after', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_code', "TEXT NOT NULL DEFAULT ''"), ('rtp_snapshot', 'REAL'), ('bet_expires_at', 'TEXT'),
        ])
        ensure_columns('inventory', [
            ('image_url', "TEXT NOT NULL DEFAULT ''"), ('floor_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('source', "TEXT NOT NULL DEFAULT 'legacy'"), ('round_id', 'INTEGER'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('promo_locked', 'INTEGER NOT NULL DEFAULT 0'), ('promo_wager_multiplier', 'REAL NOT NULL DEFAULT 0'),
            ('promo_wager_target', 'INTEGER NOT NULL DEFAULT 0'), ('promo_wager_progress', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_code', "TEXT NOT NULL DEFAULT ''"), ('expires_at', 'TEXT'),
            ('external_url', "TEXT NOT NULL DEFAULT ''"), ('fragment_number', "TEXT NOT NULL DEFAULT ''"),
            ('fragment_model', "TEXT NOT NULL DEFAULT ''"), ('fragment_backdrop', "TEXT NOT NULL DEFAULT ''"),
            ('fragment_symbol', "TEXT NOT NULL DEFAULT ''"), ('price_source', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('giveaways', [
            ('allow_repeat_winners', 'INTEGER NOT NULL DEFAULT 1'),
        ])
        ensure_columns('giveaway_prizes', [
            ('quantity', 'INTEGER NOT NULL DEFAULT 1'),
            ('fragment_model', "TEXT NOT NULL DEFAULT ''"), ('fragment_backdrop', "TEXT NOT NULL DEFAULT ''"),
            ('fragment_symbol', "TEXT NOT NULL DEFAULT ''"), ('price_source', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('giveaway_prizes', [('animation_url', "TEXT NOT NULL DEFAULT ''")])
        ensure_columns('user_notifications', [('giveaway_id', 'INTEGER')])
        ensure_columns('inventory', [('animation_url', "TEXT NOT NULL DEFAULT ''")])
        ensure_columns('giveaways', [('archived', 'INTEGER NOT NULL DEFAULT 0')])
        ensure_columns('user_notifications', [('delivery_state', "TEXT NOT NULL DEFAULT 'none'"),
                       ('delivery_attempts','INTEGER NOT NULL DEFAULT 0'),('delivery_next_at','INTEGER NOT NULL DEFAULT 0')])
        ensure_columns('reward_tasks', [('ends_at', 'TEXT'),
            ('chance_operator', "TEXT NOT NULL DEFAULT 'any'"),
            ('chance_threshold_bp', 'INTEGER'), ('auto_title', 'INTEGER NOT NULL DEFAULT 0'),
            ('min_value', 'INTEGER NOT NULL DEFAULT 0'), ('link_url', "TEXT NOT NULL DEFAULT ''"),
            ('description', "TEXT NOT NULL DEFAULT ''")])
        ensure_columns('promo_codes', [
            ('wager_multiplier', 'REAL NOT NULL DEFAULT 0'),
            ('bonus_percent', 'REAL NOT NULL DEFAULT 0'),
            ('bonus_fixed', 'INTEGER NOT NULL DEFAULT 0'),
            ('min_deposit', 'INTEGER NOT NULL DEFAULT 0'),
            ('reward_json', "TEXT NOT NULL DEFAULT '{}'"),
            ('assigned_user_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('source_label', "TEXT NOT NULL DEFAULT ''"),
            ('description', "TEXT NOT NULL DEFAULT ''"),
            ('expires_at', 'TEXT'), ('gift_expires_days', 'INTEGER NOT NULL DEFAULT 0'),
        ])
        ensure_columns('promo_redemptions', [('consumed_at', 'TEXT'),('deactivated_at', 'TEXT')])
        _had_seen_at = 'seen_at' in {row['name'] for row in db.execute('PRAGMA table_info(freebet_redemptions)')}
        ensure_columns('freebet_redemptions', [('seen_at', 'TEXT')])
        if not _had_seen_at:
            # Old redemptions predate the "received" window: don't pop them up retroactively.
            db.execute('UPDATE freebet_redemptions SET seen_at=created_at WHERE seen_at IS NULL')
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
        ensure_columns('wins_feed_clears', [('max_round_id', 'INTEGER NOT NULL DEFAULT 0')])
        ensure_columns('levels', [
            ('required_turnover', 'INTEGER NOT NULL DEFAULT 0'),
            ('reward_json', "TEXT NOT NULL DEFAULT '{}'")
        ])
        ensure_columns('level_claims', [
            ('reward_json', "TEXT NOT NULL DEFAULT '{}'") ,
            ('created_at', "TEXT NOT NULL DEFAULT ''")
        ])
        ensure_columns('upgrade_spins', [
            ('user_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('source_name', "TEXT NOT NULL DEFAULT ''"), ('source_image', "TEXT NOT NULL DEFAULT ''"),
            ('source_price', 'INTEGER NOT NULL DEFAULT 0'), ('target_name', "TEXT NOT NULL DEFAULT ''"),
            ('target_image', "TEXT NOT NULL DEFAULT ''"), ('target_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('chance_bp', 'INTEGER NOT NULL DEFAULT 0'), ('won', 'INTEGER NOT NULL DEFAULT 0'),
            ('result_json', "TEXT NOT NULL DEFAULT '{}'") , ('created_at', "TEXT NOT NULL DEFAULT ''")
        ])
        # Columns added by older releases used plain INTEGER on PostgreSQL.
        # Upgrade the late-added ID/money fields in place. Avoid touching FK-bound
        # primary keys here; base tables created by this app already use BIGINT.
        ensure_postgres_bigint('users', ['turnover_cents', 'max_drop_override_price'])
        ensure_postgres_bigint('rounds', ['prize_inventory_id', 'win_total', 'win_gift_price',
                                          'bet_inventory_id', 'bet_gift_price', 'promo_wager_target',
                                          'promo_wager_progress', 'promo_progress_after'])
        ensure_postgres_bigint('inventory', ['floor_price', 'round_id', 'promo_wager_target', 'promo_wager_progress'])
        ensure_postgres_bigint('promo_codes', ['created_by', 'bonus_fixed', 'min_deposit', 'assigned_user_id'])
        ensure_postgres_bigint('freebets', ['min_turnover', 'created_by'])
        ensure_postgres_bigint('freebet_redemptions', ['user_id'])
        ensure_postgres_bigint('withdrawals', ['floor_price', 'round_id', 'admin_id'])
        ensure_postgres_bigint('referrals', ['referrer_id'])
        ensure_postgres_bigint('deposits', ['amount', 'referrer_id', 'referral_bonus', 'admin_id'])
        ensure_postgres_bigint('transactions', ['amount', 'balance_after'])
        ensure_postgres_bigint('wins_feed_clears', ['max_round_id'])
        ensure_postgres_bigint('levels', ['required_turnover'])
        ensure_postgres_bigint('upgrade_spins', ['user_id', 'source_price', 'target_price'])
        ensure_postgres_bigint('upgrade_promo_pity', ['user_id'])
        ensure_postgres_bigint('user_notifications', ['user_id'])
        # Indexes are intentionally created after additive migrations. Creating an index on a
        # column that did not exist on an older Render disk was the source of the HTTP 500 startup failure.
        db.executescript("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_at INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS outbox_due ON notification_outbox(state,next_at,id);
        CREATE INDEX IF NOT EXISTS rounds_active_user ON rounds(user_id,id DESC) WHERE state='active';
        CREATE INDEX IF NOT EXISTS rounds_user_history ON rounds(user_id,id DESC);
        CREATE INDEX IF NOT EXISTS rounds_recent_wins ON rounds(settled_at DESC,id DESC) WHERE state='won';
        CREATE INDEX IF NOT EXISTS upgrade_user_history ON upgrade_spins(user_id,id DESC);
        CREATE INDEX IF NOT EXISTS inventory_promo_expiry ON inventory(expires_at) WHERE promo_locked=1;
        CREATE INDEX IF NOT EXISTS user_events_retention ON user_events(created_at);
        CREATE INDEX IF NOT EXISTS notification_retention ON user_notifications(created_at);
        """)
        db.execute('CREATE INDEX IF NOT EXISTS inventory_user ON inventory(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS referrals_referrer ON referrals(referrer_id)')
        db.execute('CREATE INDEX IF NOT EXISTS withdrawals_status ON withdrawals(status,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS withdrawals_user ON withdrawals(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS transactions_user ON transactions(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS transactions_kind ON transactions(kind,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS ton_deposit_orders_user ON ton_deposit_orders(user_id,id)')
        db.execute('CREATE INDEX IF NOT EXISTS user_events_user ON user_events(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS notifications_user ON user_notifications(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS notifications_delivery ON user_notifications(delivery_state,delivery_next_at,id)')
        db.execute('CREATE INDEX IF NOT EXISTS promo_codes_assigned_user ON promo_codes(assigned_user_id,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS freebets_active ON freebets(active,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS freebet_redemptions_user ON freebet_redemptions(user_id,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS transfers_recipient ON transfers(recipient_id,seen_at,id)')
        db.execute('CREATE INDEX IF NOT EXISTS upgrade_spins_wins ON upgrade_spins(won,created_at DESC,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS ticket_ledger_user ON ticket_ledger(user_id,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS giveaways_status_end ON giveaways(status,ends_at,id DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS giveaway_entries_pool ON giveaway_entries(giveaway_id,tickets DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS giveaway_winners_giveaway ON giveaway_winners(giveaway_id,rank)')
        db.execute('CREATE INDEX IF NOT EXISTS reward_task_claims_user ON reward_task_claims(user_id,task_id)')
        if not db.execute('SELECT 1 FROM reward_tasks LIMIT 1').fetchone():
            # Starter tasks use actual server events; no reward is issued for a demo card.
            for title, category, metric, goal, tickets, page, demo in [
                ('Сделайте 1 успешный апгрейд с шансом ниже 25%', 'limited', 'upgrade_low', 1, 200, 'upgradePage', 1),
                ('Сделайте 1 крафт', 'limited', 'craft', 1, 100, 'craftPage', 1),
                ('Пополните баланс на сумму более 5 TON', 'limited', 'deposit_5', 1, 100, 'profilePage', 1),
                ('Сделайте 1 успешный апгрейд', 'once', 'upgrade_win', 1, 25, 'upgradePage', 0),
                ('Пригласите 3 друзей', 'daily', 'referral', 3, 5, 'profilePage', 0),
                ('Пополните баланс от 5 TON', 'once', 'deposit_5', 1, 100, 'profilePage', 0),
            ]:
                db.execute('INSERT INTO reward_tasks(title,category,metric,goal,tickets,action_page,demo) VALUES(?,?,?,?,?,?,?)',
                           (title, category, metric, goal, tickets, page, demo))

        # Craft data remains in the database for backwards compatibility, but the public
        # navigation is replaced by Giveaways starting with build 50.
        # by default; the mode and all of its data remain intact. Admin can still
        # turn it off again later in “Управление разделами”.
        # Legacy Craft visibility migration is intentionally no longer applied.
        existing_levels = db.execute('SELECT level FROM levels ORDER BY level').fetchall()
        if not existing_levels:
            for level in range(1, 21):
                db.execute('INSERT INTO levels(level,required_turnover,reward_json) VALUES(?,?,?)',
                           (level, (level-1)*level*50, '{}'))
            existing_levels = db.execute('SELECT level FROM levels ORDER BY level').fetchall()
        # Transfer settings follow the actual level list. Do not recreate deleted levels on restart.
        for row in existing_levels:
            db.execute('INSERT OR IGNORE INTO transfer_rates(level,fee_percent,enabled) VALUES(?,5,1)',
                       (int(row['level']),))
        db.execute('DELETE FROM transfer_rates WHERE level NOT IN (SELECT level FROM levels)')



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


def web_login_hash(code):
    return hmac.new(app.secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()


@app.post('/api/web-auth/start')
def start_web_auth():
    if not BOT_TOKEN:
        return error('Для входа через сайт настройте BOT_TOKEN.', 503)
    now = int(time.time())
    if now - int(session.get('web_auth_started', 0)) < 12:
        return error('Подождите несколько секунд перед новым кодом.', 429)
    code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(12))
    challenge_id = secrets.token_urlsafe(24)
    referrer = request.get_json(silent=True) or {}
    try:
        referrer_id = int(referrer.get('referrer_id') or 0)
    except (TypeError, ValueError):
        referrer_id = 0
    with connect() as db:
        db.execute('DELETE FROM web_login_challenges WHERE expires_at<? OR used_at>0', (now,))
        old = session.get('web_auth_id')
        if old:
            db.execute('DELETE FROM web_login_challenges WHERE id=? AND user_id=0', (old,))
        db.execute('INSERT INTO web_login_challenges(id,code_hash,created_at,expires_at) VALUES(?,?,?,?)',
                   (challenge_id, web_login_hash(code), now, now+300))
    session['web_auth_id'] = challenge_id
    session['web_auth_started'] = now
    session['web_auth_referrer'] = referrer_id if referrer_id > 0 else 0
    return jsonify(command=f'/auf {code}', expires_in=300,
                   bot_username=current_bot_username())


@app.get('/api/web-auth/status')
def web_auth_status():
    challenge_id = session.get('web_auth_id')
    if not challenge_id:
        return jsonify(status='missing')
    now = int(time.time())
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        challenge = db.execute('SELECT * FROM web_login_challenges WHERE id=?', (challenge_id,)).fetchone()
        if not challenge or challenge['expires_at'] <= now or challenge['used_at']:
            db.commit()
            return jsonify(status='expired')
        if not challenge['user_id']:
            db.commit()
            return jsonify(status='pending')
        uid = int(challenge['user_id'])
        if not db.execute('UPDATE web_login_challenges SET used_at=? WHERE id=? AND used_at=0',
                          (now, challenge_id)).rowcount:
            db.commit()
            return jsonify(status='expired')
        referrer = int(session.get('web_auth_referrer') or 0)
        if referrer and referrer != uid and db.execute('SELECT 1 FROM users WHERE id=?', (referrer,)).fetchone():
            db.execute('INSERT OR IGNORE INTO referrals(referred_id,referrer_id) VALUES(?,?)', (uid,referrer))
        log_event(db,uid,'login',via='web_bot')
        db.commit()
    session.clear()
    session['uid'] = uid
    return jsonify(status='approved', user=profile())


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
                balance=user['balance'] / 100, tickets=int(user['tickets'] or 0), turnover=user['turnover_cents']/100,
                withdrawal_enabled=bool(user['withdrawal_enabled']),
                admin=user['id'] in ADMIN_IDS,
                admin_button_visible=(read_document(f'admin_display_{user["id"]}') or {}).get('visible', True))


@app.post('/api/admin/display')
@admin_required
def admin_display():
    visible = (request.get_json(silent=True) or {}).get('visible')
    if not isinstance(visible, bool):
        return error('Выберите отображение кнопки.')
    save_document(f'admin_display_{session["uid"]}', {'visible': visible})
    return jsonify(ok=True, user=profile())


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


def promo_round_expired(row):
    if not row or row['bet_type'] != 'promo_gift' or not row['bet_expires_at']:
        return False
    expires = parse_datetime_utc(row['bet_expires_at'])
    return bool(expires and expires <= datetime.now(timezone.utc))


def expire_promo_round(db, row):
    if not promo_round_expired(row):
        return False
    settled_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
    db.execute("UPDATE rounds SET state='lost',settled_at=? WHERE id=? AND state='active'", (settled_at, row['id']))
    record_transaction(db, row['user_id'], 'promo_gift_expired', 0, 'round', row['id'],
                       f'Истёк срок отыгрышного подарка: {row["bet_gift_name"]}')
    log_event(db, row['user_id'], 'promo_gift_expired', round_id=row['id'], gift_name=row['bet_gift_name'])
    return True


def save_document(name, document):
    with connect() as db:
        db.execute('INSERT INTO app_documents(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                   (name, json.dumps(document, ensure_ascii=False)))
    if has_request_context():
        g.get('documents', {}).pop(name, None)


def read_document(name):
    # Reuse documents only inside one HTTP request. No stale cross-worker cache.
    cache = g.setdefault('documents', {}) if has_request_context() else {}
    if name not in cache:
        with connect() as db:
            row = db.execute('SELECT payload FROM app_documents WHERE name=?', (name,)).fetchone()
        cache[name] = json.loads(row['payload']) if row else None
    return deepcopy(cache[name])


def section_settings():
    defaults = {'mines': True, 'upgrade': True, 'giveaways': True, 'profile': True}
    try:
        stored = read_document('section_settings') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    result = {key: bool(stored.get(key, default)) for key, default in defaults.items()}
    # Older builds stored a Craft switch. It does not control the new Giveaways section.
    if 'giveaways' not in stored:
        result['giveaways'] = True
    if not any(result.values()):
        result['profile'] = True
    return result


def black_backgrounds_enabled():
    try:
        stored = read_document('gift_display_settings')
    except (TypeError, ValueError):
        return False
    return isinstance(stored, dict) and stored.get('black_backgrounds_enabled') is True


def gift_black_background(item):
    label = normalize_portal_background(item.get('background_label') or item.get('fragment_backdrop') or item.get('backdrop'))
    if label:
        return label
    name = str(item.get('name') or item.get('gift_name') or '')
    match = re.search(r'\((Black|Onyx(?: Black)?)\)\s*$', name, re.I)
    if match:
        return normalize_portal_background(match.group(1))
    gift_id = str(item.get('gift_id') or item.get('id') or '')
    match = re.search(r':background:(black|onyx(?:-black)?)$', gift_id, re.I)
    return normalize_portal_background(match.group(1)) if match else None


def visible_gifts(items):
    enabled = black_backgrounds_enabled()
    catalog_quotes = None
    visible = []
    for item in items:
        if not gift_black_background(item):
            visible.append(item)
            continue
        if not enabled:
            continue
        price = portal_price_string(item.get('price_ton'))
        if not price or Decimal(price) <= 0:
            continue
        if item.get('price_source') in ('Fragment', 'Portal · фон'):
            visible.append(item)
            continue
        # A legacy owned gift may lack the price-source field. Show it only when
        # its stored valuation matches a separately quoted catalog backdrop.
        if catalog_quotes is None:
            catalog_quotes = {str(g['id']): portal_price_string(g.get('price_ton'))
                              for g in read_catalog(include_hidden=True)['gifts'] if gift_black_background(g)}
        if catalog_quotes.get(str(item.get('gift_id') or item.get('id'))) == price:
            visible.append(item)
    return visible


@app.after_request
def compress_response(response):
    response.vary.add('Accept-Encoding')
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    try:
        if (response.direct_passthrough or response.status_code < 200 or response.status_code >= 300
                or 'Content-Encoding' in response.headers
                or request.accept_encodings['gzip'] <= 0
                or not (response.mimetype.startswith('text/') or response.mimetype in ('application/json', 'application/javascript', 'image/svg+xml'))):
            return response
        data = response.get_data()
        if len(data) < 700:
            return response
        import gzip
        compressed = gzip.compress(data, 1)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(response.get_data()))
        response.vary.add('Accept-Encoding')
    except Exception:
        pass
    return response


@app.before_request
def enforce_available_modes():
    path = request.path
    if request.is_json and request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not (
            request.method == 'DELETE' and not request.get_data(cache=True)):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error('Ожидается JSON-объект с параметрами запроса.', 400)
    # Craft is retired from the product. Keep its old data/code for safe migration,
    # but make the API inaccessible so stale clients cannot start new crafts.
    if path.startswith('/api/craft/'):
        return error('Крафты отключены.', 404)
    mode = ('giveaways' if path == '/api/giveaways' or path.startswith('/api/giveaways/') else
            'upgrade' if path.startswith('/api/upgrade/') else
            'mines' if path.startswith('/api/game/') else None)
    if mode and path not in ('/api/game/open', '/api/game/cashout') and not section_settings().get(mode, False):
        return error('Данный режим временно недоступен.', 403)


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
    labels = {'gift_sale':'Подарок продан', 'admin_balance':'Баланс изменён',
              'deposit':'Пополнение', 'ton_deposit':'Пополнение TON', 'referral_bonus':'Реферальный бонус',
              'deposit_promo_bonus':'Бонус пополнения', 'withdrawal_request':'Заявка на вывод',
              'withdrawal_approved':'Вывод выполнен', 'withdrawal_rejected':'Подарок возвращён',
              'game_win_ton':'Выигрыш Mines', 'gift_win':'Выигран подарок',
              'promo_wager_claim':'Подарок отыгран', 'upgrade_cashback':'Компенсация апгрейда',
              'upgrade_compensation_gift':'Компенсационный подарок'}
    if kind in labels:
        if kind in ('deposit', 'ton_deposit'):
            text = deposit_notification_text(amount, balance_after or 0)
        elif kind == 'admin_balance':
            text = (f'💳 Баланс изменён на {int(amount)/100:+.2f} TON.\n\n'
                    f'Текущий баланс: {int(balance_after or 0)/100:.2f} TON')
        else:
            icons = {'gift_sale':'✅', 'referral_bonus':'🎁', 'deposit_promo_bonus':'🎁',
                     'withdrawal_request':'⏳', 'withdrawal_approved':'✅', 'withdrawal_rejected':'↩️',
                     'game_win_ton':'🏆', 'gift_win':'🎁', 'promo_wager_claim':'✅',
                     'upgrade_cashback':'🎁', 'upgrade_compensation_gift':'🎁'}
            text = icons.get(kind, '🔔') + ' ' + labels[kind]
            if details and kind in ('gift_sale','gift_win','withdrawal_request','withdrawal_approved',
                                    'withdrawal_rejected','promo_wager_claim','upgrade_compensation_gift'):
                text += '\n' + str(details)[:200]
            if amount: text += f'\nСумма: {int(amount)/100:+.2f} TON'
            if amount and balance_after is not None:
                text += f'\n\nТекущий баланс: {int(balance_after)/100:.2f} TON'
        add_user_notification(db, user_id, kind, text)


# Routine actions are not stored: rounds, upgrade_spins and transactions already hold them.
NOISY_EVENT_KINDS = ('login','mines_cell','mines_start','mines_cashout','craft_play','roll','deposit_created',
                     'upgrade','upgrade_wager_repaired','promo_gift_expired')
EVENT_RETENTION_DAYS = 30


def prune_old_logs():
    try:
        with connect() as db:
            marks = ','.join('?' for _ in NOISY_EVENT_KINDS)
            db.execute(f'DELETE FROM user_events WHERE kind IN ({marks})', NOISY_EVENT_KINDS)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
            db.execute('DELETE FROM user_events WHERE created_at < ?', (cutoff,))
            db.execute("DELETE FROM notification_outbox WHERE state IN ('sent','failed') AND created_at < ?",
                       (int(time.time()) - EVENT_RETENTION_DAYS * 86400,))
            db.execute("DELETE FROM user_notifications WHERE created_at < ? AND delivery_state IN ('sent','none','failed')", (cutoff,))
    except Exception:
        app.logger.exception('Log pruning failed')


def log_pruner_loop():
    time.sleep(20)
    while True:
        prune_old_logs()
        time.sleep(6 * 3600)


def log_event(db,user_id,kind,**details):
    if kind not in NOISY_EVENT_KINDS:
        db.execute('INSERT INTO user_events(user_id,kind,payload) VALUES(?,?,?)',
                   (user_id,kind,json.dumps(details,ensure_ascii=False)))
    notification_details = dict(details)
    if kind in ('transfer_sent', 'transfer_received'):
        row = db.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()
        if row: notification_details['balance'] = int(row['balance']) / 100
        other_id = details.get('recipient_id') if kind == 'transfer_sent' else details.get('sender_id')
        other = db.execute('SELECT name,username FROM users WHERE id=?', (other_id,)).fetchone() if other_id else None
        if other: notification_details['person'] = '@'+other['username'] if other['username'] else other['name']
    text = activity_notification_text(kind, notification_details)
    if text: add_user_notification(db, user_id, kind, text)


IMPORTANT_NOTIFICATION_KINDS = (
    'deposit','ton_deposit','giveaway_started','giveaway_win','promo_issued',
    'withdrawal_request','withdrawal_approved','withdrawal_rejected','withdrawal_access',
    'transfer_received',
)
IMPORTANT_NOTIFICATION_SQL = "kind IN (" + ','.join('?' for _ in IMPORTANT_NOTIFICATION_KINDS) + ")"


NOTIFY_WAKE = __import__('threading').Event()


def add_user_notification(db, user_id, kind, text):
    if kind not in IMPORTANT_NOTIFICATION_KINDS: return
    NOTIFY_WAKE.set()
    # These actions already have a dedicated Telegram message.
    delivered_elsewhere = {'deposit','ton_deposit','promo_issued','giveaway_win',
                          'withdrawal_approved','withdrawal_rejected','withdrawal_access','admin_level'}
    state='pending' if BOT_TOKEN and kind not in delivered_elsewhere else 'none'
    db.execute('INSERT INTO user_notifications(user_id,kind,text,delivery_state) VALUES(?,?,?,?)',
               (user_id, kind, str(text)[:1000],state))


def activity_notification_text(kind, d):
    if kind == 'upgrade' and (not d.get('won') or d.get('promo_wager')): return None
    labels = {'upgrade':'Апгрейд', 'craft_play':'Крафт', 'transfer_sent':'Перевод отправлен',
              'transfer_received':'Перевод получен', 'level_claim':'Награда уровня',
              'reward_task_claim':'Задание выполнено', 'giveaway_enter':'Участие в розыгрыше',
              'giveaway_win':'Победа в розыгрыше', 'promo_issued':'Выдан промокод',
              'promo_redeem':'Промокод активирован', 'freebet_redeem':'Freebet активирован',
              'promo_gift_expired':'Срок подарка истёк', 'admin_level':'Уровень изменён',
              'withdrawal_access':'Доступ к выводу изменён', 'admin_gift_add':'Подарок добавлен',
              'admin_gift_remove':'Подарок удалён', 'deposit_created':'Заявка на пополнение'}
    if kind not in labels: return None
    icons = {'upgrade':'🎁','craft_play':'🎁','transfer_sent':'✅','transfer_received':'💳',
             'level_claim':'🎁','reward_task_claim':'🎟','giveaway_enter':'🎟','giveaway_win':'🏆',
             'promo_issued':'🎟','promo_gift_expired':'⌛','admin_level':'⬆️',
             'withdrawal_access':'🔔','admin_gift_add':'🎁','admin_gift_remove':'🔔'}
    text = labels[kind]
    if kind == 'upgrade':
        text += f': выигрыш\n{d.get("source_name", "TON")} → {d.get("target_name", "подарок")}\nШанс: {d.get("chance", 0):g}%'
    elif kind in ('transfer_sent','transfer_received'):
        text += f'\nСумма: {float(d.get("amount",0)):.2f} TON'
        if d.get('person'): text += '\n' + ('Получатель: ' if kind == 'transfer_sent' else 'Отправитель: ') + str(d['person'])
        if kind == 'transfer_sent' and d.get('fee'): text += f'\nКомиссия: {float(d["fee"]):.2f} TON'
        if 'balance' in d: text += f'\n\nТекущий баланс: {float(d["balance"]):.2f} TON'
    elif kind in ('giveaway_enter','reward_task_claim'): text += f'\nБилеты: {d.get("tickets",0)}'
    elif kind == 'level_claim': text += f'\nУровень: {d.get("level",1)}'
    elif kind == 'admin_level': text += f'\nУровень: {d.get("previous_level")} → {d.get("new_level")}'
    elif kind == 'craft_play': text += '\n'+str(d.get('reward_name') or 'Подарок')+f'\nЦена: {float(d.get("reward_price",0)):.2f} TON'
    elif kind == 'withdrawal_access':
        text = 'Вывод доступен' if d.get('enabled') else 'Вывод временно недоступен'
        if d.get('reason'): text += '\n'+str(d['reason'])
    elif d.get('code'): text += '\n'+str(d['code'])
    elif d.get('gift_name'): text += '\n'+str(d['gift_name'])
    if kind == 'giveaway_win': text = f'Победа в розыгрыше\n{d.get("giveaway_title") or ""}\n\n🎁 {d.get("gift_name", "Подарок")}\nМесто: {d.get("rank",1)}'
    return icons.get(kind, '🔔')+' '+text


@app.get('/api/notifications')
@login_required
def user_notifications():
    try: before = max(0,int(request.args.get('before',0)))
    except (ValueError,TypeError): return error('Некорректная страница.')
    with connect() as db:
        params=(session['uid'],*IMPORTANT_NOTIFICATION_KINDS)
        rows = db.execute('SELECT * FROM user_notifications WHERE user_id=? AND '+IMPORTANT_NOTIFICATION_SQL+(' AND id<?' if before else '')+' ORDER BY id DESC LIMIT 51',
                          (*params,before) if before else params).fetchall()
        unread = db.execute('SELECT COUNT(*) AS n FROM user_notifications WHERE user_id=? AND is_read=0 AND '+IMPORTANT_NOTIFICATION_SQL,params).fetchone()['n']
    items = [{k:r[k] for k in ('id','kind','text','created_at','is_read','giveaway_id')} for r in rows[:50]]
    return jsonify(items=items,unread=unread,has_more=len(rows)>50)


@app.post('/api/notifications/read')
@login_required
def read_user_notifications():
    try: upto = int((request.get_json(silent=True) or {}).get('upto',0))
    except (ValueError,TypeError): return error('Некорректная запись.')
    if upto<1: return error('Некорректная запись.')
    with connect() as db:
        db.execute('UPDATE user_notifications SET is_read=1 WHERE user_id=? AND id<=?',(session['uid'],upto))
    return jsonify(ok=True)


def read_catalog(include_hidden=False):
    stored = read_document('portal_catalog')
    if stored is not None:
        document = stored
    elif CATALOG.exists():
        document = json.loads(CATALOG.read_text(encoding='utf-8'))
    else:
        return {'gifts': [], 'updated_at': None}
    if not isinstance(document, dict) or not isinstance(document.get('gifts'), list):
        raise ValueError('Invalid catalog')
    # Older imports fabricated backdrop prices from collection floors. Keep those
    # out of all catalog consumers, without changing already owned inventory.
    gifts = [g for g in document['gifts'] if
        not gift_black_background(g) or
        (g.get('price_source') == 'Portal · фон' and portal_price_string(g.get('price_ton'))
         and Decimal(portal_price_string(g.get('price_ton'))) > 0)]
    return dict(document, gifts=gifts if include_hidden else visible_gifts(gifts))


def repair_legacy_upgrade_wagers():
    """Restore wager gifts incorrectly changed into upgrade targets by older releases."""
    try:catalog=read_catalog(include_hidden=True).get('gifts',[])
    except (OSError,ValueError,TypeError):catalog=[]
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if DATABASE_URL:
            db.execute('SELECT pg_advisory_xact_lock(660105)')
        if db.execute('SELECT 1 FROM schema_migrations WHERE name=?', ('repair_upgrade_wagers_build66',)).fetchone():
            return
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
        db.execute('INSERT OR IGNORE INTO schema_migrations(name) VALUES(?)', ('repair_upgrade_wagers_build66',))
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
    match_name = str(gift.get('base_name') or gift.get('name') or '')
    match = None
    for field in ('telegram_gift_id', 'star_gift_id', 'gift_id'):
        candidate = str(gift.get(field) or '')
        if candidate in mapping and collection_key(mapping[candidate]) == collection_key(match_name):
            match = candidate
            break
    if match is None:
        match = names.get(collection_key(match_name))
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
    raw_expires = row['expires_at'] if 'expires_at' in row.keys() else None
    expires = parse_datetime_utc(raw_expires) if raw_expires else None
    expires_in = max(0, int((expires - datetime.now(timezone.utc)).total_seconds())) if expires else None
    def optional(name, default=''):
        return row[name] if name in row.keys() and row[name] is not None else default
    return dict(id=row['id'], gift_id=row['gift_id'], name=row['gift_name'],
                image_url=row['image_url'], price_ton=row['floor_price']/100,
                source=row['source'], created_at=row['created_at'],
                external_url=optional('external_url'), fragment_url=optional('external_url'),
                fragment_number=optional('fragment_number'), fragment_model=optional('fragment_model'),
                fragment_backdrop=optional('fragment_backdrop'), fragment_symbol=optional('fragment_symbol'),
                price_source=optional('price_source'), animation_url=optional('animation_url'),
                promo_locked=locked, promo_code=row['promo_code'] or '',
                wager_multiplier=float(row['promo_wager_multiplier'] or 0),
                wager_target=target/100, wager_progress=progress/100,
                wager_complete=bool(locked and target > 0 and progress >= target),
                wager_percent=(min(100.0, progress * 100.0 / target) if target > 0 else 0.0),
                expires_at=expires.isoformat() if expires else None, expires_in_seconds=expires_in)


def promo_gift_expiry(days):
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days > 0 else None


def purge_expired_inventory(db, user_id=None):
    params = () if user_id is None else (int(user_id),)
    where = "expires_at IS NOT NULL AND expires_at<>''" + (" AND user_id=?" if user_id is not None else '')
    rows = db.execute(f'SELECT id,user_id,gift_name,expires_at FROM inventory WHERE {where}', params).fetchall()
    now = datetime.now(timezone.utc)
    expired = []
    for row in rows:
        expires = parse_datetime_utc(row['expires_at'])
        if expires and expires <= now:
            expired.append(row)
    for row in expired:
        db.execute('DELETE FROM inventory WHERE id=?', (row['id'],))
        record_transaction(db, row['user_id'], 'promo_gift_expired', 0, 'inventory', row['id'],
                           f'Истёк срок отыгрышного подарка: {row["gift_name"]}')
        log_event(db, row['user_id'], 'promo_gift_expired', inventory_id=row['id'], gift_name=row['gift_name'])
    return len(expired)

def award_round(db, row, opened_count):
    """Settle once, atomically, including promo-wager gift bets."""
    rtp = round_rtp(row)
    factor = multiplier_for(row['mines'], opened_count, rtp)
    amount = payout_for(row, opened_count, rtp)
    settled_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')

    if row['bet_type'] == 'promo_gift':
        target = max(0, int(row['promo_wager_target'] or 0))
        previous = max(0, int(row['promo_wager_progress'] or 0))
        progress = min(target, previous + amount) if target else previous + amount
        completed = bool(target and progress >= target)
        cursor = db.execute("""INSERT INTO inventory(
                                user_id,gift_id,gift_name,image_url,floor_price,source,round_id,
                                promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                              VALUES(?,?,?,?,?,'promo_wager',?,?,?,?,?,?,?)""",
                            (row['user_id'], row['bet_gift_id'], row['bet_gift_name'], row['bet_gift_image'],
                             row['bet_gift_price'], row['id'], 0 if completed else 1,
                             0.0 if completed else float(row['promo_wager_multiplier'] or 0),
                             0 if completed else target, 0 if completed else progress,
                             '' if completed else (row['promo_code'] or ''), None if completed else row['bet_expires_at']))
        db.execute("""UPDATE rounds SET state='won',payout=0,prize_inventory_id=?,win_total=?,win_multiplier=?,
                      promo_progress_after=?,win_gift_name='',win_gift_image='',win_gift_price=NULL,
                      settled_at=? WHERE id=?""",
                   (cursor.lastrowid, amount, factor, progress, settled_at, row['id']))
        record_transaction(db, row['user_id'], 'promo_wager_progress', amount, 'round', row['id'],
                           f'Отыгрыш {row["bet_gift_name"]}: {progress/100:.2f}/{target/100:.2f} TON')
        if completed:
            record_transaction(db, row['user_id'], 'promo_wager_claim', 0, 'inventory', cursor.lastrowid,
                               f'Подарок успешно отыгран: {row["bet_gift_name"]}')
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
                          win_gift_name=?,win_gift_image=?,win_gift_price=?,settled_at=?
                      WHERE id=?""",
                   (remainder, cursor.lastrowid, amount, factor, str(prize['name'])[:140],
                    image_url, cents, settled_at, row['id']))
        if remainder:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (remainder, row['user_id']))
            record_transaction(db, row['user_id'], 'game_win_ton', remainder, 'round', row['id'],
                               f'Остаток после выигрыша подарка: {prize["name"]}')
        record_transaction(db, row['user_id'], 'gift_win', 0, 'round', row['id'], str(prize['name']))
    else:
        db.execute("""UPDATE rounds SET state='won',payout=?,win_total=?,win_multiplier=?,
                      win_gift_name='',win_gift_image='',win_gift_price=NULL,settled_at=? WHERE id=?""",
                   (amount, amount, factor, settled_at, row['id']))
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
                        wager_progress=int(row['promo_wager_progress'] or 0)/100,
                        expires_at=row['bet_expires_at'])
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
    if not value.is_finite() or value != value.to_integral_value() or abs(value) > 9223372036854775807:
        raise ValueError('Invalid money amount')
    return int(value)


@app.get('/')
def index():
    # index.html is plain HTML/CSS/JS and does not use Jinja syntax.
    # Serving it directly prevents CSS sequences such as '{#' from ever
    # being interpreted as Jinja comments.
    return send_file(BASE / 'templates' / 'index.html', mimetype='text/html')


@app.get('/health')
def health():
    return jsonify(status='ok', build=BUILD_ID)


@app.get('/ready')
def readiness():
    try:
        with connect() as db:
            db.execute('SELECT 1').fetchone()
    except Exception:
        app.logger.exception('Database readiness failed')
        return jsonify(status='unavailable'), 503
    return jsonify(status='ok', build=BUILD_ID)


@app.get('/api/build')
def build_info():
    template_path = BASE / 'templates' / 'index.html'
    try:
        template_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()[:12]
    except OSError:
        template_hash = 'unavailable'
    return jsonify(build=BUILD_ID, index_sha256=template_hash)


@app.get('/api/me')
@login_required
def me():
    with connect() as db:
        purge_expired_inventory(db, session['uid'])
        row = active_round(db, session['uid'])
        if expire_promo_round(db, row):
            row = None
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
    if kind not in ('none','balance','tickets','gift','wager_gift','personal_promo','deposit_promo','multi_promo','transfer_unlock'):
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
    if kind=='tickets':
        try: tickets=int(data.get('tickets') or data.get('amount') or 0)
        except (TypeError,ValueError): raise ValueError('Укажите количество билетов.')
        if not 1<=tickets<=1000000: raise ValueError('Количество билетов: от 1 до 1 000 000.')
        reward['tickets']=tickets
        return reward
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
                try:
                    gift_expires_days=int(data.get('gift_expires_days') or 0)
                except (TypeError,ValueError):
                    raise ValueError('Срок жизни отыгрышного подарка должен быть указан в днях.')
                if not 0<=gift_expires_days<=3650:
                    raise ValueError('Срок жизни подарка: от 0 до 3650 дней. 0 — без срока.')
                reward['gift_expires_days']=gift_expires_days
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


def public_level_reward(reward, include_hidden=True, black_enabled=None):
    if not include_hidden and black_enabled is None:
        black_enabled = black_backgrounds_enabled()
    if not include_hidden and not black_enabled:
        components = reward.get('components') or {}
        if gift_black_background(reward) or any(gift_black_background(part) for part in components.values() if isinstance(part, dict)):
            return {'type': 'none'}
    result={'type':'none',**reward}
    if isinstance(result.get('components'),dict):
        result['components']={k:public_level_reward(v, include_hidden, black_enabled) for k,v in result['components'].items()}
    for key in ('amount','gift_price','bonus_fixed','min_deposit'):
        if key in result:result[key+'_ton']=result.pop(key)/100
    return result


@app.get('/api/levels')
@login_required
def user_levels():
    black_enabled = black_backgrounds_enabled()
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
                                reward=public_level_reward(json.loads(r['reward_json']), include_hidden=False, black_enabled=black_enabled),
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
    data=request.get_json(silent=True) or {}
    try:
        threshold=parse_amount(data.get('required_turnover'))
        reward=normalize_level_reward(data.get('reward') or {'type':'none'})
    except (ValueError,InvalidOperation,TypeError) as exc:return error(str(exc))
    if threshold<0 or threshold>10000000000:return error('Слишком большой оборот.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT level FROM levels WHERE level=?',(level,)).fetchone()
        if not current:return error('Уровень не найден.',404)
        prev=db.execute('SELECT level,required_turnover FROM levels WHERE level<? ORDER BY level DESC LIMIT 1',(level,)).fetchone()
        nxt=db.execute('SELECT level,required_turnover FROM levels WHERE level>? ORDER BY level LIMIT 1',(level,)).fetchone()
        if not prev and threshold!=0:return error('Первый уровень начинается с нулевого оборота.')
        if (prev and threshold<=prev['required_turnover']) or (nxt and threshold>=nxt['required_turnover']):
            return error('Порог должен быть больше предыдущего и меньше следующего уровня.')
        db.execute('UPDATE levels SET required_turnover=?,reward_json=? WHERE level=?',
                   (threshold,json.dumps(reward,ensure_ascii=False),level))
        db.commit()
    finally:db.close()
    return jsonify(ok=True,level=level,reward=public_level_reward(reward))


@app.post('/api/admin/levels')
@admin_required
def admin_add_level():
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute('SELECT level,required_turnover FROM levels ORDER BY level').fetchall()
        if len(rows)>=100:return error('Можно создать не более 100 уровней.')
        if not rows:
            level,threshold=1,0
        else:
            level=int(rows[-1]['level'])+1
            if len(rows)>=2:
                step=max(100,int(rows[-1]['required_turnover'])-int(rows[-2]['required_turnover']))
            else:
                step=max(100,int(rows[-1]['required_turnover']) or 100)
            threshold=int(rows[-1]['required_turnover'])+step
        db.execute('INSERT INTO levels(level,required_turnover,reward_json) VALUES(?,?,?)',(level,threshold,'{}'))
        db.execute('INSERT INTO transfer_rates(level,fee_percent,enabled) VALUES(?,5,1)',(level,))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'level_add',str(level)))
        db.commit()
        return jsonify(ok=True,level=dict(level=level,required_turnover=threshold/100,reward={'type':'none'}))
    finally:db.close()


@app.delete('/api/admin/levels/<int:level>')
@admin_required
def admin_delete_level(level):
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute('SELECT level FROM levels ORDER BY level').fetchall()
        if len(rows)<=1:return error('Нельзя удалить единственный уровень.')
        if level==1:return error('Первый уровень удалить нельзя.')
        if not any(int(row['level'])==level for row in rows):return error('Уровень не найден.',404)
        db.execute('DELETE FROM level_claims WHERE level=?',(level,))
        db.execute('DELETE FROM transfer_rates WHERE level=?',(level,))
        db.execute('DELETE FROM levels WHERE level=?',(level,))
        higher=[int(row['level']) for row in rows if int(row['level'])>level]
        for old_level in higher:
            new_level=old_level-1
            db.execute('UPDATE levels SET level=? WHERE level=?',(new_level,old_level))
            db.execute('UPDATE level_claims SET level=? WHERE level=?',(new_level,old_level))
            db.execute('UPDATE transfer_rates SET level=? WHERE level=?',(new_level,old_level))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'level_delete',str(level)))
        db.commit()
        return jsonify(ok=True,deleted=level,count=len(rows)-1)
    finally:db.close()


@app.post('/api/admin/levels/bulk')
@admin_required
def admin_save_levels_bulk():
    data=request.get_json(silent=True) or {}
    items=data.get('levels')
    if not isinstance(items,list) or not 1<=len(items)<=100:return error('Передайте от 1 до 100 уровней.')
    prepared=[]
    try:
        for index,item in enumerate(items,1):
            if not isinstance(item,dict) or int(item.get('level',0))!=index:
                return error('Уровни должны идти подряд, начиная с 1.')
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
        existing=db.execute('SELECT COUNT(*) AS total FROM levels').fetchone()['total']
        if int(existing)!=len(prepared):
            return error('Список уровней изменился. Обновите раздел и повторите сохранение.',409)
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
    gift_expires_days = int(reward.get('gift_expires_days') or 0) if promo_type == 'wager_gift' else 0
    db.execute('''INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,assigned_user_id,source_label,description,expires_at,gift_expires_days)
                  VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)''',
               (code, promo_type, reward.get('amount', 0), reward.get('gift_id', ''), reward.get('gift_name', ''),
                reward.get('image_url', ''), reward.get('gift_price', 0), reward.get('wager_multiplier', 0), 0,
                deposit.get('bonus_percent', 0), deposit.get('bonus_fixed', 0), deposit.get('min_deposit', 0),
                json.dumps(reward, ensure_ascii=False) if kind == 'multi_promo' else '{}', user_id,
                f'Награда за уровень {level}', description, expires_at, gift_expires_days))
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
        elif kind=='tickets':
            tickets=int(reward.get('tickets') or 0)
            if tickets<1:return error('Награда уровня настроена неверно.',500)
            db.execute('UPDATE users SET tickets=tickets+? WHERE id=?',(tickets,session['uid']))
            db.execute('INSERT INTO ticket_ledger(user_id,amount,kind,reference_type,reference_id,details) VALUES(?,?,?,?,?,?)',
                       (session['uid'],tickets,'level','level',str(level),f'Награда за уровень {level}'))
            result=dict(type='tickets',tickets=tickets)
        elif kind in ('gift','wager_gift'):
            item=(session['uid'],reward['gift_id'],reward['gift_name'],reward['image_url'],reward['gift_price'])
            if kind=='gift':
                cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'level')",item)
            else:
                mult=reward['wager_multiplier'];target=round(reward['gift_price']*mult)
                item_expires_at=promo_gift_expiry(reward.get('gift_expires_days'))
                cur=db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,expires_at)
                                  VALUES(?,?,?,?,?,'level_wager',1,?,?,0,?)""",item+(mult,target,item_expires_at))
            result=dict(type=kind,gift=dict(id=cur.lastrowid,name=reward['gift_name'],image_url=reward['image_url'],
                        price_ton=reward['gift_price']/100,promo_locked=kind=='wager_gift',wager_multiplier=reward.get('wager_multiplier',0),
                        expires_at=item_expires_at if kind=='wager_gift' else None))
        elif kind in LEVEL_PROMO_TYPES:
            result=create_level_promo(db,session['uid'],level,reward)
        elif kind=='transfer_unlock':
            result=dict(type='transfer_unlock',description='Переводы TON разблокированы')
        db.execute('INSERT INTO level_claims(user_id,level,reward_json) VALUES(?,?,?)',
                   (session['uid'],level,json.dumps(result,ensure_ascii=False)))
        log_event(db,session['uid'],'level_claim',level=level,reward=result)
        db.commit()
        if result.get('code'):
            notify_promo_async(session['uid'], result['code'], 'levels')
        return jsonify(ok=True,reward=result,user=profile())
    finally:db.close()


def reward_description(reward):
    if reward.get('type')=='none':return 'Без награды'
    if reward.get('type')=='transfer_unlock':return 'Доступ к переводам TON'
    if reward.get('type')=='tickets':return f"{int(reward.get('tickets') or 0)} билет(ов) для розыгрышей"
    if reward.get('type')=='multi_promo':return 'Мультипромокод · '+', '.join(reward.get('components',{}))
    if reward.get('type')=='balance' or reward.get('promo_reward_type')=='balance' and reward.get('type')=='personal_promo':
        return f"{reward.get('amount',0)/100:.2f} TON"
    if reward.get('type')=='wager_gift' or reward.get('promo_reward_type')=='wager_gift':
        days=int(reward.get('gift_expires_days') or 0)
        suffix=f' · сгорит через {days} дн.' if days else ''
        return f"{reward.get('gift_name','Подарок')} · X{float(reward.get('wager_multiplier') or 0):g}{suffix}"
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
        if new_level:
            notify_level_up_async(session['uid'], new_level)
        return jsonify(spin_id=spin_id,entry_id=entry['id'],index=index,kind=entry['kind'],
                       name=entry['name'],image_url=entry.get('image_url',''),boost=new_boost,
                       applied_boost=boost,new_level=new_level,user=profile())
    finally:
        db.close()


def game_net_loss_cents(db, user_id):
    """Approximate the player's realized gaming deficit in cents.

    Promo-wager rounds are excluded because they do not spend the player's own
    balance/gift value. Cash compensation already credited to balance reduces
    the deficit so the adaptive promo boost follows the *current* loss, not the
    gross amount ever wagered.
    """
    user_id = int(user_id)
    loss = 0
    rounds = db.execute("""SELECT bet,bet_type,bet_gift_price,state,win_total,payout
                           FROM rounds WHERE user_id=? AND state IN ('won','lost')""", (user_id,)).fetchall()
    for row in rounds:
        if row['bet_type'] == 'promo_gift':
            continue
        stake = int(row['bet_gift_price'] or row['bet'] or 0) if row['bet_type'] == 'gift' else int(row['bet'] or 0)
        returned = int((row['win_total'] if row['win_total'] is not None else row['payout']) or 0) if row['state'] == 'won' else 0
        loss += stake - returned

    spins = db.execute("""SELECT source_price,target_price,won,result_json FROM upgrade_spins
                          WHERE user_id=?""", (user_id,)).fetchall()
    for row in spins:
        try:
            result = json.loads(row['result_json'] or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {}
        if result.get('reward_type') == 'wager_progress':
            continue
        stake = int(row['source_price'] or 0)
        returned = int(row['target_price'] or 0) if row['won'] else 0
        loss += stake - returned

    # Roll outcomes are valued from the current Roll configuration. This is an
    # approximation for old spins if an admin later changes the configuration.
    try:
        rolls = {str(r['id']): r for r in roll_config(db)}
        for row in db.execute('SELECT roll_id,price,outcome,gift_name FROM roll_spins WHERE user_id=?', (user_id,)).fetchall():
            returned = 0
            if row['outcome'] == 'gift':
                roll = rolls.get(str(row['roll_id'])) or {}
                match = next((e for e in roll.get('entries', []) if e.get('kind') == 'gift' and e.get('name') == row['gift_name']), None)
                returned = int((match or {}).get('price') or 0)
            loss += int(row['price'] or 0) - returned
    except Exception:
        app.logger.debug('Could not include Roll in game deficit', exc_info=True)

    cashback = db.execute("""SELECT COALESCE(SUM(amount),0) AS total FROM transactions
                             WHERE user_id=? AND kind='upgrade_cashback'""", (user_id,)).fetchone()
    loss -= int((cashback or {}).get('total') or 0)
    return max(0, int(loss))


def loss_rtp_max_boost():
    try:
        value = float((read_document('game_settings') or {}).get('loss_rtp_max_boost', 8.0))
    except (TypeError, ValueError):
        value = 8.0
    return max(0.0, min(15.0, value))


def loss_rtp_boost_points(loss_cents):
    """Extra RTP percentage points for compensation wagering gifts.

    The base curve reaches 8 pp at a 500 TON deficit. Admin can scale the
    maximum between 0 and 15 pp without changing the loss thresholds.
    """
    ton = max(0.0, float(loss_cents or 0) / 100.0)
    if ton < 1:
        base = 0.0
    elif ton < 5:
        base = 0.5 + (ton - 1) * 1.5 / 4
    elif ton < 25:
        base = 2.0 + (ton - 5) * 2.0 / 20
    elif ton < 100:
        base = 4.0 + (ton - 25) * 2.0 / 75
    elif ton < 500:
        base = 6.0 + (ton - 100) * 2.0 / 400
    else:
        base = 8.0
    return round(base * loss_rtp_max_boost() / 8.0, 4)


def compensation_promo_info(db, code):
    code = str(code or '').strip()
    if not code:
        return None
    row = db.execute("""SELECT code,source_label,reward_type,reward_json FROM promo_codes
                        WHERE code=?""", (code,)).fetchone()
    if not row or row['reward_type'] != 'wager_gift' or row['source_label'] != 'Компенсация Upgrade':
        return None
    return row


def promo_loss_adjusted_rtp(db, user_id, promo_code):
    base = promo_game_rtp()
    if not compensation_promo_info(db, promo_code):
        return base, 0.0, game_net_loss_cents(db, user_id)
    loss = game_net_loss_cents(db, user_id)
    boost = loss_rtp_boost_points(loss)
    return min(0.995, base + boost / 100.0), boost, loss


def promo_loss_adjusted_upgrade_rtp_bp(db, user_id, promo_code):
    base = upgrade_rtp_basis_points()
    if not compensation_promo_info(db, promo_code):
        return base, 0.0, game_net_loss_cents(db, user_id)
    loss = game_net_loss_cents(db, user_id)
    boost = loss_rtp_boost_points(loss)
    return min(10000, base + round(boost * 100)), boost, loss


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


def upgrade_chance(source_price,target_price,rtp_bp=None):
    if source_price < 1 or target_price <= source_price or target_price > source_price * 10:
        return 0
    rtp_bp = upgrade_rtp_basis_points() if rtp_bp is None else max(1, min(10000, int(rtp_bp)))
    chance_bp = (rtp_bp * source_price) / target_price
    # Upgrade targets are intentionally limited to the visible 1–80% range.
    # Anything outside it is not a valid target at all, not merely hidden in UI.
    if chance_bp < 100 or chance_bp > 8000:
        return 0
    return chance_bp


def upgrade_rtp_basis_points():
    try:return int((read_document('game_settings') or {}).get('upgrade_rtp_bp',9000))
    except (TypeError,ValueError):return 9000


@app.get('/api/upgrade/settings')
@login_required
def upgrade_settings():
    return jsonify(rtp=upgrade_rtp_basis_points()/100,min_chance=1,max_chance=80,max_target_multiplier=10,
                   min_bet_ton=0.1,max_bet_ton=MAX_UPGRADE_BET_CENTS/100)


@app.get('/api/upgrade/preview')
@login_required
def upgrade_preview():
    amount_text=request.args.get('amount')
    item_text=request.args.get('inventory_id')
    if bool(amount_text)==bool(item_text):return error('Выберите TON или подарок для ставки.')
    if amount_text:
        try:source_price=parse_amount(amount_text)
        except (ValueError,InvalidOperation,TypeError):return error('Укажите ставку в TON с точностью до 0.01.')
        if not 10<=source_price<=MAX_UPGRADE_BET_CENTS:return error('Ставка TON: от 0.10 до 1 000.')
        with connect() as db:
            balance=db.execute('SELECT balance FROM users WHERE id=?',(session['uid'],)).fetchone()['balance']
        if balance<source_price:return error('Недостаточно TON для ставки.')
        source_view=dict(type='ton',id=None,name='TON',image_url='/static/img/ton.png',price_ton=source_price/100)
    else:
        try:source_id=int(item_text)
        except (ValueError,TypeError):return error('Выберите свой подарок.')
        with connect() as db:
            purge_expired_inventory(db, session['uid'])
            source=db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?',(source_id,session['uid'])).fetchone()
        if not source:return error('Выберите доступный подарок из инвентаря.')
        if source['promo_locked'] and int(source['promo_wager_progress'] or 0)>=int(source['promo_wager_target'] or 0):
            return error('Отыгрыш завершён — сначала получите подарок.')
        source_price=int(source['floor_price'] or 0)
        if source_price>MAX_UPGRADE_BET_CENTS:return error('Максимальная стоимость ставки — 1 000 TON.')
        source_view=dict(type='gift',**inventory_item(source))
    target=upgrade_target(request.args.get('gift_id'))
    if not target:return error('Целевой подарок не найден в каталоге Portal.')
    effective_rtp_bp = upgrade_rtp_basis_points()
    loss_boost = 0.0
    game_loss = 0
    if not amount_text and source and source['promo_locked']:
        with connect() as db:
            effective_rtp_bp, loss_boost, game_loss = promo_loss_adjusted_upgrade_rtp_bp(db, session['uid'], source['promo_code'])
    chance=upgrade_chance(source_price,target['price'],effective_rtp_bp)
    if not chance:return error('Выберите цель с шансом от 1% до 80% и ценой не выше ×10 ставки.')
    return jsonify(source=source_view,target=dict(id=target['id'],name=target['name'],
                   image_url=target['image_url'],price_ton=target['price']/100),chance=chance/100,
                   probability=chance/10000,rtp=effective_rtp_bp/100,
                   loss_rtp_boost=round(loss_boost,2),game_loss_ton=round(game_loss/100,2))


def wins_day_start_utc():
    return datetime.now(timezone(timedelta(hours=3))).replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def wins_feed_cutoff(db, kind):
    row = db.execute('SELECT cleared_at,max_round_id FROM wins_feed_clears WHERE kind=?', (kind,)).fetchone()
    return (row['cleared_at'], int(row['max_round_id'] or 0)) if row else ('', 0)


@app.get('/api/upgrade/recent-wins')
@login_required
def upgrade_recent_wins():
    with connect() as db:
        cutoff, _ = wins_feed_cutoff(db, 'upgrade')
        rows = db.execute('''SELECT s.id,s.user_id,s.source_name,s.source_image,s.source_price,
                                   s.target_name,s.target_image,s.target_price,s.chance_bp,
                                   s.result_json,s.created_at,u.name,u.username,u.photo_url
                            FROM upgrade_spins s JOIN users u ON u.id=s.user_id
                            WHERE s.won=1 AND s.created_at>?
                            ORDER BY s.created_at DESC,s.id DESC''', (cutoff,)).fetchall()
        day_start = wins_day_start_utc()
    def upgrade_win_item(row, result=None):
        if not row:return None
        if result is None:
            try:result = json.loads(row['result_json'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):result = {}
        if not isinstance(result,dict):result={}
        try:source_price=max(0,int(row['source_price'] or 0))/100
        except (TypeError,ValueError):source_price=0
        try:target_price=max(0,int(row['target_price'] or 0))/100
        except (TypeError,ValueError):target_price=0
        try:default_chance=float(row['chance_bp'] or 0)/100
        except (TypeError,ValueError):default_chance=0
        try:chance=float(result.get('chance',default_chance) or 0)
        except (TypeError,ValueError):chance=default_chance
        return dict(id=str(row['id']),user_id=int(row['user_id']),name=str(row['name'] or 'Игрок'),username=str(row['username'] or ''),
                    photo_url=str(row['photo_url'] or ''),source_type=result.get('source_type') or
                    ('ton' if row['source_name']=='TON' else 'gift'),
                    source=dict(name=str(row['source_name'] or 'Ставка'),image_url=str(row['source_image'] or ''),
                                price_ton=source_price),
                    target=dict(name=str(row['target_name'] or 'Подарок'),image_url=str(row['target_image'] or ''),
                                price_ton=target_price),
                    chance=max(0,min(100,chance)),
                    reward_type=str(result.get('reward_type') or 'gift'),
                    created_at=row['created_at'])
    items=[]
    for row in rows:
        try:
            item=upgrade_win_item(row)
            if item:items.append(item)
        except Exception:
            app.logger.warning('Skipping malformed upgrade win row %s', row['id'] if row else '?', exc_info=True)
    if not black_backgrounds_enabled():
        items = [item for item in items if not any(gift_black_background(item[key]) for key in ('source', 'target'))]
    top_candidates=[item for item in items
                    if item.get('reward_type')!='wager_progress' and str(item.get('created_at') or '')>=day_start]
    top_drop=max(top_candidates, key=lambda item:(float(item['target'].get('price_ton') or 0),
                                                       str(item.get('created_at') or '')), default=None)
    return jsonify(items=items,top_drop=top_drop)


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
        if not 10<=ton_price<=MAX_UPGRADE_BET_CENTS:return error('Ставка TON: от 0.10 до 1 000.')
    else:
        try:source_id=int(item_text)
        except (ValueError,TypeError):return error('Выберите свой подарок.')
    target=upgrade_target(data.get('gift_id'))
    if not target:return error('Целевой подарок не найден в каталоге Portal.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        purge_expired_inventory(db, session['uid'])
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
            if source_price>MAX_UPGRADE_BET_CENTS:return error('Максимальная стоимость ставки — 1 000 TON.')
            if source['promo_locked'] and int(source['promo_wager_progress'] or 0)>=int(source['promo_wager_target'] or 0):
                return error('Отыгрыш завершён — сначала получите подарок.')
        effective_rtp_bp = upgrade_rtp_basis_points()
        if not amount_text and source['promo_locked']:
            effective_rtp_bp, _, _ = promo_loss_adjusted_upgrade_rtp_bp(db, session['uid'], source['promo_code'])
        chance=upgrade_chance(source_price,target['price'],effective_rtp_bp)
        if not chance:return error('Выберите цель с шансом от 1% до 80% и ценой не выше ×10 ставки.')
        if amount_text:
            if not db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?',
                              (source_price,session['uid'],source_price)).rowcount:
                return error('Недостаточно TON для ставки.',409)
        elif not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?',(source_id,session['uid'])).rowcount:
            return error('Подарок уже использован.',409)
        # Exact integer ratio permits rare wins without rounding the chance up
        # to 0.01% (or down to an impossible 0%).
        won=secrets.randbelow(target['price']*10000)<effective_rtp_bp*source_price
        awarded=None
        wager=bool(source['promo_locked'])
        wager_target=int(source['promo_wager_target'] or 0) if wager else 0
        wager_progress=min(wager_target,int(source['promo_wager_progress'] or 0)+target['price']) if wager and won else 0
        compensation=dict(cashback=0,cashback_percent=0,promo=None)
        if won:
            if wager:
                cur=db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                 promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                                 VALUES(?,?,?,?,?,'upgrade_wager',1,?,?,?,?,?)''',
                               (session['uid'],source['gift_id'],source['gift_name'],source['image_url'],source_price,
                                float(source['promo_wager_multiplier'] or 0),wager_target,wager_progress,source['promo_code'] or '',source['expires_at']))
            else:
                cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'upgrade')",
                               (session['uid'],target['id'],target['name'],target['image_url'],target['price']))
            awarded=cur.lastrowid
        elif not wager:
            compensation=apply_upgrade_loss_compensation(db,session['uid'],source_price,target['price'])
        result=dict(ok=True,id=request_id,won=won,chance=chance/100,
                    source_type='ton' if amount_text else 'gift',reward_type='wager_progress' if wager else 'gift',
                    source=dict(name=source['gift_name'],image_url=source['image_url'],price_ton=source_price/100),
                    target=dict(name=target['name'],image_url=target['image_url'],price_ton=target['price']/100,
                                promo_locked=False),
                    wager_progress=wager_progress/100,wager_target=wager_target/100,
                    expires_at=source['expires_at'] if wager else None,
                    awarded_inventory_id=awarded,compensation=compensation)
        db.execute('''INSERT INTO upgrade_spins(id,user_id,source_name,source_image,source_price,target_name,target_image,target_price,chance_bp,won,result_json,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (request_id,session['uid'],source['gift_name'],source['image_url'],source_price,
                    target['name'],target['image_url'],target['price'],round(chance),int(won),json.dumps(result,ensure_ascii=False),
                    datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')))
        record_transaction(db,session['uid'],'upgrade_bet',-source_price if amount_text else 0,'upgrade',request_id,
                           f'{source["gift_name"]} → {target["name"]} · {chance/100:.2f}% · {"успех" if won else "проигрыш"}')
        log_event(db,session['uid'],'upgrade',source_name=source['gift_name'],source_image=source['image_url'],
                  source_price=source_price/100,target_name=target['name'],target_image=target['image_url'],
                  target_price=target['price']/100,chance=chance/100,won=won,promo_wager=wager,
                  wager_progress=wager_progress/100 if wager and won else None,source_type='ton' if amount_text else 'gift')
        # Upgrade always advances level turnover by the stake value, for TON and gift bets.
        result['new_level']=increase_turnover(db,session['uid'],source_price)
        db.execute('UPDATE upgrade_spins SET result_json=? WHERE id=?',(json.dumps(result,ensure_ascii=False),request_id))
        db.commit()
        promo_code = ((result.get('compensation') or {}).get('promo') or {}).get('code')
        if promo_code:
            notify_promo_async(session['uid'], promo_code, 'bonuses')
        if result['new_level']:
            notify_level_up_async(session['uid'], result['new_level'])
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
    data=request.get_json(silent=True) or {}
    try:fee=float(data.get('fee_percent'))
    except (TypeError,ValueError):return error('Введите комиссию.')
    if not math.isfinite(fee) or not 0<=fee<=30:return error('Комиссия: от 0 до 30%.')
    enabled=int(bool(data.get('enabled',True)))
    with connect() as db:
        if not db.execute('SELECT 1 FROM levels WHERE level=?',(level,)).fetchone():
            return error('Неверный уровень.',404)
        db.execute('INSERT INTO transfer_rates(level,fee_percent,enabled) VALUES(?,?,?) ON CONFLICT(level) DO UPDATE SET fee_percent=excluded.fee_percent,enabled=excluded.enabled',
                   (level,fee,enabled))
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
        purge_expired_inventory(db, session['uid'])
        existing_round = active_round(db, session['uid'])
        if existing_round and expire_promo_round(db, existing_round):
            existing_round = None
        if existing_round:
            return error('Сначала завершите текущую игру.')

        bet_type = 'ton'
        snapshot = dict(item_id=None, gift_id='', name='', image='', price=0,
                        multiplier=0.0, target=0, progress=0, code='', expires_at=None)
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
                            target=target, progress=progress, code=item['promo_code'] or '',
                            expires_at=item['expires_at'])
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
        if bet_type == 'promo_gift':
            rtp_snapshot, promo_loss_boost, promo_game_loss = promo_loss_adjusted_rtp(db, session['uid'], snapshot['code'])
        else:
            rtp_snapshot, promo_loss_boost, promo_game_loss = game_rtp(), 0.0, 0
        db.execute("""INSERT INTO rounds(user_id,bet,mines,positions,bet_type,bet_inventory_id,
                       bet_gift_id,bet_gift_name,bet_gift_image,bet_gift_price,promo_wager_multiplier,
                       promo_wager_target,promo_wager_progress,promo_code,rtp_snapshot,bet_expires_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (session['uid'], bet, mines, json.dumps(positions), bet_type, snapshot['item_id'],
                    snapshot['gift_id'], snapshot['name'], snapshot['image'], snapshot['price'], snapshot['multiplier'],
                    snapshot['target'], snapshot['progress'], snapshot['code'], rtp_snapshot, snapshot['expires_at']))
        row = active_round(db, session['uid'])
        if bet_type == 'ton':
            record_transaction(db, session['uid'], 'game_bet', -bet, 'round', row['id'], f'Mines: {mines}')
        elif bet_type == 'promo_gift':
            record_transaction(db, session['uid'], 'promo_wager_bet', 0, 'round', row['id'],
                               f'{snapshot["name"]} · X{snapshot["multiplier"]:g}')
        else:
            record_transaction(db, session['uid'], 'gift_bet', 0, 'round', row['id'], snapshot['name'])
        log_event(db,session['uid'],'mines_start',round_id=row['id'],mines=mines,bet=bet/100,
                  bet_type=bet_type,gift_name=snapshot['name'],gift_image=snapshot['image'],
                  promo_rtp=round(rtp_snapshot*100,2) if bet_type=='promo_gift' else None,
                  loss_rtp_boost=round(promo_loss_boost,2) if bet_type=='promo_gift' else None,
                  game_loss_ton=round(promo_game_loss/100,2) if bet_type=='promo_gift' else None)
        new_level=increase_turnover(db,session['uid'],bet)
        db.commit()
        if new_level:
            notify_level_up_async(session['uid'], new_level)
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
        if expire_promo_round(db, row):
            db.commit()
            return error('Срок отыгрышного подарка истёк. Подарок сгорел.', 409)
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

            # A Mines round must finish as soon as there is nothing left to play for.
            # 1) Normal rounds end after every safe cell has been opened.
            # 2) Promo-wager rounds end on the first safe step whose potential payout
            #    completes the remaining wager. This prevents the player from carrying
            #    on past the exact point where the promo gift is already earned.
            finish_round = len(opened) >= 25 - int(row['mines'])
            if row['bet_type'] == 'promo_gift':
                target = max(0, int(row['promo_wager_target'] or 0))
                previous = max(0, int(row['promo_wager_progress'] or 0))
                if target > 0:
                    current_amount = payout_for(row, len(opened), round_rtp(row))
                    if previous + current_amount >= target:
                        finish_round = True
            if finish_round:
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
        if row and expire_promo_round(db, row):
            db.commit()
            return error('Срок отыгрышного подарка истёк. Подарок сгорел.', 409)
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
        cutoff, max_round_id = wins_feed_cutoff(db, 'mines')
        selection = """SELECT r.id,r.bet,r.mines,r.opened,r.payout,r.win_total,r.win_multiplier,
                                    r.win_gift_name,r.win_gift_image,r.win_gift_price,r.created_at,
                                    u.id AS user_id,u.name,u.username,u.photo_url
                             FROM rounds r JOIN users u ON u.id=r.user_id
                             WHERE r.state='won' AND COALESCE(r.bet_type,'ton')<>'promo_gift'
                               AND (r.id>? OR r.settled_at>?)"""
        rows = db.execute(selection+' ORDER BY COALESCE(r.settled_at,r.created_at) DESC,r.id DESC',
                          (max_round_id,cutoff)).fetchall()
        top = db.execute(selection+''' AND COALESCE(r.settled_at,r.created_at)>=?
                           ORDER BY COALESCE(NULLIF(r.win_total,0),NULLIF(r.win_gift_price,0),r.payout) DESC,r.id DESC LIMIT 1''',
                         (max_round_id,cutoff,wins_day_start_utc())).fetchone()
    def mines_win_item(row):
        try:
            opened_count = len(json.loads(row['opened'] or '[]'))
        except (TypeError, ValueError):
            opened_count = 0
        try:
            factor = float(row['win_multiplier']) if row['win_multiplier'] is not None else multiplier_for(row['mines'], opened_count)
        except (TypeError, ValueError, ArithmeticError):
            factor = 1.01
        total = row['win_total'] if row['win_total'] is not None else row['payout']
        return dict(
            id=row['id'], user_id=row['user_id'], name=row['name'], username=row['username'],
            photo_url=row['photo_url'], bet=row['bet']/100, multiplier=round(max(1.01, factor), 6),
            amount=(total or 0)/100, gift=(dict(name=row['win_gift_name'], image_url=row['win_gift_image'],
                                               price_ton=(row['win_gift_price'] or 0)/100)
                                           if row['win_gift_name'] else None),
            created_at=row['created_at'])
    items=[]
    for row in rows:
        try:items.append(mines_win_item(row))
        except Exception:
            app.logger.warning('Skipping malformed mines win row %s', row['id'] if row else '?', exc_info=True)
    try:top_drop=mines_win_item(top) if top else None
    except Exception:
        app.logger.warning('Skipping malformed mines top-drop row', exc_info=True)
        top_drop=None
    if not black_backgrounds_enabled():
        items = [item for item in items if not gift_black_background(item.get('gift') or {})]
        if top_drop and gift_black_background(top_drop.get('gift') or {}):
            top_drop = max((item for item in items if str(item['created_at']) >= wins_day_start_utc()),
                           key=lambda item: item['amount'], default=None)
    return jsonify(items=items,top_drop=top_drop)


FRAGMENT_GIFT_RE = re.compile(r'^https?://(?:(?:www\.)?fragment\.com/gift/|t\.me/nft/)([a-z0-9-]+?)(?:[/?#].*)?$', re.I)
fragment_preview_cache = {}
fragment_preview_lock = __import__('threading').Lock()


def record_tickets(db, user_id, amount, kind, reference_type='', reference_id='', details=''):
    amount = int(amount)
    db.execute('INSERT INTO ticket_ledger(user_id,amount,kind,reference_type,reference_id,details) VALUES(?,?,?,?,?,?)',
               (int(user_id), amount, str(kind)[:80], str(reference_type)[:80], str(reference_id)[:120], str(details)[:300]))


def _fragment_meta_content(html_text, key):
    escaped = re.escape(key)
    patterns = (
        rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html_text or '', re.I)
        if match:
            return unescape(match.group(1)).strip()
    return ''


def _fragment_traits_from_text(text):
    traits = {'model': '', 'backdrop': '', 'symbol': ''}
    text = unescape(str(text or '')).replace('•', '\n')
    for key, label in (('model', 'Model'), ('backdrop', 'Backdrop'), ('symbol', 'Symbol')):
        match = re.search(rf'(?:^|[\n\r])\s*{label}\s*:\s*([^\n\r|]+)', text, re.I)
        if not match:
            match = re.search(rf'\b{label}\s*:\s*([^,;|]+)', text, re.I)
        if match:
            traits[key] = match.group(1).strip()[:100]
    return traits


def _fragment_traits_from_json(payload):
    traits = {'model': '', 'backdrop': '', 'symbol': ''}
    if not isinstance(payload, dict):
        return traits
    attrs = payload.get('attributes') or payload.get('traits') or []
    if isinstance(attrs, dict):
        attrs = [{'trait_type': k, 'value': v} for k, v in attrs.items()]
    if isinstance(attrs, list):
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            key = str(attr.get('trait_type') or attr.get('type') or attr.get('name') or attr.get('key') or '').casefold()
            value = str(attr.get('value') or attr.get('label') or attr.get('title') or '').strip()
            if not value:
                continue
            if 'model' in key:
                traits['model'] = value[:100]
            elif 'backdrop' in key or 'background' in key:
                traits['backdrop'] = value[:100]
            elif 'symbol' in key or 'pattern' in key:
                traits['symbol'] = value[:100]
    return traits


def _fragment_json_image(payload):
    if not isinstance(payload, dict):
        return ''
    candidates = [payload.get('image'), payload.get('image_url'), payload.get('imageUrl'), payload.get('preview')]
    for parent_key in ('media', 'pics', 'images', 'previews'):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            for key in ('large', 'medium', 'small', 'webp', 'jpg', 'url'):
                candidates.append(parent.get(key))
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get('url')
        if safe_image(candidate):
            return candidate
    return ''


def _fragment_json_animation(payload):
    if not isinstance(payload, dict):
        return ''
    values = [payload.get(key) for key in ('animation_url', 'animationUrl', 'video_url', 'videoUrl', 'video')]
    media = payload.get('media')
    if isinstance(media, dict):
        values += [media.get(key) for key in ('animation', 'video', 'mp4', 'webm')]
    for value in values:
        if isinstance(value, dict):
            value = value.get('url') or value.get('src')
        if safe_image(value):
            return value
    return ''


def _fragment_price_from_text(text):
    """Best-effort listed TON price; deliberately requires a price/listing keyword."""
    clean = re.sub(r'<[^>]+>', ' ', unescape(str(text or '')))
    clean = re.sub(r'\s+', ' ', clean)
    patterns = (
        r'(?i)\b(?:price|sale price|listed(?: for)?|buy now|floor(?: price)?|стоимость|цена)\b[^0-9]{0,50}([0-9][0-9\s.,]{0,24})\s*(?:TON|GRAM)\b',
        r'(?i)([0-9][0-9\s.,]{0,24})\s*(?:TON|GRAM)\b[^A-Za-zА-Яа-я]{0,20}\b(?:price|sale|listed|buy|floor|цена|стоимость)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, clean)
        if not match:
            continue
        raw = match.group(1).replace(' ', '')
        if raw.count(',') == 1 and '.' not in raw:
            raw = raw.replace(',', '.')
        else:
            raw = raw.replace(',', '')
        try:
            value = Decimal(raw)
            if value.is_finite() and Decimal('0.01') <= value <= Decimal('10000000'):
                return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            pass
    return 0


def _price_from_json(payload):
    if not isinstance(payload, (dict, list)):
        return 0
    price_keys = {'price', 'sale_price', 'salePrice', 'floor_price', 'floorPrice', 'list_price', 'listPrice'}
    queue = [payload]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for key, value in item.items():
                if key in price_keys and not isinstance(value, (dict, list)):
                    try:
                        amount = Decimal(str(value))
                        if amount.is_finite() and Decimal('0.01') <= amount <= Decimal('10000000'):
                            return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                    except (InvalidOperation, TypeError, ValueError):
                        pass
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(item, list):
            queue.extend(x for x in item if isinstance(x, (dict, list)))
    return 0


def _portal_filter_trait_floor(payload, model='', backdrop=''):
    """Find a trait/model floor in Portal's varying filter JSON shapes."""
    target_model = re.sub(r'\s+', ' ', str(model or '').strip()).casefold()
    target_backdrop = re.sub(r'\s+', ' ', str(backdrop or '').strip()).casefold()
    model_candidates, backdrop_candidates = [], []

    def price_cents(value):
        if isinstance(value, dict):
            for key in ('floor_price', 'floorPrice', 'min_price', 'minPrice', 'price', 'amount', 'floor', 'value'):
                if key in value:
                    found = price_cents(value.get(key))
                    if found:
                        return found
            return 0
        try:
            amount = Decimal(str(value))
            if amount.is_finite() and amount > 0:
                # Portal's collection importer treats these fields as TON; keep the same unit here.
                return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        except (InvalidOperation, TypeError, ValueError):
            return 0
        return 0

    def scan(obj, context=''):
        if isinstance(obj, list):
            for x in obj:
                scan(x, context)
            return
        if not isinstance(obj, dict):
            return
        trait = str(obj.get('trait_type') or obj.get('type') or obj.get('key') or obj.get('category') or context or '').casefold()
        value = str(obj.get('value') or obj.get('name') or obj.get('label') or obj.get('title') or '').strip()
        norm = re.sub(r'\s+', ' ', value).casefold()
        price = price_cents(obj)
        if target_model and norm == target_model and ('model' in trait or not trait):
            if price: model_candidates.append(price)
        if target_backdrop and norm == target_backdrop and any(k in trait for k in ('backdrop', 'background')):
            if price: backdrop_candidates.append(price)
        for key, child in obj.items():
            key_norm = re.sub(r'\s+', ' ', str(key)).casefold()
            if target_model and key_norm == target_model:
                p = price_cents(child)
                if p: model_candidates.append(p)
            if target_backdrop and key_norm == target_backdrop:
                p = price_cents(child)
                if p: backdrop_candidates.append(p)
            if isinstance(child, (dict, list)):
                scan(child, key)
    scan(payload)
    if normalize_portal_background(backdrop):
        quote = portal_background_variants({}, payload).get(normalize_portal_background(backdrop))
        return (ton_to_cents(quote), 'Portal · фон') if quote else (0, '')
    if model_candidates:
        return min(model_candidates), 'Portal · модель'
    if backdrop_candidates:
        return min(backdrop_candidates), 'Portal · фон'
    return 0, ''


def _fragment_portal_fallback_price(collection_name, model='', backdrop=''):
    """Use Portal trait floor when possible, then collection floor from the cached catalog/public API."""
    short = portal_short_name(collection_name)
    key = saved_portal_key()
    if short:
        try:
            filters = portal_get_collection_filters(requests, key, [collection_name]).get(short)
            price, source = _portal_filter_trait_floor(filters, model, backdrop)
            if price:
                return price, source
        except (requests.RequestException, ValueError, TypeError):
            pass
    try:
        gifts = read_catalog(include_hidden=True).get('gifts', [])
    except (OSError, ValueError, TypeError):
        gifts = []
    norm = re.sub(r'[^a-z0-9]+', '', str(collection_name).casefold())
    candidates = []
    for gift in gifts:
        base = str(gift.get('base_name') or gift.get('name') or '')
        base = re.sub(r'\s*\((?:Onyx Black|Black)\)\s*$', '', base, flags=re.I)
        if re.sub(r'[^a-z0-9]+', '', base.casefold()) != norm:
            continue
        try:
            cents = ton_to_cents(gift.get('price_ton') or 0)
        except (ValueError, TypeError, InvalidOperation):
            cents = 0
        if cents > 0:
            label = normalize_portal_background(gift.get('background_label'))
            wanted = normalize_portal_background(backdrop)
            if wanted and label == wanted:
                candidates.append((0, int(cents), 'Portal · фон'))
            elif not label and not wanted:
                candidates.append((1, int(cents), 'Portal · коллекция'))
    if candidates:
        _, cents, source = min(candidates)
        return cents, source
    if normalize_portal_background(backdrop):
        return 0, ''
    try:
        response = requests.get('https://portal-market.com/api/collections', params={'search': collection_name, 'limit': 10},
                                headers=portal_headers(key), timeout=(2, 4))
        if response.ok and len(response.content) < 2_000_000:
            payload = response.json()
            rows = payload.get('collections', payload.get('data', payload)) if isinstance(payload, dict) else payload
            if isinstance(rows, dict):
                rows = rows.get('items') or rows.get('collections') or []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                if portal_short_name(row.get('name') or row.get('title') or row.get('short_name')) != short:
                    continue
                raw = next((row.get(k) for k in ('floor_price','floorPrice','price') if row.get(k) is not None), None)
                if raw is None: continue
                try:
                    cents = ton_to_cents(raw)
                except (ValueError, TypeError, InvalidOperation):
                    continue
                if cents > 0:
                    return int(cents), 'Portal · коллекция'
    except (requests.RequestException, ValueError, TypeError):
        pass
    return 0, ''


def fragment_gift_from_url(value, fetch_meta=True, refresh=False, allow_missing_price=False):
    url = str(value or '').strip()
    match = FRAGMENT_GIFT_RE.fullmatch(url)
    if not match:
        raise ValueError('Ссылка должна быть вида https://fragment.com/gift/slug-12345')
    raw_slug = match.group(1).strip('-')
    slug = raw_slug.lower()
    number_match = re.search(r'-(\d+)$', raw_slug)
    if not number_match:
        raise ValueError('Нужна ссылка на конкретный Fragment-подарок с номером, например PlushPepe-12345.')
    if fetch_meta and not refresh:
        with fragment_preview_lock:
            cached = fragment_preview_cache.get(slug)
            if cached and time.monotonic() - cached[0] < 300:
                return dict(cached[1])
    number = number_match.group(1)
    raw_base_slug = raw_slug[:number_match.start()]
    readable_base = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', raw_base_slug).replace('-', ' ')
    collection_name = ' '.join(word[:1].upper() + word[1:] for word in readable_base.split() if word) or 'Fragment Gift'
    name = f'{collection_name} #{number}'
    canonical = f'https://t.me/nft/{raw_slug}'
    image_url = f'https://nft.fragment.com/gift/{slug}.webp'
    model = backdrop = symbol = animation_url = ''
    metadata_image = False
    floor_price = 0
    price_source = ''
    if fetch_meta:
        # Fragment's public NFT JSON gives us the exact rendered gift and, when available, attributes.
        try:
            response = requests.get(f'https://nft.fragment.com/gift/{slug}.json', timeout=(2, 5), headers={
                'User-Agent': 'Mozilla/5.0 (compatible; GemDrop/1.0)', 'Accept': 'application/json,*/*'})
            if response.ok and len(response.content) < 2_000_000:
                payload = response.json()
                candidate = str(payload.get('name') or payload.get('title') or '').strip() if isinstance(payload, dict) else ''
                if candidate:
                    name = candidate[:140]
                exact_image = _fragment_json_image(payload)
                if exact_image:
                    image_url = exact_image
                    metadata_image = True
                animation_url = _fragment_json_animation(payload)
                traits = _fragment_traits_from_json(payload)
                model, backdrop, symbol = traits['model'], traits['backdrop'], traits['symbol']
                floor_price = _price_from_json(payload)
                if floor_price:
                    price_source = 'Fragment'
        except (requests.RequestException, ValueError, TypeError):
            pass
        # Telegram's public collectible page is a reliable fallback for the exact model/backdrop/symbol.
        try:
            response = requests.get(f'https://t.me/nft/{raw_slug}', timeout=(2, 5), headers={
                'User-Agent': 'Mozilla/5.0 (compatible; GemDrop/1.0)', 'Accept': 'text/html,application/xhtml+xml'})
            if response.ok and len(response.text) < 2_000_000:
                html_text = response.text
                title = _fragment_meta_content(html_text, 'og:title')
                exact_image = _fragment_meta_content(html_text, 'og:image')
                description = (_fragment_meta_content(html_text, 'twitter:description') or
                               _fragment_meta_content(html_text, 'og:description'))
                if title:
                    candidate = re.sub(r'\s*[–—-]\s*(?:Fragment|Telegram)\s*$', '', title, flags=re.I).strip()
                    if candidate:
                        name = candidate[:140]
                if not metadata_image and safe_image(exact_image):
                    image_url = exact_image
                animation_url = animation_url or safe_image(_fragment_meta_content(html_text, 'og:video'))
                traits = _fragment_traits_from_text(description)
                model = model or traits['model']; backdrop = backdrop or traits['backdrop']; symbol = symbol or traits['symbol']
                if not floor_price:
                    floor_price = _fragment_price_from_text(description + ' ' + html_text[:500000])
                    if floor_price:
                        price_source = 'Fragment'
        except requests.RequestException:
            pass
        if not animation_url or not floor_price:
            try:
                response = requests.get(f'https://fragment.com/gift/{raw_slug}', timeout=(2, 3), headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; GemDrop/1.0)', 'Accept': 'text/html'})
                if response.ok and len(response.content) < 2_000_000:
                    html_text = response.text
                    if not floor_price:
                        floor_price = _fragment_price_from_text(html_text)
                        if floor_price:
                            price_source = 'Fragment'
                    animation_url = animation_url or safe_image(_fragment_meta_content(html_text, 'og:video'))
                    if not animation_url:
                        match_video = re.search(r'<(?:video|source)\b[^>]*\bsrc=["\'](https://[^"\']+)', html_text, re.I)
                        animation_url = safe_image(unescape(match_video.group(1))) if match_video else ''
                    if not animation_url:
                        match_video = re.search(r'["\']animation_url["\']\s*:\s*["\'](https?[^"\']+)', html_text, re.I)
                        animation_url = safe_image(unescape(match_video.group(1)).replace('\\/', '/')) if match_video else ''
            except requests.RequestException:
                pass
        # A specific collectible is often not listed. In that case show the closest useful market floor:
        # model first, collection second.
        if not floor_price:
            display_collection = re.sub(r'\s*#\s*\d+.*$', '', name).strip()
            if portal_short_name(display_collection) == portal_short_name(collection_name):
                collection_name = display_collection
            floor_price, price_source = _fragment_portal_fallback_price(collection_name, model, backdrop)
    if fetch_meta and normalize_portal_background(backdrop) and not floor_price and not allow_missing_price:
        raise ValueError('Не удалось получить цену подарка с этим фоном. Подарок не добавлен.')
    gift = dict(source_type='fragment', gift_id='fragment:' + slug, gift_name=name[:140],
                image_url=image_url, floor_price=max(0, int(floor_price or 0)), fragment_url=canonical,
                fragment_number=number, fragment_model=model, fragment_backdrop=backdrop,
                fragment_symbol=symbol, price_source=price_source, animation_url=animation_url,
                slug=slug, collection_name=collection_name)
    if fetch_meta and floor_price > 0:
        with fragment_preview_lock:
            if len(fragment_preview_cache) >= 256:
                oldest = min(fragment_preview_cache, key=lambda k: fragment_preview_cache[k][0])
                fragment_preview_cache.pop(oldest, None)
            fragment_preview_cache[slug] = (time.monotonic(), dict(gift))
    return gift


def catalog_giveaway_prize(gift_id):
    gift_id = str(gift_id or '')
    gift = next((x for x in read_catalog().get('gifts', []) if str(x.get('id')) == gift_id), None)
    if not gift:
        raise ValueError('Подарок не найден в каталоге Portal.')
    try:
        price = ton_to_cents(gift.get('price_ton') or 0)
    except (ValueError, TypeError, InvalidOperation):
        price = 0
    image_url = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
    if not image_url:
        raise ValueError('У подарка нет изображения.')
    return dict(source_type='catalog', gift_id=gift_id,
                gift_name=str(gift.get('name') or 'Подарок')[:140], image_url=image_url,
                floor_price=max(0, int(price)), fragment_url='', fragment_number='',
                fragment_model='', fragment_backdrop=str(gift.get('background_label') or ''),
                fragment_symbol='', price_source=gift.get('price_source') or 'Portal')


def giveaway_prize_view(row):
    keys = row.keys()
    return dict(id=int(row['id']), source_type=row['source_type'], gift_id=row['gift_id'],
                name=row['gift_name'], image_url=row['image_url'], price_ton=int(row['floor_price'] or 0)/100,
                quantity=max(1, int(row['quantity'] or 1)) if 'quantity' in keys else 1,
                fragment_url=row['fragment_url'] or '', fragment_number=row['fragment_number'] or '',
                fragment_model=(row['fragment_model'] or '') if 'fragment_model' in keys else '',
                fragment_backdrop=(row['fragment_backdrop'] or '') if 'fragment_backdrop' in keys else '',
                fragment_symbol=(row['fragment_symbol'] or '') if 'fragment_symbol' in keys else '',
                price_source=(row['price_source'] or '') if 'price_source' in keys else '',
                animation_url=(row['animation_url'] or '') if 'animation_url' in keys else '')


def finalize_giveaway(db, giveaway_id):
    giveaway = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
    if not giveaway or giveaway['status'] != 'active':
        return False
    end = parse_datetime_utc(giveaway['ends_at'])
    if not end or end > datetime.now(timezone.utc):
        return False
    changed = db.execute("UPDATE giveaways SET status='settling' WHERE id=? AND status='active'", (giveaway_id,)).rowcount
    if not changed:
        return False
    entrants = [dict(user_id=int(r['user_id']), tickets=int(r['tickets'] or 0)) for r in
                db.execute('SELECT user_id,tickets FROM giveaway_entries WHERE giveaway_id=? AND tickets>0 ORDER BY user_id', (giveaway_id,)).fetchall()]
    prizes = db.execute('SELECT * FROM giveaway_prizes WHERE giveaway_id=? ORDER BY position,id', (giveaway_id,)).fetchall()
    requested = max(0, int(giveaway['winner_count'] or 0))
    prize_slots = []
    for prize in prizes:
        qty = max(1, int(prize['quantity'] or 1)) if 'quantity' in prize.keys() else 1
        prize_slots.extend([prize] * qty)
    # Backward compatibility: old giveaways could have winner_count larger than the number of saved prize rows.
    if prizes and len(prize_slots) < requested:
        prize_slots.extend(prizes[i % len(prizes)] for i in range(requested - len(prize_slots)))
    prize_slots = prize_slots[:requested] if requested else prize_slots
    candidates = [x for x in entrants if x['tickets'] > 0]
    allow_repeat = bool(giveaway['allow_repeat_winners']) if 'allow_repeat_winners' in giveaway.keys() else True
    notifications = {}
    for rank, prize in enumerate(prize_slots, 1):
        if not candidates:
            break
        total = sum(x['tickets'] for x in candidates)
        if total <= 0:
            break
        pick = secrets.randbelow(total)
        chosen_index = 0
        cursor = 0
        for i, candidate in enumerate(candidates):
            cursor += candidate['tickets']
            if pick < cursor:
                chosen_index = i
                break
        winner = candidates[chosen_index]
        if not allow_repeat:
            candidates.pop(chosen_index)
        cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                         external_url,fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url)
                         VALUES(?,?,?,?,?,'giveaway',?,?,?,?,?,?,?)""",
                         (winner['user_id'], prize['gift_id'], prize['gift_name'], prize['image_url'], int(prize['floor_price'] or 0),
                          prize['fragment_url'] or '', prize['fragment_number'] or '',
                          (prize['fragment_model'] or '') if 'fragment_model' in prize.keys() else '',
                          (prize['fragment_backdrop'] or '') if 'fragment_backdrop' in prize.keys() else '',
                          (prize['fragment_symbol'] or '') if 'fragment_symbol' in prize.keys() else '',
                          (prize['price_source'] or '') if 'price_source' in prize.keys() else '',
                          (prize['animation_url'] or '') if 'animation_url' in prize.keys() else ''))
        inventory_id = cur.lastrowid
        db.execute('INSERT INTO giveaway_winners(giveaway_id,user_id,prize_id,rank,tickets,inventory_id) VALUES(?,?,?,?,?,?)',
                   (giveaway_id, winner['user_id'], prize['id'], rank, winner['tickets'], inventory_id))
        log_event(db, winner['user_id'], 'giveaway_win', giveaway_id=giveaway_id, rank=rank,
                  gift_name=prize['gift_name'], tickets=winner['tickets'], giveaway_title=giveaway['title'])
        notifications.setdefault(winner['user_id'], []).append(dict(
            rank=rank, name=prize['gift_name'], fragment_url=prize['fragment_url'] or '',
            fragment_number=prize['fragment_number'] or '', price_cents=int(prize['floor_price'] or 0)))
    db.execute("UPDATE giveaways SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE id=?", (giveaway_id,))
    # Deliver one compact message per winner even if the same person won several places.
    for user_id, wins in notifications.items():
        notify_giveaway_wins_async(user_id, giveaway['title'], wins, db=db)
    return True


def finalize_due_giveaways(db):
    now = datetime.now(timezone.utc).isoformat()
    due = db.execute("SELECT id FROM giveaways WHERE status='active' AND ends_at<=? ORDER BY ends_at,id", (now,)).fetchall()
    changed = False
    for row in due:
        changed = finalize_giveaway(db, int(row['id'])) or changed
    return changed


def giveaway_view(db, row, user_id=None, include_top=False):
    giveaway_id = int(row['id'])
    prizes = [giveaway_prize_view(x) for x in db.execute(
        'SELECT * FROM giveaway_prizes WHERE giveaway_id=? ORDER BY position,id', (giveaway_id,)).fetchall()]
    prizes = visible_gifts(prizes)
    prize_count = sum(max(1, int(x.get('quantity') or 1)) for x in prizes)
    stats = db.execute('SELECT COUNT(*) AS participants,COALESCE(SUM(tickets),0) AS pool FROM giveaway_entries WHERE giveaway_id=? AND tickets>0',
                       (giveaway_id,)).fetchone()
    mine = 0
    if user_id:
        mine_row = db.execute('SELECT tickets FROM giveaway_entries WHERE giveaway_id=? AND user_id=?',
                              (giveaway_id, int(user_id))).fetchone()
        mine = int(mine_row['tickets'] or 0) if mine_row else 0
    winners = []
    if row['status'] == 'completed':
        winner_rows = db.execute('''SELECT w.rank,w.tickets,w.inventory_id,u.id AS user_id,u.name,u.username,u.photo_url,
                                  p.gift_name,p.image_url,p.fragment_url,p.fragment_number,p.floor_price,
                                  p.fragment_model,p.fragment_backdrop,p.fragment_symbol,p.price_source,p.animation_url
                                  FROM giveaway_winners w JOIN users u ON u.id=w.user_id
                                  JOIN giveaway_prizes p ON p.id=w.prize_id
                                  WHERE w.giveaway_id=? ORDER BY w.rank''', (giveaway_id,)).fetchall()
        winners = [dict(rank=int(x['rank']), tickets=int(x['tickets']), user_id=int(x['user_id']), name=x['name'],
                        username=x['username'], photo_url=x['photo_url'], inventory_id=x['inventory_id'],
                        prize=dict(name=x['gift_name'], image_url=x['image_url'], price_ton=int(x['floor_price'] or 0)/100,
                                   fragment_url=x['fragment_url'] or '', fragment_number=x['fragment_number'] or '',
                                   fragment_model=x['fragment_model'] or '', fragment_backdrop=x['fragment_backdrop'] or '',
                                   fragment_symbol=x['fragment_symbol'] or '', price_source=x['price_source'] or '',
                                   animation_url=x['animation_url'] or ''))
                   for x in winner_rows]
    if not black_backgrounds_enabled():
        winners = [winner for winner in winners if not gift_black_background(winner['prize'])]
    top = []
    if include_top:
        top_rows = db.execute('''SELECT e.user_id,e.tickets,u.name,u.username,u.photo_url
                                 FROM giveaway_entries e JOIN users u ON u.id=e.user_id
                                 WHERE e.giveaway_id=? AND e.tickets>0
                                 ORDER BY e.tickets DESC,e.updated_at ASC,e.user_id ASC LIMIT 100''', (giveaway_id,)).fetchall()
        top = [dict(rank=i+1, user_id=int(x['user_id']), tickets=int(x['tickets']), name=x['name'],
                    username=x['username'], photo_url=x['photo_url']) for i, x in enumerate(top_rows)]
    return dict(id=giveaway_id, title=row['title'], description=row['description'] or '', status=row['status'],
                starts_at=row['starts_at'], ends_at=row['ends_at'], winner_count=int(row['winner_count'] or prize_count),
                prize_count=prize_count, allow_repeat_winners=bool(row['allow_repeat_winners']) if 'allow_repeat_winners' in row.keys() else True,
                participants=int(stats['participants'] or 0), pool=int(stats['pool'] or 0), my_tickets=mine,
                prizes=prizes, winners=winners, top=top, completed_at=row['completed_at'])


TASK_METRICS = {
    'upgrade_play': ('upgrade_spins', '1=1'),
    'upgrade_gift': ('upgrade_spins', '''won=1 AND REPLACE(result_json,' ','') NOT LIKE '%"reward_type":"wager_progress"%' '''),
    'upgrade_low': ('upgrade_spins', 'won=1 AND chance_bp<2500'),
    'upgrade_win': ('upgrade_spins', 'won=1'),
    'craft': ('craft_spins', '1=1'),
    'deposit_5': ('deposits', 'amount>=500'),
    'deposit': ('deposits', 'amount>0'),
    'referral': ('referrals', '1=1'),
    'roll': ('roll_spins', '1=1'),
    'mines_play': ('rounds', "state IN ('won','lost')"),
    'mines_win': ('rounds', "state='won'"),
    'promo': ('promo_redemptions', '1=1'),
}
TASK_SPECIAL = ('level', 'link', 'subscribe')
TASK_VALUE_COLUMN = {'mines_play': 'bet', 'mines_win': 'bet', 'upgrade_play': 'source_price', 'upgrade_win': 'source_price',
                     'upgrade_gift': 'source_price', 'upgrade_low': 'source_price', 'deposit': 'amount'}
TASK_PAGES = {'upgradePage', 'craftPage', 'profilePage', 'minesPage', 'giveawayPage', 'rollPage'}


def reward_task_chance(task):
    operator = task['chance_operator'] if 'chance_operator' in task.keys() else 'any'
    threshold = task['chance_threshold_bp'] if 'chance_threshold_bp' in task.keys() else None
    if task['metric'] == 'upgrade_low' and operator == 'any':
        return 'lt', 2500
    return operator, threshold


def reward_task_title(metric, goal, operator='any', threshold=None, min_value=0):
    action = {'upgrade_play':'Сыграть в апгрейд', 'upgrade_win':'Победить в апгрейде',
              'upgrade_gift':'Выиграть подарок в апгрейде', 'upgrade_low':'Победить в апгрейде',
              'deposit':'Пополнить баланс', 'deposit_5':'Пополнить баланс от 5 TON',
              'referral':'Пригласить друзей', 'craft':'Сделать крафт', 'roll':'Сыграть в Roll',
              'mines_play':'Сыграть в Mines', 'mines_win':'Выиграть в Mines', 'promo':'Активировать промокод'}[metric]
    times = 'раза' if 2 <= goal % 10 <= 4 and not 12 <= goal % 100 <= 14 else 'раз'
    title = f'Пригласить {goal} '+('друга' if goal % 10 == 1 and goal % 100 != 11 else 'друзей') if metric == 'referral' else f'{action} {goal} {times}'
    if metric.startswith('upgrade') and operator != 'any':
        relation = {'lt':'меньше','lte':'не больше','gt':'больше','gte':'не меньше'}[operator]
        percent = format(Decimal(threshold) / 100, 'f').rstrip('0').rstrip('.') if threshold % 100 else str(threshold // 100)
        title += f' с шансом {relation} {percent}%'
    if min_value and metric in TASK_VALUE_COLUMN:
        title += f' (от {Decimal(min_value) / 100:.2f} TON)'
    return title


def normalize_reward_task(data, current=None):
    current = dict(current or {})
    metric = str(data.get('metric', current.get('metric', 'upgrade_play')))
    category = str(data.get('category', current.get('category', 'once')))
    if (metric not in TASK_METRICS and metric not in TASK_SPECIAL) or metric in ('craft','roll') or category not in ('once','daily','limited'):
        raise ValueError('Выберите действие и период задания.')
    def integer(key, default, low, high):
        raw = data.get(key, current.get(key, default))
        if isinstance(raw, bool) or not re.fullmatch(r'\d+', str(raw)):
            raise ValueError('Проверьте числовые поля задания.')
        value = int(raw)
        if not low <= value <= high:
            raise ValueError(f'Поле {key}: допустимо от {low} до {high}.')
        return value
    goal = 1 if metric in TASK_SPECIAL else integer('goal', 1, 1, 100000)
    min_value = 0
    if metric == 'level':
        min_value = integer('min_value', 1, 1, 1000)
    elif metric in TASK_VALUE_COLUMN:
        raw_min = data.get('min_value', current.get('min_value', 0) / 100 if current else 0)
        try:
            min_value = parse_amount(raw_min or 0)
        except (ValueError, TypeError, InvalidOperation):
            raise ValueError('Минимальная сумма указана неверно.')
        if not 0 <= min_value <= 100000000:
            raise ValueError('Минимальная сумма: от 0 до 1 000 000 TON.')
    link_url = str(data.get('link_url', current.get('link_url', '')) or '').strip()
    if link_url and not re.fullmatch(r'https?://[^\s<>"]{3,280}', link_url):
        raise ValueError('Ссылка должна начинаться с https://')
    if metric == 'link' and not link_url:
        raise ValueError('Для задания со ссылкой укажите URL.')
    description = str(data.get('description', current.get('description', '')) or '').strip()[:200]
    tickets = integer('tickets', 25, 1, 100000)
    old_operator, old_threshold = reward_task_chance(current) if current else ('any', None)
    operator = str(data.get('chance_operator', old_operator)) if metric.startswith('upgrade') else 'any'
    if operator not in ('any','lt','lte','gt','gte'):
        raise ValueError('Выберите сравнение шанса.')
    threshold = None
    if operator != 'any':
        try:
            percent = Decimal(str(data.get('chance_percent', Decimal(old_threshold if old_threshold is not None else 2500)/100)))
            if not percent.is_finite() or not 0 <= percent <= 100 or percent * 100 != (percent * 100).to_integral_value():
                raise ValueError()
            threshold = int(percent * 100)
        except (ValueError, TypeError, InvalidOperation):
            raise ValueError('Шанс: от 0 до 100%, не более двух знаков после запятой.')
    if metric == 'upgrade_low':
        metric = 'upgrade_win'
        if operator == 'any': operator, threshold = 'lt', 2500
    title = str(data.get('title', '' if current.get('auto_title') else current.get('title', '')) or '').strip()
    if len(title) > 160:
        raise ValueError('Название должно быть не длиннее 160 символов.')
    auto_title = not title
    default_special = {'level': f'Достигните {min_value} уровня', 'link': 'Выполните задание по ссылке', 'subscribe': 'Подпишитесь на канал'}
    title = title or default_special.get(metric) or reward_task_title(metric, goal, operator, threshold, min_value)
    end = current.get('ends_at') if category == current.get('category') else None
    if category == 'limited' and ('duration_hours' in data or not end):
        hours = integer('duration_hours', 24, 1, 2160)
        end = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    if category != 'limited': end = None
    page = 'upgradePage' if metric.startswith('upgrade') else 'minesPage' if metric.startswith('mines') else 'profilePage'
    return dict(title=title, category=category, metric=metric, goal=goal, tickets=tickets,
                action_page=page, demo=0, ends_at=end, chance_operator=operator,
                chance_threshold_bp=threshold, auto_title=int(auto_title),
                min_value=min_value, link_url=link_url, description=description)


def reward_task_period(task):
    return datetime.now(timezone.utc).strftime('%Y-%m-%d') if task['category'] == 'daily' else 'once'


def reward_task_progress(db, task, user_id):
    metric = task['metric']
    if metric == 'level':
        row = db.execute('SELECT turnover_cents FROM users WHERE id=?', (user_id,)).fetchone()
        return 1 if row and level_number(db, int(row['turnover_cents'] or 0)) >= int(task['min_value'] or 1) else 0
    if metric in ('link', 'subscribe'):
        key = 'visit:' + reward_task_period(task)
        return 1 if db.execute('SELECT 1 FROM reward_task_claims WHERE task_id=? AND user_id=? AND period_key=?',
                               (task['id'], user_id, key)).fetchone() else 0
    if metric not in TASK_METRICS:
        return 0
    table, condition = TASK_METRICS[task['metric']]
    since = parse_datetime_utc(task['created_at']) or datetime.min.replace(tzinfo=timezone.utc)
    if task['category'] == 'daily':
        since = max(since, datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
    person_column = 'referrer_id' if table == 'referrals' else 'user_id'
    params = [user_id, since.strftime('%Y-%m-%d %H:%M:%S')]
    # Both legacy ISO timestamps and SQL timestamps are stored in UTC.
    timestamp = "REPLACE(SUBSTR(created_at,1,19),'T',' ')"
    operator, threshold = reward_task_chance(task)
    if task['metric'].startswith('upgrade') and operator != 'any':
        comparison = {'lt':'<','lte':'<=','gt':'>','gte':'>='}.get(operator)
        if not comparison or threshold is None: return 0
        condition += f' AND chance_bp {comparison} ?'
        params.append(int(threshold))
    value_column = TASK_VALUE_COLUMN.get(task['metric'])
    if value_column and int(task['min_value'] or 0) > 0:
        condition += f' AND {value_column} >= ?'
        params.append(int(task['min_value']))
    end = parse_datetime_utc(task['ends_at']) if task['ends_at'] else None
    if end:
        condition += f' AND {timestamp} < ?'
        params.append(end.strftime('%Y-%m-%d %H:%M:%S'))
    row = db.execute(f'SELECT COUNT(*) AS n FROM {table} WHERE {person_column}=? AND {timestamp}>=? AND {condition}', params).fetchone()
    return min(int(row['n'] or 0), int(task['goal']))


def reward_task_view(db, task, user_id):
    period = reward_task_period(task)
    claimed = db.execute('SELECT 1 FROM reward_task_claims WHERE task_id=? AND user_id=? AND period_key=?',
                         (task['id'], user_id, period)).fetchone() is not None
    end = parse_datetime_utc(task['ends_at']) if task['ends_at'] else None
    expired = bool(end and end <= datetime.now(timezone.utc))
    operator, threshold = reward_task_chance(task)
    return dict(id=task['id'], title=task['title'], category=task['category'], metric=task['metric'],
                chance_operator=operator, chance_percent=threshold/100 if threshold is not None else None,
                goal=int(task['goal']), progress=reward_task_progress(db, task, user_id) if not task['demo'] else 0,
                tickets=int(task['tickets']), action_page=task['action_page'], demo=bool(task['demo']),
                active=bool(task['active']), claimed=claimed, expired=expired, ends_at=task['ends_at'],
                min_value=int(task['min_value'] or 0) / 100 if task['metric'] != 'level' else int(task['min_value'] or 0),
                link_url=task['link_url'] or '', description=task['description'] or '')


@app.get('/api/reward-tasks')
@login_required
def reward_tasks_list():
    with connect() as db:
        rows = db.execute('SELECT * FROM reward_tasks WHERE active=1 ORDER BY CASE category WHEN \'limited\' THEN 0 WHEN \'daily\' THEN 1 ELSE 2 END,id').fetchall()
        return jsonify(items=[reward_task_view(db, row, session['uid']) for row in rows],
                       tickets=int(db.execute('SELECT tickets FROM users WHERE id=?', (session['uid'],)).fetchone()['tickets'] or 0))


@app.post('/api/reward-tasks/<int:task_id>/visit')
@login_required
def reward_task_visit(task_id):
    with connect() as db:
        task = db.execute('SELECT * FROM reward_tasks WHERE id=? AND active=1', (task_id,)).fetchone()
        if not task or task['metric'] not in ('link', 'subscribe'):
            return error('Задание не найдено.', 404)
        db.execute('INSERT OR IGNORE INTO reward_task_claims(task_id,user_id,period_key) VALUES(?,?,?)',
                   (task_id, session['uid'], 'visit:' + reward_task_period(task)))
    return jsonify(ok=True)


@app.post('/api/reward-tasks/<int:task_id>/claim')
@login_required
def reward_task_claim(task_id):
    with connect() as pre:
        pre_task = pre.execute('SELECT metric FROM reward_tasks WHERE id=? AND active=1', (task_id,)).fetchone()
    if pre_task and pre_task['metric'] == 'subscribe':
        subscribed, reason = telegram_member_subscribed(session['uid'])
        if not subscribed:
            return error(reason or 'Сначала подпишитесь на канал.', 409)
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        task = db.execute('SELECT * FROM reward_tasks WHERE id=? AND active=1', (task_id,)).fetchone()
        if not task or task['demo']:
            return error('Это демонстрационное задание или оно больше недоступно.', 404)
        view = reward_task_view(db, task, session['uid'])
        if view['expired'] or view['claimed'] or view['progress'] < view['goal']:
            return error('Задание ещё не выполнено, уже получено или срок истёк.', 409)
        db.execute('INSERT INTO reward_task_claims(task_id,user_id,period_key) VALUES(?,?,?)',
                   (task_id, session['uid'], reward_task_period(task)))
        db.execute('UPDATE users SET tickets=tickets+? WHERE id=?', (task['tickets'], session['uid']))
        record_tickets(db, session['uid'], task['tickets'], 'task', 'reward_task', task_id, task['title'])
        log_event(db, session['uid'], 'reward_task_claim', task_id=task_id, tickets=task['tickets'])
        db.commit()
        return jsonify(ok=True, tickets=int(db.execute('SELECT tickets FROM users WHERE id=?', (session['uid'],)).fetchone()['tickets']))
    finally:
        db.close()


@app.get('/api/admin/reward-tasks')
@admin_required
def admin_reward_tasks_list():
    with connect() as db:
        rows = db.execute('SELECT * FROM reward_tasks ORDER BY id DESC LIMIT 200').fetchall()
        return jsonify(items=[dict(dict(row), chance_operator=reward_task_chance(row)[0], chance_percent=(reward_task_chance(row)[1]/100 if reward_task_chance(row)[1] is not None else None)) for row in rows])


@app.post('/api/admin/reward-tasks')
@admin_required
def admin_reward_task_create():
    try:
        values = normalize_reward_task(request.get_json(silent=True) or {})
    except ValueError as exc:
        return error(str(exc))
    with connect() as db:
        columns = ','.join(values)
        marks = ','.join('?' for _ in values)
        result = db.execute(f'INSERT INTO reward_tasks({columns}) VALUES({marks})', tuple(values.values()))
        task_id = result.lastrowid
        db.commit()
    return jsonify(ok=True, id=task_id)


@app.put('/api/admin/reward-tasks/<int:task_id>')
@admin_required
def admin_reward_task_update(task_id):
    data = request.get_json(silent=True) or {}
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        task = db.execute('SELECT * FROM reward_tasks WHERE id=?', (task_id,)).fetchone()
        if not task:
            return error('Задание не найдено.', 404)
        try:
            values = normalize_reward_task(data, task)
        except ValueError as exc:
            return error(str(exc))
        if values['category'] != task['category'] and db.execute(
                'SELECT 1 FROM reward_task_claims WHERE task_id=? LIMIT 1', (task_id,)).fetchone():
            return error('Период уже полученного задания менять нельзя. Создайте новое задание.')
        assignments = ','.join(f'{key}=?' for key in values)
        db.execute(f'UPDATE reward_tasks SET {assignments} WHERE id=?', (*values.values(), task_id))
        db.commit()
    return jsonify(ok=True, id=task_id)


@app.delete('/api/admin/reward-tasks/<int:task_id>')
@admin_required
def admin_reward_task_delete(task_id):
    with connect() as db:
        if not db.execute('SELECT 1 FROM reward_tasks WHERE id=?', (task_id,)).fetchone():
            return error('Задание не найдено.', 404)
        if db.execute("SELECT 1 FROM reward_task_claims WHERE task_id=? AND period_key NOT LIKE 'visit:%' LIMIT 1", (task_id,)).fetchone():
            db.execute('UPDATE reward_tasks SET active=0 WHERE id=?', (task_id,))
            return jsonify(ok=True, disabled=True)
        db.execute('DELETE FROM reward_task_claims WHERE task_id=?', (task_id,))
        db.execute('DELETE FROM reward_tasks WHERE id=?', (task_id,))
    return jsonify(ok=True, deleted=True)


@app.post('/api/admin/reward-tasks/<int:task_id>/toggle')
@admin_required
def admin_reward_task_toggle(task_id):
    with connect() as db:
        row = db.execute('SELECT active FROM reward_tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            return error('Задание не найдено.', 404)
        db.execute('UPDATE reward_tasks SET active=? WHERE id=?', (0 if row['active'] else 1, task_id))
        db.commit()
    return jsonify(ok=True)


@app.get('/api/giveaways')
@login_required
def giveaways_list():
    status = str(request.args.get('status') or 'active').lower()
    sort = str(request.args.get('sort') or 'date').lower()
    if status not in ('active', 'completed'):
        return error('Неизвестный раздел розыгрышей.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        finalize_due_giveaways(db)
        if status == 'active':
            rows = db.execute("SELECT * FROM giveaways WHERE status='active' AND archived=0 ORDER BY ends_at ASC,id DESC").fetchall()
        else:
            rows = db.execute("SELECT * FROM giveaways WHERE status='completed' AND archived=0 ORDER BY completed_at DESC,id DESC LIMIT 100").fetchall()
        items = [giveaway_view(db, row, session['uid'], False) for row in rows]
        items = [item for item in items if item['prizes']]
        if sort == 'pool':
            items.sort(key=lambda x: (x['pool'], x['participants'], x['id']), reverse=True)
        elif status == 'completed':
            items.sort(key=lambda x: (x.get('completed_at') or '', x['id']), reverse=True)
        else:
            items.sort(key=lambda x: (x['ends_at'], x['id']))
        db.commit()
        balance = db.execute('SELECT tickets FROM users WHERE id=?', (session['uid'],)).fetchone()
        return jsonify(items=items, tickets=int(balance['tickets'] or 0) if balance else 0)
    finally:
        db.close()


@app.get('/api/giveaways/<int:giveaway_id>')
@login_required
def giveaway_detail(giveaway_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        finalize_due_giveaways(db)
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row or row['status'] == 'cancelled' or row['archived']:
            return error('Розыгрыш не найден.', 404)
        item = giveaway_view(db, row, session['uid'], True)
        if not item['prizes']:
            return error('Розыгрыш сейчас скрыт.', 404)
        balance = db.execute('SELECT tickets FROM users WHERE id=?', (session['uid'],)).fetchone()
        db.commit()
        return jsonify(item=item, tickets=int(balance['tickets'] or 0) if balance else 0)
    finally:
        db.close()


@app.post('/api/giveaways/<int:giveaway_id>/enter')
@login_required
def giveaway_enter(giveaway_id):
    data = request.get_json(silent=True) or {}
    try:
        tickets = int(data.get('tickets') or 0)
    except (TypeError, ValueError):
        return error('Укажите количество билетов.')
    if not 1 <= tickets <= 1_000_000:
        return error('Можно добавить от 1 до 1 000 000 билетов за один раз.')
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        finalize_due_giveaways(db)
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row or row['status'] != 'active':
            return error('Розыгрыш уже завершён.', 409)
        end = parse_datetime_utc(row['ends_at'])
        if not end or end <= datetime.now(timezone.utc):
            finalize_giveaway(db, giveaway_id)
            return error('Розыгрыш уже завершён.', 409)
        user = db.execute('SELECT tickets FROM users WHERE id=?' + (' FOR UPDATE' if DATABASE_URL else ''),
                          (session['uid'],)).fetchone()
        available = int(user['tickets'] or 0) if user else 0
        if available < tickets:
            return error(f'Недостаточно билетов. Доступно: {available}.', 409)
        db.execute('UPDATE users SET tickets=tickets-? WHERE id=?', (tickets, session['uid']))
        db.execute('''INSERT INTO giveaway_entries(giveaway_id,user_id,tickets) VALUES(?,?,?)
                      ON CONFLICT(giveaway_id,user_id) DO UPDATE SET tickets=giveaway_entries.tickets+excluded.tickets,
                      updated_at=CURRENT_TIMESTAMP''', (giveaway_id, session['uid'], tickets))
        record_tickets(db, session['uid'], -tickets, 'giveaway_entry', 'giveaway', giveaway_id,
                       f'Участие в розыгрыше «{row["title"]}»')
        log_event(db, session['uid'], 'giveaway_enter', giveaway_id=giveaway_id, tickets=tickets)
        db.commit()
        fresh = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        return jsonify(ok=True, item=giveaway_view(db, fresh, session['uid'], True), user=profile())
    finally:
        db.close()


@app.get('/api/admin/giveaways')
@admin_required
def admin_giveaways():
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        finalize_due_giveaways(db)
        rows = db.execute("SELECT * FROM giveaways WHERE status<>'cancelled' AND archived=0 ORDER BY created_at DESC,id DESC LIMIT 200").fetchall()
        items = [giveaway_view(db, row, None, False) for row in rows]
        db.commit()
    finally:
        db.close()
    return jsonify(items=items)


@app.post('/api/admin/giveaways/clear-completed')
@admin_required
def admin_clear_completed_giveaways():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        count = db.execute("UPDATE giveaways SET archived=1 WHERE status IN ('completed','cancelled') AND archived=0").rowcount
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,0,?,?)',
                   (session['uid'],'giveaways_archived',str(count)))
    return jsonify(ok=True,hidden=count)


@app.get('/api/admin/game-history')
@admin_required
def admin_game_history():
    try:
        offset=max(0,min(100000,int(request.args.get('offset',0))))
        uid=int(request.args.get('user_id') or 0)
    except (ValueError,TypeError): return error('Введите ID пользователя.')
    game=request.args.get('game','all')
    if game not in ('all','mines','upgrade'): return error('Выберите игру.')
    queries=[]
    params=[]
    if game in ('all','mines'):
        queries.append("""SELECT CAST(r.id AS TEXT) AS id,r.user_id,'mines' AS game,r.state AS outcome,
                         r.bet AS stake,r.payout AS payout,0 AS chance,r.bet_gift_name AS source_name,
                         r.win_gift_name AS gift_name,r.created_at AS date,u.name,u.username
                         FROM rounds r JOIN users u ON u.id=r.user_id"""+(' WHERE r.user_id=?' if uid else ''))
        if uid: params.append(uid)
    if game in ('all','upgrade'):
        queries.append("""SELECT s.id,s.user_id,'upgrade' AS game,CASE WHEN s.won=1 THEN 'won' ELSE 'lost' END AS outcome,
                         s.source_price AS stake,CASE WHEN s.won=1 THEN s.target_price ELSE 0 END AS payout,
                         s.chance_bp AS chance,s.source_name,s.target_name AS gift_name,s.created_at AS date,u.name,u.username
                         FROM upgrade_spins s JOIN users u ON u.id=s.user_id"""+(' WHERE s.user_id=?' if uid else ''))
        if uid: params.append(uid)
    with connect() as db:
        rows=db.execute('SELECT * FROM ('+' UNION ALL '.join(queries)+") AS history ORDER BY REPLACE(SUBSTR(date,1,19),'T',' ') DESC,game,id DESC LIMIT 51 OFFSET ?",(*params,offset)).fetchall()
    items=[]
    for r in rows[:50]:
        item=dict(r)
        for key in ('stake','payout','chance'): item[key]=int(item[key] or 0)/100
        items.append(item)
    return jsonify(items=items,has_more=len(rows)>50)


def apply_fragment_price(gift, value):
    if value in (None, ''): return
    try: cents = parse_amount(str(value).strip().replace(',', '.'))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('Введите цену с точностью до 0.01 TON.')
    if not 1 <= cents <= 100000000:
        raise ValueError('Цена: от 0.01 до 1 000 000 TON.')
    gift.update(floor_price=cents, price_source='Ручная цена')


def giveaway_announcement(title, description, prizes, ends_at=None):
    lines = [f'🎉 Новый розыгрыш — {title}', '']
    if description: lines.extend([description, ''])
    lines.append('🎁 Призы')
    for index, prize in enumerate(prizes, 1):
        line = f'{index}. {prize["gift_name"]} × {prize.get("quantity", 1)}'
        if prize.get('floor_price'): line += f' · {prize["floor_price"]/100:.2f} TON'
        if prize.get('fragment_url'): line += '\n' + prize['fragment_url']
        if len(escape('\n'.join(lines))) + len(escape(line)) > 3200:
            lines.append('Все призы — в розыгрыше.')
            break
        lines.append(line)
    if ends_at:
        end = parse_datetime_utc(ends_at)
        if end: lines.extend(['', '⏰ Итоги: '+end.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y в %H:%M')+' МСК'])
    lines.extend(['', '🎟 Откройте розыгрыш и участвуйте за билеты.'])
    return '\n'.join(lines)


@app.post('/api/admin/giveaways/<int:giveaway_id>/archive')
@admin_required
def admin_archive_giveaway(giveaway_id):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row: return error('Розыгрыш не найден.', 404)
        if row['status'] not in ('completed', 'cancelled'): return error('Сначала завершите розыгрыш.')
        db.execute('UPDATE giveaways SET archived=1 WHERE id=?', (giveaway_id,))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,0,?,?)',
                   (session['uid'], 'giveaway_archived', str(giveaway_id)))
    return jsonify(ok=True)


@app.post('/api/admin/giveaways/<int:giveaway_id>/prizes/<int:prize_id>/price')
@admin_required
def admin_giveaway_prize_price(giveaway_id, prize_id):
    gift = {}
    try:
        apply_fragment_price(gift, (request.get_json(silent=True) or {}).get('price_ton'))
        if not gift: raise ValueError('Введите цену подарка.')
    except ValueError as exc: return error(str(exc))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row or row['archived']: return error('Розыгрыш не найден.', 404)
        if row['status'] != 'active' or parse_datetime_utc(row['ends_at']) <= datetime.now(timezone.utc):
            return error('Розыгрыш уже завершён.')
        changed = db.execute("UPDATE giveaway_prizes SET floor_price=?,price_source='Ручная цена' "
                             "WHERE id=? AND giveaway_id=? AND source_type='fragment'",
                             (gift['floor_price'], prize_id, giveaway_id)).rowcount
        if not changed: return error('Fragment-подарок не найден.', 404)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,0,?,?)',
                   (session['uid'], 'giveaway_prize_price', f'{giveaway_id}:{prize_id}:{gift["floor_price"]}'))
    return jsonify(ok=True)


@app.post('/api/admin/giveaways/fragment-preview')
@admin_required
def admin_fragment_preview():
    data = request.get_json(silent=True) or {}
    try:
        gift = fragment_gift_from_url(data.get('url'), True, allow_missing_price=data.get('price_ton') not in (None, ''))
        apply_fragment_price(gift, data.get('price_ton'))
        if gift_black_background(gift) and not black_backgrounds_enabled():
            return error('Отображение Black и Onyx Black выключено.')
    except ValueError as exc:
        return error(str(exc))
    if data.get('animation_url'):
        gift['animation_url'] = safe_image(data.get('animation_url'))
    return jsonify(ok=True, gift=dict(name=gift['gift_name'], image_url=gift['image_url'],
                                      fragment_url=gift['fragment_url'], fragment_number=gift['fragment_number'],
                                      gift_id=gift['gift_id'], price_ton=int(gift.get('floor_price') or 0)/100,
                                      price_source=gift.get('price_source') or '', model=gift.get('fragment_model') or '',
                                      backdrop=gift.get('fragment_backdrop') or '', symbol=gift.get('fragment_symbol') or '',
                                      animation_url=gift.get('animation_url') or ''))


@app.post('/api/admin/giveaways')
@admin_required
def admin_create_giveaway():
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()[:120]
    description = str(data.get('description') or '').strip()[:500]
    if not title:
        return error('Введите название розыгрыша.')
    try:
        if data.get('duration_minutes') not in (None, ''):
            duration_minutes = int(data.get('duration_minutes'))
        else:
            duration_minutes = round(float(data.get('duration_hours') or 0) * 60)
    except (TypeError, ValueError):
        return error('Проверьте длительность розыгрыша.')
    if not 1 <= duration_minutes <= 60 * 24 * 90:
        return error('Длительность розыгрыша: от 1 минуты до 90 дней.')
    raw_prizes = data.get('prizes')
    if not isinstance(raw_prizes, list) or not raw_prizes:
        return error('Добавьте хотя бы один подарок.')
    if len(raw_prizes) > 100:
        return error('В одном розыгрыше можно указать не более 100 разных строк подарков.')
    prizes = []
    total_slots = 0
    try:
        for item in raw_prizes:
            if not isinstance(item, dict):
                raise ValueError('Проверьте список подарков.')
            quantity = int(item.get('quantity') or 1)
            if not 1 <= quantity <= 100:
                raise ValueError('Количество одного приза должно быть от 1 до 100.')
            source = str(item.get('source_type') or item.get('type') or 'catalog')
            if source == 'fragment':
                prize = fragment_gift_from_url(item.get('fragment_url') or item.get('url'), True,
                                               allow_missing_price=item.get('price_ton') not in (None, ''))
                apply_fragment_price(prize, item.get('price_ton'))
                if item.get('animation_url'):
                    prize['animation_url'] = safe_image(item.get('animation_url'))
            elif source == 'catalog':
                prize = catalog_giveaway_prize(item.get('gift_id'))
            else:
                raise ValueError('Неизвестный источник подарка.')
            if gift_black_background(prize) and not black_backgrounds_enabled():
                raise ValueError('Отображение Black и Onyx Black выключено.')
            prize['quantity'] = quantity
            prizes.append(prize)
            total_slots += quantity
        if not 1 <= total_slots <= 100:
            raise ValueError('Всего в розыгрыше может быть от 1 до 100 призовых мест.')
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return error(str(exc))
    allow_repeat = bool(data.get('allow_repeat_winners', True))
    now = datetime.now(timezone.utc)
    ends = now + timedelta(minutes=duration_minutes)
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        result = db.execute("INSERT INTO giveaways(title,description,starts_at,ends_at,status,winner_count,allow_repeat_winners,created_by) "
                            "VALUES(?,?,?,?,'active',?,?,?) RETURNING id",
                            (title, description, now.isoformat(), ends.isoformat(), total_slots, int(allow_repeat), session['uid'])).fetchone()
        giveaway_id = int(result['id']) if result else None
        if giveaway_id is None:
            fallback = db.execute('SELECT MAX(id) AS id FROM giveaways').fetchone()
            giveaway_id = int(fallback['id'])
        for position, prize in enumerate(prizes):
            db.execute('''INSERT INTO giveaway_prizes(giveaway_id,position,source_type,gift_id,gift_name,image_url,
                          floor_price,quantity,fragment_url,fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (giveaway_id, position, prize['source_type'], prize['gift_id'], prize['gift_name'],
                        prize['image_url'], int(prize.get('floor_price') or 0), int(prize.get('quantity') or 1),
                        prize.get('fragment_url') or '', prize.get('fragment_number') or '', prize.get('fragment_model') or '',
                        prize.get('fragment_backdrop') or '', prize.get('fragment_symbol') or '', prize.get('price_source') or '',
                        prize.get('animation_url') or ''))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'giveaway_create', f'{giveaway_id}:{title}:{total_slots}'))
        announcement = giveaway_announcement(title, description, prizes, ends.isoformat())
        db.execute("INSERT INTO user_notifications(user_id,kind,text,giveaway_id,delivery_state) "
                   "SELECT id,'giveaway_started',?,?,? FROM users",
                   (announcement, giveaway_id, 'pending' if BOT_TOKEN else 'none'))
        db.commit()
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        return jsonify(ok=True, item=giveaway_view(db, row, None, False))
    finally:
        db.close()


@app.put('/api/admin/giveaways/<int:giveaway_id>')
@admin_required
def admin_edit_giveaway(giveaway_id):
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()[:120]
    description = str(data.get('description') or '').strip()[:500]
    if not title:
        return error('Введите название розыгрыша.')
    end = parse_datetime_utc(str(data.get('ends_at') or ''))
    now = datetime.now(timezone.utc)
    if not end or not now < end <= now + timedelta(days=90):
        return error('Укажите дату окончания в пределах следующих 90 дней.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row:
            return error('Розыгрыш не найден.', 404)
        old_end = parse_datetime_utc(row['ends_at'])
        if row['status'] != 'active' or (old_end and old_end <= now):
            return error('Розыгрыш уже завершён.')
        db.execute('UPDATE giveaways SET title=?,description=?,ends_at=? WHERE id=?',
                   (title, description, end.isoformat(), giveaway_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'giveaway_edit', str(giveaway_id)))
        db.commit()
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        return jsonify(ok=True, item=giveaway_view(db, row, None, False))


@app.post('/api/admin/giveaways/<int:giveaway_id>/refresh-prizes')
@admin_required
def admin_refresh_giveaway_prizes(giveaway_id):
    with connect() as db:
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row:
            return error('Розыгрыш не найден.', 404)
        if row['status'] != 'active':
            return error('Можно обновить только активный розыгрыш.')
        rows = db.execute('SELECT * FROM giveaway_prizes WHERE giveaway_id=? ORDER BY position,id',
                          (giveaway_id,)).fetchall()
    refreshed = []
    for prize in rows:
        try:
            gift = (fragment_gift_from_url(prize['fragment_url'], True, refresh=True,
                                           allow_missing_price=prize['price_source'] == 'Ручная цена') if prize['source_type'] == 'fragment'
                    else catalog_giveaway_prize(prize['gift_id']))
            # A temporary provider failure must not erase a known price, trait or animation.
            refreshed.append((prize['id'], {key: gift.get(key) or prize[key] for key in (
                'gift_name', 'image_url', 'floor_price', 'fragment_number', 'fragment_model',
                'fragment_backdrop', 'fragment_symbol', 'price_source', 'animation_url')}))
            if prize['price_source'] == 'Ручная цена':
                refreshed[-1][1].update(floor_price=prize['floor_price'], price_source='Ручная цена')
        except (ValueError, OSError, TypeError):
            continue
    if not refreshed:
        return error('Не удалось обновить подарки. Повторите позже.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        end = parse_datetime_utc(row['ends_at']) if row else None
        if not row or row['status'] != 'active' or (end and end <= datetime.now(timezone.utc)):
            return error('Розыгрыш уже завершён.')
        for prize_id, gift in refreshed:
            current = db.execute('SELECT floor_price,price_source FROM giveaway_prizes WHERE id=? AND giveaway_id=?',
                                 (prize_id, giveaway_id)).fetchone()
            if current and current['price_source'] == 'Ручная цена':
                gift.update(floor_price=current['floor_price'], price_source=current['price_source'])
            db.execute('''UPDATE giveaway_prizes SET gift_name=?,image_url=?,floor_price=?,fragment_number=?,
                          fragment_model=?,fragment_backdrop=?,fragment_symbol=?,price_source=?,animation_url=?
                          WHERE id=? AND giveaway_id=?''',
                       tuple(gift[key] for key in ('gift_name', 'image_url', 'floor_price', 'fragment_number',
                            'fragment_model', 'fragment_backdrop', 'fragment_symbol', 'price_source', 'animation_url'))
                       + (prize_id, giveaway_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'giveaway_refresh_prizes', str(giveaway_id)))
        db.commit()
        return jsonify(ok=True, updated=len(refreshed), item=giveaway_view(db, row, None, False))


@app.post('/api/admin/giveaways/<int:giveaway_id>/finish')
@admin_required
def admin_finish_giveaway(giveaway_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row or row['status'] != 'active':
            return error('Активный розыгрыш не найден.', 404)
        db.execute('UPDATE giveaways SET ends_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), giveaway_id))
        finalize_giveaway(db, giveaway_id)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'giveaway_finish_early', str(giveaway_id)))
        db.commit()
        fresh = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        return jsonify(ok=True, item=giveaway_view(db, fresh, None, False))
    finally:
        db.close()


@app.delete('/api/admin/giveaways/<int:giveaway_id>')
@admin_required
def admin_cancel_giveaway(giveaway_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM giveaways WHERE id=?', (giveaway_id,)).fetchone()
        if not row:
            return error('Розыгрыш не найден.', 404)
        if row['status'] == 'completed':
            return error('Завершённый розыгрыш удалить нельзя.', 409)
        entries = db.execute('SELECT user_id,tickets FROM giveaway_entries WHERE giveaway_id=?', (giveaway_id,)).fetchall()
        for entry in entries:
            amount = int(entry['tickets'] or 0)
            if amount > 0:
                db.execute('UPDATE users SET tickets=tickets+? WHERE id=?', (amount, entry['user_id']))
                record_tickets(db, entry['user_id'], amount, 'giveaway_refund', 'giveaway', giveaway_id,
                               f'Возврат билетов за отменённый розыгрыш «{row["title"]}»')
        db.execute("UPDATE giveaways SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE id=?", (giveaway_id,))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'giveaway_cancel', str(giveaway_id)))
        db.commit()
        return jsonify(ok=True, refunded=sum(int(x['tickets'] or 0) for x in entries))
    finally:
        db.close()


def craft_catalog_candidates():
    try:
        gifts = read_catalog().get('gifts', [])
    except (OSError, ValueError, json.JSONDecodeError):
        gifts = []
    items = []
    for gift in gifts:
        try:
            if not gift.get('id') or not gift.get('name'):
                continue
            image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
            if not image:
                continue
            price = ton_to_cents(gift.get('price_ton'))
            if price <= 0:
                continue
            items.append(dict(id=str(gift['id']), name=str(gift['name']), image_url=image, price=price))
        except (ValueError, TypeError, InvalidOperation, KeyError):
            continue
    items.sort(key=lambda x: (x['price'], x['id']))
    return items


def craft_prize_bounds(total_price):
    total_price = max(1, int(total_price or 0))
    return max(1, total_price // 4), max(total_price, total_price * 10)


def craft_preview_for_total(total_price):
    minimum, maximum = craft_prize_bounds(total_price)
    candidates = [g for g in craft_catalog_candidates() if minimum <= g['price'] <= maximum]
    if not candidates:
        return dict(ok=False, min_price=minimum / 100, max_price=maximum / 100, count=0, min_gift=None, max_gift=None)
    return dict(ok=True, min_price=candidates[0]['price'] / 100, max_price=candidates[-1]['price'] / 100, count=len(candidates),
                min_gift=dict(name=candidates[0]['name'], image_url=candidates[0]['image_url'], price_ton=candidates[0]['price'] / 100),
                max_gift=dict(name=candidates[-1]['name'], image_url=candidates[-1]['image_url'], price_ton=candidates[-1]['price'] / 100))


def choose_craft_reward(total_price, candidates):
    """Choose payout class first; catalog density must not bias win probability.

    With both pools present, 60% of crafts return at least the input value.
    Within the winning pool: 80% below x2, 17% x2..x4, 3% x4+.
    Missing bands are renormalized; no nonexistent rewards are fabricated.
    """
    if not candidates or total_price <= 0:
        return None
    wins = [g for g in candidates if g['price'] >= total_price]
    losses = [g for g in candidates if g['price'] < total_price]
    pool = wins if wins and (not losses or secrets.randbelow(100) < 60) else losses
    bands = [(1, 2, 80), (2, 4, 17), (4, 11, 3)] if pool is wins else [(0, .6, 25), (.6, 1, 75)]
    available = [(weight, [g for g in pool if low * total_price <= g['price'] < high * total_price])
                 for low, high, weight in bands]
    available = [(weight, gifts) for weight, gifts in available if gifts]
    roll = secrets.randbelow(sum(weight for weight, _ in available))
    for weight, gifts in available:
        if roll < weight:
            return secrets.choice(gifts)
        roll -= weight


def craft_win_item(row):
    if not row:
        return None
    return dict(id=int(row['id']), user_id=int(row['user_id']), name=str(row['name'] or 'Игрок'), username=str(row['username'] or ''),
                photo_url=str(row['photo_url'] or ''), input_count=int(row['input_count'] or 0), input_total=int(row['input_total'] or 0) / 100,
                reward=dict(name=str(row['reward_name'] or 'Подарок'), image_url=str(row['reward_image'] or ''), price_ton=int(row['reward_price'] or 0) / 100),
                multiplier=round(int(row['reward_price'] or 0) / max(1, int(row['input_total'] or 0)), 4), created_at=row['created_at'])


@app.get('/api/craft/inventory')
@login_required
def craft_inventory():
    with connect() as db:
        purge_expired_inventory(db, session['uid'])
        rows = db.execute('SELECT * FROM inventory WHERE user_id=? AND COALESCE(promo_locked,0)=0 AND COALESCE(promo_wager_target,0)<=COALESCE(promo_wager_progress,0) ORDER BY floor_price DESC,id DESC', (session['uid'],)).fetchall()
    return jsonify(items=[inventory_item(x) for x in rows])


@app.post('/api/craft/preview')
@login_required
def craft_preview():
    data = request.get_json(silent=True) or {}
    slot_ids = data.get('slot_ids') or []
    if not isinstance(slot_ids, list):
        return error('Передайте список ячеек.')
    ids, seen = [], set()
    for raw in slot_ids[:10]:
        try:value=int(raw or 0)
        except (TypeError, ValueError):value=0
        if value > 0 and value not in seen:
            seen.add(value); ids.append(value)
    with connect() as db:
        purge_expired_inventory(db, session['uid'])
        rows = db.execute(f"SELECT * FROM inventory WHERE user_id=? AND COALESCE(promo_locked,0)=0 AND COALESCE(promo_wager_target,0)<=COALESCE(promo_wager_progress,0) AND id IN ({','.join('?'*len(ids))})", (session['uid'], *ids)).fetchall() if ids else []
    items = [inventory_item(x) for x in rows]
    total = sum(int(round(float(item['price_ton']) * 100)) for item in items)
    preview = craft_preview_for_total(total) if len(items) >= 3 else dict(ok=False, min_price=0, max_price=0, count=0, min_gift=None, max_gift=None)
    return jsonify(selected=items, total_ton=total / 100, can_craft=len(items) >= 3 and preview.get('ok'), preview=preview)


@app.post('/api/craft/play')
@login_required
def craft_play():
    data = request.get_json(silent=True) or {}
    slot_ids = data.get('slot_ids') or []
    if not isinstance(slot_ids, list):
        return error('Передайте подарки для крафта.')
    ids, seen = [], set()
    for raw in slot_ids[:10]:
        try:value=int(raw or 0)
        except (TypeError, ValueError):value=0
        if value > 0 and value not in seen:
            seen.add(value); ids.append(value)
    if len(ids) < 3:
        return error('Для крафта нужно минимум 3 подарка.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        purge_expired_inventory(db, session['uid'])
        rows = db.execute(f"SELECT * FROM inventory WHERE user_id=? AND COALESCE(promo_locked,0)=0 AND COALESCE(promo_wager_target,0)<=COALESCE(promo_wager_progress,0) AND id IN ({','.join('?'*len(ids))}) ORDER BY id" + (" FOR UPDATE" if DATABASE_URL else ""), (session['uid'], *ids)).fetchall()
        if len(rows) != len(ids):
            return error('Часть подарков недоступна для крафта.')
        total = sum(int(r['floor_price'] or 0) for r in rows)
        minimum, maximum = craft_prize_bounds(total)
        candidates = [g for g in craft_catalog_candidates() if minimum <= g['price'] <= maximum]
        if not candidates:
            return error('Нет подходящих подарков для этого крафта.')
        winner = choose_craft_reward(total, candidates)
        if not winner:
            return error('Не удалось выбрать награду для крафта.')
        for row in rows:
            db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (int(row['id']), session['uid']))
        reward_cur = db.execute('INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,?)', (session['uid'], winner['id'], winner['name'], winner['image_url'], winner['price'], 'craft'))
        reward_id = reward_cur.lastrowid
        multiplier = round((winner['price'] / max(1, total)), 4)
        spin_row = db.execute('INSERT INTO craft_spins(user_id,input_count,input_total,min_price,max_price,reward_name,reward_image,reward_price,reward_multiplier) VALUES(?,?,?,?,?,?,?,?,?) RETURNING id', (session['uid'], len(rows), total, minimum, maximum, winner['name'], winner['image_url'], winner['price'], multiplier)).fetchone()
        spin_id = int(spin_row['id']) if spin_row and spin_row['id'] is not None else None
        record_transaction(db, session['uid'], 'craft_consume', 0, 'craft', spin_id, f'Крафт из {len(rows)} подарков')
        record_transaction(db, session['uid'], 'craft_reward', 0, 'inventory', reward_id, f'Награда за крафт: {winner["name"]}')
        log_event(db, session['uid'], 'craft_play', spin_id=spin_id, input_count=len(rows), input_total=total/100, reward_name=winner['name'], reward_price=winner['price']/100, multiplier=multiplier)
        db.commit()
        selected = [inventory_item(r) for r in rows]
    return jsonify(ok=True, selected=selected, total_ton=total/100, reward=dict(name=winner['name'], image_url=winner['image_url'], price_ton=winner['price']/100, multiplier=multiplier), user=profile(), inventory_item=dict(id=reward_id, gift_id=winner['id'], name=winner['name'], image_url=winner['image_url'], price_ton=winner['price']/100, source='craft'))


@app.get('/api/craft/recent-wins')
@login_required
def craft_recent_wins():
    with connect() as db:
        cutoff, _ = wins_feed_cutoff(db, 'craft')
        day_start = wins_day_start_utc()
        rows = db.execute('SELECT c.*,u.name,u.username,u.photo_url FROM craft_spins c JOIN users u ON u.id=c.user_id WHERE c.created_at>? AND c.reward_price>=c.input_total ORDER BY c.created_at DESC,c.id DESC LIMIT 30', (cutoff,)).fetchall()
        top_cutoff = max(cutoff or '', day_start or '')
        top = db.execute('SELECT c.*,u.name,u.username,u.photo_url FROM craft_spins c JOIN users u ON u.id=c.user_id WHERE c.created_at>? AND c.reward_price>=c.input_total ORDER BY c.reward_price DESC,c.id DESC LIMIT 1', (top_cutoff,)).fetchone()
    return jsonify(items=[craft_win_item(r) for r in rows], top_drop=craft_win_item(top) if top else None)


PUBLIC_BALANCE_KINDS = {
    'game_bet': 'Ставка Mines',
    'game_win_ton': 'Выигрыш Mines',
    'gift_win': 'Подарок из Mines',
    'gift_bet': 'Ставка подарком',
    'gift_bet_lost': 'Подарок проигран в Mines',
    'promo_wager_bet': 'Ставка отыгрышным подарком',
    'promo_wager_progress': 'Прогресс отыгрыша',
    'promo_wager_burn': 'Отыгрышный подарок сгорел',
    'promo_gift_expired': 'Срок подарка истёк',
    'promo_wager_claim': 'Отыгрыш завершён',
    'roll_spin': 'Прокрутка Roll',
    'roll_gift': 'Подарок из Roll',
    'upgrade_bet': 'Ставка Upgrade',
    'upgrade_cashback': 'Утешительный приз Upgrade',
    'gift_sale': 'Продажа подарка',
    'promo_balance': 'Промокод',
    'promo_gift': 'Подарок по промокоду',
    'promo_wager_gift': 'Отыгрышный подарок по промокоду',
    'promo_deposit_bonus': 'Бонус промокода',
    'deposit_promo_bonus': 'Бонус к пополнению',
    'level_balance': 'Награда уровня',
    'freebet_balance': 'Freebet',
    'freebet_gift': 'Подарок Freebet',
    'freebet_wager_gift': 'Отыгрышный подарок Freebet',
    'ton_deposit': 'Пополнение TON',
    'referral_bonus': 'Реферальный бонус',
    'transfer_sent': 'Перевод отправлен',
    'transfer_received': 'Перевод получен',
    'withdrawal_request': 'Запрос на вывод',
    'withdrawal_approved': 'Вывод подтверждён',
    'withdrawal_rejected': 'Вывод отклонён',
}


def public_balance_label(kind):
    if kind in PUBLIC_BALANCE_KINDS:
        return PUBLIC_BALANCE_KINDS[kind]
    if kind.startswith('freebet_'):
        return 'Freebet'
    if kind.startswith('promo_'):
        return 'Промокод / бонус'
    return kind.replace('_', ' ').strip().capitalize() or 'Операция'


@app.get('/api/users/<int:user_id>/profile')
@login_required
def public_user_profile(user_id):
    show_black = black_backgrounds_enabled()
    with connect() as db:
        user_row = db.execute('SELECT id,name,username,photo_url,turnover_cents,created_at,max_drop_override_name,max_drop_override_image,max_drop_override_price,max_drop_override_set_at FROM users WHERE id=?',
                              (user_id,)).fetchone()
        if not user_row:
            return error('Пользователь не найден.', 404)
        level_rows = db.execute('SELECT level,required_turnover FROM levels ORDER BY level').fetchall()
        turnover = int(user_row['turnover_cents'] or 0)
        current_level = max((int(r['level']) for r in level_rows if turnover >= int(r['required_turnover'] or 0)), default=1)
        current_row = next((r for r in level_rows if int(r['level']) == current_level), level_rows[0] if level_rows else None)
        next_row = next((r for r in level_rows if int(r['level']) > current_level), None)
        if not current_row or not next_row:
            level_progress = 100.0
        else:
            start = int(current_row['required_turnover'] or 0)
            finish = int(next_row['required_turnover'] or start)
            level_progress = 100.0 if finish <= start else max(0.0, min(100.0, (turnover-start)*100/(finish-start)))

        override_set_at = parse_datetime_utc(user_row['max_drop_override_set_at']) if user_row['max_drop_override_set_at'] else None
        def drop_is_after_override(value):
            if not override_set_at:
                return True
            try:
                created = parse_datetime_utc(value) if value else None
            except Exception:
                created = None
            return bool(created and created > override_set_at)

        mine_rows = db.execute('''SELECT mines,opened,win_multiplier,rtp_snapshot,state,win_gift_name,win_gift_image,
                                         win_gift_price,win_total,payout,created_at
                                  FROM rounds WHERE user_id=? ORDER BY id DESC''', (user_id,)).fetchall()
        mines_count = len(mine_rows)
        max_mines_x = 0.0
        mines_drop = None
        for row in mine_rows:
            if row['state'] != 'won':
                continue
            try:
                opened_count = len(json.loads(row['opened'] or '[]'))
            except (TypeError, ValueError, json.JSONDecodeError):
                opened_count = 0
            try:
                mult = float(row['win_multiplier']) if row['win_multiplier'] is not None else float(multiplier_for(int(row['mines']), opened_count, row['rtp_snapshot']))
            except (TypeError, ValueError, ArithmeticError):
                mult = 0.0
            max_mines_x = max(max_mines_x, mult)
            gift_price = int(row['win_gift_price'] or 0)
            total = int(row['win_total'] or row['payout'] or 0)
            candidate_price = gift_price or total
            if (show_black or not gift_black_background({'name': row['win_gift_name']})) and drop_is_after_override(row['created_at']) and candidate_price > 0 and (not mines_drop or candidate_price > mines_drop['price_cents']):
                mines_drop = dict(price_cents=candidate_price,
                                  name=row['win_gift_name'] or 'Выигрыш Mines',
                                  image_url=row['win_gift_image'] or '', source='Mines')

        upgrade_rows = db.execute('''SELECT source_price,target_price,target_name,target_image,won,result_json,created_at
                                     FROM upgrade_spins WHERE user_id=? ORDER BY created_at DESC''', (user_id,)).fetchall()
        upgrade_count = len(upgrade_rows)
        max_upgrade_x = 0.0
        upgrade_drop = None
        for row in upgrade_rows:
            source_price = int(row['source_price'] or 0)
            target_price = int(row['target_price'] or 0)
            if int(row['won'] or 0) and source_price > 0:
                max_upgrade_x = max(max_upgrade_x, target_price/source_price)
            if not int(row['won'] or 0) or target_price <= 0:
                continue
            try:
                result = json.loads(row['result_json'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                result = {}
            if isinstance(result, dict) and result.get('reward_type') == 'wager_progress':
                continue
            if (show_black or not gift_black_background({'name': row['target_name']})) and drop_is_after_override(row['created_at']) and (not upgrade_drop or target_price > upgrade_drop['price_cents']):
                upgrade_drop = dict(price_cents=target_price, name=row['target_name'] or 'Подарок Upgrade',
                                    image_url=row['target_image'] or '', source='Upgrade')

        override_drop = None
        override_price = int(user_row['max_drop_override_price'] or 0)
        override_name = str(user_row['max_drop_override_name'] or '').strip()
        if override_price > 0 and override_name and (show_black or not gift_black_background({'name': override_name})):
            override_drop = dict(price_cents=override_price, name=override_name,
                                 image_url=user_row['max_drop_override_image'] or '', source='Профиль')
        max_drop = max((x for x in (override_drop, mines_drop, upgrade_drop) if x),
                       key=lambda x: x['price_cents'], default=None)

    return jsonify(user=dict(id=int(user_row['id']), name=user_row['name'], username=user_row['username'],
                             photo_url=user_row['photo_url'], created_at=user_row['created_at']),
                   level=dict(level=current_level, max_level=len(level_rows), turnover=turnover/100,
                              next_turnover=(int(next_row['required_turnover'])/100 if next_row else None),
                              progress=round(level_progress, 1)),
                   stats=dict(mines_count=mines_count, upgrade_count=upgrade_count,
                              max_mines_x=round(max_mines_x, 4), max_upgrade_x=round(max_upgrade_x, 4)),
                   max_drop=(dict(name=max_drop['name'], image_url=max_drop['image_url'],
                                  price_ton=max_drop['price_cents']/100, source=max_drop['source']) if max_drop else None))


@app.get('/api/users/<int:user_id>/balance-history')
@login_required
def public_user_balance_history(user_id):
    try:
        offset = max(0, min(100000, int(request.args.get('offset', 0))))
    except (TypeError, ValueError):
        offset = 0
    page_size = 30
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        rows = db.execute('''SELECT id,kind,amount,balance_after,reference_type,reference_id,details,created_at
                             FROM transactions
                             WHERE user_id=? AND kind NOT IN ('admin_balance','deposit')
                             ORDER BY id DESC LIMIT ? OFFSET ?''',
                          (user_id, page_size+1, offset)).fetchall()
    items = []
    for row in rows[:page_size]:
        kind = str(row['kind'] or '')
        amount = int(row['amount'] or 0)
        # Internal admin references are never exposed in the public player card.
        details = str(row['details'] or '')
        if row['reference_type'] == 'admin':
            details = ''
        items.append(dict(id=int(row['id']), kind=kind, label=public_balance_label(kind),
                          amount=amount/100,
                          balance_after=(int(row['balance_after'])/100 if row['balance_after'] is not None else None),
                          details=details, created_at=row['created_at']))
    return jsonify(items=items, has_more=len(rows) > page_size, next_offset=offset+len(items))


@app.get('/api/admin/wins-feeds')
@admin_required
def admin_wins_feeds():
    with connect() as db:
        mines_time, mines_id = wins_feed_cutoff(db, 'mines')
        upgrade_time, _ = wins_feed_cutoff(db, 'upgrade')
        craft_time, _ = wins_feed_cutoff(db, 'craft')
        mines = db.execute("SELECT COUNT(*) AS total FROM rounds WHERE state='won' AND COALESCE(bet_type,'ton')<>'promo_gift' AND (id>? OR settled_at>?)", (mines_id,mines_time)).fetchone()['total']
        upgrade = db.execute('SELECT COUNT(*) AS total FROM upgrade_spins WHERE won=1 AND created_at>?', (upgrade_time,)).fetchone()['total']
        craft = db.execute('SELECT COUNT(*) AS total FROM craft_spins WHERE created_at>?', (craft_time,)).fetchone()['total']
    return jsonify(mines=mines,upgrade=upgrade,craft=craft,
                   cleared_at=dict(mines=mines_time or None,upgrade=upgrade_time or None,craft=craft_time or None))


@app.post('/api/admin/wins-feeds/clear')
@admin_required
def admin_clear_wins_feeds():
    mode = str((request.get_json(silent=True) or {}).get('mode') or '')
    if mode not in ('mines','upgrade','craft','both','all'):
        return error('Выберите Мины, Апгрейд, Крафт или все разделы.')
    if mode == 'both':
        kinds = ['mines','upgrade']
    elif mode == 'all':
        kinds = ['mines','upgrade','craft']
    else:
        kinds = [mode]
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        for kind in kinds:
            highest_round = db.execute('SELECT COALESCE(MAX(id),0) AS last_id FROM rounds').fetchone()['last_id'] if kind=='mines' else 0
            db.execute('INSERT INTO wins_feed_clears(kind,cleared_at,max_round_id) VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET cleared_at=excluded.cleared_at,max_round_id=excluded.max_round_id', (kind,timestamp,highest_round))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)', (session['uid'],session['uid'],'wins_feed_clear',mode))
        db.commit()
        return jsonify(ok=True,mode=mode)
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
        purge_expired_inventory(db, session['uid'])
        items = db.execute('SELECT * FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 200',
                           (session['uid'],)).fetchall()
    return jsonify(items=visible_gifts([inventory_item(item) for item in items]))


@app.post('/api/inventory/<int:item_id>/sell')
@login_required
def sell_inventory(item_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        purge_expired_inventory(db, session['uid'])
        item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?',
                          (item_id, session['uid'])).fetchone()
        if not item:
            return error('Подарок не найден или срок его действия истёк.', 404)
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


def telegram_api(method, payload=None, files=None, timeout=(2.5, 8)):
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN не настроен.')
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/{method}'
    try:
        if files:
            data = {}
            for key, value in (payload or {}).items():
                data[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            response = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            response = requests.post(url, json=payload or {}, timeout=timeout)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError('Telegram API недоступен.') from exc
    if not response.ok or not data.get('ok'):
        description = str(data.get('description') or f'HTTP {response.status_code}')
        raise RuntimeError(description[:500])
    return data.get('result')


def bot_username_value():
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    identity = read_document('bot_identity') or {}
    BOT_USERNAME = str(identity.get('username') or '').strip().lstrip('@')
    if not BOT_USERNAME and BOT_TOKEN:
        try:
            info = telegram_api('getMe')
            BOT_USERNAME = str(info.get('username') or '').strip().lstrip('@')
            if BOT_USERNAME:
                save_document('bot_identity', {'username': BOT_USERNAME,
                                                'token_fingerprint': BOT_TOKEN_FINGERPRINT,
                                                'updated_at': datetime.now(timezone.utc).isoformat()})
        except RuntimeError:
            pass
    return BOT_USERNAME


def post_channel_settings():
    data = read_document('post_channel') or {}
    return {
        'chat_id': str(data.get('chat_id') or '').strip(),
        'title': str(data.get('title') or '').strip(),
        'username': str(data.get('username') or '').strip().lstrip('@'),
        'join_url': str(data.get('join_url') or '').strip(),
        'chat_type': str(data.get('chat_type') or '').strip(),
        'saved_at': data.get('saved_at'),
    }


def normalize_channel_id(value):
    value = str(value or '').strip()
    if not value:
        raise ValueError('Введите ID или @username канала.')
    if value.startswith('@'):
        if not re.fullmatch(r'@[A-Za-z0-9_]{5,32}', value):
            raise ValueError('Проверьте @username канала.')
        return value
    if not re.fullmatch(r'-?[0-9]{5,20}', value):
        raise ValueError('ID канала должен быть числом вида -100… или @username.')
    return value


def inspect_post_channel(chat_id, create_invite=True):
    chat = telegram_api('getChat', {'chat_id': chat_id})
    me = telegram_api('getMe')
    member = telegram_api('getChatMember', {'chat_id': chat_id, 'user_id': me['id']})
    status = str(member.get('status') or '')
    if status not in ('administrator', 'creator'):
        raise RuntimeError('Бот должен быть администратором этого канала.')
    if chat.get('type') == 'channel' and status == 'administrator' and member.get('can_post_messages') is False:
        raise RuntimeError('У бота нет права публиковать сообщения в этом канале.')
    username = str(chat.get('username') or '').strip().lstrip('@')
    join_url = f'https://t.me/{username}' if username else str(chat.get('invite_link') or '').strip()
    if not join_url and create_invite:
        try:
            invite = telegram_api('createChatInviteLink', {'chat_id': chat_id, 'name': 'GemDrop Freebet'})
            join_url = str(invite.get('invite_link') or '').strip()
        except RuntimeError:
            join_url = ''
    if create_invite and not join_url:
        raise RuntimeError('Не удалось получить ссылку на канал. Для приватного канала дайте боту право приглашать пользователей.')
    return {
        'chat_id': str(chat.get('id') or chat_id),
        'title': str(chat.get('title') or username or chat_id)[:120],
        'username': username,
        'join_url': join_url,
        'chat_type': str(chat.get('type') or ''),
        'bot_status': status,
    }


def telegram_member_subscribed(user_id, channel=None):
    channel = channel or post_channel_settings()
    chat_id = channel.get('chat_id')
    if not chat_id:
        return False, 'Канал для фрибетов ещё не настроен.'
    try:
        member = telegram_api('getChatMember', {'chat_id': chat_id, 'user_id': int(user_id)})
    except RuntimeError as exc:
        return False, str(exc)
    status = str(member.get('status') or '')
    if status in ('creator', 'administrator', 'member'):
        return True, ''
    if status == 'restricted' and bool(member.get('is_member')):
        return True, ''
    return False, 'Чтобы получить этот фрибет, сначала подпишитесь на канал.'


def telegram_rating_level(user_id):
    try:
        info = telegram_api('getChat', {'chat_id': int(user_id)})
        rating = info.get('rating') or {}
        return int(rating.get('level') or 0)
    except (RuntimeError, ValueError, TypeError):
        return 0


def freebet_link(code):
    username = bot_username_value()
    return f'https://t.me/{username}?start=freebet_{code}' if username else ''


def freebet_reward_text(promo):
    return promo_purpose(promo)


def freebet_keyboard(code, channel=None):
    channel = channel or post_channel_settings()
    rows = []
    if channel.get('join_url'):
        rows.append([{'text': '📢 Подписаться', 'url': channel['join_url'], 'style': 'primary'}])
    rows.append([{'text': '✅ Проверить подписку', 'callback_data': f'freebet_check:{code}', 'style': 'success'}])
    return {'inline_keyboard': rows}


def freebet_play_keyboard():
    markup = miniapp_markup('🎮 Играть')
    if markup:
        markup['inline_keyboard'][0][0]['style'] = 'success'
    return markup


def apply_freebet_reward(db, promo, user_id, freebet_code):
    """Apply a backing promo reward without requiring a Mini App session."""
    reward_type = promo['reward_type']
    inventory_id = None
    components = {}
    if reward_type == 'balance':
        amount = max(0, int(promo['amount'] or 0))
        if amount <= 0:
            raise ValueError('Награда фрибета настроена неверно.')
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
        reward = dict(type='balance', amount=amount/100)
        record_transaction(db, user_id, 'freebet_balance', amount, 'freebet', freebet_code, f'Freebet {freebet_code}')
    elif reward_type == 'gift':
        cur = db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'freebet')",
                         (user_id, promo['gift_id'], promo['gift_name'], promo['gift_image_url'], promo['gift_price']))
        inventory_id = cur.lastrowid
        reward = dict(type='gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'], name=promo['gift_name'],
                                             image_url=promo['gift_image_url'], price_ton=promo['gift_price']/100))
        record_transaction(db, user_id, 'freebet_gift', 0, 'freebet', freebet_code, promo['gift_name'])
    elif reward_type == 'wager_gift':
        multiplier = max(1.0, float(promo['wager_multiplier'] or 1))
        target = max(1, round(int(promo['gift_price']) * multiplier))
        item_expires_at = promo_gift_expiry(promo['gift_expires_days'])
        cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                          promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                          VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?,?)""",
                         (user_id, promo['gift_id'], promo['gift_name'], promo['gift_image_url'], promo['gift_price'],
                          multiplier, target, freebet_code, item_expires_at))
        inventory_id = cur.lastrowid
        reward = dict(type='wager_gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'], name=promo['gift_name'],
                                                   image_url=promo['gift_image_url'], price_ton=promo['gift_price']/100,
                                                   promo_locked=True, wager_multiplier=multiplier,
                                                   wager_target=target/100, wager_progress=0, expires_at=item_expires_at))
        record_transaction(db, user_id, 'freebet_wager_gift', 0, 'freebet', freebet_code,
                           f'{promo["gift_name"]} · X{multiplier:g}')
    elif reward_type == 'multi':
        components = json.loads(promo['reward_json'] or '{}').get('components', {})
        if not components:
            raise ValueError('Награда фрибета настроена неверно.')
        rewards = []
        if 'balance' in components:
            amount = int(components['balance']['amount'])
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
            record_transaction(db, user_id, 'freebet_balance', amount, 'freebet', freebet_code, f'Freebet {freebet_code}')
            rewards.append(dict(type='balance', amount=amount/100))
        for kind in ('gift', 'wager_gift'):
            if kind not in components:
                continue
            comp = components[kind]
            component_expires_at = None
            if kind == 'gift':
                cur = db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'freebet')",
                                 (user_id, comp['gift_id'], comp['gift_name'], comp['image_url'], comp['gift_price']))
            else:
                multiplier = float(comp['wager_multiplier'])
                target = round(int(comp['gift_price']) * multiplier)
                component_expires_at = promo_gift_expiry(comp.get('gift_expires_days'))
                cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                  promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                                  VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?,?)""",
                                 (user_id, comp['gift_id'], comp['gift_name'], comp['image_url'], comp['gift_price'],
                                  multiplier, target, freebet_code, component_expires_at))
            inventory_id = cur.lastrowid
            record_transaction(db, user_id, 'freebet_'+kind, 0, 'freebet', freebet_code, comp['gift_name'])
            rewards.append(dict(type=kind, gift=dict(id=inventory_id, name=comp['gift_name'], image_url=comp['image_url'],
                                                      price_ton=comp['gift_price']/100,
                                                      wager_multiplier=comp.get('wager_multiplier', 0),
                                                      expires_at=component_expires_at)))
        if 'deposit_bonus' in components:
            comp = components['deposit_bonus']
            rewards.append(dict(type='deposit_bonus', code=freebet_code,
                                bonus_percent=float(comp.get('bonus_percent') or 0),
                                bonus_fixed=int(comp.get('bonus_fixed') or 0)/100,
                                min_deposit=int(comp.get('min_deposit') or 0)/100))
        reward = dict(type='multi', rewards=rewards)
    elif reward_type == 'deposit_bonus':
        reward = dict(type='deposit_bonus', code=freebet_code,
                      bonus_percent=float(promo['bonus_percent'] or 0),
                      bonus_fixed=int(promo['bonus_fixed'] or 0)/100,
                      min_deposit=int(promo['min_deposit'] or 0)/100)
    else:
        raise ValueError('Награда фрибета настроена неверно.')
    redemption_type = 'deposit_bonus' if reward_type == 'multi' and 'deposit_bonus' in components else reward_type
    if redemption_type == 'deposit_bonus':
        db.execute("""UPDATE promo_redemptions SET deactivated_at=CURRENT_TIMESTAMP
                      WHERE user_id=? AND reward_type='deposit_bonus' AND code<>?
                      AND consumed_at IS NULL AND deactivated_at IS NULL""", (user_id, freebet_code))
    db.execute('INSERT INTO promo_redemptions(code,user_id,reward_type,amount,inventory_id) VALUES(?,?,?,?,?)',
               (freebet_code, user_id, redemption_type, int(promo['amount'] or 0), inventory_id))
    db.execute('UPDATE promo_codes SET uses_count=uses_count+1 WHERE code=?', (freebet_code,))
    log_event(db, user_id, 'freebet_redeem', code=freebet_code, reward_type=reward_type, reward=reward)
    return reward


def describe_reward_items(reward):
    """Flatten a reward dict into a clear list of lines for the UI and bot message.

    Each item: kind, title, detail, image_url (optional), amount (optional).
    """
    items = []
    if not isinstance(reward, dict):
        return items
    kind = reward.get('type')
    if kind == 'multi':
        for sub in reward.get('rewards') or []:
            items.extend(describe_reward_items(sub))
        return items
    if kind == 'balance':
        amount = float(reward.get('amount') or 0)
        items.append(dict(kind='balance', title=f'+{amount:.2f} TON', detail='Зачислено на игровой баланс',
                          amount=amount))
    elif kind == 'gift':
        gift = reward.get('gift') or {}
        items.append(dict(kind='gift', title=str(gift.get('name') or 'Подарок'),
                          detail=f"Подарок в инвентаре · {float(gift.get('price_ton') or 0):.2f} TON",
                          image_url=gift.get('image_url') or '', amount=float(gift.get('price_ton') or 0)))
    elif kind == 'wager_gift':
        gift = reward.get('gift') or {}
        mult = float(gift.get('wager_multiplier') or 0)
        target = float(gift.get('wager_target') or 0)
        detail = f"Отыгрышный подарок · X{mult:g}"
        if target:
            detail += f' · нужно отыграть {target:.2f} TON'
        items.append(dict(kind='wager_gift', title=str(gift.get('name') or 'Подарок'), detail=detail,
                          image_url=gift.get('image_url') or '', amount=float(gift.get('price_ton') or 0),
                          expires_at=gift.get('expires_at') or ''))
    elif kind == 'deposit_bonus':
        pct = float(reward.get('bonus_percent') or 0)
        fixed = float(reward.get('bonus_fixed') or 0)
        minimum = float(reward.get('min_deposit') or 0)
        value = f'+{pct:g}%' if pct else f'+{fixed:.2f} TON'
        detail = 'Бонус к следующему пополнению'
        if minimum:
            detail += f' от {minimum:.2f} TON'
        items.append(dict(kind='deposit_bonus', title=f'Бонус {value}', detail=detail))
    elif kind == 'tickets':
        qty = int(reward.get('amount') or 0)
        items.append(dict(kind='tickets', title=f'{qty} билет(ов)', detail='Для участия в розыгрышах'))
    return items


def freebet_reward_html(reward, fallback=''):
    items = describe_reward_items(reward)
    if not items:
        return escape(fallback)
    lines = []
    icons = dict(balance='💰', gift='🎁', wager_gift='🔒', deposit_bonus='📈', tickets='🎟')
    for it in items:
        lines.append(f"{icons.get(it['kind'], '•')} <b>{escape(it['title'])}</b> — {escape(it['detail'])}")
    return '\n'.join(lines)


def try_activate_freebet(user_id, code):
    code = str(code or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return {'status': 'invalid', 'text': 'Фрибет не найден.'}
    with connect() as db:
        fb = db.execute('SELECT * FROM freebets WHERE code=?', (code,)).fetchone()
        if not fb or not fb['active']:
            return {'status': 'invalid', 'text': 'Фрибет не найден или отключён.'}
        if fb['expires_at']:
            expires = parse_datetime_utc(fb['expires_at'])
            if expires and expires <= datetime.now(timezone.utc):
                return {'status': 'expired', 'text': 'Срок действия этого фрибета закончился.'}
        if db.execute('SELECT 1 FROM freebet_redemptions WHERE code=? AND user_id=?', (code, user_id)).fetchone():
            return {'status': 'used', 'text': 'Вы уже получили этот фрибет. Награда уже находится в GemDrop.', 'reply_markup': freebet_play_keyboard()}
        if int(fb['max_uses'] or 0) > 0 and int(fb['uses_count'] or 0) >= int(fb['max_uses'] or 0):
            return {'status': 'exhausted', 'text': 'Фрибет закончился — все доступные активации уже получили пользователи.'}
        user = db.execute('SELECT turnover_cents FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            return {'status': 'invalid', 'text': 'Сначала запустите бота заново.'}
        turnover = int(user['turnover_cents'] or 0)
        current_level = level_number(db, turnover)
        if int(fb['min_level'] or 0) and current_level < int(fb['min_level']):
            return {'status': 'condition', 'text': f'Для этого фрибета нужен уровень GemDrop {int(fb["min_level"])} или выше. Ваш уровень: {current_level}.'}
        if int(fb['min_turnover'] or 0) and turnover < int(fb['min_turnover']):
            return {'status': 'condition', 'text': f'Для этого фрибета нужен оборот от {int(fb["min_turnover"])/100:.2f} TON. Ваш оборот: {turnover/100:.2f} TON.'}
        require_subscription = bool(fb['require_subscription'])
        min_tg_level = int(fb['min_telegram_level'] or 0)
    if require_subscription:
        subscribed, reason = telegram_member_subscribed(user_id)
        if not subscribed:
            return {'status': 'subscription', 'text': reason, 'reply_markup': freebet_keyboard(code)}
    if min_tg_level:
        actual = telegram_rating_level(user_id)
        if actual < min_tg_level:
            return {'status': 'condition', 'text': f'Для этого фрибета нужен Telegram Rating level {min_tg_level} или выше. Ваш уровень: {actual}.'}
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        fb = db.execute('SELECT * FROM freebets WHERE code=?' + (' FOR UPDATE' if DATABASE_URL else ''), (code,)).fetchone()
        if not fb or not fb['active']:
            return {'status': 'invalid', 'text': 'Фрибет не найден или отключён.'}
        if db.execute('SELECT 1 FROM freebet_redemptions WHERE code=? AND user_id=?', (code, user_id)).fetchone():
            return {'status': 'used', 'text': 'Вы уже получили этот фрибет. Награда уже находится в GemDrop.', 'reply_markup': freebet_play_keyboard()}
        if int(fb['max_uses'] or 0) > 0 and int(fb['uses_count'] or 0) >= int(fb['max_uses'] or 0):
            return {'status': 'exhausted', 'text': 'Фрибет закончился — все доступные активации уже получили пользователи.'}
        promo = db.execute('SELECT * FROM promo_codes WHERE code=?', (fb['promo_code'],)).fetchone()
        if not promo:
            raise ValueError('Награда фрибета не найдена.')
        reward = apply_freebet_reward(db, promo, user_id, code)
        db.execute('INSERT INTO freebet_redemptions(code,user_id,reward_json) VALUES(?,?,?)',
                   (code, user_id, json.dumps(reward, ensure_ascii=False)))
        db.execute('UPDATE freebets SET uses_count=uses_count+1 WHERE code=?', (code,))
        db.commit()
        return {'status': 'ok', 'reward': reward,
                'text': f'🎁 <b>Фрибет активирован!</b>\n\n<b>Вы получили:</b>\n{freebet_reward_html(reward, freebet_reward_text(promo))}\n\nНаграда уже зачислена в GemDrop. Откройте приложение и нажмите «Забрать».',
                'reply_markup': freebet_play_keyboard(), 'parse_mode': 'HTML'}
    except Exception:
        try:
            db.connection.rollback() if DATABASE_URL else db.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        db.close()


def custom_emoji_html(text):
    text = str(text or '')
    pattern = re.compile(r'\[emoji:([0-9]{5,30}):([^\]\r\n]{1,16})\]')
    return pattern.sub(lambda m: f'<tg-emoji emoji-id="{m.group(1)}">{escape(m.group(2))}</tg-emoji>', text)



EMOJI_NOTICE = ('Premium владельца бота разрешает Bot API использовать custom emoji в личных чатах, группах и супергруппах. '
                'Для сообщений именно в каналах Telegram по-прежнему требует, чтобы бот имел дополнительное имя, приобретённое через Fragment.')

CUSTOM_EMOJI_TAG_RE = re.compile(r'<tg-emoji\s+emoji-id=["\']([0-9]{5,30})["\']>(.*?)</tg-emoji>', re.S | re.I)


def remember_emojis(items):
    # Never invent a fake fallback for a custom emoji. Telegram requires the text
    # wrapped by the entity to match the sticker's own regular emoji exactly.
    for item in items:
        eid = str(item.get('id') or '')
        if re.fullmatch(r'[0-9]{5,30}', eid):
            previous = read_document('saved_emoji:' + eid) or {}
            fallback = str(item.get('emoji') or previous.get('emoji') or '')[:32]
            save_document('saved_emoji:' + eid, dict(id=eid, emoji=fallback))


def fetch_custom_emoji_map(ids):
    unique = []
    seen = set()
    for value in ids or []:
        eid = str(value or '').strip()
        if eid and re.fullmatch(r'[0-9]{5,30}', eid) and eid not in seen:
            seen.add(eid); unique.append(eid)
    result = {}
    for offset in range(0, len(unique), 200):
        batch = unique[offset:offset + 200]
        stickers = telegram_api('getCustomEmojiStickers', {'custom_emoji_ids': batch}) or []
        for sticker in stickers:
            eid = str(sticker.get('custom_emoji_id') or '')
            if eid:
                result[eid] = dict(id=eid, emoji=str(sticker.get('emoji') or ''),
                                   set_name=str(sticker.get('set_name') or ''))
    return result


def resolve_post_custom_emojis(text, rows=None):
    """Validate IDs and replace every fallback with Telegram's canonical emoji.

    This prevents Telegram from silently dropping a custom_emoji entity when a
    stored ID was paired with the wrong visible fallback character.
    """
    html = custom_emoji_html(text)
    ids = [m.group(1) for m in CUSTOM_EMOJI_TAG_RE.finditer(html)]
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list):
            for button in row:
                if isinstance(button, dict):
                    eid = str(button.get('icon_custom_emoji_id') or '').strip()
                    if eid:
                        ids.append(eid)
    if not ids:
        return html, {}
    resolved = fetch_custom_emoji_map(ids)
    missing = sorted(set(ids) - set(resolved))
    if missing:
        preview = ', '.join(missing[:5])
        if len(missing) > 5:
            preview += f' и ещё {len(missing) - 5}'
        raise ValueError('Telegram не нашёл custom emoji ID: ' + preview)
    remember_emojis(resolved.values())
    def repl(match):
        eid = match.group(1)
        fallback = resolved[eid].get('emoji') or ''
        if not fallback:
            raise ValueError('Telegram не вернул обычный emoji для ID ' + eid)
        return f'<tg-emoji emoji-id="{eid}">{escape(fallback)}</tg-emoji>'
    return CUSTOM_EMOJI_TAG_RE.sub(repl, html), resolved


def rich_custom_emoji_html(text):
    """Return Rich HTML using the same official <tg-emoji> syntax as /start.

    Bot API 10.3 accepts <tg-emoji> in Rich HTML as well as the tg://emoji
    image form. Keeping the exact same representation as the proven /start
    path avoids needless differences between bot greetings and publications.
    """
    return str(text or '')


def emojis_in_post(text, rows=None):
    items = [dict(id=m.group(1), emoji=unescape(m.group(2))) for m in
             re.finditer(r'<tg-emoji\s+emoji-id=["\']([0-9]{5,30})["\']>(.*?)</tg-emoji>', custom_emoji_html(text), re.S)]
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list):
            continue
        for button in row:
            if not isinstance(button, dict):
                continue
            eid = str(button.get('icon_custom_emoji_id') or '')
            if eid:
                items.append(dict(id=eid))
    return items


def telegram_message_html(message):
    """Preserve Telegram formatting and custom emoji using UTF-16 offsets."""
    text = str(message.get('text') or message.get('caption') or '')
    entities = message.get('entities') if message.get('text') else message.get('caption_entities')
    raw = text.encode('utf-16-le')
    spans = []
    emojis = []
    tags = {'bold': 'b', 'italic': 'i', 'underline': 'u', 'strikethrough': 's',
            'spoiler': 'tg-spoiler', 'code': 'code', 'pre': 'pre', 'blockquote': 'blockquote'}
    for e in entities or []:
        a, b = int(e.get('offset', 0)), int(e.get('offset', 0)) + int(e.get('length', 0))
        if a < 0 or b <= a or b * 2 > len(raw):
            continue
        kind = e.get('type')
        if kind == 'custom_emoji' and re.fullmatch(r'[0-9]{5,30}', str(e.get('custom_emoji_id') or '')):
            eid = str(e['custom_emoji_id'])
            fallback = raw[a*2:b*2].decode('utf-16-le')
            emojis.append(dict(id=eid, emoji=fallback))
            start, end = f'<tg-emoji emoji-id="{eid}">', '</tg-emoji>'
        elif kind == 'text_link' and re.match(r'^(https?://|tg://|mailto:)', str(e.get('url') or ''), re.I):
            start, end = '<a href="' + escape(e['url'], quote=True) + '">', '</a>'
        elif kind == 'text_mention' and isinstance((e.get('user') or {}).get('id'), int):
            start, end = f'<a href="tg://user?id={e["user"]["id"]}">', '</a>'
        elif kind == 'expandable_blockquote':
            start, end = '<blockquote expandable>', '</blockquote>'
        elif kind in tags:
            tag = tags[kind]
            start, end = f'<{tag}>', f'</{tag}>'
        else:
            continue
        spans.append((a, b, start, end))
    spans.sort(key=lambda x: (x[0], -x[1]))
    # Telegram entities are nested or disjoint; render recursively.
    def render(a, b, nodes):
        parts, cursor, i = [], a, 0
        while i < len(nodes):
            node = nodes[i]; na, nb, opening, closing = node
            if na < cursor or nb > b:
                i += 1; continue
            parts.append(escape(raw[cursor*2:na*2].decode('utf-16-le')))
            j = i + 1
            while j < len(nodes) and nodes[j][0] < nb and nodes[j][1] <= nb:
                j += 1
            parts.append(opening + render(na, nb, nodes[i+1:j]) + closing)
            cursor, i = nb, j
        parts.append(escape(raw[cursor*2:b*2].decode('utf-16-le')))
        return ''.join(parts)
    sticker = message.get('sticker') or {}
    if sticker.get('custom_emoji_id'):
        emojis.append(dict(id=str(sticker['custom_emoji_id']), emoji=sticker.get('emoji') or '⭐'))
    return render(0, len(raw)//2, spans), emojis


class GreetingHTML(HTMLParser):
    allowed = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'a',
               'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji', 'span'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.visible = [], ''
    def handle_starttag(self, tag, attrs):
        if tag not in self.allowed:
            raise ValueError('Неподдерживаемый HTML-тег: ' + tag)
        attrs = dict(attrs)
        allowed_attrs = {'a': {'href'}, 'tg-emoji': {'emoji-id'}, 'span': {'class'},
                         'blockquote': {'expandable'}, 'code': {'class'}}.get(tag, set())
        if set(attrs) - allowed_attrs:
            raise ValueError('Неподдерживаемые атрибуты: ' + tag)
        if tag == 'tg-emoji' and not re.fullmatch(r'[0-9]{5,30}', attrs.get('emoji-id') or ''):
            raise ValueError('Укажите корректный emoji-id.')
        if tag == 'a' and not re.match(r'^(https?://|tg://|mailto:)', attrs.get('href') or '', re.I):
            raise ValueError('Некорректная ссылка в приветствии.')
        if tag == 'span' and attrs.get('class') != 'tg-spoiler':
            raise ValueError('Разрешён только span с class="tg-spoiler".')
        self.stack.append(tag)
    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError('Проверьте закрывающие HTML-теги приветствия.')
    def handle_data(self, data):
        self.visible += data


def validate_greeting(text):
    parser = GreetingHTML()
    parser.feed(custom_emoji_html(text).replace('{referral_percent}', '100'))
    parser.close()
    if parser.stack:
        raise ValueError('Закройте все HTML-теги приветствия.')
    if text and not parser.visible.strip():
        raise ValueError('Приветствие должно содержать текст.')
    if len(parser.visible.encode('utf-16-le')) // 2 > 4096:
        raise ValueError('Приветствие слишком длинное: максимум 4096 символов.')


def normalize_start_buttons(raw):
    """Validate the editable /start inline keyboard and keep an editor-friendly shape."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('Кнопки /start должны быть переданы рядами.')
    rows, total = [], 0
    for raw_row in raw[:10]:
        if not isinstance(raw_row, list):
            continue
        row = []
        for item in raw_row[:6]:
            if not isinstance(item, dict):
                continue
            text = str(item.get('text') or '').strip()[:64]
            if not text:
                continue
            kind = str(item.get('type') or 'web_app').strip().lower()
            value = str(item.get('value') or '').strip()
            style = str(item.get('style') or '').strip().lower()
            icon = str(item.get('icon_custom_emoji_id') or '').strip()
            if style not in ('', 'primary', 'success', 'danger'):
                raise ValueError(f'Некорректный стиль кнопки «{text}».')
            if icon and not re.fullmatch(r'[0-9]{5,30}', icon):
                raise ValueError(f'Некорректный ID premium emoji у кнопки «{text}».')
            if kind == 'web_app':
                if value and not re.match(r'^https://', value, re.I):
                    raise ValueError(f'Web App URL кнопки «{text}» должен начинаться с https://.')
                value = value[:2048]
            elif kind == 'url':
                if not re.match(r'^(https?://|tg://)', value, re.I):
                    raise ValueError(f'У кнопки «{text}» должна быть ссылка http(s):// или tg://.')
                value = value[:2048]
            elif kind == 'copy':
                if not value:
                    raise ValueError(f'У кнопки «{text}» нет текста для копирования.')
                value = value[:256]
            elif kind == 'callback':
                if not value:
                    raise ValueError(f'У кнопки «{text}» нет callback.')
                callback_value = value if value.startswith('start:') else 'start:' + value
                if len(callback_value.encode('utf-8')) > 64:
                    raise ValueError(f'Callback кнопки «{text}» слишком длинный.')
                value = value[:58]
            else:
                raise ValueError(f'Неизвестный тип кнопки «{text}».')
            clean = {'text': text, 'type': kind, 'value': value}
            if style:
                clean['style'] = style
            if icon:
                clean['icon_custom_emoji_id'] = icon
            row.append(clean)
            total += 1
            if total >= 36:
                break
        if row:
            rows.append(row)
        if total >= 36:
            break
    return rows


def start_buttons_for_api(settings):
    rows = settings.get('buttons') if isinstance(settings, dict) else None
    if isinstance(rows, list):
        return rows
    return [[{'text': '🎮 Играть', 'type': 'web_app', 'value': '', 'style': 'primary'}]]


def build_start_keyboard(uid, referrer=None):
    settings = read_document('bot_settings') or {}
    editor_rows = start_buttons_for_api(settings)
    play_url = WEBAPP_URL + ('/?ref=' + str(referrer) if referrer else '/')
    inline = []
    for row in editor_rows:
        built = []
        for item in row if isinstance(row, list) else []:
            if not isinstance(item, dict):
                continue
            text = str(item.get('text') or '').strip()[:64]
            if not text:
                continue
            kind = str(item.get('type') or 'web_app').lower()
            value = str(item.get('value') or '').strip()
            button = {'text': text}
            style = str(item.get('style') or '').lower()
            if style in ('primary', 'success', 'danger'):
                button['style'] = style
            icon = str(item.get('icon_custom_emoji_id') or '').strip()
            if re.fullmatch(r'[0-9]{5,30}', icon):
                button['icon_custom_emoji_id'] = icon
            if kind == 'web_app':
                target = value or play_url
                target = target.replace('{webapp_url}', play_url)
                if not target.startswith('https://'):
                    target = play_url
                button['web_app'] = {'url': target[:2048]}
            elif kind == 'url':
                target = value.replace('{webapp_url}', play_url)
                if not re.match(r'^(https?://|tg://)', target, re.I):
                    continue
                button['url'] = target[:2048]
            elif kind == 'copy':
                if not value:
                    continue
                button['copy_text'] = {'text': value[:256]}
            elif kind == 'callback':
                callback_value = value if value.startswith('start:') else 'start:' + value
                if not value or len(callback_value.encode('utf-8')) > 64:
                    continue
                button['callback_data'] = callback_value
            else:
                continue
            built.append(button)
        if built:
            inline.append(built)
    if not inline:
        inline = [[{'text': '🎮 Играть', 'web_app': {'url': play_url}, 'style': 'primary'}]]
    if uid in ADMIN_IDS:
        inline.append([{'text': 'Определить ID эмодзи', 'callback_data': 'admin:emoji'}])
        inline.append([{'text': 'Импортировать пост', 'callback_data': 'admin:post'}])
    return {'inline_keyboard': inline}


@app.route('/api/admin/bot/settings', methods=['GET', 'POST'])
@admin_required
def admin_bot_settings():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        current = read_document('bot_settings') or {}
        text = str(data.get('welcome_text', current.get('welcome_text', '')) or '').strip()
        if len(text) > 16000:
            return error('Приветствие слишком длинное.')
        try:
            validate_greeting(text)
            buttons = normalize_start_buttons(data.get('buttons', start_buttons_for_api(current)))
        except ValueError as exc:
            return error(str(exc))
        save_document('bot_settings', dict(welcome_text=text, buttons=buttons))
        remember_emojis(emojis_in_post(text, buttons))
    data = read_document('bot_settings') or {}
    return jsonify(ok=True, welcome_text=data.get('welcome_text', ''),
                   effective_text=welcome_text(), buttons=start_buttons_for_api(data), emoji_notice=EMOJI_NOTICE)


@app.get('/api/admin/emojis')
@admin_required
def admin_saved_emojis():
    with connect() as db:
        rows = db.execute("SELECT payload FROM app_documents WHERE name LIKE 'saved_emoji:%' ORDER BY name").fetchall()
    items = [json.loads(r['payload']) for r in rows]
    # Refresh canonical fallbacks from Telegram so old entries that were once
    # saved as a generic star become safe to insert into a message.
    if items and BOT_TOKEN:
        try:
            resolved = fetch_custom_emoji_map([x.get('id') for x in items])
            if resolved:
                remember_emojis(resolved.values())
                items = [resolved.get(str(x.get('id')), x) for x in items]
        except RuntimeError:
            pass
    return jsonify(items=items, notice=EMOJI_NOTICE)


@app.post('/api/admin/emojis')
@admin_required
def admin_add_emoji():
    data = request.get_json(silent=True) or {}
    eid = str(data.get('id') or '').strip()
    if not re.fullmatch(r'[0-9]{5,30}', eid):
        return error('Введите корректный ID эмодзи.')
    try:
        stickers = telegram_api('getCustomEmojiStickers', {'custom_emoji_ids': [eid]}) or []
        if not stickers:
            return error('Telegram не нашёл эмодзи с таким ID.')
        item = dict(id=eid, emoji=stickers[0].get('emoji') or '⭐')
        remember_emojis([item])
        return jsonify(ok=True, item=item)
    except RuntimeError as exc:
        return error(str(exc), 409)


@app.route('/api/admin/post/draft', methods=['GET', 'POST'])
@admin_required
def admin_post_draft():
    key = 'post_draft:' + str(session['uid'])
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '')
        rows = data.get('buttons', [])
        if len(text) > 30000 or not isinstance(rows, list) or len(json.dumps(rows)) > 50000:
            return error('Черновик слишком большой.')
        # Allow incomplete button URLs while editing, but validate structure.
        if any(not isinstance(row, list) or any(not isinstance(b, dict) for b in row) for row in rows):
            return error('Некорректные кнопки.')
        draft = dict(text=text, buttons=rows, image=str(data.get('image') or '')[:20000],
                     silent=bool(data.get('silent')), protect=bool(data.get('protect')))
        save_document(key, draft)
        remember_emojis(emojis_in_post(text, rows))
    return jsonify(draft=read_document(key) or {})


def emoji_admin_keyboard():
    return {'inline_keyboard': [[{'text': 'Определить ещё', 'callback_data': 'admin:emoji'}],
                                [{'text': 'Импортировать пост', 'callback_data': 'admin:post'}]]}


def handle_admin_emoji_message(message):
    uid = (message.get('from') or {}).get('id')
    if uid not in ADMIN_IDS or (message.get('chat') or {}).get('type') != 'private':
        return None
    command = str(message.get('text') or '').split(maxsplit=1)
    command = command[0].split('@')[0].lower() if command else ''
    if command in ('/emoji', '/post', '/cancel'):
        mode = {'/emoji': 'emoji', '/post': 'post', '/cancel': ''}[command]
        save_document('bot_input:' + str(uid), {'mode': mode})
        return dict(method='sendMessage', chat_id=uid,
                    text=('Отправьте или перешлите пост с premium emoji. Его текст и форматирование появятся в редакторе Post. Текущий черновик будет заменён.' if mode == 'post' else
                          'Пришлите premium emoji, сообщение с ними или custom emoji стикер. Для выхода: /cancel.' if mode else 'Готово. Режим ввода закрыт.'))
    if command.startswith('/'):
        return None
    html, emojis = telegram_message_html(message)
    mode = (read_document('bot_input:' + str(uid)) or {}).get('mode')
    if not mode and not emojis:
        return None
    remember_emojis(emojis)
    if mode == 'post':
        if not html.strip() and not message.get('photo'):
            return dict(method='sendMessage', chat_id=uid, text='Пришлите текст поста или фото с подписью.')
        photo = (message.get('photo') or [{}])[-1].get('file_id', '')
        save_document('post_draft:' + str(uid), dict(text=html, buttons=[], image=photo, silent=False, protect=False))
        save_document('bot_input:' + str(uid), {'mode': ''})
        text = 'Пост сохранён в черновик. Откройте Post → «Загрузить сохранённый». Альбом присылайте по одному фото; остальные фото можно добавить в редакторе.'
    elif emojis:
        unique = {e['id']: e for e in emojis}
        text = '<b>ID эмодзи сохранены</b>\n\n' + '\n'.join(escape(e['emoji']) + ' — <code>' + e['id'] + '</code>' for e in list(unique.values())[:40])
        if len(unique) > 40:
            text += '\nВсе остальные ID также сохранены в каталоге сайта.'
        text += '\n\nОни доступны в разделе Post, в тексте и на кнопках.'
    else:
        text = 'Custom emoji не найдены. У обычных Unicode-эмодзи нет Telegram custom emoji ID. Пришлите именно premium emoji или перешлите исходное сообщение.'
    return dict(method='sendMessage', chat_id=uid, text=text, parse_mode='HTML', reply_markup=emoji_admin_keyboard())

def normalize_post_buttons(raw):
    if not isinstance(raw, list):
        return []
    rows = []
    total = 0
    for raw_row in raw[:12]:
        if not isinstance(raw_row, list):
            continue
        row = []
        for item in raw_row[:8]:
            if not isinstance(item, dict):
                continue
            text = str(item.get('text') or '').strip()[:64]
            if not text:
                continue
            button = {'text': text}
            style = str(item.get('style') or '').lower()
            if style in ('primary', 'success', 'danger'):
                button['style'] = style
            icon = str(item.get('icon_custom_emoji_id') or '').strip()
            if icon and not re.fullmatch(r'[0-9]{5,30}', icon):
                raise ValueError('Некорректный ID эмодзи на кнопке.')
            if icon:
                button['icon_custom_emoji_id'] = icon
            kind = str(item.get('type') or 'url')
            value = str(item.get('value') or '').strip()
            if kind == 'url':
                if not re.match(r'^(https?://|tg://)', value, re.I):
                    raise ValueError(f'У кнопки «{text}» должна быть ссылка http(s):// или tg://.')
                button['url'] = value[:2048]
            elif kind == 'copy':
                if not value:
                    raise ValueError(f'У кнопки «{text}» нет текста для копирования.')
                button['copy_text'] = {'text': value[:256]}
            elif kind == 'callback':
                callback_value = value if value.startswith('post:') else 'post:' + value
                if not value or len(callback_value.encode('utf-8')) > 64:
                    raise ValueError(f'Callback кнопки «{text}» должен занимать не более 64 байт вместе с префиксом.')
                button['callback_data'] = callback_value
            else:
                raise ValueError('Неизвестный тип кнопки.')
            row.append(button)
            total += 1
            if total >= 48:
                break
        if row:
            rows.append(row)
        if total >= 48:
            break
    return rows


def notification_premium_html(text):
    """Use the saved Telegram emoji catalogue; keep a normal emoji fallback."""
    text = str(text)
    if '<tg-emoji' in text: return text
    with connect() as db:
        rows = db.execute("SELECT payload FROM app_documents WHERE name LIKE 'saved_emoji:%' ORDER BY name").fetchall()
    emojis = {}
    for row in rows:
        try:
            item = json.loads(row['payload'])
            eid, emoji = str(item.get('id') or ''), str(item.get('emoji') or '')
            if re.fullmatch(r'[0-9]{5,30}', eid) and emoji and len(emoji) <= 16 and any(ord(c) >= 0x2000 for c in emoji):
                emojis.setdefault(escape(emoji), eid)
        except (ValueError, TypeError, AttributeError):
            continue
    if not emojis: return text
    pattern = re.compile('|'.join(re.escape(x) for x in sorted(emojis, key=len, reverse=True)))
    parts = re.split(r'(<[^>]+>)', text)
    for index in range(0, len(parts), 2):
        parts[index] = pattern.sub(lambda m: f'<tg-emoji emoji-id="{emojis[m.group(0)]}">{m.group(0)}</tg-emoji>', parts[index])
    decorated = ''.join(parts)
    return decorated if len(decorated) <= 4096 else text


def send_user_notification(user_id, text, reply_markup=None, parse_mode=None):
    if not BOT_TOKEN:
        return False
    plain_text = str(text)
    payload = {'chat_id': int(user_id), 'text': notification_premium_html(plain_text) if parse_mode == 'HTML' else plain_text}
    payload['link_preview_options'] = {'is_disabled': True}
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
                return True
        except (requests.RequestException, ValueError, TypeError):
            if attempt == 0:
                # If Telegram rejects premium entities, the notification still arrives.
                payload['text'] = plain_text
                time.sleep(.08)
    app.logger.warning('Could not deliver notification to %s', user_id)
    return False


def deliver_activity_notifications():
    if not BOT_TOKEN: return
    # Claim committed rows before network I/O; separate workers cannot send the same row.
    for _ in range(20):
        now=int(time.time())
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE user_notifications SET delivery_state='none' WHERE delivery_state IN ('pending','sending') AND NOT ("+IMPORTANT_NOTIFICATION_SQL+')',IMPORTANT_NOTIFICATION_KINDS)
            db.execute("UPDATE user_notifications SET delivery_state='pending' WHERE delivery_state='sending' AND delivery_next_at<?",(now-60,))
            row=db.execute("SELECT * FROM user_notifications WHERE delivery_state='pending' AND delivery_next_at<=? ORDER BY id LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if DATABASE_URL else ''),(now,)).fetchone()
            if not row: return
            db.execute("UPDATE user_notifications SET delivery_state='sending',delivery_next_at=? WHERE id=?",(now,row['id']))
            db.commit()
        actions = {'gift_sale':'profile','gift_win':'profile','admin_gift_add':'profile',
                   'admin_gift_remove':'profile','transfer_sent':'profile','transfer_received':'profile',
                   'promo_issued':'bonuses','level_claim':'levels','reward_task_claim':'giveaways',
                   'giveaway_enter':'giveaways','upgrade':'profile'}
        markup = miniapp_markup('Открыть розыгрыш', f'giveaways&giveaway={row["giveaway_id"]}') if row['kind'] == 'giveaway_started' else miniapp_markup('Открыть', actions.get(row['kind'], ''))
        heading, separator, body = str(row['text']).partition('\n')
        formatted = '<b>' + escape(heading) + '</b>' + (separator + escape(body) if separator else '')
        if row['kind'] == 'giveaway_started':
            formatted = re.sub(r'(?m)^https://t\.me/nft/([A-Za-z0-9-]+)$',
                               lambda m: f'<a href="{m.group(0)}">🔗 Посмотреть подарок</a>', formatted)
        ok=send_user_notification(row['user_id'],formatted,markup,'HTML')
        attempts=int(row['delivery_attempts'])+1
        with connect() as db:
            db.execute('UPDATE user_notifications SET delivery_state=?,delivery_attempts=?,delivery_next_at=? WHERE id=?',
                       ('sent' if ok else 'failed' if attempts>=3 else 'pending',attempts,now+30*attempts,row['id']))


def activity_notification_loop():
    while True:
        try:
            deliver_notification_outbox()
            deliver_activity_notifications()
        except Exception:
            app.logger.exception('Activity notification delivery failed')
        NOTIFY_WAKE.wait(1.5)
        if NOTIFY_WAKE.is_set():
            time.sleep(.2)
            NOTIFY_WAKE.clear()


def notify_user_async(user_id, text, reply_markup=None, parse_mode=None, db=None):
    if not BOT_TOKEN:
        return
    # Durable queue instead of creating one OS thread per notification.
    payload = json.dumps(dict(text=str(text), reply_markup=reply_markup,
                              parse_mode=parse_mode), ensure_ascii=False)
    params = (int(user_id), payload, int(time.time()))
    if db is not None:
        db.execute('INSERT INTO notification_outbox(user_id,payload,created_at) VALUES(?,?,?)', params)
    else:
        with connect() as queue_db:
            queue_db.execute('INSERT INTO notification_outbox(user_id,payload,created_at) VALUES(?,?,?)', params)
    NOTIFY_WAKE.set()


def deliver_notification_outbox():
    for _ in range(10):
        now = int(time.time())
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE notification_outbox SET state='pending' WHERE state='sending' AND next_at<?", (now-60,))
            row = db.execute("SELECT * FROM notification_outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT 1"
                             + (' FOR UPDATE SKIP LOCKED' if DATABASE_URL else ''), (now,)).fetchone()
            if not row:
                return
            db.execute("UPDATE notification_outbox SET state='sending',next_at=? WHERE id=?", (now, row['id']))
        payload = json.loads(row['payload'])
        ok = send_user_notification(row['user_id'], **payload)
        attempts = int(row['attempts']) + 1
        with connect() as db:
            db.execute('UPDATE notification_outbox SET state=?,attempts=?,next_at=? WHERE id=?',
                       ('sent' if ok else 'failed' if attempts >= 3 else 'pending',
                        attempts, now + 30 * attempts, row['id']))


def miniapp_markup(text, action=''):
    if not WEBAPP_URL.startswith('https://'):
        return None
    url = WEBAPP_URL + ('/?open=' + action if action else '/')
    return {'inline_keyboard': [[{'text': text, 'web_app': {'url': url}}]]}


def format_ton_cents(cents):
    return f'{int(cents) / 100:.2f}'


def deposit_notification_text(amount_cents, balance_cents, bonus_cents=0):
    bonus_line = f'\n🎁 Бонус: +{format_ton_cents(bonus_cents)} TON' if bonus_cents else ''
    return (f'✅ Ваш баланс пополнен на {format_ton_cents(amount_cents)} TON.'
            f'{bonus_line}\n\nТекущий баланс: {format_ton_cents(balance_cents)} TON')


def notify_deposit_async(user_id, amount_cents, balance_cents, bonus_cents=0):
    bonus_line = f'\n🎁 Бонус: <b>+{format_ton_cents(bonus_cents)} TON</b>' if bonus_cents else ''
    text = (f'✅ <b>Ваш баланс пополнен на {format_ton_cents(amount_cents)} TON.</b>'
            f'{bonus_line}\n\nТекущий баланс: <b>{format_ton_cents(balance_cents)} TON</b>')
    notify_user_async(user_id, text, miniapp_markup('Открыть'), 'HTML')


def notify_promo_async(user_id, code, action='bonuses'):
    safe_code = escape(str(code))
    text = f'🎟 <b>Вам выдан промокод</b>\n\n<code>{safe_code}</code>\n\nОткройте GemDrop, чтобы забрать награду.'
    notify_user_async(user_id, text, miniapp_markup('🎁 Забрать', action), 'HTML')


def notify_giveaway_wins_async(user_id, giveaway_title, winnings, db=None):
    """Send one Telegram message for all places won in a single giveaway."""
    if not winnings:
        return
    lines = [f'🏆 <b>Вы выиграли в розыгрыше «{escape(str(giveaway_title))}»!</b>', '']
    for win in winnings[:20]:
        price = int(win.get('price_cents') or 0)
        price_text = f' · {format_ton_cents(price)} TON' if price else ''
        lines.append(f'#{int(win.get("rank") or 0)} — <b>{escape(str(win.get("name") or "Подарок"))}</b>{price_text}')
    if len(winnings) > 20:
        lines.append(f'…и ещё {len(winnings)-20} приз(ов).')
    lines.extend(['', 'Награда уже добавлена в ваш инвентарь GemDrop.'])
    keyboard = []
    if WEBAPP_URL.startswith('https://'):
        keyboard.append([{'text': '🎁 Открыть инвентарь', 'web_app': {'url': WEBAPP_URL + '/?open=profile'}}])
    seen = set()
    for win in winnings:
        url = str(win.get('fragment_url') or '')
        if not re.match(r'^https://(?:t\.me/nft/|(?:www\.)?fragment\.com/gift/)', url, re.I) or url in seen:
            continue
        seen.add(url)
        label = f'🔗 Fragment #{win.get("fragment_number")}' if win.get('fragment_number') else '🔗 Открыть во Fragment'
        keyboard.append([{'text': label[:64], 'url': url}])
        if len(keyboard) >= 8:
            break
    markup = {'inline_keyboard': keyboard} if keyboard else None
    notify_user_async(user_id, '\n'.join(lines), markup, 'HTML', db=db)


def notify_level_up_async(user_id, level):
    # Level rewards stay visible in the app, without a bot message for routine play.
    return None


@app.post('/api/inventory/<int:item_id>/withdraw')
@login_required
def request_withdrawal(item_id):
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        purge_expired_inventory(db, session['uid'])
        account = db.execute('SELECT withdrawal_enabled,withdrawal_block_reason FROM users WHERE id=?',
                             (session['uid'],)).fetchone()
        if not account:
            return error('Пользователь не найден.', 404)
        if not bool(account['withdrawal_enabled']):
            reason = str(account['withdrawal_block_reason'] or '').strip()
            return error(reason or 'Вывод для вашего аккаунта временно недоступен. Обратитесь в поддержку.', 403)
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
        purge_expired_inventory(db, session['uid'])
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
                      promo_wager_progress=0,promo_code='',expires_at=NULL,source='promo_claimed'
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
    # Intentionally public: loader and visible navigation are needed before auth finishes.
    return jsonify(loader_gif=loader_settings()['path'], sections=section_settings(),
                   black_backgrounds_enabled=black_backgrounds_enabled())


@app.get('/api/admin/section-settings')
@admin_required
def admin_section_settings():
    return jsonify(sections=section_settings(), black_backgrounds_enabled=black_backgrounds_enabled())


@app.post('/api/admin/section-settings')
@admin_required
def save_admin_section_settings():
    data = request.get_json(silent=True) or {}
    current = section_settings()
    if 'black_backgrounds_enabled' in data and not isinstance(data['black_backgrounds_enabled'], bool):
        return error('Состояние отображения фонов должно быть true или false.')
    if any(not isinstance(data[key], bool) for key in current if key in data):
        return error('Состояние раздела должно быть true или false.')
    updated = {key: bool(data.get(key, current[key])) for key in current}
    if not any(updated.values()):
        return error('Нужно оставить включённым хотя бы один раздел.')
    save_document('section_settings', updated)
    if 'black_backgrounds_enabled' in data:
        save_document('gift_display_settings', {'black_backgrounds_enabled': data['black_backgrounds_enabled']})
    return jsonify(ok=True, sections=updated, black_backgrounds_enabled=black_backgrounds_enabled())


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
        depositors = db.execute('''SELECT COUNT(DISTINCT user_id) FROM deposits
                                   WHERE referrer_id=? AND referral_bonus>0''',
                                (session['uid'],)).fetchone()[0]
        total = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND kind='referral_bonus'",
                           (session['uid'],)).fetchone()[0]
    username = current_bot_username()
    return jsonify(count=count, depositors=depositors, earned=total/100,
                   percent=referral_percent(), bot_username=username,
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


def normalize_promo_bundle_items(items):
    """Normalize a v2 multi-promo bundle.

    Unlike the legacy ``components`` object this format is an ordered list, so
    the same gift can appear more than once, with different wager X values or
    quantities. Existing promo codes keep using the old format unchanged.
    """
    if not isinstance(items, list) or not items:
        raise ValueError('Добавьте хотя бы одну награду в промокод.')
    if len(items) > 30:
        raise ValueError('В одном промокоде можно настроить не более 30 строк наград.')
    try:
        catalog = {str(g.get('id')): g for g in read_catalog().get('gifts', [])}
    except (OSError, ValueError, json.JSONDecodeError):
        catalog = {}
    resolved = []
    deposit_count = 0
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError('Проверьте настройки наград промокода.')
        kind = str(raw.get('type') or '')
        if kind not in ('balance', 'gift', 'wager_gift', 'deposit_bonus'):
            raise ValueError('Неизвестный тип награды в промокоде.')
        try:
            quantity = int(raw.get('quantity') or 1)
        except (TypeError, ValueError):
            raise ValueError('Количество подарков указано неверно.')
        if not 1 <= quantity <= 50:
            raise ValueError('Количество одной награды: от 1 до 50.')
        if kind == 'balance':
            try:
                amount = parse_amount(raw.get('amount'))
            except (ValueError, TypeError, InvalidOperation):
                raise ValueError('Укажите сумму TON для награды.')
            if not 1 <= amount <= 100000000:
                raise ValueError('Сумма TON вне допустимых пределов.')
            resolved.append({'type':'balance','amount':amount,'quantity':quantity})
            continue
        if kind in ('gift', 'wager_gift'):
            gift_id = str(raw.get('gift_id') or '')
            gift = catalog.get(gift_id)
            if not gift:
                raise ValueError('Один из подарков не найден в каталоге Portal.')
            try:
                gift_price = ton_to_cents(gift.get('price_ton'))
            except (ValueError, TypeError, InvalidOperation):
                raise ValueError('У одного из подарков нет актуальной цены Portal.')
            if gift_price <= 0:
                raise ValueError('У одного из подарков нет актуальной цены Portal.')
            item = {
                'type': kind, 'quantity': quantity, 'gift_id': gift_id,
                'gift_name': str(gift.get('name') or 'Подарок')[:140],
                'image_url': safe_image(gift.get('image_url') or gift.get('portal_image_url')),
                'gift_price': gift_price,
            }
            if kind == 'wager_gift':
                try:
                    multiplier = float(raw.get('wager_multiplier') or 0)
                    gift_expires_days = int(raw.get('gift_expires_days') or 0)
                except (TypeError, ValueError):
                    raise ValueError('Проверьте X и срок отыгрышного подарка.')
                if not math.isfinite(multiplier) or not 1 <= multiplier <= 1000:
                    raise ValueError('X отыгрыша: от 1 до 1000.')
                if not 0 <= gift_expires_days <= 3650:
                    raise ValueError('Срок жизни отыгрышного подарка: от 0 до 3650 дней.')
                item.update(wager_multiplier=multiplier, gift_expires_days=gift_expires_days)
            resolved.append(item)
            continue
        deposit_count += 1
        if deposit_count > 1:
            raise ValueError('В одном промокоде может быть только один бонус к пополнению.')
        try:
            bonus_percent = float(raw.get('bonus_percent') or 0)
            bonus_fixed = parse_amount(raw.get('bonus_fixed') or 0)
            min_deposit = parse_amount(raw.get('min_deposit') or 0)
        except (ValueError, TypeError, InvalidOperation):
            raise ValueError('Проверьте бонус к пополнению.')
        if (not math.isfinite(bonus_percent) or not 0 <= bonus_percent <= 100 or
                not 0 <= bonus_fixed <= 1000000 or not 0 <= min_deposit <= 100000000 or
                not (bonus_percent or bonus_fixed) or (bonus_percent and bonus_fixed)):
            raise ValueError('Для пополнения укажите один бонус: процент до 100% или TON.')
        resolved.append({'type':'deposit_bonus','quantity':1,'bonus_percent':bonus_percent,
                         'bonus_fixed':bonus_fixed,'min_deposit':min_deposit})
    return resolved


def promo_bundle_payload(items):
    return {'format':'bundle_v2','items':normalize_promo_bundle_items(items)}


def promo_bundle_deposit(payload):
    for item in payload.get('items', []) if isinstance(payload, dict) else []:
        if item.get('type') == 'deposit_bonus':
            return item
    return {}


def promo_purpose(promo):
    custom = str(promo['description'] or '').strip() if 'description' in promo.keys() else ''
    if custom:
        return custom
    kind = promo['reward_type']
    if kind == 'balance':
        return f"Зачисляет {int(promo['amount'] or 0)/100:.2f} TON на игровой баланс."
    if kind == 'tickets':
        return f"Выдаёт {int(promo['amount'] or 0)} билет(ов) для участия в розыгрышах."
    if kind == 'gift':
        return f"Выдаёт подарок «{promo['gift_name'] or 'Подарок'}»."
    if kind == 'wager_gift':
        days = int(promo['gift_expires_days'] or 0) if 'gift_expires_days' in promo.keys() else 0
        lifetime = f' Подарок сгорит через {days} дн. после получения, если отыгрыш не завершён.' if days else ''
        return (f"Выдаёт отыгрышный подарок «{promo['gift_name'] or 'Подарок'}» "
                f"с условием X{float(promo['wager_multiplier'] or 0):g}.{lifetime}")
    if kind == 'deposit_bonus':
        pct = float(promo['bonus_percent'] or 0)
        fixed = int(promo['bonus_fixed'] or 0) / 100
        minimum = int(promo['min_deposit'] or 0) / 100
        value = f'+{pct:g}%' if pct else f'+{fixed:.2f} TON'
        suffix = f' при пополнении от {minimum:.2f} TON' if minimum else ''
        return f'Бонус {value} к следующему подтверждённому пополнению{suffix}.'
    if kind == 'multi':
        try:
            payload = json.loads(promo['reward_json'] or '{}')
        except (ValueError, TypeError, AttributeError):
            payload = {}
        items = payload.get('items') if isinstance(payload, dict) else None
        if isinstance(items, list) and items:
            gift_qty=sum(int(x.get('quantity') or 1) for x in items if x.get('type')=='gift')
            wager_qty=sum(int(x.get('quantity') or 1) for x in items if x.get('type')=='wager_gift')
            balance_qty=sum(int(x.get('quantity') or 1) for x in items if x.get('type')=='balance')
            parts=[]
            if gift_qty:parts.append(f'{gift_qty} обычн. подарок(ов)')
            if wager_qty:parts.append(f'{wager_qty} отыгрышн. подарок(ов)')
            if balance_qty:parts.append(f'{balance_qty} начисление(й) TON')
            if any(x.get('type')=='deposit_bonus' for x in items):parts.append('бонус к пополнению')
            return 'Мультипромокод: ' + ', '.join(parts) + '.'
        components = payload.get('components', {}) if isinstance(payload, dict) else {}
        labels = {'balance':'TON на баланс','gift':'подарок','wager_gift':'отыгрышный подарок','deposit_bonus':'бонус к пополнению'}
        parts = [labels.get(name, name) for name in components]
        return 'Мультипромокод: ' + ', '.join(parts) + '.' if parts else 'Мультипромокод с несколькими наградами.'
    return 'Бонусный промокод GemDrop.'


def promo_view(promo, redemption=None, viewed=False):
    used = bool(redemption)
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
        active=status == 'active', unread=status == 'active' and not viewed,
        expires_at=expires.isoformat() if expires else None,
        used_at=redemption['created_at'] if redemption and status == 'used' else None,
        created_at=promo['created_at'],
        gift_name=(promo['gift_name'] or '') if 'gift_name' in promo.keys() else '',
        gift_price_ton=(int(promo['gift_price'] or 0)/100 if 'gift_price' in promo.keys() else 0),
        wager_multiplier=(float(promo['wager_multiplier'] or 0) if 'wager_multiplier' in promo.keys() else 0),
    )


def unique_promo_code(db, prefix='GEM'):
    for _ in range(12):
        code = prefix + '-' + secrets.token_hex(4).upper()
        if not db.execute('SELECT 1 FROM promo_codes WHERE code=?', (code,)).fetchone():
            return code
    return generated_promo_code()


def create_upgrade_compensation_promo(db, user_id, source_price, force=False, pity_streak=0):
    """Issue a loss-compensation promo scaled to the size of the loss.

    Small and medium losses still mostly receive wager gifts, but serious losses
    now have a much better chance to receive a more humane reward profile:
    lower playthrough multipliers or even a normal gift promo. Hard pity also
    guarantees something meaningful instead of another extreme x100-style code.
    """
    source_ton = source_price / 100
    if source_ton < 0.10:
        return None

    game_loss = game_net_loss_cents(db, user_id) + source_price
    game_loss_ton = game_loss / 100
    if source_ton < 1:
        base_chance = 0.03
    elif source_ton < 2:
        base_chance = 0.05
    elif source_ton < 5:
        base_chance = 0.08
    elif source_ton < 10:
        base_chance = 0.11
    elif source_ton < 25:
        base_chance = 0.14
    elif source_ton < 100:
        base_chance = 0.19
    elif source_ton < 250:
        base_chance = 0.24
    else:
        base_chance = 0.30
    loss_bonus = min(0.10, max(0.0, game_loss_ton) / 2500.0)
    pity_bonus = max(0, int(pity_streak)) * 0.024
    chance = min(0.55, base_chance + loss_bonus + pity_bonus)
    if not force and secrets.randbelow(10000) >= round(chance * 10000):
        return None

    try:
        catalog = read_catalog().get('gifts', [])
    except (OSError, ValueError, json.JSONDecodeError):
        catalog = []
    valid = []
    for gift in catalog:
        try:
            price = ton_to_cents(gift.get('price_ton'))
        except (ValueError, TypeError, InvalidOperation):
            continue
        if price > 0 and gift.get('id') and gift.get('name'):
            valid.append((price, gift))
    if not valid:
        return None
    valid.sort(key=lambda x: x[0])

    normal_gift_chance = 0.0
    if source_ton >= 25:
        if source_ton < 100:
            normal_gift_chance = 0.14
        elif source_ton < 250:
            normal_gift_chance = 0.32
        else:
            normal_gift_chance = 0.46
        normal_gift_chance += min(0.12, max(0.0, game_loss_ton - 50) / 2500.0)
        if force:
            normal_gift_chance = max(normal_gift_chance, 0.45 if source_ton < 100 else 0.7)
    reward_type = 'gift' if normal_gift_chance > 0 and secrets.randbelow(10000) < round(min(0.8, normal_gift_chance) * 10000) else 'wager_gift'

    if reward_type == 'gift':
        if source_ton < 100:
            budget = round(source_price * 0.10)
            min_budget = max(100, round(budget * 0.45))
        elif source_ton < 250:
            budget = round(source_price * 0.08)
            min_budget = max(300, round(budget * 0.50))
        else:
            budget = round(source_price * 0.06)
            min_budget = max(500, round(budget * 0.50))
        budget = min(3500, max(250, budget))
    else:
        if source_ton <= 10:
            budget = round(source_price * 0.24)
        elif source_ton <= 50:
            budget = round(source_price * 0.14)
        else:
            budget = round(source_price * 0.08)
        budget = min(3000, max(20, budget))
        min_budget = max(1, round(budget * 0.25))

    candidates = [x for x in valid if min_budget <= x[0] <= budget]
    if not candidates:
        candidates = [x for x in valid if x[0] <= budget]
    if not candidates:
        if not force:
            return None
        candidates = valid[:min(10, len(valid))]
    candidates.sort(key=lambda x: x[0], reverse=True)
    top_slice = max(1, min(len(candidates), 14 if reward_type == 'gift' else 18))
    _, gift = secrets.choice(candidates[:top_slice])
    gift_id = str(gift['id'])
    gift_name = str(gift['name'])[:140]
    gift_image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
    gift_price = ton_to_cents(gift.get('price_ton'))

    if reward_type == 'wager_gift':
        if source_ton < 5:
            multipliers = [15] * 4 + [20] * 4 + [25] * 3 + [35]
        elif source_ton < 25:
            multipliers = [10] * 2 + [15] * 5 + [20] * 5 + [25] * 3 + [35]
        elif source_ton < 100:
            multipliers = [8] + [10] * 4 + [12] * 4 + [15] * 5 + [20] * 4 + [25] * 2
        else:
            multipliers = [5] + [8] * 4 + [10] * 5 + [12] * 4 + [15] * 3 + [20]
        wager_multiplier = float(secrets.choice(multipliers))
    else:
        wager_multiplier = 0.0

    boost = loss_rtp_boost_points(game_loss)
    code = unique_promo_code(db, 'UPG')
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    description = (f'Подарок «{gift_name}».' if reward_type == 'gift'
                   else f'Отыгрышный подарок «{gift_name}» · X{wager_multiplier:g}.')
    reward_json = json.dumps({
        'compensation': True,
        'source_loss_ton': round(source_ton, 2),
        'game_loss_ton': round(game_loss_ton, 2),
        'loss_rtp_boost': round(boost, 2),
        'pity_streak': int(pity_streak),
        'scaled_reward': True,
        'reward_type': reward_type,
    }, ensure_ascii=False)
    db.execute("""INSERT INTO promo_codes(
      code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,
      max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,
      assigned_user_id,source_label,description,expires_at)
      VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",
      (code, reward_type, 0, gift_id, gift_name, gift_image, gift_price, wager_multiplier,
       0, 0, 0, 0, reward_json, user_id, 'Компенсация Upgrade', description, expires_at))
    row = db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone()
    log_event(db, user_id, 'promo_issued', code=code, source='Компенсация Upgrade', reward_type=reward_type,
              wager_multiplier=wager_multiplier, game_loss_ton=round(game_loss_ton, 2),
              loss_rtp_boost=round(boost, 2), pity_streak=int(pity_streak), forced=bool(force))
    return promo_view(row)

def apply_upgrade_loss_compensation(db, user_id, source_price, target_price=None):
    """Award exactly one server-selected prize, persisted with the upgrade spin.

    Cosmetic reel items come from the same eligible pool; replaying a spin uses
    its saved result and never issues the prize twice.
    """
    empty = dict(cashback=0, cashback_percent=0, promo=None, reward=None, reel=[])
    if source_price < 500:
        return empty
    large = source_price >= 10000
    medium = source_price >= 2500
    budget = max(100, round(source_price * (.20 if large else .16)))
    catalog_candidates = craft_catalog_candidates()
    gifts = [g for g in catalog_candidates if g['price'] <= budget]
    # For medium TON losses the old compensation tape could contain only TON
    # cells when the catalog had no very cheap gifts. Keep the actual economy
    # conservative, but always put a real catalog gift into the visible/prize
    # pool when the stake is large enough to make that fair.
    visual_budget = max(budget, round(source_price * (.75 if medium or large else .42)))
    if target_price:
        try:
            visual_budget = max(visual_budget, round(int(target_price) * .18))
        except (TypeError, ValueError):
            pass
    visual_gifts = [g for g in catalog_candidates if g['price'] <= visual_budget]
    if not gifts and source_price >= 1000 and visual_gifts:
        gifts = visual_gifts[:max(1, min(18, len(visual_gifts)))]
    pool = []
    for percent in (1, 2, 3, 5):
        amount = max(1, round(source_price * percent / 100))
        pool.append(dict(type='balance', amount=amount/100, image_url='/static/img/ton.png', name='TON'))
    # Tickets are a first-class Upgrade compensation prize. Scale them with the
    # lost stake while keeping a useful minimum for small eligible losses.
    ticket_count=max(1,min(250,round(source_price/500)))
    pool.append(dict(type='tickets', tickets=ticket_count, image_url='', name='Билеты'))
    gift_options = []
    for gift in gifts:
        # Larger losses increase the eligible catalog and lower playthrough.
        for kind in ('gift', 'wager_gift', 'promo'):
            multiplier = secrets.choice([5, 8, 10] if large else [8, 10, 12] if medium else [10, 15, 20])
            gift_options.append(dict(type=kind, gift_id=gift['id'], name=gift['name'],
                                     image_url=gift['image_url'], price_ton=gift['price']/100,
                                     wager_multiplier=multiplier if kind=='wager_gift' else 0))
    if gift_options:
        # Gift / wagering / personal-code rewards together: 80%, 85%, 90%.
        weights = [('balance', 8 if large else 12 if medium else 16),
                   ('tickets', 10), ('wager_gift', 37), ('gift', 25),
                   ('promo', 20 if large else 16 if medium else 12)]
        roll = secrets.randbelow(100)
        kind = 'balance'
        for candidate, weight in weights:
            if roll < weight:
                kind = candidate
                break
            roll -= weight
        if kind in ('balance','tickets'):
            candidates=[x for x in pool if x['type']==kind]
        else:
            candidates=[g for g in gift_options if g['type']==kind]
        reward = dict(secrets.choice(candidates or pool))
    else:
        reward = dict(secrets.choice(pool))
    reel_options = pool + gift_options
    if visual_gifts:
        # Near-miss and possible compensation gift cells, so high-value TON losses
        # do not look like a tape of TON-only consolation prizes.
        extra_reel_gifts = []
        for gift in visual_gifts[-18:]:
            for kind in ('gift', 'wager_gift'):
                multiplier = secrets.choice([5, 8, 10] if large else [8, 10, 12] if medium else [10, 15, 20])
                extra_reel_gifts.append(dict(type=kind, gift_id=gift['id'], name=gift['name'],
                                             image_url=gift['image_url'], price_ton=gift['price']/100,
                                             wager_multiplier=multiplier if kind=='wager_gift' else 0))
        reel_options.extend(extra_reel_gifts)
    comp = dict(empty, reward=reward, reel=reel_options)
    if reward['type'] == 'balance':
        amount = ton_to_cents(reward['amount'])
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
        record_transaction(db, user_id, 'upgrade_cashback', amount, 'upgrade', '', 'Компенсация Upgrade')
        comp['cashback'] = amount/100
    elif reward['type']=='tickets':
        tickets=max(1,int(reward.get('tickets') or 1))
        db.execute('UPDATE users SET tickets=tickets+? WHERE id=?',(tickets,user_id))
        db.execute('INSERT INTO ticket_ledger(user_id,amount,kind,reference_type,reference_id,details) VALUES(?,?,?,?,?,?)',
                   (user_id,tickets,'upgrade_compensation','upgrade','',f'Компенсация Upgrade: {tickets} билет(ов)'))
    elif reward['type'] in ('gift', 'wager_gift'):
        locked = reward['type'] == 'wager_gift'
        price = ton_to_cents(reward['price_ton'])
        multiplier = reward['wager_multiplier']
        cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                         promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress)
                         VALUES(?,?,?,?,?,'upgrade_compensation',?,?,?,0)""",
                         (user_id,reward['gift_id'],reward['name'],reward['image_url'],price,
                          int(locked),multiplier,round(price*multiplier)))
        reward['inventory_id'] = cur.lastrowid
        reward['wager_target'] = round(price*multiplier)/100
        record_transaction(db, user_id, 'upgrade_compensation_gift', 0, 'inventory', cur.lastrowid, reward['name'])
    else:
        code = unique_promo_code(db, 'UPG')
        expires = (datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
        db.execute("""INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,
                     gift_price,wager_multiplier,max_uses,created_by,assigned_user_id,source_label,description,
                     reward_json,expires_at) VALUES(?,'gift',0,?,?,?,?,0,1,0,?,?,?,?,?)""",
                   (code,reward['gift_id'],reward['name'],reward['image_url'],ton_to_cents(reward['price_ton']),
                    user_id,'Компенсация Upgrade','Персональный промокод на подарок',
                    json.dumps(dict(compensation=True, owner_id=user_id)),expires))
        comp['promo'] = promo_view(db.execute('SELECT * FROM promo_codes WHERE code=?',(code,)).fetchone())
        reward['code'] = code
    # Keep the replay payload small even for large catalogs.
    comp['reel'] = [secrets.choice(reel_options or pool or gift_options) for _ in range(36)]
    return comp


@app.get('/api/rewards/pending')
@login_required
def pending_rewards():
    """Freebet rewards the user has received but not yet acknowledged in the Mini App."""
    with connect() as db:
        rows = db.execute("""SELECT code, reward_json, created_at FROM freebet_redemptions
                             WHERE user_id=? AND seen_at IS NULL ORDER BY created_at ASC LIMIT 20""",
                          (session['uid'],)).fetchall()
    batches = []
    for row in rows:
        try:
            reward = json.loads(row['reward_json'] or '{}')
        except (ValueError, TypeError):
            reward = {}
        items = describe_reward_items(reward)
        if items:
            batches.append(dict(code=row['code'], created_at=row['created_at'], items=items))
    return jsonify(batches=batches)


@app.post('/api/rewards/ack')
@login_required
def ack_rewards():
    data = request.get_json(silent=True) or {}
    codes = [str(c).strip().upper() for c in (data.get('codes') or []) if str(c).strip()][:50]
    with connect() as db:
        if codes:
            for code in codes:
                db.execute("""UPDATE freebet_redemptions SET seen_at=CURRENT_TIMESTAMP
                              WHERE user_id=? AND code=? AND seen_at IS NULL""", (session['uid'], code))
        else:
            db.execute("""UPDATE freebet_redemptions SET seen_at=CURRENT_TIMESTAMP
                          WHERE user_id=? AND seen_at IS NULL""", (session['uid'],))
        db.commit()
    return jsonify(ok=True)


@app.get('/api/promocodes/mine')
@login_required
def my_promocodes():
    with connect() as db:
        viewed_codes = {row['code'] for row in db.execute(
            'SELECT code FROM promo_views WHERE user_id=?', (session['uid'],)).fetchall()}
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
                                  WHERE r.user_id=? AND p.source_label<>'Freebet' ORDER BY r.created_at DESC""", (session['uid'],)).fetchall()
        for row in used_rows:
            by_code.setdefault(row['code'], row)
        items = []
        for promo in by_code.values():
            redemption = db.execute('SELECT * FROM promo_redemptions WHERE code=? AND user_id=?',
                                    (promo['code'], session['uid'])).fetchone()
            items.append(promo_view(promo, redemption, promo['code'] in viewed_codes))
    order = {'active':0, 'expired':1, 'disabled':2, 'used':3}
    items.sort(key=lambda x: x['created_at'] or '', reverse=True)
    items.sort(key=lambda x: order.get(x['status'], 9))
    return jsonify(items=items)


@app.get('/api/promocodes/unread-count')
@login_required
def unread_promocode_count():
    with connect() as db:
        rows = db.execute('''SELECT p.*,r.code AS redeemed,v.code AS viewed FROM promo_codes p
            LEFT JOIN promo_redemptions r ON r.code=p.code AND r.user_id=?
            LEFT JOIN promo_views v ON v.code=p.code AND v.user_id=?
            WHERE p.assigned_user_id=?''', (session['uid'],session['uid'],session['uid'])).fetchall()
        count = sum(1 for p in rows if p['active'] and not p['redeemed'] and not p['viewed']
                    and not promo_is_expired(p) and
                    (not p['max_uses'] or p['uses_count'] < p['max_uses']))
        for claim in db.execute('SELECT reward_json FROM level_claims WHERE user_id=?',
                                (session['uid'],)).fetchall():
            try:
                code = json.loads(claim['reward_json'] or '{}').get('code')
            except (ValueError, TypeError, AttributeError):
                continue
            if not code:
                continue
            old = db.execute('''SELECT p.*,r.code AS redeemed,v.code AS viewed FROM promo_codes p
                LEFT JOIN promo_redemptions r ON r.code=p.code AND r.user_id=?
                LEFT JOIN promo_views v ON v.code=p.code AND v.user_id=?
                WHERE p.code=? AND p.assigned_user_id<>?''',
                (session['uid'],session['uid'],code,session['uid'])).fetchone()
            if old and old['active'] and not old['redeemed'] and not old['viewed'] and \
                    not promo_is_expired(old) and (not old['max_uses'] or old['uses_count'] < old['max_uses']):
                count += 1
    return jsonify(count=count)


@app.post('/api/promocodes/<code>/view')
@login_required
def view_personal_promocode(code):
    code = str(code).strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Промокод не найден.', 404)
    with connect() as db:
        promo = db.execute('SELECT assigned_user_id FROM promo_codes WHERE code=?', (code,)).fetchone()
        owned = bool(promo and int(promo['assigned_user_id'] or 0) == int(session['uid']))
        if not owned:
            for row in db.execute('SELECT reward_json FROM level_claims WHERE user_id=?', (session['uid'],)).fetchall():
                try:
                    if json.loads(row['reward_json'] or '{}').get('code') == code:
                        owned = True
                        break
                except (ValueError, TypeError, AttributeError):
                    continue
        if not owned:
            return error('Промокод не найден.', 404)
        db.execute('INSERT OR IGNORE INTO promo_views(user_id,code) VALUES(?,?)', (session['uid'], code))
    return jsonify(ok=True)


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
        if (int(promo['assigned_user_id'] or 0) not in (0, int(session['uid']))
                or (str(promo['source_label'] or '') == 'Компенсация Upgrade'
                    and int(promo['assigned_user_id'] or 0) != int(session['uid']))):
            return error('Этот промокод предназначен другому пользователю.', 403)
        if promo_is_expired(promo):
            return error('Срок действия промокода истёк.', 409)
        prior=db.execute('SELECT * FROM promo_redemptions WHERE code=? AND user_id=?',(code,session['uid'])).fetchone()
        if prior:return error('Вы уже активировали этот промокод.',409)
        if promo['max_uses'] > 0 and promo['uses_count'] >= promo['max_uses']:
            return error('Лимит активаций этого промокода исчерпан.', 409)
        inventory_id = None
        if promo['reward_type'] == 'balance':
            amount = max(0, int(promo['amount']))
            if amount <= 0:
                return error('Награда промокода настроена неверно.', 500)
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, session['uid']))
            reward = dict(type='balance', amount=amount/100)
            record_transaction(db, session['uid'], 'promo_balance', amount, 'promo', code, f'Промокод {code}')
        elif promo['reward_type'] == 'tickets':
            tickets=max(0,int(promo['amount'] or 0))
            if tickets<=0:return error('Награда промокода настроена неверно.',500)
            db.execute('UPDATE users SET tickets=tickets+? WHERE id=?',(tickets,session['uid']))
            db.execute('INSERT INTO ticket_ledger(user_id,amount,kind,reference_type,reference_id,details) VALUES(?,?,?,?,?,?)',
                       (session['uid'],tickets,'promo','promo',code,f'Промокод {code}'))
            reward=dict(type='tickets',tickets=tickets)
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
            item_expires_at = promo_gift_expiry(promo['gift_expires_days'])
            cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                              promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                              VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?,?)""",
                             (session['uid'], promo['gift_id'], promo['gift_name'], promo['gift_image_url'],
                              promo['gift_price'], multiplier, target, code, item_expires_at))
            inventory_id = cur.lastrowid
            reward = dict(type='wager_gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'],
                                                       name=promo['gift_name'], image_url=promo['gift_image_url'],
                                                       price_ton=promo['gift_price']/100, promo_locked=True,
                                                       wager_multiplier=multiplier, wager_target=target/100,
                                                       wager_progress=0, expires_at=item_expires_at))
            record_transaction(db, session['uid'], 'promo_wager_gift', 0, 'promo', code,
                               f'{promo["gift_name"]} · X{multiplier:g}')
        elif promo['reward_type']=='multi':
            payload=json.loads(promo['reward_json'] or '{}')
            rewards=[]
            has_deposit=False
            items=payload.get('items') if isinstance(payload,dict) else None
            if isinstance(items,list) and items:
                for comp in items:
                    kind=str(comp.get('type') or '')
                    quantity=max(1,min(50,int(comp.get('quantity') or 1)))
                    if kind=='balance':
                        amount=int(comp.get('amount') or 0)*quantity
                        if amount<=0:return error('Мультипромокод настроен неверно.',500)
                        db.execute('UPDATE users SET balance=balance+? WHERE id=?',(amount,session['uid']))
                        record_transaction(db,session['uid'],'promo_balance',amount,'promo',code,f'Мультипромокод {code}')
                        rewards.append(dict(type='balance',amount=amount/100,quantity=quantity))
                    elif kind in ('gift','wager_gift'):
                        awarded=[]
                        for _ in range(quantity):
                            if kind=='gift':
                                cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'promo')",
                                               (session['uid'],comp['gift_id'],comp['gift_name'],comp['image_url'],comp['gift_price']))
                                expires_at=None
                            else:
                                multiplier=float(comp['wager_multiplier'])
                                target=round(int(comp['gift_price'])*multiplier)
                                expires_at=promo_gift_expiry(comp.get('gift_expires_days'))
                                cur=db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                                  promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                                                  VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?,?)""",
                                               (session['uid'],comp['gift_id'],comp['gift_name'],comp['image_url'],comp['gift_price'],multiplier,target,code,expires_at))
                            inventory_id=cur.lastrowid
                            awarded.append(inventory_id)
                            record_transaction(db,session['uid'],'promo_'+kind,0,'promo',code,comp['gift_name'])
                        rewards.append(dict(type=kind,quantity=quantity,inventory_ids=awarded,
                          gift=dict(id=awarded[-1],name=comp['gift_name'],image_url=comp['image_url'],price_ton=comp['gift_price']/100,
                                    wager_multiplier=comp.get('wager_multiplier',0),expires_at=expires_at if kind=='wager_gift' else None)))
                    elif kind=='deposit_bonus':
                        has_deposit=True
                        rewards.append(dict(type='deposit_bonus',code=code,
                          bonus_percent=float(comp.get('bonus_percent') or 0),
                          bonus_fixed=int(comp.get('bonus_fixed') or 0)/100,
                          min_deposit=int(comp.get('min_deposit') or 0)/100))
                    else:
                        return error('Мультипромокод настроен неверно.',500)
            else:
                components=payload.get('components',{}) if isinstance(payload,dict) else {}
                if not components:return error('Мультипромокод настроен неверно.',500)
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
                        component_expires_at=None
                    else:
                        multiplier=float(comp['wager_multiplier']);target=round(int(comp['gift_price'])*multiplier)
                        component_expires_at=promo_gift_expiry(comp.get('gift_expires_days'))
                        cur=db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                          promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at)
                                          VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?,?)""",
                                       (session['uid'],comp['gift_id'],comp['gift_name'],comp['image_url'],comp['gift_price'],multiplier,target,code,component_expires_at))
                    inventory_id=cur.lastrowid
                    record_transaction(db,session['uid'],'promo_'+kind,0,'promo',code,comp['gift_name'])
                    rewards.append(dict(type=kind,gift=dict(id=inventory_id,name=comp['gift_name'],image_url=comp['image_url'],price_ton=comp['gift_price']/100,
                       wager_multiplier=comp.get('wager_multiplier',0),expires_at=component_expires_at if kind=='wager_gift' else None)))
                if 'deposit_bonus' in components:
                    has_deposit=True
                    comp=components['deposit_bonus']
                    rewards.append(dict(type='deposit_bonus',code=code,bonus_percent=float(comp.get('bonus_percent') or 0),
                      bonus_fixed=int(comp.get('bonus_fixed') or 0)/100,min_deposit=int(comp.get('min_deposit') or 0)/100))
            reward=dict(type='multi',rewards=rewards)
        elif promo['reward_type'] == 'deposit_bonus':
            reward=dict(type='deposit_bonus',code=code,bonus_percent=float(promo['bonus_percent'] or 0),
                        bonus_fixed=int(promo['bonus_fixed'] or 0)/100,min_deposit=int(promo['min_deposit'] or 0)/100)
        else:
            return error('Награда промокода настроена неверно.', 500)
        redemption_type='deposit_bonus' if promo['reward_type']=='multi' and has_deposit else promo['reward_type']
        if redemption_type == 'deposit_bonus':
            db.execute('''UPDATE promo_redemptions SET deactivated_at=CURRENT_TIMESTAMP
                          WHERE user_id=? AND reward_type='deposit_bonus' AND code<>?
                          AND consumed_at IS NULL AND deactivated_at IS NULL''', (session['uid'], code))
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


@app.get('/api/promocodes/check-deposit')
@login_required
def check_deposit_promocode():
    code = str(request.args.get('code') or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return jsonify(valid=False, message='Введите промокод.')
    with connect() as db:
        active = db.execute("""SELECT p.code,p.bonus_percent,p.bonus_fixed,p.min_deposit FROM promo_redemptions r
                               JOIN promo_codes p ON p.code=r.code WHERE r.user_id=? AND r.reward_type='deposit_bonus'
                               AND r.consumed_at IS NULL AND r.deactivated_at IS NULL
                               ORDER BY r.created_at DESC LIMIT 1""", (session['uid'],)).fetchone()
        if active and active['code'] == code:
            return jsonify(valid=True, already_active=True, code=code,
                           bonus_percent=float(active['bonus_percent'] or 0),
                           bonus_fixed=int(active['bonus_fixed'] or 0) / 100,
                           min_deposit=int(active['min_deposit'] or 0) / 100)
        promo = db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone()
        if not promo or not promo['active']:
            return jsonify(valid=False, message='Промокод не найден.')
        if promo['reward_type'] != 'deposit_bonus':
            return jsonify(valid=False, message='Этот промокод не подходит для пополнения.')
        if (int(promo['assigned_user_id'] or 0) not in (0, int(session['uid']))
                or (str(promo['source_label'] or '') == 'Компенсация Upgrade'
                    and int(promo['assigned_user_id'] or 0) != int(session['uid']))):
            return jsonify(valid=False, message='Этот промокод предназначен другому пользователю.')
        if promo_is_expired(promo):
            return jsonify(valid=False, message='Срок действия промокода истёк.')
        prior = db.execute('SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?', (code, session['uid'])).fetchone()
        if prior:
            return jsonify(valid=False, message='Вы уже активировали этот промокод.')
        if promo['max_uses'] > 0 and promo['uses_count'] >= promo['max_uses']:
            return jsonify(valid=False, message='Лимит активаций этого промокода исчерпан.')
        return jsonify(valid=True, already_active=False, code=code,
                       bonus_percent=float(promo['bonus_percent'] or 0),
                       bonus_fixed=int(promo['bonus_fixed'] or 0) / 100,
                       min_deposit=int(promo['min_deposit'] or 0) / 100)


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


@app.get('/api/admin/post/settings')
@admin_required
def admin_post_settings():
    data = post_channel_settings()
    return jsonify(**data, configured=bool(data['chat_id']))


@app.post('/api/admin/post/settings')
@admin_required
def admin_save_post_settings():
    data = request.get_json(silent=True) or {}
    try:
        chat_id = normalize_channel_id(data.get('chat_id'))
        info = inspect_post_channel(chat_id)
    except ValueError as exc:
        return error(str(exc))
    except RuntimeError as exc:
        return error('Telegram: ' + str(exc), 409)
    info['saved_at'] = datetime.now(timezone.utc).isoformat()
    save_document('post_channel', info)
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'post_channel_save', info['chat_id']))
    return jsonify(ok=True, **info)


@app.post('/api/admin/post/publish')
@admin_required
def admin_publish_post():
    settings = post_channel_settings()
    if not settings['chat_id']:
        return error('Сначала сохраните канал в разделе Post.', 409)
    multipart = bool(request.files) or bool(request.form) or str(request.content_type or '').startswith('multipart/form-data')
    data = request.form if multipart else (request.get_json(silent=True) or {})
    raw_text = str(data.get('text') or '').strip()
    image_ref = str(data.get('image') or '').strip()
    image_refs = [x.strip() for x in re.split(r'[\r\n]+', image_ref) if x.strip()]
    if len(image_refs) > 10:
        return error('Можно добавить не более 10 изображений в один пост.')
    photo_files = []
    if multipart:
        if hasattr(request.files, 'getlist'):
            photo_files = [x for x in request.files.getlist('photos') if x and getattr(x, 'filename', '')]
        legacy_photo = request.files.get('photo') if hasattr(request.files, 'get') else None
        if legacy_photo and getattr(legacy_photo, 'filename', ''):
            photo_files.append(legacy_photo)
    if len(photo_files) + len(image_refs) > 10:
        return error('Можно добавить не более 10 изображений в один пост.')
    try:
        raw_buttons = json.loads(data.get('buttons') or '[]') if multipart else data.get('buttons', [])
        buttons = normalize_post_buttons(raw_buttons)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc))
    try:
        text, resolved_emojis = resolve_post_custom_emojis(raw_text, buttons)
    except (ValueError, RuntimeError) as exc:
        return error('Premium emoji: ' + str(exc), 409)
    remember_emojis(emojis_in_post(text, buttons))
    if not text and not image_refs and not photo_files:
        return error('Добавьте текст или изображение.')
    reply_markup = {'inline_keyboard': buttons} if buttons else None
    silent = str(data.get('silent') or '').lower() in ('1', 'true', 'on', 'yes') if multipart else bool(data.get('silent'))
    protect = str(data.get('protect') or '').lower() in ('1', 'true', 'on', 'yes') if multipart else bool(data.get('protect'))
    base_payload = {'chat_id': settings['chat_id'], 'disable_notification': silent, 'protect_content': protect}
    transport_used = 'html_message'
    def send_post_text(post_text, markup=None):
        # Mirror the proven /start transport: ordinary posts, including premium
        # emoji, go through sendMessage + HTML. Rich Messages are only needed
        # when the parsed text is longer than the normal 4096-character limit.
        nonlocal transport_used
        visible_len = len(re.sub(r'<[^>]+>', '', post_text or ''))
        if visible_len <= 4096:
            payload = dict(base_payload, text=post_text, parse_mode='HTML')
            if markup:
                payload['reply_markup'] = markup
            transport_used = 'html_message'
            return telegram_api('sendMessage', payload, timeout=(3, 15))
        rich_html = rich_custom_emoji_html(post_text)
        if len(rich_html.encode('utf-8')) > 32768:
            raise RuntimeError('Текст поста превышает лимит Rich Message (32 768 UTF-8 символов).')
        payload = dict(base_payload, rich_message={'html': rich_html})
        if markup:
            payload['reply_markup'] = markup
        transport_used = 'rich_message'
        return telegram_api('sendRichMessage', payload, timeout=(3, 18))

    try:
        sent = None
        sent_messages = []
        def remember_sent(message):
            if isinstance(message, dict) and message.get('message_id'):
                sent_messages.append(message)
            return message
        total_media = len(photo_files) + len(image_refs)
        if total_media > 1:
            # Bot API 10.2+ Rich Messages can keep several images, formatted text and
            # the inline keyboard together in one channel post.
            media = []
            media_tags = []
            files = {}
            index = 0
            for photo in photo_files:
                media_id = f'post{index}'
                attach_name = f'post_file_{index}'
                media.append({'id': media_id, 'media': {'type': 'photo', 'media': f'attach://{attach_name}'}})
                media_tags.append(f'<img src="tg://photo?id={media_id}"/>')
                files[attach_name] = (photo.filename or f'post-{index}.jpg', photo.stream, photo.mimetype or 'application/octet-stream')
                index += 1
            for ref in image_refs:
                media_id = f'post{index}'
                media.append({'id': media_id, 'media': {'type': 'photo', 'media': ref}})
                media_tags.append(f'<img src="tg://photo?id={media_id}"/>')
                index += 1
            rich_html = '<tg-collage>' + ''.join(media_tags) + '</tg-collage>'
            if text:
                rich_html += '\n' + rich_custom_emoji_html(text)
            payload = dict(base_payload, rich_message={'html': rich_html, 'media': media})
            if reply_markup:
                payload['reply_markup'] = reply_markup
            transport_used = 'rich_message'
            sent = remember_sent(telegram_api('sendRichMessage', payload, files=files or None, timeout=(3, 30)))
        elif total_media == 1:
            caption_ok = bool(text) and len(re.sub(r'<[^>]+>', '', text)) <= 1024
            if photo_files:
                photo_file = photo_files[0]
                payload = dict(base_payload)
                if reply_markup and not text:
                    payload['reply_markup'] = reply_markup
                if caption_ok:
                    payload.update(caption=text, parse_mode='HTML')
                    if reply_markup:
                        payload['reply_markup'] = reply_markup
                files = {'photo': (photo_file.filename or 'post.jpg', photo_file.stream, photo_file.mimetype or 'application/octet-stream')}
                sent = remember_sent(telegram_api('sendPhoto', payload, files=files, timeout=(3, 20)))
            else:
                payload = dict(base_payload, photo=image_refs[0])
                if reply_markup and not text:
                    payload['reply_markup'] = reply_markup
                if caption_ok:
                    payload.update(caption=text, parse_mode='HTML')
                    if reply_markup:
                        payload['reply_markup'] = reply_markup
                sent = remember_sent(telegram_api('sendPhoto', payload, timeout=(3, 15)))
            if caption_ok:
                transport_used = 'html_caption'
            if text and not caption_ok:
                sent = remember_sent(send_post_text(text, reply_markup))
        else:
            sent = remember_sent(send_post_text(text, reply_markup))
        expected = {e['id'] for e in emojis_in_post(text)}
        expected_icons = {b['icon_custom_emoji_id'] for row in buttons for b in row if b.get('icon_custom_emoji_id')}

        def collect_returned_custom_emojis(value, text_ids, icon_ids):
            if isinstance(value, dict):
                if value.get('type') == 'custom_emoji' and value.get('custom_emoji_id'):
                    text_ids.add(str(value['custom_emoji_id']))
                if value.get('custom_emoji_id') and ('alternative_text' in value or 'emoji' in value):
                    text_ids.add(str(value['custom_emoji_id']))
                if value.get('icon_custom_emoji_id'):
                    icon_ids.add(str(value['icon_custom_emoji_id']))
                for child in value.values():
                    collect_returned_custom_emojis(child, text_ids, icon_ids)
            elif isinstance(value, list):
                for child in value:
                    collect_returned_custom_emojis(child, text_ids, icon_ids)

        actual, actual_icons = set(), set()
        collect_returned_custom_emojis(sent or {}, actual, actual_icons)
        missing_text = expected - actual
        missing_icons = expected_icons - actual_icons
        warnings = []
        # Telegram does not guarantee that every formatting detail is echoed in
        # the same response fields for normal vs Rich Messages. A successful
        # send must therefore never be auto-deleted just because verification is
        # inconclusive. This was the regression that made posts appear to stop.
        if missing_text:
            if settings.get('chat_type') == 'channel':
                warnings.append(f'Telegram не вернул {len(missing_text)} custom emoji в опубликованном канальном сообщении. Для каналов Bot API требует дополнительный username бота, приобретённый через Fragment; Premium владельца действует только в private/group/supergroup.')
            else:
                warnings.append(f'Telegram принял пост, но ответ API не подтвердил {len(missing_text)} premium emoji в тексте.')
        if missing_icons:
            if settings.get('chat_type') == 'channel':
                warnings.append(f'Telegram не вернул {len(missing_icons)} premium emoji на кнопках канального сообщения. Для каналов действует то же требование дополнительного username через Fragment.')
            else:
                warnings.append(f'Telegram принял пост, но ответ API не подтвердил {len(missing_icons)} premium emoji на кнопках.')
        with connect() as db:
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'channel_post', f'{settings["chat_id"]}:{(sent or {}).get("message_id", "")}'))
        return jsonify(ok=True, message_id=(sent or {}).get('message_id'), warnings=warnings, premium_emoji_verified=bool((expected or expected_icons) and not missing_text and not missing_icons), premium_emoji_transport=(transport_used if expected else 'reply_markup' if expected_icons else transport_used))
    except RuntimeError as exc:
        # If a multi-step publish managed to send media before Telegram rejected
        # the formatted text/keyboard, clean the partial publication as well.
        for message in reversed(locals().get('sent_messages', [])):
            try:
                telegram_api('deleteMessage', {'chat_id': settings['chat_id'], 'message_id': message['message_id']}, timeout=(2, 6))
            except RuntimeError:
                pass
        return error('Telegram не опубликовал пост: ' + str(exc), 409)


def _freebet_backing_values(data, code):
    reward_type = str(data.get('reward_type') or 'balance')
    amount=0;gift_id='';gift_name='';gift_image='';gift_price=0;wager_multiplier=0.0;gift_expires_days=0
    bonus_percent=0;bonus_fixed=0;min_deposit=0;multi_reward=None
    if reward_type == 'balance':
        amount = parse_amount(data.get('amount'))
        if not 1 <= amount <= 100000000:
            raise ValueError('Сумма фрибета должна быть от 0.01 до 1 000 000 TON.')
    elif reward_type in ('gift','wager_gift'):
        gift_id = str(data.get('gift_id') or '')
        gift = next((g for g in read_catalog().get('gifts', []) if str(g.get('id')) == gift_id), None)
        if not gift:
            raise ValueError('Подарок не найден в каталоге Portal.')
        gift_name = str(gift.get('name') or 'Подарок')[:140]
        gift_image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
        gift_price = ton_to_cents(gift.get('price_ton'))
        if gift_price <= 0:
            raise ValueError('У подарка должна быть актуальная цена Portal.')
        if reward_type == 'wager_gift':
            wager_multiplier = float(str(data.get('wager_multiplier') or 0).replace(',', '.'))
            if not 1 <= wager_multiplier <= 1000:
                raise ValueError('X отыгрыша должен быть от 1 до 1000.')
            gift_expires_days = int(float(str(data.get('gift_expires_days') or 0).replace(',', '.')))
            if not 0 <= gift_expires_days <= 3650:
                raise ValueError('Срок жизни подарка: от 0 до 3650 дней.')
    elif reward_type == 'deposit_bonus':
        bonus_percent = float(str(data.get('bonus_percent') or 0).replace(',', '.'))
        bonus_fixed = parse_amount(data.get('bonus_fixed') or 0)
        min_deposit = parse_amount(data.get('min_deposit') or 0)
        if not math.isfinite(bonus_percent) or not 0 <= bonus_percent <= 100 or not (bonus_percent or bonus_fixed) or (bonus_percent and bonus_fixed):
            raise ValueError('Укажите один бонус: процент до 100% или сумму в TON.')
    elif reward_type == 'multi':
        multi_reward = normalize_level_reward({'type':'multi_promo','components':data.get('components')})
        deposit = multi_reward['components'].get('deposit_bonus', {})
        bonus_percent=deposit.get('bonus_percent',0);bonus_fixed=deposit.get('bonus_fixed',0);min_deposit=deposit.get('min_deposit',0)
    else:
        raise ValueError('Выберите тип награды.')
    return (reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,bonus_percent,bonus_fixed,
            min_deposit,json.dumps(multi_reward,ensure_ascii=False) if multi_reward else '{}',gift_expires_days)


@app.get('/api/admin/freebets')
@admin_required
def admin_freebets():
    with connect() as db:
        rows = db.execute("""SELECT f.*,p.reward_type,p.amount,p.gift_name,p.gift_price,p.wager_multiplier,
                             p.bonus_percent,p.bonus_fixed,p.min_deposit,p.reward_json,p.gift_expires_days
                             FROM freebets f JOIN promo_codes p ON p.code=f.promo_code
                             ORDER BY f.created_at DESC""").fetchall()
    items=[]
    for x in rows:
        promo=x
        items.append(dict(code=x['code'],link=freebet_link(x['code']),active=bool(x['active']),max_uses=int(x['max_uses'] or 0),
                          uses_count=int(x['uses_count'] or 0),require_subscription=bool(x['require_subscription']),
                          min_level=int(x['min_level'] or 0),min_telegram_level=int(x['min_telegram_level'] or 0),
                          min_turnover=int(x['min_turnover'] or 0)/100,expires_at=x['expires_at'],
                          created_at=x['created_at'],reward_type=x['reward_type'],purpose=promo_purpose(promo)))
    return jsonify(items=items, channel=post_channel_settings(), bot_username=bot_username_value())


@app.post('/api/admin/freebets')
@admin_required
def admin_create_freebet():
    data = request.get_json(silent=True) or {}
    code = str(data.get('code') or '').strip().upper() or ('FB_' + secrets.token_hex(4).upper())
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Код: 3–32 символа, только A-Z, 0-9, _ и -.')
    try:
        def _fb_int(value, default=0):
            text = str(value if value is not None else '').strip().replace(',', '.')
            return int(float(text)) if text else default
        max_uses=_fb_int(data.get('max_uses'), 1); min_level=_fb_int(data.get('min_level'))
        min_tg=_fb_int(data.get('min_telegram_level')); min_turnover=parse_amount(data.get('min_turnover') or 0)
        expires_days=_fb_int(data.get('expires_in_days'))
        values=_freebet_backing_values(data, code)
    except (ValueError,TypeError,InvalidOperation,OSError,json.JSONDecodeError) as exc:
        return error(str(exc) or 'Проверьте настройки фрибета.')
    if not 0 <= max_uses <= 1000000:return error('Лимит активаций: 0–1 000 000. 0 — без лимита.')
    if min_level < 0 or min_tg < 0:return error('Минимальные уровни не могут быть отрицательными.')
    if min_level:
        with connect() as check_db:
            if not check_db.execute('SELECT 1 FROM levels WHERE level=?',(min_level,)).fetchone():
                return error('Укажите существующий уровень GemDrop.')
    if not 0 <= expires_days <= 3650:return error('Срок действия: 0–3650 дней.')
    require_subscription=1 if bool(data.get('require_subscription')) else 0
    if require_subscription and not post_channel_settings().get('chat_id'):
        return error('Сначала привяжите канал в разделе Post или отключите требование подписки.',409)
    expires_at=(datetime.now(timezone.utc)+timedelta(days=expires_days)).isoformat() if expires_days else None
    (reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,bonus_percent,bonus_fixed,
     min_deposit,reward_json,gift_expires_days)=values
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM promo_codes WHERE code=?', (code,)).fetchone() or db.execute('SELECT 1 FROM freebets WHERE code=?',(code,)).fetchone():
            return error('Такой код уже существует.',409)
        db.execute("""INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,
                    max_uses,uses_count,active,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,assigned_user_id,
                    source_label,description,expires_at,gift_expires_days)
                    VALUES(?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,0,'Freebet','',?,?)""",
                   (code,reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,session['uid'],
                    bonus_percent,bonus_fixed,min_deposit,reward_json,expires_at,gift_expires_days))
        db.execute("""INSERT INTO freebets(code,promo_code,max_uses,active,require_subscription,min_level,min_telegram_level,
                    min_turnover,expires_at,created_by) VALUES(?,?,?,1,?,?,?,?,?,?)""",
                   (code,code,max_uses,require_subscription,min_level,min_tg,min_turnover,expires_at,session['uid']))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'freebet_create',code))
        db.commit()
    finally:
        db.close()
    return jsonify(ok=True,code=code,link=freebet_link(code))


@app.post('/api/admin/freebets/<code>/toggle')
@admin_required
def admin_toggle_freebet(code):
    code=str(code).upper()
    with connect() as db:
        row=db.execute('SELECT active FROM freebets WHERE code=?',(code,)).fetchone()
        if not row:return error('Фрибет не найден.',404)
        active=0 if row['active'] else 1
        db.execute('UPDATE freebets SET active=? WHERE code=?',(active,code))
    return jsonify(ok=True,active=bool(active))


@app.delete('/api/admin/freebets/<code>')
@admin_required
def admin_delete_freebet(code):
    code=str(code).upper()
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT uses_count FROM freebets WHERE code=?',(code,)).fetchone()
        if not row:return error('Фрибет не найден.',404)
        if int(row['uses_count'] or 0)>0:
            db.execute('UPDATE freebets SET active=0 WHERE code=?',(code,))
            db.commit()
            return jsonify(ok=True,disabled=True)
        db.execute('DELETE FROM freebet_redemptions WHERE code=?',(code,))
        db.execute('DELETE FROM freebets WHERE code=?',(code,))
        db.execute("DELETE FROM promo_codes WHERE code=? AND source_label='Freebet'",(code,))
        db.commit()
        return jsonify(ok=True,deleted=True)
    finally:db.close()


@app.get('/api/admin/promocodes')
@admin_required
def admin_promocodes():
    with connect() as db:
        rows = db.execute("SELECT * FROM promo_codes WHERE source_label<>'Freebet' ORDER BY created_at DESC,code DESC LIMIT 300").fetchall()
    return jsonify(items=[dict(code=x['code'], reward_type=x['reward_type'], amount=(x['amount'] if x['reward_type']=='tickets' else x['amount']/100), tickets=(int(x['amount'] or 0) if x['reward_type']=='tickets' else 0),
                               gift_id=x['gift_id'], gift_name=x['gift_name'], image_url=x['gift_image_url'],
                               gift_price=x['gift_price']/100, wager_multiplier=float(x['wager_multiplier'] or 0),
                               max_uses=x['max_uses'], uses_count=x['uses_count'],
                               active=bool(x['active']), created_at=x['created_at'],
                               assigned_user_id=int(x['assigned_user_id'] or 0),source=x['source_label'] or '',
                               description=x['description'] or '',expires_at=x['expires_at'],expired=promo_is_expired(x),gift_expires_days=int(x['gift_expires_days'] or 0),
                               bonus_percent=float(x['bonus_percent'] or 0),bonus_fixed=x['bonus_fixed']/100,
                               min_deposit=x['min_deposit']/100,purpose=promo_purpose(x),
                               components=public_level_reward(json.loads(x['reward_json'])).get('components',{})
                               if x['reward_type']=='multi' else {},
                               bundle_items=(json.loads(x['reward_json'] or '{}').get('items',[]) if x['reward_type']=='multi' else [])) for x in rows])


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
    amount = 0; gift_id = ''; gift_name = ''; gift_image = ''; gift_price = 0; wager_multiplier = 0.0; gift_expires_days = 0
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
    elif reward_type == 'tickets':
        try: amount=int(data.get('tickets') or data.get('amount') or 0)
        except (TypeError,ValueError): return error('Укажите количество билетов.')
        if not 1<=amount<=1000000:return error('Количество билетов: от 1 до 1 000 000.')
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
            try:
                gift_expires_days=int(data.get('gift_expires_days') or 0)
            except (TypeError,ValueError):
                return error('Срок жизни подарка указан неверно.')
            if not 0<=gift_expires_days<=3650:
                return error('Срок жизни подарка: от 0 до 3650 дней.')
    elif reward_type=='multi':
        try:
            if isinstance(data.get('bundle_items'),list):
                multi_reward=promo_bundle_payload(data.get('bundle_items'))
                deposit=promo_bundle_deposit(multi_reward)
            else:
                multi_reward=normalize_level_reward({'type':'multi_promo','components':data.get('components')})
                deposit=multi_reward['components'].get('deposit_bonus',{})
        except (ValueError,TypeError,InvalidOperation) as exc:return error(str(exc))
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
            db.execute('INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,assigned_user_id,source_label,description,expires_at,gift_expires_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (code, reward_type, amount, gift_id, gift_name, gift_image, gift_price,
                        wager_multiplier, max_uses, session['uid'],
                        bonus_percent,bonus_fixed,min_deposit,
                        json.dumps(multi_reward,ensure_ascii=False) if multi_reward else '{}',
                        assigned_user_id,source_label,description,expires_at,gift_expires_days))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'promo_create', code))
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return error('Такой промокод уже существует.', 409)
        raise
    if assigned_user_id:
        notify_promo_async(assigned_user_id, code, 'bonuses')
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
            users = db.execute('''SELECT u.id,u.name,u.username,u.photo_url,u.balance,COUNT(i.id) AS gifts
                                  FROM users u LEFT JOIN inventory i ON i.user_id=u.id
                                  WHERE CAST(u.id AS TEXT) LIKE ? OR u.username LIKE ? OR u.name LIKE ?
                                  GROUP BY u.id ORDER BY u.id DESC LIMIT 50''',
                               (f'%{term}%', f'%{term}%', f'%{term}%')).fetchall()
        else:
            users = db.execute('''SELECT u.id,u.name,u.username,u.photo_url,u.balance,COUNT(i.id) AS gifts
                                  FROM users u LEFT JOIN inventory i ON i.user_id=u.id
                                  GROUP BY u.id ORDER BY u.id DESC LIMIT 50''').fetchall()
    return jsonify(users=[dict(id=u['id'], name=u['name'], username=u['username'], photo_url=u['photo_url'] or '',
                               balance=u['balance']/100, gifts=u['gifts']) for u in users])


@app.get('/api/admin/users/<int:user_id>')
@admin_required
def admin_user(user_id):
    with connect() as db:
        user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            return error('Пользователь не найден.', 404)
        purge_expired_inventory(db, user_id)
        items = db.execute('SELECT * FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 200',
                           (user_id,)).fetchall()
        promos = db.execute('SELECT * FROM promo_codes WHERE assigned_user_id=? ORDER BY created_at DESC LIMIT 20',
                            (user_id,)).fetchall()
        level=level_number(db,int(user['turnover_cents'] or 0))
        available_levels=[int(r['level']) for r in db.execute('SELECT level FROM levels ORDER BY level').fetchall()]
    return jsonify(user=dict(id=user['id'], name=user['name'], username=user['username'], photo_url=user['photo_url'] or '',
                             balance=user['balance']/100,level=level,
                             turnover=user['turnover_cents']/100,
                             withdrawal_enabled=bool(user['withdrawal_enabled']),
                             withdrawal_block_reason=user['withdrawal_block_reason'] or '',
                             max_drop_override=(dict(name=user['max_drop_override_name'],image_url=user['max_drop_override_image'],
                                                     price_ton=int(user['max_drop_override_price'] or 0)/100,
                                                     set_at=user['max_drop_override_set_at'])
                                                if int(user['max_drop_override_price'] or 0)>0 and user['max_drop_override_name'] else None)),items=[inventory_item(x) for x in items],
                   available_levels=available_levels,
                   promos=[dict(code=p['code'],purpose=promo_purpose(p),expired=promo_is_expired(p),
                                active=bool(p['active'] and not promo_is_expired(p)),
                                used=bool(p['uses_count'])) for p in promos])


@app.post('/api/admin/users/<int:user_id>/max-drop')
@admin_required
def admin_user_max_drop(user_id):
    data = request.get_json(silent=True) or {}
    clear = bool(data.get('clear'))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        if clear:
            db.execute('''UPDATE users SET max_drop_override_name='',max_drop_override_image='',
                          max_drop_override_price=0,max_drop_override_set_at=NULL WHERE id=?''', (user_id,))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], user_id, 'max_drop_override', 'clear'))
            db.commit()
            return jsonify(ok=True, max_drop=None)
        gift_id = str(data.get('gift_id') or '').strip()
        try:
            gifts = read_catalog().get('gifts', [])
        except Exception:
            gifts = []
        gift = next((g for g in gifts if str(g.get('id')) == gift_id), None)
        if not gift:
            return error('Подарок не найден в каталоге Portal.', 404)
        try:
            price = ton_to_cents(gift.get('price_ton'))
        except (ValueError, TypeError, InvalidOperation):
            return error('У подарка некорректная цена.')
        if price <= 0:
            return error('У подарка должна быть положительная цена.')
        name = str(gift.get('name') or 'Подарок')[:140]
        image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
        set_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        db.execute('''UPDATE users SET max_drop_override_name=?,max_drop_override_image=?,
                      max_drop_override_price=?,max_drop_override_set_at=? WHERE id=?''',
                   (name, image, price, set_at, user_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'max_drop_override', f'{gift_id}:{name}:{price}'))
        db.commit()
    return jsonify(ok=True, max_drop=dict(name=name,image_url=image,price_ton=price/100,set_at=set_at))


@app.post('/api/admin/users/<int:user_id>/withdrawal-access')
@admin_required
def admin_user_withdrawal_access(user_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled'))
    reason = str(data.get('reason') or '').strip()[:240]
    if not enabled and not reason:
        reason = 'Вывод для вашего аккаунта временно недоступен. Обратитесь в поддержку.'
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        db.execute('UPDATE users SET withdrawal_enabled=?,withdrawal_block_reason=? WHERE id=?',
                   (1 if enabled else 0, '' if enabled else reason, user_id))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'withdrawal_access', 'enabled' if enabled else reason))
        log_event(db, user_id, 'withdrawal_access', enabled=enabled, reason='' if enabled else reason,
                  admin_id=session['uid'])
        db.commit()
    if enabled:
        notify_user_async(user_id, '✅ <b>Вывод подарков доступен</b>', miniapp_markup('Открыть', 'profile'), 'HTML')
    else:
        notify_user_async(user_id, f'⚠️ <b>Вывод временно недоступен</b>\n\n{escape(reason)}', miniapp_markup('Открыть', 'profile'), 'HTML')
    return jsonify(ok=True, enabled=enabled, reason='' if enabled else reason)


@app.post('/api/admin/users/<int:user_id>/promocodes/new')
@admin_required
def admin_create_user_promocode(user_id):
    data = request.get_json(silent=True) or {}
    kind = str(data.get('reward_type') or '')
    if kind not in ('balance','tickets','deposit_bonus','gift','wager_gift','multi'):
        return error('Выберите награду личного промокода.')
    code = str(data.get('code') or '').strip().upper()
    if code and not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Код: от 3 до 32 символов, латинские буквы, цифры, _ или -.')
    try:
        days = int(data.get('expires_in_days') or 0)
        if not 0 <= days <= 3650:raise ValueError()
    except (TypeError,ValueError):
        return error('Срок действия: от 0 до 3650 дней.')
    amount=gift_price=min_deposit=gift_expires_days=bonus_fixed=0
    gift_id=gift_name=gift_image=''
    bonus_percent=wager_multiplier=0.0
    multi_reward=None
    if kind=='balance':
        try:amount=parse_amount(data.get('amount'))
        except (ValueError,TypeError,InvalidOperation):return error('Укажите сумму TON с точностью до 0.01.')
        if not 1<=amount<=100000000:return error('Сумма: от 0.01 до 1 000 000 TON.')
    elif kind=='tickets':
        try:amount=int(data.get('tickets') or data.get('amount') or 0)
        except (ValueError,TypeError):return error('Укажите количество билетов.')
        if not 1<=amount<=1000000:return error('Количество билетов: от 1 до 1 000 000.')
    elif kind=='deposit_bonus':
        try:
            bonus_percent=float(data.get('bonus_percent') or 0)
            min_deposit=parse_amount(data.get('min_deposit') or 0)
        except (ValueError,TypeError,InvalidOperation):return error('Проверьте процент и минимальный депозит.')
        if not math.isfinite(bonus_percent) or not 0<bonus_percent<=100 or not 0<=min_deposit<=100000000:
            return error('Бонус от 0.1 до 100%; депозит от 0 до 1 000 000 TON.')
    elif kind=='multi':
        try:
            multi_reward=promo_bundle_payload(data.get('bundle_items'))
        except (ValueError,TypeError,InvalidOperation) as exc:
            return error(str(exc))
        deposit=promo_bundle_deposit(multi_reward)
        bonus_percent=float(deposit.get('bonus_percent') or 0)
        bonus_fixed=int(deposit.get('bonus_fixed') or 0)
        min_deposit=int(deposit.get('min_deposit') or 0)
    else:
        gift_id=str(data.get('gift_id') or '')
        try:gift=next((g for g in read_catalog().get('gifts',[]) if str(g.get('id'))==gift_id),None)
        except (OSError,ValueError,json.JSONDecodeError):gift=None
        if not gift:return error('Подарок не найден в каталоге Portal.')
        gift_name=str(gift.get('name') or 'Подарок')[:140]
        gift_image=safe_image(gift.get('image_url') or gift.get('portal_image_url'))
        try:gift_price=ton_to_cents(gift.get('price_ton'))
        except (ValueError,TypeError,InvalidOperation):return error('Цена подарка не задана.')
        if gift_price<=0:return error('Цена подарка не задана.')
        if kind=='wager_gift':
            try:wager_multiplier=float(data.get('wager_multiplier') or 0)
            except (ValueError,TypeError):return error('Укажите X отыгрыша.')
            if not math.isfinite(wager_multiplier) or not 1<=wager_multiplier<=1000:
                return error('X отыгрыша: от 1 до 1000.')
            try:
                gift_expires_days=int(data.get('gift_expires_days') or 0)
            except (TypeError,ValueError):
                return error('Срок жизни подарка указан неверно.')
            if not 0<=gift_expires_days<=3650:
                return error('Срок жизни подарка: от 0 до 3650 дней.')
    expires_at=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat() if days else None
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=?',(user_id,)).fetchone():
            return error('Пользователь не найден.',404)
        code=code or unique_promo_code(db,'PERS')
        db.execute('''INSERT INTO promo_codes(
                      code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,
                      max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,
                      assigned_user_id,source_label,description,expires_at,gift_expires_days)
                      VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)''',
                   (code,kind,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,
                    session['uid'],bonus_percent,bonus_fixed,min_deposit,json.dumps(multi_reward,ensure_ascii=False) if multi_reward else '{}',user_id,'Администрация','',expires_at,gift_expires_days))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'promo_issue',code))
        log_event(db,user_id,'promo_issued',code=code,source='Администрация')
        db.commit()
        notify_promo_async(user_id, code, 'bonuses')
        return jsonify(ok=True,code=code)
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return error('Такой код уже существует. Введите другой.',409)
        app.logger.exception('Не удалось создать личный промокод')
        return error('Не удалось выдать промокод. Повторите попытку.',500)
    finally:
        db.close()


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
            assigned_user_id,source_label,description,expires_at,gift_expires_days)
            VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)''',
            (issued_code,template['reward_type'],template['amount'],template['gift_id'],
             template['gift_name'],template['gift_image_url'],template['gift_price'],
             template['wager_multiplier'],session['uid'],template['bonus_percent'],
             template['bonus_fixed'],template['min_deposit'],template['reward_json'],
             user_id,'Выдан администратором',promo_purpose(template),template['expires_at'],template['gift_expires_days']))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'promo_issue',f'{code} → {issued_code}'))
        log_event(db,user_id,'promo_issued',code=issued_code,source='Администрация')
        db.commit()
        notify_promo_async(user_id, issued_code, 'bonuses')
        return jsonify(ok=True,code=issued_code)
    finally:
        db.close()


@app.post('/api/admin/users/<int:user_id>/level')
@admin_required
def admin_user_level(user_id):
    data=request.get_json(silent=True) or {}
    try:level=int(data.get('level'))
    except (ValueError,TypeError):return error('Выберите существующий уровень.')
    if level<1:return error('Выберите существующий уровень.')
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        user=db.execute('SELECT turnover_cents FROM users WHERE id=?'+(' FOR UPDATE' if DATABASE_URL else ''),(user_id,)).fetchone()
        row=db.execute('SELECT required_turnover FROM levels WHERE level=?',(level,)).fetchone()
        if not user:return error('Пользователь не найден.',404)
        if not row:return error('Уровень не найден.',404)
        old=level_number(db,int(user['turnover_cents'] or 0))
        db.execute('UPDATE users SET turnover_cents=? WHERE id=?',(row['required_turnover'],user_id))
        reset_levels=[]
        if level < old:
            reset_levels=[int(x['level']) for x in db.execute(
                'SELECT level FROM level_claims WHERE user_id=? AND level>? ORDER BY level',
                (user_id,level)).fetchall()]
            if reset_levels:
                db.execute('DELETE FROM level_claims WHERE user_id=? AND level>?',(user_id,level))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'level_set',f'{old} → {level}; rewards reset: {reset_levels}'))
        log_event(db,user_id,'admin_level',previous_level=old,new_level=level,admin_id=session['uid'],
                  reset_rewards=reset_levels)
        db.commit()
    finally:db.close()
    return jsonify(ok=True,level=level,turnover=row['required_turnover']/100,reset_rewards=reset_levels)


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
        balance_now=int(db.execute('SELECT balance FROM users WHERE id=?',(user_id,)).fetchone()['balance'])
        db.commit()
        notify_deposit_async(user_id, amount, balance_now)
        return jsonify(ok=True, balance_added=amount/100, referral_bonus=0, balance=balance_now/100)
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
        log_event(db,user_id,'admin_gift_add',gift_name=str(gift['name']))
    return jsonify(ok=True)


@app.delete('/api/admin/users/<int:user_id>/inventory/<int:item_id>')
@admin_required
def admin_remove_inventory(user_id, item_id):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        item=db.execute('SELECT gift_name FROM inventory WHERE id=? AND user_id=?',(item_id,user_id)).fetchone()
        result = db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (item_id, user_id))
        if not result.rowcount:
            return error('Подарок не найден.', 404)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'gift_remove', str(item_id)))
        log_event(db,user_id,'admin_gift_remove',gift_name=item['gift_name'] if item else 'Подарок')
    return jsonify(ok=True, removed_id=item_id)


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
        notify_user_async(row['user_id'], f'✅ <b>Вывод выполнен</b>\n\n🎁 {escape(row["gift_name"])}',
                          miniapp_markup('Открыть', 'profile'), 'HTML')
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
        notify_user_async(row['user_id'], f'↩️ <b>Вывод отклонён</b>\n\n🎁 {escape(row["gift_name"])}\n\nПодарок возвращён в ваш инвентарь.',
                          miniapp_markup('Открыть', 'profile'), 'HTML')
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
                   upgrade_rtp=upgrade_rtp_basis_points()/100,
                   loss_rtp_max_boost=round(loss_rtp_max_boost(),2),mode='global')


@app.post('/api/admin/rtp')
@admin_required
def admin_rtp_set():
    data = request.get_json(silent=True) or {}
    try:
        percent = float(data.get('rtp'))
        promo_percent = float(data.get('promo_rtp', promo_game_rtp()*100))
        upgrade_percent = float(data.get('upgrade_rtp',upgrade_rtp_basis_points()/100))
        loss_boost = float(data.get('loss_rtp_max_boost', loss_rtp_max_boost()))
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
    if not math.isfinite(loss_boost) or not 0<=loss_boost<=15:
        return error('Максимальная прибавка RTP от игрового минуса: от 0 до 15 п.п.')
    save_document('game_settings', {'rtp': percent/100, 'promo_rtp': promo_percent/100,
                                    'upgrade_rtp_bp':round(upgrade_percent*100),
                                    'loss_rtp_max_boost':round(loss_boost,2),
                                    'updated_at': datetime.now(timezone.utc).isoformat(),
                                    'admin_id': session['uid']})
    return jsonify(ok=True, rtp=round(game_rtp()*100, 2), promo_rtp=round(promo_game_rtp()*100, 2),
                   upgrade_rtp=upgrade_rtp_basis_points()/100,
                   loss_rtp_max_boost=round(loss_rtp_max_boost(),2))


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
            credited_amount=int(fresh['amount'])
            credited_bonus_row=db.execute('SELECT balance FROM users WHERE id=?',(session['uid'],)).fetchone()
            balance_now=int(credited_bonus_row['balance']) if credited_bonus_row else 0
            promo_bonus_row=db.execute("SELECT COALESCE(SUM(amount),0) AS total FROM transactions WHERE user_id=? AND kind='deposit_promo_bonus' AND reference_type='ton_tx' AND reference_id=?",(session['uid'],tx_hash)).fetchone()
            promo_bonus=int(promo_bonus_row['total'] or 0) if promo_bonus_row else 0
            db.commit()
        finally:
            db.close()
        notify_deposit_async(session['uid'], credited_amount, balance_now, promo_bonus)
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



PORTAL_BACKGROUND_LABELS = {
    'black': 'Black',
    'onyx': 'Onyx Black',
    'onyxblack': 'Onyx Black',
}


def normalize_portal_background(value):
    text = str(value or '').strip()
    if not text:
        return None
    key = re.sub(r'[\W_]+', '', text.casefold(), flags=re.UNICODE)
    return PORTAL_BACKGROUND_LABELS.get(key)


def portal_background_key(label):
    if label == 'Black':
        return 'black'
    if label == 'Onyx Black':
        return 'onyx-black'
    return re.sub(r'[^a-z0-9]+', '-', str(label).casefold()).strip('-')


def portal_short_name(name):
    return str(name or '').replace(' ', '').replace("'", '').replace('’', '').replace('-', '').lower()


def portal_price_string(value):
    if isinstance(value, dict):
        for key in ('floor_price', 'floorPrice', 'min_price', 'minPrice', 'price', 'amount', 'value', 'floor'):
            if value.get(key) is not None:
                price = portal_price_string(value.get(key))
                if price is not None:
                    return price
        for key in ('stats', 'market_stats', 'pricing'):
            if isinstance(value.get(key), dict):
                price = portal_price_string(value[key])
                if price is not None:
                    return price
        return None
    try:
        price = Decimal(str(value))
        if price.is_finite() and price >= 0:
            return format(price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def portal_variant_name(obj):
    if isinstance(obj, str):
        return normalize_portal_background(obj)
    if not isinstance(obj, dict):
        return None
    for key in ('background', 'background_name', 'backgroundName', 'backdrop', 'backdrop_name',
                'backdropName', 'name', 'title', 'label', 'value'):
        value = obj.get(key)
        if isinstance(value, dict):
            found = portal_variant_name(value)
        else:
            found = normalize_portal_background(value)
        if found:
            return found
    return None


def portal_background_variants(item, filters=None):
    """Return supported background price variants embedded in a Portal collection row.

    Portal may expose background/backdrop floors in several shapes depending on
    endpoint/version, for example:
    {"backgrounds": [{"name": "Black", "floor_price": 20000}]},
    {"backdrop_prices": {"Onyx Black": 10000}}, or attribute-style arrays.
    We keep the parser deliberately permissive but only accept the two supported
    labels, so ordinary fields cannot accidentally create extra gifts.
    """
    variants = {}

    def remember(label, raw_price):
        label = normalize_portal_background(label) or label
        if label not in ('Black', 'Onyx Black'):
            return
        price = portal_price_string(raw_price)
        if price is not None and Decimal(price) > 0:
            variants[label] = price

    def scan_container(container):
        if isinstance(container, list):
            for entry in container:
                scan_container(entry)
            return
        if not isinstance(container, dict):
            return

        trait = str(container.get('trait_type') or container.get('type') or container.get('key') or '').casefold()
        if any(word in trait for word in ('model', 'symbol', 'pattern')):
            return

        # Mapping shape: {"Black": 20000, "Onyx Black": {"floor_price": 10000}}
        for key, value in container.items():
            label = normalize_portal_background(key)
            if label:
                remember(label, value)

        # Object shape: {"name": "Black", "floor_price": 20000}
        label = portal_variant_name(container)
        if label:
            remember(label, container)

        # Attribute/trait shape: {"trait_type":"Backdrop", "value":"Black", "floor_price":...}
        trait = str(container.get('trait_type') or container.get('type') or container.get('key') or '').casefold()
        if any(word in trait for word in ('background', 'backdrop', 'фон')):
            label = normalize_portal_background(container.get('value') or container.get('name') or container.get('label'))
            if label:
                remember(label, container)

        for key, child in container.items():
            if key not in ('models', 'symbols', 'patterns') and isinstance(child, (dict, list)):
                scan_container(child)

    direct_keys = (
        'backgrounds', 'background', 'background_prices', 'backgroundPrices', 'prices_by_background',
        'floor_prices_by_background', 'floors_by_background', 'backdrops', 'backdrop', 'backdrop_prices',
        'backdropPrices', 'prices_by_backdrop', 'floor_prices_by_backdrop', 'floors_by_backdrop',
        'attributes', 'traits', 'filters', 'variants', 'prices', 'floors', 'stats', 'market_stats',
        'data', 'result', 'floor_prices', 'floorPrices',
    )
    for key in direct_keys:
        if isinstance(item, dict) and item.get(key) is not None:
            scan_container(item.get(key))
    if filters is not None:
        scan_container(filters)
    return variants


def portal_catalog_entries(base_gift, portal_item, previous_by_id, filters=None):
    entries = [base_gift]
    base_id = str(base_gift['id'])
    base_name = str(base_gift['name'])
    discovered = portal_background_variants(portal_item, filters)
    # A backdrop name or collection floor is not a quote for this variant.
    for label in ('Black', 'Onyx Black'):
        price = discovered.get(label)
        if not price or Decimal(price) <= 0:
            continue
        bg_key = portal_background_key(label)
        variant_id = f'{base_id}:background:{bg_key}'
        old = previous_by_id.get(variant_id, {})
        variant = dict(base_gift)
        variant.update(
            id=variant_id,
            base_id=base_id,
            base_name=base_name,
            name=f'{base_name} ({label})',
            price_ton=price,
            background_label=label,
            background_key=bg_key,
            background_tone='black' if label == 'Black' else 'onyx-black',
            price_source='Portal · фон',
            image_url=old.get('image_url') or base_gift.get('image_url') or base_gift.get('portal_image_url', ''),
            image_match=bool(old.get('image_match', base_gift.get('image_match'))),
            telegram_gift_id=old.get('telegram_gift_id', base_gift.get('telegram_gift_id', '')),
        )
        entries.append(variant)
    return entries


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
                    timeout=(3, 6),
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


def portal_get_collection_filters(session_http, key, names, deadline=None):
    """Best-effort background floors. Never let this optional request freeze Portal import."""
    short_names = []
    seen_names = set()
    for name in names:
        short = portal_short_name(name)
        if short and short not in seen_names:
            seen_names.add(short)
            short_names.append(short)
        if len(short_names) >= 40:
            # Keep the URL short and the Portal filters endpoint responsive. The
            # main collection page is still imported fully even if backgrounds are
            # only discovered for part of the page.
            break
    if not short_names:
        return {}
    if deadline is not None and time.monotonic() > deadline - 4:
        return {}
    params = {'short_names': ','.join(short_names)}
    url = 'https://portal-market.com/api/collections/filters'
    auth_candidates = [str(key or '').strip()]
    if auth_candidates[0]:
        auth_candidates.append('')
    for auth_key in auth_candidates:
        if deadline is not None and time.monotonic() > deadline - 3:
            return {}
        try:
            response = session_http.get(url, params=params, headers=portal_headers(auth_key), timeout=(1.5, 2.5))
            if response.status_code in (401, 403, 404):
                continue
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            # Filters are optional: if Portal does not answer, continue with the
            # ordinary gifts and any background data embedded into collections.
            return {}
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            payload = payload['data']
        floors = payload.get('collections', payload.get('floor_prices', payload.get('floorPrices', payload))) if isinstance(payload, dict) else {}
        if not isinstance(floors, dict):
            return {}
        result = {}
        for key_name, value in floors.items():
            result[portal_short_name(key_name)] = value
        return result
    return {}


def fetch_portal_catalog(key, progress=None):
    """Fetch Portal collections without blocking the admin UI for the whole import."""
    previous = read_catalog(include_hidden=True)['gifts']
    previous_by_id = {str(gift.get('id')): gift for gift in previous}
    gifts, seen = [], set()
    offset = 0
    session_http = requests.Session()
    page_signatures = set()
    request_limit = 80
    deadline = time.monotonic() + 42

    # The public collections endpoint is also used by the Portals web app. Some
    # deployments cap a page below the requested limit, so advance by the real
    # number of rows instead of assuming a fixed page size.
    for page in range(30):
        if time.monotonic() >= deadline:
            append_portal_log('Импорт остановлен по защитному лимиту времени; уже полученные коллекции сохранены.', 'error')
            break
        response = portal_get_collections(
            session_http, key, {
                'limit': request_limit, 'offset': offset,
                # Unsupported params are ignored by Portal, but newer responses can
                # include background/backdrop floor prices without extra requests.
                'include': 'backgrounds,backdrops', 'with': 'backgrounds,backdrops',
            }
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

        page_filters = portal_get_collection_filters(
            session_http, key,
            [item.get('name') or item.get('title') or item.get('gift_name') for item in items if isinstance(item, dict)],
            deadline=deadline,
        )

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
            price = portal_price_string(raw)
            img = next((safe_image(item.get(k)) for k in
                        ('image_url', 'photo_url', 'preview_url', 'image', 'icon_url', 'png_url')
                        if safe_image(item.get(k))), '')
            gift = dict(id=gift_id, name=str(name)[:140], price_ton=price, portal_image_url=img)
            old = previous_by_id.get(gift_id, {})
            if price is None or Decimal(price) <= 0:
                saved_price = portal_price_string(old.get('price_ton'))
                if saved_price and Decimal(saved_price) > 0:
                    gift['price_ton'] = saved_price
            gift.update(image_url=old.get('image_url') or img,
                        image_match=bool(old.get('image_match')),
                        telegram_gift_id=old.get('telegram_gift_id', ''))
            entries = portal_catalog_entries(gift, item, previous_by_id, page_filters.get(portal_short_name(name)))
            for entry in entries:
                entry_id = str(entry.get('id'))
                if entry_id in seen and entry_id != gift_id:
                    continue
                seen.add(entry_id)
                gifts.append(entry)

        if not added:
            break
        offset += len(items)

        # Publish a partial catalog after every page. The admin panel can show
        # gifts immediately instead of appearing frozen until PNG matching ends.
        partial = gifts + [g for g in previous if str(g.get('id')) not in seen
                           and not (g.get('background_label') and str(g.get('base_id')) in seen)]
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

    retained = [g for g in previous if str(g.get('id')) not in seen
                and not (g.get('background_label') and str(g.get('base_id')) in seen)]
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
        document = read_catalog(include_hidden=True)
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
    configured = str((read_document('bot_settings') or {}).get('welcome_text') or '')
    if configured:
        return custom_emoji_html(configured.replace('{referral_percent}', pct_text))
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
    callback = update.get('callback_query') or {}
    if callback and isinstance((callback.get('from') or {}).get('id'), int):
        callback_id = str(callback.get('id') or '')
        callback_data = str(callback.get('data') or '')
        uid = int(callback['from']['id'])
        if callback_data in ('admin:emoji', 'admin:post'):
            if uid not in ADMIN_IDS or ((callback.get('message') or {}).get('chat') or {}).get('type') != 'private':
                return jsonify(method='answerCallbackQuery', callback_query_id=callback_id, text='Нет доступа.')
            try:
                telegram_api('answerCallbackQuery', {'callback_query_id': callback_id})
            except RuntimeError:
                pass
            command_message = {'from': {'id': uid}, 'chat': {'type': 'private'},
                               'text': '/emoji' if callback_data == 'admin:emoji' else '/post'}
            return jsonify(**handle_admin_emoji_message(command_message))
        if callback_data.startswith('start:'):
            try:
                telegram_api('answerCallbackQuery', {'callback_query_id': callback_id, 'text': 'Готово'})
            except RuntimeError:
                pass
            return jsonify(ok=True)
        if callback_data.startswith('freebet_check:'):
            code = callback_data.split(':',1)[1].strip().upper()
            try:
                result = try_activate_freebet(uid, code)
                try:
                    telegram_api('answerCallbackQuery', {'callback_query_id': callback_id,
                                                         'text': 'Проверено' if result['status']=='ok' else result['text'][:180],
                                                         'show_alert': result['status'] not in ('ok','subscription')})
                except RuntimeError:
                    pass
                msg = callback.get('message') or {}
                target_chat = (msg.get('chat') or {}).get('id') or uid
                payload={'chat_id':target_chat,'text':result['text']}
                if result.get('parse_mode'):payload['parse_mode']=result['parse_mode']
                if result.get('reply_markup'):payload['reply_markup']=result['reply_markup']
                telegram_api('sendMessage', payload)
                return jsonify(ok=True)
            except Exception:
                app.logger.exception('Freebet callback failed')
                try:telegram_api('answerCallbackQuery', {'callback_query_id':callback_id,'text':'Не удалось проверить фрибет. Повторите попытку.','show_alert':True})
                except RuntimeError:pass
                return jsonify(ok=True)
        if callback_data.startswith('post:'):
            try:telegram_api('answerCallbackQuery', {'callback_query_id':callback_id,'text':'Готово'})
            except RuntimeError:pass
            return jsonify(ok=True)
        return jsonify(ok=True)
    emoji_reply = handle_admin_emoji_message(message)
    if emoji_reply is not None:
        return jsonify(**emoji_reply)
    if (chat.get('type') == 'private' and command and
            command[0].split('@')[0].lower() in ('/auf','auf') and isinstance(sender.get('id'), int)):
        code = command[1].strip().upper() if len(command) == 2 else ''
        uid = sender['id']
        success = False
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('INSERT OR IGNORE INTO bot_updates(update_id) VALUES(?)',
                              (int(update['update_id']),)).rowcount:
                db.commit()
                return jsonify(ok=True)
            if re.fullmatch(r'[A-HJ-NP-Z2-9]{12}', code):
                db.execute('''INSERT OR IGNORE INTO users(id,name,username,balance) VALUES(?,?,?,0)''',
                           (uid,str(sender.get('first_name') or 'Игрок')[:80],
                            str(sender.get('username') or '')[:80]))
                db.execute('UPDATE users SET name=?,username=? WHERE id=?',
                           (str(sender.get('first_name') or 'Игрок')[:80],
                            str(sender.get('username') or '')[:80],uid))
                success = bool(db.execute('''UPDATE web_login_challenges SET user_id=?
                    WHERE code_hash=? AND user_id=0 AND used_at=0 AND expires_at>?''',
                    (uid,web_login_hash(code),int(time.time()))).rowcount)
            db.commit()
        return jsonify(method='sendMessage',chat_id=chat['id'],
                       text=('✅ Вход подтверждён. Вернитесь на страницу GemDrop.' if success else
                             'Код неверный или срок его действия истёк. Получите новый код на сайте.'))
    if (chat.get('type') != 'private' or not command or
            command[0].split('@')[0] != '/start' or not isinstance(sender.get('id'), int)):
        return jsonify(ok=True)
    try:
        update_id = int(update['update_id'])
        uid = sender['id']
        referrer = None
        freebet_code = None
        if len(command) == 2 and re.fullmatch(r'ref_[0-9]{1,20}', command[1]):
            referrer = int(command[1][4:])
        elif len(command) == 2 and re.fullmatch(r'freebet_[A-Za-z0-9_-]{3,32}', command[1], re.I):
            freebet_code = command[1][8:].upper()
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
        if freebet_code:
            try:
                result = try_activate_freebet(uid, freebet_code)
                payload = dict(method='sendMessage', chat_id=chat['id'], text=result['text'])
                if result.get('reply_markup'): payload['reply_markup'] = result['reply_markup']
                if result.get('parse_mode'): payload['parse_mode'] = result['parse_mode']
                return jsonify(**payload)
            except Exception:
                app.logger.exception('Freebet activation failed for %s', uid)
                return jsonify(method='sendMessage', chat_id=chat['id'],
                               text='Не удалось активировать фрибет. Попробуйте ещё раз через несколько секунд.')
        button = build_start_keyboard(uid, referrer)
        save_document('bot_input:' + str(uid), {'mode': ''})
        # /start intentionally uses the same sendMessage + HTML path that is
        # now also used by ordinary publications with premium emoji.
        return jsonify(method='sendMessage', chat_id=chat['id'], text=welcome_text(),
                       reply_markup=button, parse_mode='HTML')
    except (ValueError, KeyError, sqlite3.Error) as exc:
        app.logger.warning('Telegram update failed: %s', type(exc).__name__)
        if isinstance(update.get('update_id'), int):
            with connect() as db:
                db.execute('DELETE FROM bot_updates WHERE update_id=?', (update['update_id'],))
        return error('Не удалось обработать команду.', 500)


def database_busy(exc):
    app.logger.warning('Database temporarily unavailable: %s', type(exc).__name__)
    response = jsonify(error='Сервер занят. Повторите запрос через несколько секунд.')
    response.status_code = 503
    response.headers['Retry-After'] = '2'
    return response


@app.errorhandler(sqlite3.OperationalError)
def sqlite_error(exc):
    if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
        return database_busy(exc)
    return internal_error_handler(exc)


if DATABASE_URL:
    import psycopg
    from psycopg_pool import PoolTimeout, TooManyRequests
    for transient_error in (PoolTimeout, TooManyRequests, psycopg.OperationalError):
        app.register_error_handler(transient_error, database_busy)


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


def singleton_background(target, lock_id):
    if not DATABASE_URL:
        target()
        return
    import psycopg
    while True:
        try:
            # A dedicated connection owns the lock without occupying the HTTP pool.
            with psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=5) as guard:
                if guard.execute('SELECT pg_try_advisory_lock(%s)', (lock_id,)).fetchone()[0]:
                    target()
                    return
        except Exception:
            app.logger.exception('Background leader failed: %s', target.__name__)
        time.sleep(15)


def start_background(target, lock_id):
    if os.environ.get('ENABLE_BACKGROUND_JOBS', '1') == '1':
        Thread(target=singleton_background, args=(target, lock_id), daemon=True).start()


start_background(portal_auto_loop, 660101)
if BOT_TOKEN:
    start_background(activity_notification_loop, 660102)


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
                                           'allowed_updates': ['message', 'callback_query'],
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
    start_background(configure_bot, 660103)


if os.environ.get('RUN_LEGACY_REPAIR', '1') == '1':
    repair_legacy_upgrade_wagers()
start_background(log_pruner_loop, 660104)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
