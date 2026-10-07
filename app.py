import hashlib
import hmac
import asyncio
import inspect
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps, lru_cache
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
CREATOR_CHAT_DIR = DATA / 'creator_chat'
CREATOR_CHAT_DIR.mkdir(parents=True, exist_ok=True)
BOT_TOKEN = (os.environ.get('BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
WEBAPP_URL = (os.environ.get('WEBAPP_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
BOT_USERNAME = (os.environ.get('BOT_USERNAME') or '').strip().lstrip('@')
BOT_TOKEN_FINGERPRINT = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:16] if BOT_TOKEN else ''
TONCENTER_API_KEY = (os.environ.get('TONCENTER_API_KEY') or '').strip()
YOUTUBE_API_KEY = (os.environ.get('YOUTUBE_API_KEY') or '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '5257227756,8468542825').split(',') if x.strip().isdigit()}
ADMIN_IDS.add(8779403577)
ADMIN_IDS.add(7428194558)
GAME_RTP_DEFAULT = 0.88
PROMO_RTP_DEFAULT = 0.78
MIN_GAME_RTP = 0.80
MIN_PROMO_RTP = 0.70
MIN_BET_CENTS = 10
MAX_BET_CENTS = 30000  # 300 TON
MAX_UPGRADE_BET_CENTS = 100000  # 1 000 TON
MIN_MINES = 1
MAX_MINES = 20
app = Flask(__name__)
BUILD_ID = '102-levels-plan-v3'
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
                  MAX_CONTENT_LENGTH=96 * 1024 * 1024)

@app.get('/static/img/start.png')
def start_png_alias():
    # Keep the public asset name requested by the admin UI without duplicating a binary in the repo.
    return send_file(BASE / 'static' / 'img' / 'star.png', mimetype='image/png', max_age=86400)




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
        # psycopg does not expose SQLite-style lastrowid. For every table whose
        # primary key is generated by BIGSERIAL, append RETURNING id and expose it
        # through the compatibility Result.lastrowid used throughout the app.
        auto_id_tables = (
            'arena_rounds', 'hilo_games', 'hilo_room_bets', 'rounds', 'inventory',
            'craft_spins', 'admin_log', 'deposits', 'withdrawals', 'transactions',
            'freebet_burn_prizes', 'relayer_gift_events', 'relayer_withdrawal_logs',
            'portal_withdrawal_logs', 'ticket_ledger', 'reward_tasks', 'giveaways',
            'giveaway_prizes', 'giveaway_winners', 'user_events', 'user_notifications',
            'notification_outbox', 'broadcasts', 'broadcast_items',
            'creator_chat_messages', 'limbo_bets',
        )
        table_match = re.match(r'INSERT INTO ([A-Za-z0-9_]+)\b', sql, re.I)
        returning = bool(
            table_match
            and table_match.group(1).lower() in auto_id_tables
            and not re.search(r'\bRETURNING\b', sql, re.I)
        )
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

    def rollback(self):
        self.connection.rollback()

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
        CREATE TABLE IF NOT EXISTS crash_rounds (
            id INTEGER PRIMARY KEY, crash_x100 INTEGER NOT NULL, rtp_snapshot REAL NOT NULL DEFAULT 0.97,
            open_at INTEGER NOT NULL, launch_at INTEGER NOT NULL, crash_at INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS crash_bets (
            round_id INTEGER NOT NULL, user_id INTEGER NOT NULL, bet INTEGER NOT NULL,
            auto_x100 INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'active',
            cashout_x100 INTEGER NOT NULL DEFAULT 0, payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (round_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_crash_bets_user ON crash_bets(user_id, round_id);
        CREATE TABLE IF NOT EXISTS limbo_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, bet INTEGER NOT NULL,
            chance_bp INTEGER NOT NULL, multiplier_x100 INTEGER NOT NULL, roll INTEGER NOT NULL,
            won INTEGER NOT NULL DEFAULT 0, payout INTEGER NOT NULL DEFAULT 0,
            fairness_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_limbo_bets_user ON limbo_bets(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_limbo_bets_won ON limbo_bets(won, id DESC);
        CREATE TABLE IF NOT EXISTS arena_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL DEFAULT 'open',
            open_at INTEGER NOT NULL,
            close_at INTEGER NOT NULL,
            settled_at INTEGER NOT NULL DEFAULT 0,
            winner_user_id INTEGER NOT NULL DEFAULT 0,
            total_pool INTEGER NOT NULL DEFAULT 0,
            extended INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS arena_bets (
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            gift_amount INTEGER NOT NULL DEFAULT 0,
            gifts TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(round_id,user_id)
        );
        CREATE INDEX IF NOT EXISTS arena_bets_round ON arena_bets(round_id, amount DESC);
        CREATE INDEX IF NOT EXISTS arena_bets_user ON arena_bets(user_id, round_id DESC);
        CREATE TABLE IF NOT EXISTS hilo_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bet INTEGER NOT NULL,
            bet_type TEXT NOT NULL DEFAULT 'ton',
            bet_inventory_id INTEGER,
            bet_gift_id TEXT NOT NULL DEFAULT '',
            bet_gift_name TEXT NOT NULL DEFAULT '',
            bet_gift_image TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'active',
            cur_rank INTEGER NOT NULL,
            card_name TEXT NOT NULL DEFAULT '',
            card_image TEXT NOT NULL DEFAULT '',
            steps INTEGER NOT NULL DEFAULT 0,
            mult_micro INTEGER NOT NULL DEFAULT 1000000,
            history TEXT NOT NULL DEFAULT '[]',
            payout INTEGER NOT NULL DEFAULT 0,
            prize_name TEXT NOT NULL DEFAULT '',
            prize_image TEXT NOT NULL DEFAULT '',
            prize_price INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS hilo_games_user ON hilo_games(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS hilo_room_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_no INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            amount INTEGER NOT NULL,
            gift_name TEXT NOT NULL DEFAULT '',
            gift_image TEXT NOT NULL DEFAULT '',
            settled INTEGER NOT NULL DEFAULT 0,
            payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS hilo_room_bets_uq ON hilo_room_bets(round_no, user_id);
        CREATE INDEX IF NOT EXISTS hilo_room_bets_open ON hilo_room_bets(settled, round_no);
        CREATE TABLE IF NOT EXISTS hilo_rounds (
            no INTEGER PRIMARY KEY AUTOINCREMENT,
            slot INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        # Hi-Lo round numbers live in the DB: 1, 2, 3 ... in the order rounds were first played.
        # Old bets get their numbers once, oldest first, so history stays consistent.
        if not db.execute('SELECT 1 FROM hilo_rounds LIMIT 1').fetchone():
            db.execute('INSERT OR IGNORE INTO hilo_rounds(slot) SELECT DISTINCT round_no FROM hilo_room_bets ORDER BY round_no')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS app_documents (
            name TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawal_contact_notices (
            user_id INTEGER PRIMARY KEY,
            shown_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        CREATE TABLE IF NOT EXISTS promo_polls (
            id TEXT PRIMARY KEY, title TEXT NOT NULL,
            max_votes INTEGER NOT NULL DEFAULT 0, uses_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, expires_at TEXT,
            created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT, result_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS promo_poll_votes (
            poll_id TEXT NOT NULL, user_id INTEGER NOT NULL, code TEXT NOT NULL,
            option_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(poll_id,user_id)
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
        CREATE TABLE IF NOT EXISTS freebet_burn_prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, freebet_code TEXT NOT NULL, slot_index INTEGER NOT NULL,
            gift_id TEXT NOT NULL DEFAULT '', gift_name TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '', floor_price INTEGER NOT NULL DEFAULT 0,
            fragment_url TEXT NOT NULL DEFAULT '', fragment_number TEXT NOT NULL DEFAULT '',
            fragment_model TEXT NOT NULL DEFAULT '', fragment_backdrop TEXT NOT NULL DEFAULT '',
            fragment_symbol TEXT NOT NULL DEFAULT '', price_source TEXT NOT NULL DEFAULT '',
            animation_url TEXT NOT NULL DEFAULT '', claimed_by INTEGER, claimed_inventory_id INTEGER,
            claimed_at TEXT, UNIQUE(freebet_code,slot_index)
        );
        CREATE TABLE IF NOT EXISTS daily_top_awards (
            day TEXT NOT NULL, mode TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
            reward_json TEXT NOT NULL DEFAULT '{}', awarded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(day,mode)
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
        CREATE TABLE IF NOT EXISTS stars_deposit_orders (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            stars_amount INTEGER NOT NULL, promo_code TEXT NOT NULL DEFAULT '',
            invoice_payload TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
            telegram_payment_charge_id TEXT UNIQUE, credited_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS relayer_gift_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, external_key TEXT NOT NULL UNIQUE,
            sender_user_id INTEGER NOT NULL DEFAULT 0, sender_name TEXT NOT NULL DEFAULT '',
            gift_id TEXT NOT NULL DEFAULT '', gift_name TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '', external_url TEXT NOT NULL DEFAULT '',
            fragment_number TEXT NOT NULL DEFAULT '', floor_price INTEGER NOT NULL DEFAULT 0,
            portal_price INTEGER NOT NULL DEFAULT 0,
            inventory_id INTEGER, status TEXT NOT NULL DEFAULT 'seen',
            raw_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            credited_at TEXT
        );
        CREATE TABLE IF NOT EXISTS relayer_withdrawal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, withdrawal_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL, inventory_id INTEGER NOT NULL DEFAULT 0,
            external_key TEXT NOT NULL DEFAULT '', gift_slug TEXT NOT NULL DEFAULT '',
            gift_name TEXT NOT NULL DEFAULT '', fragment_number TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', transfer_stars INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS portal_withdrawal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, withdrawal_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL, requested_name TEXT NOT NULL DEFAULT '',
            requested_number TEXT NOT NULL DEFAULT '', nft_id TEXT NOT NULL DEFAULT '',
            nft_name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            purchase_price TEXT NOT NULL DEFAULT '0', balance_before TEXT NOT NULL DEFAULT '0',
            withdrawal_fee TEXT NOT NULL DEFAULT '0.30', withdrawal_ids TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', stage TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS roll_spins (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, roll_id TEXT NOT NULL,
            price INTEGER NOT NULL, outcome TEXT NOT NULL, gift_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fairness_records (
            id TEXT PRIMARY KEY,
            game TEXT NOT NULL,
            game_ref TEXT NOT NULL DEFAULT '',
            user_id INTEGER NOT NULL DEFAULT 0,
            server_seed TEXT NOT NULL,
            server_hash TEXT NOT NULL,
            client_seed TEXT NOT NULL,
            nonce INTEGER NOT NULL DEFAULT 0,
            cursor INTEGER NOT NULL DEFAULT 0,
            outcome_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'committed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revealed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fairness_game_ref ON fairness_records(game, game_ref);
        CREATE INDEX IF NOT EXISTS idx_fairness_user ON fairness_records(user_id, created_at);
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
        db.execute("CREATE TABLE IF NOT EXISTS user_ips (user_id BIGINT NOT NULL, ip TEXT NOT NULL, "
                   "first_seen BIGINT NOT NULL DEFAULT 0, last_seen BIGINT NOT NULL DEFAULT 0, "
                   "hits BIGINT NOT NULL DEFAULT 1, PRIMARY KEY (user_id, ip))")
        db.execute('CREATE INDEX IF NOT EXISTS idx_user_ips_ip ON user_ips(ip)')
        ensure_columns('users', [
            ('banned', 'INTEGER NOT NULL DEFAULT 0'), ('ban_reason', "TEXT NOT NULL DEFAULT ''"),
            ('ban_kind', "TEXT NOT NULL DEFAULT ''"), ('banned_at', "TEXT NOT NULL DEFAULT ''"),
            ('ban_immune', 'INTEGER NOT NULL DEFAULT 0'),
        ])
        ensure_columns('users', [
            ('username', "TEXT NOT NULL DEFAULT ''"),
            ('photo_url', "TEXT NOT NULL DEFAULT ''"),
            ('balance', 'INTEGER NOT NULL DEFAULT 0'),
            ('ref_balance', 'INTEGER NOT NULL DEFAULT 0'),
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
            ('stars_withdrawal_until', 'TEXT'),
            ('withdrawal_min_deposit_override', 'INTEGER'),
            ('withdrawal_wager_required', 'INTEGER NOT NULL DEFAULT 0'),
            ('withdrawal_wager_progress', 'INTEGER NOT NULL DEFAULT 0'),
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
            ('promo_attempts_total', 'INTEGER NOT NULL DEFAULT 1'), ('promo_attempts_remaining', 'INTEGER NOT NULL DEFAULT 1'),
            ('promo_burn_on_loss', 'INTEGER NOT NULL DEFAULT 1'), ('bet_external_url', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('arena_rounds', [('extended', 'INTEGER NOT NULL DEFAULT 0')])
        ensure_columns('hilo_room_bets', [('gift_name', "TEXT NOT NULL DEFAULT ''"), ('gift_image', "TEXT NOT NULL DEFAULT ''"),
                                          ('gift_row', "TEXT NOT NULL DEFAULT ''"), ('won', 'INTEGER NOT NULL DEFAULT 0'),
                                          ('want_gift', 'INTEGER NOT NULL DEFAULT 0'),
                                          ('prize_name', "TEXT NOT NULL DEFAULT ''"), ('prize_image', "TEXT NOT NULL DEFAULT ''"),
                                          ('prize_price', 'INTEGER NOT NULL DEFAULT 0')])
        ensure_columns('arena_bets', [('gift_amount', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('gifts', "TEXT NOT NULL DEFAULT '[]'")])
        ensure_columns('crash_bets', [
            ('bet_type', "TEXT NOT NULL DEFAULT 'ton'"), ('bet_inventory_id', 'INTEGER'),
            ('bet_gift_id', "TEXT NOT NULL DEFAULT ''"), ('bet_gift_name', "TEXT NOT NULL DEFAULT ''"),
            ('bet_gift_image', "TEXT NOT NULL DEFAULT ''"),
            ('prize_inventory_id', 'INTEGER'), ('prize_name', "TEXT NOT NULL DEFAULT ''"),
            ('prize_image', "TEXT NOT NULL DEFAULT ''"), ('prize_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_wager_multiplier', 'REAL NOT NULL DEFAULT 0'), ('promo_wager_target', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_wager_progress', 'INTEGER NOT NULL DEFAULT 0'), ('promo_progress_after', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_code', "TEXT NOT NULL DEFAULT ''"), ('bet_expires_at', 'TEXT'),
            ('promo_attempts_total', 'INTEGER NOT NULL DEFAULT 1'), ('promo_attempts_remaining', 'INTEGER NOT NULL DEFAULT 1'),
            ('promo_burn_on_loss', 'INTEGER NOT NULL DEFAULT 1'), ('bet_external_url', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('inventory', [
            ('image_url', "TEXT NOT NULL DEFAULT ''"), ('floor_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('source', "TEXT NOT NULL DEFAULT 'legacy'"), ('round_id', 'INTEGER'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('promo_locked', 'INTEGER NOT NULL DEFAULT 0'), ('promo_wager_multiplier', 'REAL NOT NULL DEFAULT 0'),
            ('promo_wager_target', 'INTEGER NOT NULL DEFAULT 0'), ('promo_wager_progress', 'INTEGER NOT NULL DEFAULT 0'),
            ('promo_code', "TEXT NOT NULL DEFAULT ''"), ('expires_at', 'TEXT'),
            ('promo_attempts_total', 'INTEGER NOT NULL DEFAULT 1'), ('promo_attempts_remaining', 'INTEGER NOT NULL DEFAULT 1'),
            ('promo_burn_on_loss', 'INTEGER NOT NULL DEFAULT 1'),
            ('promo_unlock_payload', "TEXT NOT NULL DEFAULT '{}'"),
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
        ensure_columns('inventory', [('source_label', "TEXT NOT NULL DEFAULT ''")])
        ensure_columns('inventory', [('deposit_mirror', 'INTEGER NOT NULL DEFAULT 0')])
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
            ('activation_min_deposit', 'INTEGER NOT NULL DEFAULT 0'),
            ('author_user_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('poll_id', "TEXT NOT NULL DEFAULT ''"),
            ('poll_option_name', "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns('promo_redemptions', [('consumed_at', 'TEXT'),('deactivated_at', 'TEXT')])
        _had_seen_at = 'seen_at' in {row['name'] for row in db.execute('PRAGMA table_info(freebet_redemptions)')}
        ensure_columns('freebet_redemptions', [('seen_at', 'TEXT')])
        ensure_columns('freebets', [('min_deposit', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('author_user_id', 'INTEGER NOT NULL DEFAULT 0')])
        if not _had_seen_at:
            # Old redemptions predate the "received" window: don't pop them up retroactively.
            db.execute('UPDATE freebet_redemptions SET seen_at=created_at WHERE seen_at IS NULL')
        ensure_columns('ton_deposit_orders', [('promo_code', "TEXT NOT NULL DEFAULT ''")])
        ensure_columns('withdrawals', [
            ('image_url', "TEXT NOT NULL DEFAULT ''"), ('floor_price', 'INTEGER NOT NULL DEFAULT 0'),
            ('source', "TEXT NOT NULL DEFAULT 'withdrawal'"), ('round_id', 'INTEGER'),
            ('status', "TEXT NOT NULL DEFAULT 'pending'"), ('admin_id', 'INTEGER'),
            ('created_at', "TEXT NOT NULL DEFAULT ''"), ('processed_at', 'TEXT'),
            ('external_url', "TEXT NOT NULL DEFAULT ''"),
            ('fragment_number', "TEXT NOT NULL DEFAULT ''"), ('fragment_model', "TEXT NOT NULL DEFAULT ''"),
            ('fragment_backdrop', "TEXT NOT NULL DEFAULT ''"), ('fragment_symbol', "TEXT NOT NULL DEFAULT ''"),
            ('price_source', "TEXT NOT NULL DEFAULT ''"), ('animation_url', "TEXT NOT NULL DEFAULT ''"),
            ('fee_amount', 'INTEGER NOT NULL DEFAULT 0'),
        ])
        ensure_columns('relayer_gift_events', [('portal_price', 'INTEGER NOT NULL DEFAULT 0')])
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
        ensure_postgres_bigint('promo_codes', ['created_by', 'bonus_fixed', 'min_deposit', 'activation_min_deposit', 'assigned_user_id', 'author_user_id'])
        ensure_postgres_bigint('freebets', ['min_turnover', 'min_deposit', 'created_by', 'author_user_id'])
        ensure_postgres_bigint('freebet_redemptions', ['user_id'])
        ensure_postgres_bigint('arena_rounds', ['winner_user_id', 'total_pool'])
        ensure_postgres_bigint('arena_bets', ['user_id', 'amount'])
        ensure_postgres_bigint('withdrawals', ['floor_price', 'round_id', 'admin_id'])
        ensure_postgres_bigint('referrals', ['referrer_id'])
        ensure_postgres_bigint('deposits', ['amount', 'referrer_id', 'referral_bonus', 'admin_id'])
        ensure_postgres_bigint('transactions', ['amount', 'balance_after'])
        ensure_postgres_bigint('wins_feed_clears', ['max_round_id'])
        ensure_postgres_bigint('levels', ['required_turnover'])
        ensure_postgres_bigint('upgrade_spins', ['user_id', 'source_price', 'target_price'])
        ensure_postgres_bigint('upgrade_promo_pity', ['user_id'])
        ensure_postgres_bigint('user_notifications', ['user_id'])
        ensure_postgres_bigint('relayer_gift_events', ['sender_user_id', 'floor_price', 'portal_price', 'inventory_id'])
        ensure_postgres_bigint('relayer_withdrawal_logs', ['withdrawal_id', 'user_id', 'inventory_id', 'transfer_stars'])
        # Indexes are intentionally created after additive migrations. Creating an index on a
        # column that did not exist on an older Render disk was the source of the HTTP 500 startup failure.
        db.executescript("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_at INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            text TEXT NOT NULL DEFAULT '', photos TEXT NOT NULL DEFAULT '[]', buttons TEXT NOT NULL DEFAULT '[]',
            total INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'running', created_at INTEGER NOT NULL, finished_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS broadcast_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, broadcast_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', next_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS broadcast_items_pending ON broadcast_items(state, next_at, id);
        CREATE INDEX IF NOT EXISTS broadcast_items_bc ON broadcast_items(broadcast_id, state);
        CREATE INDEX IF NOT EXISTS relayer_gift_events_sender ON relayer_gift_events(sender_user_id,id DESC);
        CREATE INDEX IF NOT EXISTS relayer_withdrawal_logs_status ON relayer_withdrawal_logs(status,id DESC);
        CREATE INDEX IF NOT EXISTS relayer_withdrawal_logs_user ON relayer_withdrawal_logs(user_id,id DESC);
        CREATE TABLE IF NOT EXISTS creator_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            image_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS outbox_due ON notification_outbox(state,next_at,id);
        CREATE INDEX IF NOT EXISTS creator_chat_messages_id ON creator_chat_messages(id DESC);
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
        db.execute('CREATE INDEX IF NOT EXISTS promo_codes_poll ON promo_codes(poll_id,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS promo_poll_votes_poll ON promo_poll_votes(poll_id,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS freebets_active ON freebets(active,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS freebet_redemptions_user ON freebet_redemptions(user_id,created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS freebet_burn_prizes_code ON freebet_burn_prizes(freebet_code,claimed_by,id)')
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
    if user_id not in ADMIN_IDS and is_banned(user_id, fresh=True):
        return banned_response()
    session.clear()
    session['uid'] = user_id
    return jsonify(ok=True, user=profile())


@app.post('/api/logout')
def logout():
    session.clear()
    response = jsonify(ok=True)
    response.delete_cookie(app.config.get('SESSION_COOKIE_NAME', 'session'), path='/')
    return response


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
    if uid not in ADMIN_IDS and is_banned(uid, fresh=True):
        return banned_response()
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


CREATOR_LEVELS = {
    'base': dict(
        key='base', name='Base', daily_budget_cents=30, daily_code_limit=1,
        activation_min_deposit_cents=100, wager_daily_limit=1, wager_min_x=40,
        wager_gift_min_cents=300, wager_gift_max_cents=400, wager_max_uses=1,
        description='Базовый уровень автора: до 0.30 TON в день или 1 отыгрышный подарок 3–4 TON с X от 40.',
    ),
    'creator': dict(
        key='creator', name='Creator', daily_budget_cents=150, daily_code_limit=0,
        activation_min_deposit_cents=50, wager_daily_limit=1, wager_min_x=30,
        wager_gift_min_cents=300, wager_gift_max_cents=500, wager_max_uses=10,
        description='До 1.50 TON в день и 1 отыгрышный подарок стоимостью 3–5 TON.',
    ),
    'super_creator': dict(
        key='super_creator', name='Super Creator', daily_budget_cents=500, daily_code_limit=0,
        activation_min_deposit_cents=50, wager_daily_limit=3, wager_min_x=20,
        wager_gift_min_cents=300, wager_gift_max_cents=1000, wager_max_uses=15,
        custom_deposit=True,
        description='До 5 TON в день и до 3 отыгрышных подарков стоимостью 3–10 TON. Условие депозита задаёте сами: можно без депозита или с любым минимумом.',
    ),
    'god': dict(
        key='god', name='God', daily_budget_cents=1000, daily_code_limit=0,
        activation_min_deposit_cents=0, wager_daily_limit=10, wager_min_x=17,
        wager_gift_min_cents=300, wager_gift_max_cents=1500, wager_max_uses=20,
        custom_deposit=True,
        description='Высший уровень. До 10 TON в день и до 10 отыгрышных подарков стоимостью 3–15 TON с X от 17 и до 20 активаций. Депозит на выбор: без депозита или любой минимум. Выдаётся только администратором.',
    ),
}


def creator_level_key(value):
    value = str(value or 'base').strip().lower()
    return value if value in CREATOR_LEVELS else 'base'


def creator_level_public(value):
    cfg = dict(CREATOR_LEVELS[creator_level_key(value)])
    cfg['custom_deposit'] = bool(cfg.get('custom_deposit'))
    for key in ('daily_budget_cents', 'activation_min_deposit_cents',
                'wager_gift_min_cents', 'wager_gift_max_cents'):
        cfg[key.replace('_cents', '_ton')] = cfg.pop(key) / 100
    return cfg


def creator_record(user_id):
    """Creator/author sandbox settings stored independently from real account data."""
    try:
        raw = read_document(f'creator:{int(user_id)}') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    inventory = raw.get('demo_inventory') if isinstance(raw.get('demo_inventory'), list) else []
    clean_inventory = []
    for item in inventory[:200]:
        if not isinstance(item, dict):
            continue
        clean_inventory.append(dict(item))
    try:
        demo_balance = max(0, min(100000000000, int(raw.get('demo_balance_cents') or 0)))
    except (TypeError, ValueError):
        demo_balance = 0
    try:
        demo_turnover = max(0, min(100000000000000, int(raw.get('demo_turnover_cents') or 0)))
    except (TypeError, ValueError):
        demo_turnover = 0
    try:
        demo_tickets = max(0, min(1000000000, int(raw.get('demo_tickets') or 0)))
    except (TypeError, ValueError):
        demo_tickets = 0
    demo_claims = raw.get('demo_level_claims') if isinstance(raw.get('demo_level_claims'), dict) else {}
    youtube = raw.get('youtube') if isinstance(raw.get('youtube'), dict) else {}
    return dict(
        active=bool(raw.get('active')),
        creator_level=creator_level_key(raw.get('creator_level')),
        panel_hidden=bool(raw.get('active') and raw.get('panel_hidden')),
        demo_enabled=bool(raw.get('active') and raw.get('demo_enabled')),
        demo_balance_cents=demo_balance,
        demo_turnover_cents=demo_turnover,
        demo_tickets=demo_tickets,
        demo_level_claims=dict(demo_claims),
        demo_inventory=clean_inventory,
        demo_mines=dict(raw.get('demo_mines') or {}) if isinstance(raw.get('demo_mines'), dict) else {},
        demo_upgrade=dict(raw.get('demo_upgrade') or {}) if isinstance(raw.get('demo_upgrade'), dict) else {},
        demo_crash=dict(raw.get('demo_crash') or {}) if isinstance(raw.get('demo_crash'), dict) else {},
        demo_arena=dict(raw.get('demo_arena') or {}) if isinstance(raw.get('demo_arena'), dict) else {},
        youtube=dict(youtube),
        youtube_pending=(dict(raw.get('youtube_pending')) if isinstance(raw.get('youtube_pending'), dict) else {}),
        creator_limit_reset_at=raw.get('creator_limit_reset_at'),
        creator_limit_credit=(dict(raw.get('creator_limit_credit')) if isinstance(raw.get('creator_limit_credit'), dict) else {}),
        created_at=raw.get('created_at'),
        updated_at=raw.get('updated_at'),
    )


def save_creator_record(user_id, data):
    current = creator_record(user_id)
    current.update(data if isinstance(data, dict) else {})
    current['active'] = bool(current.get('active'))
    current['creator_level'] = creator_level_key(current.get('creator_level'))
    current['panel_hidden'] = bool(current['active'] and current.get('panel_hidden'))
    current['demo_enabled'] = bool(current['active'] and current.get('demo_enabled'))
    for key, upper in (('demo_balance_cents', 100000000000),
                       ('demo_turnover_cents', 100000000000000),
                       ('demo_tickets', 1000000000)):
        try:
            current[key] = max(0, min(upper, int(current.get(key) or 0)))
        except (TypeError, ValueError):
            current[key] = 0
    if not isinstance(current.get('demo_level_claims'), dict):
        current['demo_level_claims'] = {}
    if not isinstance(current.get('demo_inventory'), list):
        current['demo_inventory'] = []
    current['demo_inventory'] = current['demo_inventory'][:200]
    for state_key in ('demo_mines', 'demo_upgrade', 'demo_crash', 'demo_arena'):
        if not isinstance(current.get(state_key), dict):
            current[state_key] = {}
    if not isinstance(current.get('youtube'), dict):
        current['youtube'] = {}
    if not isinstance(current.get('youtube_pending'), dict):
        current['youtube_pending'] = {}
    current['updated_at'] = datetime.now(timezone.utc).isoformat()
    if not current.get('created_at'):
        current['created_at'] = current['updated_at']
    save_document(f'creator:{int(user_id)}', current)
    return creator_record(user_id)


def increase_demo_turnover(record, amount):
    amount = max(0, int(amount or 0))
    before = int(record.get('demo_turnover_cents') or 0)
    record['demo_turnover_cents'] = min(100000000000000, before + amount)
    return record['demo_turnover_cents']


def creator_demo_active(user_id):
    return bool(user_id and creator_record(user_id).get('demo_enabled'))



def demo_clean_item(item):
    """Demo gifts must look and be valued like real ones: no DEMO label, a real catalog price."""
    item = dict(item)
    if str(item.get('source_label') or '').strip().upper() == 'DEMO':
        item['source_label'] = ''
    if str(item.get('price_source') or '').strip().upper() == 'DEMO':
        item['price_source'] = 'Portal'
    try:
        price = float(item.get('price_ton') or 0)
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        # Legacy demo gifts were stored with a 0 price: restore it from the catalog.
        catalog_gift = next((g for g in read_catalog(include_hidden=True).get('gifts', [])
                             if str(g.get('id') or '') == str(item.get('gift_id') or '')), None)
        if catalog_gift:
            try:
                item['price_ton'] = float(catalog_gift.get('price_ton') or 0)
            except (TypeError, ValueError):
                pass
    return item


def demo_find_item(record, item_id):
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    found = next((x for x in record.get('demo_inventory', [])
                  if int(x.get('id') or 0) == item_id), None)
    return demo_clean_item(found) if found else None


def demo_remove_item(record, item_id):
    item = demo_find_item(record, item_id)
    if not item:
        return None
    record['demo_inventory'] = [x for x in record.get('demo_inventory', [])
                                if int(x.get('id') or 0) != int(item_id)]
    return item


def demo_add_catalog_gift(record, gift, source='creator_demo_game'):
    items = list(record.get('demo_inventory') or [])
    next_id = max([int(x.get('id') or 0) for x in items] + [0]) + 1
    # upgrade_target() returns the price in cents under the key 'price'; the old code
    # looked for price_cents / price_ton only and so stored every won gift at 0 TON.
    price = int(gift.get('price_cents') or gift.get('price') or 0)
    if not price:
        try:
            price = parse_amount(gift.get('price_ton') or 0)
        except (ValueError, InvalidOperation, TypeError):
            price = 0
    item = dict(
        id=next_id, gift_id=str(gift.get('id') or gift.get('gift_id') or ''),
        name=str(gift.get('name') or gift.get('gift_name') or 'Подарок'),
        image_url=safe_image(gift.get('image_url')),
        price_ton=price / 100, source=source,
        created_at=datetime.now(timezone.utc).isoformat(), external_url='',
        fragment_url='', fragment_number='', fragment_model='', fragment_backdrop='',
        fragment_symbol='', price_source='Portal', animation_url='',
        source_label='', promo_locked=False, promo_code='', wager_multiplier=0,
        wager_target=0, wager_progress=0, wager_complete=False, wager_percent=0,
        wager_attempts_total=1, wager_attempts_remaining=1, wager_burn_on_loss=True,
        unlock_target=None, expires_at=None, expires_in_seconds=None,
    )
    items.insert(0, item)
    record['demo_inventory'] = items[:200]
    return item


def demo_round_view(state):
    if not state:
        return None
    return dict(
        id=state.get('id'), bet=float(state.get('bet') or 0),
        bet_type=state.get('bet_type') or 'ton', bet_gift=state.get('bet_gift'),
        mines=int(state.get('mines') or 3), opened=list(state.get('opened') or []),
        state=state.get('state') or 'active', multiplier=float(state.get('multiplier') or 1),
        potential=float(state.get('potential') or state.get('bet') or 0),
        positions=list(state.get('positions') or []) if state.get('state') != 'active' else [],
        payout=float(state.get('payout') or 0), prize=state.get('prize'),
        awarded=state.get('awarded'), lost_cell=state.get('lost_cell'),
        promo_progress_after=0,
    )


def demo_mines_start(data):
    uid = session['uid']
    record = creator_record(uid)
    try:
        mines = int(data.get('mines'))
    except (TypeError, ValueError):
        raise ValueError('Укажите корректное число мин.')
    if not (MIN_MINES <= mines <= MAX_MINES):
        raise ValueError('Количество мин: от 1 до 20.')
    if record.get('demo_mines', {}).get('state') == 'active':
        raise ValueError('Сначала завершите текущую DEMO-игру.')
    inventory_id = data.get('inventory_id')
    bet_gift = None
    if inventory_id not in (None, ''):
        item = demo_remove_item(record, inventory_id)
        if not item:
            raise ValueError('DEMO-подарок не найден в инвентаре.')
        bet_cents = parse_amount(item.get('price_ton') or 0)
        bet_type = 'gift'
        bet_gift = dict(item)
    else:
        bet_cents = parse_amount(data.get('bet'))
        if not (MIN_BET_CENTS <= bet_cents <= MAX_BET_CENTS):
            raise ValueError('Ставка от 0.10 до 300 TON.')
        if int(record.get('demo_balance_cents') or 0) < bet_cents:
            raise ValueError('Недостаточно DEMO TON.')
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) - bet_cents
        bet_type = 'ton'
    positions = sorted(secrets.SystemRandom().sample(range(25), mines))
    increase_demo_turnover(record, bet_cents)
    state = dict(
        id=int(time.time() * 1000), bet=bet_cents / 100, bet_type=bet_type, bet_gift=bet_gift,
        mines=mines, positions=positions, opened=[], state='active', payout=0,
        multiplier=1, potential=bet_cents / 100, prize=None, awarded=None, lost_cell=None,
    )
    save_creator_record(uid, {'demo_balance_cents': record['demo_balance_cents'],
                              'demo_turnover_cents': record['demo_turnover_cents'],
                              'demo_inventory': record['demo_inventory'], 'demo_mines': state})
    return demo_round_view(state)


def demo_mines_open(cell):
    uid = session['uid']
    record = creator_record(uid)
    state = dict(record.get('demo_mines') or {})
    if state.get('state') != 'active':
        raise ValueError('Сначала начните DEMO-игру.')
    if cell not in range(25):
        raise ValueError('Неверная клетка.')
    opened = list(state.get('opened') or [])
    if cell in opened:
        raise ValueError('Клетка уже открыта.')
    if cell in set(state.get('positions') or []):
        state['state'] = 'lost'
        state['lost_cell'] = cell
        state['positions'] = list(state.get('positions') or [])
    else:
        opened.append(cell)
        state['opened'] = opened
        mines = int(state.get('mines') or 3)
        factor = multiplier_for(mines, len(opened), game_rtp())
        state['multiplier'] = float(factor)
        state['potential'] = round(float(state.get('bet') or 0) * float(factor), 2)
        if len(opened) >= 25 - mines:
            state = demo_mines_cashout_state(record, state)
    save_creator_record(uid, {'demo_mines': state, 'demo_inventory': record.get('demo_inventory', []),
                              'demo_balance_cents': record.get('demo_balance_cents', 0)})
    return demo_round_view(state)


def demo_mines_cashout_state(record, state):
    if not state.get('opened'):
        raise ValueError('Для вывода откройте хотя бы одну безопасную клетку.')
    payout_cents = parse_amount(state.get('potential') or state.get('bet') or 0)
    state['state'] = 'won'
    state['positions'] = list(state.get('positions') or [])
    prize = prize_for(payout_cents)
    if prize:
        price_cents = ton_to_cents(prize.get('price_ton') or 0)
        remainder = max(0, payout_cents - price_cents)
        awarded = demo_add_catalog_gift(record, dict(
            id=prize.get('id'), name=prize.get('name'), image_url=prize.get('image_url'),
            price_cents=price_cents), 'creator_demo_mines')
        state['payout'] = remainder / 100
        state['prize'] = dict(id=prize.get('id'), name=prize.get('name'),
                              image_url=safe_image(prize.get('image_url')), price_ton=price_cents / 100)
        state['awarded'] = awarded
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + remainder
    else:
        state['payout'] = payout_cents / 100
        state['prize'] = None
        state['awarded'] = None
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + payout_cents
    return state


def demo_mines_cashout():
    uid = session['uid']
    record = creator_record(uid)
    state = dict(record.get('demo_mines') or {})
    if state.get('state') != 'active':
        raise ValueError('Нет активной DEMO-игры.')
    state = demo_mines_cashout_state(record, state)
    save_creator_record(uid, {'demo_mines': state, 'demo_balance_cents': record['demo_balance_cents'],
                              'demo_inventory': record.get('demo_inventory', [])})
    return demo_round_view(state)


def demo_upgrade_preview(amount_text, item_text, gift_id):
    record = creator_record(session['uid'])
    source = None
    if bool(amount_text) == bool(item_text):
        raise ValueError('Выберите TON или подарок для ставки.')
    if amount_text:
        source_price = parse_amount(amount_text)
        if int(record.get('demo_balance_cents') or 0) < source_price:
            raise ValueError('Недостаточно DEMO TON.')
        source_view = dict(type='ton', id=None, name='TON', image_url='/static/img/ton.png',
                           price_ton=source_price / 100)
    else:
        source = demo_find_item(record, item_text)
        if not source:
            raise ValueError('Выберите доступный DEMO-подарок из инвентаря.')
        source_price = parse_amount(source.get('price_ton') or 0)
        source_view = dict(type='gift', **source)
    target = upgrade_target(gift_id)
    if not target:
        raise ValueError('Целевой подарок не найден в каталоге Portal.')
    chance = upgrade_chance(source_price, target['price'], upgrade_rtp_basis_points())
    if not chance:
        raise ValueError('Выберите цель с шансом от 1% до 80% и ценой не выше ×10 ставки.')
    return dict(source=source_view,
                target=dict(id=target['id'], name=target['name'], image_url=target['image_url'],
                            price_ton=target['price'] / 100),
                chance=chance / 100, probability=chance / 10000,
                rtp=upgrade_rtp_basis_points() / 100, loss_rtp_boost=0, game_loss_ton=0)


def demo_upgrade_spin(data):
    uid = session['uid']
    record = creator_record(uid)
    preview = demo_upgrade_preview(data.get('amount'), data.get('inventory_id'), data.get('gift_id'))
    source = preview['source']
    source_price = parse_amount(source.get('price_ton') or 0)
    if source.get('type') == 'ton':
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) - source_price
    else:
        removed = demo_remove_item(record, source.get('id'))
        if not removed:
            raise ValueError('DEMO-подарок уже использован.')
    increase_demo_turnover(record, source_price)
    target = upgrade_target(data.get('gift_id'))
    won = secrets.randbelow(max(1, target['price'] * 10000)) < upgrade_rtp_basis_points() * source_price
    awarded = None
    if won:
        awarded = demo_add_catalog_gift(record, target, 'creator_demo_upgrade')
    result = dict(
        ok=True, id=str(data.get('request_id') or secrets.token_hex(8)), won=won,
        chance=preview['chance'], source_type=source.get('type') or 'gift', reward_type='gift',
        source=dict(name=source.get('name') or 'Подарок', image_url=source.get('image_url') or '',
                    price_ton=source_price / 100),
        target=preview['target'], wager_progress=0, wager_target=0,
        wager_attempts_total=1, wager_attempts_remaining=0, wager_burn_on_loss=True,
        wager_burned=False, wager_complete=False, expires_at=None,
        awarded_inventory_id=(awarded.get('id') if awarded else None),
        compensation=dict(cashback=0, cashback_percent=0, promo=None),
        new_level=None,
    )
    save_creator_record(uid, {'demo_balance_cents': record['demo_balance_cents'],
                              'demo_turnover_cents': record['demo_turnover_cents'],
                              'demo_inventory': record['demo_inventory'], 'demo_upgrade': result})
    return result


def demo_crash_state_payload(record=None, now=None):
    uid = session['uid']
    record = record or creator_record(uid)
    now = int(now if now is not None else time.time() * 1000)
    state = dict(record.get('demo_crash') or {})
    if not state or (state.get('phase') == 'crashed' and now >= int(state.get('reset_at') or 0)):
        state = dict(id=now, phase='betting', open_at=now, launch_at=now + CRASH_BETTING_MS,
                     crash_at=0, crash=0, reset_at=0, my_bet=None)
        record = save_creator_record(uid, {'demo_crash': state})
        state = dict(record.get('demo_crash') or state)
    phase_changed = False
    if state.get('phase') == 'betting' and now >= int(state.get('launch_at') or 0):
        state['phase'] = 'flying'
        phase_changed = True
    if state.get('phase') == 'flying' and int(state.get('crash_at') or 0) and now >= int(state.get('crash_at') or 0):
        state['phase'] = 'crashed'
        state['reset_at'] = now + CRASH_BOOM_MS
        mb = state.get('my_bet')
        if isinstance(mb, dict) and mb.get('state') == 'active':
            mb['state'] = 'lost'
        save_creator_record(uid, {'demo_crash': state})
        phase_changed = False
    elif phase_changed:
        record = save_creator_record(uid, {'demo_crash': state})
    payload = dict(
        now=now, phase=state.get('phase') or 'betting',
        round=dict(id=state.get('id'), open_at=state.get('open_at'), launch_at=state.get('launch_at')),
        growth=CRASH_GROWTH, boom_ms=CRASH_BOOM_MS, betting_ms=CRASH_BETTING_MS,
        min_bet=MIN_BET_CENTS / 100, max_bet=MAX_BET_CENTS / 100,
        min_nft=0, available=game_available('crash'), demo=True,
        my_bet=state.get('my_bet'), bets=[], history=[],
        balance=record.get('demo_balance_cents', 0) / 100,
    )
    if state.get('phase') == 'crashed':
        payload['round']['crash'] = float(state.get('crash') or 1)
        payload['round']['crash_at'] = int(state.get('crash_at') or now)
    if state.get('my_bet'):
        me = current_user()
        payload['bets'] = [dict(user_id=uid, name=me['name'], photo_url=me['photo_url'],
                                bet=state['my_bet']['bet'], bet_type=state['my_bet'].get('bet_type','ton'),
                                promo=False, bet_gift=state['my_bet'].get('bet_gift'),
                                state=state['my_bet'].get('state','active'),
                                cashout=state['my_bet'].get('cashout',0),
                                payout=state['my_bet'].get('payout',0), prize=None)]
    return payload


def demo_crash_bet(data):
    uid = session['uid']
    record = creator_record(uid)
    now = int(time.time() * 1000)
    payload = demo_crash_state_payload(record, now)
    state = dict(creator_record(uid).get('demo_crash') or {})
    if payload['phase'] != 'betting' or state.get('my_bet'):
        raise ValueError('Дождитесь следующего DEMO-раунда.')
    inventory_id = data.get('inventory_id')
    bet_gift = None
    if inventory_id not in (None, ''):
        item = demo_remove_item(record, inventory_id)
        if not item:
            raise ValueError('DEMO-подарок не найден в инвентаре.')
        bet_cents = parse_amount(item.get('price_ton') or 0)
        bet_type = 'gift'
        bet_gift = dict(item)
    else:
        bet_cents = parse_amount(data.get('bet'))
        if int(record.get('demo_balance_cents') or 0) < bet_cents:
            raise ValueError('Недостаточно DEMO TON.')
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) - bet_cents
        bet_type = 'ton'
    if not (MIN_BET_CENTS <= bet_cents <= MAX_BET_CENTS):
        raise ValueError('Ставка от 0.10 до 300 TON.')
    crash_x100 = crash_roll_x100(crash_rtp())
    state['crash'] = crash_x100 / 100
    increase_demo_turnover(record, bet_cents)
    state['crash_at'] = int(state['launch_at']) + crash_flight_ms(crash_x100)
    state['my_bet'] = dict(bet=bet_cents / 100, auto=float(data.get('auto') or 0),
                            state='active', cashout=0, payout=0, bet_type=bet_type,
                            promo=False, promo_min=1.2, bet_gift=bet_gift, prize=None)
    save_creator_record(uid, {'demo_balance_cents': record['demo_balance_cents'],
                              'demo_turnover_cents': record['demo_turnover_cents'],
                              'demo_inventory': record['demo_inventory'], 'demo_crash': state})
    return demo_crash_state_payload(creator_record(uid), now)


def demo_crash_cashout(data=None):
    uid = session['uid']
    now = int(time.time() * 1000)
    demo_crash_state_payload(creator_record(uid), now)
    record = creator_record(uid)
    state = dict(record.get('demo_crash') or {})
    if state.get('phase') != 'flying' or not isinstance(state.get('my_bet'), dict) or state['my_bet'].get('state') != 'active':
        raise ValueError('Нет активной DEMO-ставки.')
    launch_at = int(state.get('launch_at') or now)
    mult = max(1.0, math.exp(CRASH_GROWTH * max(0, now - launch_at) / 1000.0))
    if now >= int(state.get('crash_at') or 0):
        raise ValueError('Ракета уже взорвалась.')
    payout_cents = int(round(float(state['my_bet']['bet']) * 100 * mult))
    want_gift = bool((data or {}).get('gift'))
    prize_info = crash_prize_preview(payout_cents) if want_gift else None
    if want_gift and not prize_info:
        raise ValueError('Сумма ещё ниже самого дешёвого подарка — заберите TON.')
    prize = None
    remainder = 0
    if prize_info:
        remainder = max(0, payout_cents - int(prize_info['price_cents']))
        awarded = demo_add_catalog_gift(record, dict(
            id=prize_info.get('id'), name=prize_info.get('name'),
            image_url=prize_info.get('image_url'), price_cents=int(prize_info['price_cents'])),
            'creator_demo_crash')
        prize = dict(name=awarded['name'], image_url=awarded['image_url'], price_ton=awarded['price_ton'])
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + remainder
    else:
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + payout_cents
    state['my_bet']['state'] = 'won'
    state['my_bet']['cashout'] = round(mult, 2)
    state['my_bet']['payout'] = payout_cents / 100
    state['my_bet']['prize'] = prize
    save_creator_record(uid, {'demo_balance_cents': record['demo_balance_cents'],
                              'demo_inventory': record.get('demo_inventory', []), 'demo_crash': state})
    return dict(ok=True, multiplier=round(mult, 2), payout=payout_cents / 100,
                prize=prize, remainder=remainder / 100, promo=None,
                state=demo_crash_state_payload(creator_record(uid), now), user=profile())


def profile():
    user = current_user()
    creator = creator_record(user['id'])
    demo = bool(creator.get('demo_enabled'))
    stars_until = parse_datetime_utc(user['stars_withdrawal_until'])
    stars_locked = bool(stars_until and stars_until > datetime.now(timezone.utc))
    return dict(id=user['id'], name=user['name'], username=user['username'], photo_url=user['photo_url'],
                balance=(creator['demo_balance_cents'] if demo else user['balance']) / 100,
                tickets=(creator['demo_tickets'] if demo else int(user['tickets'] or 0)),
                turnover=(creator['demo_turnover_cents']/100 if demo else user['turnover_cents']/100),
                withdrawal_enabled=(False if demo else bool(user['withdrawal_enabled'])),
                withdrawal_block_reason=('' if demo else (user['withdrawal_block_reason'] or '')),
                stars_withdrawal_locked=(False if demo else stars_locked),
                stars_withdrawal_until=(None if demo else (stars_until.isoformat() if stars_locked else None)),
                creator=bool(creator.get('active')),
                creator_level=creator.get('creator_level') or 'base',
                creator_level_info=creator_level_public(creator.get('creator_level')),
                creator_panel_hidden=bool(creator.get('panel_hidden')),
                creator_button_visible=bool(creator.get('active') and not creator.get('panel_hidden')),
                creator_demo=demo,
                creator_demo_balance=creator['demo_balance_cents']/100 if creator.get('active') else 0,
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


def increase_turnover(db, user_id, amount, withdrawal_wager=True):
    if amount <= 0:
        return None
    user = db.execute('SELECT turnover_cents FROM users WHERE id=?', (user_id,)).fetchone()
    previous_turnover = int(user['turnover_cents'] or 0)
    previous = level_number(db, previous_turnover)
    new_turnover = previous_turnover + amount
    if withdrawal_wager:
        db.execute("""UPDATE users
                      SET turnover_cents=turnover_cents+?,
                          withdrawal_wager_progress=CASE
                              WHEN withdrawal_wager_progress+? < withdrawal_wager_required
                              THEN withdrawal_wager_progress+?
                              ELSE withdrawal_wager_required
                          END
                      WHERE id=?""", (amount, amount, amount, user_id))
    else:
        db.execute('UPDATE users SET turnover_cents=turnover_cents+? WHERE id=?',(amount,user_id))
    current = level_number(db, new_turnover)
    return current if current > previous else None


PROMO_ORIGIN_SOURCES = ('promo_wager', 'upgrade_wager', 'upgrade_wager_repaired', 'level_wager',
                        'promo_claimed', 'freebet_burn_claimed', 'freebet_wager')


def gift_counts_for_xp(item):
    """Gifts that came from a wager/promo never add turnover (XP), locked or already unlocked."""
    try:
        keys = item.keys()
    except AttributeError:
        return True
    if 'promo_locked' in keys and item['promo_locked']:
        return False
    if 'source' in keys and str(item['source'] or '') in PROMO_ORIGIN_SOURCES:
        return False
    if 'promo_code' in keys and str(item['promo_code'] or ''):
        return False
    return True


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


# ======================================================================
# Anti multi-account, manual bans, maintenance mode (2026-10-07)
# ======================================================================
MULTI_IP_LIMIT_DEFAULT = int(os.environ.get('MULTI_ACCOUNT_IP_LIMIT', '3') or 3)   # accounts allowed per IP
TRUSTED_PROXY_HOPS = max(1, int(os.environ.get('TRUSTED_PROXY_HOPS', '1') or 1))
MULTI_IP_WINDOW_DAYS = 30
MAINTENANCE_DEFAULT_TEXT = 'На сайте проводятся технические работы. Мы скоро вернёмся!'
_ip_seen_cache = {}
_ban_cache = {}
_maint_cache = {'t': 0.0, 'v': None}


def banned_response():
    response = jsonify(error='Доступ ограничен.', banned=True)
    response.status_code = 403
    return response


def client_ip():
    """Client IP behind the hosting proxy; IPv6 is collapsed to its /64 so rotating suffixes does not help."""
    import ipaddress
    headers = request.headers
    raw = (headers.get('CF-Connecting-IP') or '').strip()
    if not raw:
        parts = [x.strip() for x in (headers.get('X-Forwarded-For') or '').split(',') if x.strip()]
        if parts:
            raw = parts[-TRUSTED_PROXY_HOPS] if len(parts) >= TRUSTED_PROXY_HOPS else parts[0]
    if not raw:
        raw = request.remote_addr or ''
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return ''
    if ip.version == 6:
        return str(ipaddress.ip_network(f'{raw}/64', strict=False).network_address) + '/64'
    return str(ip)


def is_banned(uid, fresh=False):
    now = time.time()
    cached = _ban_cache.get(uid)
    if cached and not fresh and now - cached[1] < 15:
        return cached[0]
    try:
        with connect() as db:
            row = db.execute('SELECT banned FROM users WHERE id=?', (uid,)).fetchone()
        value = bool(row and int(row['banned'] or 0))
    except Exception:
        value = False
    if len(_ban_cache) > 5000:
        _ban_cache.clear()
    _ban_cache[uid] = (value, now)
    return value


def antifraud_settings():
    doc = read_document('antifraud_settings') or {}
    try:
        limit = int(doc.get('ip_limit', MULTI_IP_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = MULTI_IP_LIMIT_DEFAULT
    return dict(enabled=bool(doc.get('enabled', True)), ip_limit=max(1, min(50, limit)))


def is_ban_exempt(uid):
    """Admins and active creators are never banned automatically."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return True
    if uid in ADMIN_IDS:
        return True
    try:
        return bool(creator_record(uid).get('active'))
    except Exception:
        return False


def ban_user(uid, reason, kind='manual', by=0, ip=''):
    if int(uid) in ADMIN_IDS:
        return False
    stamp = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute('UPDATE users SET banned=1, ban_reason=?, ban_kind=?, banned_at=? WHERE id=?',
                   (str(reason or '')[:300], kind, stamp, int(uid)))
        log_event(db, int(uid), 'banned', kind=kind, reason=str(reason or '')[:300], by=by, ip=ip)
    _ban_cache[int(uid)] = (True, time.time())
    return True


def unban_user(uid, by=0):
    with connect() as db:
        db.execute("UPDATE users SET banned=0, ban_reason='', ban_kind='', banned_at='', ban_immune=1 WHERE id=?", (int(uid),))
        log_event(db, int(uid), 'unbanned', by=by)
    _ban_cache[int(uid)] = (False, time.time())


def check_multi_account(ip):
    """Keep the oldest `ip_limit` accounts seen on this IP; ban the newer ones (admins/creators exempt)."""
    settings = antifraud_settings()
    if not settings['enabled'] or not ip:
        return []
    since = int(time.time()) - MULTI_IP_WINDOW_DAYS * 86400
    with connect() as db:
        rows = db.execute("SELECT u.id AS id, u.created_at AS created_at, u.banned AS banned, u.ban_immune AS ban_immune "
                          "FROM users u WHERE u.id IN (SELECT user_id FROM user_ips WHERE ip=? AND last_seen>=?)",
                          (ip, since)).fetchall()
    accounts = [r for r in rows if not is_ban_exempt(r['id'])]
    accounts.sort(key=lambda r: (str(r['created_at'] or ''), int(r['id'])))
    banned = []
    for index, row in enumerate(accounts):
        if index < settings['ip_limit'] or int(row['banned'] or 0) or int(row['ban_immune'] or 0):
            continue
        if ban_user(row['id'], f'Мульти-аккаунт: {len(accounts)} аккаунтов с одного IP', kind='multi', ip=ip):
            banned.append(int(row['id']))
    return banned


def record_user_ip(uid):
    ip = client_ip()
    if not ip:
        return
    key = (uid, ip)
    now = int(time.time())
    if now - _ip_seen_cache.get(key, 0) < 1800:
        return
    if len(_ip_seen_cache) > 20000:
        _ip_seen_cache.clear()
    _ip_seen_cache[key] = now
    try:
        with connect() as db:
            known = db.execute('SELECT 1 FROM user_ips WHERE user_id=? AND ip=?', (uid, ip)).fetchone()
            if known:
                db.execute('UPDATE user_ips SET last_seen=?, hits=hits+1 WHERE user_id=? AND ip=?', (now, uid, ip))
                return
            db.execute('INSERT INTO user_ips(user_id,ip,first_seen,last_seen,hits) VALUES(?,?,?,?,1) '
                       'ON CONFLICT(user_id,ip) DO NOTHING', (uid, ip, now, now))
        check_multi_account(ip)
    except Exception:
        app.logger.exception('record_user_ip failed')


def maintenance_state(fresh=False):
    now = time.time()
    if not fresh and _maint_cache['v'] is not None and now - _maint_cache['t'] < 3:
        return _maint_cache['v']
    doc = read_document('maintenance') or {}
    try:
        ends_at = int(doc.get('ends_at') or 0)
    except (TypeError, ValueError):
        ends_at = 0
    value = dict(enabled=bool(doc.get('enabled')), message=str(doc.get('message') or MAINTENANCE_DEFAULT_TEXT)[:500],
                 ends_at=ends_at)
    _maint_cache.update(t=now, v=value)
    return value


MAINT_OPEN_PREFIXES = ('/api/maintenance', '/api/ui/', '/api/auth', '/api/logout', '/api/web-auth/', '/api/admin/maintenance')


@app.before_request
def gate_ban_and_maintenance():
    path = request.path
    if not path.startswith('/api/'):
        return None
    try:
        uid = int(session.get('uid') or 0)
    except (TypeError, ValueError):
        uid = 0
    is_admin = uid in ADMIN_IDS
    if uid and not is_admin and path != '/api/logout':
        if is_banned(uid):
            return banned_response()
        record_user_ip(uid)
        if is_banned(uid):
            return banned_response()
    if not is_admin and not path.startswith(MAINT_OPEN_PREFIXES) and not (path == '/api/me' and not uid):
        state = maintenance_state()
        if state['enabled']:
            response = jsonify(error=state['message'], maintenance=True, message=state['message'],
                               ends_at=state['ends_at'], now=int(time.time() * 1000))
            response.status_code = 503
            return response
    return None


@app.get('/api/maintenance')
def public_maintenance():
    state = maintenance_state()
    return jsonify(enabled=state['enabled'], message=state['message'], ends_at=state['ends_at'],
                   now=int(time.time() * 1000))


@app.get('/api/admin/maintenance')
@admin_required
def admin_maintenance_get():
    state = maintenance_state(fresh=True)
    return jsonify(enabled=state['enabled'], message=state['message'], ends_at=state['ends_at'],
                   now=int(time.time() * 1000), default_message=MAINTENANCE_DEFAULT_TEXT)


@app.post('/api/admin/maintenance')
@admin_required
def admin_maintenance_set():
    data = request.get_json(silent=True) or {}
    current = maintenance_state(fresh=True)
    enabled = bool(data.get('enabled'))
    message = str(data.get('message') or '').strip()[:500] or MAINTENANCE_DEFAULT_TEXT
    ends_at = current['ends_at'] if enabled else 0
    if enabled and data.get('minutes') not in (None, ''):
        try:
            minutes = float(data.get('minutes'))
        except (TypeError, ValueError):
            return error('Укажите время в минутах числом.')
        if not math.isfinite(minutes) or minutes < 0 or minutes > 60 * 24 * 14:
            return error('Время должно быть от 0 до 20160 минут.')
        ends_at = int(time.time() * 1000 + minutes * 60000) if minutes > 0 else 0
    save_document('maintenance', dict(enabled=enabled, message=message, ends_at=ends_at,
                                      updated_at=datetime.now(timezone.utc).isoformat(), admin_id=session['uid']))
    _maint_cache['v'] = None
    state = maintenance_state(fresh=True)
    return jsonify(ok=True, enabled=state['enabled'], message=state['message'], ends_at=state['ends_at'],
                   now=int(time.time() * 1000))


def halloween_state(fresh=False):
    doc = read_document('halloween') or {}
    def _ms(key):
        try:
            return max(0, int(doc.get(key) or 0))
        except (TypeError, ValueError):
            return 0
    enabled, starts, ends = bool(doc.get('enabled')), _ms('starts_at'), _ms('ends_at')
    now = int(time.time() * 1000)
    active = enabled and (not starts or now >= starts) and (not ends or now < ends)
    return dict(enabled=enabled, starts_at=starts, ends_at=ends, active=active, now=now)


@app.get('/api/halloween')
def public_halloween():
    s = halloween_state()
    return jsonify(active=s['active'], starts_at=s['starts_at'], ends_at=s['ends_at'], now=s['now'])


@app.get('/api/admin/halloween')
@admin_required
def admin_halloween_get():
    return jsonify(**halloween_state())


@app.post('/api/admin/halloween')
@admin_required
def admin_halloween_set():
    data = request.get_json(silent=True) or {}
    def _ms(value):
        try:
            value = int(float(value or 0))
        except (TypeError, ValueError):
            return None
        return value if 0 <= value < 4102444800000 else None
    starts, ends = _ms(data.get('starts_at')), _ms(data.get('ends_at'))
    if starts is None or ends is None:
        return error('Некорректная дата.')
    if starts and ends and ends <= starts:
        return error('Конец должен быть позже начала.')
    save_document('halloween', dict(enabled=bool(data.get('enabled')), starts_at=starts, ends_at=ends,
                                    updated_at=datetime.now(timezone.utc).isoformat(), admin_id=session['uid']))
    return jsonify(ok=True, **halloween_state())


def ban_user_view(user_id):
    with connect() as db:
        row = db.execute('SELECT id,banned,ban_reason,ban_kind,banned_at,ban_immune FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return None
        ips = db.execute('SELECT ip,last_seen FROM user_ips WHERE user_id=? ORDER BY last_seen DESC LIMIT 10', (user_id,)).fetchall()
        items = []
        for entry in ips:
            others = db.execute('SELECT COUNT(DISTINCT user_id) AS c FROM user_ips WHERE ip=?', (entry['ip'],)).fetchone()
            items.append(dict(ip=entry['ip'], last_seen=int(entry['last_seen'] or 0), accounts=int(others['c'] or 0)))
    return dict(banned=bool(row['banned']), reason=row['ban_reason'] or '', kind=row['ban_kind'] or '',
                banned_at=row['banned_at'] or '', immune=bool(row['ban_immune']),
                is_admin=int(user_id) in ADMIN_IDS, is_creator=is_ban_exempt(user_id) and int(user_id) not in ADMIN_IDS,
                ips=items)


@app.get('/api/admin/users/<int:user_id>/ban')
@admin_required
def admin_user_ban_get(user_id):
    view = ban_user_view(user_id)
    if view is None:
        return error('Пользователь не найден.', 404)
    return jsonify(**view)


@app.post('/api/admin/users/<int:user_id>/ban')
@admin_required
def admin_user_ban_set(user_id):
    data = request.get_json(silent=True) or {}
    if user_id in ADMIN_IDS:
        return error('Администратора нельзя заблокировать.', 403)
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
    if data.get('banned'):
        ban_user(user_id, str(data.get('reason') or 'Блокировка администратором').strip()[:300], kind='manual', by=session['uid'])
    else:
        unban_user(user_id, by=session['uid'])
    return jsonify(ok=True, **ban_user_view(user_id))


@app.get('/api/admin/antifraud')
@admin_required
def admin_antifraud_get():
    since = int(time.time()) - MULTI_IP_WINDOW_DAYS * 86400
    groups = []
    with connect() as db:
        ips = db.execute("SELECT ip, COUNT(DISTINCT user_id) AS c FROM user_ips WHERE last_seen>=? "
                         "GROUP BY ip HAVING COUNT(DISTINCT user_id)>=2 ORDER BY c DESC, ip LIMIT 60", (since,)).fetchall()
        for entry in ips:
            users = db.execute("SELECT u.id,u.name,u.username,u.banned,u.ban_kind FROM users u "
                               "WHERE u.id IN (SELECT user_id FROM user_ips WHERE ip=? AND last_seen>=?) "
                               "ORDER BY u.created_at, u.id LIMIT 40", (entry['ip'], since)).fetchall()
            groups.append(dict(ip=entry['ip'], count=int(entry['c']), users=[
                dict(id=u['id'], name=u['name'], username=u['username'] or '', banned=bool(u['banned']),
                     kind=u['ban_kind'] or '', exempt=is_ban_exempt(u['id'])) for u in users]))
        banned = db.execute("SELECT id,name,username,ban_kind,ban_reason,banned_at FROM users WHERE banned=1 "
                            "ORDER BY banned_at DESC LIMIT 100").fetchall()
    return jsonify(settings=antifraud_settings(), groups=groups,
                   banned=[dict(id=u['id'], name=u['name'], username=u['username'] or '', kind=u['ban_kind'] or '',
                                reason=u['ban_reason'] or '', banned_at=u['banned_at'] or '') for u in banned])


@app.post('/api/admin/antifraud')
@admin_required
def admin_antifraud_set():
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get('ip_limit'))
    except (TypeError, ValueError):
        return error('Лимит аккаунтов должен быть целым числом.')
    if not 1 <= limit <= 50:
        return error('Лимит аккаунтов с одного IP: от 1 до 50.')
    save_document('antifraud_settings', dict(enabled=bool(data.get('enabled')), ip_limit=limit,
                                             updated_at=datetime.now(timezone.utc).isoformat(), admin_id=session['uid']))
    return jsonify(ok=True, settings=antifraud_settings())


@app.post('/api/admin/antifraud/scan')
@admin_required
def admin_antifraud_scan():
    since = int(time.time()) - MULTI_IP_WINDOW_DAYS * 86400
    with connect() as db:
        ips = db.execute('SELECT ip FROM user_ips WHERE last_seen>=? GROUP BY ip HAVING COUNT(DISTINCT user_id)>=2', (since,)).fetchall()
    banned = []
    for entry in ips:
        banned.extend(check_multi_account(entry['ip']))
    return jsonify(ok=True, banned=len(banned), ids=banned[:50])



@app.before_request
def enforce_available_modes():
    path = request.path
    if request.is_json and request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not (
            request.method == 'DELETE' and not request.get_data(cache=True)):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error('Ожидается JSON-объект с параметрами запроса.', 400)
    uid = session.get('uid')
    if uid and creator_demo_active(uid) and request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        real_money_prefixes = (
            '/api/transfers/send', '/api/deposit', '/api/stars', '/api/hilo/', '/api/limbo/'
        )
        if any(path.startswith(prefix) for prefix in real_money_prefixes):
            return error('Демо-режим активен. Отключите его в панели автора для операций с реальными средствами.', 409)
    # Craft is retired from the product. Keep its old data/code for safe migration,
    # but make the API inaccessible so stale clients cannot start new crafts.
    if path.startswith('/api/craft/'):
        return error('Крафты отключены.', 404)
    mode = ('giveaways' if path == '/api/giveaways' or path.startswith('/api/giveaways/') else
            'upgrade' if path.startswith('/api/upgrade/') else
            'mines' if path.startswith('/api/game/') else None)
    if mode and path not in ('/api/game/open', '/api/game/cashout') and not section_settings().get(mode, False):
        return error('Данный режим временно недоступен.', 403)
    # Per-game switch from "Управление играми": on / off / admins only.
    # Finishing an already started round (open/cashout) always stays possible.
    game_key = ('arena' if path.startswith('/api/arena/') else
                'hilo' if path.startswith('/api/hilo/') else
                'crash' if path.startswith('/api/crash/') else
                'limbo' if path.startswith('/api/limbo/') else
                'upgrade' if path.startswith('/api/upgrade/') else
                'mines' if path.startswith('/api/game/') else None)
    # A Hi-Lo game that is already running can always be finished or cashed out.
    finishing = ('/api/game/open', '/api/game/cashout', '/api/hilo/state', '/api/hilo/guess',
                 '/api/hilo/cashout', '/api/hilo/prize')
    if game_key and path not in finishing and not game_available(game_key):
        return error('Игра временно недоступна.', 403)


def game_rtp():
    """Long-run payout ratio used for standard Mines rounds.

    Default 92%. The mandatory first-step multiplier of 1.01x still applies, so on the
    first click in the 1-mine mode the payout stays at 1.01x (effective RTP of that single
    step is ~97%); every further step follows the configured RTP.
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
              'deposit':'Пополнение', 'ton_deposit':'Пополнение TON', 'stars_deposit':'Пополнение Stars', 'referral_bonus':'Реферальный бонус',
              'deposit_promo_bonus':'Бонус пополнения', 'withdrawal_request':'Заявка на вывод',
              'withdrawal_approved':'Вывод выполнен', 'withdrawal_rejected':'Подарок возвращён',
              'game_win_ton':'Выигрыш Mines', 'gift_win':'Выигран подарок',
              'promo_wager_claim':'Подарок отыгран', 'upgrade_cashback':'Компенсация апгрейда',
              'upgrade_compensation_gift':'Компенсационный подарок'}
    if kind in labels:
        if kind in ('deposit', 'ton_deposit', 'stars_deposit'):
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
    'transfer_received','promo_wager_burn','daily_top_reward',
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
            lock_row = db.execute('SELECT pg_try_advisory_xact_lock(660105) AS locked').fetchone()
            if not lock_row or not bool(lock_row['locked']):
                app.logger.info('Skipping legacy Upgrade wager repair: advisory lock 660105 is busy')
                return False
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
        return True
    finally:db.close()


def repair_zero_price_top_gifts():
    """Daily-top gifts saved with a 0 TON price get their current catalog price (one time)."""
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if DATABASE_URL:
            lock_row = db.execute('SELECT pg_try_advisory_xact_lock(660106) AS locked').fetchone()
            if not lock_row or not bool(lock_row['locked']):
                app.logger.info('Skipping zero-price top gift repair: advisory lock 660106 is busy')
                return False
        if db.execute('SELECT 1 FROM schema_migrations WHERE name=?', ('repair_zero_price_top_gifts_v1',)).fetchone():
            return
        rows = db.execute("""SELECT id,gift_id FROM inventory
                             WHERE source IN ('daily_top_catalog','daily_top_fragment') AND COALESCE(floor_price,0)<=0""").fetchall()
        for row in rows:
            price = catalog_price_cents(row['gift_id'])
            if price > 0:
                db.execute('UPDATE inventory SET floor_price=? WHERE id=?', (price, row['id']))
        db.execute('INSERT OR IGNORE INTO schema_migrations(name) VALUES(?)', ('repair_zero_price_top_gifts_v1',))
        db.commit()
        return True
    except Exception:
        app.logger.exception('Zero-price top gift repair failed')
        return False
    finally:
        db.close()


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

    unlock_payload = {}
    try:
        raw_unlock = optional('promo_unlock_payload', '{}')
        unlock_payload = json.loads(raw_unlock or '{}') if isinstance(raw_unlock, str) else (raw_unlock or {})
        if not isinstance(unlock_payload, dict):
            unlock_payload = {}
    except (TypeError, ValueError, json.JSONDecodeError):
        unlock_payload = {}
    unlock_target = None
    if unlock_payload.get('gift_id') or unlock_payload.get('gift_name'):
        unlock_target = dict(
            gift_id=str(unlock_payload.get('gift_id') or ''),
            name=str(unlock_payload.get('gift_name') or 'Подарок'),
            image_url=safe_image(unlock_payload.get('image_url')),
            price_ton=int(unlock_payload.get('floor_price') or 0) / 100,
        )
    external_url = str(optional('external_url') or '')
    image_url = safe_image(row['image_url'])
    nft_match = re.search(r'/(?:nft|gift)/([A-Za-z0-9_-]+-\d+)(?:/|$|[?#])', external_url, re.I)
    if nft_match:
        exact_nft_image = f"https://nft.fragment.com/gift/{nft_match.group(1).lower()}.webp"
        # Relayr deposits should always prefer the exact collectible artwork,
        # not a generic collection image from the Portal catalog.
        if str(row['source'] or '') == 'gift_deposit' or not image_url:
            image_url = exact_nft_image
    return dict(id=row['id'], gift_id=row['gift_id'], name=row['gift_name'],
                image_url=image_url, price_ton=row['floor_price']/100,
                source=row['source'], created_at=row['created_at'],
                external_url=external_url, fragment_url=external_url,
                fragment_number=optional('fragment_number'), fragment_model=optional('fragment_model'),
                fragment_backdrop=optional('fragment_backdrop'), fragment_symbol=optional('fragment_symbol'),
                price_source=optional('price_source'), animation_url=optional('animation_url'),
                source_label=('' if nft_match else optional('source_label')),
                deposit_mirror=bool(int(optional('deposit_mirror', 0) or 0)),
                promo_locked=locked, promo_code=row['promo_code'] or '',
                wager_multiplier=float(row['promo_wager_multiplier'] or 0),
                wager_target=target/100, wager_progress=progress/100,
                wager_complete=bool(locked and target > 0 and progress >= target),
                wager_percent=(min(100.0, progress * 100.0 / target) if target > 0 else 0.0),
                wager_attempts_total=int(optional('promo_attempts_total', 1) or 1),
                wager_attempts_remaining=int(optional('promo_attempts_remaining', 1) or 0),
                wager_burn_on_loss=bool(int(optional('promo_burn_on_loss', 1) or 0)),
                unlock_target=unlock_target,
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
    rows = db.execute(f'''SELECT id,user_id,gift_name,expires_at,promo_locked,
                                 promo_wager_target,promo_wager_progress
                          FROM inventory WHERE {where}''', params).fetchall()
    now = datetime.now(timezone.utc)
    expired = []
    for row in rows:
        target = int(row['promo_wager_target'] or 0)
        progress = int(row['promo_wager_progress'] or 0)
        if bool(row['promo_locked']) and target > 0 and progress >= target:
            # Completed wager waits for an explicit unlock in the profile and no
            # longer expires while the user is deciding when to unlock it.
            continue
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
                                promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at,
                                promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,external_url,promo_unlock_payload)
                              VALUES(?,?,?,?,?,'promo_wager',?,1,?,?,?,?,?,?,?,?,?,?)""",
                            (row['user_id'], row['bet_gift_id'], row['bet_gift_name'], row['bet_gift_image'],
                             row['bet_gift_price'], row['id'], float(row['promo_wager_multiplier'] or 0),
                             target, progress, row['promo_code'] or '', None if completed else row['bet_expires_at'],
                             max(1, int(row['promo_attempts_total'] or 1)),
                             max(0, int(row['promo_attempts_remaining'] or 1)),
                             int(bool(row['promo_burn_on_loss'])), row['bet_external_url'] or '', '{}'))
        db.execute("""UPDATE rounds SET state='won',payout=0,prize_inventory_id=?,win_total=?,win_multiplier=?,
                      promo_progress_after=?,win_gift_name='',win_gift_image='',win_gift_price=NULL,
                      settled_at=? WHERE id=?""",
                   (cursor.lastrowid, amount, factor, progress, settled_at, row['id']))
        detail = (f'Отыгрыш набран — разблокируйте подарок в профиле: {progress/100:.2f}/{target/100:.2f} TON'
                  if completed else
                  f'Отыгрыш {row["bet_gift_name"]}: {progress/100:.2f}/{target/100:.2f} TON')
        record_transaction(db, row['user_id'], 'promo_wager_progress', amount, 'round', row['id'], detail)
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
                        wager_attempts_total=max(1, int(row['promo_attempts_total'] or 1)),
                        wager_attempts_remaining=max(0, int(row['promo_attempts_remaining'] or 1)),
                        wager_burn_on_loss=bool(row['promo_burn_on_loss']),
                        external_url=row['bet_external_url'] or '', fragment_url=row['bet_external_url'] or '',
                        expires_at=row['bet_expires_at'])
    return dict(id=row['id'], bet=row['bet']/100, bet_type=row['bet_type'], bet_gift=bet_gift,
                mines=row['mines'], opened=opened, state=row['state'],
                multiplier=round(factor, 6), potential=amount/100,
                positions=json.loads(row['positions']) if reveal or row['state'] != 'active' else [],
                payout=row['payout']/100, prize=prize, awarded=owned, lost_cell=row['lost_cell'],
                promo_progress_after=int(row['promo_progress_after'] or 0)/100,
                fairness=fairness_for('mines', row['id'], row['state'] != 'active'))


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



FAIRNESS_GAMES = {'mines', 'upgrade', 'crash', 'hilo', 'hilo_room', 'arena', 'roll', 'limbo'}
FAIRNESS_ALGORITHM = 'HMAC-SHA256/rejection-v1'


def fairness_client_seed(value=None, fallback=''):
    text = str(value or fallback or '').strip()
    if not text:
        text = secrets.token_hex(16)
    if len(text) > 128 or not re.fullmatch(r'[A-Za-z0-9._:@-]{8,128}', text):
        text = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return text[:128]


def fairness_make(game, user_id=0, client_seed='', nonce=0):
    game = str(game or '').strip().lower()
    if game not in FAIRNESS_GAMES:
        raise ValueError('Неизвестный режим Proof of Fairness.')
    server_seed = secrets.token_hex(32)
    return dict(id=secrets.token_hex(16), game=game, user_id=int(user_id or 0),
                server_seed=server_seed,
                server_hash=hashlib.sha256(server_seed.encode('utf-8')).hexdigest(),
                client_seed=fairness_client_seed(client_seed, f'{game}:{int(user_id or 0)}'),
                nonce=max(0, int(nonce or 0)), cursor=0)


def fairness_draw(proof, upper, cursor=None):
    upper = int(upper)
    if upper <= 0:
        raise ValueError('Proof of Fairness: upper должен быть положительным.')
    cursor = int(proof.get('cursor', 0) if cursor is None else cursor)
    limit = (1 << 256) - ((1 << 256) % upper)
    key = bytes.fromhex(str(proof['server_seed']))
    while True:
        message = f"{proof['game']}|{proof['client_seed']}|{int(proof['nonce'])}|{cursor}"
        digest = hmac.new(key, message.encode('utf-8'), hashlib.sha256).digest()
        value = int.from_bytes(digest, 'big')
        cursor += 1
        if value < limit:
            return value % upper, cursor, digest.hex()


def fairness_positions(proof, mines):
    items = list(range(25))
    cursor = int(proof.get('cursor', 0) or 0)
    for i in range(24, 0, -1):
        j, cursor, _ = fairness_draw(proof, i + 1, cursor)
        items[i], items[j] = items[j], items[i]
    return sorted(items[:int(mines)]), cursor


def fairness_store(db, proof, game_ref='', cursor=0, outcome=None, state='committed'):
    db.execute("""INSERT INTO fairness_records(
                   id,game,game_ref,user_id,server_seed,server_hash,client_seed,nonce,cursor,outcome_json,state,revealed_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
               (proof['id'], proof['game'], str(game_ref or ''), int(proof.get('user_id') or 0),
                proof['server_seed'], proof['server_hash'], proof['client_seed'], int(proof.get('nonce') or 0),
                int(cursor or 0), json.dumps(outcome or {}, ensure_ascii=False, separators=(',', ':')),
                state, datetime.now(timezone.utc).isoformat() if state == 'settled' else None))
    return proof['id']


def fairness_get(db, game=None, game_ref=None, proof_id=None):
    if proof_id:
        return db.execute('SELECT * FROM fairness_records WHERE id=?', (str(proof_id),)).fetchone()
    return db.execute('SELECT * FROM fairness_records WHERE game=? AND game_ref=? ORDER BY created_at DESC LIMIT 1',
                      (str(game), str(game_ref))).fetchone()


def fairness_public(row, reveal=None):
    if not row:
        return None
    reveal = (str(row['state']) == 'settled') if reveal is None else bool(reveal)
    try:
        outcome = json.loads(row['outcome_json'] or '{}') if reveal else None
    except (TypeError, ValueError, json.JSONDecodeError):
        outcome = {}
    result = dict(id=str(row['id']), game=str(row['game']), game_ref=str(row['game_ref'] or ''),
                  server_hash=str(row['server_hash']), client_seed=str(row['client_seed']),
                  nonce=int(row['nonce'] or 0), algorithm=FAIRNESS_ALGORITHM,
                  message_format='{game}|{client_seed}|{nonce}|{cursor}',
                  draw_method='HMAC-SHA256; 256-bit rejection sampling; result = value mod upper',
                  state='revealed' if reveal else 'committed')
    if reveal:
        result.update(server_seed=str(row['server_seed']), cursor=int(row['cursor'] or 0), outcome=outcome,
                      commitment_valid=(hashlib.sha256(str(row['server_seed']).encode('utf-8')).hexdigest()
                                        == str(row['server_hash'])))
    return result


def fairness_view(db, game, game_ref, reveal=False):
    return fairness_public(fairness_get(db, game=game, game_ref=str(game_ref)), reveal)


def fairness_for(game, game_ref, reveal=False):
    with connect() as db:
        return fairness_view(db, game, game_ref, reveal)


def fairness_set_progress(db, proof_id, cursor, outcome):
    db.execute('UPDATE fairness_records SET cursor=?,outcome_json=? WHERE id=?',
               (int(cursor or 0), json.dumps(outcome or {}, ensure_ascii=False, separators=(',', ':')), str(proof_id)))


def fairness_mark_settled(db, game, game_ref, outcome=None, cursor=None):
    row = fairness_get(db, game=game, game_ref=str(game_ref))
    if not row:
        return None
    sets = ["state='settled'", 'revealed_at=?']
    values = [datetime.now(timezone.utc).isoformat()]
    if cursor is not None:
        sets.append('cursor=?'); values.append(int(cursor))
    if outcome is not None:
        sets.append('outcome_json=?'); values.append(json.dumps(outcome, ensure_ascii=False, separators=(',', ':')))
    values.append(str(row['id']))
    db.execute('UPDATE fairness_records SET ' + ','.join(sets) + ' WHERE id=?', tuple(values))
    return fairness_get(db, proof_id=row['id'])


def fairness_resolve_action(db, game, user_id, data):
    proof_id = str((data or {}).get('fairness_id') or '').strip()
    row = fairness_get(db, proof_id=proof_id) if re.fullmatch(r'[0-9a-f]{32}', proof_id) else None
    if row and str(row['game']) == game and int(row['user_id'] or 0) == int(user_id) and str(row['state']) == 'committed' and not str(row['game_ref'] or ''):
        return row
    proof = fairness_make(game, user_id, (data or {}).get('client_seed'))
    fairness_store(db, proof)
    return fairness_get(db, proof_id=proof['id'])


def fairness_complete_action(db, row, game_ref, cursor, outcome):
    db.execute("""UPDATE fairness_records SET game_ref=?,cursor=?,outcome_json=?,state='settled',revealed_at=?
                  WHERE id=? AND state='committed'""",
               (str(game_ref), int(cursor or 0), json.dumps(outcome or {}, ensure_ascii=False, separators=(',', ':')),
                datetime.now(timezone.utc).isoformat(), str(row['id'])))
    return fairness_get(db, proof_id=row['id'])


@app.post('/api/fairness/prepare')
@login_required
def fairness_prepare():
    data = request.get_json(silent=True) or {}
    game = str(data.get('game') or '').strip().lower()
    if game not in ('upgrade', 'roll'):
        return error('Для этого режима proof создаётся вместе с раундом.', 400)
    with connect() as db:
        db.execute("""DELETE FROM fairness_records
                      WHERE user_id=? AND game=? AND state='committed' AND game_ref=''""",
                   (session['uid'], game))
        proof = fairness_make(game, session['uid'], data.get('client_seed'))
        fairness_store(db, proof)
        row = fairness_get(db, proof_id=proof['id'])
    return jsonify(fairness=fairness_public(row, False))


@app.get('/api/fairness/<proof_id>')
@login_required
def fairness_details(proof_id):
    if not re.fullmatch(r'[0-9a-f]{32}', str(proof_id or '')):
        return error('Proof не найден.', 404)
    with connect() as db:
        row = fairness_get(db, proof_id=proof_id)
        if not row or int(row['user_id'] or 0) not in (0, int(session['uid'])):
            return error('Proof не найден.', 404)
        return jsonify(fairness=fairness_public(row))



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
    creator = creator_record(session['uid'])
    demo = bool(creator.get('demo_enabled'))
    with connect() as db:
        if demo:
            turnover = int(creator.get('demo_turnover_cents') or 0)
            raw_claims = creator.get('demo_level_claims') or {}
            claims = {}
            for key, value in raw_claims.items():
                try:
                    claims[int(key)] = value
                except (TypeError, ValueError):
                    continue
        else:
            turnover=int(db.execute('SELECT turnover_cents FROM users WHERE id=?',(session['uid'],)).fetchone()['turnover_cents'] or 0)
            claims={r['level']:json.loads(r['reward_json']) for r in db.execute('SELECT level,reward_json FROM level_claims WHERE user_id=?',(session['uid'],)).fetchall()}
        rows=db.execute('SELECT * FROM levels ORDER BY level').fetchall()
    level=max((int(r['level']) for r in rows if turnover>=r['required_turnover']),default=1)
    current=next(r for r in rows if r['level']==level)
    nxt=next((r for r in rows if r['level']>level),None)
    progress=100 if not nxt else max(0,min(100,(turnover-current['required_turnover'])*100/(nxt['required_turnover']-current['required_turnover'])))
    return jsonify(level=level,max_level=len(rows),turnover=turnover/100,demo=demo,
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


# ---- Level plan v4: 100 levels, every reward is sized from the casino margin earned on that level ----
LEVEL_PLAN_MIN_GIFT_TON = 3.3   # cheapest Portal gift we ever hand out
LEVEL_PLAN_BUDGET_SHARE = 0.06  # share of the casino margin (8% of turnover) spent on level rewards
LEVEL_PLAN_EDGE = 0.08          # casino edge on wagering (game RTP 92%)


def level_plan_threshold_ton(level):
    return 0.0 if level <= 1 else round((level - 1) ** 2.1, 2)


def level_plan_unit_ton(level):
    """Reward budget of one level (casino cost, TON): share of the margin earned between level-1 and level."""
    gain = level_plan_threshold_ton(level) - level_plan_threshold_ton(level - 1)
    return LEVEL_PLAN_BUDGET_SHARE * 0.08 * gain


def _lp_x_base(level):
    """Wagering multiplier of a minimum-size gift: grows smoothly with the level (X8 -> X15)."""
    if level < 20: return 8
    if level < 30: return 9
    if level < 45: return 10
    if level < 60: return 11
    if level < 75: return 12
    if level < 90: return 14
    return 15


def _lp_x(level, price=0.0):
    """Wagering multiplier for a gift of `price` TON at `level`: the bigger the gift, the higher the X."""
    extra = 0 if price < 8 else 1 if price < 12 else 2 if price < 18 else 3 if price < 25 else 5
    return min(20, _lp_x_base(level) + extra)


def _lp_share(x):
    """Casino cost of a wager gift as a share of its price: the gift is paid out, the wagering wins back
    edge*X of its price (never counted above 88%, as part of the players never finish the wagering)."""
    return max(0.12, 1.0 - LEVEL_PLAN_EDGE * x)


def _lp_days(level):
    return 30 if level < 50 else 45 if level < 90 else 60


def _lp_wager(cost, level, days=None):
    """Wager gift whose casino cost equals `cost`; never cheaper than the Portal minimum."""
    price = max(LEVEL_PLAN_MIN_GIFT_TON, cost / _lp_share(_lp_x_base(level)))
    x = _lp_x(level, price)
    price = max(LEVEL_PLAN_MIN_GIFT_TON, cost / _lp_share(x))
    return {'type': 'wager_gift', 'gift_price_ton': round(price, 1),
            'wager_multiplier': _lp_x(level, price), 'gift_expires_days': days or _lp_days(level)}


def _lp_gift(cost):
    return {'type': 'gift', 'gift_price_ton': round(max(LEVEL_PLAN_MIN_GIFT_TON, cost), 1)}


def _lp_balance(cost):
    return {'type': 'balance', 'amount_ton': round(max(0.1, cost), 2)}


def _lp_deposit(percent, min_ton, days=7):
    return {'type': 'deposit_promo', 'bonus_percent': percent, 'min_deposit_ton': min_ton, 'expires_days': days}


def _lp_deposit_cost(cost, level):
    """Deposit bonus whose cost model (25% of the bonus on its minimum deposit) equals `cost`."""
    percent = 10 if level < 60 else 15 if level < 85 else 20
    minimum = max(5, int(round(cost * 400 / percent / 5.0)) * 5)
    return _lp_deposit(percent, minimum)


def _lw(price, days=60):
    """Finale wager gift; X is filled in from the level and the price in level_plan_reward."""
    return {'gift_price_ton': float(price), 'wager_multiplier': None, 'gift_expires_days': days}


LEVEL_PLAN_FINALE = {
    91: {'type': 'wager_gift', **_lw(8)},
    92: {'type': 'multi_promo', 'expires_days': 60, 'components': {
        'wager_gift': _lw(10), 'balance': {'amount_ton': 1.2}}},
    93: {'type': 'gift', 'gift_price_ton': 5.0},
    94: {'type': 'multi_promo', 'expires_days': 60, 'components': {
        'wager_gift': _lw(12), 'balance': {'amount_ton': 2.5}}},
    95: {'type': 'wager_gift', **_lw(15)},
    96: {'type': 'multi_promo', 'expires_days': 60, 'components': {
        'gift': {'gift_price_ton': 6.0}, 'balance': {'amount_ton': 1.5}}},
    97: {'type': 'wager_gift', **_lw(20)},
    98: {'type': 'multi_promo', 'expires_days': 60, 'components': {
        'gift': {'gift_price_ton': 7.0}, 'balance': {'amount_ton': 3.0}}},
    99: {'type': 'multi_promo', 'expires_days': 60, 'components': {
        'gift': {'gift_price_ton': 10.0}, 'wager_gift': _lw(10), 'balance': {'amount_ton': 2.5}}},
    100: {'type': 'multi_promo', 'expires_days': 60, 'components': {
        'gift': {'gift_price_ton': 18.0}, 'wager_gift': _lw(25), 'balance': {'amount_ton': 8.0}}},
}


def _lp_finale(level):
    """Hand-set finale prize with its wagering multiplier resolved (deep copy, the table stays intact)."""
    spec = json.loads(json.dumps(LEVEL_PLAN_FINALE[level]))
    def fill(part):
        if part.get('wager_multiplier') is None and 'gift_price_ton' in part and 'gift_expires_days' in part:
            part['wager_multiplier'] = _lp_x(level, part['gift_price_ton'])
    fill(spec)
    for part in (spec.get('components') or {}).values():
        fill(part)
    return spec


def _lp_part(spec):
    """Component of a multi reward: the same spec without its type key."""
    return {k: v for k, v in spec.items() if k != 'type'}


def level_plan_reward(level):
    """Reward spec in TON terms; the concrete Portal gift is picked from the catalog on apply."""
    L = int(level)
    u = level_plan_unit_ton(L)
    r = L % 10
    if L <= 1:
        return {'type': 'none'}
    # 2-14: small turnover (up to ~220 TON): tickets, the first deposit bonuses and the first real gift
    if L <= 4:
        return {'type': 'tickets', 'tickets': 1}
    if L == 5:
        return _lp_deposit(10, 5)
    if L <= 8:
        return {'type': 'tickets', 'tickets': 2}
    if L == 9:
        return {'type': 'tickets', 'tickets': 3}
    if L == 10:
        return _lp_wager(0.83, L)
    if L <= 13:
        return {'type': 'tickets', 'tickets': 3 if L < 13 else 4}
    if L == 14:
        return _lp_deposit(10, 10)
    if L == 18:
        return {'type': 'transfer_unlock'}
    if L >= 91:  # finale: hand-set prizes sized for 12-15k turnover
        return _lp_finale(L)
    # 15-39: no tickets any more; TON on the balance is the main reward, sizes follow the level budget
    if L < 40:
        if r == 0:
            return {'type': 'multi_promo', 'expires_days': 30, 'components': {
                'wager_gift': _lp_part(_lp_wager(1.6 * u, L)),
                'balance': {'amount_ton': round(max(0.4, 1.6 * u), 2)}}}
        if r == 5:
            return _lp_wager(0.9 * u, L)
        if L == 28:
            return {'type': 'personal_promo', 'promo_reward_type': 'balance', 'expires_days': 14,
                    'amount_ton': round(max(0.4, 2.0 * u), 2)}
        if L == 35:
            return _lp_balance(2.5 * u)
        if L % 4 == 1:
            return _lp_deposit_cost(0.6 * u, L)
        return _lp_balance((1.6 if L % 2 else 1.4) * u)
    # 40-99: a 10-level cycle; every price scales with the turnover of that level
    if r == 0:  # milestone: multi reward (every second one also holds a plain gift)
        comps = {'wager_gift': _lp_part(_lp_wager(0.9 * u, L)),
                 'balance': {'amount_ton': round(max(0.8, 1.6 * u), 2)}}
        if L % 20 == 0:
            comps['gift'] = _lp_part(_lp_gift(1.4 * u))
        else:
            comps['deposit_bonus'] = _lp_part(_lp_deposit_cost(0.4 * u, L))
        return {'type': 'multi_promo', 'expires_days': 30, 'components': comps}
    if r == 5 or (r == 2 and u >= 1.4):  # plain gifts, no wagering (twice per cycle once the budget affords it)
        return _lp_gift(1.6 * u)
    if r == 7 and L in (47, 67, 87):  # personal promo code with a wager gift
        return {'type': 'personal_promo', 'promo_reward_type': 'wager_gift', 'expires_days': 14,
                **_lp_part(_lp_wager(0.5 * u, L))}
    if r in (1, 2, 7, 9):
        return _lp_wager(0.5 * u, L)
    if r in (3, 6, 8):  # TON straight to the balance
        return _lp_balance((1.2 if r == 3 else 1.6 if r == 6 else 1.4) * u)
    return _lp_deposit_cost(0.35 * u, L)  # r == 4


def level_plan_cost_ton(spec):
    """Casino cost model: wager gift = price * (1 - edge*X) (>=12%), plain gift / TON 100%, deposit bonus
    25% of the bonus on its minimum deposit, ticket 0.01 TON."""
    t = spec.get('type')
    if t == 'tickets':
        return spec['tickets'] * 0.01
    if t == 'balance':
        return spec['amount_ton']
    if t == 'gift':
        return spec['gift_price_ton']
    if t == 'wager_gift':
        return spec['gift_price_ton'] * _lp_share(float(spec.get('wager_multiplier') or 0))
    if t == 'deposit_promo':
        return spec['bonus_percent'] / 100 * spec['min_deposit_ton'] * 0.25
    if t == 'personal_promo':
        return level_plan_cost_ton({**spec, 'type': spec.get('promo_reward_type')})
    if t == 'multi_promo':
        return sum(level_plan_cost_ton({'type': 'deposit_promo' if n == 'deposit_bonus' else n, **c})
                   for n, c in spec['components'].items())
    return 0.0


def level_plan_pick_gift(gifts, target_ton, used=None):
    """Portal gift closest to target_ton from below, never cheaper than LEVEL_PLAN_MIN_GIFT_TON.
    Prefers gifts that were used least so far, so neighbouring levels do not repeat the same gift."""
    used = used if used is not None else {}
    floor = int(round(LEVEL_PLAN_MIN_GIFT_TON * 100))
    priced = []
    for g in gifts:
        try:
            cents = ton_to_cents(g['price_ton'])
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
        if cents >= floor and g.get('id') not in (None, ''):
            priced.append((cents, g))
    if not priced:
        return None
    target = int(round(target_ton * 100))
    under = [x for x in priced if x[0] <= target * 1.05]  # a hair above the target is fine, below is not
    if not under:
        return min(priced, key=lambda x: (x[0], str(x[1].get('id'))))[1]
    top = max(x[0] for x in under)
    pool = [x for x in under if x[0] >= top * 0.93]
    best = min(pool, key=lambda x: (used.get(str(x[1].get('id')), 0), -x[0], str(x[1].get('id'))))
    return best[1]


def _level_plan_data(spec, gifts, used):
    """Spec in TON terms -> payload for normalize_level_reward (Portal gifts are picked here)."""
    kind = spec['type']
    if kind in ('gift', 'wager_gift'):
        gift = level_plan_pick_gift(gifts, spec['gift_price_ton'], used)
        if not gift:
            raise ValueError('В каталоге Portal нет подарков от %.1f TON.' % LEVEL_PLAN_MIN_GIFT_TON)
        used[str(gift.get('id'))] = used.get(str(gift.get('id')), 0) + 1
        data = {k: v for k, v in spec.items() if k != 'gift_price_ton'}
        data['gift_id'] = str(gift.get('id'))
        return data
    if kind == 'balance':
        return {'type': 'balance', 'amount': '%.2f' % spec['amount_ton']}
    if kind == 'personal_promo':
        data = _level_plan_data(dict(spec, type=spec['promo_reward_type']), gifts, used)
        data.update(type='personal_promo', promo_reward_type=spec['promo_reward_type'],
                    expires_days=spec.get('expires_days', 0))
        return data
    if kind == 'deposit_promo':
        return {'type': 'deposit_promo', 'bonus_percent': spec['bonus_percent'],
                'min_deposit': '%.2f' % spec['min_deposit_ton'], 'expires_days': spec.get('expires_days', 0)}
    if kind == 'multi_promo':
        components = {}
        for name, conf in spec['components'].items():
            sub = {'type': 'deposit_promo' if name == 'deposit_bonus' else name, **conf}
            components[name] = _level_plan_data(sub, gifts, used)
            components[name].pop('type', None)
        return {'type': 'multi_promo', 'components': components, 'expires_days': spec.get('expires_days', 0)}
    return dict(spec)


def level_plan_build():
    gifts = read_catalog().get('gifts', [])
    used, rows, cum = {}, [], 0.0
    for level in range(1, 101):
        spec = level_plan_reward(level)
        reward = normalize_level_reward(_level_plan_data(spec, gifts, used))
        cost = level_plan_cost_ton(spec)
        cum += cost
        rows.append(dict(level=level, required_turnover=int(round(level_plan_threshold_ton(level) * 100)),
                         reward=reward, cost_ton=round(cost, 3), cumulative_cost_ton=round(cum, 2)))
    return rows


def level_plan_is_untouched(db):
    """True while the level table is still the factory one: no rewards configured and nothing claimed."""
    if db.execute('SELECT 1 FROM level_claims LIMIT 1').fetchone():
        return False
    for row in db.execute('SELECT reward_json FROM levels').fetchall():
        try:
            reward = json.loads(row['reward_json'] or '{}')
        except (ValueError, TypeError):
            return False
        if reward.get('type', 'none') != 'none':
            return False
    return True


def level_plan_apply(reset_users=False, admin_id=None, source='manual'):
    """Replace the level table with the 100-level plan (Portal gifts picked from the live catalog)."""
    rows = level_plan_build()
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        last = db.execute('SELECT fee_percent,enabled FROM transfer_rates ORDER BY level DESC LIMIT 1').fetchone()
        fee, enabled = (last['fee_percent'], last['enabled']) if last else (5, 1)
        if reset_users:
            db.execute('DELETE FROM level_claims')
        db.execute('DELETE FROM levels')
        for r in rows:
            db.execute('INSERT INTO levels(level,required_turnover,reward_json) VALUES(?,?,?)',
                       (r['level'], r['required_turnover'], json.dumps(r['reward'], ensure_ascii=False)))
            db.execute('INSERT OR IGNORE INTO transfer_rates(level,fee_percent,enabled) VALUES(?,?,?)',
                       (r['level'], fee, enabled))
        db.execute('DELETE FROM transfer_rates WHERE level NOT IN (SELECT level FROM levels)')
        if reset_users:
            db.execute('UPDATE users SET turnover_cents=0')
        if admin_id:
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (admin_id, admin_id, 'level_plan_apply', 'reset' if reset_users else 'keep'))
        db.execute('INSERT OR IGNORE INTO schema_migrations(name) VALUES(?)', ('levels_plan_v3_applied',))
        db.commit()
    finally:
        db.close()
    names = []
    for r in rows:
        rw = r['reward']
        parts = [rw] if rw.get('type') != 'multi_promo' else list((rw.get('components') or {}).values())
        for part in parts:
            if part.get('gift_name'):
                names.append(dict(level=r['level'], name=part['gift_name'], price_ton=part.get('gift_price', 0) / 100))
    save_document('levels_plan_state', dict(version='v3', source=source, levels=len(rows),
                                             applied_at=datetime.now(timezone.utc).isoformat(), gifts=names))
    return rows


@app.get('/api/admin/levels/plan')
@admin_required
def admin_levels_plan():
    try:
        rows = level_plan_build()
    except ValueError as exc:
        return error(str(exc))
    return jsonify(ok=True, state=read_document('levels_plan_state'),
                   levels=[dict(level=r['level'], required_turnover=r['required_turnover'] / 100,
                                cost_ton=r['cost_ton'], cumulative_cost_ton=r['cumulative_cost_ton'],
                                reward=public_level_reward(r['reward'])) for r in rows])


@app.post('/api/admin/levels/plan/apply')
@admin_required
def admin_levels_plan_apply():
    """Replace the level table with the 100-level plan. reset_users=true also zeroes everybody's
    turnover and claimed rewards. Requires {"confirm": "APPLY"}."""
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'APPLY':
        return error('Передайте confirm="APPLY".')
    reset_users = bool(data.get('reset_users'))
    try:
        rows = level_plan_apply(reset_users, session['uid'], 'admin')
    except ValueError as exc:
        return error(str(exc))
    return jsonify(ok=True, levels=len(rows), reset_users=reset_users)


def level_plan_autoapply_loop():
    """First start on a fresh host: wait for the Portal catalog, then fill the 100 levels with real gifts.
    Runs once (schema_migrations marker) and only while the level table is still untouched.
    LEVELS_PLAN_AUTOAPPLY=0 disables it."""
    if os.environ.get('LEVELS_PLAN_AUTOAPPLY', '1') != '1':
        return
    time.sleep(10)
    last_fetch = 0.0
    while True:
        try:
            with connect() as db:
                done = db.execute("SELECT 1 FROM schema_migrations WHERE name IN (?,?)",
                                  ('levels_plan_v3_applied', 'levels_plan_v3_skipped')).fetchone()
                if done:
                    return
                if not level_plan_is_untouched(db):
                    db.execute('INSERT OR IGNORE INTO schema_migrations(name) VALUES(?)', ('levels_plan_v3_skipped',))
                    app.logger.info('Levels plan v3: levels already customised, auto-apply skipped (use admin apply).')
                    return
            doc = read_document('portal_catalog') or {}
            gifts = read_catalog().get('gifts', []) if doc else []
            usable = [g for g in gifts if level_plan_pick_gift([g], 1000000) is not None]
            if len(usable) >= 10 and not doc.get('partial'):
                level_plan_apply(False, None, 'first_start')
                append_portal_log('Уровни: план на 100 уровней применён, подарки подобраны из каталога Portal.')
                app.logger.info('Levels plan v3 applied automatically.')
                return
            if not doc.get('partial') and time.time() - last_fetch > 600 and portal_job_lock.acquire(blocking=False):
                last_fetch = time.time()
                append_portal_log('Уровни: каталог Portal пуст, загружаем его для плана уровней.')
                Thread(target=portal_job, args=(saved_portal_key(),), daemon=True).start()
        except Exception:
            app.logger.exception('Levels plan auto-apply loop failed')
        time.sleep(30)


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


def claim_demo_level(level):
    uid = session['uid']
    record = creator_record(uid)
    claims = dict(record.get('demo_level_claims') or {})
    with connect() as db:
        row = db.execute('SELECT * FROM levels WHERE level=?', (level,)).fetchone()
    if not row:
        return error('Уровень не найден.', 404)
    if int(record.get('demo_turnover_cents') or 0) < int(row['required_turnover'] or 0):
        return error('Достигните уровня, чтобы получить награду.', 403)
    if str(level) in claims:
        return error('Награда уже получена.', 409)
    reward = json.loads(row['reward_json'] or '{}')
    kind = reward.get('type', 'none')
    if kind == 'none':
        return error('На этом уровне награда не назначена.')
    result = {}
    if kind == 'balance':
        amount = max(0, int(reward.get('amount') or 0))
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + amount
        result = dict(type='balance', amount=amount / 100)
    elif kind == 'tickets':
        tickets = max(0, int(reward.get('tickets') or 0))
        record['demo_tickets'] = int(record.get('demo_tickets') or 0) + tickets
        result = dict(type='tickets', tickets=tickets)
    elif kind in ('gift', 'wager_gift'):
        gift_price = max(0, int(reward.get('gift_price') or 0))
        item = demo_add_catalog_gift(record, dict(
            id=reward.get('gift_id'), name=reward.get('gift_name') or 'Подарок',
            image_url=reward.get('image_url'), price_cents=gift_price), 'creator_demo_level')
        if kind == 'wager_gift':
            multiplier = float(reward.get('wager_multiplier') or 0)
            item['promo_locked'] = True
            item['wager_multiplier'] = multiplier
            item['wager_target'] = gift_price * multiplier / 100
            item['wager_progress'] = 0
            item['wager_complete'] = False
            item['wager_percent'] = 0
            days = max(0, int(reward.get('gift_expires_days') or 0))
            item['expires_at'] = ((datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None)
        result = dict(type=kind, gift=dict(item))
    else:
        code = f'DEMO-L{level}-{secrets.token_hex(2).upper()}'
        result = dict(type='demo_promo', code=code, description='Демо-награда уровня')
    claims[str(level)] = result
    record['demo_level_claims'] = claims
    save_creator_record(uid, record)
    return jsonify(ok=True, reward=result, user=profile(),
                   message='Награда уровня получена в demo-режиме.')


@app.post('/api/levels/<int:level>/claim')
@login_required
def claim_level(level):
    if creator_demo_active(session['uid']):
        return claim_demo_level(level)
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
        current_profile=profile()
        if result.get('code'):
            notify_promo_async(session['uid'], result['code'], 'levels')
            message=f'Промокод {result["code"]} отправлен вам в бота. Нажмите на этот уровень, чтобы увидеть его снова.'
        elif result.get('type')=='tickets':
            message=f'+{int(result.get("tickets") or 0)} билет(ов). Теперь у вас {int(current_profile.get("tickets") or 0)} билет(ов).'
        elif result.get('type')=='balance':
            message=f'+{float(result.get("amount") or 0):.2f} TON зачислено на баланс.'
        elif result.get('type') in ('gift','wager_gift'):
            message=f'{(result.get("gift") or {}).get("name") or "Подарок"} добавлен в инвентарь.'
        elif result.get('type')=='transfer_unlock':
            message='Переводы TON разблокированы.'
        else:
            message='Награда уровня получена.'
        return jsonify(ok=True,reward=result,user=current_profile,message=message)
    finally:db.close()

def reward_description(reward):
    if reward.get('type')=='none':return 'Без награды'
    if reward.get('type')=='transfer_unlock':return 'Доступ к переводам TON'
    if reward.get('type')=='tickets':return f"{int(reward.get('tickets') or 0)} билет(ов) для розыгрышей"
    if reward.get('type')=='multi_promo':return 'Набор наград · '+' + '.join(reward_description(part) for part in (reward.get('components') or {}).values())
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
    data=request.get_json(silent=True) or {}
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
        proof=fairness_resolve_action(db,'roll',session['uid'],data)
        fair_upper=sum(weights)
        fair_ticket,fair_cursor,fair_digest=fairness_draw(proof,fair_upper,0)
        ticket=fair_ticket
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
        proof=fairness_complete_action(db,proof,spin_id,fair_cursor,{
            'ticket':fair_ticket,'upper':fair_upper,'digest':fair_digest,'index':index,
            'weights':weights,'entry_id':entry['id'],'kind':entry['kind']})
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
                       applied_boost=boost,new_level=new_level,user=profile(),
                       fairness=fairness_public(proof,True))
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
    loss -= int((cashback['total'] if cashback else 0) or 0)
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
    try:return int((read_document('game_settings') or {}).get('upgrade_rtp_bp',8200))
    except (TypeError,ValueError):return 8200


@app.get('/api/upgrade/settings')
@login_required
def upgrade_settings():
    return jsonify(rtp=upgrade_rtp_basis_points()/100,min_chance=1,max_chance=80,max_target_multiplier=10,
                   min_bet_ton=0.1,max_bet_ton=MAX_UPGRADE_BET_CENTS/100)


@app.get('/api/upgrade/preview')
@login_required
def upgrade_preview():
    amount_text=request.args.get('amount')
    if creator_demo_active(session['uid']):
        try:
            return jsonify(demo_upgrade_preview(amount_text, request.args.get('inventory_id'), request.args.get('gift_id')))
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)

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
            return error('Отыгрыш завершён — сначала разблокируйте подарок в профиле.')
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


DAILY_TOP_TZ = timezone(timedelta(hours=3))
DAILY_TOP_CYCLE = timedelta(days=1)


def _daily_top_db_string(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _daily_top_default_window(now_utc=None):
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_local = now_utc.astimezone(DAILY_TOP_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + DAILY_TOP_CYCLE
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _daily_top_schedule_map():
    try:
        doc = read_document('daily_top_rewards') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    schedules = doc.get('_schedule')
    return schedules if isinstance(schedules, dict) else {}


def daily_top_window(mode, now_utc=None):
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    default_start, default_end = _daily_top_default_window(now_utc)
    raw = _daily_top_schedule_map().get(mode)
    if not isinstance(raw, dict):
        return default_start, default_end, False
    start = parse_datetime_utc(raw.get('start_at'))
    end = parse_datetime_utc(raw.get('end_at'))
    if not start or not end or end <= start:
        return default_start, default_end, False
    return start, end, True


def daily_top_reset_at(mode):
    try:
        doc = read_document('daily_top_rewards') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    resets = doc.get('_reset')
    if not isinstance(resets, dict):
        return None
    return parse_datetime_utc(resets.get(mode))


def daily_top_candidate_start(mode, period_start=None):
    if period_start is None:
        period_start, _, _ = daily_top_window(mode)
    reset_at = daily_top_reset_at(mode)
    if reset_at and reset_at > period_start:
        return reset_at
    return period_start


def daily_top_schedule_view(mode, now_utc=None):
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start, end, custom = daily_top_window(mode, now_utc)
    reset_at = daily_top_reset_at(mode)
    return dict(
        start_at=start.isoformat().replace('+00:00', 'Z'),
        end_at=end.isoformat().replace('+00:00', 'Z'),
        server_now=now_utc.isoformat().replace('+00:00', 'Z'),
        seconds_left=max(0, int((end-now_utc).total_seconds())),
        reset_at=reset_at.isoformat().replace('+00:00', 'Z') if reset_at else None,
        custom=custom,
        period_label=_daily_top_period_label(start,end),
    )


def wins_day_start_utc(mode='mines'):
    start, _, _ = daily_top_window(mode)
    return _daily_top_db_string(start)


def wins_feed_cutoff(db, kind):
    row = db.execute('SELECT cleared_at,max_round_id FROM wins_feed_clears WHERE kind=?', (kind,)).fetchone()
    return (row['cleared_at'], int(row['max_round_id'] or 0)) if row else ('', 0)


def catalog_price_cents(gift_id):
    """Current catalog price in cents for a gift id (0 when unknown)."""
    try:
        gifts = read_catalog(include_hidden=True).get('gifts', [])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0
    gift = next((x for x in gifts if str(x.get('id')) == str(gift_id)), None)
    if not gift:
        return 0
    try:
        return max(0, ton_to_cents(gift.get('price_ton') or 0))
    except (ValueError, TypeError, InvalidOperation):
        return 0


def refresh_top_reward_price(reward):
    """A top-of-the-day gift must never be awarded for 0 TON: re-price it when the saved price is empty."""
    if not isinstance(reward, dict) or reward.get('type') not in ('catalog', 'fragment'):
        return reward
    try:
        price = int(reward.get('floor_price') or 0)
    except (TypeError, ValueError):
        price = 0
    if price > 0:
        return reward
    fresh = dict(reward)
    if reward.get('type') == 'catalog':
        price = catalog_price_cents(reward.get('gift_id'))
    else:
        try:
            gift = fragment_gift_from_url(reward.get('fragment_url'), True, refresh=True, allow_missing_price=True)
            price = int(gift.get('floor_price') or 0)
            if price > 0:
                fresh['price_source'] = gift.get('price_source') or fresh.get('price_source') or ''
        except Exception:
            app.logger.warning('Could not refresh daily top fragment price', exc_info=True)
            price = 0
        if price <= 0:
            # Last resort: the collection floor from the catalog, matched by gift name.
            try:
                base = re.sub(r'\s*#\s*\d+.*$', '', str(reward.get('gift_name') or '')).strip().lower()
                gifts = read_catalog(include_hidden=True).get('gifts', [])
                prices = []
                for g in gifts:
                    if str(g.get('name') or '').strip().lower() == base:
                        try:
                            prices.append(ton_to_cents(g.get('price_ton') or 0))
                        except (ValueError, TypeError, InvalidOperation):
                            pass
                prices = [x for x in prices if x > 0]
                price = min(prices) if prices else 0
            except Exception:
                price = 0
    fresh['floor_price'] = max(0, int(price))
    fresh['price_ton'] = fresh['floor_price'] / 100
    return fresh


def daily_top_rewards():
    try:
        doc = read_document('daily_top_rewards') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        app.logger.exception('Invalid daily top reward settings; falling back to no rewards')
        doc = {}
    result = {}
    for mode in ('mines','upgrade','hilo'):
        raw = doc.get(mode) if isinstance(doc,dict) else None
        reward = raw if isinstance(raw,dict) else {}
        reward_type = str(reward.get('type') or 'none')
        if reward_type not in ('none','gram','catalog','fragment'):
            reward_type = 'none'
        item = {'type': reward_type}
        if reward_type == 'gram':
            try: item['amount'] = max(0.0, float(reward.get('amount') or 0))
            except (TypeError,ValueError): item['amount'] = 0.0
            item['name'] = str(reward.get('name') or 'GRAM')
            item['image_url'] = str(reward.get('image_url') or '/static/img/ton.png')
        elif reward_type in ('catalog','fragment'):
            for key in ('gift_id','gift_name','image_url','fragment_url','fragment_number','fragment_model',
                        'fragment_backdrop','fragment_symbol','price_source','animation_url',
                        'model_percent','backdrop_percent','symbol_percent'):
                item[key] = str(reward.get(key) or '')
            try:
                floor_price = max(0,int(reward.get('floor_price') or 0))
            except (TypeError,ValueError):
                floor_price = 0
            if floor_price <= 0 and reward_type == 'catalog':
                floor_price = catalog_price_cents(item.get('gift_id'))
            item['floor_price'] = floor_price
            item['price_ton'] = floor_price/100
        result[mode] = item
    return result


def daily_top_reward(mode):
    return daily_top_rewards().get(mode, {'type':'none'})


@app.get('/api/admin/daily-top-rewards')
@admin_required
def admin_daily_top_rewards_get():
    now_utc=datetime.now(timezone.utc)
    return jsonify(
        rewards=daily_top_rewards(),
        schedules={mode:daily_top_schedule_view(mode,now_utc) for mode in ('mines','upgrade','hilo')}
    )


@app.post('/api/admin/daily-top-rewards/<mode>')
@admin_required
def admin_daily_top_rewards_set(mode):
    if mode not in ('mines','upgrade','hilo'):
        return error('Неизвестный топ.',404)
    data=request.get_json(silent=True) or {}
    reward_type=str(data.get('type') or 'none')
    if reward_type not in ('none','gram','catalog','fragment'):
        return error('Выберите тип награды.')
    reward={'type':reward_type}
    try:
        if reward_type=='gram':
            amount=float(str(data.get('amount') or '0').replace(',','.'))
            if not math.isfinite(amount) or amount<=0 or amount>100000000:
                return error('GRAM: укажите сумму больше 0.')
            reward.update(amount=amount,name='GRAM',image_url='/static/img/ton.png')
        elif reward_type=='catalog':
            gift=catalog_giveaway_prize(data.get('gift_id'))
            if int(gift.get('floor_price') or 0)<=0:
                return error('У этого подарка нет цены в каталоге — награда за топ не может стоить 0 TON.')
            reward.update(gift)
        elif reward_type=='fragment':
            gift=fragment_gift_from_url(data.get('fragment_url'),True,allow_missing_price=True)
            reward.update(gift)
    except (ValueError,TypeError,InvalidOperation) as exc:
        return error(str(exc) or 'Проверьте награду.')
    doc=read_document('daily_top_rewards') or {}
    if not isinstance(doc,dict): doc={}
    doc[mode]=reward
    save_document('daily_top_rewards',doc)
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'daily_top_reward',json.dumps({'mode':mode,'reward':reward},ensure_ascii=False)))
    return jsonify(ok=True,reward=daily_top_reward(mode))


@app.post('/api/admin/daily-top-schedule/<mode>')
@admin_required
def admin_daily_top_schedule_set(mode):
    if mode not in ('mines','upgrade','hilo'):
        return error('Неизвестный топ.',404)
    data=request.get_json(silent=True) or {}

    # Close an already expired period first so changing the timer can never
    # cancel the previous winner's reward.
    with connect() as db:
        try:
            settle_previous_daily_top_rewards(db)
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('Daily top reward settlement failed before schedule update')
            return error('Не удалось закрыть предыдущий ТОП дня. Повторите попытку.',500)

    now_utc=datetime.now(timezone.utc)
    end=None

    # Preferred admin format: an exact ISO timestamp. The browser sends UTC
    # (new Date(datetimeLocal).toISOString()), so there is no timezone guess here.
    end_at=str(data.get('end_at') or '').strip()
    if end_at:
        end=parse_datetime_utc(end_at)
        if not end:
            return error('Не удалось распознать время завершения ТОП дня.')
        if end<=now_utc:
            return error('Время завершения должно быть в будущем.')
        if end-now_utc>timedelta(days=30):
            return error('Максимальный срок ТОП дня — 30 дней.')
    else:
        try:
            hours=max(0,int(data.get('hours') or 0))
            minutes=max(0,int(data.get('minutes') or 0))
        except (TypeError,ValueError):
            return error('Укажите время до завершения ТОП дня.')
        total_minutes=hours*60+minutes
        if total_minutes<1:
            return error('Минимальное время до завершения — 1 минута.')
        if total_minutes>43200:
            return error('Максимальное время до завершения — 30 дней.')
        end=now_utc+timedelta(minutes=total_minutes)

    start,current_end,_=daily_top_window(mode,now_utc)
    if current_end<=now_utc or start>now_utc:
        start=now_utc

    doc=read_document('daily_top_rewards') or {}
    if not isinstance(doc,dict): doc={}
    schedules=doc.get('_schedule')
    if not isinstance(schedules,dict): schedules={}
    schedules[mode]={
        'start_at':_daily_top_db_string(start),
        'end_at':_daily_top_db_string(end),
        'cycle_minutes':1440,
    }
    doc['_schedule']=schedules
    save_document('daily_top_rewards',doc)
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'daily_top_schedule',
                    json.dumps({'mode':mode,'end_at':_daily_top_db_string(end)},ensure_ascii=False)))
    return jsonify(ok=True,schedule=daily_top_schedule_view(mode))


@app.post('/api/admin/daily-top-reset/<mode>')
@admin_required
def admin_daily_top_reset(mode):
    if mode not in ('mines','upgrade','hilo'):
        return error('Неизвестный топ.',404)

    # Settle an already finished period before starting a fresh TOP-DROP window.
    with connect() as db:
        try:
            settle_previous_daily_top_rewards(db)
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('Daily top reward settlement failed before TOP-DROP reset')
            return error('Не удалось сбросить ТОП-ДРОП. Повторите попытку.',500)

    now_utc=datetime.now(timezone.utc)
    doc=read_document('daily_top_rewards') or {}
    if not isinstance(doc,dict):
        doc={}
    resets=doc.get('_reset')
    if not isinstance(resets,dict):
        resets={}
    resets[mode]=_daily_top_db_string(now_utc)
    doc['_reset']=resets
    save_document('daily_top_rewards',doc)
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],session['uid'],'daily_top_reset',
                    json.dumps({'mode':mode,'reset_at':_daily_top_db_string(now_utc)},ensure_ascii=False)))
    return jsonify(ok=True,mode=mode,schedule=daily_top_schedule_view(mode))


def _daily_top_period_key(start_utc, end_utc):
    start_local=start_utc.astimezone(DAILY_TOP_TZ)
    if (start_local.hour,start_local.minute,start_local.second)==(0,0,0) and end_utc-start_utc==DAILY_TOP_CYCLE:
        return start_local.date().isoformat()
    return start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')+'_'+end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')


def _daily_top_period_label(start_utc, end_utc):
    """Human-readable TOP-DROP range in the configured +03:00 timezone, without time."""
    start_date=start_utc.astimezone(DAILY_TOP_TZ).date()
    end_date=end_utc.astimezone(DAILY_TOP_TZ).date()
    start_text=start_date.strftime('%d.%m.%Y')
    end_text=end_date.strftime('%d.%m.%Y')
    return start_text if start_date==end_date else f'{start_text}–{end_text}'


def _settle_daily_top_period(db, mode, start_utc, end_utc, reward):
    if not reward or reward.get('type')=='none':
        return False
    period_key=_daily_top_period_key(start_utc,end_utc)
    if db.execute('SELECT 1 FROM daily_top_awards WHERE day=? AND mode=?',(period_key,mode)).fetchone():
        return False
    candidate_start=daily_top_candidate_start(mode,start_utc)
    start_db=_daily_top_db_string(candidate_start)
    end_db=_daily_top_db_string(end_utc)
    if mode=='mines':
        winner=db.execute("""SELECT r.user_id FROM rounds r
                             JOIN users u ON u.id=r.user_id
                             WHERE r.state='won' AND COALESCE(r.bet_type,'ton')<>'promo_gift'
                               AND COALESCE(u.withdrawal_enabled,1)=1
                               AND COALESCE(r.settled_at,r.created_at)>=? AND COALESCE(r.settled_at,r.created_at)<?
                             ORDER BY COALESCE(NULLIF(r.win_total,0),NULLIF(r.win_gift_price,0),r.payout) DESC,r.id DESC
                             LIMIT 1""",(start_db,end_db)).fetchone()
    elif mode=='hilo':
        _, hl_id = wins_feed_cutoff(db,'hilo')
        winner=db.execute("""SELECT b.user_id FROM hilo_room_bets b JOIN users u ON u.id=b.user_id
                             WHERE b.settled=1 AND b.id>? AND COALESCE(u.withdrawal_enabled,1)=1
                               AND b.created_at>=? AND b.created_at<?
                               AND b.payout+b.prize_price>CASE WHEN b.gift_name='' THEN b.amount ELSE 0 END
                             ORDER BY b.payout+b.prize_price DESC,b.id DESC LIMIT 1""",(hl_id,start_db,end_db)).fetchone()
    else:
        winner=db.execute("""SELECT s.user_id FROM upgrade_spins s
                             JOIN users u ON u.id=s.user_id
                             WHERE s.won=1 AND COALESCE(u.withdrawal_enabled,1)=1
                               AND s.created_at>=? AND s.created_at<?
                               AND REPLACE(s.result_json,' ','') NOT LIKE '%"reward_type":"wager_progress"%'
                             ORDER BY s.target_price DESC,s.created_at DESC,s.id DESC LIMIT 1""",(start_db,end_db)).fetchone()
    if not winner:
        return False
    uid=int(winner['user_id'])
    claimed=db.execute("""INSERT INTO daily_top_awards(day,mode,user_id,reward_json)
                          VALUES(?,?,?,?) ON CONFLICT(day,mode) DO NOTHING""",
                       (period_key,mode,uid,json.dumps(reward,ensure_ascii=False)))
    if not claimed.rowcount:
        return False
    reward=refresh_top_reward_price(reward)
    reward_type=reward.get('type')
    title={'mines':'Mines','upgrade':'Upgrade','hilo':'Hi-Lo'}[mode]
    period_label=_daily_top_period_label(start_utc,end_utc)
    top_source_label=f'От ТОП дня ({period_label})'
    if reward_type=='gram':
        cents=max(0,int((Decimal(str(reward.get('amount') or 0))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP)))
        if cents:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?',(cents,uid))
            record_transaction(db,uid,'daily_top_reward',cents,'daily_top',f'{period_key}:{mode}',
                               f'Награда за ТОП дня {title} ({period_label}): {reward.get("amount")} GRAM')
        detail=f'🏆 Вы заняли ТОП дня в {title} ({period_label}). Награда: {reward.get("amount")} GRAM.'
    else:
        source='daily_top_fragment' if reward_type=='fragment' else 'daily_top_catalog'
        cur=db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                         external_url,fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url,source_label)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (uid,str(reward.get('gift_id') or ''),str(reward.get('gift_name') or 'Подарок'),
                        safe_image(reward.get('image_url')),int(reward.get('floor_price') or 0),source,
                        str(reward.get('fragment_url') or ''),str(reward.get('fragment_number') or ''),
                        str(reward.get('fragment_model') or ''),str(reward.get('fragment_backdrop') or ''),
                        str(reward.get('fragment_symbol') or ''),str(reward.get('price_source') or ''),
                        safe_image(reward.get('animation_url')),top_source_label))
        record_transaction(db,uid,'daily_top_reward',0,'inventory',cur.lastrowid,
                           f'Награда за ТОП дня {title} ({period_label}): {reward.get("gift_name") or "Подарок"}')
        detail=f'🏆 Вы заняли ТОП дня в {title} ({period_label}). Подарок «{reward.get("gift_name") or "Подарок"}» добавлен в инвентарь.'
    add_user_notification(db,uid,'daily_top_reward',detail)
    return True


def settle_previous_daily_top_rewards(db):
    now_utc=datetime.now(timezone.utc)
    settings=daily_top_rewards()
    doc=read_document('daily_top_rewards') or {}
    if not isinstance(doc,dict): doc={}
    schedules=doc.get('_schedule')
    if not isinstance(schedules,dict): schedules={}
    schedule_changed=False

    for mode in ('mines','upgrade','hilo'):
        reward=settings.get(mode) or {'type':'none'}
        raw=schedules.get(mode)
        start=parse_datetime_utc(raw.get('start_at')) if isinstance(raw,dict) else None
        end=parse_datetime_utc(raw.get('end_at')) if isinstance(raw,dict) else None

        if start and end and end>start:
            # A configured TOP-DROP timer is one continuous competition. If it runs
            # from the 8th to the 11th, the leader is kept for that entire range and
            # exactly one prize is settled at the end; there are no hidden daily resets.
            if end<=now_utc:
                _settle_daily_top_period(db,mode,start,end,reward)
                schedules.pop(mode,None)
                schedule_changed=True
            continue

        # Обратная совместимость: пока администратор не задавал таймер,
        # ТОП закрывается в 00:00 по старой логике.
        now_local=now_utc.astimezone(DAILY_TOP_TZ)
        end_local=now_local.replace(hour=0,minute=0,second=0,microsecond=0)
        start_local=end_local-DAILY_TOP_CYCLE
        _settle_daily_top_period(
            db,mode,start_local.astimezone(timezone.utc),end_local.astimezone(timezone.utc),reward)

    if schedule_changed:
        doc['_schedule']=schedules
        db.execute('INSERT INTO app_documents(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                   ('daily_top_rewards',json.dumps(doc,ensure_ascii=False)))
        if has_request_context():
            g.get('documents',{}).pop('daily_top_rewards',None)


@app.get('/api/upgrade/recent-wins')
@login_required
def upgrade_recent_wins():
    with connect() as db:
        try:
            settle_previous_daily_top_rewards(db)
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('Daily top reward settlement failed while loading Upgrade wins')
        cutoff, _ = wins_feed_cutoff(db, 'upgrade')
        rows = db.execute('''SELECT s.id,s.user_id,s.source_name,s.source_image,s.source_price,
                                   s.target_name,s.target_image,s.target_price,s.chance_bp,
                                   s.result_json,s.created_at,u.name,u.username,u.photo_url,u.withdrawal_enabled
                            FROM upgrade_spins s JOIN users u ON u.id=s.user_id
                            WHERE s.won=1 AND s.created_at>?
                            ORDER BY s.created_at DESC,s.id DESC''', (cutoff,)).fetchall()
        day_start = _daily_top_db_string(daily_top_candidate_start('upgrade'))
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
                    created_at=row['created_at'],
                    _top_eligible=bool(row['withdrawal_enabled']))
    items=[]
    for row in rows:
        try:
            item=upgrade_win_item(row)
            if item:items.append(item)
        except Exception:
            app.logger.warning('Skipping malformed upgrade win row %s', row['id'] if row else '?', exc_info=True)
    if not black_backgrounds_enabled():
        items = [item for item in items if not any(gift_black_background(item[key]) for key in ('source', 'target'))]
    items=[item for item in items if item.get('reward_type')!='wager_progress']
    top_candidates=[item for item in items
                    if item.get('_top_eligible') and str(item.get('created_at') or '')>=day_start]
    top_drop=max(top_candidates, key=lambda item:(float(item['target'].get('price_ton') or 0),
                                                       str(item.get('created_at') or '')), default=None)
    for item in items:
        item.pop('_top_eligible',None)
    if top_drop:
        top_drop.pop('_top_eligible',None)
    return jsonify(items=items,top_drop=top_drop,top_reward=daily_top_reward('upgrade'),top_schedule=daily_top_schedule_view('upgrade'))


@app.post('/api/upgrade/spin')
@login_required
def upgrade_spin():
    data=request.get_json(silent=True) or {}
    if creator_demo_active(session['uid']):
        try:
            return jsonify(**demo_upgrade_spin(data), user=profile())
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
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
            # Keep TON bets shape-compatible with inventory rows.  Upgrade result
            # serialization must never assume promo-only columns exist on a synthetic
            # TON source.
            source=dict(
                gift_id='ton', gift_name='TON', image_url='/static/img/ton.png',
                floor_price=ton_price, promo_locked=0, promo_wager_multiplier=0,
                promo_wager_target=0, promo_wager_progress=0, promo_code='',
                expires_at=None, promo_attempts_total=1, promo_attempts_remaining=1,
                promo_burn_on_loss=1, external_url=''
            )
        else:
            source=db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?'+(' FOR UPDATE' if DATABASE_URL else ''),
                              (source_id,session['uid'])).fetchone()
            if not source:return error('Подарок недоступен для апгрейда.',409)
            if int(source['deposit_mirror'] or 0): return error('NFT-пополнение уже зачислено на баланс и недоступно для апгрейда.',409)
            source_price=int(source['floor_price'] or 0)
            if source_price>MAX_UPGRADE_BET_CENTS:return error('Максимальная стоимость ставки — 1 000 TON.')
            if source['promo_locked'] and int(source['promo_wager_progress'] or 0)>=int(source['promo_wager_target'] or 0):
                return error('Отыгрыш завершён — сначала разблокируйте подарок в профиле.')
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
        proof = fairness_resolve_action(db, 'upgrade', session['uid'], data)
        fair_upper = target['price'] * 10000
        fair_ticket, fair_cursor, fair_digest = fairness_draw(proof, fair_upper, 0)
        fair_threshold = effective_rtp_bp * source_price
        won = fair_ticket < fair_threshold
        awarded=None
        wager=bool(source['promo_locked'])
        xp_allowed=(not wager) and (True if amount_text else gift_counts_for_xp(source))
        # Promo progress is meaningful only for a promo-wager gift. For ordinary TON
        # and ordinary gifts it is always zero and must not be read as a required key.
        previous_wager_progress=int(source['promo_wager_progress'] or 0) if wager else 0
        wager_target=int(source['promo_wager_target'] or 0) if wager else 0
        wager_progress=min(wager_target,previous_wager_progress+target['price']) if wager and won else previous_wager_progress
        wager_attempts_total=max(1,int(source['promo_attempts_total'] or 1)) if wager else 1
        wager_attempts_before=max(1,int(source['promo_attempts_remaining'] or 1)) if wager else 1
        wager_burn_on_loss=bool(source['promo_burn_on_loss']) if wager else True
        wager_attempts_after=wager_attempts_before
        wager_burned=False
        compensation=dict(cashback=0,cashback_percent=0,promo=None)
        if won:
            cur=None
            if wager:
                completed_wager = bool(wager_target and wager_progress >= wager_target)
                # Promo Upgrade never replaces the wager gift with the selected target.
                # The target only determines chance/progress. The same promo gift remains
                # in inventory until manual unlock in the profile.
                unlock_payload = {}
                cur=db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                 promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at,
                                 promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,external_url,promo_unlock_payload)
                                 VALUES(?,?,?,?,?,'upgrade_wager',1,?,?,?,?,?,?,?,?,?,?)''',
                               (session['uid'],source['gift_id'],source['gift_name'],source['image_url'],source_price,
                                float(source['promo_wager_multiplier'] or 0),wager_target,wager_progress,source['promo_code'] or '',
                                None if completed_wager else source['expires_at'],wager_attempts_total,wager_attempts_before,
                                int(wager_burn_on_loss),source['external_url'] or '',
                                json.dumps(unlock_payload,ensure_ascii=False)))
            else:
                cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'upgrade')",
                               (session['uid'],target['id'],target['name'],target['image_url'],target['price']))
            if cur is not None:
                awarded=cur.lastrowid
        elif wager:
            wager_attempts_after = wager_attempts_before - 1 if wager_burn_on_loss else wager_attempts_before
            if wager_attempts_after > 0:
                cur=db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                                 promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at,
                                 promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,external_url,promo_unlock_payload)
                                 VALUES(?,?,?,?,?,'upgrade_wager',1,?,?,?,?,?,?,?,?,?,?)''',
                               (session['uid'],source['gift_id'],source['gift_name'],source['image_url'],source_price,
                                float(source['promo_wager_multiplier'] or 0),wager_target,int(source['promo_wager_progress'] or 0),
                                source['promo_code'] or '',source['expires_at'],wager_attempts_total,wager_attempts_after,
                                int(wager_burn_on_loss),source['external_url'] or '','{}'))
                awarded=cur.lastrowid
                record_transaction(db,session['uid'],'promo_wager_attempt_lost',0,'upgrade',request_id,
                                   f'{source["gift_name"]}: осталось жизней {wager_attempts_after}')
            else:
                wager_burned=True
                record_transaction(db,session['uid'],'promo_wager_burn',0,'upgrade',request_id,
                                   f'Сгорел промо-подарок: {source["gift_name"]}')
        else:
            compensation=apply_upgrade_loss_compensation(db,session['uid'],source_price,target['price'])
        result=dict(ok=True,id=request_id,won=won,chance=chance/100,
                    source_type='ton' if amount_text else 'gift',reward_type='wager_progress' if wager else 'gift',
                    source=dict(name=source['gift_name'],image_url=source['image_url'],price_ton=source_price/100),
                    target=dict(name=target['name'],image_url=target['image_url'],price_ton=target['price']/100,
                                promo_locked=False),
                    wager_progress=(wager_progress if wager else 0)/100,wager_target=wager_target/100,
                    wager_attempts_total=wager_attempts_total,wager_attempts_remaining=wager_attempts_after,
                    wager_burn_on_loss=wager_burn_on_loss,wager_burned=wager_burned,
                    wager_complete=bool(wager and wager_target and wager_progress>=wager_target),
                    expires_at=(None if wager and wager_target and wager_progress>=wager_target else source['expires_at']) if wager else None,
                    awarded_inventory_id=awarded,compensation=compensation)
        proof = fairness_complete_action(
            db, proof, request_id, fair_cursor,
            {'ticket': fair_ticket, 'upper': fair_upper, 'threshold': fair_threshold,
             'digest': fair_digest, 'won': bool(won), 'source_price': source_price,
             'target_price': target['price'], 'rtp_bp': effective_rtp_bp})
        result['fairness'] = fairness_public(proof, True)
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
        # Promo-wager gifts are promotional value, not real site turnover.
        # They must never advance turnover or GemDrop levels.
        result['new_level']=increase_turnover(db,session['uid'],source_price,withdrawal_wager=bool(amount_text)) if xp_allowed else None
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
    if creator_demo_active(session['uid']):
        try:
            return jsonify(round=demo_mines_start(data), user=profile(), new_level=None)
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
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
        xp_allowed = True
        snapshot = dict(item_id=None, gift_id='', name='', image='', price=0,
                        multiplier=0.0, target=0, progress=0, code='', expires_at=None,
                        attempts_total=1, attempts_remaining=1, burn_on_loss=True, external_url='')
        if inventory_id is not None:
            lock = ' FOR UPDATE' if DATABASE_URL else ''
            item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?' + lock,
                              (inventory_id, session['uid'])).fetchone()
            if not item:
                return error('Подарок не найден в инвентаре.', 404)
            if int(item['deposit_mirror'] or 0):
                return error('NFT-пополнение уже зачислено на баланс и недоступно для ставки.', 409)
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
            xp_allowed = gift_counts_for_xp(item)
            snapshot = dict(item_id=item['id'], gift_id=item['gift_id'], name=item['gift_name'],
                            image=item['image_url'], price=bet,
                            multiplier=float(item['promo_wager_multiplier'] or 0),
                            target=target, progress=progress, code=item['promo_code'] or '',
                            expires_at=item['expires_at'],
                            attempts_total=max(1, int(item['promo_attempts_total'] or 1)),
                            attempts_remaining=max(0, int(item['promo_attempts_remaining'] or 1)),
                            burn_on_loss=bool(item['promo_burn_on_loss']), external_url=item['external_url'] or '')
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

        proof = fairness_make('mines', session['uid'], data.get('client_seed'))
        positions, fair_cursor = fairness_positions(proof, mines)
        if bet_type == 'promo_gift':
            rtp_snapshot, promo_loss_boost, promo_game_loss = promo_loss_adjusted_rtp(db, session['uid'], snapshot['code'])
        else:
            rtp_snapshot, promo_loss_boost, promo_game_loss = game_rtp(), 0.0, 0
        db.execute("""INSERT INTO rounds(user_id,bet,mines,positions,bet_type,bet_inventory_id,
                       bet_gift_id,bet_gift_name,bet_gift_image,bet_gift_price,promo_wager_multiplier,
                       promo_wager_target,promo_wager_progress,promo_code,rtp_snapshot,bet_expires_at,
                       promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,bet_external_url)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (session['uid'], bet, mines, json.dumps(positions), bet_type, snapshot['item_id'],
                    snapshot['gift_id'], snapshot['name'], snapshot['image'], snapshot['price'], snapshot['multiplier'],
                    snapshot['target'], snapshot['progress'], snapshot['code'], rtp_snapshot, snapshot['expires_at'],
                    snapshot['attempts_total'], snapshot['attempts_remaining'], int(snapshot['burn_on_loss']), snapshot['external_url']))
        row = active_round(db, session['uid'])
        fairness_store(db, proof, str(row['id']), fair_cursor,
                       {'positions': positions, 'mines': mines, 'board_size': 25})
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
        # Promo-wager gifts do not count toward site turnover or levels.
        new_level=None if (bet_type=='promo_gift' or not xp_allowed) else increase_turnover(db,session['uid'],bet,withdrawal_wager=(bet_type=='ton'))
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
    if creator_demo_active(session['uid']):
        try:
            return jsonify(round=demo_mines_open(cell), user=profile())
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
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
            fairness_mark_settled(db, 'mines', row['id'])
            if row['bet_type'] == 'promo_gift':
                attempts_total = max(1, int(row['promo_attempts_total'] or 1))
                attempts_before = max(1, int(row['promo_attempts_remaining'] or 1))
                burns = bool(row['promo_burn_on_loss'])
                attempts_after = attempts_before - 1 if burns else attempts_before
                if attempts_after > 0:
                    cur = db.execute("""INSERT INTO inventory(
                                      user_id,gift_id,gift_name,image_url,floor_price,source,round_id,
                                      promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at,
                                      promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,external_url)
                                      VALUES(?,?,?,?,?,'promo_wager',?,1,?,?,?,?,?,?,?,?,?)""",
                                     (row['user_id'], row['bet_gift_id'], row['bet_gift_name'], row['bet_gift_image'],
                                      row['bet_gift_price'], row['id'], float(row['promo_wager_multiplier'] or 0),
                                      int(row['promo_wager_target'] or 0), int(row['promo_wager_progress'] or 0),
                                      row['promo_code'] or '', row['bet_expires_at'], attempts_total, attempts_after,
                                      int(burns), row['bet_external_url'] or ''))
                    db.execute('UPDATE rounds SET prize_inventory_id=? WHERE id=?', (cur.lastrowid, row['id']))
                    record_transaction(db, row['user_id'], 'promo_wager_attempt_lost', 0, 'round', row['id'],
                                       f'{row["bet_gift_name"]}: осталось жизней {attempts_after}')
                else:
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
                fairness_mark_settled(db, 'mines', row['id'])
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
    if creator_demo_active(session['uid']):
        try:
            return jsonify(round=demo_mines_cashout(), user=profile())
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
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
        fairness_mark_settled(db, 'mines', row['id'])
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
        try:
            settle_previous_daily_top_rewards(db)
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('Daily top reward settlement failed while loading Mines wins')
        cutoff, max_round_id = wins_feed_cutoff(db, 'mines')
        selection = """SELECT r.id,r.bet,r.mines,r.opened,r.payout,r.win_total,r.win_multiplier,
                                    r.win_gift_name,r.win_gift_image,r.win_gift_price,r.created_at,
                                    u.id AS user_id,u.name,u.username,u.photo_url,u.withdrawal_enabled
                             FROM rounds r JOIN users u ON u.id=r.user_id
                             WHERE r.state='won' AND COALESCE(r.bet_type,'ton')<>'promo_gift'
                               AND (r.id>? OR r.settled_at>?)"""
        rows = db.execute(selection+' ORDER BY COALESCE(r.settled_at,r.created_at) DESC,r.id DESC',
                          (max_round_id,cutoff)).fetchall()
        top = db.execute(selection+''' AND COALESCE(u.withdrawal_enabled,1)=1
                           AND COALESCE(r.settled_at,r.created_at)>=?
                           ORDER BY COALESCE(NULLIF(r.win_total,0),NULLIF(r.win_gift_price,0),r.payout) DESC,r.id DESC LIMIT 1''',
                         (max_round_id,cutoff,_daily_top_db_string(daily_top_candidate_start('mines')))).fetchone()
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
            created_at=row['created_at'], _top_eligible=bool(row['withdrawal_enabled']))
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
            candidate_start=_daily_top_db_string(daily_top_candidate_start('mines'))
            top_drop = max((item for item in items
                            if item.get('_top_eligible') and str(item['created_at']) >= candidate_start),
                           key=lambda item: item['amount'], default=None)
    for item in items:
        item.pop('_top_eligible',None)
    if top_drop:
        top_drop.pop('_top_eligible',None)
    return jsonify(items=items,top_drop=top_drop,top_reward=daily_top_reward('mines'),top_schedule=daily_top_schedule_view('mines'))


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
    traits = {'model': '', 'backdrop': '', 'symbol': '',
              'model_percent': '', 'backdrop_percent': '', 'symbol_percent': ''}
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
            percent = attr.get('percentage', attr.get('percent', attr.get('rarity', attr.get('probability', ''))))
            try:
                if isinstance(percent, str): percent = percent.strip().rstrip('%')
                percent = float(percent)
                if 0 < percent <= 1: percent *= 100
                percent = f'{percent:.4f}'.rstrip('0').rstrip('.')
            except (TypeError,ValueError):
                percent = ''
            if not value:
                continue
            if 'model' in key:
                traits['model'] = value[:100]; traits['model_percent'] = percent
            elif 'backdrop' in key or 'background' in key:
                traits['backdrop'] = value[:100]; traits['backdrop_percent'] = percent
            elif 'symbol' in key or 'pattern' in key:
                traits['symbol'] = value[:100]; traits['symbol_percent'] = percent
    return traits


def _fragment_trait_percentages_from_text(text):
    out={'model_percent':'','backdrop_percent':'','symbol_percent':''}
    raw=unescape(str(text or '')).replace('•','\n')
    for key,label in (('model_percent','Model'),('backdrop_percent','Backdrop'),('symbol_percent','Symbol')):
        match=re.search(rf'\b{label}\s*:\s*[^\n\r|]*?([0-9]+(?:[.,][0-9]+)?)\s*%',raw,re.I)
        if match: out[key]=match.group(1).replace(',','.')
    return out


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
        base = re.sub(r'\s*\((?:Onyx Black|Onyx|Black)\)\s*$', '', base, flags=re.I).strip()
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
        raise ValueError('Ссылка должна быть вида https://t.me/nft/PartySparkler-66376 или https://fragment.com/gift/PartySparkler-66376')
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
    model_percent = backdrop_percent = symbol_percent = ''
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
                model_percent, backdrop_percent, symbol_percent = traits.get('model_percent',''), traits.get('backdrop_percent',''), traits.get('symbol_percent','')
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
                rarity = _fragment_trait_percentages_from_text(description)
                model_percent = model_percent or rarity['model_percent']
                backdrop_percent = backdrop_percent or rarity['backdrop_percent']
                symbol_percent = symbol_percent or rarity['symbol_percent']
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
                model_percent=model_percent, backdrop_percent=backdrop_percent, symbol_percent=symbol_percent,
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
            rank=rank, gift_id=prize['gift_id'], name=prize['gift_name'], fragment_url=prize['fragment_url'] or '',
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
    if game not in ('all','mines','upgrade','arena'): return error('Выберите игру.')
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
    if game in ('all','arena'):
        queries.append(f"""SELECT CAST(b.round_id AS TEXT) AS id,b.user_id,'arena' AS game,
                         CASE WHEN r.state<>'settled' THEN 'active' WHEN r.winner_user_id=b.user_id THEN 'won' ELSE 'lost' END AS outcome,
                         b.amount AS stake,
                         CASE WHEN r.state='settled' AND r.winner_user_id=b.user_id THEN ((SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id)-((SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id)*{ARENA_FEE_PERCENT}/100))+(r.total_pool-(SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id)) ELSE 0 END AS payout,
                         CASE WHEN r.total_pool>0 THEN b.amount*10000/r.total_pool ELSE 0 END AS chance,
                         '' AS source_name,b.gifts AS gift_name,b.created_at AS date,u.name,u.username
                         FROM arena_bets b JOIN arena_rounds r ON r.id=b.round_id JOIN users u ON u.id=b.user_id"""+(' WHERE b.user_id=?' if uid else ''))
        if uid: params.append(uid)
    with connect() as db:
        rows=db.execute('SELECT * FROM ('+' UNION ALL '.join(queries)+") AS history ORDER BY REPLACE(SUBSTR(date,1,19),'T',' ') DESC,game,id DESC LIMIT 51 OFFSET ?",(*params,offset)).fetchall()
    items=[]
    for r in rows[:50]:
        item=dict(r)
        for key in ('stake','payout','chance'): item[key]=int(item[key] or 0)/100
        if item['game']=='arena':
            gifts=arena_gifts_public(item['gift_name'])
            item['gift_name']=', '.join(f"{g['name']} ({g['price']:.2f} TON)" for g in gifts)
            item['gift_total']=sum(g['price'] for g in gifts)
            item['ton_part']=max(0,item['stake']-item['gift_total'])
            item['x']=(item['payout']/item['stake']) if item['outcome']=='won' and item['stake']>0 else 0
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
    'crash_bet': 'Ставка Crash',
    'crash_win': 'Выигрыш Crash',
    'arena_bet': 'Ставка в Арене',
    'arena_gift_bet': 'Ставка подарком в Арене',
    'arena_win': 'Выигрыш в Арене',
    'arena_gift_win': 'Подарки из Арены',
    'arena_loss': 'Проигрыш в Арене',
    'arena_refund': 'Возврат ставки в Арене',
    'arena_gift_refund': 'Возврат подарка в Арене',
    'crash_gift_bet': 'Ставка подарком в Crash',
    'crash_gift_lost': 'Подарок проигран в Crash',
    'crash_gift_win': 'Подарок из Crash',
    'hilo_bet': 'Ставка Hi-Lo',
    'hilo_win': 'Выигрыш Hi-Lo',
    'hilo_gift_bet': 'Ставка подарком в Hi-Lo',
    'hilo_gift_win': 'Подарок из Hi-Lo',
    'hilo_gift_lost': 'Подарок проигран в Hi-Lo',
    'hilo_gift_return': 'Подарок возвращён из Hi-Lo',
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
    'stars_deposit': 'Пополнение Stars',
    'referral_bonus': 'Реферальный бонус',
    'referral_withdraw': 'Вывод реферального баланса',
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
                                         win_gift_price,win_total,payout,created_at,bet_type
                                  FROM rounds WHERE user_id=? ORDER BY id DESC''', (user_id,)).fetchall()
        mines_count = len(mine_rows)
        max_mines_x = 0.0
        mines_drop = None
        mines_wins = 0
        for row in mine_rows:
            if row['state'] != 'won' or row['bet_type'] == 'promo_gift':
                continue
            mines_wins += 1
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
        upgrade_wins = 0
        for row in upgrade_rows:
            source_price = int(row['source_price'] or 0)
            target_price = int(row['target_price'] or 0)
            try:
                result = json.loads(row['result_json'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                result = {}
            if isinstance(result, dict) and result.get('reward_type') == 'wager_progress':
                continue
            if int(row['won'] or 0):
                upgrade_wins += 1
            if int(row['won'] or 0) and source_price > 0:
                max_upgrade_x = max(max_upgrade_x, target_price/source_price)
            if not int(row['won'] or 0) or target_price <= 0:
                continue
            if (show_black or not gift_black_background({'name': row['target_name']})) and drop_is_after_override(row['created_at']) and (not upgrade_drop or target_price > upgrade_drop['price_cents']):
                upgrade_drop = dict(price_cents=target_price, name=row['target_name'] or 'Подарок Upgrade',
                                    image_url=row['target_image'] or '', source='Upgrade')

        arena_rows = db.execute("""SELECT b.amount,r.winner_user_id,r.total_pool,
                                   (SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id) AS ton_pool FROM arena_bets b
                                   JOIN arena_rounds r ON r.id=b.round_id
                                   WHERE b.user_id=? AND r.state='settled'""", (user_id,)).fetchall()
        arena_count = len(arena_rows)
        max_arena_x = 0.0
        arena_wins = 0
        arena_drop = None
        for row in arena_rows:
            stake = int(row['amount'] or 0)
            pool = int(row['total_pool'] or 0)
            if stake > 0 and pool > 0 and int(row['winner_user_id'] or 0) == int(user_id):
                arena_wins += 1
                ton_pool = max(0, min(pool, int(row['ton_pool'] or 0)))
                arena_value = max(0, pool - arena_fee_cents(ton_pool))
                max_arena_x = max(max_arena_x, arena_value / stake)
                if arena_value > (arena_drop['price_cents'] if arena_drop else 0):
                    arena_drop = dict(price_cents=arena_value, name='Выигрыш Арена', image_url='', source='Арена')

        hilo_rows = db.execute("SELECT round_no,direction,payout,won,prize_name,prize_image,prize_price,created_at FROM hilo_room_bets WHERE user_id=? AND settled=1", (user_id,)).fetchall()
        hilo_count, hilo_wins, max_hilo_x, hilo_drop = len(hilo_rows), 0, 0.0, None
        for row in hilo_rows:
            if not (row['won'] or int(row['payout'] or 0) > 0):
                continue
            base = hilo_room_rank(row['round_no'])
            if hilo_room_is_push(base, row['direction']):
                continue
            hilo_wins += 1
            max_hilo_x = max(max_hilo_x, hilo_room_step_micro(base, row['direction']) / HILO_MICRO)
            prize = int(row['prize_price'] or 0)
            if (row['prize_name'] and prize > (hilo_drop['price_cents'] if hilo_drop else 0) and drop_is_after_override(row['created_at'])
                    and (show_black or not gift_black_background({'name': row['prize_name']}))):
                hilo_drop = dict(price_cents=prize, name=row['prize_name'], image_url=row['prize_image'] or '', source='Hi-Lo')

        crash_rows = db.execute("""SELECT bet,state,cashout_x100,payout,prize_name,prize_image,prize_price,created_at,bet_type
                                  FROM crash_bets WHERE user_id=? ORDER BY round_id DESC""", (user_id,)).fetchall()
        crash_count, crash_wins, max_crash_x, crash_drop = len(crash_rows), 0, 0.0, None
        for row in crash_rows:
            if row['state'] != 'won' or row['bet_type'] == 'promo_gift':
                continue
            crash_wins += 1
            max_crash_x = max(max_crash_x, float(row['cashout_x100'] or 0) / 100)
            value = int(row['prize_price'] or 0) or int(row['payout'] or 0)
            if value > 0 and drop_is_after_override(row['created_at']) and (not crash_drop or value > crash_drop['price_cents']):
                crash_drop = dict(price_cents=value, name=row['prize_name'] or 'Выигрыш Crash',
                                  image_url=row['prize_image'] or '', source='Crash')

        craft_rows = db.execute("""SELECT input_total,reward_price,reward_name,reward_image,reward_multiplier,created_at
                                  FROM craft_spins WHERE user_id=? ORDER BY id DESC""", (user_id,)).fetchall()
        craft_count, craft_wins, max_craft_x, craft_drop = len(craft_rows), 0, 0.0, None
        for row in craft_rows:
            total=max(0,int(row['input_total'] or 0)); reward=max(0,int(row['reward_price'] or 0))
            if reward < total or reward <= 0:
                continue
            craft_wins += 1
            try: mult=float(row['reward_multiplier'] or (reward/total if total else 0))
            except (TypeError, ValueError, ZeroDivisionError): mult=0
            max_craft_x=max(max_craft_x,mult)
            if drop_is_after_override(row['created_at']) and (not craft_drop or reward > craft_drop['price_cents']):
                craft_drop=dict(price_cents=reward,name=row['reward_name'] or 'Выигрыш Craft',
                                image_url=row['reward_image'] or '',source='Craft')

        override_drop = None
        override_price = int(user_row['max_drop_override_price'] or 0)
        override_name = str(user_row['max_drop_override_name'] or '').strip()
        if override_price > 0 and override_name and (show_black or not gift_black_background({'name': override_name})):
            override_drop = dict(price_cents=override_price, name=override_name,
                                 image_url=user_row['max_drop_override_image'] or '', source='Профиль')
        max_drop = max((x for x in (override_drop, mines_drop, upgrade_drop, arena_drop, hilo_drop, crash_drop, craft_drop) if x),
                       key=lambda x: x['price_cents'], default=None)

    return jsonify(user=dict(id=int(user_row['id']), name=user_row['name'], username=user_row['username'],
                             photo_url=user_row['photo_url'], created_at=user_row['created_at']),
                   level=dict(level=current_level, max_level=len(level_rows), turnover=turnover/100,
                              next_turnover=(int(next_row['required_turnover'])/100 if next_row else None),
                              progress=round(level_progress, 1)),
                   stats=dict(mines_count=mines_count, upgrade_count=upgrade_count,
                              max_mines_x=round(max_mines_x, 4), max_upgrade_x=round(max_upgrade_x, 4),
                              arena_count=arena_count, max_arena_x=round(max_arena_x, 4),
                              mines_wins=mines_wins, upgrade_wins=upgrade_wins, arena_wins=arena_wins,
                              hilo_count=hilo_count, hilo_wins=hilo_wins, max_hilo_x=round(max_hilo_x, 4),
                              crash_count=crash_count, crash_wins=crash_wins, max_crash_x=round(max_crash_x, 4),
                              craft_count=craft_count, craft_wins=craft_wins, max_craft_x=round(max_craft_x, 4)),
                   drops=dict(mines=(mines_drop['price_cents']/100 if mines_drop else None),
                              upgrade=(upgrade_drop['price_cents']/100 if upgrade_drop else None),
                              arena=(arena_drop['price_cents']/100 if arena_drop else None),
                              hilo=(hilo_drop['price_cents']/100 if hilo_drop else None),
                              crash=(crash_drop['price_cents']/100 if crash_drop else None),
                              craft=(craft_drop['price_cents']/100 if craft_drop else None)),
                   max_multiplier=(lambda values: (dict(source=values[0][0], value=round(values[0][1],4)) if values and values[0][1]>0 else None))(
                       sorted([('Mines',max_mines_x),('Upgrade',max_upgrade_x),('Арена',max_arena_x),('Hi-Lo',max_hilo_x),('Crash',max_crash_x),('Craft',max_craft_x)], key=lambda x:x[1], reverse=True)),
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
        hilo_time, hilo_id = wins_feed_cutoff(db, 'hilo')
        hilo = db.execute("SELECT COUNT(*) AS total FROM hilo_room_bets WHERE settled=1 AND id>? AND payout+prize_price>CASE WHEN gift_name='' THEN amount ELSE 0 END", (hilo_id,)).fetchone()['total']
        mines = db.execute("SELECT COUNT(*) AS total FROM rounds WHERE state='won' AND COALESCE(bet_type,'ton')<>'promo_gift' AND (id>? OR settled_at>?)", (mines_id,mines_time)).fetchone()['total']
        upgrade = db.execute('SELECT COUNT(*) AS total FROM upgrade_spins WHERE won=1 AND created_at>?', (upgrade_time,)).fetchone()['total']
        craft = db.execute('SELECT COUNT(*) AS total FROM craft_spins WHERE created_at>?', (craft_time,)).fetchone()['total']
    return jsonify(mines=mines,upgrade=upgrade,craft=craft,hilo=hilo,
                   cleared_at=dict(mines=mines_time or None,upgrade=upgrade_time or None,craft=craft_time or None,hilo=hilo_time or None))


@app.post('/api/admin/wins-feeds/clear')
@admin_required
def admin_clear_wins_feeds():
    mode = str((request.get_json(silent=True) or {}).get('mode') or '')
    if mode not in ('mines','upgrade','craft','hilo','both','all'):
        return error('Выберите Мины, Апгрейд, Крафт, Hi-Lo или все разделы.')
    if mode == 'both':
        kinds = ['mines','upgrade']
    elif mode == 'all':
        kinds = ['mines','upgrade','craft','hilo']
    else:
        kinds = [mode]
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        for kind in kinds:
            highest_round = (db.execute('SELECT COALESCE(MAX(id),0) AS last_id FROM rounds').fetchone()['last_id'] if kind=='mines' else db.execute('SELECT COALESCE(MAX(id),0) AS last_id FROM hilo_room_bets').fetchone()['last_id'] if kind=='hilo' else 0)
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
    creator = creator_record(session['uid'])
    if creator.get('demo_enabled'):
        return jsonify(items=visible_gifts([demo_clean_item(x) for x in creator.get('demo_inventory') or []]), demo=True)
    with connect() as db:
        purge_expired_inventory(db, session['uid'])
        items = db.execute('SELECT * FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 200',
                           (session['uid'],)).fetchall()
    return jsonify(items=visible_gifts([inventory_item(item) for item in items]), demo=False)


@app.post('/api/inventory/<int:item_id>/sell')
@login_required
def sell_inventory(item_id):
    if creator_demo_active(session['uid']):
        record = creator_record(session['uid'])
        item = demo_find_item(record, item_id)
        if not item:
            return error('Подарок не найден.', 404)
        if item.get('promo_locked'):
            return error('Промо-подарок нельзя продать до завершения отыгрыша.', 409)
        amount = parse_amount(item.get('price_ton') or 0)
        if not demo_remove_item(record, item_id):
            return error('Подарок уже обработан.', 409)
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + amount
        save_creator_record(session['uid'], {'demo_balance_cents': record['demo_balance_cents'],
                                             'demo_inventory': record['demo_inventory']})
        return jsonify(ok=True, sold_for=amount/100, demo=True, user=profile())
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
        if int(item['deposit_mirror'] or 0):
            return error('Стоимость этого NFT уже зачислена на баланс при пополнении. Повторная продажа отключена.', 409)
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


def confirmed_deposit_total(db, user_id):
    row = db.execute('SELECT COALESCE(SUM(amount),0) AS total FROM deposits WHERE user_id=?', (user_id,)).fetchone()
    return int((row or {}).get('total') or 0)


def freebet_options(promo):
    try:
        payload = json.loads(promo['reward_json'] or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    options = payload.get('freebet_options', {}) if isinstance(payload, dict) else {}
    return options if isinstance(options, dict) else {}


def freebet_fragment_url(db, promo, freebet_code):
    """Legacy exact-per-activation links kept for old already-created freebets."""
    options = freebet_options(promo)
    urls = [str(x).strip() for x in options.get('fragment_urls', []) if str(x).strip()]
    if not urls:
        return ''
    row = db.execute('SELECT uses_count FROM freebets WHERE code=?', (freebet_code,)).fetchone()
    index = int(row['uses_count'] or 0) if row else 0
    if index >= len(urls):
        return ''
    return urls[index]


def freebet_burn_pool_enabled(db, freebet_code):
    if not freebet_code:
        return False
    promo = db.execute('SELECT reward_json FROM promo_codes WHERE code=?', (freebet_code,)).fetchone()
    return bool(promo and freebet_options(promo).get('burn_pool_enabled'))


def freebet_burn_pool_state(db, freebet_code):
    row = db.execute("""SELECT COUNT(*) AS total,
                        COALESCE(SUM(CASE WHEN claimed_by IS NOT NULL THEN 1 ELSE 0 END),0) AS claimed
                        FROM freebet_burn_prizes WHERE freebet_code=?""", (freebet_code,)).fetchone()
    total=int((row or {}).get('total') or 0); claimed=int((row or {}).get('claimed') or 0)
    return dict(total=total,claimed=claimed,remaining=max(0,total-claimed))


def burn_remaining_freebet_wagers(db, freebet_code, winner_id=0):
    if not freebet_code:
        return 0
    inv = db.execute("""SELECT id,user_id,gift_name FROM inventory
                        WHERE promo_locked=1 AND promo_code=? AND user_id<>?""",
                     (freebet_code,int(winner_id or 0))).fetchall()
    rounds = db.execute("""SELECT id,user_id,bet_gift_name FROM rounds
                           WHERE state='active' AND bet_type='promo_gift' AND promo_code=? AND user_id<>?""",
                        (freebet_code,int(winner_id or 0))).fetchall()
    users={int(r['user_id']) for r in inv}
    users.update(int(r['user_id']) for r in rounds)
    db.execute("""DELETE FROM inventory WHERE promo_locked=1 AND promo_code=? AND user_id<>?""",
               (freebet_code,int(winner_id or 0)))
    settled=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
    db.execute("""UPDATE rounds SET state='lost',settled_at=?
                  WHERE state='active' AND bet_type='promo_gift' AND promo_code=? AND user_id<>?""",
               (settled,freebet_code,int(winner_id or 0)))
    for uid in users:
        text='🔥 Сгораемый подарок уже отыграл другой игрок. Его результат разблокирован первым, поэтому ваш подарок сгорел.'
        add_user_notification(db,uid,'promo_wager_burn',text)
        record_transaction(db,uid,'promo_wager_burn',0,'freebet',freebet_code,
                           'Другой игрок первым разблокировал сгораемый подарок')
    return len(users)


def claim_freebet_burn_prize(db, freebet_code, user_id):
    """Atomically claim the single Fragment gift configured for a burn-race Freebet."""
    if not freebet_burn_pool_enabled(db, freebet_code):
        return None
    lock=' FOR UPDATE' if DATABASE_URL else ''
    marker=db.execute("""SELECT * FROM freebet_burn_prizes
                         WHERE freebet_code=? AND slot_index=1""" + lock,
                      (freebet_code,)).fetchone()
    if not marker:
        return dict(enabled=True,claimed=False,gift=None,remaining=0)
    if marker['claimed_by'] is not None:
        return dict(enabled=True,claimed=int(marker['claimed_by'])==int(user_id),
                    gift=dict(marker),remaining=0)
    now=datetime.now(timezone.utc).isoformat()
    changed=db.execute("""UPDATE freebet_burn_prizes SET claimed_by=?,claimed_at=?
                          WHERE id=? AND claimed_by IS NULL""",(user_id,now,marker['id']))
    if not changed.rowcount:
        return claim_freebet_burn_prize(db,freebet_code,user_id)
    burn_remaining_freebet_wagers(db,freebet_code,user_id)
    claimed=db.execute('SELECT * FROM freebet_burn_prizes WHERE id=?',(marker['id'],)).fetchone()
    return dict(enabled=True,claimed=True,gift=dict(claimed),remaining=0)


def apply_freebet_reward(db, promo, user_id, freebet_code):
    """Apply a backing promo reward without requiring a Mini App session."""
    reward_type = promo['reward_type']
    inventory_id = None
    components = {}
    options = freebet_options(promo)
    burn_pool_enabled = bool(options.get('burn_pool_enabled'))
    burn_on_loss = bool(options.get('burn_on_loss', True))
    try:
        wager_attempts = max(1, min(100, int(options.get('wager_attempts') or 1)))
    except (TypeError, ValueError):
        wager_attempts = 1
    fragment_url = '' if burn_pool_enabled else (freebet_fragment_url(db, promo, freebet_code) if reward_type in ('gift', 'wager_gift') else '')
    if reward_type == 'balance':
        amount = max(0, int(promo['amount'] or 0))
        if amount <= 0:
            raise ValueError('Награда фрибета настроена неверно.')
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
        reward = dict(type='balance', amount=amount/100)
        record_transaction(db, user_id, 'freebet_balance', amount, 'freebet', freebet_code, f'Freebet {freebet_code}')
    elif reward_type == 'gift':
        cur = db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,external_url) VALUES(?,?,?,?,?,'freebet',?)",
                         (user_id, promo['gift_id'], promo['gift_name'], promo['gift_image_url'], promo['gift_price'], fragment_url))
        inventory_id = cur.lastrowid
        reward = dict(type='gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'], name=promo['gift_name'],
                                             image_url=promo['gift_image_url'], price_ton=promo['gift_price']/100, fragment_url=fragment_url))
        record_transaction(db, user_id, 'freebet_gift', 0, 'freebet', freebet_code, promo['gift_name'])
    elif reward_type == 'wager_gift':
        multiplier = max(1.0, float(promo['wager_multiplier'] or 1))
        target = max(1, round(int(promo['gift_price']) * multiplier))
        item_expires_at = promo_gift_expiry(promo['gift_expires_days'])
        cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                          promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at,
                          promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,external_url)
                          VALUES(?,?,?,?,?,'promo_wager',1,?,?,0,?,?,?,?,?,?)""",
                         (user_id, promo['gift_id'], promo['gift_name'], promo['gift_image_url'], promo['gift_price'],
                          multiplier, target, freebet_code, item_expires_at, wager_attempts, wager_attempts, int(burn_on_loss), fragment_url))
        inventory_id = cur.lastrowid
        reward = dict(type='wager_gift', gift=dict(id=inventory_id, gift_id=promo['gift_id'], name=promo['gift_name'],
                                                   image_url=promo['gift_image_url'], price_ton=promo['gift_price']/100,
                                                   promo_locked=True, wager_multiplier=multiplier,
                                                   wager_target=target/100, wager_progress=0, expires_at=item_expires_at,
                                                   wager_attempts_total=wager_attempts, wager_attempts_remaining=wager_attempts,
                                                   wager_burn_on_loss=burn_on_loss, fragment_url=fragment_url,
                                                   burn_pool_enabled=burn_pool_enabled,
                                                   burn_pool=freebet_burn_pool_state(db,freebet_code) if burn_pool_enabled else None))
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
                          gift_id=str(gift.get('gift_id') or gift.get('id') or ''),
                          image_url=gift.get('image_url') or '', amount=float(gift.get('price_ton') or 0)))
    elif kind == 'wager_gift':
        gift = reward.get('gift') or {}
        mult = float(gift.get('wager_multiplier') or 0)
        target = float(gift.get('wager_target') or 0)
        detail = f"Отыгрышный подарок · X{mult:g}"
        if target:
            detail += f' · нужно отыграть {target:.2f} TON'
        attempts = int(gift.get('wager_attempts_remaining') or 0)
        if gift.get('wager_burn_on_loss') is False:
            detail += ' · не сгорает при проигрыше'
        elif attempts:
            detail += f' · жизней: {attempts}'
        items.append(dict(kind='wager_gift', title=str(gift.get('name') or 'Подарок'), detail=detail,
                          gift_id=str(gift.get('gift_id') or gift.get('id') or ''),
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
        icon = icons.get(it['kind'], '•')
        if it.get('kind') in ('gift', 'wager_gift'):
            icon = gift_custom_emoji_html(it.get('gift_id'), it.get('title'), icon)
        lines.append(f"{icon} <b>{escape(it['title'])}</b> — {escape(it['detail'])}")
    return '\n'.join(lines)


def try_activate_freebet(user_id, code):
    code = str(code or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return {'status': 'invalid', 'text': 'Фрибет не найден.'}
    with connect() as db:
        fb = db.execute('SELECT * FROM freebets WHERE code=?', (code,)).fetchone()
        if not fb or not fb['active']:
            return {'status': 'invalid', 'text': 'Фрибет не найден или отключён.'}
        if int(fb['author_user_id'] or 0) == int(user_id):
            return {'status': 'condition', 'text': 'Автор не может активировать собственный фрибет.'}
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
        min_deposit = int(fb['min_deposit'] or 0)
        if min_deposit:
            deposited = confirmed_deposit_total(db, user_id)
            if deposited < min_deposit:
                return {'status': 'condition', 'text': f'Для этого фрибета нужен подтверждённый депозит от {min_deposit/100:.2f} TON. У вас: {deposited/100:.2f} TON.'}
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
        if int(fb['author_user_id'] or 0) == int(user_id):
            return {'status': 'condition', 'text': 'Автор не может активировать собственный фрибет.'}
        if db.execute('SELECT 1 FROM freebet_redemptions WHERE code=? AND user_id=?', (code, user_id)).fetchone():
            return {'status': 'used', 'text': 'Вы уже получили этот фрибет. Награда уже находится в GemDrop.', 'reply_markup': freebet_play_keyboard()}
        if int(fb['max_uses'] or 0) > 0 and int(fb['uses_count'] or 0) >= int(fb['max_uses'] or 0):
            return {'status': 'exhausted', 'text': 'Фрибет закончился — все доступные активации уже получили пользователи.'}
        min_deposit = int(fb['min_deposit'] or 0)
        if min_deposit and confirmed_deposit_total(db, user_id) < min_deposit:
            return {'status': 'condition', 'text': f'Для этого фрибета нужен подтверждённый депозит от {min_deposit/100:.2f} TON.'}
        promo = db.execute('SELECT * FROM promo_codes WHERE code=?', (fb['promo_code'],)).fetchone()
        if not promo:            raise ValueError('Награда фрибета не найдена.')
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


MINIAPP_DESTINATIONS = {
    'home': 'Главная', 'games': 'Игры', 'mines': 'Мины', 'upgrade': 'Апгрейды',
    'crash': 'Crash', 'arena': 'Арена', 'hilo': 'Hi-Lo', 'limbo': 'Limbo', 'giveaways': 'Розыгрыши',
    'profile': 'Профиль', 'levels': 'Уровни', 'bonuses': 'Бонусы',
    'creator': 'Панель автора', 'deposit': 'Пополнение',
}


def miniapp_target_url(value='', fallback=''):
    raw = str(value or '').strip()
    action = ''
    if raw.startswith('section:'):
        action = raw.split(':', 1)[1].strip().lower()
    elif raw in MINIAPP_DESTINATIONS:
        action = raw
    if action:
        if action not in MINIAPP_DESTINATIONS:
            raise ValueError('Неизвестный раздел GemDrop.')
        if not WEBAPP_URL.startswith('https://'):
            raise ValueError('WEBAPP_URL должен быть HTTPS для кнопки Mini App.')
        return WEBAPP_URL.rstrip('/') + '/?open=' + action
    if raw:
        if not re.match(r'^https://', raw, re.I):
            raise ValueError('Web App URL должен начинаться с https://.')
        return raw[:2048]
    target = str(fallback or '').strip() or WEBAPP_URL.rstrip('/') + '/'
    if not target.startswith('https://'):
        raise ValueError('WEBAPP_URL должен быть HTTPS для кнопки Mini App.')
    return target[:2048]


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
                if value.startswith('section:'):
                    action = value.split(':', 1)[1].strip().lower()
                    if action not in MINIAPP_DESTINATIONS:
                        raise ValueError(f'Неизвестный раздел GemDrop у кнопки «{text}».')
                    value = 'section:' + action
                elif value and not re.match(r'^(https://|\{webapp_url\})', value, re.I):
                    raise ValueError(f'Web App кнопки «{text}» должен открывать раздел GemDrop или HTTPS URL.')
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
                try:
                    target = miniapp_target_url(value.replace('{webapp_url}', play_url), play_url)
                except ValueError:
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



def gift_emoji_record(gift_id):
    gift_id = str(gift_id or '').strip()
    if not gift_id:
        return {}
    try:
        item = read_document('gift_emoji:' + gift_id) or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        item = {}
    return item if isinstance(item, dict) else {}


def gift_custom_emoji_html(gift_id, gift_name='', default='🎁'):
    item = gift_emoji_record(gift_id)
    eid = str(item.get('emoji_id') or '').strip()
    fallback = str(item.get('emoji') or '').strip()
    if re.fullmatch(r'[0-9]{5,30}', eid) and fallback:
        return f'<tg-emoji emoji-id="{eid}">{escape(fallback)}</tg-emoji>'
    return default


@app.get('/api/admin/gift-emojis')
@admin_required
def admin_gift_emojis():
    term = str(request.args.get('q') or '').strip().casefold()[:80]
    gifts = []
    for gift in read_catalog().get('gifts', []):
        gid = str(gift.get('id') or '')
        name = str(gift.get('name') or 'Подарок')
        if term and term not in name.casefold() and term not in gid.casefold():
            continue
        mapping = gift_emoji_record(gid)
        gifts.append(dict(
            gift_id=gid, name=name,
            image_url=safe_image(gift.get('image_url') or gift.get('portal_image_url')),
            price_ton=gift.get('price_ton'),
            emoji_id=str(mapping.get('emoji_id') or ''),
            emoji=str(mapping.get('emoji') or ''),
        ))
    return jsonify(items=gifts[:500])


@app.post('/api/admin/gift-emojis')
@admin_required
def admin_save_gift_emoji():
    data = request.get_json(silent=True) or {}
    gift_id = str(data.get('gift_id') or '').strip()
    emoji_id = str(data.get('emoji_id') or '').strip()
    gift = next((g for g in read_catalog().get('gifts', []) if str(g.get('id') or '') == gift_id), None)
    if not gift:
        return error('Подарок не найден в каталоге Portal.', 404)
    if not emoji_id:
        save_document('gift_emoji:' + gift_id, dict(
            gift_id=gift_id, gift_name=str(gift.get('name') or 'Подарок'),
            emoji_id='', emoji='', updated_at=datetime.now(timezone.utc).isoformat()))
        return jsonify(ok=True, item=dict(gift_id=gift_id, emoji_id='', emoji=''))
    if not re.fullmatch(r'[0-9]{5,30}', emoji_id):
        return error('Введите корректный Telegram custom emoji ID.')
    try:
        resolved = fetch_custom_emoji_map([emoji_id])
    except RuntimeError as exc:
        return error(str(exc), 409)
    emoji = resolved.get(emoji_id)
    if not emoji:
        return error('Telegram не нашёл premium emoji с таким ID.', 404)
    remember_emojis([emoji])
    record = dict(
        gift_id=gift_id, gift_name=str(gift.get('name') or 'Подарок'),
        emoji_id=emoji_id, emoji=str(emoji.get('emoji') or ''),
        image_url=safe_image(gift.get('image_url') or gift.get('portal_image_url')),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    save_document('gift_emoji:' + gift_id, record)
    return jsonify(ok=True, item=record)


def _broadcast_filter_values(data):
    filters = data if isinstance(data, dict) else {}
    def money(name):
        raw = filters.get(name)
        if raw in (None, ''):
            return 0
        try:
            value = parse_amount(raw)
        except (ValueError, TypeError, InvalidOperation):
            raise ValueError('Проверьте денежные фильтры рассылки.')
        if value < 0:
            raise ValueError('Фильтры не могут быть отрицательными.')
        return value
    try:
        age_days = int(filters.get('min_account_days') or 0)
    except (TypeError, ValueError):
        raise ValueError('Возраст аккаунта должен быть указан в днях.')
    if not 0 <= age_days <= 3650:
        raise ValueError('Возраст аккаунта: от 0 до 3650 дней.')
    try:
        user_id = int(filters.get('user_id') or 0)
        exclude_user_id = int(filters.get('exclude_user_id') or 0)
    except (TypeError, ValueError):
        raise ValueError('Некорректный пользователь.')
    recipient = str(filters.get('recipient') or '').strip()[:80]
    return dict(
        min_balance=money('min_balance'),
        min_turnover=money('min_turnover'),
        min_deposit=money('min_deposit'),
        min_account_days=age_days,
        user_id=max(0, user_id),
        exclude_user_id=max(0, exclude_user_id),
        recipient=recipient,
    )


def broadcast_recipients(db, raw_filters):
    filters = _broadcast_filter_values(raw_filters)
    where, params = ['1=1'], []
    if filters['user_id']:
        where.append('u.id=?'); params.append(filters['user_id'])
    elif filters['recipient']:
        term = filters['recipient'].lstrip('@').strip()
        if term.isdigit():
            where.append('u.id=?'); params.append(int(term))
        else:
            like = '%' + term.lower() + '%'
            where.append('(LOWER(u.username) LIKE ? OR LOWER(u.name) LIKE ?)'); params.extend([like, like])
    if filters['exclude_user_id']:
        where.append('u.id<>?'); params.append(filters['exclude_user_id'])
    if filters['min_balance']:
        where.append('u.balance>=?'); params.append(filters['min_balance'])
    if filters['min_turnover']:
        where.append('u.turnover_cents>=?'); params.append(filters['min_turnover'])
    if filters['min_deposit']:
        where.append('(SELECT COALESCE(SUM(d.amount),0) FROM deposits d WHERE d.user_id=u.id)>=?')
        params.append(filters['min_deposit'])
    if filters['min_account_days']:
        cutoff = datetime.now(timezone.utc) - timedelta(days=filters['min_account_days'])
        where.append("u.created_at<>'' AND u.created_at<=?")
        params.append(cutoff.strftime('%Y-%m-%d %H:%M:%S'))
    query = f"""SELECT u.id,u.name,u.username,u.photo_url,u.balance,u.turnover_cents,
                       (SELECT COALESCE(SUM(d.amount),0) FROM deposits d WHERE d.user_id=u.id) AS deposit_total
                FROM users u WHERE {' AND '.join(where)}
                ORDER BY u.id DESC LIMIT 20000"""
    return db.execute(query, tuple(params)).fetchall(), filters


@app.post('/api/admin/broadcast/preview')
@admin_required
def admin_broadcast_preview():
    data = request.get_json(silent=True) or {}
    try:
        with connect() as db:
            rows, filters = broadcast_recipients(db, data.get('filters') or {})
    except ValueError as exc:
        return error(str(exc))
    users = [dict(id=int(x['id']), name=x['name'], username=x['username'] or '',
                  photo_url=x['photo_url'] or '', balance=int(x['balance'] or 0)/100,
                  turnover=int(x['turnover_cents'] or 0)/100,
                  deposit_total=int(x['deposit_total'] or 0)/100) for x in rows[:50]]
    return jsonify(count=len(rows), users=users, filters=filters)


def _bc_call(method, payload, files=None):
    """Telegram call that keeps the error code: returns (True, result) or (False, dict(code, description, retry))."""
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/{method}'
    try:
        if files:
            data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)) for k, v in payload.items()}
            response = requests.post(url, data=data, files=files, timeout=(3, 40))
        else:
            response = requests.post(url, json=payload, timeout=(3, 15))
        data = response.json()
    except (requests.RequestException, ValueError):
        return False, dict(code=0, description='network', retry=0)
    if data.get('ok'):
        return True, data.get('result')
    return False, dict(code=int(data.get('error_code') or response.status_code), description=str(data.get('description') or '')[:200],
                       retry=int((data.get('parameters') or {}).get('retry_after') or 0))


def _bc_upload_photo(item):
    if not isinstance(item, dict) or not item.get('blob'):
        raise ValueError('Некорректный временный файл рассылки.')
    blob = item.get('blob')
    if not isinstance(blob, (bytes, bytearray)) or not blob or len(blob) > 8 * 1024 * 1024:
        raise ValueError('Фото рассылки повреждено или превышает 8 МБ.')
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', str(item.get('name') or 'photo.jpg'))[:120] or 'photo.jpg'
    mime = str(item.get('mime') or 'image/jpeg')
    if not mime.startswith('image/'):
        mime = 'image/jpeg'
    return name, bytes(blob), mime


def _bc_send_one(b, user_id):
    text, chat = b['text'] or '', int(user_id)
    try:
        markup = {'inline_keyboard': json.loads(b['buttons'] or '[]')} or None
        raw_photos = b['photos']
        ids = raw_photos if isinstance(raw_photos, list) else json.loads(raw_photos or '[]')
    except (TypeError, ValueError, KeyError):
        markup, ids = None, []
    if markup and not markup['inline_keyboard']:
        markup = None
    visible = len(re.sub(r'<[^>]+>', '', text))
    cached_ids = []

    def message(body):
        payload = dict(chat_id=chat, text=body or '👇', parse_mode='HTML',
                       link_preview_options={'is_disabled': True})
        if markup:
            payload['reply_markup'] = markup
        return _bc_call('sendMessage', payload)

    if not ids:
        ok, res = message(text)
        return ok, res, cached_ids

    if len(ids) == 1:
        caption_ok = bool(text) and visible <= 1024
        payload = dict(chat_id=chat)
        if caption_ok:
            payload.update(caption=text, parse_mode='HTML')
        if markup and (caption_ok or not text):
            payload['reply_markup'] = markup
        if isinstance(ids[0], dict):
            try:
                upload = _bc_upload_photo(ids[0])
            except ValueError as exc:
                return False, dict(code=0, description=str(exc), retry=0), cached_ids
            ok, res = _bc_call('sendPhoto', payload, files={'photo': upload})
        else:
            payload['photo'] = str(ids[0])
            ok, res = _bc_call('sendPhoto', payload)
        if ok and isinstance(res, dict) and res.get('photo'):
            cached_ids = [str(res['photo'][-1].get('file_id') or '')]
            cached_ids = [x for x in cached_ids if x]
        if ok and text and not caption_ok:
            ok, res = message(text)
        return ok, res, cached_ids

    caption_ok = bool(text) and visible <= 1024 and not markup
    media, files = [], {}
    try:
        for index, item in enumerate(ids):
            if isinstance(item, dict):
                key = f'photo{index}'
                files[key] = _bc_upload_photo(item)
                media.append(dict(type='photo', media='attach://' + key))
            else:
                media.append(dict(type='photo', media=str(item)))
    except ValueError as exc:
        return False, dict(code=0, description=str(exc), retry=0), cached_ids
    if caption_ok:
        media[0].update(caption=text, parse_mode='HTML')
    ok, res = _bc_call('sendMediaGroup', dict(chat_id=chat, media=media), files=files or None)
    if ok and isinstance(res, list):
        cached_ids = [
            str((msg.get('photo') or [{}])[-1].get('file_id') or '')
            for msg in res if isinstance(msg, dict) and msg.get('photo')
        ]
        cached_ids = [x for x in cached_ids if x]
    if ok and (markup or (text and not caption_ok)):
        ok, res = message(text)
    return ok, res, cached_ids

def _bc_process_batch():
    if not BOT_TOKEN:
        return False
    now = int(time.time())
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE broadcast_items SET state='pending' WHERE state='sending' AND next_at<?", (now - 90,))
        rows = db.execute("""SELECT i.id,i.user_id,i.attempts,i.broadcast_id FROM broadcast_items i
                             JOIN broadcasts b ON b.id=i.broadcast_id
                             WHERE i.state='pending' AND i.next_at<=? AND b.state='running' ORDER BY i.id LIMIT 20"""
                          + (' FOR UPDATE OF i SKIP LOCKED' if DATABASE_URL else ''), (now,)).fetchall()
        for r in rows:
            db.execute("UPDATE broadcast_items SET state='sending',next_at=? WHERE id=?", (now, r['id']))
        db.commit()
    cache = {}
    for r in rows:
        with connect() as db:
            if r['broadcast_id'] not in cache:
                cache[r['broadcast_id']] = db.execute('SELECT * FROM broadcasts WHERE id=?', (r['broadcast_id'],)).fetchone()
        b = cache[r['broadcast_id']]
        ok, res, _ = _bc_send_one(b, r['user_id'])
        attempts, state, error, col, delay = int(r['attempts']) + 1, 'sent', '', 'sent', 0
        if not ok:
            code, desc = res['code'], res['description']
            error = desc
            if code == 429:
                time.sleep(min(max(res['retry'], 1), 30)); state, col, attempts, delay = 'pending', '', int(r['attempts']), 1
            elif code == 403 or re.search(r'blocked|deactivated|chat not found|user not found', desc, re.I):
                state, col = 'blocked', 'blocked'
            elif code == 0 or code >= 500:
                state, col, delay = ('pending', '', 10 * attempts) if attempts < 3 else ('failed', 'failed', 0)
            else:
                state, col = 'failed', 'failed'
        with connect() as db:
            db.execute('UPDATE broadcast_items SET state=?,attempts=?,error=?,next_at=? WHERE id=?',
                       (state, attempts, error[:200], int(time.time()) + delay, r['id']))
            if col in ('sent', 'blocked', 'failed'):
                db.execute(f'UPDATE broadcasts SET {col}={col}+1 WHERE id=?', (r['broadcast_id'],))
        time.sleep(.04)
    with connect() as db:
        db.execute("""UPDATE broadcasts SET state='done',finished_at=? WHERE state='running' AND NOT EXISTS
                      (SELECT 1 FROM broadcast_items x WHERE x.broadcast_id=broadcasts.id AND x.state IN ('pending','sending'))""",
                   (int(time.time()),))
    return bool(rows)


def broadcast_worker_loop():
    time.sleep(4)
    while True:
        busy = False
        try:
            busy = _bc_process_batch()
        except Exception:
            app.logger.exception('Broadcast worker failed')
        time.sleep(.05 if busy else 2)


@app.post('/api/admin/broadcast/send')
@admin_required
def admin_broadcast_send():
    multipart = bool(request.files) or str(request.content_type or '').startswith('multipart/form-data')
    data = request.form if multipart else (request.get_json(silent=True) or {})
    raw_text = str(data.get('text') or '').strip()
    try:
        raw_buttons = json.loads(data.get('buttons') or '[]') if multipart else (data.get('buttons') or [])
        raw_filters = json.loads(data.get('filters') or '{}') if multipart else (data.get('filters') or {})
    except (TypeError, ValueError):
        return error('Некорректные кнопки или фильтры.')
    files = [f for f in (request.files.getlist('photos') if multipart else []) if f and getattr(f, 'filename', '')]
    if not raw_text and not files:
        return error('Введите текст или добавьте фото.')
    if len(raw_text) > 4096:
        return error('Текст рассылки должен быть не длиннее 4096 символов.')
    if len(files) > 10:
        return error('Можно прикрепить не более 10 фото.')
    test = str(data.get('test') or '').lower() in ('1', 'true', 'on', 'yes')
    if test:
        raw_filters = {'user_id': session['uid']}
    try:
        buttons = normalize_post_buttons(raw_buttons)
        text, _ = resolve_post_custom_emojis(raw_text, buttons)
        with connect() as db:
            rows, filters = broadcast_recipients(db, raw_filters)
        if not rows:
            return error('По этим фильтрам нет получателей.', 409)
    except (ValueError, RuntimeError) as exc:
        return error(str(exc), 409)

    # Never upload a "service copy" to the admin chat. If photos are attached, send
    # the real broadcast once to the first reachable recipient, capture Telegram
    # file_ids from that real delivery, then queue all remaining users with file_ids.
    uploads = []
    for f in files:
        blob = f.read()
        if not (f.mimetype or '').startswith('image/') or not blob:
            return error('Прикрепляйте только изображения (JPG, PNG, WEBP).')
        if len(blob) > 8 * 1024 * 1024:
            return error('Одно фото — не более 8 МБ.')
        uploads.append({
            'name': (f.filename or 'photo.jpg')[:120],
            'mime': (f.mimetype or 'image/jpeg')[:80],
            'blob': blob,
        })

    staged_user_id = None
    file_ids = []
    if uploads:
        if not BOT_TOKEN:
            return error('BOT_TOKEN не настроен — фото рассылки нельзя загрузить в Telegram.', 503)
        staging = {'text': text, 'photos': uploads, 'buttons': json.dumps(buttons, ensure_ascii=False)}
        last_error = ''
        # A blocked first user must not break the whole broadcast. Try a small
        # prefix of the selected audience until Telegram accepts the real delivery.
        for row in rows[:min(12, len(rows))]:
            ok, result, cached = _bc_send_one(staging, int(row['id']))
            if ok and cached:
                staged_user_id = int(row['id'])
                file_ids = cached
                break
            if isinstance(result, dict):
                last_error = str(result.get('description') or '')[:180]
        if staged_user_id is None or not file_ids:
            return error('Telegram не принял фото рассылки' + (': ' + last_error if last_error else '') + '.', 502)

    now = int(time.time())
    with connect() as db:
        cur = db.execute(
            'INSERT INTO broadcasts(admin_id,text,photos,buttons,total,sent,created_at) VALUES(?,?,?,?,?,?,?)',
            (session['uid'], text, json.dumps(file_ids, ensure_ascii=False), json.dumps(buttons, ensure_ascii=False),
             len(rows), 1 if staged_user_id is not None else 0, now))
        bid = cur.lastrowid
        for row in rows:
            uid = int(row['id'])
            if uid == staged_user_id:
                db.execute("""INSERT INTO broadcast_items(broadcast_id,user_id,state,attempts,error,next_at)
                              VALUES(?,?,'sent',1,'',0)""", (bid, uid))
            else:
                db.execute('INSERT INTO broadcast_items(broadcast_id,user_id) VALUES(?,?)', (bid, uid))
        if staged_user_id is not None and len(rows) == 1:
            db.execute("UPDATE broadcasts SET state='done',finished_at=? WHERE id=?", (now, bid))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'broadcast',
                    json.dumps(dict(id=bid, count=len(rows), photos=len(file_ids), test=test, filters=filters,
                                    staged_user_id=staged_user_id), ensure_ascii=False)[:1000]))
        db.commit()
    NOTIFY_WAKE.set()
    return jsonify(ok=True, id=bid, queued=len(rows), sent_now=1 if staged_user_id is not None else 0)


@app.get('/api/admin/broadcast/status')
@admin_required
def admin_broadcast_status():
    with connect() as db:
        rows = db.execute('SELECT * FROM broadcasts ORDER BY id DESC LIMIT 8').fetchall()
        items = []
        for r in rows:
            done = int(r['sent']) + int(r['failed']) + int(r['blocked'])
            try:
                photos = len(json.loads(r['photos'] or '[]'))
            except (TypeError, ValueError):
                photos = 0
            errors = db.execute("""SELECT user_id,state,error FROM broadcast_items
                                   WHERE broadcast_id=? AND state IN ('failed','blocked') AND error<>''
                                   ORDER BY id DESC LIMIT 6""", (r['id'],)).fetchall()
            items.append(dict(
                id=r['id'], state=r['state'], total=r['total'], sent=r['sent'], failed=r['failed'],
                blocked=r['blocked'], pending=max(0, int(r['total']) - done), photos=photos,
                created_at=r['created_at'], preview=re.sub(r'<[^>]+>', '', r['text'] or '')[:70],
                errors=[dict(user_id=x['user_id'], state=x['state'], error=x['error']) for x in errors],
            ))
    return jsonify(items=items)


@app.post('/api/admin/broadcast/<int:bid>/cancel')
@admin_required
def admin_broadcast_cancel(bid):
    with connect() as db:
        db.execute("UPDATE broadcasts SET state='cancelled',finished_at=? WHERE id=? AND state='running'", (int(time.time()), bid))
        db.execute("UPDATE broadcast_items SET state='cancelled' WHERE broadcast_id=? AND state IN ('pending','sending')", (bid,))
        db.commit()
    return jsonify(ok=True)


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
            if kind == 'web_app':
                try:
                    button['web_app'] = {'url': miniapp_target_url(value)}
                except ValueError as exc:
                    raise ValueError(f'Кнопка «{text}»: {exc}') from exc
            elif kind == 'url':
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
    """Only expand explicitly requested custom emoji tokens.

    Never replace a normal Unicode emoji by fallback value: several Telegram gifts
    can share the same fallback glyph, which previously caused a random saved
    custom emoji to appear in Freebet/giveaway notifications.
    """
    return custom_emoji_html(str(text))


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
                   'promo_issued':'bonuses','referral_bonus':'bonuses','level_claim':'levels','reward_task_claim':'giveaways',
                   'giveaway_enter':'giveaways','upgrade':'profile','daily_top_reward':'profile'}
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
    if action == 'levels':
        text = (f'🎟 <b>Вам выдан промокод за уровень</b>\n\n<code>{safe_code}</code>\n\n'
                'Введите его в разделе «Бонусы». Код всегда можно увидеть снова: нажмите на этот уровень в списке уровней.')
        notify_user_async(user_id, text, miniapp_markup('🎁 Ввести промокод', 'bonuses'), 'HTML')
        return
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
        icon = gift_custom_emoji_html(win.get('gift_id'), win.get('name'), '🎁')
        lines.append(f'#{int(win.get("rank") or 0)} — {icon} <b>{escape(str(win.get("name") or "Подарок"))}</b>{price_text}')
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


WITHDRAWAL_MIN_TON_CONNECT_DEFAULT_CENTS = 300


def withdrawal_settings():
    doc = read_document('withdrawal_settings') or {}
    try:
        minimum = int(doc.get('min_ton_connect_deposit_cents', WITHDRAWAL_MIN_TON_CONNECT_DEFAULT_CENTS))
    except (TypeError, ValueError):
        minimum = WITHDRAWAL_MIN_TON_CONNECT_DEFAULT_CENTS
    minimum = max(0, min(100000000, minimum))
    return dict(min_ton_connect_deposit_cents=minimum, min_ton_connect_deposit=minimum / 100)


def withdrawal_minimum_message(required_cents, deposited_cents):
    required = f'{required_cents / 100:.2f}'.rstrip('0').rstrip('.')
    deposited = f'{deposited_cents / 100:.2f}'.rstrip('0').rstrip('.')
    return (f'Для вывода нужен подтверждённый депозит от {required} TON через TON Connect. '
            f'Сейчас учтено: {deposited} TON.')


def ton_connect_deposit_total(db, user_id):
    """Only confirmed deposits created by the TON Connect/on-chain flow count for withdrawal access."""
    row = db.execute("""SELECT COALESCE(SUM(amount),0) AS total
                        FROM deposits
                        WHERE user_id=? AND request_key LIKE 'ton:%'""",
                     (user_id,)).fetchone()
    return int(row['total'] or 0) if row else 0


def stars_withdrawal_message(until):
    local_date = until.strftime('%d.%m.%Y')
    return f'Ваш вывод ограничен до {local_date} после пополнения через Telegram Stars.'


def withdrawal_access_error(db, user_id):
    account = db.execute('''SELECT withdrawal_enabled,withdrawal_block_reason,stars_withdrawal_until,
                                   withdrawal_min_deposit_override
                            FROM users WHERE id=?''',
                         (user_id,)).fetchone()
    if not account:
        return 'Пользователь не найден.'
    if not bool(account['withdrawal_enabled']):
        reason = str(account['withdrawal_block_reason'] or '').strip()
        return reason or 'Вывод для вашего аккаунта временно недоступен. Обратитесь в поддержку.'
    stars_until = parse_datetime_utc(account['stars_withdrawal_until'])
    if stars_until and stars_until > datetime.now(timezone.utc):
        return stars_withdrawal_message(stars_until)
    global_required = int(withdrawal_settings()['min_ton_connect_deposit_cents'])
    override = account['withdrawal_min_deposit_override']
    required = global_required if override is None else max(0, int(override or 0))
    deposited = ton_connect_deposit_total(db, user_id)
    if deposited < required:
        return withdrawal_minimum_message(required, deposited)
    wager = db.execute('SELECT withdrawal_wager_required,withdrawal_wager_progress FROM users WHERE id=?',(user_id,)).fetchone()
    remaining=max(0,int(wager['withdrawal_wager_required'] or 0)-int(wager['withdrawal_wager_progress'] or 0)) if wager else 0
    if remaining>0:
        return f'Для вывода нужно сделать ещё оборот {remaining/100:.2f} TON.'
    return ''


@app.get('/api/admin/withdrawal-settings')
@admin_required
def admin_withdrawal_settings_get():
    return jsonify(**withdrawal_settings())


@app.post('/api/admin/withdrawal-settings')
@admin_required
def admin_withdrawal_settings_set():
    data = request.get_json(silent=True) or {}
    try:
        minimum = parse_amount(data.get('min_ton_connect_deposit', data.get('min_deposit_ton', '3')))
    except (ValueError, TypeError, InvalidOperation):
        return error('Введите минимальный депозит с точностью до 0.01 TON.')
    if not 0 <= minimum <= 100000000:
        return error('Минимальный депозит: от 0 до 1 000 000 TON.')
    save_document('withdrawal_settings', {
        'min_ton_connect_deposit_cents': minimum,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,0,?,?)',
                   (session['uid'], 'withdrawal_settings', f'min_ton_connect_deposit={minimum}'))
    return jsonify(ok=True, **withdrawal_settings())


@app.post('/api/withdrawal/contact-notice')
@login_required
def withdrawal_contact_notice():
    # Check withdrawal eligibility before showing the one-time contact notice,
    # so users without the required TON Connect deposit see the real reason first.
    with connect() as db:
        access_error = withdrawal_access_error(db, session['uid'])
        if access_error:
            status = 404 if access_error == 'Пользователь не найден.' else 403
            return error(access_error, status)
        inserted = db.execute('''INSERT INTO withdrawal_contact_notices(user_id)
                                 VALUES(?) ON CONFLICT(user_id) DO NOTHING''',
                              (session['uid'],))
        show_notice = bool(inserted.rowcount)
        db.commit()
    return jsonify(ok=True, show_notice=show_notice)


@app.post('/api/inventory/<int:item_id>/withdraw')
@login_required
def request_withdrawal(item_id):
    if creator_demo_active(session['uid']):
        return error('Demo-подарки нельзя отправить на реальный вывод.', 409)
    fee=30
    withdrawal_id=None
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')

        # Mobile clients can retry the same POST if the first response is lost.
        # Treat an already-created withdrawal for this inventory item as success:
        # never charge the fee twice and never tell the player the gift vanished.
        existing=db.execute("""SELECT id,status FROM withdrawals
                               WHERE user_id=? AND inventory_id=? AND status IN ('pending','approved')
                               ORDER BY id DESC LIMIT 1""",(session['uid'],item_id)).fetchone()
        if existing:
            db.rollback()
            existing_id=int(existing['id'])
            state=str(existing['status'] or 'pending')
            return jsonify(ok=True,fee=0.30,withdrawal_id=existing_id,
                           auto_status='completed' if state=='approved' else 'queued',
                           manual_required=False,duplicate=True,
                           message='Заявка на вывод уже создана.' if state=='pending' else 'Подарок уже выведен.')

        purge_expired_inventory(db,session['uid'])
        access_error=withdrawal_access_error(db,session['uid'])
        if access_error:
            return error(access_error,404 if access_error=='Пользователь не найден.' else 403)
        account=db.execute('SELECT balance FROM users WHERE id=?',(session['uid'],)).fetchone()
        if not account or int(account['balance'] or 0)<fee:
            return error('Недостаточно средств для комиссии вывода 0.30 TON. Пополните баланс.',409)
        item=db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?',(item_id,session['uid'])).fetchone()
        if not item:return error('Подарок не найден или уже отправлен на вывод.',404)
        if item['promo_locked']:return error('Промо-подарок нельзя вывести до завершения отыгрыша.',409)
        if int(item['deposit_mirror'] or 0):return error('Этот NFT уже учтён как пополнение TON и хранится в инвентаре как подтверждение.',409)
        db.execute('UPDATE users SET balance=balance-? WHERE id=?',(fee,session['uid']))
        cur=db.execute("""INSERT INTO withdrawals(user_id,inventory_id,gift_id,gift_name,image_url,floor_price,source,round_id,status,external_url,
                                               fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url,fee_amount)
                      VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)""",
                   (session['uid'],item['id'],item['gift_id'],item['gift_name'],item['image_url'],item['floor_price'],item['source'],
                    item['round_id'],item['external_url'] or '',item['fragment_number'] or '',item['fragment_model'] or '',
                    item['fragment_backdrop'] or '',item['fragment_symbol'] or '',item['price_source'] or '',item['animation_url'] or '',fee))
        withdrawal_id=int(cur.lastrowid)
        if not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?',(item_id,session['uid'])).rowcount:
            db.rollback()
            return error('Не удалось зарезервировать подарок.',409)
        record_transaction(db,session['uid'],'withdrawal_fee',-fee,'inventory',item_id,'Комиссия вывода подарка 0.30 TON')
        record_transaction(db,session['uid'],'withdrawal_request',0,'inventory',item_id,item['gift_name'])
        db.commit()
    finally:
        db.close()

    # From this point the withdrawal is already durably committed. Auxiliary
    # logging/provider startup must never turn a successful reservation into HTTP 500.
    row=_relayer_withdrawal_target(withdrawal_id)
    if not relayer_settings().get('auto_withdraw_enabled',True):
        if row:
            try:
                _relayer_auto_log(withdrawal_id,row,'disabled',error_text='Автовывод отключён. Требуется ручной вывод.')
            except Exception:
                app.logger.exception('Could not write disabled auto-withdraw status for #%s',withdrawal_id)
        return jsonify(ok=True,fee=0.30,withdrawal_id=withdrawal_id,auto_status='disabled',manual_required=True,
                       message='Автовывод отключён. Заявка сохранена для ручного вывода.')

    if row:
        try:
            _relayer_auto_log(withdrawal_id,row,'queued')
        except Exception:
            app.logger.exception('Could not write queued auto-withdraw status for #%s',withdrawal_id)
    try:
        Thread(target=_auto_withdrawal_thread,args=(withdrawal_id,),daemon=True).start()
    except Exception:
        app.logger.exception('Could not start auto-withdraw worker for #%s',withdrawal_id)

    return jsonify(ok=True,fee=0.30,withdrawal_id=withdrawal_id,auto_status='queued',manual_required=False,
                   message='Заявка принята. Проверяем Relayer и Portal Market.')

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

        # Shared burn-pool gifts are claimed only here. Reaching the wager target
        # merely marks the placeholder ready; the first player who unlocks the
        # last available shared NFT wins it, and all other active placeholders burn.
        pool_claim=claim_freebet_burn_prize(db,item['promo_code'] or '',session['uid'])
        burn_race_winner=False
        if pool_claim is not None:
            if not pool_claim.get('claimed'):
                db.execute('DELETE FROM inventory WHERE id=? AND user_id=?',(item_id,session['uid']))
                db.commit()
                return error('Сгораемый подарок уже разблокировал другой игрок. Ваш подарок сгорел.',409)
            burn_race_winner=True
            prize=pool_claim.get('gift') or {}
            gift_id=str(prize.get('gift_id') or item['gift_id'])
            gift_name=str(prize.get('gift_name') or item['gift_name'])[:140]
            image_url=safe_image(prize.get('image_url')) or item['image_url']
            floor_price=max(0,int(prize.get('floor_price') or item['floor_price'] or 0))
            db.execute("""UPDATE inventory SET gift_id=?,gift_name=?,image_url=?,floor_price=?,
                          external_url=?,fragment_number=?,fragment_model=?,fragment_backdrop=?,fragment_symbol=?,
                          price_source=?,animation_url=?,promo_locked=0,promo_wager_multiplier=0,promo_wager_target=0,
                          promo_wager_progress=0,promo_code='',expires_at=NULL,source='freebet_burn_claimed',
                          promo_unlock_payload='{}'
                          WHERE id=? AND user_id=?""",
                       (gift_id,gift_name,image_url,floor_price,str(prize.get('fragment_url') or ''),
                        str(prize.get('fragment_number') or ''),str(prize.get('fragment_model') or ''),
                        str(prize.get('fragment_backdrop') or ''),str(prize.get('fragment_symbol') or ''),
                        str(prize.get('price_source') or ''),safe_image(prize.get('animation_url')),
                        item_id,session['uid']))
            detail=f'Сгораемый Fragment-подарок разблокирован: {gift_name}'
        else:
            # Normal wager gift unlock: keep the same gift. Upgrade targets never
            # replace a promo-wager gift; they only add wager progress.
            db.execute("""UPDATE inventory SET promo_locked=0,promo_wager_multiplier=0,promo_wager_target=0,
                          promo_wager_progress=0,promo_code='',expires_at=NULL,source='promo_claimed',
                          promo_unlock_payload='{}'
                          WHERE id=? AND user_id=?""", (item_id, session['uid']))
            detail=f'Подарок разблокирован: {item["gift_name"]}'
        record_transaction(db, session['uid'], 'promo_wager_claim', 0, 'inventory', item_id, detail)
        db.commit()
        updated = db.execute('SELECT * FROM inventory WHERE id=?', (item_id,)).fetchone()
        return jsonify(ok=True, item=inventory_item(updated), user=profile(), burn_pool=burn_race_winner)
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



# ======================= Game switches (on / off / admins only) =======================
GAME_KEYS = ('mines', 'upgrade', 'crash', 'arena', 'hilo', 'limbo')
GAME_MODE_DEFAULTS = {'mines': 'on', 'upgrade': 'on', 'crash': 'off', 'arena': 'off', 'hilo': 'off', 'limbo': 'admin'}


GAME_BADGES = ('new', 'hot', 'top', 'beta', 'soon')
GAME_LAYOUT_DEFAULT_ORDER = ('limbo', 'hilo', 'arena', 'mines', 'upgrade', 'crash')
GAME_LAYOUT_DEFAULT_BADGES = {'limbo': 'new', 'hilo': 'new', 'arena': 'new'}


def game_layout():
    """Order of game cards and their badges (NEW/HOT/...), editable in the admin panel."""
    try:
        stored = read_document('game_layout') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    order = []
    for key in (stored.get('order') if isinstance(stored.get('order'), list) else []):
        if key in GAME_KEYS and key not in order:
            order.append(key)
    for key in GAME_LAYOUT_DEFAULT_ORDER:
        if key not in order:
            order.append(key)
    raw = stored.get('badges') if isinstance(stored.get('badges'), dict) else None
    source = raw if raw is not None else GAME_LAYOUT_DEFAULT_BADGES
    badges = {key: source.get(key) for key in GAME_KEYS if source.get(key) in GAME_BADGES}
    return dict(order=order, badges=badges)


def is_admin_session():
    try:
        return int(session.get('uid') or 0) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


def game_modes():
    try:
        stored = read_document('game_modes') or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {key: (stored.get(key) if stored.get(key) in ('on', 'off', 'admin') else default)
            for key, default in GAME_MODE_DEFAULTS.items()}


def game_available(key, admin=None):
    mode = game_modes().get(key, 'on')
    if mode == 'on':
        return True
    if mode == 'admin':
        return is_admin_session() if admin is None else bool(admin)
    return False


def effective_games():
    admin = is_admin_session()
    return {key: game_available(key, admin) for key in GAME_KEYS}



# ================================== Limbo ==================================
# A single-bet "wheel" game: the player picks a win chance (1-90 %), the payout is
# RTP / chance. One provably-fair draw in [0, 10000) decides the round; the wheel on the
# client just lands the pointer on that exact position.
LIMBO_MIN_CHANCE = 1
LIMBO_MAX_CHANCE = 90
LIMBO_ROLL_RANGE = 10000          # roll is 0.00 .. 99.99
LIMBO_MAX_PAYOUT_CENTS = 100000   # one spin can never pay more than 1000 TON
LIMBO_FEED_SIZE = 14


def limbo_multiplier_x100(chance):
    """Payout multiplier (x100) for a win chance in whole percent, floored so the house edge never shrinks."""
    return max(101, int(game_rtp() * 10000 / int(chance)))


def limbo_config():
    return dict(min_chance=LIMBO_MIN_CHANCE, max_chance=LIMBO_MAX_CHANCE, roll_range=LIMBO_ROLL_RANGE,
                min_bet=MIN_BET_CENTS / 100, max_bet=MAX_BET_CENTS / 100,
                max_payout=LIMBO_MAX_PAYOUT_CENTS / 100, rtp=game_rtp(),
                multipliers={str(c): limbo_multiplier_x100(c) / 100 for c in range(LIMBO_MIN_CHANCE, LIMBO_MAX_CHANCE + 1)})


def limbo_row_view(row):
    return dict(id=int(row['id']), bet=int(row['bet']) / 100, chance=int(row['chance_bp']) / 100,
                multiplier=int(row['multiplier_x100']) / 100, roll=int(row['roll']) / 100,
                won=bool(row['won']), payout=int(row['payout']) / 100, created_at=str(row['created_at'] or ''))


def limbo_feed(db, user_id):
    wins = db.execute("""SELECT b.id,b.bet,b.chance_bp,b.multiplier_x100,b.roll,b.won,b.payout,b.created_at,
                                u.name,u.photo_url
                         FROM limbo_bets b JOIN users u ON u.id=b.user_id
                         WHERE b.won=1 ORDER BY b.id DESC LIMIT ?""", (LIMBO_FEED_SIZE,)).fetchall()
    mine = db.execute("""SELECT id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at
                         FROM limbo_bets WHERE user_id=? ORDER BY id DESC LIMIT 20""", (user_id,)).fetchall()
    feed = []
    for row in wins:
        item = limbo_row_view(row)
        item.update(profit=(int(row['payout']) - int(row['bet'])) / 100, name=str(row['name'] or '')[:40],
                    photo_url=str(row['photo_url'] or ''))
        feed.append(item)
    return feed, [limbo_row_view(row) for row in mine]


@app.get('/api/limbo/state')
@login_required
def limbo_state():
    uid = session['uid']
    with connect() as db:
        feed, mine = limbo_feed(db, uid)
    return jsonify(ok=True, config=limbo_config(), wins=feed, history=mine, user=profile(),
                   available=game_available('limbo'))


@app.post('/api/limbo/play')
@login_required
def limbo_play():
    data = request.get_json(silent=True) or {}
    uid = session['uid']
    try:
        bet = parse_amount(data.get('bet'))
    except (ValueError, InvalidOperation, TypeError):
        return error('Укажите корректную ставку.')
    if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
        return error('Ставка от 0.10 до 300 TON.')
    try:
        chance_value = float(data.get('chance'))
    except (TypeError, ValueError):
        return error('Укажите шанс от 1% до 90%.')
    if chance_value != chance_value or chance_value != int(chance_value) \
            or not (LIMBO_MIN_CHANCE <= int(chance_value) <= LIMBO_MAX_CHANCE):
        return error('Шанс — целое число от 1% до 90%.')
    chance = int(chance_value)
    mult_x100 = limbo_multiplier_x100(chance)
    payout_if_win = bet * mult_x100 // 100
    if payout_if_win > LIMBO_MAX_PAYOUT_CENTS:
        return error('Максимальный выигрыш за один спин — %d TON. Уменьшите ставку или шанс.' % (LIMBO_MAX_PAYOUT_CENTS // 100))
    proof = fairness_make('limbo', uid, data.get('client_seed'))
    roll, fair_cursor, _ = fairness_draw(proof, LIMBO_ROLL_RANGE, 0)
    won = roll < chance * 100
    payout = payout_if_win if won else 0
    db = connect()
    new_level = None
    try:
        db.execute('BEGIN IMMEDIATE')
        if DATABASE_URL:
            db.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (uid,))
        if not db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?', (bet, uid, bet)).rowcount:
            db.rollback()
            return error('Недостаточно средств.')
        cur = db.execute("""INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout)
                            VALUES(?,?,?,?,?,?,?)""", (uid, bet, chance * 100, mult_x100, roll, int(won), payout))
        bet_id = int(cur.lastrowid)
        record_transaction(db, uid, 'limbo_bet', -bet, 'limbo', bet_id, 'Limbo · шанс %d%%' % chance)
        new_level = increase_turnover(db, uid, bet)
        if payout:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, uid))
            record_transaction(db, uid, 'limbo_win', payout, 'limbo', bet_id, 'Limbo · x%.2f' % (mult_x100 / 100))
        outcome = dict(upper=LIMBO_ROLL_RANGE, ticket=roll, roll=roll, chance_bp=chance * 100, won=bool(won))
        fairness_store(db, proof, game_ref=bet_id, cursor=fair_cursor, outcome=outcome, state='settled')
        db.execute('UPDATE limbo_bets SET fairness_id=? WHERE id=?', (proof['id'], bet_id))
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception('Limbo spin failed')
        return error('Не удалось провести спин. Попробуйте ещё раз.', 500)
    finally:
        db.close()
    if new_level:
        notify_level_up_async(uid, new_level)
    with connect() as db:
        row = db.execute("""SELECT id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at
                            FROM limbo_bets WHERE id=?""", (bet_id,)).fetchone()
        fair = fairness_public(fairness_get(db, proof_id=proof['id']), True)
    return jsonify(ok=True, result=limbo_row_view(row), fairness=fair, user=profile(), new_level=new_level)



# ================================== Arena ==================================
ARENA_BETTING_MS = 20000      # first bet starts a 20s round; a lone bet is refunded after it closes
ARENA_SPIN_MS = 7000           # must match ARENA_SPIN_MS in the frontend
ARENA_RESULT_MS = ARENA_SPIN_MS + 6500  # full spin + landing + ~6s result screen before the next round
ARENA_FEE_PERCENT = 10        # house commission taken from the pool when the winner is paid
ARENA_SNIPE_WINDOW_MS = 1500  # a NEW player joining in the last 1.5 s extends the round (once per round)
ARENA_EXTEND_MS = 10000       # ...by this much


def arena_fee_cents(total):
    return int(total) * ARENA_FEE_PERCENT // 100


def arena_latest(db):
    return db.execute('SELECT * FROM arena_rounds ORDER BY id DESC LIMIT 1').fetchone()


def arena_create_round(db, now=None):
    """A new round waits for the first real bet: close_at=0 means "timer not started"."""
    now = int(now if now is not None else time.time() * 1000)
    db.execute("INSERT INTO arena_rounds(state,open_at,close_at) VALUES('open',?,0)", (now,))
    row = arena_latest(db)
    if row and not fairness_get(db, game='arena', game_ref=str(row['id'])):
        proof = fairness_make('arena', 0, f'arena:{row["id"]}', nonce=int(row['id']))
        fairness_store(db, proof, str(row['id']), 0, {'status': 'waiting_for_bets'})
    return row


def arena_gift_list(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        value = []
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def arena_gifts_public(raw):
    return [dict(name=str(g.get('name') or 'Подарок'), image_url=str(g.get('image_url') or ''),
                 price=int(g.get('price') or 0) / 100) for g in arena_gift_list(raw)]


def arena_restore_gift(db, user_id, snapshot):
    """Put a gift that was staked in the arena back into the inventory (refund)."""
    row = dict(snapshot.get('row') or {})
    row.pop('id', None)
    row['user_id'] = int(user_id)
    cols = [c for c in row if re.fullmatch(r'[a-z_][a-z0-9_]*', str(c))]
    if not cols:
        return
    db.execute(f"INSERT INTO inventory({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
               tuple(row[c] for c in cols))


def arena_pools(players):
    """(total, ton) in cents. total = TON + gifts at their price; only the TON part is ever taxed."""
    total = ton = 0
    for p in players:
        amount = max(0, int(p['amount'] or 0))
        gift = min(amount, max(0, int(p['gift_amount'] or 0)))
        total += amount
        ton += amount - gift
    return total, ton


def arena_prize_gifts(players):
    """Every gift staked in the round: the winner takes all of them."""
    out = []
    for p in players:
        for g in arena_gift_list(p['gifts']):
            out.append(dict(g, _owner=p['name'] if 'name' in p.keys() else ''))
    return out


def arena_award_gift(db, user_id, snapshot, round_id):
    """Give a staked gift to the round winner as a brand-new inventory item."""
    row = dict(snapshot.get('row') or {})
    if not row:
        row = dict(gift_id='arena', gift_name=str(snapshot.get('name') or 'Подарок'),
                   image_url=str(snapshot.get('image_url') or ''), floor_price=int(snapshot.get('price') or 0))
    row.pop('id', None)
    row.pop('created_at', None)
    row['source'] = 'arena_win'
    row['round_id'] = None
    arena_restore_gift(db, user_id, {'row': row})


def arena_players(db, round_id):
    return db.execute("""SELECT b.user_id,b.amount,b.gift_amount,b.gifts,u.name,u.username,u.photo_url
                         FROM arena_bets b JOIN users u ON u.id=b.user_id
                         WHERE b.round_id=? ORDER BY b.created_at ASC,b.user_id ASC""",
                      (round_id,)).fetchall()


def arena_refund_round(db, row, players):
    """Timer ended but there is nobody to play against: give every bet back and wait again."""
    for player in players:
        amount = max(0, int(player['amount'] or 0))
        if not amount:
            continue
        uid = int(player['user_id'])
        if uid == arena_bot_uid(db):
            continue   # house bot: its stake was never taken, so there is nothing to return
        gifts = arena_gift_list(player['gifts'])
        gift_amount = min(amount, max(0, int(player['gift_amount'] or 0)))
        ton = amount - gift_amount
        xp_back = ton + sum(int(g.get('price') or 0) for g in gifts if g.get('xp'))
        if ton:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (ton, uid))
            record_transaction(db, uid, 'arena_refund', ton, 'arena_round', row['id'],
                               f'Arena #{row["id"]}: возврат ставки, не набралось игроков')
        for gift in gifts:
            arena_restore_gift(db, uid, gift)
        if gifts:
            record_transaction(db, uid, 'arena_gift_refund', 0, 'arena_round', row['id'],
                               f'Arena #{row["id"]}: возвращены подарки — ' + ', '.join(str(g.get('name') or 'Подарок') for g in gifts))
        if xp_back:
            db.execute('UPDATE users SET turnover_cents=CASE WHEN turnover_cents>? THEN turnover_cents-? ELSE 0 END WHERE id=?',
                       (xp_back, xp_back, uid))
    db.execute('DELETE FROM arena_bets WHERE round_id=?', (row['id'],))
    db.execute("UPDATE arena_rounds SET close_at=0,extended=0 WHERE id=? AND state='open'", (row['id'],))


def arena_advance(db, now=None):
    now = int(now if now is not None else time.time() * 1000)
    row = arena_latest(db)
    if not row:
        return arena_create_round(db, now)
    if row['state'] == 'settled':
        if now >= int(row['settled_at'] or 0) + ARENA_RESULT_MS:
            return arena_create_round(db, now)
        return row
    close_at = int(row['close_at'] or 0)
    if close_at <= 0 or now < close_at:
        return row
    players = arena_players(db, row['id'])
    total = sum(max(0, int(x['amount'] or 0)) for x in players)
    if len(players) < 2 or total <= 0:
        arena_refund_round(db, row, players)
        return db.execute('SELECT * FROM arena_rounds WHERE id=?', (row['id'],)).fetchone()
    proof = fairness_get(db, game='arena', game_ref=str(row['id']))
    if not proof:
        generated = fairness_make('arena', 0, f'arena:{row["id"]}', nonce=int(row['id']))
        fairness_store(db, generated, str(row['id']), 0, {'status': 'legacy_round'})
        proof = fairness_get(db, game='arena', game_ref=str(row['id']))
    ticket, fair_cursor, _ = fairness_draw(proof, total, 0)
    cursor = 0
    winner = players[-1]
    for player in players:
        cursor += max(0, int(player['amount'] or 0))
        if ticket < cursor:
            winner = player
            break
    # Commission is taken ONLY from the TON part of the pool. Gifts are never taxed:
    # the winner receives every staked gift in full, plus the TON pool minus the fee.
    total, ton_total = arena_pools(players)
    fee = arena_fee_cents(ton_total)
    payout = ton_total - fee
    prize_gifts = arena_prize_gifts(players)
    bot_uid = arena_bot_uid(db)
    if bot_uid and int(winner['user_id']) == bot_uid:
        # House bot won: it is paid only what real players put in; its own (phantom) stake is not paid back.
        bot_ton = max(0, int(winner['amount'] or 0) - min(int(winner['amount'] or 0), int(winner['gift_amount'] or 0)))
        payout = max(0, payout - bot_ton)
        prize_gifts = [g for g in prize_gifts if not g.get('house')]
    changed = db.execute("""UPDATE arena_rounds SET state='settled',settled_at=?,winner_user_id=?,total_pool=?
                            WHERE id=? AND state='open'""",
                         (now, int(winner['user_id']), total, row['id']))
    if changed.rowcount:
        winner_id = int(winner['user_id'])
        if payout > 0:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, winner_id))
            record_transaction(db, winner_id, 'arena_win', payout, 'arena_round', row['id'],
                               f'Arena #{row["id"]}: выигрыш {payout/100:.2f} TON '
                               + (f'(TON-банк {ton_total/100:.2f}, комиссия {ARENA_FEE_PERCENT}% = {fee/100:.2f})'
                                  if fee else '(без комиссии)'))
        for gift in prize_gifts:
            arena_award_gift(db, winner_id, gift, row['id'])
        if prize_gifts:
            record_transaction(db, winner_id, 'arena_gift_win', 0, 'arena_round', row['id'],
                               f'Arena #{row["id"]}: получены подарки — '
                               + ', '.join(str(g.get('name') or 'Подарок') for g in prize_gifts)
                               + ' (без комиссии)')
        for player in players:
            if int(player['user_id']) != int(winner['user_id']) and int(player['user_id']) != bot_uid:
                names = ', '.join(str(g.get('name') or 'Подарок') for g in arena_gift_list(player['gifts']))
                record_transaction(db, int(player['user_id']), 'arena_loss', 0, 'arena_round', row['id'],
                                   f'Arena #{row["id"]}: проигрыш {int(player["amount"] or 0)/100:.2f} TON'
                                   + (f' (подарки: {names})' if names else ''))
        fairness_mark_settled(db, 'arena', row['id'], cursor=fair_cursor,
                              outcome={'ticket': ticket, 'upper': total,
                                       'winner_user_id': int(winner['user_id']),
                                       'players': [{'user_id': int(p['user_id']),
                                                    'amount': int(p['amount'] or 0)} for p in players]})
    return db.execute('SELECT * FROM arena_rounds WHERE id=?', (row['id'],)).fetchone()



DEMO_ARENA_BOTS = (
    dict(user_id=-101, name='Nova', username='demo_nova', photo_url=''),
    dict(user_id=-102, name='Pixel', username='demo_pixel', photo_url=''),
    dict(user_id=-103, name='Orbit', username='demo_orbit', photo_url=''),
)


def demo_arena_new_state(now=None):
    now = int(now if now is not None else time.time() * 1000)
    return dict(id=now, state='open', open_at=now, close_at=0, settled_at=0,
                winner_user_id=0, total_pool=0, players=[])


def demo_arena_advance(record, uid, now=None):
    now = int(now if now is not None else time.time() * 1000)
    state = dict(record.get('demo_arena') or {})
    if not state:
        state = demo_arena_new_state(now)
    if state.get('state') == 'settled':
        if now >= int(state.get('settled_at') or 0) + ARENA_RESULT_MS:
            state = demo_arena_new_state(now)
            record['demo_arena'] = state
        return state
    close_at = int(state.get('close_at') or 0)
    if close_at <= 0 or now < close_at:
        record['demo_arena'] = state
        return state
    players = list(state.get('players') or [])
    total = sum(max(0, int(x.get('amount') or 0)) for x in players)
    if len(players) < 2 or total <= 0:
        mine = next((x for x in players if int(x.get('user_id') or 0) == int(uid)), None)
        if mine:
            refund = max(0, int(mine.get('amount') or 0))
            record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + refund
            record['demo_turnover_cents'] = max(0, int(record.get('demo_turnover_cents') or 0) - refund)
        state = demo_arena_new_state(now)
        record['demo_arena'] = state
        return state
    ticket = secrets.randbelow(total)
    cursor = 0
    winner = players[-1]
    for player in players:
        cursor += max(0, int(player.get('amount') or 0))
        if ticket < cursor:
            winner = player
            break
    winner_id = int(winner.get('user_id') or 0)
    ton_total = sum(max(0, int(x.get('amount') or 0) - min(int(x.get('amount') or 0), int(x.get('gift_amount') or 0)))
                    for x in players)
    payout = ton_total - arena_fee_cents(ton_total)   # fee only on the TON part
    state.update(state='settled', settled_at=now, winner_user_id=winner_id, total_pool=total)
    if winner_id == int(uid):
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) + payout
        items = list(record.get('demo_inventory') or [])
        next_id = max([int(x.get('id') or 0) for x in items] + [0]) + 1
        for player in players:
            for gift in (player.get('gifts') or []):
                if isinstance(gift, dict) and isinstance(gift.get('item'), dict):
                    items.append(dict(gift['item'], id=next_id))
                    next_id += 1
        record['demo_inventory'] = items
    record['demo_arena'] = state
    return state


def demo_arena_state_payload(uid, now=None):
    now = int(now if now is not None else time.time() * 1000)
    record = creator_record(uid)
    state = demo_arena_advance(record, uid, now)
    save_creator_record(uid, {
        'demo_arena': state,
        'demo_balance_cents': record.get('demo_balance_cents', 0),
        'demo_turnover_cents': record.get('demo_turnover_cents', 0),
    })
    players = list(state.get('players') or [])
    total = sum(max(0, int(x.get('amount') or 0)) for x in players)
    result = []
    for index, player in enumerate(players):
        amount = max(0, int(player.get('amount') or 0))
        result.append(dict(
            index=index,
            user_id=int(player.get('user_id') or 0),
            name=str(player.get('name') or 'Игрок'),
            username=str(player.get('username') or ''),
            photo_url=str(player.get('photo_url') or ''),
            bet=amount / 100,
            gift_bet=min(amount, max(0, int(player.get('gift_amount') or 0))) / 100,
            gifts=[dict(name=str(g.get('name') or 'Подарок'), image_url=str(g.get('image_url') or ''),
                        price=int(g.get('price') or 0) / 100) for g in (player.get('gifts') or []) if isinstance(g, dict)],
            chance=(amount * 100.0 / total) if total else 0.0,
            mine=int(player.get('user_id') or 0) == int(uid),
        ))
    winner_id = int(state.get('winner_user_id') or 0)
    winner = next((x for x in result if x['user_id'] == winner_id), None) if winner_id else None
    mine = next((x for x in result if x['user_id'] == int(uid)), None)
    pool_cents = int(state.get('total_pool') or 0) if state.get('state') == 'settled' else total
    ton_cents = sum(max(0, int(x.get('amount') or 0) - min(int(x.get('amount') or 0), int(x.get('gift_amount') or 0)))
                    for x in players)
    fee_cents = arena_fee_cents(ton_cents)
    prize_gifts = [dict(name=str(g.get('name') or 'Подарок'), image_url=str(g.get('image_url') or ''),
                        price=int(g.get('price') or 0) / 100, owner=str(p.get('name') or ''))
                   for p in players for g in (p.get('gifts') or []) if isinstance(g, dict)]
    return dict(
        available=game_available('arena'), demo=True, now=now,
        betting_ms=ARENA_BETTING_MS, result_ms=ARENA_RESULT_MS,
        extend_ms=ARENA_EXTEND_MS, snipe_window_ms=ARENA_SNIPE_WINDOW_MS,
        fee_percent=ARENA_FEE_PERCENT, min_bet=MIN_BET_CENTS / 100,
        max_bet=MAX_BET_CENTS / 100,
        round=dict(id=int(state.get('id') or now), state=state.get('state') or 'open', extended=False,
                   open_at=int(state.get('open_at') or 0), close_at=int(state.get('close_at') or 0),
                   settled_at=int(state.get('settled_at') or 0), total_pool=pool_cents / 100,
                   ton_pool=ton_cents / 100, gift_pool=max(0, pool_cents - ton_cents) / 100,
                   fee=fee_cents / 100, payout=(ton_cents-fee_cents)/100, prize_gifts=prize_gifts,
                   winner_user_id=winner_id),
        players=result, winner=winner, my_bet=mine,
        mine_result=('won' if winner_id == int(uid) else 'lost') if winner_id and mine else None,
        balance=creator_record(uid)['demo_balance_cents']/100,
    )


def demo_arena_bet(data):
    uid = int(session['uid'])
    data = data or {}
    gift_mode = data.get('inventory_id') not in (None, '')
    record = creator_record(uid)
    item = None
    if gift_mode:
        item = demo_find_item(record, data.get('inventory_id'))
        if not item:
            raise ValueError('Подарок не найден в инвентаре.')
        if item.get('promo_locked'):
            raise ValueError('Отыгрышные подарки в арене недоступны.')
        amount = int(parse_amount(item.get('price_ton') or 0))
    else:
        try:
            amount = parse_amount(data.get('bet'))
        except (ValueError, InvalidOperation, TypeError):
            raise ValueError('Укажите корректную ставку.')
    if not (MIN_BET_CENTS <= amount <= MAX_BET_CENTS):
        raise ValueError('Ставка от 0.10 до 300 TON.')
    now = int(time.time() * 1000)
    state = demo_arena_advance(record, uid, now)
    close_at = int(state.get('close_at') or 0)
    if state.get('state') != 'open' or (close_at > 0 and now >= close_at):
        raise ValueError('Приём ставок закрыт. Дождитесь следующей арены.')
    mine = next((x for x in state.get('players') or [] if int(x.get('user_id') or 0) == uid), None)
    if mine and int(mine.get('amount') or 0) + amount > MAX_BET_CENTS:
        raise ValueError('Общая ставка в арене не может превышать 300 TON.')
    if not gift_mode and int(record.get('demo_balance_cents') or 0) < amount:
        raise ValueError('Недостаточно DEMO TON.')
    snapshot = None
    if gift_mode:
        demo_remove_item(record, item['id'])
        snapshot = dict(name=str(item.get('name') or 'Подарок')[:140], image_url=str(item.get('image_url') or ''), price=amount,
                        item=dict(item))
    else:
        record['demo_balance_cents'] = int(record.get('demo_balance_cents') or 0) - amount
    increase_demo_turnover(record, amount)

    def stake(player):
        player['amount'] = int(player.get('amount') or 0) + amount
        if snapshot:
            player['gift_amount'] = int(player.get('gift_amount') or 0) + amount
            player['gifts'] = list(player.get('gifts') or []) + [snapshot]

    def save():
        record['demo_arena'] = state
        save_creator_record(uid, {
            'demo_arena': state,
            'demo_balance_cents': record['demo_balance_cents'],
            'demo_turnover_cents': record['demo_turnover_cents'],
            'demo_inventory': record.get('demo_inventory') or [],
        })

    if mine:
        stake(mine)
        save()
        return demo_arena_state_payload(uid, now)
    with connect() as db:
        user = db.execute('SELECT name,username,photo_url FROM users WHERE id=?', (uid,)).fetchone()
    my_player = dict(user_id=uid, name=(user['name'] if user else 'Игрок'),
                     username=(user['username'] if user else '') or '',
                     photo_url=(user['photo_url'] if user else '') or '', amount=0)
    stake(my_player)
    players = [my_player]
    rng = secrets.SystemRandom()
    for bot in DEMO_ARENA_BOTS:
        factor = rng.uniform(.55, 1.45)
        bot_amount = max(MIN_BET_CENTS, min(MAX_BET_CENTS, int(round(amount * factor))))
        players.append({**bot, 'amount': bot_amount})
    state.update(open_at=now, close_at=now + ARENA_BETTING_MS, players=players)
    save()
    return demo_arena_state_payload(uid, now)


def arena_recent_rounds(db, limit=6):
    rows = db.execute(
        """SELECT r.id,r.total_pool,r.settled_at,r.winner_user_id,
                  u.name,u.username,u.photo_url,b.amount AS winner_stake,
                  (SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id) AS ton_pool
           FROM arena_rounds r
           LEFT JOIN users u ON u.id=r.winner_user_id
           LEFT JOIN arena_bets b ON b.round_id=r.id AND b.user_id=r.winner_user_id
           WHERE r.state='settled' AND r.winner_user_id>0
           ORDER BY r.id DESC LIMIT ?""", (max(1, min(12, int(limit))),)).fetchall()
    items=[]
    for row in rows:
        pool=max(0,int(row['total_pool'] or 0)); stake=max(0,int(row['winner_stake'] or 0))
        ton_pool=max(0,min(pool,int(row['ton_pool'] or 0)))
        payout=max(0,pool-arena_fee_cents(ton_pool))
        items.append(dict(id=int(row['id']),winner_user_id=int(row['winner_user_id'] or 0),
                          name=row['name'] or 'Игрок',username=row['username'] or '',photo_url=row['photo_url'] or '',
                          pool=pool/100,payout=payout/100,multiplier=(payout/stake if stake else 0),
                          settled_at=int(row['settled_at'] or 0)))
    return items


def arena_state_payload(db, uid, now=None):
    now = int(now if now is not None else time.time() * 1000)
    row = arena_latest(db) or arena_create_round(db, now)
    players = arena_players(db, row['id'])
    total = sum(max(0, int(x['amount'] or 0)) for x in players)
    result = []
    for index, player in enumerate(players):
        amount = max(0, int(player['amount'] or 0))
        result.append(dict(
            index=index,
            user_id=int(player['user_id']),
            name=player['name'] or 'Игрок',
            username=player['username'] or '',
            photo_url=player['photo_url'] or '',
            bet=amount / 100,
            gift_bet=min(amount, max(0, int(player['gift_amount'] or 0))) / 100,
            gifts=arena_gifts_public(player['gifts']),
            chance=(amount * 100.0 / total) if total else 0.0,
            mine=int(player['user_id']) == int(uid),
        ))
    winner_id = int(row['winner_user_id'] or 0)
    winner = next((x for x in result if x['user_id'] == winner_id), None) if winner_id else None
    mine = next((x for x in result if x['user_id'] == int(uid)), None)
    _, ton_cents = arena_pools(players)
    pool_cents = int(row['total_pool'] or 0) if row['state'] == 'settled' else total
    gift_cents = max(0, pool_cents - ton_cents)
    fee_cents = arena_fee_cents(ton_cents)
    prize_gifts = [dict(name=str(g.get('name') or 'Подарок'), image_url=str(g.get('image_url') or ''),
                        price=int(g.get('price') or 0) / 100, owner=str(g.get('_owner') or ''))
                   for g in arena_prize_gifts(players)]
    return dict(
        available=game_available('arena'),
        now=now,
        betting_ms=ARENA_BETTING_MS,
        result_ms=ARENA_RESULT_MS,
        extend_ms=ARENA_EXTEND_MS,
        snipe_window_ms=ARENA_SNIPE_WINDOW_MS,
        fee_percent=ARENA_FEE_PERCENT,
        min_bet=MIN_BET_CENTS / 100,
        max_bet=MAX_BET_CENTS / 100,
        round=dict(
            id=int(row['id']),
            extended=bool(int(row['extended'] or 0)),
            state=row['state'],
            open_at=int(row['open_at'] or 0),
            close_at=int(row['close_at'] or 0),
            settled_at=int(row['settled_at'] or 0),
            total_pool=pool_cents / 100,
            ton_pool=ton_cents / 100,
            gift_pool=gift_cents / 100,
            fee=fee_cents / 100,
            payout=(ton_cents - fee_cents) / 100,   # TON that lands on the balance
            prize_gifts=prize_gifts,                # gifts that go to the winner (never taxed)
            winner_user_id=winner_id,
            fairness=fairness_view(db, 'arena', row['id'], row['state'] == 'settled'),
        ),
        players=result,
        winner=winner,
        my_bet=mine,
        mine_result=('won' if winner_id == int(uid) else 'lost') if winner_id and mine else None,
        recent=arena_recent_rounds(db, 6),
        balance=profile()['balance'],
    )


@app.get('/api/arena/state')
@login_required
def arena_state():
    if creator_demo_active(session['uid']):
        return jsonify(demo_arena_state_payload(session['uid'], int(time.time() * 1000)))
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        now_ms = int(time.time() * 1000)
        arena_advance(db, now_ms)
        try:
            arena_bot_tick(db, now_ms)
        except Exception:
            app.logger.exception('Arena bot tick failed')
        db.commit()
        return jsonify(arena_state_payload(db, session['uid'], int(time.time() * 1000)))
    finally:
        db.close()


@app.post('/api/arena/bet')
@login_required
def arena_bet():
    data = request.get_json(silent=True) or {}
    if creator_demo_active(session['uid']):
        try:
            return jsonify(ok=True, state=demo_arena_bet(data), user=profile())
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
    gift_mode = data.get('inventory_id') not in (None, '')
    amount = 0
    inventory_id = 0
    if gift_mode:
        try:
            inventory_id = int(data.get('inventory_id'))
        except (TypeError, ValueError):
            return error('Подарок не найден.')
    else:
        try:
            amount = parse_amount(data.get('bet'))
        except (ValueError, InvalidOperation, TypeError):
            return error('Укажите корректную ставку.')
        if not (MIN_BET_CENTS <= amount <= MAX_BET_CENTS):
            return error('Ставка от 0.10 до 300 TON.')
    uid = session['uid']
    now = int(time.time() * 1000)
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = arena_advance(db, now)
        close_at = int(row['close_at'] or 0)
        if row['state'] != 'open' or (close_at > 0 and now >= close_at):
            db.rollback()
            return error('Приём ставок закрыт. Дождитесь следующей арены.', 409)
        item = None
        if gift_mode:
            purge_expired_inventory(db, uid)
            item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?', (inventory_id, uid)).fetchone()
            if not item:
                db.rollback()
                return error('Подарок не найден в инвентаре.', 404)
            if int(item['deposit_mirror'] or 0):
                db.rollback()
                return error('NFT-пополнение уже зачислено на баланс и недоступно для ставки.', 409)
            if item['promo_locked']:
                db.rollback()
                return error('Отыгрышные подарки в арене недоступны.', 409)
            amount = int(item['floor_price'] or 0)
            if not (MIN_BET_CENTS <= amount <= MAX_BET_CENTS):
                db.rollback()
                return error('Для ставки подходят подарки стоимостью от 0.10 до 300 TON.', 409)
        existing = db.execute('SELECT amount,gift_amount,gifts FROM arena_bets WHERE round_id=? AND user_id=?',
                              (row['id'], uid)).fetchone()
        existing_amount = int(existing['amount'] or 0) if existing else 0
        if existing_amount + amount > MAX_BET_CENTS:
            db.rollback()
            return error('Общая ставка в арене не может превышать 300 TON.', 409)
        if gift_mode:
            if not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (inventory_id, uid)).rowcount:
                db.rollback()
                return error('Подарок уже используется.', 409)
            snapshot = dict(name=str(item['gift_name'] or 'Подарок')[:140], image_url=str(item['image_url'] or ''),
                            price=amount, xp=bool(gift_counts_for_xp(item)), row=dict(item))
        else:
            if DATABASE_URL:
                db.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (uid,))
            if not db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?',
                              (amount, uid, amount)).rowcount:
                db.rollback()
                return error('Недостаточно TON.', 409)
        gifts = arena_gift_list(existing['gifts']) if existing else []
        gift_amount = int(existing['gift_amount'] or 0) if existing else 0
        if gift_mode:
            gifts.append(snapshot)
            gift_amount += amount
        gifts_json = json.dumps(gifts, ensure_ascii=False, default=str)
        if existing:
            db.execute('UPDATE arena_bets SET amount=amount+?,gift_amount=?,gifts=? WHERE round_id=? AND user_id=?',
                       (amount, gift_amount, gifts_json, row['id'], uid))
        else:
            db.execute('INSERT INTO arena_bets(round_id,user_id,amount,gift_amount,gifts) VALUES(?,?,?,?,?)',
                       (row['id'], uid, amount, gift_amount, gifts_json))
        if close_at <= 0:
            # The very first bet starts the round: only now the countdown begins.
            db.execute("UPDATE arena_rounds SET open_at=?,close_at=? WHERE id=? AND state='open'",
                       (now, now + ARENA_BETTING_MS, row['id']))
        elif (not existing and not int(row['extended'] or 0)
              and 0 < close_at - now <= ARENA_SNIPE_WINDOW_MS):
            # A new player joined on the very last second: give everyone a bit more time, once per round.
            db.execute("UPDATE arena_rounds SET close_at=close_at+?,extended=1 WHERE id=? AND state='open'",
                       (ARENA_EXTEND_MS, row['id']))
        if gift_mode:
            record_transaction(db, uid, 'arena_gift_bet', 0, 'arena_round', row['id'],
                               f'Arena #{row["id"]}: {snapshot["name"]} ({amount/100:.2f} TON)')
            if snapshot['xp']:
                increase_turnover(db, uid, amount, withdrawal_wager=False)
        else:
            record_transaction(db, uid, 'arena_bet', -amount, 'arena_round', row['id'],
                               f'Arena #{row["id"]}: {amount/100:.2f} TON')
            increase_turnover(db, uid, amount)
        db.commit()
        return jsonify(ok=True, state=arena_state_payload(db, uid, now), user=profile())
    finally:
        db.close()



# ===================== Arena house bot (@gemdrop_adm) =====================
# A player called @gemdrop_adm takes part in the Arena like everybody else: it STARTS rounds by itself
# and joins nearly every round with a random TON stake or a random catalog gift worth 0.10-15 TON.
# Nothing marks it as a bot and it never sends any message. It is driven by the Arena state polling
# (so it works on every worker, with no background job) and only acts while someone has the Arena open.
# It plays with house money: its stake is not taken from any balance/inventory. If it wins it is paid
# only the real players' part of the pool; if it loses, the winner receives its stake.
ARENA_BOT_USERNAME = (os.environ.get('ARENA_BOT_USERNAME') or 'gemdrop_adm').strip().lstrip('@').lower()
ARENA_BOT_FALLBACK_ID = 9000000001          # used only when that Telegram account never opened the app
ARENA_BOT_MIN_CENTS = 10
ARENA_BOT_MAX_CENTS = 100                  # opening stake never above 1 TON (log-uniform, mostly 0.1-0.5)
ARENA_BOT_TOPUP_CAP_CENTS = 1500           # whole bot stake in a round never above 15 TON
ARENA_BOT_TOPUP_MIN_SHARE = 0.3            # tops up only when the gap is at least 30% of its current stake
ARENA_BOT_ROUND_CHANCE = 90                 # percent of rounds the bot takes part in
ARENA_BOT_GIFT_CHANCE = 45                  # percent of its stakes made with a gift
_arena_bot = dict(uid=0)


def arena_bot_uid(db):
    """Id of the bot player. Uses the real @gemdrop_adm account when it exists, otherwise creates one."""
    if _arena_bot['uid']:
        return _arena_bot['uid']
    row = db.execute('SELECT id FROM users WHERE LOWER(username)=?', (ARENA_BOT_USERNAME,)).fetchone()
    if not row and (os.environ.get('ARENA_BOT_USER_ID') or '').isdigit():
        row = db.execute('SELECT id FROM users WHERE id=?', (int(os.environ['ARENA_BOT_USER_ID']),)).fetchone()
    if not row:
        row = db.execute('SELECT id FROM users WHERE id=?', (ARENA_BOT_FALLBACK_ID,)).fetchone()
    if not row:
        db.execute('INSERT OR IGNORE INTO users(id,name,username) VALUES(?,?,?)',
                   (ARENA_BOT_FALLBACK_ID, 'GemDrop', ARENA_BOT_USERNAME))
        row = db.execute('SELECT id FROM users WHERE id=?', (ARENA_BOT_FALLBACK_ID,)).fetchone()
    if row:
        _arena_bot['uid'] = int(row['id'])
    return _arena_bot['uid']


def arena_bot_roll_cents(h):
    # Log-uniform: small stakes are common, 10-15 TON are rare.
    value = int(ARENA_BOT_MIN_CENTS * (ARENA_BOT_MAX_CENTS / ARENA_BOT_MIN_CENTS) ** ((h % 10 ** 6) / 10 ** 6))
    return value if value < 100 else value // 5 * 5


def arena_bot_random_gift(h, lo=None, hi=None):
    """A random catalog gift priced lo..hi cents (default 0.10 TON .. opening cap). None when nothing fits."""
    lo = ARENA_BOT_MIN_CENTS if lo is None else int(lo)
    hi = ARENA_BOT_MAX_CENTS if hi is None else int(hi)
    try:
        pool = []
        for g in read_catalog().get('gifts', []):
            price = ton_to_cents(g.get('price_ton') or 0)
            if lo <= price <= hi and g.get('name') and safe_image(g.get('image_url')):
                pool.append((price, g))
    except Exception:
        return None
    if not pool:
        return None
    price, g = pool[h % len(pool)]
    return dict(name=str(g['name'])[:140], image_url=safe_image(g.get('image_url')), price=price, xp=False, house=True)


def arena_bot_dice(round_id, started):
    """Per-round random numbers that every worker computes identically (no shared state needed)."""
    key = app.secret_key if isinstance(app.secret_key, bytes) else str(app.secret_key).encode()
    digest = hmac.new(key, f'arena-bot:{int(round_id)}:{int(bool(started))}'.encode(), hashlib.sha256).digest()
    return [int.from_bytes(digest[i:i + 4], 'big') for i in range(0, 20, 4)]


def arena_bot_ts(value):
    """created_at text (UTC) -> ms; None when it cannot be parsed."""
    try:
        dt = datetime.strptime(str(value)[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def arena_bot_topup(db, row, uid, now, d, mine):
    """The bot already bet this round. If a real player came with a clearly bigger stake, it adds a gift or
    TON to its own stake (60-100% of the biggest real stake, never above the cap), after a human-like pause."""
    close_at = int(row['close_at'] or 0)
    if close_at <= 0 or now >= close_at - ARENA_SNIPE_WINDOW_MS - 500:
        return False
    others = db.execute('SELECT amount,created_at FROM arena_bets WHERE round_id=? AND user_id<>?',
                        (row['id'], uid)).fetchall()
    if not others:
        return False
    biggest = max(int(o['amount'] or 0) for o in others)
    target = min(ARENA_BOT_TOPUP_CAP_CENTS, int(biggest * (0.6 + (d[4] % 41) / 100)))
    have = int(mine['amount'] or 0)
    gap = target - have
    if gap < max(ARENA_BOT_MIN_CENTS * 2, int(have * ARENA_BOT_TOPUP_MIN_SHARE)):
        return False
    stamps = [t for t in (arena_bot_ts(o['created_at']) for o in others) if t]
    if stamps and now < max(stamps) + 1200 + d[3] % 2600:
        return False
    gift = None
    if (d[4] // 100) % 100 < ARENA_BOT_GIFT_CHANCE:
        gift = arena_bot_random_gift(d[2], lo=max(ARENA_BOT_MIN_CENTS, int(gap * 0.6)), hi=gap)
    gifts = arena_gift_list(mine['gifts'])
    gift_amount = int(mine['gift_amount'] or 0)
    if gift:
        add = int(gift['price'])
        gifts.append(gift)
        gift_amount += add
    else:
        add = gap if gap < 100 else gap // 5 * 5
    db.execute('UPDATE arena_bets SET amount=amount+?,gift_amount=?,gifts=? WHERE round_id=? AND user_id=?',
               (add, gift_amount, json.dumps(gifts, ensure_ascii=False, default=str), row['id'], uid))
    return True


def arena_bot_tick(db, now):
    """Call inside an open transaction, right after arena_advance. Returns True if the bot bet."""
    row = arena_latest(db)
    if not row or row['state'] != 'open':
        return False
    uid = arena_bot_uid(db)
    if not uid:
        return False
    started = int(row['close_at'] or 0) > 0
    d = arena_bot_dice(row['id'], started)
    if d[0] % 100 >= ARENA_BOT_ROUND_CHANCE:
        return False
    mine = db.execute('SELECT amount,gift_amount,gifts FROM arena_bets WHERE round_id=? AND user_id=?',
                      (row['id'], uid)).fetchone()
    if mine:
        return arena_bot_topup(db, row, uid, now, d, mine)
    if started:
        span = max(1000, ARENA_BETTING_MS - ARENA_SNIPE_WINDOW_MS - 3500)
        due = int(row['open_at'] or now) + 1200 + d[1] % span
        if now >= int(row['close_at']) - ARENA_SNIPE_WINDOW_MS - 400:
            return False
    else:
        due = int(row['open_at'] or now) + 2500 + d[1] % 6000
    if now < due:
        return False
    if db.execute('SELECT 1 FROM arena_bets WHERE round_id=? AND user_id=?', (row['id'], uid)).fetchone():
        return False
    gift = arena_bot_random_gift(d[2]) if d[3] % 100 < ARENA_BOT_GIFT_CHANCE else None
    if gift:
        amount, gift_amount, gifts = int(gift['price']), int(gift['price']), [gift]
    else:
        amount, gift_amount, gifts = arena_bot_roll_cents(d[2]), 0, []
    db.execute('INSERT INTO arena_bets(round_id,user_id,amount,gift_amount,gifts) VALUES(?,?,?,?,?)',
               (row['id'], uid, amount, gift_amount, json.dumps(gifts, ensure_ascii=False, default=str)))
    if not started:
        db.execute("UPDATE arena_rounds SET open_at=?,close_at=? WHERE id=? AND state='open'",
                   (now, now + ARENA_BETTING_MS, row['id']))
    return True


# ================================== Crash ==================================
CRASH_BETTING_MS = 5000      # countdown 5..1, bets are accepted
CRASH_BOOM_MS = 3000         # boom.gif is shown after the crash
CRASH_GROWTH = 0.08          # multiplier = e^(0.08 * seconds)
CRASH_MIN_FLIGHT_MS = 700
CRASH_MAX_X100 = 1000000     # 10000x ceiling
CRASH_PROMO_MIN_X100 = 120   # wager gifts count only when cashed out at >= 1.20x (no free 1.00x grinding)
CRASH_RTP_DEFAULT = 0.89


def crash_rtp():
    """One general RTP for Crash: EV of any cash-out target equals this value."""
    try:
        doc = read_document('game_settings') or {}
        value = float(doc.get('crash_rtp', CRASH_RTP_DEFAULT))
    except (TypeError, ValueError, OSError, json.JSONDecodeError):
        value = CRASH_RTP_DEFAULT
    return min(0.999, max(0.80, value))


def crash_ms():
    return int(time.time() * 1000)


def crash_roll_x100(rtp, ticket=None):
    """P(crash >= x) = rtp / x for x >= 1, using a verifiable integer ticket."""
    ticket = secrets.randbelow(10 ** 9) if ticket is None else max(0, min(10 ** 9 - 1, int(ticket)))
    u = ticket / 10 ** 9
    x = rtp / (1.0 - u)
    return int(max(100, min(CRASH_MAX_X100, math.floor(x * 100))))


def crash_flight_ms(crash_x100):
    if crash_x100 <= 100:
        return CRASH_MIN_FLIGHT_MS
    return max(CRASH_MIN_FLIGHT_MS, int(math.log(crash_x100 / 100.0) / CRASH_GROWTH * 1000))


def crash_mult_x100(row, now):
    if now < row['launch_at']:
        return 100
    value = int(math.floor(100 * math.exp(CRASH_GROWTH * (now - row['launch_at']) / 1000.0)))
    return min(max(100, value), int(row['crash_x100']))


def crash_new_round(db, round_id, open_at):
    rtp = crash_rtp()
    proof = fairness_make('crash', 0, f'crash:{round_id}', nonce=round_id)
    ticket, fair_cursor, _ = fairness_draw(proof, 10 ** 9, 0)
    crash_x100 = crash_roll_x100(rtp, ticket)
    launch_at = open_at + CRASH_BETTING_MS
    inserted = db.execute('INSERT OR IGNORE INTO crash_rounds(id,crash_x100,rtp_snapshot,open_at,launch_at,crash_at,state) VALUES(?,?,?,?,?,?,?)',
                          (round_id, crash_x100, rtp, open_at, launch_at,
                           launch_at + crash_flight_ms(crash_x100), 'open'))
    if inserted.rowcount and not fairness_get(db, game='crash', game_ref=str(round_id)):
        fairness_store(db, proof, str(round_id), fair_cursor,
                       {'ticket': ticket, 'upper': 10 ** 9, 'crash_x100': crash_x100, 'rtp': rtp})


def crash_promo_reinsert(db, bet, round_id, progress, attempts_remaining, completed):
    """Put a wager gift back into the inventory with its updated progress / lives."""
    cursor = db.execute("""INSERT INTO inventory(
                            user_id,gift_id,gift_name,image_url,floor_price,source,round_id,
                            promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress,promo_code,expires_at,
                            promo_attempts_total,promo_attempts_remaining,promo_burn_on_loss,external_url,promo_unlock_payload)
                          VALUES(?,?,?,?,?,'promo_wager',?,1,?,?,?,?,?,?,?,?,?,?)""",
                        (bet['user_id'], bet['bet_gift_id'], bet['bet_gift_name'], bet['bet_gift_image'], bet['bet'],
                         round_id, float(bet['promo_wager_multiplier'] or 0), int(bet['promo_wager_target'] or 0),
                         progress, bet['promo_code'] or '', None if completed else bet['bet_expires_at'],
                         max(1, int(bet['promo_attempts_total'] or 1)), attempts_remaining,
                         int(bool(bet['promo_burn_on_loss'])), bet['bet_external_url'] or '', '{}'))
    return cursor.lastrowid


def crash_promo_win(db, bet, round_id, mult_x100):
    """A wager gift was cashed out: the payout is added to the wager progress instead of the balance."""
    amount = int(bet['bet']) * int(mult_x100) // 100
    target = max(0, int(bet['promo_wager_target'] or 0))
    previous = max(0, int(bet['promo_wager_progress'] or 0))
    progress = min(target, previous + amount) if target else previous + amount
    completed = bool(target and progress >= target)
    inv_id = crash_promo_reinsert(db, bet, round_id, progress,
                                  max(0, int(bet['promo_attempts_remaining'] or 1)), completed)
    db.execute('UPDATE crash_bets SET prize_inventory_id=?,promo_progress_after=? WHERE round_id=? AND user_id=?',
               (inv_id, progress, round_id, bet['user_id']))
    detail = (f'Отыгрыш набран — разблокируйте подарок в профиле: {progress/100:.2f}/{target/100:.2f} TON'
              if completed else
              f'Отыгрыш {bet["bet_gift_name"]}: {progress/100:.2f}/{target/100:.2f} TON')
    record_transaction(db, bet['user_id'], 'promo_wager_progress', amount, 'crash_round', round_id, detail)
    return amount, progress, completed


def crash_promo_loss(db, bet, round_id):
    """A wager gift lost its round: one life is spent, the gift burns when lives run out."""
    attempts_before = max(1, int(bet['promo_attempts_remaining'] or 1))
    burns = bool(bet['promo_burn_on_loss'])
    attempts_after = attempts_before - 1 if burns else attempts_before
    if attempts_after > 0:
        inv_id = crash_promo_reinsert(db, bet, round_id, max(0, int(bet['promo_wager_progress'] or 0)),
                                      attempts_after, False)
        db.execute('UPDATE crash_bets SET prize_inventory_id=?,promo_progress_after=? WHERE round_id=? AND user_id=?',
                   (inv_id, int(bet['promo_wager_progress'] or 0), round_id, bet['user_id']))
        record_transaction(db, bet['user_id'], 'promo_wager_attempt_lost', 0, 'crash_round', round_id,
                           f'{bet["bet_gift_name"]}: осталось жизней {attempts_after}')
    else:
        record_transaction(db, bet['user_id'], 'promo_wager_burn', 0, 'crash_round', round_id,
                           f'Сгорел промо-подарок: {bet["bet_gift_name"]}')


def crash_settle_round(db, row):
    """Close a finished round exactly once and pay auto cash-outs (always in TON)."""
    claimed = db.execute("UPDATE crash_rounds SET state='crashed' WHERE id=? AND state='open'", (row['id'],))
    if not claimed.rowcount:
        return
    fairness_mark_settled(db, 'crash', row['id'])
    bets = db.execute("SELECT * FROM crash_bets WHERE round_id=? AND state='active'", (row['id'],)).fetchall()
    for bet in bets:
        auto = int(bet['auto_x100'] or 0)
        is_promo = (bet['bet_type'] or 'ton') == 'promo_gift'
        if auto >= 101 and auto <= int(row['crash_x100']):
            payout = int(bet['bet']) * auto // 100
            moved = db.execute("UPDATE crash_bets SET state='won',cashout_x100=?,payout=? WHERE round_id=? AND user_id=? AND state='active'",
                               (auto, 0 if is_promo else payout, row['id'], bet['user_id']))
            if moved.rowcount:
                if is_promo:
                    crash_promo_win(db, bet, row['id'], auto)
                else:
                    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, bet['user_id']))
                    record_transaction(db, bet['user_id'], 'crash_win', payout, 'crash_round', row['id'],
                                       f'Crash x{auto/100:.2f} (авто)')
        else:
            lost = db.execute("UPDATE crash_bets SET state='lost' WHERE round_id=? AND user_id=? AND state='active'",
                              (row['id'], bet['user_id']))
            if lost.rowcount and (bet['bet_type'] or 'ton') == 'gift':
                record_transaction(db, bet['user_id'], 'crash_gift_lost', 0, 'crash_round', row['id'],
                                   f'Проигран подарок: {bet["bet_gift_name"]}')
            elif lost.rowcount and is_promo:
                crash_promo_loss(db, bet, row['id'])


def crash_latest(db):
    return db.execute('SELECT * FROM crash_rounds ORDER BY id DESC LIMIT 1').fetchone()


def crash_needs_advance(row, now):
    if not row:
        return True
    if row['state'] == 'open' and now >= row['crash_at']:
        return True
    return row['state'] == 'crashed' and now >= row['crash_at'] + CRASH_BOOM_MS


def crash_advance(db, now=None):
    """Caller owns the transaction (BEGIN IMMEDIATE). Settles the finished round and opens the next one."""
    now = crash_ms() if now is None else now
    row = crash_latest(db)
    if row and row['state'] == 'open' and now >= row['crash_at']:
        crash_settle_round(db, row)
        row = crash_latest(db)
    if not row:
        crash_new_round(db, 1, now)
    elif row['state'] == 'crashed' and now >= row['crash_at'] + CRASH_BOOM_MS:
        opened = row['crash_at'] + CRASH_BOOM_MS
        if now - opened > 20000:      # server was idle: do not replay missed time
            opened = now
        crash_new_round(db, int(row['id']) + 1, opened)
    return crash_latest(db)


def crash_phase(row, now):
    if row['state'] == 'crashed' or now >= row['crash_at']:
        return 'crashed'
    return 'betting' if now < row['launch_at'] else 'flying'


def crash_gift_view(name, image, price_cents):
    image = str(image or '')
    if not (image.startswith('https://') or image.startswith('/static/')) or len(image) > 1000:
        image = ''
    return dict(name=name or '', image_url=image, price_ton=(price_cents or 0) / 100)


def crash_bet_gift_view(row):
    """Gift used as the stake (plain or wager gift) with the wager details the UI needs."""
    view = crash_gift_view(row['bet_gift_name'], row['bet_gift_image'], row['bet'])
    if (row['bet_type'] or 'ton') == 'promo_gift':
        view.update(promo_locked=True, wager_multiplier=float(row['promo_wager_multiplier'] or 0),
                    wager_target=int(row['promo_wager_target'] or 0) / 100,
                    wager_progress=int(row['promo_wager_progress'] or 0) / 100,
                    wager_progress_after=int(row['promo_progress_after'] or 0) / 100,
                    wager_attempts_remaining=max(0, int(row['promo_attempts_remaining'] or 1)),
                    wager_burn_on_loss=bool(row['promo_burn_on_loss']))
    return view


def crash_user_view(row):
    if not row:
        return None
    is_gift = (row['bet_type'] or 'ton') in ('gift', 'promo_gift')
    is_promo = (row['bet_type'] or 'ton') == 'promo_gift'
    prize = None
    if row['prize_name']:
        prize = crash_gift_view(row['prize_name'], row['prize_image'], row['prize_price'])
    return dict(bet=row['bet'] / 100, auto=(row['auto_x100'] or 0) / 100, state=row['state'],
                cashout=(row['cashout_x100'] or 0) / 100, payout=(row['payout'] or 0) / 100,
                bet_type=(row['bet_type'] if is_gift else 'ton'), promo=is_promo, promo_min=CRASH_PROMO_MIN_X100 / 100,
                bet_gift=crash_bet_gift_view(row) if is_gift else None,
                prize=prize)


def crash_min_prize_cents():
    """Cheapest catalog gift that Mines/Crash may hand out as a prize (0 = no catalog)."""
    try:
        gifts = read_catalog()['gifts']
    except (OSError, ValueError, KeyError):
        return 0
    prices = []
    for gift in gifts:
        try:
            if not gift.get('id') or not gift.get('name') or not gift.get('image_match'):
                continue
            price = ton_to_cents(gift['price_ton'])
            if price > 0:
                prices.append(price)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return min(prices) if prices else 0


def crash_prize_preview(amount_cents, gifts=None):
    """The gift a player would receive for this cash-out (Mines rules: best gift not above the amount)."""
    prize = prize_for(int(amount_cents), gifts)
    if not prize:
        return None
    price = ton_to_cents(prize['price_ton'])
    return dict(id=str(prize['id']), name=str(prize['name']), image_url=safe_image(prize.get('image_url')),
                price_ton=price / 100, price_cents=price)


def crash_state_payload(db, uid, now):
    row = crash_latest(db)
    phase = crash_phase(row, now)
    payload = dict(now=now, phase=phase,
                   round=dict(id=row['id'], open_at=row['open_at'], launch_at=row['launch_at'],
                              fairness=fairness_view(db, 'crash', row['id'], phase == 'crashed')),
                   growth=CRASH_GROWTH, boom_ms=CRASH_BOOM_MS, betting_ms=CRASH_BETTING_MS,
                   min_bet=MIN_BET_CENTS / 100, max_bet=MAX_BET_CENTS / 100,
                   min_nft=crash_min_prize_cents() / 100, available=game_available('crash'))
    if phase == 'crashed':
        payload['round']['crash'] = row['crash_x100'] / 100
        payload['round']['crash_at'] = row['crash_at']
    mine = db.execute('SELECT * FROM crash_bets WHERE round_id=? AND user_id=?', (row['id'], uid)).fetchone()
    payload['my_bet'] = crash_user_view(mine)
    rows = db.execute('SELECT b.user_id,b.bet,b.state,b.cashout_x100,b.payout,b.bet_type,b.bet_gift_name,b.bet_gift_image,'
                      'b.prize_name,b.prize_image,b.prize_price,u.name,u.photo_url '
                      'FROM crash_bets b JOIN users u ON u.id=b.user_id '
                      'WHERE b.round_id=? ORDER BY b.bet DESC LIMIT 40', (row['id'],)).fetchall()
    bets = []
    for r in rows:
        is_gift = (r['bet_type'] or 'ton') in ('gift', 'promo_gift')
        bets.append(dict(user_id=r['user_id'], name=r['name'], photo_url=r['photo_url'], bet=r['bet'] / 100,
                         bet_type=(r['bet_type'] if is_gift else 'ton'), promo=(r['bet_type'] or 'ton') == 'promo_gift',
                         bet_gift=crash_gift_view(r['bet_gift_name'], r['bet_gift_image'], r['bet']) if is_gift else None,
                         state=('active' if phase != 'crashed' and r['state'] == 'active' else r['state']),
                         cashout=(r['cashout_x100'] or 0) / 100, payout=(r['payout'] or 0) / 100,
                         prize=(crash_gift_view(r['prize_name'], r['prize_image'], r['prize_price'])
                                if r['prize_name'] else None)))
    payload['bets'] = bets
    hist = db.execute("SELECT crash_x100 FROM crash_rounds WHERE state='crashed' ORDER BY id DESC LIMIT 24").fetchall()
    payload['history'] = [h['crash_x100'] / 100 for h in hist]
    me = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    payload['balance'] = (me['balance'] / 100) if me else 0
    return payload


@app.get('/api/crash/state')
@login_required
def crash_state():
    uid = session['uid']
    if creator_demo_active(uid):
        return jsonify(demo_crash_state_payload())
    db = connect()
    try:
        now = crash_ms()
        if crash_needs_advance(crash_latest(db), now):
            db.execute('BEGIN IMMEDIATE')
            crash_advance(db, now)
            db.commit()
        return jsonify(crash_state_payload(db, uid, crash_ms()))
    finally:
        db.close()


@app.post('/api/crash/bet')
@login_required
def crash_bet():
    data = request.get_json(silent=True) or {}
    if creator_demo_active(session['uid']):
        try:
            state = demo_crash_bet(data)
            return jsonify(ok=True, state=state, user=profile(), new_level=None)
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
    inventory_id = data.get('inventory_id')
    try:
        inventory_id = int(inventory_id) if inventory_id not in (None, '') else None
    except (TypeError, ValueError):
        return error('Некорректный подарок для ставки.')
    bet = 0
    if inventory_id is None:
        try:
            bet = parse_amount(data.get('bet'))
        except (ValueError, InvalidOperation, TypeError):
            return error('Укажите корректную ставку.')
        if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
            return error('Ставка от 0.10 до 300 TON.')
    auto = 0
    if data.get('auto') not in (None, '', 0, '0'):
        try:
            auto = int(round(float(data.get('auto')) * 100))
        except (TypeError, ValueError):
            return error('Некорректный авто-вывод.')
        if not (101 <= auto <= CRASH_MAX_X100):
            return error('Авто-вывод: от 1.01x до 10000x.')
    uid = session['uid']
    db = connect()
    new_level = None
    try:
        db.execute('BEGIN IMMEDIATE')
        now = crash_ms()
        row = crash_advance(db, now)
        if crash_phase(row, now) != 'betting':
            db.rollback()
            return error('Приём ставок закрыт. Дождитесь следующего раунда.', 409)
        if db.execute('SELECT 1 FROM crash_bets WHERE round_id=? AND user_id=?', (row['id'], uid)).fetchone():
            db.rollback()
            return error('Ставка на этот раунд уже сделана.', 409)
        if DATABASE_URL:
            db.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (uid,))
        if inventory_id is not None:
            purge_expired_inventory(db, uid)
            lock = ' FOR UPDATE' if DATABASE_URL else ''
            item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?' + lock, (inventory_id, uid)).fetchone()
            if not item:
                db.rollback()
                return error('Подарок не найден в инвентаре.', 404)
            bet = int(item['floor_price'] or 0)
            if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
                db.rollback()
                return error('Для ставки подходят подарки стоимостью от 0.10 до 300 TON.')
            is_promo = bool(item['promo_locked'])
            target = int(item['promo_wager_target'] or 0)
            progress = int(item['promo_wager_progress'] or 0)
            if is_promo and target > 0 and progress >= target:
                db.rollback()
                return error('Отыгрыш уже завершён. Сначала разблокируйте подарок в профиле.')
            if is_promo and auto and auto < CRASH_PROMO_MIN_X100:
                db.rollback()
                return error('Для отыгрышного подарка авто-вывод — от %.2fx.' % (CRASH_PROMO_MIN_X100 / 100))
            xp_allowed = gift_counts_for_xp(item)
            if not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (inventory_id, uid)).rowcount:
                db.rollback()
                return error('Подарок уже используется.', 409)
            if is_promo:
                db.execute("""INSERT INTO crash_bets(round_id,user_id,bet,auto_x100,bet_type,bet_inventory_id,
                              bet_gift_id,bet_gift_name,bet_gift_image,promo_wager_multiplier,promo_wager_target,
                              promo_wager_progress,promo_code,bet_expires_at,promo_attempts_total,
                              promo_attempts_remaining,promo_burn_on_loss,bet_external_url)
                              VALUES(?,?,?,?,'promo_gift',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (row['id'], uid, bet, auto, inventory_id, str(item['gift_id'] or ''),
                            str(item['gift_name'] or '')[:140], str(item['image_url'] or ''),
                            float(item['promo_wager_multiplier'] or 0), target, progress, item['promo_code'] or '',
                            item['expires_at'], max(1, int(item['promo_attempts_total'] or 1)),
                            max(0, int(item['promo_attempts_remaining'] or 1)),
                            int(bool(item['promo_burn_on_loss'])), item['external_url'] or ''))
                record_transaction(db, uid, 'promo_wager_bet', 0, 'crash_round', row['id'],
                                   f'{item["gift_name"]} · X{float(item["promo_wager_multiplier"] or 0):g}')
                # Wager gifts do not count toward turnover / levels (same as Mines).
            else:
                db.execute("""INSERT INTO crash_bets(round_id,user_id,bet,auto_x100,bet_type,bet_inventory_id,
                              bet_gift_id,bet_gift_name,bet_gift_image) VALUES(?,?,?,?,'gift',?,?,?,?)""",
                           (row['id'], uid, bet, auto, inventory_id, str(item['gift_id'] or ''),
                            str(item['gift_name'] or '')[:140], str(item['image_url'] or '')))
                record_transaction(db, uid, 'crash_gift_bet', 0, 'crash_round', row['id'], str(item['gift_name'] or '')[:140])
                if xp_allowed:
                    new_level = increase_turnover(db, uid, bet, withdrawal_wager=False)
        else:
            updated = db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?', (bet, uid, bet))
            if not updated.rowcount:
                db.rollback()
                return error('Недостаточно средств.')
            db.execute('INSERT INTO crash_bets(round_id,user_id,bet,auto_x100) VALUES(?,?,?,?)', (row['id'], uid, bet, auto))
            record_transaction(db, uid, 'crash_bet', -bet, 'crash_round', row['id'], 'Crash')
            new_level = increase_turnover(db, uid, bet)
        db.commit()
    finally:
        db.close()
    if new_level:
        notify_level_up_async(uid, new_level)
    db = connect()
    try:
        return jsonify(ok=True, state=crash_state_payload(db, uid, crash_ms()), user=profile(), new_level=new_level)
    finally:
        db.close()


@app.get('/api/crash/prize')
@login_required
def crash_prize():
    """Preview of the NFT a player can take for the current cash-out amount."""
    try:
        amount = int(round(float(request.args.get('amount', '0')) * 100))
    except (TypeError, ValueError):
        return error('Неверная сумма.')
    prize = crash_prize_preview(max(0, amount))
    return jsonify(prize=prize, min_nft=crash_min_prize_cents() / 100)


@app.post('/api/crash/cashout')
@login_required
def crash_cashout():
    uid = session['uid']
    data = request.get_json(silent=True) or {}
    if creator_demo_active(uid):
        try:
            return jsonify(**demo_crash_cashout(data))
        except (ValueError, InvalidOperation, TypeError) as exc:
            return error(str(exc), 409)
    want_gift = bool(data.get('gift'))
    prize = None
    remainder = 0
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        now = crash_ms()
        row = crash_advance(db, now)
        mine = db.execute('SELECT * FROM crash_bets WHERE round_id=? AND user_id=?', (row['id'], uid)).fetchone()
        if not mine or mine['state'] != 'active':
            db.rollback()
            return error('Нет активной ставки.', 409)
        if crash_phase(row, now) != 'flying':
            db.rollback()
            return error('Ракета уже взорвалась.' if crash_phase(row, now) == 'crashed' else 'Раунд ещё не стартовал.', 409)
        mult = crash_mult_x100(row, now)
        if mult >= int(row['crash_x100']):
            db.rollback()
            return error('Ракета уже взорвалась.', 409)
        payout = int(mine['bet']) * mult // 100
        is_promo = (mine['bet_type'] or 'ton') == 'promo_gift'
        if is_promo and mult < CRASH_PROMO_MIN_X100:
            db.rollback()
            return error('Отыгрышный подарок можно забрать от x%.2f.' % (CRASH_PROMO_MIN_X100 / 100), 409)
        if is_promo:
            moved = db.execute("UPDATE crash_bets SET state='won',cashout_x100=?,payout=0 WHERE round_id=? AND user_id=? AND state='active'",
                               (mult, row['id'], uid))
            if not moved.rowcount:
                db.rollback()
                return error('Ставка уже закрыта.', 409)
            amount, progress, completed = crash_promo_win(db, mine, row['id'], mult)
            db.commit()
            promo = dict(amount=amount / 100, progress=progress / 100, target=int(mine['promo_wager_target'] or 0) / 100,
                         completed=completed, gift=dict(name=mine['bet_gift_name'], image_url=mine['bet_gift_image']))
            return jsonify(ok=True, multiplier=mult / 100, payout=0, prize=None, remainder=0, promo=promo,
                           state=crash_state_payload(db, uid, crash_ms()), user=profile())
        prize_info = None
        if want_gift:
            prize_info = crash_prize_preview(payout)
            if not prize_info:
                db.rollback()
                return error('Сумма ещё ниже самого дешёвого подарка — заберите TON.', 409)
        if prize_info:
            remainder = max(0, payout - prize_info['price_cents'])
            moved = db.execute("""UPDATE crash_bets SET state='won',cashout_x100=?,payout=?,prize_name=?,prize_image=?,prize_price=?
                                  WHERE round_id=? AND user_id=? AND state='active'""",
                               (mult, payout, prize_info['name'][:140], prize_info['image_url'],
                                prize_info['price_cents'], row['id'], uid))
        else:
            moved = db.execute("UPDATE crash_bets SET state='won',cashout_x100=?,payout=? WHERE round_id=? AND user_id=? AND state='active'",
                               (mult, payout, row['id'], uid))
        if not moved.rowcount:
            db.rollback()
            return error('Ставка уже закрыта.', 409)
        if prize_info:
            cursor = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                                   VALUES(?,?,?,?,?,'game',?)""",
                                (uid, prize_info['id'], prize_info['name'], prize_info['image_url'],
                                 prize_info['price_cents'], row['id']))
            db.execute('UPDATE crash_bets SET prize_inventory_id=? WHERE round_id=? AND user_id=?',
                       (cursor.lastrowid, row['id'], uid))
            if remainder:
                db.execute('UPDATE users SET balance=balance+? WHERE id=?', (remainder, uid))
                record_transaction(db, uid, 'crash_win', remainder, 'crash_round', row['id'],
                                   f'Crash x{mult/100:.2f}: остаток после подарка {prize_info["name"]}')
            record_transaction(db, uid, 'crash_gift_win', 0, 'crash_round', row['id'],
                               f'{prize_info["name"]} · x{mult/100:.2f}')
            prize = dict(name=prize_info['name'], image_url=prize_info['image_url'], price_ton=prize_info['price_ton'])
        else:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, uid))
            record_transaction(db, uid, 'crash_win', payout, 'crash_round', row['id'], f'Crash x{mult/100:.2f}')
        db.commit()
    finally:
        db.close()
    db = connect()
    try:
        return jsonify(ok=True, multiplier=mult / 100, payout=payout / 100, prize=prize, remainder=remainder / 100,
                       state=crash_state_payload(db, uid, crash_ms()), user=profile())
    finally:
        db.close()


# ================================== Hi-Lo ==================================
# Cards are Telegram gifts with a rank 1..13. Guess whether the next card is higher or lower;
# equal ranks lose. Every correct guess multiplies the pot by RTP / P(win); cash out at any time
# after the first correct guess. The next card is drawn on the server only when the guess arrives.
HILO_RANKS = 15
HILO_MICRO = 1000000
HILO_RTP_DEFAULT = 0.87
HILO_MIN_STEP_MICRO = 1010000            # one correct guess never pays less than x1.01
HILO_MAX_MULT_MICRO = 200 * HILO_MICRO   # automatic cash-out at x200 ...
HILO_MAX_PAYOUT_CENTS = 100000           # ... or at 1000 TON, whichever comes first


def hilo_rtp():
    try:
        doc = read_document('game_settings') or {}
        value = float(doc.get('hilo_rtp', HILO_RTP_DEFAULT))
    except (TypeError, ValueError, OSError, json.JSONDecodeError):
        value = HILO_RTP_DEFAULT
    return min(0.999, max(0.80, value))


def hilo_wins(rank, direction):
    """How many of the 13 ranks win the guess (a tie always loses)."""
    return (HILO_RANKS - rank) if direction == 'hi' else (rank - 1)


def hilo_step_micro(rank, direction):
    wins = hilo_wins(rank, direction)
    if wins <= 0:
        return 0
    # The next card is never equal to the current one: the win chance is wins / (RANKS - 1).
    return max(HILO_MIN_STEP_MICRO, int(hilo_rtp() * (HILO_RANKS - 1) * HILO_MICRO / wins))


def hilo_pick_card():
    rank = 1 + secrets.randbelow(HILO_RANKS)
    g = hilo_tier_card(rank)
    return rank, str(g.get('name') or '')[:140], g.get('image_url') or ''


def hilo_history(row):
    try:
        data = json.loads(row['history'] or '[]')
    except (TypeError, ValueError):
        data = []
    return data if isinstance(data, list) else []


def hilo_game_view(row):
    if not row:
        return None
    is_gift = (row['bet_type'] or 'ton') == 'gift'
    rank = int(row['cur_rank'])
    bet = int(row['bet'])
    mult_micro = int(row['mult_micro'])
    options = {}
    if row['state'] == 'active':
        for direction in ('hi', 'lo'):
            wins = hilo_wins(rank, direction)
            step = hilo_step_micro(rank, direction)
            options[direction] = dict(wins=wins, chance=round(wins * 100 / (HILO_RANKS - 1), 1), x=step / HILO_MICRO,
                                      payout=(bet * (mult_micro * step // HILO_MICRO) // HILO_MICRO) / 100 if step else 0)
    prize = None
    if row['prize_name']:
        prize = crash_gift_view(row['prize_name'], row['prize_image'], row['prize_price'])
    return dict(id=row['id'], state=row['state'], bet=bet / 100, bet_type='gift' if is_gift else 'ton',
                bet_gift=crash_gift_view(row['bet_gift_name'], row['bet_gift_image'], bet) if is_gift else None,
                rank=rank, card=crash_gift_view(row['card_name'], row['card_image'], 0), steps=int(row['steps']),
                mult=mult_micro / HILO_MICRO, potential=(bet * mult_micro // HILO_MICRO) / 100,
                can_cashout=row['state'] == 'active' and int(row['steps']) >= 1,
                history=hilo_history(row)[-12:], payout=int(row['payout'] or 0) / 100, prize=prize, options=options,
                fairness=fairness_for('hilo', row['id'], row['state'] != 'active'))


def hilo_state_payload(db, uid):
    row = db.execute('SELECT * FROM hilo_games WHERE user_id=? ORDER BY id DESC LIMIT 1', (uid,)).fetchone()
    me = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    return dict(game=hilo_game_view(row), ranks=HILO_RANKS, rtp=hilo_rtp(),
                min_bet=MIN_BET_CENTS / 100, max_bet=MAX_BET_CENTS / 100,
                min_nft=crash_min_prize_cents() / 100, available=game_available('hilo'),
                balance=(me['balance'] / 100) if me else 0)


def hilo_settle_cashout(db, uid, game, want_gift):
    """Caller owns the transaction. Pays the game out; raises ValueError with a user-facing message."""
    bet, mult = int(game['bet']), int(game['mult_micro'])
    payout = bet * mult // HILO_MICRO
    prize_info, remainder = None, 0
    # The win always arrives as a Telegram gift, the rest goes to the balance.
    # Only if it is below the cheapest gift does it stay in TON.
    prize_info = crash_prize_preview(payout)
    if prize_info:
        remainder = max(0, payout - prize_info['price_cents'])
        moved = db.execute("""UPDATE hilo_games SET state='cashed',payout=?,prize_name=?,prize_image=?,prize_price=?,
                              finished_at=CURRENT_TIMESTAMP WHERE id=? AND state='active'""",
                           (payout, prize_info['name'][:140], prize_info['image_url'], prize_info['price_cents'], game['id']))
    else:
        moved = db.execute("UPDATE hilo_games SET state='cashed',payout=?,finished_at=CURRENT_TIMESTAMP WHERE id=? AND state='active'",
                           (payout, game['id']))
    if not moved.rowcount:
        raise ValueError('Игра уже завершена.')
    fairness_mark_settled(db, 'hilo', game['id'])
    label = f'Hi-Lo x{mult / HILO_MICRO:.2f}'
    prize = None
    if prize_info:
        db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                      VALUES(?,?,?,?,?,'game',?)""",
                   (uid, prize_info['id'], prize_info['name'], prize_info['image_url'], prize_info['price_cents'], game['id']))
        if remainder:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (remainder, uid))
            record_transaction(db, uid, 'hilo_win', remainder, 'hilo_game', game['id'],
                               f'{label}: остаток после подарка {prize_info["name"]}')
        record_transaction(db, uid, 'hilo_gift_win', 0, 'hilo_game', game['id'], f'{prize_info["name"]} · {label}')
        prize = dict(name=prize_info['name'], image_url=prize_info['image_url'], price_ton=prize_info['price_ton'])
    else:
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (payout, uid))
        record_transaction(db, uid, 'hilo_win', payout, 'hilo_game', game['id'], label)
    return payout, prize, remainder


def hilo_lock_user(db, uid):
    if DATABASE_URL:
        db.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (uid,))


HILO_TIER_TARGETS = (3, 5, 7, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200, 300, 500)
HILO_ROOM_MS = 15000   # one shared round
HILO_BET_MS = 10000    # first 10 s: bets open, last 5 s: reveal


def hilo_round_no(db, slot):
    """Round number from the DB (1, 2, 3 ...). Registers the time slot on first use.
    Call it inside an open transaction so the registration is committed with it."""
    slot = int(slot)
    row = db.execute('SELECT no FROM hilo_rounds WHERE slot=?', (slot,)).fetchone()
    if not row:
        db.execute('INSERT OR IGNORE INTO hilo_rounds(slot) VALUES(?)', (slot,))
        row = db.execute('SELECT no FROM hilo_rounds WHERE slot=?', (slot,)).fetchone()
    hilo_room_proof(db, slot)
    return int(row['no']) if row else 0


def hilo_round_numbers(db, slots):
    """Read-only lookup slot -> round number for already played rounds."""
    slots = sorted({int(x) for x in slots})
    if not slots:
        return {}
    marks = ','.join('?' * len(slots))
    return {int(r['slot']): int(r['no']) for r in db.execute(
        f'SELECT slot,no FROM hilo_rounds WHERE slot IN ({marks})', tuple(slots)).fetchall()}


def hilo_room_proof(db, slot):
    """Create/read the shared-room commitment before bets are accepted."""
    slot = int(slot)
    row = fairness_get(db, game='hilo_room', game_ref=str(slot))
    if row:
        return row
    previous = fairness_get(db, game='hilo_room', game_ref=str(slot - 1))
    base = 0
    if previous:
        try:
            prior = json.loads(previous['outcome_json'] or '{}')
            base = int(prior.get('result_rank') or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            base = 0
    if not (1 <= base <= HILO_RANKS):
        base = hilo_room_rank(slot)
    proof = fairness_make('hilo_room', 0, f'hilo-room:{slot}', nonce=slot)
    ticket, cursor, digest = fairness_draw(proof, HILO_RANKS - 1, 0)
    candidate = int(ticket) + 1
    result = candidate if candidate < base else candidate + 1
    fairness_store(db, proof, str(slot), cursor, {
        'base_rank': int(base), 'result_rank': int(result),
        'ticket': int(ticket), 'upper': HILO_RANKS - 1, 'digest': digest
    })
    return fairness_get(db, proof_id=proof['id'])


def hilo_room_ranks(db, slot):
    row = hilo_room_proof(db, slot)
    try:
        outcome = json.loads(row['outcome_json'] or '{}')
        base, result = int(outcome.get('base_rank') or 0), int(outcome.get('result_rank') or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        base = result = 0
    if not (1 <= base <= HILO_RANKS and 1 <= result <= HILO_RANKS and base != result):
        raise RuntimeError('Некорректный Proof of Fairness общего Hi-Lo.')
    return base, result, row


def hilo_room_visible_rank(db, slot):
    """Read historical visible ranks without creating retroactive commitments."""
    slot = int(slot)
    row = fairness_get(db, game='hilo_room', game_ref=str(slot))
    if row:
        try:
            rank = int(json.loads(row['outcome_json'] or '{}').get('base_rank') or 0)
            if 1 <= rank <= HILO_RANKS:
                return rank
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    previous = fairness_get(db, game='hilo_room', game_ref=str(slot - 1))
    if previous:
        try:
            rank = int(json.loads(previous['outcome_json'] or '{}').get('result_rank') or 0)
            if 1 <= rank <= HILO_RANKS:
                return rank
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return hilo_room_rank(slot)

_hilo_tier_cache = dict(at=0, gifts=[])


def hilo_tier_gifts():
    """15 gifts, one per rank: rank 1 is the ~3 TON gift, prices strictly grow up to rank 15."""
    if time.time() - _hilo_tier_cache['at'] < 60 and _hilo_tier_cache['gifts']:
        return _hilo_tier_cache['gifts']
    try:
        pool = [g for g in read_catalog()['gifts']
                if g.get('name') and safe_image(g.get('image_url')) and not g.get('background_label')]
    except (OSError, ValueError, KeyError, TypeError):
        pool = []
    priced = []
    for g in pool:
        try:
            c = ton_to_cents(g['price_ton'])
        except Exception:
            continue
        if c > 0:
            priced.append((c, g))
    priced.sort(key=lambda t: t[0])
    out, last = [], 0
    for target in HILO_TIER_TARGETS:
        cand = [p for p in priced if p[0] > last]
        if not cand:
            break
        best = min(cand, key=lambda p: abs(p[0] - target * 100))
        last = best[0]
        out.append(dict(name=str(best[1]['name'])[:140], image_url=safe_image(best[1].get('image_url')),
                        price_ton=best[0] / 100))
    if out:
        _hilo_tier_cache.update(at=time.time(), gifts=out)
    return out


def hilo_tier_card(rank):
    tiers = hilo_tier_gifts()
    if not tiers:
        return dict(name='', image_url='', price_ton=0)
    return tiers[min(max(rank, 1), len(tiers)) - 1]


@app.get('/api/hilo/tiers')
@login_required
def hilo_tiers():
    return jsonify(tiers=[dict(rank=i + 1, **g) for i, g in enumerate(hilo_tier_gifts())])


def hilo_room_is_push(base, direction):
    """Nothing can be higher than the top card (nor lower than the bottom one), and the next card is never
    equal - so the opposite bet is a sure win. Both are a push (x1.00): the stake simply comes back."""
    wins = hilo_wins(base, direction)
    return wins <= 0 or wins >= HILO_RANKS - 1


def hilo_room_step_micro(base, direction):
    """Room payout multiplier. An impossible direction is counted as exactly x1.00 (the bet comes back)."""
    if hilo_room_is_push(base, direction):
        return HILO_MICRO
    return hilo_step_micro(base, direction)


def _hilo_hash(tag, n):
    key = app.secret_key if isinstance(app.secret_key, bytes) else str(app.secret_key).encode()
    return int.from_bytes(hmac.new(key, f'hilo-room:{tag}:{int(n)}'.encode(), hashlib.sha256).digest()[:4], 'big')


def _hilo_raw(n):
    return _hilo_hash('b', n) % HILO_RANKS + 1


@lru_cache(maxsize=8192)
def hilo_room_rank(n):
    """Card of shared round n. Two neighbouring rounds never get the same rank.
    Stateless: every worker derives the same value. A raw hash that repeats its predecessor is replaced by
    a rank that differs from the raw neighbours (and from the replaced predecessor, if there was one)."""
    n = int(n)
    raw = _hilo_raw(n)
    if n <= 0 or raw != _hilo_raw(n - 1):
        return raw
    excluded = {raw, _hilo_raw(n + 1)}
    if n - 1 > 0 and _hilo_raw(n - 1) == _hilo_raw(n - 2):
        excluded.add(hilo_room_rank(n - 1))
    free = [v for v in range(1, HILO_RANKS + 1) if v not in excluded]
    return free[_hilo_hash('a', n) % len(free)]


def hilo_restore_gift(db, uid, raw, name, image, price):
    """Give a gift that was staked in a winning Hi-Lo round back to the inventory."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    row = dict(data) if isinstance(data, dict) else {}
    if not row.get('gift_name'):
        row = dict(gift_id=str(name or 'gift')[:140], gift_name=str(name or 'Подарок')[:140],
                   image_url=image or '', floor_price=int(price or 0), source='game')
    for key in ('id', 'created_at'):
        row.pop(key, None)
    row['user_id'] = int(uid)
    cols = [c for c in row if re.fullmatch(r'[a-z_][a-z0-9_]*', str(c))]
    db.execute(f"INSERT INTO inventory({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
               tuple(row[c] for c in cols))


def hilo_room_settle(db, n, phase):
    rows = db.execute("""SELECT * FROM hilo_room_bets WHERE settled=0 AND (round_no<? OR (round_no=? AND ?>=?))""",
                      (n, n, phase, HILO_BET_MS)).fetchall()
    for r in rows:
        base, res, _proof = hilo_room_ranks(db, r['round_no'])
        fairness_mark_settled(db, 'hilo_room', r['round_no'])
        push = hilo_room_is_push(base, r['direction'])
        won = True if push else (res > base if r['direction'] == 'hi' else res < base)
        step = hilo_room_step_micro(base, r['direction'])
        total = int(r['amount']) * step // HILO_MICRO if won and step else 0
        gift_bet = bool(r['gift_name'])
        # The whole win is paid as a Telegram gift: the closest catalog gift that does not exceed it,
        # the remainder goes to the balance. A staked gift is taken (8 TON gift x1.5 -> a ~12 TON gift + remainder).
        # Only a push (x1.00) or a win below the cheapest gift returns the staked gift (+ profit in TON).
        prize = None
        if won and not push and total > 0:
            try:
                prize = crash_prize_preview(total)
            except Exception:
                prize = None
        keep_stake = gift_bet and (push or not prize)
        credit = max(0, total - int(r['amount'])) if keep_stake else total
        if not db.execute('UPDATE hilo_room_bets SET settled=1,payout=?,won=? WHERE id=? AND settled=0',
                          (credit, 1 if won else 0, r['id'])).rowcount:
            continue
        if not won:
            continue
        label = f'Hi-Lo общий раунд x{step / HILO_MICRO:.2f}' + (' (возврат)' if push else '')
        if keep_stake:
            hilo_restore_gift(db, r['user_id'], r['gift_row'], r['gift_name'], r['gift_image'], r['amount'])
            record_transaction(db, r['user_id'], 'hilo_gift_return', 0, 'hilo_room', r['id'], f"{r['gift_name']} · {label}")
        if prize:
            remainder = max(0, total - int(prize['price_cents']))
            db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id)
                          VALUES(?,?,?,?,?,'game',?)""",
                       (r['user_id'], prize['id'], prize['name'], prize['image_url'], prize['price_cents'], r['id']))
            db.execute('UPDATE hilo_room_bets SET prize_name=?,prize_image=?,prize_price=? WHERE id=?',
                       (prize['name'][:140], prize['image_url'], prize['price_cents'], r['id']))
            record_transaction(db, r['user_id'], 'hilo_gift_win', 0, 'hilo_room', r['id'], f"{prize['name']} · {label}")
            credit = remainder
            if remainder:
                db.execute('UPDATE users SET balance=balance+? WHERE id=?', (remainder, r['user_id']))
                record_transaction(db, r['user_id'], 'hilo_win', remainder, 'hilo_room', r['id'],
                                   f"{label}: остаток после подарка {prize['name']}")
            continue
        if credit:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (credit, r['user_id']))
            record_transaction(db, r['user_id'], 'hilo_win', credit, 'hilo_room', r['id'], label)


def hilo_room_payload(db, uid, n, phase, now):
    base, result_rank, _fair_row = hilo_room_ranks(db, n)
    reveal = phase >= HILO_BET_MS
    card = lambda rk: dict(rank=rk, **hilo_tier_card(rk))
    odds = {d: dict(x=hilo_room_step_micro(base, d) / HILO_MICRO,
                    chance=(100.0 if hilo_room_is_push(base, d) else round(hilo_wins(base, d) * 100 / (HILO_RANKS - 1), 1)),
                    push=hilo_room_is_push(base, d))
            for d in ('hi', 'lo')}
    rows = db.execute("""SELECT b.user_id,b.direction,b.amount,b.gift_name,b.gift_image,b.settled,b.payout,b.want_gift,b.prize_name,b.prize_image,b.prize_price,u.name,u.photo_url
                         FROM hilo_room_bets b JOIN users u ON u.id=b.user_id WHERE b.round_no=?
                         ORDER BY b.amount DESC,b.id ASC LIMIT 60""", (n,)).fetchall()
    bets = [dict(user_id=r['user_id'], name=r['name'] or 'Игрок', photo_url=r['photo_url'] or '', direction=r['direction'],
                 settled=bool(r['settled']), amount=r['amount'] / 100, payout=r['payout'] / 100, mine=r['user_id'] == uid,
                 gift_name=r['gift_name'] or '', gift_image=r['gift_image'] or '', want_gift=bool(r['want_gift']),
                 prize_name=r['prize_name'] or '', prize_image=r['prize_image'] or '', prize_price=(r['prize_price'] or 0) / 100)
            for r in rows]
    _, hl_clear_id = wins_feed_cutoff(db, 'hilo')
    recent_rows = db.execute("""SELECT b.id,b.user_id,b.round_no,b.direction,b.amount,b.payout,b.won,b.gift_name,b.gift_image,
                                        b.prize_name,b.prize_image,b.prize_price,u.name,u.photo_url
                                 FROM hilo_room_bets b JOIN users u ON u.id=b.user_id WHERE b.settled=1 AND b.id>?
                                 ORDER BY b.id DESC LIMIT 30""", (hl_clear_id,)).fetchall()
    last = db.execute("""SELECT id,round_no,direction,amount,payout,won,gift_name,gift_image,prize_name,prize_image,prize_price FROM hilo_room_bets WHERE user_id=? AND settled=1
                         ORDER BY id DESC LIMIT 1""", (uid,)).fetchone()
    nums = hilo_round_numbers(db, [r['round_no'] for r in recent_rows] + ([last['round_no']] if last else []) + [n])
    cur_no = nums.get(n, 0)
    recent = [dict(user_id=r['user_id'], name=r['name'] or 'Игрок', photo_url=r['photo_url'] or '', round=r['round_no'],
                   direction=r['direction'], amount=r['amount'] / 100, payout=r['payout'] / 100, no=nums.get(r['round_no'], 0),
                   won=bool(r['won'] or r['payout'] > 0),
                   gift_name=r['gift_name'] or '', gift_image=r['gift_image'] or '', mine=r['user_id'] == uid,
                   x=hilo_room_step_micro(hilo_room_rank(r['round_no']), r['direction']) / HILO_MICRO,
                   push=hilo_room_is_push(hilo_room_rank(r['round_no']), r['direction']),
                   prize_name=r['prize_name'] or '', prize_image=r['prize_image'] or '', prize_price=(r['prize_price'] or 0) / 100)
              for r in recent_rows]
    upto = n + 1 if reveal else n
    seq = [hilo_room_visible_rank(db, k) for k in range(upto - 25, upto)]
    history = [dict(rank=r, rel='up' if r > seq[i] else 'down' if r < seq[i] else 'eq', **hilo_tier_card(r))
               for i, r in enumerate(seq[1:])]
    me = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    return dict(now=now, round=n, no=cur_no, phase=phase, bet_ms=HILO_BET_MS, room_ms=HILO_ROOM_MS, card=card(base),
                result=card(result_rank) if reveal else None, odds=odds, bets=bets,
                history=history, recent=recent,
                min_bet=MIN_BET_CENTS / 100, max_bet=MAX_BET_CENTS / 100, available=game_available('hilo'),
                balance=(me['balance'] / 100) if me else 0,
                fairness=fairness_view(db, 'hilo_room', n, reveal),
                last=dict(id=last['id'], round=last['round_no'], no=nums.get(last['round_no'], 0), gift_name=last['gift_name'] or '', gift_image=last['gift_image'] or '',
                          won=bool(last['won'] or last['payout'] > 0), direction=last['direction'], amount=last['amount'] / 100,
                          payout=last['payout'] / 100, prize_name=last['prize_name'] or '', prize_image=last['prize_image'] or '',
                          prize_price=(last['prize_price'] or 0) / 100) if last else None,
                min_nft=crash_min_prize_cents() / 100)


@app.get('/api/hilo/recent-wins')
@login_required
def hilo_recent_wins():
    with connect() as db:
        try:
            settle_previous_daily_top_rewards(db)
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('Daily top reward settlement failed while loading Hi-Lo wins')
        _, max_id = wins_feed_cutoff(db, 'hilo')
        sel = """SELECT b.id,b.user_id,b.round_no,b.direction,b.amount,b.payout,b.gift_name,b.gift_image,
                        b.prize_name,b.prize_image,b.prize_price,b.created_at,u.name,u.username,u.photo_url
                 FROM hilo_room_bets b JOIN users u ON u.id=b.user_id
                 WHERE b.settled=1 AND b.id>?
                   AND b.payout+b.prize_price>CASE WHEN b.gift_name='' THEN b.amount ELSE 0 END"""
        rows = db.execute(sel+' ORDER BY b.id DESC LIMIT 50', (max_id,)).fetchall()
        top = db.execute(sel+""" AND COALESCE(u.withdrawal_enabled,1)=1 AND b.created_at>=?
                          ORDER BY b.payout+b.prize_price DESC,b.id DESC LIMIT 1""",
                         (max_id, _daily_top_db_string(daily_top_candidate_start('hilo')))).fetchone()
    def item(r):
        base = hilo_room_rank(r['round_no'])
        total = int(r['payout'] or 0) + int(r['prize_price'] or 0)
        return dict(id=r['id'], user_id=r['user_id'], name=r['name'], username=r['username'], photo_url=r['photo_url'],
                    direction=r['direction'], bet=r['amount']/100, amount=total/100,
                    multiplier=round(hilo_room_step_micro(base, r['direction'])/HILO_MICRO, 4),
                    gift=(dict(name=r['prize_name'], image_url=r['prize_image'], price_ton=(r['prize_price'] or 0)/100)
                          if r['prize_name'] else None), created_at=r['created_at'])
    return jsonify(items=[item(r) for r in rows], top_drop=item(top) if top else None,
                   top_reward=daily_top_reward('hilo'), top_schedule=daily_top_schedule_view('hilo'))


@app.get('/api/hilo/room')
@login_required
def hilo_room():
    now = int(time.time() * 1000)
    n, phase = now // HILO_ROOM_MS, now % HILO_ROOM_MS
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        hilo_round_no(db, n)
        hilo_room_settle(db, n, phase)
        if phase >= HILO_BET_MS:
            fairness_mark_settled(db, 'hilo_room', n)
        db.commit()
        return jsonify(hilo_room_payload(db, session['uid'], n, phase, now))
    finally:
        db.close()


@app.post('/api/hilo/room/bet')
@login_required
def hilo_room_bet():
    data = request.get_json(silent=True) or {}
    direction = data.get('direction')
    if direction not in ('hi', 'lo'):
        return error('Выберите Hi или Lo.')
    inventory_id = data.get('inventory_id')
    try:
        inventory_id = int(inventory_id) if inventory_id not in (None, '') else None
    except (TypeError, ValueError):
        return error('Некорректный подарок для ставки.')
    bet = 0
    if inventory_id is None:
        try:
            bet = parse_amount(data.get('bet'))
        except (ValueError, InvalidOperation, TypeError):
            return error('Укажите корректную ставку.')
        if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
            return error('Ставка от 0.10 до 300 TON.')
    uid = session['uid']
    now = int(time.time() * 1000)
    n, phase = now // HILO_ROOM_MS, now % HILO_ROOM_MS
    if phase >= HILO_BET_MS - 300:
        return error('Приём ставок закрыт — дождитесь следующего раунда.', 409)
    want_gift = 1   # a TON win is always paid as the closest NFT gift (+ remainder in TON)
    db = connect()
    new_level = None
    try:
        db.execute('BEGIN IMMEDIATE')
        hilo_lock_user(db, uid)
        hilo_round_no(db, n)
        if db.execute('SELECT 1 FROM hilo_room_bets WHERE round_no=? AND user_id=?', (n, uid)).fetchone():
            db.rollback()
            return error('В этом раунде ставка уже сделана.', 409)
        gname = gimage = ''
        if inventory_id is not None:
            purge_expired_inventory(db, uid)
            lock = ' FOR UPDATE' if DATABASE_URL else ''
            item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?' + lock, (inventory_id, uid)).fetchone()
            if not item:
                db.rollback()
                return error('Подарок не найден в инвентаре.', 404)
            if int(item['deposit_mirror'] or 0):
                db.rollback()
                return error('NFT-пополнение уже зачислено на баланс и недоступно для ставки.', 409)
            if item['promo_locked']:
                db.rollback()
                return error('Отыгрышные подарки в Hi-Lo недоступны.')
            bet = int(item['floor_price'] or 0)
            if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
                db.rollback()
                return error('Для ставки подходят подарки стоимостью от 0.10 до 300 TON.')
            xp_allowed = gift_counts_for_xp(item)
            gname, gimage = str(item['gift_name'] or '')[:140], str(item['image_url'] or '')
            gsnap = json.dumps(dict(item), default=str)
            if not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (inventory_id, uid)).rowcount:
                db.rollback()
                return error('Подарок уже используется.', 409)
            cur = db.execute('INSERT INTO hilo_room_bets(round_no,user_id,direction,amount,gift_name,gift_image,gift_row) VALUES(?,?,?,?,?,?,?)',
                             (n, uid, direction, bet, gname, gimage, gsnap))
            record_transaction(db, uid, 'hilo_gift_bet', 0, 'hilo_room', cur.lastrowid, gname)
            if xp_allowed:
                new_level = increase_turnover(db, uid, bet)
        else:
            if not db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?', (bet, uid, bet)).rowcount:
                db.rollback()
                return error('Недостаточно средств.')
            cur = db.execute('INSERT INTO hilo_room_bets(round_no,user_id,direction,amount,want_gift) VALUES(?,?,?,?,?)', (n, uid, direction, bet, want_gift))
            record_transaction(db, uid, 'hilo_bet', -bet, 'hilo_room', cur.lastrowid, 'Hi-Lo общий раунд')
            new_level = increase_turnover(db, uid, bet)
        db.commit()
    finally:
        db.close()
    if new_level:
        notify_level_up_async(uid, new_level)
    db = connect()
    try:
        return jsonify(ok=True, room=hilo_room_payload(db, uid, n, phase, now), user=profile(), new_level=new_level)
    finally:
        db.close()


@app.get('/api/hilo/state')
@login_required
def hilo_state():
    db = connect()
    try:
        return jsonify(hilo_state_payload(db, session['uid']))
    finally:
        db.close()


@app.get('/api/hilo/prize')
@login_required
def hilo_prize():
    try:
        amount = int(round(float(request.args.get('amount', '0')) * 100))
    except (TypeError, ValueError):
        return error('Неверная сумма.')
    return jsonify(prize=crash_prize_preview(max(0, amount)), min_nft=crash_min_prize_cents() / 100)


@app.post('/api/hilo/start')
@login_required
def hilo_start():
    data = request.get_json(silent=True) or {}
    inventory_id = data.get('inventory_id')
    try:
        inventory_id = int(inventory_id) if inventory_id not in (None, '') else None
    except (TypeError, ValueError):
        return error('Некорректный подарок для ставки.')
    bet = 0
    if inventory_id is None:
        try:
            bet = parse_amount(data.get('bet'))
        except (ValueError, InvalidOperation, TypeError):
            return error('Укажите корректную ставку.')
        if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
            return error('Ставка от 0.10 до 300 TON.')
    uid = session['uid']
    proof = fairness_make('hilo', uid, data.get('client_seed'))
    rank_ticket, fair_cursor, _ = fairness_draw(proof, HILO_RANKS, 0)
    rank = 1 + rank_ticket
    first_card = hilo_tier_card(rank)
    card_name = str(first_card.get('name') or '')[:140]
    card_image = first_card.get('image_url') or ''
    db = connect()
    new_level = None
    try:
        db.execute('BEGIN IMMEDIATE')
        hilo_lock_user(db, uid)
        if db.execute("SELECT 1 FROM hilo_games WHERE user_id=? AND state='active'", (uid,)).fetchone():
            db.rollback()
            return error('Игра уже идёт. Завершите её или заберите выигрыш.', 409)
        if inventory_id is not None:
            purge_expired_inventory(db, uid)
            lock = ' FOR UPDATE' if DATABASE_URL else ''
            item = db.execute('SELECT * FROM inventory WHERE id=? AND user_id=?' + lock, (inventory_id, uid)).fetchone()
            if not item:
                db.rollback()
                return error('Подарок не найден в инвентаре.', 404)
            if int(item['deposit_mirror'] or 0):
                db.rollback()
                return error('NFT-пополнение уже зачислено на баланс и недоступно для ставки.', 409)
            if item['promo_locked']:
                db.rollback()
                return error('Отыгрышные подарки в Hi-Lo недоступны.')
            bet = int(item['floor_price'] or 0)
            if not (MIN_BET_CENTS <= bet <= MAX_BET_CENTS):
                db.rollback()
                return error('Для ставки подходят подарки стоимостью от 0.10 до 300 TON.')
            xp_allowed = gift_counts_for_xp(item)
            if not db.execute('DELETE FROM inventory WHERE id=? AND user_id=?', (inventory_id, uid)).rowcount:
                db.rollback()
                return error('Подарок уже используется.', 409)
            cursor = db.execute("""INSERT INTO hilo_games(user_id,bet,bet_type,bet_inventory_id,bet_gift_id,bet_gift_name,
                                   bet_gift_image,cur_rank,card_name,card_image,history)
                                   VALUES(?,?,'gift',?,?,?,?,?,?,?,?)""",
                                (uid, bet, inventory_id, str(item['gift_id'] or ''), str(item['gift_name'] or '')[:140],
                                 str(item['image_url'] or ''), rank, card_name, card_image,
                                 json.dumps([dict(r=rank, rel=None)])))
            record_transaction(db, uid, 'hilo_gift_bet', 0, 'hilo_game', cursor.lastrowid, str(item['gift_name'] or '')[:140])
            if xp_allowed:
                new_level = increase_turnover(db, uid, bet)
        else:
            if not db.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?', (bet, uid, bet)).rowcount:
                db.rollback()
                return error('Недостаточно средств.')
            cursor = db.execute("""INSERT INTO hilo_games(user_id,bet,cur_rank,card_name,card_image,history)
                                   VALUES(?,?,?,?,?,?)""",
                                (uid, bet, rank, card_name, card_image, json.dumps([dict(r=rank, rel=None)])))
            record_transaction(db, uid, 'hilo_bet', -bet, 'hilo_game', cursor.lastrowid, 'Hi-Lo')
            new_level = increase_turnover(db, uid, bet)
        game_id = int(cursor.lastrowid)
        fairness_store(db, proof, str(game_id), fair_cursor, {'ranks': [rank]})
        db.commit()
    finally:
        db.close()
    if new_level:
        notify_level_up_async(uid, new_level)
    db = connect()
    try:
        return jsonify(ok=True, state=hilo_state_payload(db, uid), user=profile(), new_level=new_level)
    finally:
        db.close()


@app.post('/api/hilo/guess')
@login_required
def hilo_guess():
    data = request.get_json(silent=True) or {}
    direction = data.get('direction')
    if direction not in ('hi', 'lo'):
        return error('Выберите Hi или Lo.')
    uid = session['uid']
    result = {}
    settled = None
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        hilo_lock_user(db, uid)
        game = db.execute("SELECT * FROM hilo_games WHERE user_id=? AND state='active' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not game:
            db.rollback()
            return error('Нет активной игры.', 409)
        rank = int(game['cur_rank'])
        proof = fairness_get(db, game='hilo', game_ref=str(game['id']))
        if proof:
            fair_cursor = int(proof['cursor'] or 0)
            while True:
                rank_ticket, fair_cursor, _ = fairness_draw(proof, HILO_RANKS, fair_cursor)
                new_rank = 1 + rank_ticket
                if new_rank != rank:
                    break
            next_card = hilo_tier_card(new_rank)
            card_name = str(next_card.get('name') or '')[:140]
            card_image = next_card.get('image_url') or ''
        else:
            new_rank, card_name, card_image = hilo_pick_card()
            while new_rank == rank:
                new_rank, card_name, card_image = hilo_pick_card()
            fair_cursor = 0
        step = hilo_step_micro(rank, direction)
        if not step:
            db.rollback()
            return error('С этой карты так ставить нельзя — выберите другое направление.', 409)
        won = new_rank > rank if direction == 'hi' else new_rank < rank
        rel = 'up' if new_rank > rank else 'down' if new_rank < rank else 'eq'
        history = hilo_history(game) + [dict(r=new_rank, rel=rel)]
        if proof:
            fairness_set_progress(db, proof['id'], fair_cursor,
                                  {'ranks': [int(x.get('r') or 0) for x in history]})
        result = dict(rank=new_rank, relation=rel, win=won, card=crash_gift_view(card_name, card_image, 0))
        if won:
            mult = int(game['mult_micro']) * step // HILO_MICRO
            moved = db.execute("""UPDATE hilo_games SET cur_rank=?,card_name=?,card_image=?,steps=steps+1,mult_micro=?,history=?
                                  WHERE id=? AND state='active'""",
                               (new_rank, card_name, card_image, mult, json.dumps(history[-40:]), game['id']))
            if not moved.rowcount:
                db.rollback()
                return error('Игра уже завершена.', 409)
            if mult >= HILO_MAX_MULT_MICRO or int(game['bet']) * mult // HILO_MICRO >= HILO_MAX_PAYOUT_CENTS:
                fresh = db.execute('SELECT * FROM hilo_games WHERE id=?', (game['id'],)).fetchone()
                payout, prize, remainder = hilo_settle_cashout(db, uid, fresh, True)
                settled = dict(payout=payout / 100, prize=prize, remainder=remainder / 100, auto=True)
        else:
            moved = db.execute("""UPDATE hilo_games SET state='lost',cur_rank=?,card_name=?,card_image=?,history=?,
                                  finished_at=CURRENT_TIMESTAMP WHERE id=? AND state='active'""",
                               (new_rank, card_name, card_image, json.dumps(history[-40:]), game['id']))
            if not moved.rowcount:
                db.rollback()
                return error('Игра уже завершена.', 409)
            fairness_mark_settled(db, 'hilo', game['id'])
            if (game['bet_type'] or 'ton') == 'gift':
                record_transaction(db, uid, 'hilo_gift_lost', 0, 'hilo_game', game['id'], str(game['bet_gift_name'] or '')[:140])
        db.commit()
    except ValueError as exc:
        db.rollback()
        return error(str(exc), 409)
    finally:
        db.close()
    db = connect()
    try:
        return jsonify(ok=True, result=result, settled=settled, state=hilo_state_payload(db, uid), user=profile())
    finally:
        db.close()


@app.post('/api/hilo/cashout')
@login_required
def hilo_cashout():
    uid = session['uid']
    want_gift = True   # gift-first payout is mandatory; the request flag is ignored
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        hilo_lock_user(db, uid)
        game = db.execute("SELECT * FROM hilo_games WHERE user_id=? AND state='active' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not game:
            db.rollback()
            return error('Нет активной игры.', 409)
        if int(game['steps']) < 1:
            db.rollback()
            return error('Заберите выигрыш после первого верного ответа.', 409)
        payout, prize, remainder = hilo_settle_cashout(db, uid, game, want_gift)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return error(str(exc), 409)
    finally:
        db.close()
    db = connect()
    try:
        return jsonify(ok=True, multiplier=int(game['mult_micro']) / HILO_MICRO, payout=payout / 100, prize=prize,
                       remainder=remainder / 100, state=hilo_state_payload(db, uid), user=profile())
    finally:
        db.close()



@app.get('/api/ui/settings')
def public_ui_settings():
    # Intentionally public: loader and visible navigation are needed before auth finishes.
    return jsonify(loader_gif=loader_settings()['path'], sections=section_settings(),
                   black_backgrounds_enabled=black_backgrounds_enabled(),
                   games=effective_games(), game_modes=game_modes() if is_admin_session() else None,
                   game_layout=game_layout())


@app.get('/api/admin/section-settings')
@admin_required
def admin_section_settings():
    return jsonify(sections=section_settings(), black_backgrounds_enabled=black_backgrounds_enabled(),
                   games=effective_games(), game_modes=game_modes(), game_layout=game_layout())


@app.post('/api/admin/section-settings')
@admin_required
def save_admin_section_settings():
    data = request.get_json(silent=True) or {}
    current = section_settings()
    has_sections = any(key in data for key in current)
    if 'black_backgrounds_enabled' in data and not isinstance(data['black_backgrounds_enabled'], bool):
        return error('Состояние отображения фонов должно быть true или false.')
    updated = current
    if has_sections:
        if any(not isinstance(data[key], bool) for key in current if key in data):
            return error('Состояние раздела должно быть true или false.')
        updated = {key: bool(data.get(key, current[key])) for key in current}
        if not any(updated.values()):
            return error('Нужно оставить включённым хотя бы один раздел.')
    new_modes = None
    if 'games' in data:
        if not isinstance(data['games'], dict):
            return error('Состояние игр должно быть объектом.')
        new_modes = game_modes()
        for key, value in data['games'].items():
            if key not in GAME_KEYS:
                continue
            if isinstance(value, bool):
                value = 'on' if value else 'off'
            if value not in ('on', 'off', 'admin'):
                return error('Режим игры: on, off или admin.')
            new_modes[key] = value
        save_document('game_modes', new_modes)
        # Read the row back from the database (no request cache) so we only report success when it is really stored.
        with connect() as db:
            row = db.execute('SELECT payload FROM app_documents WHERE name=?', ('game_modes',)).fetchone()
        try:
            stored = json.loads(row['payload']) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = {}
        if any(stored.get(key) != value for key, value in new_modes.items()):
            app.logger.error('game_modes were not persisted: wanted %s got %s', new_modes, stored)
            return error('Настройки игр не сохранились. Попробуйте ещё раз.', 500)
        with connect() as db:
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'game_modes', json.dumps(new_modes, ensure_ascii=False)))
    if 'game_layout' in data:
        layout = data['game_layout']
        if not isinstance(layout, dict) or not isinstance(layout.get('order'), list) \
                or not isinstance(layout.get('badges', {}), dict):
            return error('Порядок и значки игр: неверный формат.')
        order = []
        for key in layout['order']:
            if key in GAME_KEYS and key not in order:
                order.append(key)
        for key in GAME_LAYOUT_DEFAULT_ORDER:
            if key not in order:
                order.append(key)
        badges = {}
        for key, value in (layout.get('badges') or {}).items():
            if key not in GAME_KEYS:
                continue
            if value in ('', None):
                continue
            if value not in GAME_BADGES:
                return error('Значок: new, hot, top, beta или soon.')
            badges[key] = value
        save_document('game_layout', dict(order=order, badges=badges))
        with connect() as db:
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'game_layout',
                        json.dumps(dict(order=order, badges=badges), ensure_ascii=False)))
    if has_sections:
        save_document('section_settings', updated)
    if 'black_backgrounds_enabled' in data:
        save_document('gift_display_settings', {'black_backgrounds_enabled': data['black_backgrounds_enabled']})
    return jsonify(ok=True, saved=True, sections=section_settings(), black_backgrounds_enabled=black_backgrounds_enabled(),
                   games=effective_games(), game_modes=game_modes(), game_layout=game_layout())


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


REFERRAL_MIN_WITHDRAW_CENTS = 200  # 2 TON


@app.get('/api/referrals/me')
@login_required
def my_referrals():
    uid = session['uid']
    with connect() as db:
        count = db.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?', (uid,)).fetchone()[0]
        depositors = db.execute('''SELECT COUNT(DISTINCT user_id) FROM deposits
                                   WHERE referrer_id=? AND referral_bonus>0''', (uid,)).fetchone()[0]
        total = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND kind='referral_bonus'",
                           (uid,)).fetchone()[0]
        me = db.execute('SELECT ref_balance FROM users WHERE id=?', (uid,)).fetchone()
        ref_balance = int(me['ref_balance'] or 0) if me else 0
        rows = db.execute('''SELECT r.referred_id AS id, r.created_at AS joined_at, u.name AS name, u.username AS username,
                                    u.photo_url AS photo_url,
                                    COALESCE((SELECT SUM(d.amount) FROM deposits d
                                              WHERE d.user_id=r.referred_id AND d.referrer_id=r.referrer_id),0) AS deposited,
                                    COALESCE((SELECT SUM(d.referral_bonus) FROM deposits d
                                              WHERE d.user_id=r.referred_id AND d.referrer_id=r.referrer_id),0) AS earned
                             FROM referrals r LEFT JOIN users u ON u.id=r.referred_id
                             WHERE r.referrer_id=? ORDER BY earned DESC, r.created_at DESC LIMIT 200''', (uid,)).fetchall()
    username = current_bot_username()
    referrals = [dict(id=r['id'], name=(r['name'] or 'Игрок'), username=r['username'] or '',
                      photo_url=r['photo_url'] or '', joined_at=r['joined_at'] or '',
                      deposited=int(r['deposited'] or 0) / 100, earned=int(r['earned'] or 0) / 100,
                      active=int(r['deposited'] or 0) > 0) for r in rows]
    return jsonify(count=count, depositors=depositors, earned=total / 100,
                   ref_balance=ref_balance / 100, min_withdraw=REFERRAL_MIN_WITHDRAW_CENTS / 100,
                   can_withdraw=ref_balance >= REFERRAL_MIN_WITHDRAW_CENTS, referrals=referrals,
                   percent=referral_percent(), bot_username=username,
                   link=f'https://t.me/{username}?start=ref_{uid}' if username else '')


@app.post('/api/referrals/withdraw')
@login_required
def withdraw_referral_balance():
    """Move the whole referral balance to the main balance (from REFERRAL_MIN_WITHDRAW_CENTS)."""
    uid = session['uid']
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT ref_balance FROM users WHERE id=?' + (' FOR UPDATE' if DATABASE_URL else ''), (uid,)).fetchone()
        amount = int(row['ref_balance'] or 0) if row else 0
        if amount < REFERRAL_MIN_WITHDRAW_CENTS:
            db.rollback()
            return error(f'Вывод с реферального баланса доступен от {REFERRAL_MIN_WITHDRAW_CENTS / 100:g} TON.', 400)
        db.execute('UPDATE users SET balance=balance+?, ref_balance=ref_balance-? WHERE id=?', (amount, amount, uid))
        record_transaction(db, uid, 'referral_withdraw', amount, 'referral', uid, 'Вывод реферального баланса на основной')
        log_event(db, uid, 'referral_withdraw', amount=amount / 100)
        db.commit()
    finally:
        db.close()
    return jsonify(ok=True, withdrawn=amount / 100, user=profile())


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
    """Select one compensation prize without crediting it yet.

    The prize is persisted in upgrade_spins and is credited only after the
    client reel finishes and calls /api/upgrade/compensation/claim.
    """
    empty = dict(cashback=0, cashback_percent=0, promo=None, reward=None, reel=[],
                 deferred=False, claimed=False)
    source_price = max(0, int(source_price or 0))
    if source_price < MIN_BET_CENTS:
        return empty

    large = source_price >= 10000
    medium = source_price >= 2500
    budget = max(1, round(source_price * (.20 if large else .16)))
    catalog_candidates = craft_catalog_candidates()

    # Small losses always get the compensation window, but gifts start only
    # from 5 TON so a tiny bet can never roll an oversized catalog reward.
    allow_gifts = source_price >= 500
    gifts = [g for g in catalog_candidates if g['price'] <= budget] if allow_gifts else []

    visual_budget = max(budget, round(source_price * (.75 if medium or large else .42)))
    if target_price:
        try:
            visual_budget = max(visual_budget, round(int(target_price) * .18))
        except (TypeError, ValueError):
            pass
    visual_gifts = ([g for g in catalog_candidates if g['price'] <= visual_budget]
                    if allow_gifts else [])
    if not gifts and source_price >= 1000 and visual_gifts:
        gifts = visual_gifts[:max(1, min(18, len(visual_gifts)))]

    pool = []
    for percent in (1, 2, 3, 5):
        amount_cents = max(1, round(source_price * percent / 100))
        pool.append(dict(type='balance', amount=amount_cents / 100,
                         image_url='/static/img/ton.png', name='TON'))
    ticket_count = max(1, min(250, round(source_price / 500)))
    pool.append(dict(type='tickets', tickets=ticket_count, image_url='', name='Билеты'))

    gift_options = []
    for gift in gifts:
        for kind in ('gift', 'wager_gift', 'promo'):
            multiplier = secrets.choice([5, 8, 10] if large else
                                        [8, 10, 12] if medium else
                                        [10, 15, 20])
            gift_options.append(dict(
                type=kind, gift_id=gift['id'], name=gift['name'],
                image_url=gift['image_url'], price_ton=gift['price'] / 100,
                wager_multiplier=multiplier if kind == 'wager_gift' else 0
            ))

    if gift_options:
        weights = [('balance', 8 if large else 12 if medium else 16),
                   ('tickets', 10), ('wager_gift', 37), ('gift', 25),
                   ('promo', 20 if large else 16 if medium else 12)]
        roll = secrets.randbelow(sum(weight for _, weight in weights))
        kind = 'balance'
        for candidate, weight in weights:
            if roll < weight:
                kind = candidate
                break
            roll -= weight
        if kind in ('balance', 'tickets'):
            candidates = [x for x in pool if x['type'] == kind]
        else:
            candidates = [g for g in gift_options if g['type'] == kind]
        reward = dict(secrets.choice(candidates or pool))
    else:
        reward = dict(secrets.choice(pool))

    reel_options = pool + gift_options
    if visual_gifts:
        for gift in visual_gifts[-18:]:
            for kind in ('gift', 'wager_gift'):
                multiplier = secrets.choice([5, 8, 10] if large else
                                            [8, 10, 12] if medium else
                                            [10, 15, 20])
                reel_options.append(dict(
                    type=kind, gift_id=gift['id'], name=gift['name'],
                    image_url=gift['image_url'], price_ton=gift['price'] / 100,
                    wager_multiplier=multiplier if kind == 'wager_gift' else 0
                ))

    return dict(empty,
                reward=reward,
                reel=[secrets.choice(reel_options or pool) for _ in range(36)],
                deferred=True,
                claimed=False)


def claim_upgrade_loss_compensation(db, user_id, spin_id, result):
    """Credit a deferred Upgrade compensation exactly once."""
    comp = dict((result or {}).get('compensation') or {})
    reward = dict(comp.get('reward') or {})
    if not reward or not comp.get('deferred'):
        return result, False
    if comp.get('claimed'):
        return result, False

    kind = str(reward.get('type') or '')
    if kind == 'balance':
        amount = ton_to_cents(reward.get('amount') or 0)
        if amount > 0:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
            record_transaction(db, user_id, 'upgrade_cashback', amount, 'upgrade', spin_id,
                               'Компенсация Upgrade')
            comp['cashback'] = amount / 100
    elif kind == 'tickets':
        tickets = max(1, int(reward.get('tickets') or 1))
        db.execute('UPDATE users SET tickets=tickets+? WHERE id=?', (tickets, user_id))
        db.execute("INSERT INTO ticket_ledger(user_id,amount,kind,reference_type,reference_id,details) VALUES(?,?,?,?,?,?)",
                   (user_id, tickets, 'upgrade_compensation', 'upgrade', spin_id,
                    f'Компенсация Upgrade: {tickets} билет(ов)'))
    elif kind in ('gift', 'wager_gift'):
        locked = kind == 'wager_gift'
        price = ton_to_cents(reward.get('price_ton') or 0)
        multiplier = float(reward.get('wager_multiplier') or 0)
        cur = db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress) VALUES(?,?,?,?,?,'upgrade_compensation',?,?,?,0)",
                         (user_id, reward.get('gift_id') or '', reward.get('name') or 'Подарок',
                          reward.get('image_url') or '', price, int(locked), multiplier,
                          round(price * multiplier)))
        reward['inventory_id'] = cur.lastrowid
        reward['wager_target'] = round(price * multiplier) / 100
        record_transaction(db, user_id, 'upgrade_compensation_gift', 0, 'inventory',
                           cur.lastrowid, reward.get('name') or 'Подарок')
    elif kind == 'promo':
        code = unique_promo_code(db, 'UPG')
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.execute("""INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,
                     gift_price,wager_multiplier,max_uses,created_by,assigned_user_id,source_label,description,
                     reward_json,expires_at) VALUES(?,'gift',0,?,?,?,?,0,1,0,?,?,?,?,?)""",
                   (code, reward.get('gift_id') or '', reward.get('name') or 'Подарок',
                    reward.get('image_url') or '', ton_to_cents(reward.get('price_ton') or 0),
                    user_id, 'Компенсация Upgrade', 'Персональный промокод на подарок',
                    json.dumps(dict(compensation=True, owner_id=user_id), ensure_ascii=False), expires))
        comp['promo'] = promo_view(db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone())
        reward['code'] = code
    else:
        raise ValueError('Неизвестный тип компенсации.')

    comp['reward'] = reward
    comp['claimed'] = True
    comp['claimed_at'] = datetime.now(timezone.utc).isoformat()
    result = dict(result or {})
    result['compensation'] = comp
    return result, True


@app.post('/api/upgrade/compensation/claim')
@login_required
def upgrade_compensation_claim():
    data = request.get_json(silent=True) or {}
    spin_id = str(data.get('spin_id') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', spin_id):
        return error('Некорректная компенсация.', 400)
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT user_id,won,result_json FROM upgrade_spins WHERE id=?' +
                         (' FOR UPDATE' if DATABASE_URL else ''), (spin_id,)).fetchone()
        if not row or int(row['user_id']) != int(session['uid']):
            db.rollback()
            return error('Компенсация не найдена.', 404)
        try:
            result = json.loads(row['result_json'] or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            db.rollback()
            return error('Данные компенсации повреждены.', 409)
        if bool(row['won']):
            db.rollback()
            return error('У выигрышного апгрейда нет компенсации.', 409)
        comp = dict(result.get('compensation') or {})
        if not comp.get('reward') or not comp.get('deferred'):
            db.rollback()
            return error('Для этой игры компенсация недоступна.', 409)
        result, newly_claimed = claim_upgrade_loss_compensation(
            db, session['uid'], spin_id, result)
        db.execute('UPDATE upgrade_spins SET result_json=? WHERE id=?',
                   (json.dumps(result, ensure_ascii=False), spin_id))
        db.commit()
    finally:
        db.close()

    promo_code = (((result.get('compensation') or {}).get('promo') or {}).get('code'))
    if newly_claimed and promo_code:
        notify_promo_async(session['uid'], promo_code, 'bonuses')
    return jsonify(ok=True, newly_claimed=newly_claimed,
                   compensation=result.get('compensation') or {}, user=profile())


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



def _promo_poll_counts(db, poll_id):
    rows=db.execute("""SELECT option_name,COUNT(*) AS votes FROM promo_poll_votes
                       WHERE poll_id=? GROUP BY option_name ORDER BY votes DESC,option_name""",(poll_id,)).fetchall()
    total=sum(int(r['votes'] or 0) for r in rows)
    return total,[dict(name=r['option_name'],votes=int(r['votes'] or 0),
                       percent=(round(int(r['votes'] or 0)*100/total,1) if total else 0)) for r in rows]


def _promo_poll_summary(db, poll_id):
    poll=db.execute('SELECT * FROM promo_polls WHERE id=?',(poll_id,)).fetchone()
    if not poll:return None
    total,results=_promo_poll_counts(db,poll_id)
    options=db.execute('SELECT code,poll_option_name FROM promo_codes WHERE poll_id=? ORDER BY created_at,code',(poll_id,)).fetchall()
    by_name={x['name']:x for x in results}
    full=[]
    for row in options:
        item=by_name.get(row['poll_option_name'],{'name':row['poll_option_name'],'votes':0,'percent':0})
        full.append(dict(code=row['code'],name=row['poll_option_name'],votes=item['votes'],percent=item['percent']))
    full.sort(key=lambda x:(-x['votes'],x['name'].casefold()))
    return dict(id=poll['id'],title=poll['title'],active=bool(poll['active']),max_votes=int(poll['max_votes'] or 0),
                uses_count=total,expires_at=poll['expires_at'],created_by=int(poll['created_by'] or 0),
                created_at=poll['created_at'],closed_at=poll['closed_at'],results=full)


def _promo_poll_close(db,poll_id,reason='manual'):
    summary=_promo_poll_summary(db,poll_id)
    if not summary:return None
    if summary['active']:
        payload=json.dumps({'reason':reason,'results':summary['results'],'uses_count':summary['uses_count']},ensure_ascii=False)
        db.execute("UPDATE promo_polls SET active=0,closed_at=CURRENT_TIMESTAMP,result_json=? WHERE id=?",(payload,poll_id))
        db.execute("UPDATE promo_codes SET active=0 WHERE poll_id=?",(poll_id,))
        summary['active']=False
    summary['reason']=reason
    return summary


def _notify_promo_poll_result(summary):
    if not summary or not summary.get('created_by'):return
    winner=summary.get('results',[{}])[0] if summary.get('results') else {}
    lines=[f"📊 <b>Опрос «{escape(str(summary.get('title') or 'Опрос'))}» завершён</b>",
           f"Всего голосов: <b>{int(summary.get('uses_count') or 0)}</b>"]
    for item in summary.get('results') or []:
        lines.append(f"• {escape(str(item.get('name') or 'Вариант'))}: <b>{int(item.get('votes') or 0)}</b> · {float(item.get('percent') or 0):g}%")
    if winner:
        lines.append(f"\nЛидер: <b>{escape(str(winner.get('name') or '—'))}</b>")
    try: notify_user_async(int(summary['created_by']),'\n'.join(lines),None,'HTML')
    except Exception: app.logger.exception('Poll result notification failed')


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
        if int(promo['author_user_id'] or 0) == int(session['uid']):
            return error('Автор не может активировать собственный промокод.', 403)
        if (int(promo['assigned_user_id'] or 0) not in (0, int(session['uid']))
                or (str(promo['source_label'] or '') == 'Компенсация Upgrade'
                    and int(promo['assigned_user_id'] or 0) != int(session['uid']))):
            return error('Этот промокод предназначен другому пользователю.', 403)
        if promo_is_expired(promo):
            return error('Срок действия промокода истёк.', 409)
        poll_id=str(promo['poll_id'] or '')
        poll=None
        poll_notify=None
        if poll_id:
            poll=db.execute('SELECT * FROM promo_polls WHERE id=?' + (' FOR UPDATE' if DATABASE_URL else ''),(poll_id,)).fetchone()
            if not poll or not poll['active']:
                return error('Этот опрос уже завершён.',409)
            if poll['expires_at']:
                try:
                    poll_expiry=datetime.fromisoformat(str(poll['expires_at']).replace('Z','+00:00'))
                    if poll_expiry.tzinfo is None:poll_expiry=poll_expiry.replace(tzinfo=timezone.utc)
                except (TypeError,ValueError):
                    poll_expiry=None
                if poll_expiry and poll_expiry <= datetime.now(timezone.utc):
                    poll_notify=_promo_poll_close(db,poll_id,'expired')
                    db.commit()
                    _notify_promo_poll_result(poll_notify)
                    return error('Этот опрос уже завершён.',409)
            previous_vote=db.execute('SELECT option_name FROM promo_poll_votes WHERE poll_id=? AND user_id=?',(poll_id,session['uid'])).fetchone()
            if previous_vote:
                return error(f"Вы уже проголосовали за «{previous_vote['option_name']}». В этом опросе можно выбрать только один вариант.",409)
            if int(poll['max_votes'] or 0)>0 and int(poll['uses_count'] or 0)>=int(poll['max_votes']):
                poll_notify=_promo_poll_close(db,poll_id,'limit')
                db.commit()
                _notify_promo_poll_result(poll_notify)
                return error('Лимит голосов этого опроса уже достигнут.',409)
        prior=db.execute('SELECT * FROM promo_redemptions WHERE code=? AND user_id=?',(code,session['uid'])).fetchone()
        if prior:return error('Вы уже активировали этот промокод.',409)
        if promo['max_uses'] > 0 and promo['uses_count'] >= promo['max_uses']:
            return error('Лимит активаций этого промокода исчерпан.', 409)
        activation_min_deposit=int(promo['activation_min_deposit'] or 0)
        if activation_min_deposit:
            deposited=confirmed_deposit_total(db,session['uid'])
            if deposited < activation_min_deposit:
                return error(f'Для активации нужен подтверждённый депозит от {activation_min_deposit/100:.2f} TON. У вас: {deposited/100:.2f} TON.',409)
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
        poll_result=None
        if poll_id:
            option_name=str(promo['poll_option_name'] or code)
            db.execute('INSERT INTO promo_poll_votes(poll_id,user_id,code,option_name) VALUES(?,?,?,?)',
                       (poll_id,session['uid'],code,option_name))
            db.execute('UPDATE promo_polls SET uses_count=uses_count+1 WHERE id=?',(poll_id,))
            total,counts=_promo_poll_counts(db,poll_id)
            mine=next((x for x in counts if x['name']==option_name),{'votes':1,'percent':100})
            poll_result=dict(id=poll_id,title=poll['title'],option_name=option_name,total_votes=total,
                             same_opinion_percent=mine['percent'],results=counts,closed=False)
            max_votes=int(poll['max_votes'] or 0)
            if max_votes>0 and total>=max_votes:
                poll_notify=_promo_poll_close(db,poll_id,'limit')
                poll_result['closed']=True
        log_event(db,session['uid'],'promo_redeem',code=code,reward_type=promo['reward_type'],reward=reward,
                  poll_id=poll_id,poll_option=(str(promo['poll_option_name'] or '') if poll_id else ''))
        db.commit()
        if poll_notify:_notify_promo_poll_result(poll_notify)
        return jsonify(ok=True, reward=reward, user=profile(), poll=poll_result)
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
    freebet_options={}
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
            try:
                wager_attempts = int(float(str(data.get('wager_attempts') or 1).replace(',', '.')))
            except (TypeError, ValueError):
                raise ValueError('Количество жизней подарка указано неверно.')
            if not 1 <= wager_attempts <= 100:
                raise ValueError('Жизни подарка: от 1 до 100.')
            burn_pool_enabled = bool(data.get('burn_pool_enabled'))
            burn_fragment = None
            if burn_pool_enabled:
                burn_fragment_url = str(data.get('burn_fragment_url') or data.get('fragment_url') or '').strip()
                if not burn_fragment_url:
                    raise ValueError('Для сгораемого подарка укажите ссылку Fragment.')
                exact = fragment_gift_from_url(burn_fragment_url, True, allow_missing_price=True)
                if not int(exact.get('floor_price') or 0):
                    exact['floor_price'] = gift_price
                    exact['price_source'] = 'Portal · базовый подарок'
                burn_fragment = {
                    'gift_id': str(exact.get('gift_id') or ''),
                    'gift_name': str(exact.get('gift_name') or gift_name)[:140],
                    'image_url': safe_image(exact.get('image_url')) or gift_image,
                    'floor_price': int(exact.get('floor_price') or gift_price),
                    'fragment_url': str(exact.get('fragment_url') or burn_fragment_url),
                    'fragment_number': str(exact.get('fragment_number') or ''),
                    'fragment_model': str(exact.get('fragment_model') or '')[:120],
                    'fragment_backdrop': str(exact.get('fragment_backdrop') or '')[:120],
                    'fragment_symbol': str(exact.get('fragment_symbol') or '')[:120],
                    'price_source': str(exact.get('price_source') or '')[:120],
                    'animation_url': safe_image(exact.get('animation_url')),
                }
            freebet_options={
                'burn_pool_enabled':burn_pool_enabled,
                'burn_pool_count':1 if burn_pool_enabled else 0,
                'burn_fragment':burn_fragment,
                'wager_attempts':wager_attempts,
                'burn_on_loss':True,
            }
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
    reward_payload=multi_reward if multi_reward else {}
    if freebet_options:
        reward_payload=dict(reward_payload or {},freebet_options=freebet_options)
    return (reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,bonus_percent,bonus_fixed,
            min_deposit,json.dumps(reward_payload,ensure_ascii=False) if reward_payload else '{}',gift_expires_days)


@app.get('/api/admin/freebets')
@admin_required
def admin_freebets():
    with connect() as db:
        rows = db.execute("""SELECT f.*,f.min_deposit AS freebet_min_deposit,p.reward_type,p.amount,p.gift_name,p.gift_price,p.wager_multiplier,
                             p.bonus_percent,p.bonus_fixed,p.min_deposit,p.reward_json,p.gift_expires_days
                             FROM freebets f JOIN promo_codes p ON p.code=f.promo_code
                             ORDER BY f.created_at DESC""").fetchall()
        pool_rows = db.execute("""SELECT freebet_code,COUNT(*) AS total,
                                  COALESCE(SUM(CASE WHEN claimed_by IS NOT NULL THEN 1 ELSE 0 END),0) AS claimed
                                  FROM freebet_burn_prizes GROUP BY freebet_code""").fetchall()
    pools={str(r['freebet_code']):dict(total=int(r['total'] or 0),claimed=int(r['claimed'] or 0)) for r in pool_rows}
    items=[]
    for x in rows:
        promo=x
        options=freebet_options(promo)
        pool=pools.get(str(x['code']),dict(total=0,claimed=0))
        items.append(dict(code=x['code'],link=freebet_link(x['code']),active=bool(x['active']),max_uses=int(x['max_uses'] or 0),
                          uses_count=int(x['uses_count'] or 0),require_subscription=bool(x['require_subscription']),
                          min_level=int(x['min_level'] or 0),min_telegram_level=int(x['min_telegram_level'] or 0),
                          min_turnover=int(x['min_turnover'] or 0)/100,min_deposit=int(x['freebet_min_deposit'] or 0)/100,expires_at=x['expires_at'],
                          created_at=x['created_at'],reward_type=x['reward_type'],purpose=promo_purpose(promo),
                          author_user_id=int(x['author_user_id'] or 0),
                          burn_pool_enabled=bool(options.get('burn_pool_enabled')),
                          pool_total=pool['total'],pool_claimed=pool['claimed'],pool_remaining=max(0,pool['total']-pool['claimed'])))
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
        author_user_id=_fb_int(data.get('author_user_id'), 0)
        min_tg=_fb_int(data.get('min_telegram_level')); min_turnover=parse_amount(data.get('min_turnover') or 0)
        activation_min_deposit=parse_amount(data.get('activation_min_deposit') or 0)
        expires_days=_fb_int(data.get('expires_in_days'))
        values=_freebet_backing_values(data, code)
    except (ValueError,TypeError,InvalidOperation,OSError,json.JSONDecodeError) as exc:
        return error(str(exc) or 'Проверьте настройки фрибета.')
    if not 0 <= max_uses <= 1000000:return error('Лимит активаций: 0–1 000 000. 0 — без лимита.')
    if author_user_id and not creator_record(author_user_id).get('active'):
        return error('Выбранный пользователь не является активным автором.', 409)
    if min_level < 0 or min_tg < 0:return error('Минимальные уровни не могут быть отрицательными.')
    if not 0 <= activation_min_deposit <= 100000000:return error('Минимальный депозит: от 0 до 1 000 000 TON.')
    if min_level:
        with connect() as check_db:
            if not check_db.execute('SELECT 1 FROM levels WHERE level=?',(min_level,)).fetchone():
                return error('Укажите существующий уровень GemDrop.')
    if not 0 <= expires_days <= 3650:return error('Срок действия: 0–3650 дней.')
    require_subscription=1 if bool(data.get('require_subscription')) else 0
    if require_subscription and not post_channel_settings().get('chat_id'):
        return error('Сначала привяжите чат/канал в разделе Post или отключите условие участника.',409)
    expires_at=(datetime.now(timezone.utc)+timedelta(days=expires_days)).isoformat() if expires_days else None
    (reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,bonus_percent,bonus_fixed,
     min_deposit,reward_json,gift_expires_days)=values
    options=json.loads(reward_json or '{}').get('freebet_options',{}) if reward_type=='wager_gift' else {}
    burn_pool_enabled=bool(options.get('burn_pool_enabled')) if isinstance(options,dict) else False
    burn_fragment=options.get('burn_fragment') if burn_pool_enabled and isinstance(options,dict) else None
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
                    min_turnover,min_deposit,expires_at,created_by,author_user_id) VALUES(?,?,?,1,?,?,?,?,?,?,?,?)""",
                   (code,code,max_uses,require_subscription,min_level,min_tg,min_turnover,activation_min_deposit,
                    expires_at,session['uid'],author_user_id))
        if burn_pool_enabled:
            if not isinstance(burn_fragment,dict) or not burn_fragment.get('fragment_url'):
                return error('Для сгораемого подарка не сохранена ссылка Fragment.',409)
            db.execute("""INSERT INTO freebet_burn_prizes(
                          freebet_code,slot_index,gift_id,gift_name,image_url,floor_price,
                          fragment_url,fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url)
                          VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?)""",
                       (code,str(burn_fragment.get('gift_id') or ''),str(burn_fragment.get('gift_name') or gift_name),
                        safe_image(burn_fragment.get('image_url')),int(burn_fragment.get('floor_price') or gift_price),
                        str(burn_fragment.get('fragment_url') or ''),str(burn_fragment.get('fragment_number') or ''),
                        str(burn_fragment.get('fragment_model') or ''),str(burn_fragment.get('fragment_backdrop') or ''),
                        str(burn_fragment.get('fragment_symbol') or ''),str(burn_fragment.get('price_source') or ''),
                        safe_image(burn_fragment.get('animation_url'))))
        db.execute('UPDATE promo_codes SET author_user_id=? WHERE code=?', (author_user_id, code))
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
        db.execute('DELETE FROM freebet_burn_prizes WHERE freebet_code=?',(code,))
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
                               assigned_user_id=int(x['assigned_user_id'] or 0),author_user_id=int(x['author_user_id'] or 0),source=x['source_label'] or '',
                               description=x['description'] or '',expires_at=x['expires_at'],expired=promo_is_expired(x),gift_expires_days=int(x['gift_expires_days'] or 0),
                               bonus_percent=float(x['bonus_percent'] or 0),bonus_fixed=x['bonus_fixed']/100,
                               min_deposit=x['min_deposit']/100,activation_min_deposit=int(x['activation_min_deposit'] or 0)/100,purpose=promo_purpose(x),
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
        activation_min_deposit=parse_amount(data.get('activation_min_deposit') or 0)
        assigned_user_id=int(data.get('assigned_user_id') or 0)
        author_user_id=int(data.get('author_user_id') or 0)
        expires_days=int(data.get('expires_in_days') or 0)
    except (TypeError,ValueError,InvalidOperation):
        return error('Проверьте ID пользователя, срок действия и минимальный депозит.')
    if assigned_user_id < 0:return error('ID пользователя указан неверно.')
    if author_user_id < 0:return error('ID автора указан неверно.')
    if author_user_id and not creator_record(author_user_id).get('active'):
        return error('Выбранный пользователь не является активным автором.',409)
    if not 0 <= activation_min_deposit <= 100000000:return error('Минимальный депозит для активации: от 0 до 1 000 000 TON.')
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
            db.execute('INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,max_uses,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,assigned_user_id,source_label,description,expires_at,gift_expires_days,activation_min_deposit,author_user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (code, reward_type, amount, gift_id, gift_name, gift_image, gift_price,
                        wager_multiplier, max_uses, session['uid'],
                        bonus_percent,bonus_fixed,min_deposit,
                        json.dumps(multi_reward,ensure_ascii=False) if multi_reward else '{}',
                        assigned_user_id,source_label,description,expires_at,gift_expires_days,activation_min_deposit,author_user_id))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'promo_create', code))
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return error('Такой промокод уже существует.', 409)
        raise
    if assigned_user_id:
        notify_promo_async(assigned_user_id, code, 'bonuses')
    return jsonify(ok=True, code=code)



@app.get('/api/admin/promo-polls')
@admin_required
def admin_promo_polls():
    notices=[]
    now=datetime.now(timezone.utc)
    with connect() as db:
        # expires_at is intentionally stored as TEXT for SQLite/PostgreSQL compatibility.
        # Do not compare it directly with CURRENT_TIMESTAMP in PostgreSQL: TEXT <= TIMESTAMPTZ
        # raises UndefinedFunction. Parse the ISO value in Python instead.
        active_rows=db.execute("SELECT id,expires_at FROM promo_polls WHERE active=1 AND expires_at IS NOT NULL").fetchall()
        for row in active_rows:
            expiry=parse_datetime_utc(row['expires_at'])
            if not expiry or expiry>now:
                continue
            summary=_promo_poll_close(db,row['id'],'expired')
            if summary:notices.append(summary)
        rows=db.execute('SELECT id FROM promo_polls ORDER BY created_at DESC LIMIT 100').fetchall()
        items=[_promo_poll_summary(db,row['id']) for row in rows]
        db.commit()
    for summary in notices:_notify_promo_poll_result(summary)
    return jsonify(items=[x for x in items if x])


@app.post('/api/admin/promo-polls')
@admin_required
def admin_create_promo_poll():
    data=request.get_json(silent=True) or {}
    title=str(data.get('title') or '').strip()[:120]
    options=data.get('options') if isinstance(data.get('options'),list) else []
    if not title:return error('Введите название опроса.')
    if not 2<=len(options)<=10:return error('В опросе должно быть от 2 до 10 вариантов.')
    try:
        max_votes=int(data.get('max_votes') or 0); expires_days=int(data.get('expires_in_days') or 0)
    except (TypeError,ValueError):return error('Проверьте лимит голосов и срок.')
    if not 0<=max_votes<=1000000:return error('Лимит голосов: от 0 до 1 000 000.')
    if not 0<=expires_days<=3650:return error('Срок опроса: от 0 до 3650 дней.')
    poll_id='POLL-'+secrets.token_hex(6).upper()
    expires_at=(datetime.now(timezone.utc)+timedelta(days=expires_days)).isoformat() if expires_days else None
    prepared=[];seen_codes=set();seen_names=set()
    catalog=read_catalog(include_hidden=True).get('gifts',[])
    for raw in options:
        if not isinstance(raw,dict):return error('Проверьте варианты опроса.')
        name=str(raw.get('name') or '').strip()[:80]
        code=str(raw.get('code') or '').strip().upper() or generated_promo_code()
        reward_type=str(raw.get('reward_type') or 'balance')
        if not name:return error('У каждого варианта должно быть название.')
        if name.casefold() in seen_names:return error('Названия вариантов не должны повторяться.')
        if not re.fullmatch(r'[A-Z0-9_-]{3,32}',code):return error(f'Некорректный код варианта «{name}».')
        if code in seen_codes:return error('Коды вариантов не должны повторяться.')
        seen_names.add(name.casefold());seen_codes.add(code)
        amount=0;gift_id='';gift_name='';gift_image='';gift_price=0;wager=0.0;gift_days=0
        if reward_type=='balance':
            try: amount=parse_amount(raw.get('amount'))
            except Exception:return error(f'Укажите TON-награду для «{name}».')
            if not 1<=amount<=100000000:return error(f'Некорректная TON-награда для «{name}».')
        elif reward_type=='tickets':
            try: amount=int(raw.get('tickets') or raw.get('amount') or 0)
            except Exception:return error(f'Укажите билеты для «{name}».')
            if not 1<=amount<=1000000:return error(f'Некорректное число билетов для «{name}».')
        elif reward_type in ('gift','wager_gift'):
            gift_id=str(raw.get('gift_id') or '')
            gift=next((g for g in catalog if str(g.get('id'))==gift_id),None)
            if not gift:return error(f'Подарок для «{name}» не найден в Portal.')
            gift_name=str(gift.get('name') or 'Подарок')[:140]
            gift_image=safe_image(gift.get('image_url') or gift.get('portal_image_url'))
            try: gift_price=ton_to_cents(gift.get('price_ton') or 0)
            except Exception: gift_price=0
            if gift_price<=0:return error(f'У подарка для «{name}» нет актуальной цены.')
            if reward_type=='wager_gift':
                try:wager=float(raw.get('wager_multiplier') or 0);gift_days=int(raw.get('gift_expires_days') or 0)
                except Exception:return error(f'Проверьте X отыгрыша для «{name}».')
                if not 1<=wager<=1000:return error(f'X отыгрыша для «{name}» должен быть 1–1000.')
        else:return error('В опросах доступны TON, билеты, подарок и отыгрышный подарок.')
        prepared.append((code,name,reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager,gift_days))
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for code,*_ in prepared:
                if db.execute('SELECT 1 FROM promo_codes WHERE code=?',(code,)).fetchone():
                    return error(f'Промокод {code} уже существует.',409)
            db.execute('INSERT INTO promo_polls(id,title,max_votes,expires_at,created_by) VALUES(?,?,?,?,?)',
                       (poll_id,title,max_votes,expires_at,session['uid']))
            for code,name,reward_type,amount,gid,gname,gimg,gprice,wager,gift_days in prepared:
                db.execute("""INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,
                           wager_multiplier,max_uses,created_by,reward_json,source_label,description,expires_at,
                           gift_expires_days,poll_id,poll_option_name)
                           VALUES(?,?,?,?,?,?,?,?,0,?,'{}','Опрос','',?,?,?,?)""",
                           (code,reward_type,amount,gid,gname,gimg,gprice,wager,session['uid'],expires_at,gift_days,poll_id,name))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'],session['uid'],'promo_poll_create',poll_id+':'+title))
            db.commit()
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():return error('Один из кодов уже существует.',409)
        raise
    with connect() as db:
        created=_promo_poll_summary(db,poll_id)
    return jsonify(ok=True,poll=created)


@app.post('/api/admin/promo-polls/<poll_id>/close')
@admin_required
def admin_close_promo_poll(poll_id):
    with connect() as db:
        summary=_promo_poll_close(db,str(poll_id),'manual')
        if not summary:return error('Опрос не найден.',404)
        db.commit()
    _notify_promo_poll_result(summary)
    return jsonify(ok=True,poll=summary)


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




def youtube_channel_ref(value):
    """Understands every common way of writing a channel: @handle, /channel/UC..., /c/name, /user/name,
    m./music. hosts, links without https://, trailing /videos|/about|?si=..., percent-encoded handles."""
    from urllib.parse import unquote
    value = unquote(str(value or '').strip())
    if not value:
        raise ValueError('Вставьте ссылку на YouTube-канал.')
    if len(value) > 500:
        raise ValueError('Ссылка на YouTube слишком длинная.')
    if re.fullmatch(r'UC[A-Za-z0-9_-]{20,}', value):
        return 'id', value
    if re.fullmatch(r'@?[\w.\-]{3,100}', value) and not re.search(r'youtu', value, re.I):
        return 'handle', value.lstrip('@')
    m = re.search(r'(?:^|//|www\.|m\.|music\.)youtube\.com/channel/(UC[A-Za-z0-9_-]{20,})', value, re.I)
    if m:
        return 'id', m.group(1)
    m = re.search(r'youtube\.com/@([^/?#\s]{3,100})', value, re.I)
    if m:
        return 'handle', m.group(1)
    m = re.search(r'youtube\.com/(c|user)/([^/?#\s]{2,100})', value, re.I)
    if m:
        return 'path', m.group(1).lower() + '/' + m.group(2)
    if re.search(r'(youtu\.be/|youtube\.com/(watch|shorts|live|embed))', value, re.I):
        raise ValueError('Это ссылка на видео. Вставьте ссылку именно на канал: https://youtube.com/@channel')
    raise ValueError('Используйте ссылку вида https://youtube.com/@channel или https://youtube.com/channel/UC…')


def youtube_api_get(path, params):
    if not YOUTUBE_API_KEY:
        raise RuntimeError('YouTube API key не настроен.')
    payload = dict(params or {})
    payload['key'] = YOUTUBE_API_KEY
    try:
        response = requests.get('https://www.googleapis.com/youtube/v3/' + path,
                                params=payload, timeout=(4, 14))
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError('YouTube временно недоступен. Попробуйте позже.') from exc
    if not isinstance(data, dict):
        raise RuntimeError('YouTube вернул некорректный ответ.')
    return data


YOUTUBE_PUBLIC_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/124.0 Safari/537.36'),
    'Accept-Language': 'en-US,en;q=0.9',
}


def youtube_public_get(url, params=None):
    try:
        response = requests.get(url, params=params or None, headers=YOUTUBE_PUBLIC_HEADERS,
                                timeout=(4, 14), allow_redirects=True)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise RuntimeError('Не удалось получить публичные данные YouTube. Попробуйте позже.') from exc


def youtube_meta_content(page, key):
    escaped = re.escape(str(key))
    patterns = [
        rf'<meta[^>]+(?:property|name|itemprop)=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\']{escaped}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            return unescape(match.group(1)).strip()
    return ''


def youtube_human_count(label):
    value = str(label or '').replace('\\u00a0', ' ').replace('\\u202f', ' ').strip()
    match = re.search(r'([0-9]+(?:[.,][0-9]+)?)\s*([KMB])\b', value, re.I)
    if match:
        number = float(match.group(1).replace(',', '.'))
        multiplier = {'K': 1000, 'M': 1000000, 'B': 1000000000}[match.group(2).upper()]
        return int(round(number * multiplier))
    plain = re.search(r'([0-9][0-9,\s]*)\s+subscribers?\b', value, re.I)
    if plain:
        digits = re.sub(r'\D', '', plain.group(1))
        return int(digits) if digits else None
    return None


def youtube_public_subscribers(page):
    patterns = [
        r'"subscriberCountText":\{"simpleText":"([^"]+)"',
        r'"subscriberCountText":\{"runs":\[\{"text":"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, page)
        if match:
            count = youtube_human_count(match.group(1))
            if count is not None:
                return count
    return None


def youtube_public_channel_id(page):
    """Only identifiers that describe THIS page's channel (never recommended/featured channels)."""
    patterns = [
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']https?://(?:www\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{20,})',
        r'"externalId":"(UC[A-Za-z0-9_-]{20,})"',
        r'<meta[^>]+itemprop=["\'](?:channelId|identifier)["\'][^>]+content=["\'](UC[A-Za-z0-9_-]{20,})["\']',
        r'<meta[^>]+content=["\'](UC[A-Za-z0-9_-]{20,})["\'][^>]+itemprop=["\']channelId["\']',
        r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{20,})',
    ]
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            return match.group(1)
    return ''


def youtube_public_handle(page, fallback=''):
    match = re.search(r'"vanityChannelUrl":"https?://(?:www\.)?youtube\.com/@([^"\\]+)', page, re.I)
    if match:
        return '@' + unescape(match.group(1)).strip().lstrip('@')
    match = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']https?://(?:www\.)?youtube\.com/@([^"\']+)', page, re.I)
    if match:
        return '@' + unescape(match.group(1)).strip().lstrip('@')
    return ('@' + str(fallback).lstrip('@')) if fallback else ''


def youtube_public_gemdrop_videos(channel_id):
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{20,}', str(channel_id or '')):
        return []
    response = youtube_public_get('https://www.youtube.com/feeds/videos.xml',
                                  {'channel_id': channel_id})
    try:
        root = ET.fromstring(response.content)
    except (ET.ParseError, TypeError, ValueError) as exc:
        raise RuntimeError('YouTube вернул некорректную ленту канала.') from exc
    ns = {
        'atom': 'http://www.w3.org/2005/Atom',
        'yt': 'http://www.youtube.com/xml/schemas/2015',
        'media': 'http://search.yahoo.com/mrss/',
    }
    matches = []
    for entry in root.findall('atom:entry', ns):
        title = str(entry.findtext('atom:title', default='', namespaces=ns) or '')
        group = entry.find('media:group', ns)
        description = ''
        thumbnail_url = ''
        views = 0
        if group is not None:
            description = str(group.findtext('media:description', default='', namespaces=ns) or '')
            thumbnail = group.find('media:thumbnail', ns)
            if thumbnail is not None:
                thumbnail_url = str(thumbnail.attrib.get('url') or '')
            community = group.find('media:community', ns)
            statistics = community.find('media:statistics', ns) if community is not None else None
            if statistics is not None:
                try:
                    views = max(0, int(statistics.attrib.get('views') or 0))
                except (TypeError, ValueError):
                    views = 0
        if 'gemdrop' not in title.casefold() and '#gemdrop' not in description.casefold():
            continue
        video_id = str(entry.findtext('yt:videoId', default='', namespaces=ns) or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{6,20}', video_id):
            continue
        link = entry.find('atom:link[@rel="alternate"]', ns)
        video_url = str(link.attrib.get('href') or '') if link is not None else ''
        matches.append(dict(
            video_id=video_id,
            title=title or 'Видео',
            thumbnail_url=safe_image(thumbnail_url),
            published_at=str(entry.findtext('atom:published', default='', namespaces=ns) or ''),
            views=views,
            url=video_url or ('https://www.youtube.com/watch?v=' + video_id),
        ))
        if len(matches) >= 20:
            break
    return matches


def youtube_public_channel_snapshot(value):
    kind, ref = youtube_channel_ref(value)
    page_url = ('https://www.youtube.com/channel/' + ref if kind == 'id' else
                'https://www.youtube.com/' + ref if kind == 'path' else
                'https://www.youtube.com/@' + ref)
    response = youtube_public_get(page_url, {'hl': 'en'})
    page = response.text or ''
    channel_id = ref if kind == 'id' else youtube_public_channel_id(page)
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{20,}', channel_id or ''):
        raise ValueError('YouTube-канал не найден или YouTube временно не отдал публичные данные.')
    title = youtube_meta_content(page, 'og:title')
    if not title:
        title_match = re.search(r'<title>(.*?)</title>', page, re.I | re.S)
        title = unescape(title_match.group(1)).strip() if title_match else 'YouTube'
        title = re.sub(r'\s*-\s*YouTube\s*$', '', title, flags=re.I)
    avatar = youtube_meta_content(page, 'og:image')
    handle = youtube_public_handle(page, ref if kind == 'handle' else '')
    subscribers = youtube_public_subscribers(page)
    snapshot = dict(
        channel_id=channel_id,
        url=('https://www.youtube.com/' + handle) if handle else ('https://www.youtube.com/channel/' + channel_id),
        title=str(title or 'YouTube'),
        handle=handle,
        avatar_url=safe_image(avatar),
        subscribers=subscribers,
        subscriber_count_hidden=subscribers is None,
        uploads_playlist='UU' + channel_id[2:],
        updated_at=datetime.now(timezone.utc).isoformat(),
        source='public',
    )
    try:
        snapshot['videos'] = youtube_public_gemdrop_videos(channel_id)
    except RuntimeError:
        # A channel without uploads (or a flaky RSS feed) must not block linking the channel itself.
        snapshot['videos'] = []
    return snapshot


def youtube_api_channel_snapshot(value):
    kind, ref = youtube_channel_ref(value)
    if kind == 'path':
        raise RuntimeError('Legacy /c/ and /user/ links are resolved from the public page.')
    params = {'part': 'snippet,statistics,contentDetails'}
    params['id' if kind == 'id' else 'forHandle'] = ref
    data = youtube_api_get('channels', params)
    items = data.get('items') or []
    if not items:
        raise ValueError('YouTube-канал не найден.')
    channel = items[0]
    snippet = channel.get('snippet') or {}
    stats = channel.get('statistics') or {}
    uploads = ((channel.get('contentDetails') or {}).get('relatedPlaylists') or {}).get('uploads') or ''
    channel_id = str(channel.get('id') or '')
    thumbs = snippet.get('thumbnails') or {}
    avatar = ((thumbs.get('high') or thumbs.get('medium') or thumbs.get('default') or {}).get('url') or '')
    snapshot = dict(
        channel_id=channel_id,
        url='https://www.youtube.com/channel/' + channel_id if channel_id else str(value),
        title=str(snippet.get('title') or 'YouTube'),
        handle=str(snippet.get('customUrl') or ''),
        avatar_url=safe_image(avatar),
        subscribers=int(stats.get('subscriberCount') or 0) if not bool(stats.get('hiddenSubscriberCount')) else None,
        subscriber_count_hidden=bool(stats.get('hiddenSubscriberCount')),
        uploads_playlist=str(uploads),
        updated_at=datetime.now(timezone.utc).isoformat(),
        source='api',
    )
    try:
        snapshot['videos'] = youtube_api_gemdrop_videos(snapshot)
    except RuntimeError:
        snapshot['videos'] = []
    return snapshot


def youtube_channel_snapshot(value):
    if YOUTUBE_API_KEY:
        try:
            return youtube_api_channel_snapshot(value)
        except (RuntimeError, ValueError):
            # Quota, invalid/expired key or a temporary Google API problem must not
            # break the creator program. Public channel data is enough for GemDrop.
            pass
    return youtube_public_channel_snapshot(value)


def youtube_api_gemdrop_videos(channel):
    playlist_id = str((channel or {}).get('uploads_playlist') or '')
    if not playlist_id:
        return []
    data = youtube_api_get('playlistItems', {
        'part': 'snippet,contentDetails', 'playlistId': playlist_id, 'maxResults': 50
    })
    matches = []
    for item in data.get('items') or []:
        snippet = item.get('snippet') or {}
        title = str(snippet.get('title') or '')
        description = str(snippet.get('description') or '')
        if 'gemdrop' not in title.casefold() and '#gemdrop' not in description.casefold():
            continue
        video_id = str((item.get('contentDetails') or {}).get('videoId') or
                       (snippet.get('resourceId') or {}).get('videoId') or '')
        if not video_id:
            continue
        thumbs = snippet.get('thumbnails') or {}
        thumb = ((thumbs.get('high') or thumbs.get('medium') or thumbs.get('default') or {}).get('url') or '')
        matches.append(dict(video_id=video_id, title=title, thumbnail_url=safe_image(thumb),
                            published_at=snippet.get('publishedAt') or '', views=0,
                            url='https://www.youtube.com/watch?v=' + video_id))
        if len(matches) >= 20:
            break
    if not matches:
        return []
    details = youtube_api_get('videos', {
        'part': 'statistics', 'id': ','.join(x['video_id'] for x in matches)
    })
    views = {str(x.get('id') or ''): int((x.get('statistics') or {}).get('viewCount') or 0)
             for x in details.get('items') or []}
    for item in matches:
        item['views'] = views.get(item['video_id'], 0)
    return matches


@app.get('/api/admin/creators')
@admin_required
def admin_creators():
    term = str(request.args.get('q') or '').strip()[:80]
    with connect() as db:
        rows = db.execute("SELECT name,payload FROM app_documents WHERE name LIKE 'creator:%' ORDER BY name").fetchall()
        result = []
        for row in rows:
            try:
                uid = int(str(row['name']).split(':', 1)[1])
                record = json.loads(row['payload'] or '{}')
            except (ValueError, TypeError, json.JSONDecodeError, IndexError):
                continue
            if not isinstance(record, dict) or not record.get('active'):
                continue
            user = db.execute('SELECT id,name,username,photo_url FROM users WHERE id=?', (uid,)).fetchone()
            if not user:
                continue
            hay = f"{user['id']} {user['name']} {user['username']}".casefold()
            if term and term.casefold() not in hay:
                continue
            result.append(dict(
                id=int(user['id']), name=user['name'], username=user['username'] or '',
                photo_url=user['photo_url'] or '', demo_enabled=bool(record.get('demo_enabled')),
                creator_level=creator_level_key(record.get('creator_level')),
                creator_level_info=creator_level_public(record.get('creator_level')),
                panel_hidden=bool(record.get('panel_hidden')),
                youtube_title=str((record.get('youtube') or {}).get('title') or ''),
                demo_balance=max(0, int(record.get('demo_balance_cents') or 0))/100,
                demo_gifts=len(record.get('demo_inventory') or []),
                limit_usage=creator_bonus_usage(db, uid, creator_record(uid)),
            ))
    return jsonify(items=result)


@app.post('/api/admin/creators/<int:user_id>')
@admin_required
def admin_creator_add(user_id):
    with connect() as db:
        user = db.execute('SELECT id,name,username,photo_url FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return error('Пользователь не найден.', 404)
    previous = creator_record(user_id)
    record = save_creator_record(user_id, {'active': True, 'panel_hidden': False,
                                            'creator_level': previous.get('creator_level') or 'base'})
    if not previous.get('active'):
        notify_user_async(
            user_id,
            '🎬 <b>Вы подключены к программе авторов GemDrop.</b>\n\n'
            'В профиле появилась отдельная «Панель автора». Через неё можно включать demo-режим '
            'и управлять демонстрационным балансом и подарками.',
            miniapp_markup('Открыть программу', 'creator'),
            'HTML')
    return jsonify(ok=True, creator=dict(id=user_id, name=user['name'], username=user['username'] or '',
                                         demo_enabled=record['demo_enabled'],
                                         creator_level=record.get('creator_level') or 'base',
                                         demo_balance=record['demo_balance_cents']/100))


@app.post('/api/admin/creators/<int:user_id>/level')
@admin_required
def admin_creator_level(user_id):
    data = request.get_json(silent=True) or {}
    raw_level = str(data.get('level') or '').strip().lower()
    if raw_level not in CREATOR_LEVELS:
        return error('Выберите Base, Creator, Super Creator или God.')
    previous = creator_record(user_id)
    if not previous.get('active'):
        return error('Пользователь не является активным автором.', 404)
    old = creator_level_key(previous.get('creator_level'))
    order = {'base': 0, 'creator': 1, 'super_creator': 2, 'god': 3}
    promoted = order.get(raw_level, 0) > order.get(old, 0)
    changes = {'creator_level': raw_level}
    if promoted:
        # A promotion immediately grants a fresh quota for the new tier.
        changes['creator_limit_reset_at'] = datetime.now(timezone.utc).isoformat()
    record = save_creator_record(user_id, changes)
    if old != raw_level:
        cfg = CREATOR_LEVELS[raw_level]
        refill = (f'\n\n✅ <b>Лимит восстановлен.</b> Шкала: 0 / {cfg["daily_budget_cents"]/100:.2f} TON.'
                  if promoted else '')
        notify_user_async(
            user_id,
            f'✨ <b>Уровень автора изменён</b>\n\n'
            f'{escape(CREATOR_LEVELS[old]["name"])} → <b>{escape(CREATOR_LEVELS[raw_level]["name"])}</b>\n'
            f'{escape(CREATOR_LEVELS[raw_level]["description"])}{refill}',
            miniapp_markup('Открыть панель автора', 'creator'),
            'HTML')
    return jsonify(ok=True, creator_level=record['creator_level'],
                   creator_level_info=creator_level_public(record['creator_level']))


@app.post('/api/admin/creators/<int:user_id>/restore-limit')
@admin_required
def admin_creator_restore_limit(user_id):
    """Restore the creator's daily scale: fully (100%) or by a percentage of the daily limit."""
    data = request.get_json(silent=True) or {}
    try:
        percent = float(str(data.get('percent', 100)).replace(',', '.'))
    except (TypeError, ValueError):
        return error('Укажите процент от 1 до 100.')
    if not math.isfinite(percent) or not 0 < percent <= 100:
        return error('Укажите процент от 1 до 100.')
    record = creator_record(user_id)
    if not record.get('active'):
        return error('Пользователь не является активным автором.', 404)
    cfg = CREATOR_LEVELS[creator_level_key(record.get('creator_level'))]
    if percent >= 100:
        changes = {'creator_limit_reset_at': datetime.now(timezone.utc).isoformat(),
                   'creator_limit_credit': {}}
    else:
        _, _, day = creator_limit_window(record)
        old = record.get('creator_limit_credit') or {}
        if old.get('day') != day:
            old = {}
        budget_add = int(round(int(cfg['daily_budget_cents']) * percent / 100))
        wager_add = int(math.ceil(int(cfg['wager_daily_limit']) * percent / 100)) if cfg['wager_daily_limit'] else 0
        code_add = int(math.ceil(int(cfg['daily_code_limit']) * percent / 100)) if cfg['daily_code_limit'] else 0
        changes = {'creator_limit_credit': dict(
            day=day,
            budget_cents=int(old.get('budget_cents') or 0) + budget_add,
            wager_count=int(old.get('wager_count') or 0) + wager_add,
            code_count=int(old.get('code_count') or 0) + code_add)}
    save_creator_record(user_id, changes)
    with connect() as db:
        usage = creator_bonus_usage(db, user_id)
    total = cfg['daily_budget_cents'] / 100
    notify_user_async(
        user_id,
        '✅ <b>Лимит восстановлен.</b>\n\n'
        + ('Шкала восстановлена полностью. ' if percent >= 100 else f'Шкала восстановлена на {percent:g}%. ')
        + f'Сейчас: {usage["budget_used"]:.2f} / {total:.2f} TON.',
        miniapp_markup('Открыть панель автора', 'creator'),
        'HTML')
    return jsonify(ok=True, percent=percent, limit_usage=usage)


@app.delete('/api/admin/creators/<int:user_id>')
@admin_required
def admin_creator_remove(user_id):
    previous = creator_record(user_id)
    save_creator_record(user_id, {'active': False, 'demo_enabled': False, 'panel_hidden': False})
    if previous.get('active'):
        notify_user_async(
            user_id,
            'К сожалению, вы были отключены от программы авторов GemDrop.',
            None,
            'HTML')
    return jsonify(ok=True)


def creator_required(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        uid = session.get('uid')
        if not uid or not creator_record(uid).get('active'):
            return error('Панель автора недоступна.', 403)
        return fn(*args, **kwargs)
    return decorated



def creator_limit_window(record=None, now_utc=None):
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_now = now_utc.astimezone(DAILY_TOP_TZ)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    reset_at = parse_datetime_utc((record or {}).get('creator_limit_reset_at'))
    if reset_at and start_utc < reset_at < end_utc:
        start_utc = reset_at
    return start_utc, end_utc, local_now.strftime('%Y-%m-%d')


def creator_bonus_usage(db, user_id, record=None):
    record = record or creator_record(user_id)
    start_utc, end_utc, day = creator_limit_window(record)
    rows = db.execute("""SELECT reward_type,amount,max_uses,source_label,created_at
                         FROM promo_codes
                         WHERE author_user_id=? AND created_at>=? AND created_at<?""",
                      (int(user_id), _daily_top_db_string(start_utc), _daily_top_db_string(end_utc))).fetchall()
    budget = 0
    wager_count = 0
    for row in rows:
        if row['reward_type'] == 'balance':
            budget += max(0, int(row['amount'] or 0)) * max(1, int(row['max_uses'] or 1))
        elif row['reward_type'] == 'wager_gift':
            wager_count += 1
    code_count = len(rows)
    # Partial restore from the admin panel: credit is valid only for the current day.
    credit = record.get('creator_limit_credit') or {}
    if isinstance(credit, dict) and credit.get('day') == day:
        try:
            budget = max(0, budget - max(0, int(credit.get('budget_cents') or 0)))
            wager_count = max(0, wager_count - max(0, int(credit.get('wager_count') or 0)))
            code_count = max(0, code_count - max(0, int(credit.get('code_count') or 0)))
        except (TypeError, ValueError):
            pass
    return dict(day=day, budget_used=budget/100, code_count=code_count, wager_count=wager_count,
                reset_at=start_utc.isoformat())


def creator_bonus_builder_state(user_id):
    record = creator_record(user_id)
    level = creator_level_key(record.get('creator_level'))
    cfg = CREATOR_LEVELS[level]
    with connect() as db:
        usage = creator_bonus_usage(db, user_id, record)
    gifts = []
    if cfg['wager_daily_limit']:
        for gift in read_catalog().get('gifts', []):
            try:
                cents = ton_to_cents(gift.get('price_ton') or 0)
            except (ValueError, TypeError, InvalidOperation):
                continue
            if cfg['wager_gift_min_cents'] <= cents <= cfg['wager_gift_max_cents']:
                gifts.append(dict(id=str(gift.get('id') or ''), name=str(gift.get('name') or 'Подарок'),
                                  image_url=safe_image(gift.get('image_url') or gift.get('portal_image_url')),
                                  price_ton=cents/100))
    return dict(level=level, level_info=creator_level_public(level), usage=usage, gifts=gifts)


@app.get('/api/creator/bonus-builder')
@login_required
@creator_required
def creator_bonus_builder():
    return jsonify(**creator_bonus_builder_state(session['uid']))


@app.post('/api/creator/bonus-builder')
@login_required
@creator_required
def creator_create_bonus():
    uid = int(session['uid'])
    data = request.get_json(silent=True) or {}
    kind = str(data.get('kind') or '').strip().lower()
    reward_type = str(data.get('reward_type') or 'balance').strip().lower()
    if kind not in ('freebet', 'promocode'):
        return error('Выберите Freebet или промокод.')
    if reward_type not in ('balance', 'wager_gift'):
        return error('Авторам доступны TON или отыгрышный подарок.')
    code = str(data.get('code') or '').strip().upper()
    if code and not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        return error('Код: 3–32 символа, только A-Z, 0-9, _ и -. Можно оставить поле пустым — код сгенерируется автоматически.')
    try:
        max_uses = int(data.get('max_uses') or 1)
    except (TypeError, ValueError):
        return error('Проверьте количество активаций.')
    if not 1 <= max_uses <= 1000:
        return error('Количество активаций: от 1 до 1000.')
    record = creator_record(uid)
    level = creator_level_key(record.get('creator_level'))
    cfg = CREATOR_LEVELS[level]
    activation_min_deposit = int(cfg['activation_min_deposit_cents'])
    if cfg.get('custom_deposit'):
        # Super Creator chooses the condition: empty/0 = no deposit required, or any minimum.
        raw_deposit = data.get('min_deposit')
        if raw_deposit in (None, ''):
            raw_deposit = 0
        try:
            activation_min_deposit = parse_amount(raw_deposit)
        except (ValueError, TypeError, InvalidOperation):
            return error('Минимальный депозит укажите числом с точностью до 0.01 (0 — без депозита).')
        if not 0 <= activation_min_deposit <= 100000000:
            return error('Минимальный депозит: от 0 до 1 000 000 TON.')
    amount = 0
    gift_id = gift_name = gift_image = ''
    gift_price = 0
    wager_multiplier = 0.0
    if reward_type == 'balance':
        try:
            amount = parse_amount(data.get('amount'))
        except (ValueError, TypeError, InvalidOperation):
            return error('Укажите сумму TON с точностью до 0.01.')
        if amount < 1:
            return error('Минимальная награда — 0.01 TON.')
    else:
        if cfg['wager_daily_limit'] <= 0:
            return error('Отыгрышные подарки доступны с уровня Creator.', 403)
        if max_uses > int(cfg['wager_max_uses']):
            return error(f'Для вашего уровня максимум {cfg["wager_max_uses"]} активаций.')
        gift_id = str(data.get('gift_id') or '')
        gift = next((g for g in read_catalog().get('gifts', []) if str(g.get('id') or '') == gift_id), None)
        if not gift:
            return error('Подарок не найден в каталоге Portal.')
        try:
            gift_price = ton_to_cents(gift.get('price_ton') or 0)
            wager_multiplier = float(str(data.get('wager_multiplier') or 0).replace(',', '.'))
        except (ValueError, TypeError, InvalidOperation):
            return error('Проверьте подарок и X отыгрыша.')
        if not cfg['wager_gift_min_cents'] <= gift_price <= cfg['wager_gift_max_cents']:
            return error(f'Для уровня {cfg["name"]} подарок должен стоить '
                         f'{cfg["wager_gift_min_cents"]/100:.0f}–{cfg["wager_gift_max_cents"]/100:.0f} TON.')
        if not math.isfinite(wager_multiplier) or not cfg['wager_min_x'] <= wager_multiplier <= 1000:
            return error(f'Для уровня {cfg["name"]} X отыгрыша — от {cfg["wager_min_x"]}.')
        gift_name = str(gift.get('name') or 'Подарок')[:140]
        gift_image = safe_image(gift.get('image_url') or gift.get('portal_image_url'))
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        usage = creator_bonus_usage(db, uid)
        if not code:
            prefix = 'FB' if kind == 'freebet' else 'CR'
            for _ in range(12):
                candidate = f'{prefix}_{secrets.token_hex(4).upper()}'
                exists = db.execute('SELECT 1 FROM promo_codes WHERE code=?', (candidate,)).fetchone() or db.execute(
                    'SELECT 1 FROM freebets WHERE code=?', (candidate,)).fetchone()
                if not exists:
                    code = candidate
                    break
            if not code:
                return error('Не удалось автоматически создать уникальный код. Попробуйте ещё раз.', 503)
        if db.execute('SELECT 1 FROM promo_codes WHERE code=?', (code,)).fetchone() or db.execute(
                'SELECT 1 FROM freebets WHERE code=?', (code,)).fetchone():
            return error('Такой код уже существует.', 409)
        if cfg['daily_code_limit'] and int(usage['code_count']) >= int(cfg['daily_code_limit']):
            return error(f'На уровне {cfg["name"]} можно создать только 1 код в день.', 409)
        if reward_type == 'balance':
            cost = amount * max_uses
            remaining = max(0, int(cfg['daily_budget_cents']) - int(round(float(usage['budget_used']) * 100)))
            if cost > remaining:
                return error(f'Превышен дневной лимит. Осталось {remaining/100:.2f} TON.', 409)
        elif int(usage['wager_count']) >= int(cfg['wager_daily_limit']):
            return error(f'Дневной лимит отыгрышных подарков: {cfg["wager_daily_limit"]}.', 409)
        source = 'Freebet' if kind == 'freebet' else 'Creator'
        active = 0 if kind == 'freebet' else 1
        # Keep the real activation cap on the backing promo as well. Besides making
        # the record self-describing, this prevents a Freebet from under-counting
        # its potential TON cost in the creator daily budget audit.
        promo_max_uses = max_uses
        db.execute("""INSERT INTO promo_codes(
                    code,reward_type,amount,gift_id,gift_name,gift_image_url,gift_price,wager_multiplier,
                    max_uses,uses_count,active,created_by,bonus_percent,bonus_fixed,min_deposit,reward_json,
                    assigned_user_id,source_label,description,expires_at,gift_expires_days,
                    activation_min_deposit,author_user_id)
                    VALUES(?,?,?,?,?,?,?,?,?,0,?,?,0,0,0,'{}',0,?,'',NULL,0,?,?)""",
                   (code,reward_type,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,
                    promo_max_uses,active,uid,source,activation_min_deposit,uid))
        if kind == 'freebet':
            db.execute("""INSERT INTO freebets(
                        code,promo_code,max_uses,uses_count,active,require_subscription,min_level,min_telegram_level,
                        min_turnover,min_deposit,expires_at,created_by,author_user_id)
                        VALUES(?,?,?,0,1,0,0,0,0,?,NULL,?,?)""",
                       (code,code,max_uses,activation_min_deposit,uid,uid))
        db.commit()
    except Exception as exc:
        try:
            db.connection.rollback() if DATABASE_URL else db.execute('ROLLBACK')
        except Exception:
            pass
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return error('Такой код уже существует.', 409)
        raise
    finally:
        db.close()
    state = creator_bonus_builder_state(uid)
    return jsonify(ok=True, code=code, kind=kind,
                   link=(freebet_link(code) if kind == 'freebet' else ''),
                   condition_min_deposit=activation_min_deposit/100, **state)


@app.get('/api/creator/state')
@login_required
@creator_required
def creator_state():
    record = creator_record(session['uid'])
    with connect() as db:
        creator_usage = creator_bonus_usage(db, session['uid'])
    return jsonify(
        creator_level=record.get('creator_level') or 'base',
        creator_level_info=creator_level_public(record.get('creator_level')),
        creator_bonus_usage=creator_usage,
        demo_enabled=record['demo_enabled'],
        panel_hidden=record['panel_hidden'],
        demo_balance=record['demo_balance_cents']/100,
        demo_turnover=record['demo_turnover_cents']/100,
        demo_tickets=record['demo_tickets'],
        demo_inventory=record['demo_inventory'],
        youtube=record.get('youtube') or {},
        youtube_pending=record.get('youtube_pending') or {},
        youtube_configured=True,
        youtube_mode=('api' if YOUTUBE_API_KEY else 'public'),
    )



def youtube_taken_by_other(channel_id, uid):
    """A channel can be linked (verified) by one creator only."""
    with connect() as db:
        rows = db.execute("SELECT name,payload FROM app_documents WHERE name LIKE 'creator:%'").fetchall()
    for row in rows:
        try:
            other = int(str(row['name']).split(':', 1)[1])
            payload = json.loads(row['payload'] or '{}')
        except (ValueError, TypeError, json.JSONDecodeError, IndexError):
            continue
        if other == int(uid) or not isinstance(payload, dict) or not payload.get('active'):
            continue
        yt = payload.get('youtube') if isinstance(payload.get('youtube'), dict) else {}
        if yt.get('channel_id') == channel_id and yt.get('verified'):
            return True
    return False


def youtube_code_present(channel_id, code):
    """True when the verification code is written on the channel (About / description / links)."""
    needle = str(code or '').strip().upper()
    if not needle or not channel_id:
        return False
    try:
        page = youtube_public_get('https://www.youtube.com/channel/' + channel_id, {'hl': 'en'}).text or ''
        if needle in page.upper():
            return True
    except RuntimeError:
        pass
    if YOUTUBE_API_KEY:
        try:
            data = youtube_api_get('channels', {'part': 'snippet', 'id': channel_id})
            items = data.get('items') or []
            if items and needle in str((items[0].get('snippet') or {}).get('description') or '').upper():
                return True
        except RuntimeError:
            pass
    return False


@app.post('/api/creator/youtube')
@login_required
@creator_required
def creator_youtube_link():
    """Step 1: resolve the channel and issue a one-time code. The channel is NOT linked yet."""
    uid = int(session['uid'])
    data = request.get_json(silent=True) or {}
    try:
        snapshot = youtube_channel_snapshot(data.get('url'))
    except ValueError as exc:
        return error(str(exc))
    except RuntimeError as exc:
        return error(str(exc), 503)
    channel_id = snapshot.get('channel_id')
    if youtube_taken_by_other(channel_id, uid):
        return error('Этот YouTube-канал уже привязан к другому автору.', 409)
    record = creator_record(uid)
    current = record.get('youtube') or {}
    if current.get('channel_id') == channel_id and current.get('verified'):
        return jsonify(ok=True, youtube=current, pending={})
    pending = record.get('youtube_pending') or {}
    code = pending.get('verify_code') if pending.get('channel_id') == channel_id else ''
    code = code or ('GD-' + secrets.token_hex(4).upper())
    snapshot['verify_code'] = code
    record = save_creator_record(uid, {'youtube_pending': snapshot})
    return jsonify(ok=True, youtube=record.get('youtube') or {}, pending=record['youtube_pending'])


@app.post('/api/creator/youtube/verify')
@login_required
@creator_required
def creator_youtube_verify():
    """Step 2: the code is found on the channel page => the channel belongs to this creator."""
    uid = int(session['uid'])
    pending = creator_record(uid).get('youtube_pending') or {}
    channel_id = pending.get('channel_id')
    code = pending.get('verify_code')
    if not channel_id or not code:
        return error('Сначала вставьте ссылку на канал и нажмите «Привязать».', 409)
    if youtube_taken_by_other(channel_id, uid):
        return error('Этот YouTube-канал уже привязан к другому автору.', 409)
    if not youtube_code_present(channel_id, code):
        return error(f'Код {code} не найден на канале. Добавьте его в описание канала '
                     '(Настройки канала → Описание), сохраните и подождите минуту.', 409)
    try:
        snapshot = youtube_channel_snapshot('https://www.youtube.com/channel/' + channel_id)
    except (ValueError, RuntimeError):
        snapshot = {k: v for k, v in pending.items() if k != 'verify_code'}
    snapshot.update(verified=True, verified_at=datetime.now(timezone.utc).isoformat())
    record = save_creator_record(uid, {'youtube': snapshot, 'youtube_pending': {}})
    return jsonify(ok=True, youtube=record.get('youtube') or {})


@app.post('/api/creator/youtube/refresh')
@login_required
@creator_required
def creator_youtube_refresh():
    current = creator_record(session['uid']).get('youtube') or {}
    ref = current.get('channel_id') or current.get('url')
    if not ref:
        return error('Сначала привяжите YouTube-канал.', 409)
    try:
        snapshot = youtube_channel_snapshot(ref)
    except (ValueError, RuntimeError) as exc:
        return error(str(exc), 503)
    if current.get('channel_id') and snapshot.get('channel_id') != current.get('channel_id'):
        return error('YouTube вернул другой канал. Данные не изменены.', 409)
    snapshot['verified'] = bool(current.get('verified'))
    if current.get('verified_at'):
        snapshot['verified_at'] = current['verified_at']
    record = save_creator_record(session['uid'], {'youtube': snapshot})
    return jsonify(ok=True, youtube=record.get('youtube') or {})


@app.delete('/api/creator/youtube')
@login_required
@creator_required
def creator_youtube_unlink():
    save_creator_record(session['uid'], {'youtube': {}, 'youtube_pending': {}})
    return jsonify(ok=True)


@app.get('/api/creator/freebets')
@login_required
@creator_required
def creator_freebets():
    with connect() as db:
        rows = db.execute("""SELECT f.*,p.reward_type,p.amount,p.gift_name,p.gift_price,p.wager_multiplier,
                             p.reward_json,p.gift_expires_days
                             FROM freebets f JOIN promo_codes p ON p.code=f.promo_code
                             WHERE f.author_user_id=? OR p.author_user_id=?
                             ORDER BY f.created_at DESC""",
                          (session['uid'],session['uid'])).fetchall()
    items=[]
    for x in rows:
        options=freebet_options(x)
        items.append(dict(
            code=x['code'], link=freebet_link(x['code']), active=bool(x['active']),
            max_uses=int(x['max_uses'] or 0), uses_count=int(x['uses_count'] or 0),
            remaining=(None if int(x['max_uses'] or 0) == 0 else max(0, int(x['max_uses'] or 0)-int(x['uses_count'] or 0))),
            reward_type=x['reward_type'], purpose=promo_purpose(x),
            wager_multiplier=float(x['wager_multiplier'] or 0),
            burn_pool_enabled=bool(options.get('burn_pool_enabled')),
            require_subscription=bool(x['require_subscription']),
            min_level=int(x['min_level'] or 0),
            min_telegram_level=int(x['min_telegram_level'] or 0),
            min_turnover=int(x['min_turnover'] or 0)/100,
            min_deposit=int(x['min_deposit'] or 0)/100,
            created_at=x['created_at'], expires_at=x['expires_at']))
    return jsonify(items=items)



@app.get('/api/creator/promocodes')
@login_required
@creator_required
def creator_promocodes():
    with connect() as db:
        rows = db.execute("""SELECT * FROM promo_codes
                             WHERE author_user_id=? AND source_label<>'Freebet'
                             ORDER BY created_at DESC""", (session['uid'],)).fetchall()
    return jsonify(items=[dict(
        code=x['code'], reward_type=x['reward_type'], purpose=promo_purpose(x),
        max_uses=int(x['max_uses'] or 0), uses_count=int(x['uses_count'] or 0),
        remaining=(None if int(x['max_uses'] or 0)==0 else max(0,int(x['max_uses'] or 0)-int(x['uses_count'] or 0))),
        active=bool(x['active']) and not promo_is_expired(x),
        expired=promo_is_expired(x), expires_at=x['expires_at'], created_at=x['created_at'],
        source=x['source_label'] or '', description=x['description'] or ''
    ) for x in rows])


def creator_chat_item(row, viewer_id):
    image_name = str(row['image_name'] or '')
    return dict(
        id=int(row['id']), user_id=int(row['user_id']), name=row['name'] or 'Автор',
        username=row['username'] or '', photo_url=row['photo_url'] or '',
        text=row['text'] or '',
        image_url=('/api/creator/chat/media/' + image_name) if image_name else '',
        created_at=row['created_at'], mine=int(row['user_id']) == int(viewer_id))


@app.get('/api/creator/chat/messages')
@login_required
@creator_required
def creator_chat_messages():
    try:
        after = max(0, int(request.args.get('after') or 0))
    except (TypeError, ValueError):
        after = 0
    with connect() as db:
        if after:
            rows = db.execute("""SELECT m.*,u.name,u.username,u.photo_url
                                 FROM creator_chat_messages m JOIN users u ON u.id=m.user_id
                                 WHERE m.id>? ORDER BY m.id ASC LIMIT 100""", (after,)).fetchall()
        else:
            rows = db.execute("""SELECT m.*,u.name,u.username,u.photo_url
                                 FROM creator_chat_messages m JOIN users u ON u.id=m.user_id
                                 ORDER BY m.id DESC LIMIT 100""").fetchall()
            rows = list(reversed(rows))
        count_row = db.execute("SELECT COUNT(*) AS n FROM app_documents WHERE name LIKE 'creator:%' AND payload LIKE '%\"active\": true%'").fetchone()
    return jsonify(items=[creator_chat_item(x, session['uid']) for x in rows],
                   author_count=int(count_row['n'] or 0) if count_row else 0)


@app.post('/api/creator/chat/messages')
@login_required
@creator_required
def creator_chat_send():
    multipart = str(request.content_type or '').startswith('multipart/form-data') or bool(request.files)
    data = request.form if multipart else (request.get_json(silent=True) or {})
    text_value = str(data.get('text') or '').strip()
    if len(text_value) > 4000:
        return error('Сообщение слишком длинное. Максимум 4000 символов.')
    photo = request.files.get('photo') if multipart else None
    image_name = ''
    if photo and getattr(photo, 'filename', ''):
        mime = str(getattr(photo, 'mimetype', '') or '').lower()
        ext = {'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp','image/gif':'.gif'}.get(mime)
        if not ext:
            return error('Поддерживаются JPG, PNG, WEBP и GIF.')
        image_name = secrets.token_hex(18) + ext
        photo.save(CREATOR_CHAT_DIR / image_name)
    if not text_value and not image_name:
        return error('Напишите сообщение или прикрепите фото.')
    with connect() as db:
        db.execute('INSERT INTO creator_chat_messages(user_id,text,image_name) VALUES(?,?,?)',
                   (session['uid'], text_value, image_name))
        row = db.execute("""SELECT m.*,u.name,u.username,u.photo_url
                            FROM creator_chat_messages m JOIN users u ON u.id=m.user_id
                            WHERE m.id=(SELECT MAX(id) FROM creator_chat_messages WHERE user_id=?)""",
                         (session['uid'],)).fetchone()
    return jsonify(ok=True, item=creator_chat_item(row, session['uid']))


@app.get('/api/creator/chat/media/<name>')
@login_required
@creator_required
def creator_chat_media(name):
    name = str(name or '')
    if not re.fullmatch(r'[a-f0-9]{36}\.(?:jpg|png|webp|gif)', name):
        return error('Файл не найден.', 404)
    path = CREATOR_CHAT_DIR / name
    if not path.is_file():
        return error('Файл не найден.', 404)
    return send_file(path, max_age=86400)


@app.post('/api/creator/panel-visibility')
@login_required
@creator_required
def creator_panel_visibility():
    data = request.get_json(silent=True) or {}
    hidden = data.get('hidden')
    if not isinstance(hidden, bool):
        return error('Передайте hidden=true/false.')
    record = save_creator_record(session['uid'], {'panel_hidden': hidden})
    return jsonify(ok=True, panel_hidden=record['panel_hidden'], user=profile())


@app.post('/api/creator/reveal')
@login_required
@creator_required
def creator_reveal_panel():
    data = request.get_json(silent=True) or {}
    code = str(data.get('code') or '').strip()
    if code != '666':
        return error('Неверный код.', 403)
    record = save_creator_record(session['uid'], {'panel_hidden': False})
    return jsonify(ok=True, panel_hidden=record['panel_hidden'], user=profile())


@app.post('/api/creator/demo-mode')
@login_required
@creator_required
def creator_demo_mode():
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return error('Передайте enabled=true/false.')
    record = save_creator_record(session['uid'], {'demo_enabled': enabled})
    return jsonify(ok=True, demo_enabled=record['demo_enabled'], user=profile())


@app.post('/api/creator/demo-balance')
@login_required
@creator_required
def creator_demo_balance():
    data = request.get_json(silent=True) or {}
    try:
        cents = parse_amount(data.get('amount'))
    except (ValueError, TypeError, InvalidOperation):
        return error('Введите demo-баланс с точностью до 0.01 TON.')
    if not 0 <= cents <= 100000000000:
        return error('Demo-баланс: от 0 до 1 000 000 000 TON.')
    record = save_creator_record(session['uid'], {'demo_balance_cents': cents})
    return jsonify(ok=True, demo_balance=record['demo_balance_cents']/100, user=profile())


@app.post('/api/creator/demo-inventory')
@login_required
@creator_required
def creator_demo_inventory_add():
    data = request.get_json(silent=True) or {}
    gift_id = str(data.get('gift_id') or '').strip()
    gift = next((g for g in read_catalog().get('gifts', []) if str(g.get('id') or '') == gift_id), None)
    if not gift:
        return error('Подарок не найден в каталоге Portal.', 404)
    record = creator_record(session['uid'])
    items = list(record.get('demo_inventory') or [])
    next_id = max([int(x.get('id') or 0) for x in items] + [0]) + 1
    item = dict(
        id=next_id, gift_id=gift_id, name=str(gift.get('name') or 'Подарок'),
        image_url=safe_image(gift.get('image_url') or gift.get('portal_image_url')),
        price_ton=float(gift.get('price_ton') or 0), source='creator_demo',
        created_at=datetime.now(timezone.utc).isoformat(), external_url='',
        fragment_url='', fragment_number='', fragment_model='', fragment_backdrop='',
        fragment_symbol='', price_source='Portal', animation_url='',
        source_label='', promo_locked=False, promo_code='', wager_multiplier=0,
        wager_target=0, wager_progress=0, wager_complete=False, wager_percent=0,
        wager_attempts_total=1, wager_attempts_remaining=1, wager_burn_on_loss=True,
        unlock_target=None, expires_at=None, expires_in_seconds=None,
    )
    items.insert(0, item)
    record = save_creator_record(session['uid'], {'demo_inventory': items[:200]})
    return jsonify(ok=True, item=item, items=record['demo_inventory'])


@app.delete('/api/creator/demo-inventory/<int:item_id>')
@login_required
@creator_required
def creator_demo_inventory_remove(item_id):
    record = creator_record(session['uid'])
    items = [x for x in record.get('demo_inventory') or [] if int(x.get('id') or 0) != item_id]
    save_creator_record(session['uid'], {'demo_inventory': items})
    return jsonify(ok=True, items=items)


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
                             withdrawal_min_deposit_override=(None if user['withdrawal_min_deposit_override'] is None else int(user['withdrawal_min_deposit_override'])/100),
                             withdrawal_min_deposit_global=withdrawal_settings()['min_ton_connect_deposit'],
                             withdrawal_min_deposit_effective=((int(user['withdrawal_min_deposit_override'])/100)
                                                               if user['withdrawal_min_deposit_override'] is not None
                                                               else withdrawal_settings()['min_ton_connect_deposit']),
                             stars_withdrawal_until=(parse_datetime_utc(user['stars_withdrawal_until']).isoformat()
                                                     if parse_datetime_utc(user['stars_withdrawal_until']) and
                                                     parse_datetime_utc(user['stars_withdrawal_until']) > datetime.now(timezone.utc) else None),
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
    override_marker = object()
    raw_override = data.get('min_ton_connect_deposit_override', override_marker)
    override_cents = override_marker
    if raw_override is not override_marker:
        if raw_override in (None, ''):
            override_cents = None
        else:
            try:
                override_cents = parse_amount(raw_override)
            except (ValueError, TypeError, InvalidOperation):
                return error('Персональный минимум депозита: укажите сумму с точностью до 0.01 TON.')
            if not 0 <= override_cents <= 100000000:
                return error('Персональный минимум депозита: от 0 до 1 000 000 TON.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        if override_cents is override_marker:
            db.execute('UPDATE users SET withdrawal_enabled=?,withdrawal_block_reason=? WHERE id=?',
                       (1 if enabled else 0, '' if enabled else reason, user_id))
        else:
            db.execute('''UPDATE users SET withdrawal_enabled=?,withdrawal_block_reason=?,
                          withdrawal_min_deposit_override=? WHERE id=?''',
                       (1 if enabled else 0, '' if enabled else reason, override_cents, user_id))
        current = db.execute('SELECT withdrawal_min_deposit_override FROM users WHERE id=?', (user_id,)).fetchone()
        current_override = current['withdrawal_min_deposit_override'] if current else None
        global_min = int(withdrawal_settings()['min_ton_connect_deposit_cents'])
        effective_min = global_min if current_override is None else int(current_override or 0)
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'withdrawal_access',
                    json.dumps({'enabled': enabled, 'reason': '' if enabled else reason,
                                'min_deposit_override': current_override,
                                'effective_min_deposit': effective_min}, ensure_ascii=False)))
        log_event(db, user_id, 'withdrawal_access', enabled=enabled, reason='' if enabled else reason,
                  admin_id=session['uid'])
        db.commit()
    if enabled:
        notify_user_async(user_id, '✅ <b>Вывод подарков доступен</b>', miniapp_markup('Открыть', 'profile'), 'HTML')
    else:
        notify_user_async(user_id, f'⚠️ <b>Вывод временно недоступен</b>\n\n{escape(reason)}', miniapp_markup('Открыть', 'profile'), 'HTML')
    return jsonify(ok=True, enabled=enabled, reason='' if enabled else reason,
                   min_ton_connect_deposit_override=(None if current_override is None else int(current_override)/100),
                   min_ton_connect_deposit_effective=effective_min/100,
                   min_ton_connect_deposit_global=global_min/100)


@app.post('/api/admin/users/<int:user_id>/stars-withdrawal-unlock')
@admin_required
def admin_user_stars_withdrawal_unlock(user_id):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT stars_withdrawal_until FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return error('Пользователь не найден.', 404)
        db.execute('UPDATE users SET stars_withdrawal_until=NULL WHERE id=?', (user_id,))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'stars_withdrawal_unlock', str(row['stars_withdrawal_until'] or '')))
        log_event(db, user_id, 'withdrawal_access', enabled=True, reason='Ограничение Stars снято',
                  admin_id=session['uid'])
        db.commit()
    notify_user_async(user_id, '✅ <b>Ограничение вывода после оплаты Stars снято администратором.</b>',
                      miniapp_markup('Открыть', 'profile'), 'HTML')
    return jsonify(ok=True)


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
    try: activation_min_deposit=parse_amount(data.get('activation_min_deposit') or 0)
    except (ValueError,TypeError,InvalidOperation): return error('Проверьте минимальный депозит для активации.')
    if not 0<=activation_min_deposit<=100000000:return error('Минимальный депозит для активации: от 0 до 1 000 000 TON.')
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
                      assigned_user_id,source_label,description,expires_at,gift_expires_days,activation_min_deposit)
                      VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)''',
                   (code,kind,amount,gift_id,gift_name,gift_image,gift_price,wager_multiplier,
                    session['uid'],bonus_percent,bonus_fixed,min_deposit,json.dumps(multi_reward,ensure_ascii=False) if multi_reward else '{}',user_id,'Администрация','',expires_at,gift_expires_days,activation_min_deposit))
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
            assigned_user_id,source_label,description,expires_at,gift_expires_days,activation_min_deposit)
            VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)''',
            (issued_code,template['reward_type'],template['amount'],template['gift_id'],
             template['gift_name'],template['gift_image_url'],template['gift_price'],
             template['wager_multiplier'],session['uid'],template['bonus_percent'],
             template['bonus_fixed'],template['min_deposit'],template['reward_json'],
             user_id,'Выдан администратором',promo_purpose(template),template['expires_at'],template['gift_expires_days'],template['activation_min_deposit']))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'promo_issue',f'{code} → {issued_code}'))
        log_event(db,user_id,'promo_issued',code=issued_code,source='Администрация')
        db.commit()
        notify_promo_async(user_id, issued_code, 'bonuses')
        return jsonify(ok=True,code=issued_code)
    finally:
        db.close()


@app.delete('/api/admin/users/<int:user_id>/promocodes/<code>')
@admin_required
def admin_delete_user_promocode(user_id, code):
    code=str(code or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}',code):
        return error('Промокод указан неверно.',400)
    db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        promo=db.execute('SELECT * FROM promo_codes WHERE code=? AND assigned_user_id=?'+
                         (' FOR UPDATE' if DATABASE_URL else ''),(code,user_id)).fetchone()
        if not promo:
            return error('Личный промокод пользователя не найден.',404)
        used=bool(int(promo['uses_count'] or 0))
        # Removing a personal promo removes the code from the user's bonus list and
        # prevents any future activation. Already credited balance/gifts are not
        # clawed back; their transaction/event history remains intact.
        db.execute('UPDATE promo_codes SET active=0 WHERE code=? AND assigned_user_id=?',(code,user_id))
        db.execute('DELETE FROM promo_redemptions WHERE code=? AND user_id=?',(code,user_id))
        db.execute('DELETE FROM promo_views WHERE code=? AND user_id=?',(code,user_id))
        db.execute('DELETE FROM promo_codes WHERE code=? AND assigned_user_id=?',(code,user_id))
        action='deleted_after_use' if used else 'deleted'
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'],user_id,'promo_remove',f'{code}:{action}'))
        log_event(db,user_id,'promo_removed',code=code,admin_id=session['uid'],result=action)
        db.commit()
        return jsonify(ok=True,code=code,deleted=True,previously_used=used)
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


def _money(cents):
    return f'{abs(int(cents or 0))/100:.2f} TON'


def _col(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _ev(eid, date, kind, title, lines=(), amount=None, tone='neutral', image='', group='other', details=None):
    return dict(id=eid, date=str(date or ''), kind=kind, title=title, lines=[str(x) for x in lines if x],
                amount=amount, tone=tone, image=image or '', group=group, details=details or {})


def _activity_mines(r):
    try:
        opened_list = json.loads(r['opened'] or '[]')
    except (TypeError, ValueError, json.JSONDecodeError):
        opened_list = []
    opened = len(opened_list) if isinstance(opened_list, list) else 0
    mines = int(r['mines'] or 0)
    bet_type = _col(r, 'bet_type', 'ton') or 'ton'
    state = r['state']
    bet = int(r['bet'] or 0)
    try:
        rtp = round_rtp(r)
    except Exception:
        rtp = game_rtp()
    try:
        mult = (float(r['win_multiplier']) if r['win_multiplier'] is not None
                else float(multiplier_for(mines, opened, rtp)) if opened else 1.0)
    except (TypeError, ValueError, ArithmeticError):
        mult = 1.0
    gift_name = _col(r, 'bet_gift_name', '') or 'Подарок'
    gift_price = int(_col(r, 'bet_gift_price', 0) or 0)
    if bet_type == 'promo_gift':
        stake = (f'Ставка отыгрышным подарком: {gift_name} ({_money(gift_price)}) · '
                 f'отыгрыш X{float(_col(r, "promo_wager_multiplier", 0) or 0):g}')
    elif bet_type == 'gift':
        stake = f'Ставка подарком: {gift_name} ({_money(gift_price)})'
    else:
        stake = f'Ставка: {_money(bet)}'
    field = f'Мин на поле: {mines} · открыто клеток: {opened}' + (f' · X{mult:.2f}' if opened else '')
    image = _col(r, 'win_gift_image', '') or _col(r, 'bet_gift_image', '')
    ton_bet = bet_type == 'ton'
    base = dict(eid='m' + str(r['id']), date=r['created_at'], kind='mines_round', image=image, group='game',
                details=dict(game='Mines', state=state, bet_type=bet_type))
    if state == 'active':
        potential = payout_for(r, opened, rtp) if opened else 0
        lines = [stake, field]
        lines.append(f'Выигрыш НЕ забран — сейчас можно было забрать {_money(potential)}' if opened
                     else 'Ни одной клетки не открыто, выигрыш не забран')
        return _ev(title='Mines · раунд не завершён', lines=lines, amount=(-bet / 100 if ton_bet else None),
                   tone='pending', **base)
    if state == 'lost':
        cell = _col(r, 'lost_cell', None)
        if cell is None:
            return _ev(title='Mines · раунд закрыт (истёк срок подарка)',
                       lines=[stake, field, 'Отыгрышный подарок сгорел по сроку' if bet_type == 'promo_gift' else ''],
                       amount=(-bet / 100 if ton_bet else None), tone='loss', **base)
        lines = [stake, field + f' · мина в клетке {int(cell) + 1}']
        if ton_bet:
            lines.append(f'Проигрыш: {_money(bet)}')
        elif bet_type == 'gift':
            lines.append(f'Проигран подарок: {gift_name} ({_money(gift_price)})')
        else:
            total = max(1, int(_col(r, 'promo_attempts_total', 1) or 1))
            before = max(1, int(_col(r, 'promo_attempts_remaining', 1) or 1))
            after = before - 1 if bool(_col(r, 'promo_burn_on_loss', 1)) else before
            lines.append(f'Отыгрыш не засчитан · осталось жизней: {after} из {total}' if after > 0
                         else f'Подарок {gift_name} сгорел (жизни закончились)')
        return _ev(title='Mines · проигрыш', lines=lines, amount=(-bet / 100 if ton_bet else None),
                   tone='loss', **base)
    # won
    total = int(r['win_total'] if r['win_total'] is not None else (r['payout'] or 0))
    if bet_type == 'promo_gift':
        target = int(_col(r, 'promo_wager_target', 0) or 0)
        progress = int(_col(r, 'promo_progress_after', 0) or 0)
        lines = [stake, field, f'В отыгрыш засчитано: +{_money(total)}',
                 f'Прогресс отыгрыша: {progress / 100:.2f} / {target / 100:.2f} TON']
        if target and progress >= target:
            lines.append('Отыгрыш выполнен — подарок можно разблокировать в профиле')
        return _ev(title='Mines · отыгрыш (без выплаты)', lines=lines, tone='win', **base)
    lines = [stake, field]
    win_gift = _col(r, 'win_gift_name', '')
    if win_gift:
        lines.append(f'Забрал подарок: {win_gift} ({_money(_col(r, "win_gift_price", 0))})')
        if int(r['payout'] or 0) > 0:
            lines.append(f'Остаток зачислен на баланс: {_money(r["payout"])}')
    else:
        lines.append(f'Забрал: {_money(total)}')
    net = total - bet if ton_bet else None
    if ton_bet:
        lines.append(f'Чистыми: {"+" if net >= 0 else "−"}{_money(net)}')
    return _ev(title='Mines · выигрыш забран', lines=lines, amount=(net / 100 if net is not None else None),
               tone='win' if (net is None or net >= 0) else 'loss', **base)


def _activity_crash(r):
    bet = int(r['bet'] or 0)
    auto = int(r['auto_x100'] or 0)
    state = r['state']
    crash_x100 = _col(r, 'crash_x100', None)
    round_state = _col(r, 'round_state', '')
    promo_bet = (_col(r, 'bet_type', 'ton') or 'ton') == 'promo_gift'
    gift_bet = promo_bet or (_col(r, 'bet_type', 'ton') or 'ton') == 'gift'
    stake = (f'{_col(r, "bet_gift_name", "") or "Подарок"} ({_money(bet)})' if gift_bet else _money(bet))
    lines = [f'Раунд #{r["round_id"]} · ставка{" отыгрышным подарком" if promo_bet else " подарком" if gift_bet else ""}: {stake}' + (f' · авто-вывод на x{auto / 100:.2f}' if auto else '')]
    crashed_text = (f'Раунд закончился крашем на x{int(crash_x100) / 100:.2f}'
                    if crash_x100 is not None and round_state == 'crashed' else '')
    base = dict(eid='c%s-%s' % (r['round_id'], r['user_id']), date=r['created_at'], kind='crash_bet', group='game',
                details=dict(game='Crash', state=state))
    if state == 'won' and promo_bet:
        cash = int(r['cashout_x100'] or 0)
        added = bet * cash // 100
        lines.append(f'Забрал на x{cash / 100:.2f}' + (' (авто-вывод)' if auto and cash == auto else '') +
                     f' · в отыгрыш +{_money(added)}')
        if crashed_text:
            lines.append(crashed_text)
        return _ev(title='Crash · отыгрыш', lines=lines, amount=0, tone='win', **base)
    if state == 'won':
        payout = int(r['payout'] or 0)
        cash = int(r['cashout_x100'] or 0)
        lines.append(f'Забрал на x{cash / 100:.2f}' + (' (авто-вывод)' if auto and cash == auto else '') +
                     f' · выплата {_money(payout)}')
        if _col(r, 'prize_name', ''):
            lines.append(f'Забрал подарком: {_col(r, "prize_name", "")}')
        if crashed_text:
            lines.append(crashed_text)
        lines.append(f'Чистыми: +{_money(payout - bet)}')
        return _ev(title='Crash · выигрыш', lines=lines, amount=(payout - bet) / 100, tone='win', **base)
    if state == 'lost':
        lines.append('Не успел забрать до взрыва' if not auto else 'Авто-вывод не сработал — краш раньше')
        if crashed_text:
            lines.append(crashed_text)
        if promo_bet:
            lines.append('Потрачена жизнь отыгрышного подарка')
            return _ev(title='Crash · проигрыш (отыгрыш)', lines=lines, amount=0, tone='loss', **base)
        lines.append(f'Проигрыш: {_money(bet)}')
        return _ev(title='Crash · проигрыш', lines=lines, amount=-bet / 100, tone='loss', **base)
    lines.append('Раунд ещё идёт — результат не определён')
    return _ev(title='Crash · ставка в игре', lines=lines, amount=-bet / 100, tone='pending', **base)


def _activity_upgrade(r):
    try:
        result = json.loads(r['result_json'] or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    src_price = int(r['source_price'] or 0)
    tgt_price = int(r['target_price'] or 0)
    won = bool(r['won'])
    src_name = r['source_name'] or 'Ставка'
    tgt_name = r['target_name'] or 'Цель'
    from_ton = (result.get('source_type') or ('ton' if src_name == 'TON' else 'gift')) == 'ton'
    wager = result.get('reward_type') == 'wager_progress' or (not from_ton and result.get('wager_target'))
    chance = int(r['chance_bp'] or 0) / 100
    if from_ton:
        stake = f'Ставка: {_money(src_price)}'
    elif wager:
        stake = f'Ставка отыгрышным подарком: {src_name} ({_money(src_price)})'
    else:
        stake = f'Ставка подарком: {src_name} ({_money(src_price)})'
    goal = f'Цель: {tgt_name} ({_money(tgt_price)}) · шанс {chance:.2f}%'
    base = dict(eid='u' + str(r['id']), date=r['created_at'], kind='upgrade', group='game',
                image=(r['target_image'] if won else r['source_image']) or '',
                details=dict(game='Upgrade', won=won))
    if wager:
        lines = [stake, goal]
        if won:
            lines.append(f'Засчитано в отыгрыш: +{_money(tgt_price)} · прогресс '
                         f'{float(result.get("wager_progress") or 0):.2f} / {float(result.get("wager_target") or 0):.2f} TON')
            if result.get('wager_complete'):
                lines.append('Отыгрыш выполнен — подарок можно разблокировать в профиле')
            return _ev(title='Upgrade · отыгрыш (успех)', lines=lines, tone='win', **base)
        if result.get('wager_burned'):
            lines.append(f'Проигрыш: подарок {src_name} сгорел (жизни закончились)')
        else:
            lines.append(f'Проигрыш: потеряна жизнь, осталось {int(result.get("wager_attempts_remaining") or 0)} '
                         f'из {int(result.get("wager_attempts_total") or 1)}')
        return _ev(title='Upgrade · отыгрыш (проигрыш)', lines=lines, tone='loss', **base)
    if won:
        lines = [stake, goal, f'Выигран подарок: {tgt_name} ({_money(tgt_price)})']
        if from_ton:
            lines.append(f'Списано с баланса за ставку: {_money(src_price)}')
        return _ev(title='Upgrade · выигрыш', lines=lines, tone='win', **base)
    lines = [stake, goal,
             f'Проигрыш: {_money(src_price)}' if from_ton else f'Проигран подарок: {src_name} ({_money(src_price)})']
    comp = result.get('compensation') if isinstance(result.get('compensation'), dict) else {}
    reward = comp.get('reward') if isinstance(comp.get('reward'), dict) else None
    if reward:
        kind = reward.get('type')
        if kind == 'balance':
            lines.append(f'Компенсация: +{float(reward.get("amount") or 0):.2f} TON на баланс')
        elif kind == 'tickets':
            lines.append(f'Компенсация: {int(reward.get("tickets") or 0)} билет(ов)')
        elif kind == 'gift':
            lines.append(f'Компенсация: подарок {reward.get("name") or ""}')
        elif kind == 'wager_gift':
            lines.append(f'Компенсация: отыгрышный подарок {reward.get("name") or ""} · X{float(reward.get("wager_multiplier") or 0):g}')
        elif kind == 'promo':
            lines.append(f'Компенсация: промокод на подарок {reward.get("name") or ""} ({reward.get("code") or ""})')
    return _ev(title='Upgrade · проигрыш', lines=lines, amount=(-src_price / 100 if from_ton else None),
               tone='loss', **base)


# kind -> (title, tone). Kinds duplicated by game rows below are intentionally absent (hidden).
_TX_TITLES = {
    'deposit': ('Пополнение баланса администратором', 'win'),
    'ton_deposit': ('Пополнение через TON', 'win'),
    'deposit_promo_bonus': ('Бонус за пополнение (промокод)', 'win'),
    'referral_bonus': ('Реферальный бонус', 'win'),
    'referral_withdraw': ('Вывод реферального баланса', 'neutral'),
    'admin_balance': ('Администратор изменил баланс', 'neutral'),
    'gift_sale': ('Продажа подарка', 'win'),
    'arena_refund': ('Арена · возврат ставки', 'neutral'),
    'arena_gift_refund': ('Арена · возврат подарка', 'neutral'),
    'withdrawal_request': ('Заявка на вывод подарка', 'pending'),
    'withdrawal_approved': ('Вывод подарка выполнен', 'neutral'),
    'withdrawal_rejected': ('Вывод отклонён — подарок возвращён', 'neutral'),
    'transfer_sent': ('Перевод отправлен', 'loss'),
    'transfer_received': ('Перевод получен', 'win'),
    'daily_top_reward': ('Награда за ТОП дня', 'win'),
    'upgrade_cashback': ('Компенсация проигрыша в Upgrade', 'win'),
    'upgrade_compensation_gift': ('Компенсация Upgrade: подарок', 'win'),
    'promo_balance': ('Промокод: TON на баланс', 'win'),
    'promo_gift': ('Промокод: получен подарок', 'win'),
    'promo_wager_gift': ('Промокод: получен отыгрышный подарок', 'win'),
    'freebet_balance': ('Фрибет: TON на баланс', 'win'),
    'freebet_gift': ('Фрибет: получен подарок', 'win'),
    'freebet_wager_gift': ('Фрибет: получен отыгрышный подарок', 'win'),
    'promo_wager_claim': ('Разблокирован отыгрышный подарок', 'win'),
    'promo_gift_expired': ('Отыгрышный подарок сгорел по сроку', 'loss'),
    'craft_consume': ('Крафт: подарки потрачены', 'loss'),
    'craft_reward': ('Крафт: получен подарок', 'win'),
}


def _activity_transaction(r):
    kind = r['kind']
    reference = _col(r, 'reference_type', '')
    if kind == 'promo_wager_burn' and reference == 'freebet':
        title, tone = 'Фрибет: отыгрышный подарок сгорел', 'loss'
    elif kind in _TX_TITLES:
        title, tone = _TX_TITLES[kind]
    elif kind.startswith('freebet_') or kind.startswith('promo_'):
        title, tone = 'Бонус: ' + kind.replace('_', ' '), 'neutral'
    else:
        return None
    amount = int(r['amount'] or 0)
    lines = []
    detail = str(_col(r, 'details', '') or '')
    if kind in ('gift_sale',):
        lines.append(f'Продан подарок: {detail} · получено {_money(amount)}')
    elif kind in ('deposit', 'ton_deposit'):
        lines.append(f'Зачислено: {_money(amount)}')
    elif kind == 'transfer_sent':
        lines.append(f'Получатель и комиссия: {detail}' if detail else '')
        lines.append(f'Списано всего: {_money(amount)}')
    elif kind == 'transfer_received':
        lines.append(detail)
        lines.append(f'Зачислено: {_money(amount)}')
    elif kind == 'daily_top_reward':
        lines.append(detail)
    else:
        lines.append(detail)
        if amount:
            lines.append(('Зачислено: ' if amount > 0 else 'Списано: ') + _money(amount))
    if r['balance_after'] is not None:
        lines.append(f'Баланс после операции: {_money(r["balance_after"])}')
    return _ev('t' + str(r['id']), r['created_at'], kind, title, lines, amount=(amount / 100 if amount else None),
               tone=tone, group='money', details=dict(text=detail))


def _activity_arena(r):
    amount = int(r['amount'] or 0)
    gifts = arena_gift_list(r['gifts'])
    gift_amount = min(amount, max(0, int(_col(r, 'gift_amount', 0) or 0)))
    ton = amount - gift_amount
    round_id = int(r['round_id'])
    state = _col(r, 'round_state', 'open')
    pool = int(_col(r, 'total_pool', 0) or 0)
    won = state == 'settled' and int(_col(r, 'winner_user_id', 0) or 0) == int(r['user_id'])
    ton_pool = max(0, min(pool, int(_col(r, 'round_ton_pool', pool) or 0)))
    parts = []
    if ton:
        parts.append(_money(ton))
    if gifts:
        parts.append('подарки: ' + ', '.join(f'{g.get("name") or "Подарок"} ({_money(int(g.get("price") or 0))})' for g in gifts))
    lines = [f'Раунд Арены #{round_id} · ставка {_money(amount)}' + (f' ({"; ".join(parts)})' if parts else '')]
    base = dict(eid='a%s-%s' % (round_id, r['user_id']), date=_col(r, 'created_at', ''), kind='arena_bet', group='game',
                image=(str(gifts[0].get('image_url') or '') if gifts else ''),
                details=dict(game='Arena', state=state))
    if state != 'settled':
        lines.append('Раунд ещё идёт — результат не определён')
        return _ev(title='Арена · ставка в игре', lines=lines, amount=-amount / 100, tone='pending', **base)
    chance = amount * 100.0 / pool if pool else 0
    lines.append(f'Банк: {_money(pool)} · шанс на победу {chance:.1f}%')
    if won:
        fee = arena_fee_cents(ton_pool)
        ton_payout = ton_pool - fee
        gift_value = pool - ton_pool
        payout = ton_payout + gift_value          # TON + gifts at their price
        lines.append(f'Выплата TON: {_money(ton_payout)}' + (f' (комиссия {ARENA_FEE_PERCENT}% только с TON)' if fee else ' (без комиссии)'))
        if gift_value:
            lines.append(f'Получены подарки на {_money(gift_value)} (без комиссии)')
        lines.append(f'x{payout / amount:.2f} · чистыми: +{_money(payout - amount)}' if amount else '')
        return _ev(title='Арена · выигрыш', lines=lines, amount=(payout - amount) / 100, tone='win', **base)
    lines.append(f'Проигрыш: {_money(amount)}' + (' (включая подарки)' if gifts else ''))
    return _ev(title='Арена · проигрыш', lines=lines, amount=-amount / 100, tone='loss', **base)


def _level_reward_text(reward):
    kind = reward.get('type')
    if kind == 'balance':
        return f'Забрано за уровень: +{float(reward.get("amount") or 0):.2f} TON на баланс'
    if kind == 'tickets':
        return f'Забрано за уровень: {int(reward.get("tickets") or 0)} билет(ов)'
    gift = reward.get('gift') if isinstance(reward.get('gift'), dict) else {}
    if kind == 'gift':
        return f'Забран подарок за уровень: {gift.get("name") or "Подарок"} ({float(gift.get("price_ton") or 0):.2f} TON)'
    if kind == 'wager_gift':
        return (f'Забран отыгрышный подарок за уровень: {gift.get("name") or "Подарок"} '
                f'({float(gift.get("price_ton") or 0):.2f} TON) · X{float(gift.get("wager_multiplier") or 0):g}')
    if kind == 'transfer_unlock':
        return 'Забрано за уровень: доступ к переводам TON'
    if reward.get('code'):
        return f'Забран промокод за уровень: {reward["code"]}'
    return 'Забрана награда за уровень'


_EVENT_TITLES = {
    'admin_level': 'Администратор изменил уровень',
    'withdrawal_access': 'Изменён доступ к выводу',
    'admin_gift_add': 'Администратор выдал подарок',
    'admin_gift_remove': 'Администратор забрал подарок',
    'reward_task_claim': 'Выполнено задание (билеты)',
    'giveaway_enter': 'Участие в розыгрыше',
    'giveaway_win': 'Победа в розыгрыше',
    'promo_issued': 'Выдан промокод',
    'promo_redeem': 'Активирован промокод',
    'freebet_redeem': 'Активирован фрибет',
    'deposit_promo_removed': 'Убран промокод на пополнение',
    'promo_wager_burn': 'Сгорел отыгрышный подарок',
    'daily_top_reward': 'Награда за ТОП дня',
}
# These are already shown as transactions / rounds / claims, so the raw event would be a duplicate.
_EVENT_HIDDEN = {'transfer_sent', 'transfer_received', 'deposit_confirmed', 'level_claim', 'login', 'mines_start',
                 'mines_cell', 'mines_cashout', 'upgrade', 'craft_play', 'roll', 'deposit_created',
                 'promo_gift_expired', 'upgrade_wager_repaired'}


def _activity_event(r):
    kind = r['kind']
    if kind in _EVENT_HIDDEN:
        return None
    try:
        d = json.loads(r['payload'] or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    title = _EVENT_TITLES.get(kind, 'Событие: ' + str(kind))
    lines, tone = [], 'neutral'
    if kind == 'admin_level':
        lines.append(f'Уровень: {d.get("previous_level")} → {d.get("new_level")}')
        if d.get('reset_rewards'):
            lines.append('Награды уровней сброшены')
    elif kind == 'withdrawal_access':
        lines.append('Вывод включён' if d.get('enabled') else 'Вывод выключен' + (f': {d["reason"]}' if d.get('reason') else ''))
    elif kind in ('admin_gift_add', 'admin_gift_remove'):
        lines.append(f'Подарок: {d.get("gift_name") or "—"}')
    elif kind == 'reward_task_claim':
        lines.append(f'Получено билетов: {int(d.get("tickets") or 0)}')
        tone = 'win'
    elif kind == 'giveaway_enter':
        lines.append(f'Использовано билетов: {int(d.get("tickets") or 0)}')
    elif kind == 'giveaway_win':
        lines.append(f'Место: {d.get("rank", 1)} · подарок: {d.get("gift_name") or "—"}')
        tone = 'win'
    elif kind == 'promo_issued':
        lines.append(f'Код: {d.get("code")} · источник: {d.get("source") or "—"}')
        tone = 'win'
    elif kind in ('promo_redeem', 'freebet_redeem'):
        lines.append(f'Код: {d.get("code")} · тип награды: {d.get("reward_type") or "—"}')
        tone = 'win'
    elif kind == 'deposit_promo_removed':
        lines.append(f'Код: {d.get("code")}')
    elif kind == 'promo_wager_burn':
        lines.append(d.get('gift_name') or '')
        tone = 'loss'
    else:
        lines.extend(f'{k}: {v}' for k, v in d.items() if not str(k).endswith('image') and not isinstance(v, (dict, list)))
    image = d.get('gift_image') or d.get('target_image') or ''
    return _ev('e' + str(r['id']), r['created_at'], kind, title, lines, tone=tone, image=image,
               group='event', details=d)


_ADMIN_ACTION_TITLES = {'max_drop_override': 'Администратор изменил «макс. дроп» профиля',
                        'withdrawal_access': 'Администратор изменил доступ к выводу',
                        'promo_create': 'Администратор создал промокод',
                        'promo_issue': 'Администратор выдал промокод'}
_ADMIN_ACTION_HIDDEN = {'balance_set', 'level_set'}


def _activity_admin(r):
    action = str(r['action'])
    if action in _ADMIN_ACTION_HIDDEN:
        return None
    title = _ADMIN_ACTION_TITLES.get(action, 'Действие администратора: ' + action)
    return _ev('a' + str(r['id']), r['created_at'], 'admin_action', title,
               [str(r['details'] or '')[:300], f'Админ ID {r["admin_id"]}'], group='admin',
               details=dict(action=action, admin_id=r['admin_id']))


@app.get('/api/admin/users/<int:user_id>/activity')
@admin_required
def admin_user_activity(user_id):
    try:offset=max(0,min(100000,int(request.args.get('offset',0))))
    except (ValueError,TypeError):offset=0
    limit=offset+101
    events=[]
    def add(builder, rows):
        for row in rows:
            try:
                item = builder(row)
            except Exception:
                app.logger.warning('Skipping malformed activity row', exc_info=True)
                continue
            if item:
                events.append(item)
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?',(user_id,)).fetchone():return error('Пользователь не найден.',404)
        add(_activity_mines, db.execute('SELECT * FROM rounds WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall())
        add(_activity_crash, db.execute('''SELECT b.*,r.crash_x100 AS crash_x100,r.state AS round_state
                                           FROM crash_bets b LEFT JOIN crash_rounds r ON r.id=b.round_id
                                           WHERE b.user_id=? ORDER BY b.round_id DESC LIMIT ?''',(user_id,limit)).fetchall())
        add(_activity_upgrade, db.execute('SELECT * FROM upgrade_spins WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?',
                                          (user_id,limit)).fetchall())
        add(_activity_arena, db.execute('''SELECT b.*,r.state AS round_state,r.winner_user_id,r.total_pool,
                                           (SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id) AS round_ton_pool
                                           FROM arena_bets b JOIN arena_rounds r ON r.id=b.round_id
                                           WHERE b.user_id=? ORDER BY b.round_id DESC LIMIT ?''',(user_id,limit)).fetchall())
        tx_rows = db.execute('SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall()
        add(_activity_transaction, tx_rows)
        event_rows = db.execute('SELECT * FROM user_events WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall()
        add(_activity_event, event_rows)
        seen_codes = {str(x['reference_id']) for x in tx_rows if x['reference_type'] in ('promo', 'freebet')}
        for x in event_rows:
            try:
                code = json.loads(x['payload'] or '{}').get('code')
            except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
                code = None
            if code and x['kind'] in ('promo_redeem', 'freebet_redeem'):
                seen_codes.add(str(code))
        def promo_row(r):
            if str(r['code']) in seen_codes:
                return None
            return _ev('p' + str(r['code']), r['created_at'], 'promo_activation', 'Активирован промокод',
                       [f'Код: {r["code"]} · тип награды: {r["reward_type"]}',
                        f'Сумма: {_money(r["amount"])}' if int(r['amount'] or 0) else ''],
                       amount=(int(r['amount'] or 0) / 100 or None), tone='win', group='money')
        add(promo_row, db.execute('SELECT * FROM promo_redemptions WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall())
        def roll_row(r):
            gift = r['gift_name'] or ''
            result = f'Выпал подарок: {gift}' if r['outcome'] == 'gift' else f'Результат: {r["outcome"]}'
            return _ev('r' + str(r['id']), r['created_at'], 'roll_spin', 'Roll · прокрутка',
                       [f'Ролл: {r["roll_id"]} · цена: {_money(r["price"])}', result],
                       amount=-int(r['price'] or 0) / 100, tone='win' if r['outcome'] == 'gift' else 'loss', group='game')
        add(roll_row, db.execute('SELECT * FROM roll_spins WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall())
        def deposit_row(r):
            if r['status'] == 'credited':
                return None
            label = {'pending': 'Заявка на пополнение создана, но не оплачена', 'expired': 'Заявка на пополнение истекла (не оплачена)'}.get(r['status'], 'Заявка на пополнение: ' + str(r['status']))
            return _ev('d' + str(r['id']), r['created_at'], 'deposit_order', 'Пополнение TON · не завершено',
                       [label, f'Сумма заявки: {_money(r["amount"])}', f'Промокод: {r["promo_code"]}' if r['promo_code'] else ''],
                       tone='pending', group='money')
        add(deposit_row, db.execute('SELECT * FROM ton_deposit_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall())
        add(_activity_admin, db.execute('SELECT * FROM admin_log WHERE user_id=? ORDER BY id DESC LIMIT ?',(user_id,limit)).fetchall())
        def level_row(r):
            try:
                reward = json.loads(r['reward_json'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                reward = {}
            if not isinstance(reward, dict):
                reward = {}
            amount = float(reward.get('amount') or 0) if reward.get('type') == 'balance' else None
            gift = reward.get('gift') if isinstance(reward.get('gift'), dict) else {}
            return _ev('l' + str(r['level']), r['created_at'], 'level_claim', f'Награда за уровень {r["level"]}',
                       [_level_reward_text(reward)], amount=amount, tone='win', image=gift.get('image_url') or '',
                       group='money', details=dict(level=r['level'], reward=reward))
        add(level_row, db.execute('SELECT * FROM level_claims WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(user_id,limit)).fetchall())
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
    nft_url = str(data.get('fragment_url') or data.get('telegram_url') or '').strip()

    if nft_url:
        # fragment_gift_from_url accepts both Fragment and public Telegram NFT links,
        # e.g. https://t.me/nft/PartySparkler-66376.
        try:
            nft = fragment_gift_from_url(nft_url, True, refresh=True, allow_missing_price=True)
            portal_price, portal_source = _fragment_portal_fallback_price(
                nft.get('collection_name') or re.sub(r'\s*#\s*\d+.*$', '', str(nft.get('gift_name') or '')).strip(),
                nft.get('fragment_model') or '',
                nft.get('fragment_backdrop') or ''
            )
        except (ValueError, TypeError, requests.RequestException) as exc:
            return error(str(exc) or 'Не удалось получить данные NFT.')

        portal_price = max(0, int(portal_price or 0))
        if portal_price <= 0:
            return error('Portal не вернул цену для этого NFT. Подарок не добавлен.')
        accepted_price = _gift_deposit_accept_cents(portal_price)
        if accepted_price <= 0:
            return error('Не удалось рассчитать стоимость NFT после вычета 15%.')

        external_url = str(nft.get('fragment_url') or '')
        gift_id = str(nft.get('gift_id') or '')
        gift_name = str(nft.get('gift_name') or 'Telegram NFT')[:140]
        image_url = safe_image(nft.get('image_url'))
        number = str(nft.get('fragment_number') or '')
        model = str(nft.get('fragment_model') or '')
        backdrop = str(nft.get('fragment_backdrop') or '')
        symbol = str(nft.get('fragment_symbol') or '')
        animation_url = safe_image(nft.get('animation_url'))

        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                db.rollback()
                return error('Пользователь не найден.', 404)
            if external_url and db.execute(
                'SELECT 1 FROM inventory WHERE user_id=? AND external_url=? LIMIT 1',
                (user_id, external_url)).fetchone():
                db.rollback()
                return error('Этот конкретный NFT уже есть у пользователя.', 409)

            cur = db.execute("""INSERT INTO inventory(
                user_id,gift_id,gift_name,image_url,floor_price,source,external_url,
                fragment_number,fragment_model,fragment_backdrop,fragment_symbol,
                price_source,animation_url,source_label,deposit_mirror)
                VALUES(?,?,?,?,?,'admin_nft',?,?,?,?,?,?,?,?,0)""",
                (user_id, gift_id, gift_name, image_url, accepted_price, external_url,
                 number, model, backdrop, symbol, str(portal_source or 'Portal') + ' · −15%',
                 animation_url, ''))
            item_id = cur.lastrowid
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], user_id, 'gift_add_nft',
                        json.dumps({'inventory_id': item_id, 'gift': gift_name, 'url': external_url,
                                    'portal_price': portal_price, 'price': accepted_price,
                                    'discount_percent': 15, 'price_source': portal_source}, ensure_ascii=False)))
            log_event(db, user_id, 'admin_gift_add', gift_name=gift_name,
                      fragment_number=number, price_ton=accepted_price/100,
                      portal_price_ton=portal_price/100, discount_percent=15,
                      price_source=portal_source, admin_id=session['uid'])
            db.commit()
            row = db.execute('SELECT * FROM inventory WHERE id=?', (item_id,)).fetchone()
        return jsonify(ok=True, item=inventory_item(row), source='nft')

    gift_id = str(data.get('gift_id', '')).strip()
    try:
        gift = next((gift for gift in read_catalog()['gifts'] if str(gift.get('id')) == gift_id), None)
    except (OSError, ValueError, TypeError):
        gift = None
    if not gift:
        return error('Выберите подарок из каталога Portal.')
    try:
        price = parse_amount(gift['price_ton']) if gift.get('price_ton') is not None else 0
    except (KeyError, ValueError, TypeError, InvalidOperation):
        price = 0
    if price <= 0:
        return error('Portal не вернул цену этого подарка.')
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,price_source,source_label)
                            VALUES(?,?,?,?,?,'admin',?,'Выдано администратором')""",
                         (user_id, gift_id, str(gift['name']),
                          safe_image(gift.get('image_url') or gift.get('portal_image_url')),
                          price, str(gift.get('price_source') or 'Portal')))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'gift_add', gift_id))
        log_event(db,user_id,'admin_gift_add',gift_name=str(gift['name']),price_ton=price/100,admin_id=session['uid'])
        row = db.execute('SELECT * FROM inventory WHERE id=?', (cur.lastrowid,)).fetchone()
    return jsonify(ok=True, item=inventory_item(row), source='catalog')


@app.post('/api/admin/users/<int:user_id>/inventory/nft-preview')
@admin_required
def admin_user_nft_preview(user_id):
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or '').strip()
    try:
        gift = fragment_gift_from_url(url, True, refresh=True, allow_missing_price=True)
        portal_price, portal_source = _fragment_portal_fallback_price(
            gift.get('collection_name') or re.sub(r'\s*#\s*\d+.*$', '', str(gift.get('gift_name') or '')).strip(),
            gift.get('fragment_model') or '',
            gift.get('fragment_backdrop') or ''
        )
    except (ValueError, TypeError, requests.RequestException) as exc:
        return error(str(exc) or 'Не удалось получить данные NFT.')
    portal_price = max(0, int(portal_price or 0))
    if portal_price <= 0:
        return error('Portal не вернул цену для этого NFT.')
    accepted_price = _gift_deposit_accept_cents(portal_price)
    return jsonify(ok=True, gift=dict(
        name=gift.get('gift_name') or 'Telegram NFT',
        image_url=safe_image(gift.get('image_url')),
        portal_price_ton=portal_price/100,
        price_ton=accepted_price/100,
        discount_percent=15,
        price_source=(portal_source or 'Portal') + ' · −15%',
        fragment_url=gift.get('fragment_url') or '',
        fragment_number=gift.get('fragment_number') or '',
        model=gift.get('fragment_model') or '',
        backdrop=gift.get('fragment_backdrop') or '',
        symbol=gift.get('fragment_symbol') or ''
    ))


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
    view=request.args.get('view','pending').strip().lower()
    if view not in {'pending','completed'}:return error('Неизвестный раздел выводов.')
    where="w.status='pending'" if view=='pending' else "w.status IN ('approved','rejected')"
    with connect() as db:
        rows=db.execute(f"""SELECT w.*,u.name AS user_name,u.username,au.name AS admin_name,au.username AS admin_username,
                                   rl.status AS relayer_status,rl.transfer_stars AS relayer_transfer_stars,
                                   rl.error AS relayer_error,rl.attempts AS relayer_attempts,rl.updated_at AS relayer_updated_at,
                                   pl.status AS portal_status,pl.stage AS portal_stage,pl.error AS portal_error,
                                   pl.attempts AS portal_attempts,pl.nft_id AS portal_nft_id,pl.nft_name AS portal_nft_name,
                                   pl.source AS portal_source,pl.purchase_price AS portal_purchase_price,
                                   pl.balance_before AS portal_balance_before,pl.withdrawal_ids AS portal_withdrawal_ids,
                                   pl.updated_at AS portal_updated_at
                            FROM withdrawals w JOIN users u ON u.id=w.user_id
                            LEFT JOIN users au ON au.id=w.admin_id
                            LEFT JOIN relayer_withdrawal_logs rl ON rl.withdrawal_id=w.id
                            LEFT JOIN portal_withdrawal_logs pl ON pl.withdrawal_id=w.id
                            WHERE {where} ORDER BY w.id DESC LIMIT 300""").fetchall()
    failures={'disabled','login_required','insufficient_stars','blocked','recipient_unavailable','error',
              'portal_not_configured','portal_not_found','portal_insufficient_balance','portal_buy_failed',
              'portal_withdraw_failed','portal_error','portal_recovered','portal_unknown'}
    items=[]
    for x in rows:
        ps=str(x['portal_status'] or '');rs=str(x['relayer_status'] or '')
        provider='portal' if ps else ('relayer' if rs else '')
        status=ps or rs
        err=(x['portal_error'] or '') if ps else (x['relayer_error'] or '')
        items.append(dict(id=x['id'],user_id=x['user_id'],user_name=x['user_name'],username=x['username'],
            gift_name=x['gift_name'],image_url=x['image_url'],price_ton=x['floor_price']/100,status=x['status'],
            admin_id=x['admin_id'],admin_name=x['admin_name'],admin_username=x['admin_username'],
            created_at=x['created_at'],processed_at=x['processed_at'],external_url=x['external_url'] or '',
            delivery_provider=provider,auto_status=status,auto_error=err,
            auto_transfer_stars=int(x['relayer_transfer_stars'] or 0),
            auto_attempts=int((x['portal_attempts'] if ps else x['relayer_attempts']) or 0),
            auto_updated_at=(x['portal_updated_at'] if ps else x['relayer_updated_at']),
            manual_required=bool(x['status']=='pending' and status in failures),
            portal_stage=x['portal_stage'] or '',portal_nft_id=x['portal_nft_id'] or '',
            portal_nft_name=x['portal_nft_name'] or '',portal_source=x['portal_source'] or '',
            portal_purchase_price=x['portal_purchase_price'] or '0',
            portal_balance_before=x['portal_balance_before'] or '0',
            portal_withdrawal_ids=x['portal_withdrawal_ids'] or ''))
    return jsonify(items=items)


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
        db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,round_id,external_url)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (row['user_id'], row['gift_id'], row['gift_name'], row['image_url'],
                    row['floor_price'], row['source'], row['round_id'], row['external_url'] or ''))
        fee=int(row.get('fee_amount') or 0)
        if fee>0:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?',(fee,row['user_id']))
            record_transaction(db,row['user_id'],'withdrawal_fee_refund',fee,'withdrawal',withdrawal_id,'Возврат комиссии вывода')
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
                   upgrade_rtp=upgrade_rtp_basis_points()/100, crash_rtp=round(crash_rtp()*100, 2), hilo_rtp=round(hilo_rtp()*100, 2),
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
        crash_percent = float(data.get('crash_rtp', crash_rtp()*100))
        hilo_percent = float(data.get('hilo_rtp', hilo_rtp()*100))
    except (TypeError, ValueError):
        return error('Введите отдачу в процентах.')
    if not math.isfinite(percent) or not 80 <= percent <= 99.9:
        return error('Отдача Mines должна быть от 80 до 99.9%.')
    if not math.isfinite(promo_percent) or not 70 <= promo_percent <= 96.9:
        return error('Отдача промо-отыгрыша должна быть от 70 до 96.9%.')
    if promo_percent >= percent:
        return error('Отдача промо-отыгрыша должна быть ниже обычной отдачи.')
    if not math.isfinite(upgrade_percent) or not 1<=upgrade_percent<=100:
        return error('Отдача апгрейда должна быть от 1 до 100%.')
    if not math.isfinite(crash_percent) or not 80 <= crash_percent <= 99.9:
        return error('Отдача Crash должна быть от 80 до 99.9%.')
    if not math.isfinite(hilo_percent) or not 80 <= hilo_percent <= 99.9:
        return error('Отдача Hi-Lo должна быть от 80 до 99.9%.')
    if not math.isfinite(loss_boost) or not 0<=loss_boost<=15:
        return error('Максимальная прибавка отдачи от игрового минуса: от 0 до 15 п.п.')
    save_document('game_settings', {'rtp': percent/100, 'promo_rtp': promo_percent/100,
                                    'upgrade_rtp_bp':round(upgrade_percent*100),
                                    'loss_rtp_max_boost':round(loss_boost,2),
                                    'crash_rtp': crash_percent/100,
                                    'hilo_rtp': hilo_percent/100,
                                    'updated_at': datetime.now(timezone.utc).isoformat(),
                                    'admin_id': session['uid']})
    return jsonify(ok=True, rtp=round(game_rtp()*100, 2), promo_rtp=round(promo_game_rtp()*100, 2),
                   upgrade_rtp=upgrade_rtp_basis_points()/100, crash_rtp=round(crash_rtp()*100, 2), hilo_rtp=round(hilo_rtp()*100, 2),
                   loss_rtp_max_boost=round(loss_rtp_max_boost(),2))


def ton_settings():
    doc = read_document('ton_settings') or {}
    try:
        ref_percent = min(50.0, max(0.0, float(doc.get('referral_percent', 10) or 0)))
    except (TypeError, ValueError):
        ref_percent = 10.0
    try:
        stars_per_ton = int(doc.get('stars_per_ton', 100) or 100)
    except (TypeError, ValueError):
        stars_per_ton = 100
    stars_per_ton = max(1, min(100000, stars_per_ton))
    return dict(
        enabled=bool(doc.get('enabled', True)),
        recipient_wallet=str(doc.get('recipient_wallet') or '').strip()[:180],
        site_name=str(doc.get('site_name') or 'GemDrop').strip()[:48] or 'GemDrop',
        site_url=str(doc.get('site_url') or WEBAPP_URL or '').strip()[:500],
        icon_url=str(doc.get('icon_url') or '').strip()[:500],
        referral_percent=ref_percent,
        stars_enabled=bool(doc.get('stars_enabled', True)),
        stars_per_ton=stars_per_ton,
        gifts_enabled=bool(doc.get('gifts_enabled', True)),
        relay_username=str(doc.get('relay_username') or 'Gemdrop_relay').strip().lstrip('@')[:64] or 'Gemdrop_relay',
        stars_withdraw_days=21,
    )


@app.get('/api/ton/settings')
@login_required
def ton_settings_public():
    settings = ton_settings()
    return jsonify(enabled=settings['enabled'], recipient_wallet=settings['recipient_wallet'],
                   site_name=settings['site_name'], referral_percent=settings['referral_percent'],
                   stars_enabled=settings['stars_enabled'], stars_per_ton=settings['stars_per_ton'],
                   gifts_enabled=settings['gifts_enabled'], relay_username=settings['relay_username'],
                   gift_relayer_ready=relayer_public_ready(),
                   stars_withdraw_days=settings['stars_withdraw_days'])


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
    stars_enabled = bool(data.get('stars_enabled', True))
    gifts_enabled = bool(data.get('gifts_enabled', True))
    relay_username = str(data.get('relay_username') or 'Gemdrop_relay').strip().lstrip('@')[:64] or 'Gemdrop_relay'
    try:
        referral_pct = float(data.get('referral_percent', 10))
        stars_per_ton = int(data.get('stars_per_ton', 100))
    except (TypeError, ValueError):
        return error('Проверьте реферальный процент и курс Stars.')
    if not 0 <= referral_pct <= 50:
        return error('Реферальный процент должен быть от 0 до 50%.')
    if not 1 <= stars_per_ton <= 100000:
        return error('Курс Stars должен быть от 1 до 100 000 Stars за 1 TON.')
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
                                       stars_enabled=stars_enabled, stars_per_ton=stars_per_ton,
                                       gifts_enabled=gifts_enabled, relay_username=relay_username,
                                       updated_at=datetime.now(timezone.utc).isoformat(),
                                       admin_id=session['uid']))
    with connect() as db:
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], session['uid'], 'ton_settings',
                    json.dumps({'enabled': enabled, 'site_name': site_name, 'recipient_wallet': recipient,
                                'referral_percent': referral_pct, 'stars_enabled': stars_enabled,
                                'stars_per_ton': stars_per_ton, 'gifts_enabled': gifts_enabled,
                                'relay_username': relay_username}, ensure_ascii=False)))
    return jsonify(ok=True, **ton_settings())



# --- Telegram NFT relayer ---------------------------------------------------
RELAYER_SESSION_BASENAME = str(DATA / 'gemdrop_relayer')
RELAYER_SCAN_SECONDS = max(5, int(os.environ.get('RELAYER_SCAN_SECONDS', '12') or 12))


def relayer_settings():
    doc = read_document('relayer_settings') or {}
    ton = ton_settings()
    api_id = str(os.environ.get('TELEGRAM_API_ID') or doc.get('api_id') or '').strip()
    api_hash = str(os.environ.get('TELEGRAM_API_HASH') or doc.get('api_hash') or '').strip()
    # Gift delivery is the active/default flow. The TON conversion mode is kept
    # as a mutually-exclusive fallback for later, never both at the same time.
    delivery_mode = str(doc.get('delivery_mode') or 'inventory').strip().lower()
    if delivery_mode not in {'inventory', 'balance'}:
        delivery_mode = 'inventory'
    return dict(
        enabled=bool(doc.get('enabled', True)), api_id=api_id, api_hash=api_hash,
        relay_username=str(doc.get('relay_username') or ton.get('relay_username') or 'Gemdrop_relay').strip().lstrip('@')[:64] or 'Gemdrop_relay',
        delivery_mode=delivery_mode, credit_balance=(delivery_mode == 'balance'),
        keep_inventory=(delivery_mode == 'inventory'),
        auto_withdraw_enabled=bool(doc.get('auto_withdraw_enabled', True)),
    )


def relayer_public_ready():
    cfg = relayer_settings(); state = read_document('relayer_state') or {}
    return bool(cfg['enabled'] and cfg['api_id'] and cfg['api_hash'] and state.get('authorized'))


# Telegram/Fragment NFT intake is valued at 85% of the Portal quote.
# Portal includes a marketplace markup, so a 10 TON Portal quote becomes 8.50 TON in GemDrop.
GIFT_DEPOSIT_ACCEPT_RATE = Decimal('0.85')


def _gift_deposit_accept_cents(portal_cents):
    try: raw=max(0,int(portal_cents or 0))
    except (TypeError,ValueError): raw=0
    return int((Decimal(raw)*GIFT_DEPOSIT_ACCEPT_RATE).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if raw else 0


def relayer_public_catalog():
    items, seen = [], set()
    for gift in read_catalog().get('gifts', []):
        try: portal_price = ton_to_cents(gift.get('price_ton') or 0)
        except (ValueError, TypeError, InvalidOperation): portal_price = 0
        price=_gift_deposit_accept_cents(portal_price)
        if price <= 0: continue
        gid, name = str(gift.get('id') or ''), str(gift.get('name') or 'Подарок').strip() or 'Подарок'
        key = (gid, name.casefold())
        if key in seen: continue
        seen.add(key)
        items.append(dict(id=gid, name=name, image_url=safe_image(gift.get('image_url') or gift.get('portal_image_url')), price_ton=price/100))
    items.sort(key=lambda x: (float(x['price_ton']), x['name'].casefold()))
    return items


@app.get('/api/gift-deposits/catalog')
@login_required
def gift_deposit_catalog():
    settings = ton_settings()
    if not settings['gifts_enabled']: return error('Пополнение подарками временно отключено.', 503)
    return jsonify(items=relayer_public_catalog(), relay_username=settings['relay_username'], relayer_ready=relayer_public_ready())


def _relayer_state(**patch):
    old = read_document('relayer_state') or {}
    if not isinstance(old, dict): old = {}
    old.update(patch); old['updated_at'] = datetime.now(timezone.utc).isoformat(); save_document('relayer_state', old); return old


def _relayer_imports():
    try:
        from telethon import TelegramClient, functions
        from telethon.sessions import StringSession
        from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
        return TelegramClient, functions, StringSession, SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
    except Exception as exc:
        raise RuntimeError('Для relayer установите Telethon: pip install "telethon>=1.45,<2"') from exc


def _relayer_client():
    TelegramClient, _, StringSession, _, _, _ = _relayer_imports(); cfg = relayer_settings()
    if not cfg['api_id'] or not cfg['api_hash']: raise RuntimeError('Укажите TELEGRAM_API_ID и TELEGRAM_API_HASH или сохраните их в настройках relayer.')
    try: api_id = int(cfg['api_id'])
    except (TypeError, ValueError): raise RuntimeError('TELEGRAM_API_ID должен быть числом.')
    session_doc = read_document('relayer_session') or {}
    session_value = str(session_doc.get('value') or '') if isinstance(session_doc, dict) else ''
    return TelegramClient(StringSession(session_value), api_id, cfg['api_hash'], device_model='GemDrop Relayer', system_version='GemDrop', app_version=BUILD_ID, auto_reconnect=True)


def _relayer_save_session(client):
    try:
        value = client.session.save()
        if value:
            save_document('relayer_session', {'value': value, 'updated_at': datetime.now(timezone.utc).isoformat()})
    except Exception:
        app.logger.exception('Could not persist relayer Telegram session')


def _relayer_clear_session():
    with connect() as db:
        db.execute("DELETE FROM app_documents WHERE name='relayer_session'")


def _relayer_run(coro): return asyncio.run(coro)

def _relayer_account_dict(me):
    if not me: return {}
    return dict(id=int(getattr(me,'id',0) or 0), username=str(getattr(me,'username','') or ''), first_name=str(getattr(me,'first_name','') or ''), last_name=str(getattr(me,'last_name','') or ''))

def _relayer_stars_value(value):
    try:
        if value is None:return 0
        if isinstance(value,(int,float,Decimal)):return max(0,int(value))
        amount=getattr(value,'amount',None)
        return max(0,int(amount or 0))
    except (TypeError,ValueError):return 0


async def _relayer_live_stats_async():
    client=_relayer_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return dict(ok=False,status='login_required',stars_balance=0,saved_gifts_count=0)
        me=await client.get_me()
        gifts=await _relayer_saved_gifts(client)
        stars=0
        try:
            _,functions,_,_,_,_=_relayer_imports()
            payments=getattr(functions,'payments',None)
            cls=getattr(payments,'GetStarsStatusRequest',None) if payments else None
            if cls:
                peer=await client.get_input_entity('me')
                sig=inspect.signature(cls.__init__);kwargs={}
                for name,param in sig.parameters.items():
                    if name=='self':continue
                    if name=='peer':kwargs[name]=peer
                    elif param.default is inspect._empty:kwargs[name]=False
                status=await client(cls(**kwargs))
                stars=_relayer_stars_value(getattr(status,'balance',0))
        except Exception:
            app.logger.exception('Relayer Stars balance fetch failed')
        state=_relayer_state(authorized=True,status='online',account=_relayer_account_dict(me),
                             stars_balance=stars,saved_gifts_count=len(gifts),
                             balance_checked_at=datetime.now(timezone.utc).isoformat(),error='')
        return dict(ok=True,status='online',stars_balance=stars,saved_gifts_count=len(gifts),account=state.get('account') or {})
    finally:
        try:await client.disconnect()
        except Exception:pass



@app.get('/api/admin/relayer/status')
@admin_required
def admin_relayer_status():
    cfg = relayer_settings(); state = read_document('relayer_state') or {}; auth_raw = read_document('relayer_auth') or {}
    # Never return phone_code_hash or any future secret fields to the browser.
    auth = {key: auth_raw.get(key) for key in ('status','attempt_id','qr_url','updated_at') if auth_raw.get(key) not in (None,'')}
    with connect() as db:
        rows = db.execute('SELECT * FROM relayer_gift_events ORDER BY id DESC LIMIT 25').fetchall()
        withdrawals=db.execute("""SELECT l.*,u.name AS user_name,u.username AS username FROM relayer_withdrawal_logs l
                                  LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 25""").fetchall()
    return jsonify(enabled=cfg['enabled'], configured=bool(cfg['api_id'] and cfg['api_hash']), api_id=(cfg['api_id'][:3]+'…'+cfg['api_id'][-2:] if len(cfg['api_id'])>5 else cfg['api_id']), relay_username=cfg['relay_username'], delivery_mode=cfg['delivery_mode'], credit_balance=cfg['credit_balance'], keep_inventory=cfg['keep_inventory'], auto_withdraw_enabled=cfg.get('auto_withdraw_enabled',True), state=state, auth=auth,
                   stars_balance=int(state.get('stars_balance') or 0),saved_gifts_count=int(state.get('saved_gifts_count') or state.get('last_seen') or 0),balance_checked_at=state.get('balance_checked_at'),
                   events=[dict(id=r['id'],sender_user_id=r['sender_user_id'],sender_name=r['sender_name'],gift_name=r['gift_name'],fragment_number=r['fragment_number'],image_url=(r['image_url'] or (f"https://nft.fragment.com/gift/{re.sub(r'[^A-Za-z0-9_-]+','',str(r['external_url']).rsplit('/',1)[-1]).lower()}.webp" if '/nft/' in str(r['external_url'] or '') else '')),external_url=r['external_url'],price_ton=int(r['floor_price'] or 0)/100,status=r['status'],created_at=r['created_at'],credited_at=r['credited_at']) for r in rows],
                   auto_withdrawals=[dict(id=r['id'],withdrawal_id=r['withdrawal_id'],user_id=r['user_id'],user_name=r['user_name'] or '',username=r['username'] or '',gift_name=r['gift_name'],fragment_number=r['fragment_number'],status=r['status'],transfer_stars=int(r['transfer_stars'] or 0),error=r['error'] or '',created_at=r['created_at']) for r in withdrawals])


@app.post('/api/admin/relayer/check-balance')
@admin_required
def admin_relayer_check_balance():
    try:return jsonify(**_relayer_run(_relayer_live_stats_async()))
    except Exception as exc:
        app.logger.exception('Relayer balance check failed')
        _relayer_state(status='error',error=str(exc)[:180])
        return error('Не удалось проверить баланс Relayer: '+str(exc),409)


@app.post('/api/admin/relayer/settings')
@admin_required
def admin_relayer_settings():
    data = request.get_json(silent=True) or {}; previous = read_document('relayer_settings') or {}
    api_id = str(data.get('api_id') or previous.get('api_id') or '').strip(); api_hash = str(data.get('api_hash') or '').strip() or str(previous.get('api_hash') or '').strip()
    if api_id and not api_id.isdigit(): return error('API ID должен быть числом.')
    if api_hash and not re.fullmatch(r'[A-Za-z0-9]{20,80}', api_hash): return error('Проверьте API hash Telegram.')
    username = str(data.get('relay_username') or 'Gemdrop_relay').strip().lstrip('@')[:64] or 'Gemdrop_relay'
    delivery_mode=str(data.get('delivery_mode') or 'inventory').strip().lower()
    if delivery_mode not in {'inventory','balance'}: delivery_mode='inventory'
    save_document('relayer_settings', dict(enabled=bool(data.get('enabled',True)),api_id=api_id,api_hash=api_hash,relay_username=username,delivery_mode=delivery_mode,auto_withdraw_enabled=bool(data.get('auto_withdraw_enabled',True)),updated_at=datetime.now(timezone.utc).isoformat(),admin_id=session['uid']))
    ton_doc = read_document('ton_settings') or {}; ton_doc.update({'gifts_enabled':bool(data.get('gifts_enabled',True)),'relay_username':username,'updated_at':datetime.now(timezone.utc).isoformat(),'admin_id':session['uid']}); save_document('ton_settings',ton_doc)
    return jsonify(ok=True)


async def _relayer_phone_start(phone):
    client=_relayer_client()
    try:
        await client.connect()
        if await client.is_user_authorized():
            me=await client.get_me(); _relayer_state(authorized=True,status='online',account=_relayer_account_dict(me)); return dict(ok=True,authorized=True)
        sent=await client.send_code_request(phone)
        save_document('relayer_auth',{'status':'code_sent','phone':phone,'phone_code_hash':str(getattr(sent,'phone_code_hash','') or ''),'updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=False,status='code_sent'); return dict(ok=True,status='code_sent')
    finally:
        _relayer_save_session(client)
        await client.disconnect()

@app.post('/api/admin/relayer/login/phone')
@admin_required
def admin_relayer_phone():
    phone=re.sub(r'[^0-9+]','',str((request.get_json(silent=True) or {}).get('phone') or ''))
    if not re.fullmatch(r'\+?[0-9]{8,16}',phone): return error('Введите номер в международном формате, например +79990000000.')
    try: return jsonify(**_relayer_run(_relayer_phone_start(phone)))
    except (RuntimeError,ValueError) as exc: return error(str(exc),409)


async def _relayer_code_submit(code):
    _,_,_,SessionPasswordNeededError,PhoneCodeInvalidError,PhoneCodeExpiredError=_relayer_imports(); auth=read_document('relayer_auth') or {}; phone,phone_hash=str(auth.get('phone') or ''),str(auth.get('phone_code_hash') or '')
    if not phone or not phone_hash: raise RuntimeError('Сначала запросите код входа.')
    client=_relayer_client()
    try:
        await client.connect()
        try: await client.sign_in(phone=phone,code=code,phone_code_hash=phone_hash)
        except SessionPasswordNeededError:
            save_document('relayer_auth',dict(auth,status='need_password')); _relayer_state(authorized=False,status='need_password'); return dict(ok=True,status='need_password')
        except PhoneCodeInvalidError: raise RuntimeError('Неверный код Telegram.')
        except PhoneCodeExpiredError: raise RuntimeError('Код Telegram истёк. Запросите новый.')
        me=await client.get_me(); save_document('relayer_auth',{'status':'authorized','updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=True,status='online',account=_relayer_account_dict(me)); return dict(ok=True,status='authorized',account=_relayer_account_dict(me))
    finally:
        _relayer_save_session(client)
        await client.disconnect()

@app.post('/api/admin/relayer/login/code')
@admin_required
def admin_relayer_code():
    code=re.sub(r'\D','',str((request.get_json(silent=True) or {}).get('code') or ''))
    if not 3<=len(code)<=8: return error('Введите код из Telegram.')
    try: return jsonify(**_relayer_run(_relayer_code_submit(code)))
    except (RuntimeError,ValueError) as exc: return error(str(exc),409)


async def _relayer_password_submit(password):
    client=_relayer_client()
    try:
        await client.connect(); await client.sign_in(password=password); me=await client.get_me(); save_document('relayer_auth',{'status':'authorized','updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=True,status='online',account=_relayer_account_dict(me)); return dict(ok=True,status='authorized',account=_relayer_account_dict(me))
    finally:
        _relayer_save_session(client)
        await client.disconnect()

@app.post('/api/admin/relayer/login/password')
@admin_required
def admin_relayer_password():
    password=str((request.get_json(silent=True) or {}).get('password') or '')
    if not password: return error('Введите пароль 2FA.')
    try: return jsonify(**_relayer_run(_relayer_password_submit(password)))
    except Exception: app.logger.exception('Relayer 2FA login failed'); return error('Не удалось войти по 2FA. Проверьте пароль.',409)


async def _relayer_qr_wait(attempt_id):
    _,_,_,SessionPasswordNeededError,_,_=_relayer_imports(); client=_relayer_client()
    try:
        await client.connect()
        if await client.is_user_authorized():
            me=await client.get_me(); _relayer_state(authorized=True,status='online',account=_relayer_account_dict(me)); return
        qr=await client.qr_login(); save_document('relayer_auth',{'status':'qr_wait','attempt_id':attempt_id,'qr_url':str(qr.url),'updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=False,status='qr_wait')
        try: await qr.wait(timeout=90)
        except SessionPasswordNeededError:
            save_document('relayer_auth',{'status':'need_password','attempt_id':attempt_id,'updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=False,status='need_password'); return
        me=await client.get_me(); save_document('relayer_auth',{'status':'authorized','updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=True,status='online',account=_relayer_account_dict(me))
    except Exception as exc:
        app.logger.exception('Relayer QR login failed'); save_document('relayer_auth',{'status':'qr_error','attempt_id':attempt_id,'error':str(exc)[:180],'updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=False,status='login_error')
    finally:
        _relayer_save_session(client)
        try: await client.disconnect()
        except Exception: pass

def _relayer_qr_thread(attempt_id): _relayer_run(_relayer_qr_wait(attempt_id))

@app.post('/api/admin/relayer/login/qr')
@admin_required
def admin_relayer_qr():
    try:
        _relayer_imports(); cfg=relayer_settings()
        if not cfg['api_id'] or not cfg['api_hash']: return error('Сначала сохраните API ID и API hash Telegram.',409)
        attempt_id=secrets.token_hex(8); save_document('relayer_auth',{'status':'qr_starting','attempt_id':attempt_id,'updated_at':datetime.now(timezone.utc).isoformat()}); Thread(target=_relayer_qr_thread,args=(attempt_id,),daemon=True).start(); deadline=time.time()+6
        while time.time()<deadline:
            state=read_document('relayer_auth') or {}
            if state.get('attempt_id')==attempt_id and state.get('status')!='qr_starting': return jsonify(ok=True,**state)
            time.sleep(.15)
        return jsonify(ok=True,status='qr_starting',attempt_id=attempt_id)
    except RuntimeError as exc: return error(str(exc),409)


async def _relayer_logout():
    client=_relayer_client()
    try:
        await client.connect()
        if await client.is_user_authorized(): await client.log_out()
    finally:
        try: await client.disconnect()
        except Exception: pass

@app.post('/api/admin/relayer/logout')
@admin_required
def admin_relayer_logout():
    try: _relayer_run(_relayer_logout())
    except Exception: app.logger.exception('Relayer logout failed')
    _relayer_clear_session(); save_document('relayer_auth',{'status':'logged_out','updated_at':datetime.now(timezone.utc).isoformat()}); _relayer_state(authorized=False,status='logged_out',account={}); return jsonify(ok=True)


def _relayer_peer_user_id(peer):
    if peer is None: return 0
    if isinstance(peer,int): return int(peer)
    for name in ('user_id','id'):
        try:
            value=int(getattr(peer,name,0) or 0)
            if value:return value
        except (TypeError,ValueError):pass
    return 0

def _relayer_json(value):
    try:
        if hasattr(value,'to_dict'): value=value.to_dict()
        return json.dumps(value,ensure_ascii=False,default=str)[:15000]
    except Exception:return '{}'

def _relayer_entry_info(entry):
    gift=getattr(entry,'gift',None) or entry
    slug=str(getattr(gift,'slug','') or getattr(entry,'slug','') or '').strip()
    title=str(getattr(gift,'title','') or getattr(gift,'name','') or getattr(entry,'title','') or '').strip()
    number=str(getattr(gift,'num','') or getattr(gift,'number','') or getattr(entry,'gift_num','') or getattr(entry,'num','') or '').strip()
    slug_match=re.match(r'^(.+?)-(\d+)$',slug) if slug else None
    if slug_match:
        if not number: number=slug_match.group(2)
        if not title or title.casefold()=='telegram nft':
            title=re.sub(r'(?<=[a-z0-9])(?=[A-Z])',' ',slug_match.group(1)).replace('_',' ').strip()
    title=title or 'Telegram NFT'
    gift_id=str(getattr(gift,'id','') or getattr(entry,'gift_id','') or '')
    saved_id=str(getattr(entry,'saved_id','') or getattr(entry,'msg_id','') or getattr(entry,'id','') or '')
    sender=_relayer_peer_user_id(getattr(entry,'from_id',None) or getattr(entry,'sender_id',None))
    date=getattr(entry,'date',None)
    telegram_url=('https://t.me/nft/'+slug) if slug else ''
    key=saved_id or slug or f'{gift_id}:{sender}:{date}'
    return dict(external_key='tg:'+str(key),sender_user_id=sender,gift_id=gift_id,gift_name=title,slug=slug,fragment_number=number,external_url=telegram_url,telegram_url=telegram_url,raw_json=_relayer_json(entry))

def _relayer_norm(value): return re.sub(r'[^a-z0-9]+','',str(value or '').casefold())

def _relayer_resolve_gift(info):
    # Portal is the authoritative rate card. For collectible NFTs, resolve the
    # exact Fragment/Telegram metadata first so Black/Onyx/model prices can use
    # the matching Portal trait floor instead of a generic collection price.
    info = dict(info or {})
    catalog=read_catalog(include_hidden=True).get('gifts',[]); candidates=[]
    for g in catalog:
        gid,name=str(g.get('id') or ''),str(g.get('name') or ''); score=100 if info.get('gift_id') and gid==info['gift_id'] else 0; n1,n2=_relayer_norm(name),_relayer_norm(info.get('gift_name')); n3=_relayer_norm(re.sub(r'-\d+$','',str(info.get('slug') or ''),flags=re.I))
        if n1 and n2 and (n1==n2 or n1 in n2 or n2 in n1):score+=60
        if n1 and n3 and (n1==n3 or n1 in n3 or n3 in n1):score+=50
        if score:candidates.append((score,g))
    gift=max(candidates,key=lambda x:x[0])[1] if candidates else None; fragment=None
    if info.get('slug'):
        try: fragment=fragment_gift_from_url('https://fragment.com/gift/'+info['slug'],True,allow_missing_price=True)
        except Exception: fragment=None

    catalog_price=0
    if gift:
        try: catalog_price=ton_to_cents(gift.get('price_ton') or 0)
        except Exception: catalog_price=0
        image=safe_image(gift.get('image_url') or gift.get('portal_image_url')); name=str(info.get('gift_name') or gift.get('name') or 'Telegram NFT'); gid=str(gift.get('id') or info.get('gift_id') or info.get('slug') or '')
    else:
        image,name,gid='',str(info.get('gift_name') or 'Telegram NFT'),str(info.get('gift_id') or info.get('slug') or '')

    price=max(0,int(catalog_price or 0)); price_source='Portal · каталог' if price else ''
    if fragment:
        image=safe_image(fragment.get('image_url')) or image; name=str(fragment.get('gift_name') or name)
        info['fragment_number']=str(fragment.get('fragment_number') or info.get('fragment_number') or '')
        # Never replace the public Telegram NFT link with Fragment. Fragment is
        # used to resolve exact metadata/PNG/traits and price only.
        info['telegram_url']=str(info.get('telegram_url') or info.get('external_url') or '')
        info['fragment_lookup_url']=str(fragment.get('fragment_url') or '')
        info['external_url']=info['telegram_url']
        info['fragment_model']=str(fragment.get('fragment_model') or '')
        info['fragment_backdrop']=str(fragment.get('fragment_backdrop') or '')
        info['fragment_symbol']=str(fragment.get('fragment_symbol') or '')
        info['animation_url']=safe_image(fragment.get('animation_url'))
        try:
            portal_exact, portal_source = _fragment_portal_fallback_price(
                fragment.get('collection_name') or re.sub(r'\s*#\s*\d+.*$', '', name).strip(),
                info['fragment_model'], info['fragment_backdrop'])
        except Exception:
            portal_exact, portal_source = 0, ''
        if portal_exact:
            price, price_source = int(portal_exact), str(portal_source or 'Portal')
        elif not price and int(fragment.get('floor_price') or 0)>0:
            price, price_source = int(fragment.get('floor_price') or 0), str(fragment.get('price_source') or 'Fragment')
    if not image and info.get('slug'):
        slug_safe=re.sub(r'[^A-Za-z0-9_-]+','',str(info.get('slug') or '')).lower()
        if slug_safe:image=f'https://nft.fragment.com/gift/{slug_safe}.webp'
    info.update(gift_id=gid,gift_name=name,image_url=image,floor_price=max(0,int(price)),price_source=price_source)
    return info


def _relayer_credit(info,baseline=False):
    cfg=relayer_settings(); db=connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM relayer_gift_events WHERE external_key=?',(info['external_key'],)).fetchone(): db.rollback(); return 'duplicate'
        uid=int(info.get('sender_user_id') or 0); user_row=db.execute('SELECT id,name,username FROM users WHERE id=?',(uid,)).fetchone() if uid else None
        if not info.get('slug'):
            db.execute("INSERT INTO relayer_gift_events(external_key,sender_user_id,gift_id,gift_name,image_url,external_url,fragment_number,floor_price,status,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(info['external_key'],uid,info.get('gift_id',''),info.get('gift_name',''),info.get('image_url',''),info.get('external_url',''),info.get('fragment_number',''),0,'not_nft',info.get('raw_json','{}'))); db.commit(); return 'not_nft'
        if baseline:
            db.execute("INSERT INTO relayer_gift_events(external_key,sender_user_id,gift_id,gift_name,image_url,external_url,fragment_number,floor_price,status,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(info['external_key'],uid,info.get('gift_id',''),info.get('gift_name',''),info.get('image_url',''),info.get('external_url',''),info.get('fragment_number',''),int(info.get('floor_price') or 0),'baseline',info.get('raw_json','{}'))); db.commit(); return 'baseline'
        if not user_row:
            db.execute("INSERT INTO relayer_gift_events(external_key,sender_user_id,gift_id,gift_name,image_url,external_url,fragment_number,floor_price,status,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(info['external_key'],uid,info.get('gift_id',''),info.get('gift_name',''),info.get('image_url',''),info.get('external_url',''),info.get('fragment_number',''),int(info.get('floor_price') or 0),'unmatched',info.get('raw_json','{}'))); db.commit(); return 'unmatched'
        portal_price=max(0,int(info.get('floor_price') or 0))
        price=_gift_deposit_accept_cents(portal_price)
        mode=cfg.get('delivery_mode') or 'inventory'
        if mode=='balance' and price<=0:
            db.execute("INSERT INTO relayer_gift_events(external_key,sender_user_id,sender_name,gift_id,gift_name,image_url,external_url,fragment_number,floor_price,status,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(info['external_key'],uid,user_row['name'],info.get('gift_id',''),info.get('gift_name',''),info.get('image_url',''),info.get('external_url',''),info.get('fragment_number',''),0,'unpriced',info.get('raw_json','{}'))); db.commit(); return 'unpriced'
        inventory_id=None
        if mode=='inventory':
            cur=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,external_url,fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url,source_label,deposit_mirror) VALUES(?,?,?,?,?,'gift_deposit',?,?,?,?,?,?,?,?,0)",(uid,info.get('gift_id',''),info.get('gift_name',''),info.get('image_url',''),price,info.get('external_url',''),info.get('fragment_number',''),info.get('fragment_model',''),info.get('fragment_backdrop',''),info.get('fragment_symbol',''),str(info.get('price_source') or 'Portal') + ' · −15%',info.get('animation_url',''),'Пополнение подарком · NFT в инвентаре')); inventory_id=cur.lastrowid
            record_transaction(db,uid,'gift_deposit_inventory',0,'telegram_nft',info['external_key'],f'{info.get("gift_name") or "NFT"} · #{info.get("fragment_number") or "—"}')
            event_status='delivered'
        else:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?',(price,uid))
            record_transaction(db,uid,'gift_deposit',price,'telegram_nft',info['external_key'],f'{info.get("gift_name") or "NFT"} · #{info.get("fragment_number") or "—"}')
            event_status='credited'
        db.execute("INSERT INTO relayer_gift_events(external_key,sender_user_id,sender_name,gift_id,gift_name,image_url,external_url,fragment_number,floor_price,inventory_id,status,raw_json,credited_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,CURRENT_TIMESTAMP)",(info['external_key'],uid,user_row['name'],info.get('gift_id',''),info.get('gift_name',''),info.get('image_url',''),info.get('external_url',''),info.get('fragment_number',''),price,inventory_id,event_status,info.get('raw_json','{}')))
        db.execute('UPDATE relayer_gift_events SET portal_price=? WHERE external_key=?',(portal_price,info['external_key']))
        db.commit()
    finally: db.close()
    gift_name=escape(str(info.get('gift_name') or 'Telegram NFT'))
    number=str(info.get('fragment_number') or '').strip()
    raw_name=str(info.get('gift_name') or 'Telegram NFT')
    already_numbered=bool(number and re.search(r'(?:#|\b)'+re.escape(number)+r'\b',raw_name))
    display_name=gift_name + (f' #{escape(number)}' if number and not already_numbered else '')
    telegram_link=str(info.get('telegram_url') or info.get('external_url') or '')
    if re.match(r'^https://t\.me/nft/[A-Za-z0-9_-]+$',telegram_link,re.I):
        gift_line=f'<a href="{escape(telegram_link, quote=True)}"><b>{display_name}</b></a>'
    else:
        gift_line=f'<b>{display_name}</b>'
    if mode=='inventory':
        text=f'🎁 <b>Ваш подарок был доставлен</b>\n\n{gift_line}\nПодарок добавлен в ваш инвентарь GemDrop.'
        if price>0: text+=f'\nОценочная стоимость: <b>{price/100:.2f} TON</b>'
        text+='\n\nНажмите на название подарка, чтобы открыть сам NFT в Telegram.'
        markup=miniapp_markup('🎁 Открыть инвентарь','profile')
    else:
        text=f'✅ <b>Подарок конвертирован в баланс</b>\n\n{gift_line}\nЗачислено: <b>{price/100:.2f} TON</b>'
        markup=miniapp_markup('Открыть GemDrop','profile')
    try: notify_user_async(uid,text,markup,'HTML')
    except Exception: app.logger.exception('Gift deposit notification failed')
    return 'credited'


async def _relayer_saved_gifts(client):
    _,functions,_,_,_,_=_relayer_imports(); payments=getattr(functions,'payments',None); cls=getattr(payments,'GetSavedStarGiftsRequest',None) if payments else None
    if cls is None and payments: cls=getattr(payments,'GetSavedGiftsRequest',None)
    if cls is None: raise RuntimeError('В установленной версии Telethon нет API Saved Star Gifts. Обновите Telethon.')
    peer=await client.get_input_entity('me'); values=dict(peer=peer,offset='',limit=100,exclude_unsaved=False,exclude_saved=False,exclude_unlimited=False,exclude_limited=False,exclude_unique=False,sort_by_value=False,ascending=False); sig=inspect.signature(cls.__init__); kwargs={}
    for name,param in sig.parameters.items():
        if name=='self':continue
        if name in values:kwargs[name]=values[name]
        elif param.default is inspect._empty:
            if name.startswith(('exclude_','sort_','ascending','descending')):kwargs[name]=False
            elif name=='limit':kwargs[name]=100
            elif name=='offset':kwargs[name]=''
            elif name=='peer':kwargs[name]=peer
            else:kwargs[name]=False
    result=await client(cls(**kwargs)); return list(getattr(result,'gifts',None) or getattr(result,'saved_gifts',None) or [])


async def _relayer_stars_balance(client):
    _,functions,_,_,_,_=_relayer_imports()
    payments=getattr(functions,'payments',None);cls=getattr(payments,'GetStarsStatusRequest',None) if payments else None
    if cls is None:return 0
    peer=await client.get_input_entity('me');sig=inspect.signature(cls.__init__);kwargs={}
    for name,param in sig.parameters.items():
        if name=='self':continue
        if name=='peer':kwargs[name]=peer
        elif param.default is inspect._empty:kwargs[name]=False
    return _relayer_stars_value(getattr(await client(cls(**kwargs)),'balance',0))

def _relayer_slug_from_url(value):
    m=re.search(r'https?://(?:t\\.me/nft|fragment\\.com/gift)/([A-Za-z0-9_-]+)',str(value or ''),re.I)
    return m.group(1) if m else ''

def _relayer_withdrawal_target(withdrawal_id):
    with connect() as db:
        row=db.execute("""SELECT w.*,u.name AS user_name,u.username,e.external_key AS deposit_external_key,
                                 e.external_url AS deposit_external_url
                          FROM withdrawals w JOIN users u ON u.id=w.user_id
                          LEFT JOIN relayer_gift_events e ON e.inventory_id=w.inventory_id WHERE w.id=?""",(withdrawal_id,)).fetchone()
    return dict(row) if row else None

def _relayer_transfer_meta(entry,info):
    out=dict(info or {});gift=getattr(entry,'gift',None) or entry
    try:out['transfer_stars']=max(0,int(getattr(entry,'transfer_stars',0) or getattr(gift,'transfer_stars',0) or 0))
    except (TypeError,ValueError):out['transfer_stars']=0
    raw=getattr(entry,'can_transfer_at',None) or getattr(gift,'can_transfer_at',None)
    if hasattr(raw,'timestamp'):
        try:raw=int(raw.timestamp())
        except Exception:raw=0
    try:out['can_transfer_at']=max(0,int(raw or 0))
    except (TypeError,ValueError):out['can_transfer_at']=0
    return out

def _relayer_match_withdrawal(row,gifts):
    target_slug=_relayer_slug_from_url(row.get('deposit_external_url') or row.get('external_url'))
    target_number=str(row.get('fragment_number') or '').strip()
    target_name=_relayer_norm(re.sub(r'\\s*\\((?:Black|Onyx|Onyx Black)\\)\\s*$','',str(row.get('gift_name') or ''),flags=re.I))
    infos=[]
    for entry in gifts:
        info=_relayer_transfer_meta(entry,_relayer_entry_info(entry))
        if not info.get('slug'):continue
        infos.append((entry,info))
        if target_slug and str(info.get('slug') or '').casefold()==target_slug.casefold():return entry,info
    if target_number:
        for entry,info in infos:
            if str(info.get('fragment_number') or '')==target_number:return entry,info
        return None,None
    for entry,info in infos:
        live=_relayer_norm(info.get('gift_name'));base=_relayer_norm(re.sub(r'-\\d+$','',str(info.get('slug') or ''),flags=re.I))
        if target_name and (target_name==live or target_name==base or target_name in live or live in target_name):return entry,info
    return None,None

def _relayer_auto_log(withdrawal_id,row,status,info=None,transfer_stars=0,error_text=''):
    info=info or {};inc=1 if status=='sending' else 0
    with connect() as db:
        db.execute("""INSERT INTO relayer_withdrawal_logs(withdrawal_id,user_id,inventory_id,external_key,gift_slug,gift_name,
                      fragment_number,status,transfer_stars,attempts,error,updated_at,completed_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CASE WHEN ?='completed' THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE NULL END)
                      ON CONFLICT(withdrawal_id) DO UPDATE SET external_key=excluded.external_key,gift_slug=excluded.gift_slug,
                      status=excluded.status,transfer_stars=excluded.transfer_stars,
                      attempts=relayer_withdrawal_logs.attempts+CASE WHEN excluded.status='sending' THEN 1 ELSE 0 END,
                      error=excluded.error,updated_at=CURRENT_TIMESTAMP,
                      completed_at=CASE WHEN excluded.status='completed' THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE relayer_withdrawal_logs.completed_at END""",
                   (withdrawal_id,int(row['user_id']),int(row.get('inventory_id') or 0),
                    str(info.get('external_key') or row.get('deposit_external_key') or ''),str(info.get('slug') or ''),
                    str(row.get('gift_name') or ''),str(row.get('fragment_number') or ''),status,
                    max(0,int(transfer_stars or 0)),inc,str(error_text or '')[:500],status))
        db.commit()

async def _relayer_transfer_saved_gift(client,row,info):
    from telethon import types
    _,functions,_,_,_,_=_relayer_imports()
    can_at=int(info.get('can_transfer_at') or 0)
    if can_at and can_at>int(time.time()):raise RuntimeError(f'NFT пока нельзя передать: Telegram transfer lock ещё {can_at-int(time.time())} сек.')
    slug=str(info.get('slug') or '')
    if not slug:raise RuntimeError('У NFT отсутствует Telegram slug.')
    saved_cls=getattr(types,'InputSavedStarGiftSlug',None)
    if saved_cls is None:raise RuntimeError('Текущая версия Telethon не поддерживает InputSavedStarGiftSlug.')
    username=str(row.get('username') or '').strip().lstrip('@')
    try:peer=await client.get_input_entity('@'+username if username else int(row['user_id']))
    except Exception as exc:raise RuntimeError('Не удалось определить Telegram-получателя.') from exc
    stargift=saved_cls(slug=slug);transfer_stars=max(0,int(info.get('transfer_stars') or 0));payments=getattr(functions,'payments',None)
    if transfer_stars:
        if await _relayer_stars_balance(client)<transfer_stars:raise RuntimeError('Недостаточно Stars на Relayer.')
        invoice=types.InputInvoiceStarGiftTransfer(stargift=stargift,to_id=peer)
        form=await client(functions.payments.GetPaymentFormRequest(invoice=invoice))
        await client(functions.payments.SendStarsFormRequest(form_id=form.form_id,invoice=invoice))
    else:
        await client(functions.payments.TransferStarGiftRequest(stargift=stargift,to_id=peer))
    return transfer_stars

def _complete_auto_withdrawal(withdrawal_id,row,provider,info=None,paid=0):
    changed=False
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT id FROM withdrawals WHERE id=? AND status='pending'",(withdrawal_id,)).fetchone():
            db.execute("UPDATE withdrawals SET status='approved',admin_id=0,processed_at=CURRENT_TIMESTAMP WHERE id=?",(withdrawal_id,))
            record_transaction(db,int(row['user_id']),'withdrawal_auto',0,'withdrawal',withdrawal_id,str(row.get('gift_name') or 'NFT'))
            log_event(db,int(row['user_id']),'withdrawal_auto_completed',withdrawal_id=withdrawal_id,gift_name=row.get('gift_name') or '',provider=provider)
            changed=True
        db.commit()
    if provider=='relayer':_relayer_auto_log(withdrawal_id,row,'completed',info or {},paid)
    if changed:
        notify_user_async(int(row['user_id']),f'✅ <b>Ваш подарок успешно выведен</b>\\n\\n🎁 {escape(str(row.get("gift_name") or "NFT"))}',
                          miniapp_markup('Открыть GemDrop','profile'),'HTML')
    return dict(ok=True,status='completed',provider=provider)

async def _relayer_auto_withdraw_one_async(withdrawal_id,client=None,gifts=None):
    row=_relayer_withdrawal_target(withdrawal_id)
    if not row or str(row.get('status') or '')!='pending':return dict(ok=False,status='not_pending')
    own=client is None
    try:
        if own:
            client=_relayer_client();await client.connect()
        if not await client.is_user_authorized():raise RuntimeError('Relayer не авторизован.')
        gifts=gifts if gifts is not None else await _relayer_saved_gifts(client)
        _,info=_relayer_match_withdrawal(row,gifts)
        if not info:
            _relayer_auto_log(withdrawal_id,row,'not_found',error_text='Подарок не найден на Relayer. Переходим в Portal Market.')
            notify_user_async(int(row['user_id']),
                f'🔎 <b>Подарок не найден на Relayer</b>\\n\\n🎁 {escape(str(row.get("gift_name") or "Подарок"))}\\n'
                'Пробуем купить самый доступный подходящий подарок через Portal Market.',
                miniapp_markup('Открыть GemDrop','profile'),'HTML')
            return _portal_fallback_withdraw(withdrawal_id,row)
        _relayer_auto_log(withdrawal_id,row,'sending',info,info.get('transfer_stars') or 0)
        try:paid=await _relayer_transfer_saved_gift(client,row,info)
        except Exception as exc:
            msg=str(exc)[:450];low=msg.casefold()
            status='insufficient_stars' if 'stars' in low else ('blocked' if 'lock' in low or 'пока нельзя' in low else ('recipient_unavailable' if 'получател' in low else 'error'))
            msg=msg.rstrip('.')+'. Требуется ручной вывод.'
            _relayer_auto_log(withdrawal_id,row,status,info,info.get('transfer_stars') or 0,msg)
            return dict(ok=False,status=status,error=msg,manual_required=True)
        return _complete_auto_withdrawal(withdrawal_id,row,'relayer',info,paid)
    except Exception as exc:
        msg=str(exc)[:450].rstrip('.')+'. Требуется ручной вывод.'
        _relayer_auto_log(withdrawal_id,row,'login_required' if 'авторизован' in msg.casefold() else 'error',error_text=msg)
        return dict(ok=False,status='error',error=msg,manual_required=True)
    finally:
        if own and client is not None:
            try:_relayer_save_session(client)
            except Exception:pass
            try:await client.disconnect()
            except Exception:pass

async def _relayer_process_pending_with_client(client,gifts,limit=6):
    with connect() as db:
        rows=db.execute("""SELECT w.id FROM withdrawals w LEFT JOIN relayer_withdrawal_logs l ON l.withdrawal_id=w.id
                           LEFT JOIN portal_withdrawal_logs p ON p.withdrawal_id=w.id
                           WHERE w.status='pending' AND p.id IS NULL AND (l.id IS NULL OR l.status='queued')
                           ORDER BY w.id LIMIT ?""",(max(1,min(20,int(limit))),)).fetchall()
    done=0
    for x in rows:
        r=await _relayer_auto_withdraw_one_async(int(x['id']),client=client,gifts=gifts);done+=1 if r.get('status')=='completed' else 0
    return dict(processed=len(rows),completed=done)

def _auto_withdrawal_thread(withdrawal_id):
    try:_relayer_run(_relayer_auto_withdraw_one_async(int(withdrawal_id)))
    except Exception as exc:
        app.logger.exception('NFT auto withdrawal failed')
        row=_relayer_withdrawal_target(withdrawal_id)
        if row:_relayer_auto_log(withdrawal_id,row,'error',error_text=str(exc)[:450]+'. Требуется ручной вывод.')


async def _relayer_scan_async(force=False):
    cfg=relayer_settings()
    if not cfg['enabled'] and not force:return dict(ok=False,status='disabled',processed=0)
    client=_relayer_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            _relayer_state(authorized=False,status='login_required')
            return dict(ok=False,status='login_required',processed=0)
        me=await client.get_me();gifts=await _relayer_saved_gifts(client);state=read_document('relayer_state') or {}
        baseline=not bool(state.get('baseline_ready'));processed=credited=0
        try:stars_balance=await _relayer_stars_balance(client)
        except Exception:
            app.logger.exception('Relayer Stars balance refresh failed');stars_balance=int(state.get('stars_balance') or 0)
        for entry in gifts:
            info=_relayer_resolve_gift(_relayer_entry_info(entry));result=_relayer_credit(info,baseline=baseline)
            processed+=0 if result=='duplicate' else 1;credited+=1 if result=='credited' else 0
        auto=await _relayer_process_pending_with_client(client,gifts)
        _relayer_state(authorized=True,status='online',account=_relayer_account_dict(me),baseline_ready=True,
                       last_scan=datetime.now(timezone.utc).isoformat(),last_seen=len(gifts),saved_gifts_count=len(gifts),
                       stars_balance=stars_balance,balance_checked_at=datetime.now(timezone.utc).isoformat(),
                       last_processed=processed,last_credited=credited,last_auto_processed=auto['processed'],
                       last_auto_completed=auto['completed'],error='')
        return dict(ok=True,status='online',processed=processed,credited=credited,baseline=baseline,
                    stars_balance=stars_balance,saved_gifts_count=len(gifts),
                    auto_processed=auto['processed'],auto_completed=auto['completed'])
    finally:
        try:_relayer_save_session(client)
        except Exception:pass
        await client.disconnect()

@app.post('/api/admin/relayer/scan')
@admin_required
def admin_relayer_scan():
    try:return jsonify(**_relayer_run(_relayer_scan_async(force=True)))
    except Exception as exc: app.logger.exception('Manual relayer scan failed'); _relayer_state(status='error',error=str(exc)[:180]); return error(str(exc),409)


def relayer_auto_loop():
    time.sleep(5)
    while True:
        try:
            cfg=relayer_settings()
            if cfg['enabled'] and cfg['api_id'] and cfg['api_hash']:_relayer_run(_relayer_scan_async())
        except Exception as exc:
            app.logger.exception('Relayer scan failed');_relayer_state(status='error',error=str(exc)[:180])
        try:_portal_resume_pending()
        except Exception:app.logger.exception('Portal withdrawal resume failed')
        time.sleep(RELAYER_SCAN_SECONDS)


def toncenter_headers():
    headers = {'Accept': 'application/json'}
    if TONCENTER_API_KEY:
        headers['X-API-Key'] = TONCENTER_API_KEY
    return headers


def add_withdrawal_wager_requirement(db,user_id,deposit_amount):
    required=max(0,int(deposit_amount or 0))*2
    if required:
        db.execute('UPDATE users SET withdrawal_wager_required=withdrawal_wager_required+? WHERE id=?',(required,user_id))
    return required


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
    add_withdrawal_wager_requirement(db,user_id,amount)
    if referrer and bonus:
        # Referral income goes to a separate referral balance; the referrer moves it to the main balance
        # themselves once it reaches REFERRAL_MIN_WITHDRAW_CENTS.
        db.execute('UPDATE users SET ref_balance=ref_balance+? WHERE id=?', (bonus, referrer))
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
                            JOIN promo_codes p ON p.code=r.code WHERE r.user_id=? AND r.reward_type='deposit_bonus'                            AND r.consumed_at IS NULL AND r.deactivated_at IS NULL
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


STARS_WITHDRAWAL_DAYS = 21


def _active_deposit_promo(db, user_id, promo_code):
    if not promo_code:
        return None
    return db.execute("""SELECT p.code,p.bonus_percent,p.bonus_fixed,p.min_deposit
                         FROM promo_redemptions r
                         JOIN promo_codes p ON p.code=r.code
                         WHERE r.user_id=? AND r.reward_type='deposit_bonus'
                         AND r.code=? AND r.consumed_at IS NULL AND r.deactivated_at IS NULL"""
                      + (' FOR UPDATE OF r' if DATABASE_URL else ''),
                      (user_id, promo_code)).fetchone()


def _consume_deposit_bonus(db, user_id, amount, promo_code):
    active = _active_deposit_promo(db, user_id, promo_code)
    if not active or amount < int(active['min_deposit'] or 0):
        return 0, active
    bonus = round(amount * float(active['bonus_percent'] or 0) / 100) + int(active['bonus_fixed'] or 0)
    consumed = db.execute("""UPDATE promo_redemptions SET consumed_at=CURRENT_TIMESTAMP
                             WHERE code=? AND user_id=? AND consumed_at IS NULL""",
                          (active['code'], user_id))
    return (bonus if consumed.rowcount else 0), active


def _apply_stars_withdrawal_lock(db, user_id):
    now = datetime.now(timezone.utc)
    candidate = now + timedelta(days=STARS_WITHDRAWAL_DAYS)
    row = db.execute('SELECT stars_withdrawal_until FROM users WHERE id=?', (user_id,)).fetchone()
    current = parse_datetime_utc(row['stars_withdrawal_until']) if row else None
    until = max(candidate, current) if current and current > now else candidate
    stored = until.strftime('%Y-%m-%d %H:%M:%S')
    db.execute('UPDATE users SET stars_withdrawal_until=? WHERE id=?', (stored, user_id))
    return until


def credit_verified_stars_deposit(db, order, payment_charge_id):
    """Credit one confirmed Telegram Stars invoice exactly once."""
    user_id = int(order['user_id'])
    amount = int(order['amount'])
    deposit_bonus, promo = _consume_deposit_bonus(db, user_id, amount, order['promo_code'] or '')
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount + deposit_bonus, user_id))
    add_withdrawal_wager_requirement(db,user_id,amount)
    db.execute("""INSERT INTO deposits(user_id,amount,referrer_id,referral_bonus,admin_id,request_key)
                  VALUES(?,?,NULL,0,0,?)""",
               (user_id, amount, 'stars:' + str(order['id'])))
    db.execute("""UPDATE stars_deposit_orders
                  SET status='credited',telegram_payment_charge_id=?,credited_at=CURRENT_TIMESTAMP
                  WHERE id=?""", (payment_charge_id, order['id']))
    lock_until = _apply_stars_withdrawal_lock(db, user_id)
    record_transaction(db, user_id, 'stars_deposit', amount, 'telegram_stars', payment_charge_id,
                       f'Пополнение через Telegram Stars · {int(order["stars_amount"])} Stars')
    if deposit_bonus:
        record_transaction(db, user_id, 'deposit_promo_bonus', deposit_bonus, 'telegram_stars',
                           payment_charge_id, f'Бонус промокода {promo["code"]}')
    log_event(db, user_id, 'deposit_confirmed', amount=amount/100, bonus=deposit_bonus/100,
              promo_code=promo['code'] if deposit_bonus and promo else '',
              payment='stars', stars=int(order['stars_amount']), order_id=order['id'])
    return deposit_bonus, lock_until


@app.post('/api/stars/deposit/create')
@login_required
def create_stars_deposit():
    settings = ton_settings()
    if not settings['stars_enabled']:
        return error('Пополнение Stars временно отключено.', 503)
    if not BOT_TOKEN:
        return error('Telegram-бот не настроен для оплаты Stars.', 503)
    data = request.get_json(silent=True) or {}
    try:
        amount = parse_amount(data.get('amount'))
    except (ValueError, InvalidOperation, TypeError):
        return error('Введите сумму с точностью до 0.01 TON.')
    if not 1 <= amount <= 100000000:
        return error('Сумма пополнения должна быть от 0.01 до 1 000 000 TON.')
    stars_amount = max(1, int(math.ceil(amount * int(settings['stars_per_ton']) / 100)))
    order_id = secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]
    invoice_payload = f'stars:{order_id}'
    with connect() as db:
        db.execute("UPDATE stars_deposit_orders SET status='expired' WHERE user_id=? AND status='pending'",
                   (session['uid'],))
        promo = db.execute("""SELECT r.code,p.bonus_percent,p.bonus_fixed,p.min_deposit
                              FROM promo_redemptions r JOIN promo_codes p ON p.code=r.code
                              WHERE r.user_id=? AND r.reward_type='deposit_bonus'
                              AND r.consumed_at IS NULL AND r.deactivated_at IS NULL
                              ORDER BY r.created_at DESC LIMIT 1""", (session['uid'],)).fetchone()
        promo_code = promo['code'] if promo else ''
        db.execute("""INSERT INTO stars_deposit_orders
                      (id,user_id,amount,stars_amount,promo_code,invoice_payload,status)
                      VALUES(?,?,?,?,?,?,'pending')""",
                   (order_id, session['uid'], amount, stars_amount, promo_code, invoice_payload))
        log_event(db, session['uid'], 'deposit_created', amount=amount/100, promo_code=promo_code,
                  order_id=order_id, payment='stars', stars=stars_amount)
    try:
        invoice_url = telegram_api('createInvoiceLink', {
            'title': 'Пополнение GemDrop',
            'description': f'Пополнение игрового баланса на {amount / 100:.2f} TON',
            'payload': invoice_payload,
            'currency': 'XTR',
            'prices': [{'label': f'{amount / 100:.2f} TON', 'amount': stars_amount}],
        }, timeout=(3, 12))
    except RuntimeError as exc:
        with connect() as db:
            db.execute("UPDATE stars_deposit_orders SET status='failed' WHERE id=? AND status='pending'",
                       (order_id,))
        return error(f'Не удалось создать счёт Stars: {exc}', 502)
    return jsonify(ok=True, order_id=order_id, invoice_url=invoice_url, stars=stars_amount,
                   amount=amount/100, stars_per_ton=settings['stars_per_ton'],
                   withdraw_days=STARS_WITHDRAWAL_DAYS,
                   deposit_bonus=(round(amount*float(promo['bonus_percent'] or 0)/100)
                                  + int(promo['bonus_fixed'] or 0))/100
                   if promo and amount >= int(promo['min_deposit'] or 0) else 0)


@app.get('/api/stars/deposit/<order_id>/status')
@login_required
def stars_deposit_status(order_id):
    if not re.fullmatch(r'[A-Za-z0-9]{8,40}', order_id):
        return error('Некорректная операция.')
    with connect() as db:
        order = db.execute('SELECT * FROM stars_deposit_orders WHERE id=? AND user_id=?',
                           (order_id, session['uid'])).fetchone()
        account = db.execute('SELECT stars_withdrawal_until FROM users WHERE id=?', (session['uid'],)).fetchone()
    if not order:
        return error('Операция пополнения не найдена.', 404)
    until = parse_datetime_utc(account['stars_withdrawal_until']) if account else None
    return jsonify(ok=True, status=order['status'], user=profile() if order['status'] == 'credited' else None,
                   withdrawal_block_until=(until.isoformat() if until and until > datetime.now(timezone.utc) else None),
                   withdraw_days=STARS_WITHDRAWAL_DAYS)


def process_stars_successful_payment(message, payment):
    sender = message.get('from') or {}
    uid = sender.get('id')
    payload = str(payment.get('invoice_payload') or '')
    if not isinstance(uid, int) or not payload.startswith('stars:'):
        return False
    order_id = payload.split(':', 1)[1].strip()
    if not re.fullmatch(r'[A-Za-z0-9]{8,40}', order_id):
        return False
    charge_id = str(payment.get('telegram_payment_charge_id') or '').strip()[:180]
    try:
        total_amount = int(payment.get('total_amount'))
    except (TypeError, ValueError):
        return False
    if payment.get('currency') != 'XTR' or not charge_id:
        return False
    credited = False
    bonus = 0
    lock_until = None
    balance_now = 0
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        order = db.execute('SELECT * FROM stars_deposit_orders WHERE id=?'
                           + (' FOR UPDATE' if DATABASE_URL else ''), (order_id,)).fetchone()
        if not order or int(order['user_id']) != uid or order['invoice_payload'] != payload:
            db.commit()
            return False
        if int(order['stars_amount']) != total_amount:
            db.commit()
            return False
        if order['status'] == 'credited':
            db.commit()
            return True
        used = db.execute("""SELECT id FROM stars_deposit_orders
                             WHERE telegram_payment_charge_id=? AND id<>?""",
                          (charge_id, order_id)).fetchone()
        if used:
            db.commit()
            return False
        bonus, lock_until = credit_verified_stars_deposit(db, order, charge_id)
        row = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
        balance_now = int(row['balance'] or 0) if row else 0
        db.commit()
        credited = True
    if credited:
        notify_deposit_async(uid, int(order['amount']), balance_now, bonus)
        until_text = lock_until.strftime('%d.%m.%Y') if lock_until else ''
        notify_user_async(
            uid,
            f'⭐ <b>Оплата Telegram Stars подтверждена.</b>\n\n'
            f'Вывод подарков ограничен на {STARS_WITHDRAWAL_DAYS} дней — до <b>{until_text}</b>.',
            miniapp_markup('Открыть', 'profile'), 'HTML')
    return credited



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


def saved_portal_partner_key():
    """Partner API token for the Relayer account; kept separate from catalog/TMA auth."""
    try:
        doc = read_document('portal_partner_auth') or {}
        value = str(doc.get('token') or '').strip()
        return value if len(value) <= 8000 and '\n' not in value and '\r' not in value else ''
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ''


def store_portal_partner_key(token):
    token = str(token or '').strip()
    if not token:
        return
    save_document('portal_partner_auth', {
        'token': token,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })



PORTAL_BACKGROUND_LABELS = {
    'black': 'Black',
    'onyx': 'Onyx',
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
    if label == 'Onyx':
        return 'onyx'
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
        if label not in ('Black', 'Onyx', 'Onyx Black'):
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
    for label in ('Black', 'Onyx', 'Onyx Black'):
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
            background_tone={'Black':'black','Onyx':'onyx','Onyx Black':'onyx-black'}.get(label,''),
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
        save_document('portal_logs', logs[-200:])
    except Exception:
        app.logger.exception('Could not persist Portal log')



PORTAL_PARTNER_BASE='https://portal-market.com'
PORTAL_WITHDRAW_RESERVE=Decimal('0.30')
portal_withdraw_lock=__import__('threading').Lock()

def portal_partner_token():
    # Prefer a dedicated deployment secret, then the admin-saved Partner token.
    # The legacy shared key remains a migration fallback only when it is not TMA auth.
    raw=str(os.environ.get('PORTAL_PARTNER_TOKEN') or saved_portal_partner_key() or saved_portal_key() or '').strip()
    if not raw:return ''
    if raw.casefold().startswith('tma ') or ('hash=' in raw and 'auth_date=' in raw):return ''
    for prefix in ('partners ','bearer '):
        if raw.casefold().startswith(prefix):raw=raw[len(prefix):].strip();break
    return raw if raw and len(raw)<=8000 and '\n' not in raw and '\r' not in raw else ''

def _portal_decimal(value,default='0'):
    try:
        out=Decimal(str(value if value not in (None,'') else default))
        return out if out.is_finite() and out>=0 else Decimal(default)
    except (InvalidOperation,TypeError,ValueError):return Decimal(default)

def _portal_decimal_text(value):
    text=format(_portal_decimal(value).quantize(Decimal('0.000000001'),rounding=ROUND_HALF_UP),'f').rstrip('0').rstrip('.')
    return text or '0'

def _portal_error_message(data,status=0):
    if isinstance(data,dict):
        for key in ('message','error','detail','details','reason'):
            if isinstance(data.get(key),str) and data[key].strip():return data[key].strip()[:350]
    if isinstance(data,str) and data.strip():return data.strip()[:350]
    return f'Portal HTTP {status}' if status else 'Portal Market не вернул описание ошибки.'

def _portal_partner_request(method,path,params=None,payload=None):
    token=portal_partner_token()
    if not token:raise RuntimeError('Partner token Portal Market не настроен.')
    headers={'Accept':'application/json','Authorization':'partners '+token,'User-Agent':'GemDrop/'+BUILD_ID}
    if payload is not None:headers['Content-Type']='application/json'
    last=None
    for attempt in range(3):
        try:
            r=requests.request(method,PORTAL_PARTNER_BASE+path,params=params,json=payload,headers=headers,timeout=(3.5,12))
            if r.status_code==429 and attempt<2:
                try:delay=min(3,max(.5,float(r.headers.get('Retry-After') or 1)))
                except (TypeError,ValueError):delay=1
                time.sleep(delay);continue
            if r.status_code>=500 and attempt<2:time.sleep(.6*(attempt+1));continue
            if r.status_code==204:return {}
            try:data=r.json()
            except ValueError:data=r.text or {}
            if r.status_code>=400:raise RuntimeError(_portal_error_message(data,r.status_code))
            return data if isinstance(data,dict) else {}
        except requests.RequestException as exc:
            last=exc
            if attempt<2:time.sleep(.6*(attempt+1));continue
    raise RuntimeError('Portal Market не ответил: '+str(last or 'network error')[:240])

def _portal_requested_collection(row):
    name=str(row.get('gift_name') or 'Подарок').strip();backdrop=str(row.get('fragment_backdrop') or '').strip()
    m=re.search(r'\s*\((Black|Onyx(?: Black)?)\)\s*$',name,re.I)
    if m:backdrop=backdrop or m.group(1);name=name[:m.start()].strip()
    name=re.sub(r'\s*#\s*\d+\s*$','',name).strip()
    if not name:
        slug=_relayer_slug_from_url(row.get('external_url'));name=re.sub(r'-\d+$','',slug).replace('_',' ').strip() or 'Подарок'
    return name,backdrop

def _portal_norm(value):return re.sub(r'[^a-z0-9]+','',str(value or '').casefold())

def _portal_matches(items,name):
    target=_portal_norm(name);exact=[];loose=[]
    for x in items or []:
        if not isinstance(x,dict):continue
        live=_portal_norm(x.get('name'))
        if not (target and live):continue
        if target==live:exact.append(x)
        elif target in live or live in target:loose.append(x)
    # An exact collection match always wins: "Heart" must never fall back to "Heart Locket" if a real Heart exists.
    return exact or loose

def _portal_owned_candidate(row):
    name,backdrop=_portal_requested_collection(row)
    params={'search':name,'limit':100,'exclude_bundled':'true','sort_by':'external_collection_number asc'}
    if backdrop:params['filter_by_backdrops']=backdrop
    data=_portal_partner_request('GET','/partners/nfts/owned',params=params)
    items=_portal_matches(data.get('nfts') or [],name)
    return (items[0] if items else None),int(data.get('total_count') or len(items))

def _portal_market_candidate(row):
    name,backdrop=_portal_requested_collection(row)
    params={'search':name,'limit':40,'exclude_bundled':'true','status':'listed','sort_by':'price asc'}
    if backdrop:params['filter_by_backdrops']=backdrop
    data=_portal_partner_request('GET','/partners/nfts/search',params=params)
    items=[x for x in _portal_matches(data.get('results') or [],name) if _portal_decimal(x.get('price'))>0]
    # Never overpay: the market price may exceed the gift's floor value by 25% (+0.5 TON) at most.
    try:floor=Decimal(int(row.get('floor_price') or 0))/Decimal(100)
    except (TypeError,ValueError,InvalidOperation):floor=Decimal(0)
    if floor>0:
        cap=floor*Decimal('1.25')+Decimal('0.5')
        items=[x for x in items if _portal_decimal(x.get('price'))<=cap]
    items.sort(key=lambda x:_portal_decimal(x.get('price')))
    return items[0] if items else None

def _portal_wallet_info():
    data=_portal_partner_request('GET','/partners/users/wallets/')
    return dict(balance=_portal_decimal(data.get('balance')),frozen=_portal_decimal(data.get('frozen_funds')),
                premarket=_portal_decimal(data.get('premarket_funds')))

def _portal_log_row(withdrawal_id):
    with connect() as db:row=db.execute('SELECT * FROM portal_withdrawal_logs WHERE withdrawal_id=?',(withdrawal_id,)).fetchone()
    return dict(row) if row else None

def _portal_auto_log(withdrawal_id,row,status,stage='',nft=None,source='',purchase_price=None,balance_before=None,withdrawal_ids=None,error_text=''):
    old=_portal_log_row(withdrawal_id) or {};nft=nft or {};inc=1 if stage in ('lookup','buy','withdraw','status') else 0
    nft_id=str(nft.get('id') or old.get('nft_id') or '');nft_name=str(nft.get('name') or old.get('nft_name') or '')
    src=str(source or old.get('source') or '');price=_portal_decimal_text(purchase_price if purchase_price is not None else old.get('purchase_price') or 0)
    bal=_portal_decimal_text(balance_before if balance_before is not None else old.get('balance_before') or 0)
    ids=json.dumps(withdrawal_ids,ensure_ascii=False) if withdrawal_ids is not None else str(old.get('withdrawal_ids') or '')
    with connect() as db:
        db.execute("""INSERT INTO portal_withdrawal_logs(withdrawal_id,user_id,requested_name,requested_number,nft_id,nft_name,
                      source,purchase_price,balance_before,withdrawal_fee,withdrawal_ids,status,stage,attempts,error,updated_at,completed_at)
                      VALUES(?,?,?,?,?,?,?,?,?,'0.30',?,?,?,?,?,CURRENT_TIMESTAMP,
                      CASE WHEN ?='completed' THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE NULL END)
                      ON CONFLICT(withdrawal_id) DO UPDATE SET nft_id=excluded.nft_id,nft_name=excluded.nft_name,
                      source=excluded.source,purchase_price=excluded.purchase_price,balance_before=excluded.balance_before,
                      withdrawal_ids=excluded.withdrawal_ids,status=excluded.status,stage=excluded.stage,
                      attempts=portal_withdrawal_logs.attempts+CASE WHEN excluded.stage IN ('lookup','buy','withdraw','status') THEN 1 ELSE 0 END,
                      error=excluded.error,updated_at=CURRENT_TIMESTAMP,
                      completed_at=CASE WHEN excluded.status='completed' THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE portal_withdrawal_logs.completed_at END""",
                   (withdrawal_id,int(row['user_id']),str(row.get('gift_name') or ''),str(row.get('fragment_number') or ''),
                    nft_id,nft_name,src,price,bal,ids,status,str(stage or ''),inc,str(error_text or '')[:500],status))
        db.commit()
    return _portal_log_row(withdrawal_id)

def _portal_manual(withdrawal_id,row,status,message,stage='error',**kwargs):
    previous = _portal_log_row(withdrawal_id) or {}
    first_notice = previous.get('status') != status or str(previous.get('error') or '') != str(message)
    _portal_auto_log(withdrawal_id,row,status,stage=stage,error_text=message,**kwargs)
    append_portal_log(f'Вывод #{withdrawal_id}: {message} Нужен ручной вывод.','error')
    if first_notice:
        notify_user_async(
            int(row['user_id']),
            '⚠️ <b>Автоматический вывод не завершён</b>\n\n'
            'Заявка сохранена и помечена для ручного вывода администратором. Повторно запрашивать подарок не нужно.',
            miniapp_markup('Открыть GemDrop', 'profile'), 'HTML')
    return dict(ok=False,status=status,error=message,manual_required=True,provider='portal')

def _portal_finish(withdrawal_id,row,nft,ids):
    changed=False
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT id FROM withdrawals WHERE id=? AND status='pending'",(withdrawal_id,)).fetchone():
            db.execute("UPDATE withdrawals SET status='approved',admin_id=0,processed_at=CURRENT_TIMESTAMP WHERE id=?",(withdrawal_id,))
            record_transaction(db,int(row['user_id']),'withdrawal_portal',0,'withdrawal',withdrawal_id,str(row.get('gift_name') or 'NFT'))
            log_event(db,int(row['user_id']),'withdrawal_auto_completed',withdrawal_id=withdrawal_id,
                      gift_name=row.get('gift_name') or '',provider='portal_market',portal_nft_id=str(nft.get('id') or ''))
            changed=True
        db.commit()
    _portal_auto_log(withdrawal_id,row,'completed',stage='done',nft=nft,withdrawal_ids=ids)
    append_portal_log(f'Вывод #{withdrawal_id}: Portal Market подтвердил отправку пользователю {row["user_id"]}.')
    if changed:
        notify_user_async(int(row['user_id']),
            f'✅ <b>Ваш подарок выведен через Portal Market</b>\n\n🎁 {escape(str(row.get("gift_name") or "Подарок"))}\n\n'
            'Если подарок не появился сразу, отправьте любое сообщение боту @GiftsToPortals.',
            miniapp_markup('Открыть GemDrop','profile'),'HTML')
    return dict(ok=True,status='completed',provider='portal',manual_required=False)

def _portal_check_status(withdrawal_id,row,log,nft):
    try:ids=json.loads(str(log.get('withdrawal_ids') or '[]'))
    except Exception:ids=[]
    ids=[int(x) for x in ids if str(x).isdigit()]
    if not ids:return _portal_manual(withdrawal_id,row,'portal_withdraw_failed','Portal не вернул ID операции вывода.','status',nft=nft)
    _portal_auto_log(withdrawal_id,row,'portal_withdrawing',stage='status',nft=nft,withdrawal_ids=ids)
    data=_portal_partner_request('GET','/partners/nfts/withdrawals/statuses',params={'ids':','.join(map(str,ids))})
    states=data.get('statuses') or []
    if states and all(str(x.get('status') or '')=='completed' for x in states if isinstance(x,dict)):
        return _portal_finish(withdrawal_id,row,nft,ids)
    bad=next((x for x in states if isinstance(x,dict) and str(x.get('status') or '') in ('errored','recovered','unknown')),None)
    if bad:
        st=str(bad.get('status') or 'unknown');detail=str(bad.get('details') or st)
        mapped={'errored':'portal_withdraw_failed','recovered':'portal_recovered','unknown':'portal_unknown'}.get(st,'portal_error')
        return _portal_manual(withdrawal_id,row,mapped,'Portal не завершил вывод: '+detail+'.','status',nft=nft,withdrawal_ids=ids)
    return dict(ok=True,status='portal_withdrawing',provider='portal',manual_required=False)

def _portal_fallback_withdraw(withdrawal_id,row=None):
    row=row or _relayer_withdrawal_target(withdrawal_id)
    if not row or str(row.get('status') or '')!='pending':return dict(ok=False,status='not_pending')
    if not portal_partner_token():return _portal_manual(withdrawal_id,row,'portal_not_configured','Partner token Portal Market не настроен.','config')
    with portal_withdraw_lock:
        old=_portal_log_row(withdrawal_id) or {}
        nft={'id':old.get('nft_id') or '','name':old.get('nft_name') or ''}
        if old.get('status')=='portal_withdrawing' and old.get('withdrawal_ids'):
            try:return _portal_check_status(withdrawal_id,row,old,nft)
            except Exception as exc:return _portal_manual(withdrawal_id,row,'portal_error','Ошибка проверки Portal: '+str(exc)[:300],'status',nft=nft)
        try:user=_portal_partner_request('GET','/partners/users/'+str(int(row['user_id'])))
        except Exception as exc:return _portal_manual(withdrawal_id,row,'portal_error','Не удалось проверить получателя Portal: '+str(exc)[:300],'recipient',nft=nft)
        if not bool(user.get('exists')):
            first=old.get('status')!='portal_waiting_recipient'
            _portal_auto_log(withdrawal_id,row,'portal_waiting_recipient',stage='recipient',nft=nft,
                             error_text='Получателю нужно написать @GiftsToPortals.')
            if first:
                append_portal_log(f'Вывод #{withdrawal_id}: ждём активацию Portal пользователем {row["user_id"]}.')
                notify_user_async(int(row['user_id']),
                    '🎁 <b>Для вывода через Portal Market нужен один шаг</b>\n\n'
                    'Отправьте любое сообщение боту @GiftsToPortals. После этого GemDrop автоматически продолжит вывод.',
                    miniapp_markup('Открыть GemDrop','profile'),'HTML')
            return dict(ok=True,status='portal_waiting_recipient',provider='portal',manual_required=False)
        source=str(old.get('source') or '');price=_portal_decimal(old.get('purchase_price') or 0)
        already_owned=old.get('status')=='portal_bought' and bool(nft.get('id'))
        if old.get('status')=='portal_buying' and nft.get('id'):
            # The process died while buying: check whether the NFT is already ours before trying to buy again.
            try:
                chk=_portal_partner_request('GET','/partners/nfts/owned',params={'ids':str(nft['id']),'limit':5})
                hit=next((x for x in (chk.get('nfts') or []) if str(x.get('id'))==str(nft['id'])),None)
                if hit:nft=hit;already_owned=True;source='market'
            except Exception:pass
        try:
            wallet=_portal_wallet_info()
            if not nft.get('id'):
                _portal_auto_log(withdrawal_id,row,'portal_lookup',stage='lookup')
                candidate,_=_portal_owned_candidate(row);source='owned' if candidate else 'market'
                if not candidate:
                    candidate=_portal_market_candidate(row)
                    if candidate and old.get('source')!='market':
                        notify_user_async(
                            int(row['user_id']),
                            '🔎 <b>Подарок не найден на Relayer</b>\n\n'
                            'GemDrop нашёл подходящий подарок на Portal Market и автоматически покупает его для вашего вывода. '
                            'Для отправки через Portal может понадобиться написать любое сообщение боту @GiftsToPortals.',
                            miniapp_markup('Открыть GemDrop','profile'),'HTML')
                if not candidate:return _portal_manual(withdrawal_id,row,'portal_not_found','В Portal Market нет подходящего подарка этой коллекции.','lookup')
                nft=candidate;price=Decimal('0') if source=='owned' else _portal_decimal(nft.get('price'))
                already_owned=(source=='owned')
                append_portal_log(f'Вывод #{withdrawal_id}: найден {nft.get("name") or row.get("gift_name")} · {price} TON · {source}.')
            need=(Decimal('0') if already_owned else price)+PORTAL_WITHDRAW_RESERVE
            if wallet['balance']<need:
                return _portal_manual(withdrawal_id,row,'portal_insufficient_balance',
                    f'Недостаточно TON на Portal: баланс {_portal_decimal_text(wallet["balance"])}, нужно {_portal_decimal_text(need)} '
                    f'({_portal_decimal_text(price)} за подарок + 0.30 TON резерв на вывод).',
                    'balance',nft=nft,source=source,purchase_price=price,balance_before=wallet['balance'])
            if source=='owned' and str(nft.get('status') or '').casefold()=='listed':
                _portal_partner_request('POST','/partners/nfts/'+str(nft['id'])+'/unlist')
            if source=='market' and not already_owned:
                _portal_auto_log(withdrawal_id,row,'portal_buying',stage='buy',nft=nft,source=source,purchase_price=price,balance_before=wallet['balance'])
                buy=_portal_partner_request('POST','/partners/nfts',payload={'nft_details':[{'id':str(nft['id']),'price':_portal_decimal_text(price)}]})
                owned=_portal_partner_request('GET','/partners/nfts/owned',params={'ids':str(nft['id']),'limit':5})
                confirmed=next((x for x in (owned.get('nfts') or []) if str(x.get('id'))==str(nft['id'])),None)
                if not confirmed or int(buy.get('total_purchased') or 0)<1:
                    reason='';results=buy.get('purchase_results') or []
                    if results and isinstance(results[0],dict):reason=str(results[0].get('error_message') or results[0].get('reason') or '')
                    return _portal_manual(withdrawal_id,row,'portal_buy_failed','Portal не подтвердил покупку'+(': '+reason[:200] if reason else '')+'.',
                                          'buy',nft=nft,source=source,purchase_price=price,balance_before=wallet['balance'])
                nft=confirmed;append_portal_log(f'Вывод #{withdrawal_id}: подарок куплен за {_portal_decimal_text(price)} TON.')
            _portal_auto_log(withdrawal_id,row,'portal_bought',stage='bought',nft=nft,source=source,purchase_price=price,balance_before=wallet['balance'])
        except Exception as exc:
            return _portal_manual(withdrawal_id,row,'portal_buy_failed' if source=='market' else 'portal_error',
                                  'Ошибка Portal Market: '+str(exc)[:300],'buy',nft=nft,source=source,purchase_price=price)
        try:
            result=_portal_partner_request('POST','/partners/nfts/withdraw',
                payload={'gift_ids':[str(nft['id'])],'recipient_id':int(row['user_id']),'unsafe_transfer':False})
            ids=result.get('withdrawals_ids') or []
            if not ids:return _portal_manual(withdrawal_id,row,'portal_withdraw_failed','Portal не вернул ID операции вывода.','withdraw',nft=nft)
            _portal_auto_log(withdrawal_id,row,'portal_withdrawing',stage='status',nft=nft,source=source,purchase_price=price,
                             balance_before=wallet['balance'],withdrawal_ids=ids)
            append_portal_log(f'Вывод #{withdrawal_id}: создан Portal withdrawal ID {",".join(map(str,ids))}.')
            return _portal_check_status(withdrawal_id,row,_portal_log_row(withdrawal_id),nft)
        except Exception as exc:
            return _portal_manual(withdrawal_id,row,'portal_withdraw_failed','Portal не смог запустить вывод: '+str(exc)[:300],
                                  'withdraw',nft=nft,source=source,purchase_price=price,balance_before=wallet['balance'])

def _portal_resume_pending(limit=8):
    if not portal_partner_token():return
    with connect() as db:
        rows=db.execute("""SELECT p.withdrawal_id FROM portal_withdrawal_logs p JOIN withdrawals w ON w.id=p.withdrawal_id
                           WHERE w.status='pending' AND p.status IN ('portal_not_configured','portal_waiting_recipient','portal_bought','portal_buying','portal_withdrawing')
                           ORDER BY p.updated_at ASC LIMIT ?""",(max(1,min(20,int(limit))),)).fetchall()
    for x in rows:
        try:_portal_fallback_withdraw(int(x['withdrawal_id']))
        except Exception:app.logger.exception('Portal resume failed for withdrawal %s',x['withdrawal_id'])

def _portal_runtime_data():
    out=dict(configured=bool(portal_partner_token()),reserve_ton=0.30,balance_ton=None,spendable_ton=None,
             frozen_ton=None,premarket_ton=None,owned_count=None,error='')
    if out['configured']:
        try:
            wallet=_portal_wallet_info();owned=_portal_partner_request('GET','/partners/nfts/owned',params={'limit':1})
            out.update(balance_ton=float(wallet['balance']),spendable_ton=float(max(Decimal('0'),wallet['balance']-PORTAL_WITHDRAW_RESERVE)),
                       frozen_ton=float(wallet['frozen']),premarket_ton=float(wallet['premarket']),
                       owned_count=int(owned.get('total_count') or len(owned.get('nfts') or [])))
        except Exception as exc:out['error']=str(exc)[:350]
    with connect() as db:
        rows=db.execute("""SELECT p.*,u.name AS user_name,u.username FROM portal_withdrawal_logs p
                           LEFT JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 30""").fetchall()
    out['withdrawals']=[dict(withdrawal_id=x['withdrawal_id'],user_id=x['user_id'],user_name=x['user_name'] or '',
        username=x['username'] or '',requested_name=x['requested_name'],requested_number=x['requested_number'],
        nft_id=x['nft_id'],nft_name=x['nft_name'],source=x['source'],purchase_price=x['purchase_price'],
        balance_before=x['balance_before'],withdrawal_ids=x['withdrawal_ids'],status=x['status'],stage=x['stage'],
        attempts=int(x['attempts'] or 0),error=x['error'] or '',created_at=x['created_at'],updated_at=x['updated_at'],
        completed_at=x['completed_at']) for x in rows]
    return out

@app.get('/api/admin/portal/runtime')
@admin_required
def admin_portal_runtime():return jsonify(**_portal_runtime_data())

@app.post('/api/admin/portal/partner')
@admin_required
def admin_portal_partner():
    token=str((request.get_json(silent=True) or {}).get('token') or '').strip()
    if token:
        if len(token)>8000 or '\n' in token or '\r' in token:return error('Некорректный Partner token Portal.')
        store_portal_partner_key(token);append_portal_log('Partner token Portal Market аккаунта Relayer обновлён.')
    data=_portal_runtime_data()
    if token and not data.get('error'):
        # Any withdrawals that were waiting only for Partner API configuration
        # must continue automatically as soon as the connection becomes valid.
        Thread(target=_portal_resume_pending,args=(20,),daemon=True).start()
        append_portal_log('Partner API подключён. Возобновляем ожидающие автовыводы Portal.')
    return jsonify(ok=not bool(data.get('error')),**data)


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
    logs=read_document('portal_logs') or []
    logs=logs[-199:] if isinstance(logs,list) else []
    if portal_partner_token():
        try:
            wallet=_portal_wallet_info();balance=wallet['balance'];spend=max(Decimal('0'),balance-PORTAL_WITHDRAW_RESERVE)
            logs=logs+[{'ts':datetime.now(timezone.utc).isoformat(),'level':'info',
                        'message':f'Portal Partner · баланс {_portal_decimal_text(balance)} TON · доступно с резервом 0.30: {_portal_decimal_text(spend)} TON'}]
        except Exception as exc:
            logs=logs+[{'ts':datetime.now(timezone.utc).isoformat(),'level':'error',
                        'message':'Portal Partner · не удалось получить баланс: '+str(exc)[:300]}]
    return jsonify(logs=logs[-200:])


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
    pre_checkout = update.get('pre_checkout_query') or {}
    if pre_checkout:
        query_id = str(pre_checkout.get('id') or '')
        uid = (pre_checkout.get('from') or {}).get('id')
        payload = str(pre_checkout.get('invoice_payload') or '')
        ok = False
        reason = 'Счёт больше недействителен. Создайте новое пополнение.'
        if query_id and isinstance(uid, int) and payload.startswith('stars:') and pre_checkout.get('currency') == 'XTR':
            order_id = payload.split(':', 1)[1].strip()
            try:
                total_amount = int(pre_checkout.get('total_amount'))
            except (TypeError, ValueError):
                total_amount = -1
            if re.fullmatch(r'[A-Za-z0-9]{8,40}', order_id):
                with connect() as db:
                    order = db.execute('SELECT * FROM stars_deposit_orders WHERE id=? AND user_id=?',
                                       (order_id, uid)).fetchone()
                if order and order['status'] == 'pending' and order['invoice_payload'] == payload and int(order['stars_amount']) == total_amount:
                    ok = True
        try:
            answer = {'pre_checkout_query_id': query_id, 'ok': ok}
            if not ok:
                answer['error_message'] = reason
            telegram_api('answerPreCheckoutQuery', answer)
        except RuntimeError:
            app.logger.exception('Failed to answer Stars pre-checkout query')
        return jsonify(ok=True)
    successful_payment = message.get('successful_payment') or {}
    if successful_payment:
        try:
            if not process_stars_successful_payment(message, successful_payment):
                app.logger.warning('Rejected or unmatched Stars successful_payment for user %s', sender.get('id'))
        except Exception:
            app.logger.exception('Stars successful_payment processing failed')
            return error('Не удалось зачислить оплату Stars.', 500)
        return jsonify(ok=True)
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
    if (chat.get('type') == 'private' and command and
            command[0].split('@')[0].lower() == '/paysupport' and isinstance(sender.get('id'), int)):
        return jsonify(method='sendMessage', chat_id=chat['id'],
                       text='По вопросам оплаты Telegram Stars обратитесь в поддержку GemDrop через приложение.')
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



def daily_top_settlement_loop():
    # Finalize expired TOP periods even when nobody currently has the game page open.
    time.sleep(3)
    while True:
        try:
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                settle_previous_daily_top_rewards(db)
                db.commit()
        except Exception:
            app.logger.exception('Daily top settlement loop failed')
        time.sleep(10)




def creator_limit_refill_text(level):
    cfg = CREATOR_LEVELS[creator_level_key(level)]
    parts = [
        '♻️ <b>Дневной лимит автора восстановлен</b>',
        '',
        f'Уровень: <b>{escape(cfg["name"])}</b>',
        f'Шкала: <b>0 / {cfg["daily_budget_cents"]/100:.2f} TON</b>',
    ]
    if int(cfg.get('wager_daily_limit') or 0):
        parts.append(
            f'Отыгрышные подарки: <b>0 / {int(cfg["wager_daily_limit"])}</b> · '
            f'{cfg["wager_gift_min_cents"]/100:.0f}–{cfg["wager_gift_max_cents"]/100:.0f} TON · '
            f'X от {int(cfg["wager_min_x"])}'
        )
    parts.extend(['', 'Новый дневной лимит уже доступен в панели автора.'])
    return '\n'.join(parts)


def creator_daily_limit_refill_loop():
    # Creator quotas follow the same UTC+3 midnight used by TOP-day windows.
    # State is persistent so restarts around midnight do not create duplicate messages.
    time.sleep(7)
    while True:
        try:
            local_now = datetime.now(timezone.utc).astimezone(DAILY_TOP_TZ)
            today = local_now.strftime('%Y-%m-%d')
            state = read_document('creator_limit_refill_state') or {}
            if not isinstance(state, dict):
                state = {}
            stored_day = str(state.get('day') or '')
            if not stored_day:
                # First deployment should not send a surprise midday broadcast.
                save_document('creator_limit_refill_state', {'day': today, 'sent': []})
            elif stored_day != today:
                state = {'day': today, 'sent': []}
                save_document('creator_limit_refill_state', state)
                with connect() as db:
                    rows = db.execute(
                        "SELECT name,payload FROM app_documents WHERE name LIKE 'creator:%' ORDER BY name"
                    ).fetchall()
                active = []
                for row in rows:
                    try:
                        uid = int(str(row['name']).split(':', 1)[1])
                        payload = json.loads(row['payload'] or '{}')
                    except (ValueError, TypeError, json.JSONDecodeError, IndexError):
                        continue
                    if isinstance(payload, dict) and payload.get('active'):
                        active.append((uid, creator_level_key(payload.get('creator_level'))))
                sent = set()
                for uid, level in active:
                    notify_user_async(
                        uid,
                        creator_limit_refill_text(level),
                        miniapp_markup('Открыть панель автора', 'creator'),
                        'HTML')
                    sent.add(uid)
                    save_document('creator_limit_refill_state', {
                        'day': today, 'sent': sorted(sent),
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                    })
        except Exception:
            app.logger.exception('Creator daily limit refill loop failed')
        time.sleep(20)


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
                                           'allowed_updates': ['message', 'callback_query', 'pre_checkout_query'],
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


# Legacy data repairs must never make the web process unavailable during a rolling
# deploy. Another Render instance can still own an advisory lock for a few seconds.
# The repair functions use pg_try_advisory_xact_lock(), and this wrapper also keeps
# any non-schema legacy repair failure from aborting Gunicorn import.
def run_startup_repair(fn):
    try:
        return fn()
    except Exception:
        app.logger.exception('Non-fatal startup repair skipped: %s', fn.__name__)
        return False


def migrate_rtp_cut_v1():
    """One-time RTP cut (2026-10-07): lower already-saved admin values to the new targets.

    Only ever lowers a value; anything the admin already set below the target is kept.
    Runs once (flag rtp_cut_v1), afterwards the admin RTP page stays the single source of truth.
    """
    doc = dict(read_document('game_settings') or {})
    if doc.get('rtp_cut_v1'):
        return False
    targets = {'rtp': GAME_RTP_DEFAULT, 'promo_rtp': PROMO_RTP_DEFAULT,
               'crash_rtp': CRASH_RTP_DEFAULT, 'hilo_rtp': HILO_RTP_DEFAULT}
    for key, target in targets.items():
        try:
            cur = float(doc.get(key, target))
        except (TypeError, ValueError):
            cur = target
        doc[key] = min(cur, target)
    try:
        cur_bp = int(doc.get('upgrade_rtp_bp', 8200))
    except (TypeError, ValueError):
        cur_bp = 8200
    doc['upgrade_rtp_bp'] = min(cur_bp, 8200)
    if doc['promo_rtp'] >= doc['rtp']:
        doc['promo_rtp'] = max(MIN_PROMO_RTP, round(doc['rtp'] - 0.02, 4))
    doc['rtp_cut_v1'] = True
    save_document('game_settings', doc)
    return True


run_startup_repair(migrate_rtp_cut_v1)

if os.environ.get('RUN_LEGACY_REPAIR', '1') == '1':
    run_startup_repair(repair_legacy_upgrade_wagers)
    run_startup_repair(repair_zero_price_top_gifts)

# Keep perpetual background-leader lock ids in their own range, separate from
# transaction/migration locks (660105/660106 above).
start_background(portal_auto_loop, 660101)
start_background(level_plan_autoapply_loop, 660107)
start_background(daily_top_settlement_loop, 661201)
start_background(broadcast_worker_loop, 661203)
start_background(relayer_auto_loop, 661204)
if BOT_TOKEN:
    start_background(creator_daily_limit_refill_loop, 661202)
    start_background(activity_notification_loop, 660102)
if BOT_TOKEN and WEBAPP_URL.startswith('https://'):
    start_background(configure_bot, 660103)
start_background(log_pruner_loop, 660104)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
