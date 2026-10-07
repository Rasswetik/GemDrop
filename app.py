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
TWITCH_CLIENT_ID = (os.environ.get('TWITCH_CLIENT_ID') or '').strip()
TWITCH_CLIENT_SECRET = (os.environ.get('TWITCH_CLIENT_SECRET') or '').strip()
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
UPGRADE_COMPENSATION_MIN_CENTS = 500  # a lost upgrade is compensated from 5 TON
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
