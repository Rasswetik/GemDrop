"""Confirmed payment accounting and the standalone operator console.

The ledger reads verified providers and received NFTs, never admin grants.
Crypto Pay amounts stay in integer TON cents; testnet cannot credit real funds.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from flask import jsonify, request, session, send_file


PAYMENT_LEDGER = """
SELECT 'payment:' || CAST(d.id AS TEXT) AS id,d.user_id,d.amount,d.created_at,
       CASE WHEN d.request_key LIKE 'ton:%' THEN 'ton'
            WHEN d.request_key LIKE 'stars:%' THEN 'stars' ELSE 'cryptobot' END AS method,
       d.request_key AS reference,'' AS gift_name,'' AS external_url,'' AS raw_json,'' AS model
FROM deposits d WHERE d.admin_id=0 AND d.amount>0 AND
 (d.request_key LIKE 'ton:%' OR d.request_key LIKE 'stars:%' OR d.request_key LIKE 'crypto:%')
UNION ALL
SELECT 'nft:' || CAST(e.id AS TEXT),e.sender_user_id,e.floor_price,
       COALESCE(NULLIF(e.credited_at,''),e.created_at),'nft',e.external_key,e.gift_name,e.external_url,e.raw_json,e.fragment_model
FROM relayer_gift_events e WHERE e.status IN ('delivered','credited') AND e.floor_price>0
"""


def deposit_total(db, user_id, since=None):
    sql = f'SELECT COALESCE(SUM(amount),0) AS total FROM ({PAYMENT_LEDGER}) ledger WHERE user_id=?'
    params = [int(user_id)]
    if since:
        sql += " AND REPLACE(SUBSTR(created_at,1,19),'T',' ')>=?"
        params.append(since)
    row = db.execute(sql, tuple(params)).fetchone()
    return max(0, int(row['total'] or 0))


def withdrawal_state(db, user_id, settings):
    row = db.execute('''SELECT withdrawal_enabled,withdrawal_block_reason,stars_withdrawal_until,
                              withdrawal_min_deposit_override,withdrawal_wager_required,
                              withdrawal_wager_progress FROM users WHERE id=?''', (user_id,)).fetchone()
    if not row:
        return None
    required = int(settings['min_ton_connect_deposit_cents']) if row['withdrawal_min_deposit_override'] is None else max(0, int(row['withdrawal_min_deposit_override']))
    deposited = deposit_total(db, user_id)
    wager_required = max(0, int(row['withdrawal_wager_required'] or 0))
    wager_progress = max(0, int(row['withdrawal_wager_progress'] or 0))
    remaining = max(0, wager_required - wager_progress)
    reasons = []
    if not row['withdrawal_enabled']:
        reasons.append(dict(code='admin', text=row['withdrawal_block_reason'] or 'Вывод отключён администратором.'))
    until = None
    if row['stars_withdrawal_until']:
        try:
            until = datetime.fromisoformat(str(row['stars_withdrawal_until']).replace('Z', '+00:00'))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    if until and until > datetime.now(timezone.utc):
        reasons.append(dict(code='stars', text=f'Ограничение после Stars до {until:%d.%m.%Y}.'))
    else:
        until = None
    if deposited < required:
        reasons.append(dict(code='deposit', text=f'Не хватает подтверждённых пополнений: {(required-deposited)/100:.2f} TON. Засчитываются TON, CryptoBot, Stars и оценённые NFT.'))
    if remaining:
        reasons.append(dict(code='wager', text=f'Осталось отыграть {remaining/100:.2f} TON (депозиты ×2).'))
    return dict(allowed=not reasons, reasons=reasons, deposited=deposited/100,
                minimum=required/100, deposit_remaining=max(0, required-deposited)/100,
                wager_required=wager_required/100, wager_progress=wager_progress/100,
                wager_remaining=remaining/100, stars_until=until.isoformat() if until else None)


def norm(value):
    return ''.join(c for c in str(value or '').casefold() if c.isalnum())


def trait_name(value):
    if isinstance(value, dict):
        value = value.get('name') or value.get('value') or ''
    return re.sub(r'\s*\((?:\d+(?:[.,]\d+)?\s*%|\d+\s*‰)\)\s*$', '', str(value or '')).strip()


def requested_traits(row, catalog=()):
    gift_id = str(row.get('gift_id') or '')
    base_id, _, encoded_model = gift_id.partition('@m:')
    model = trait_name(row.get('fragment_model') or encoded_model)
    name = str(row.get('gift_name') or '').strip()
    base = next((g for g in catalog if str(g.get('id')) == base_id), None)
    if base:
        name = str(base.get('base_name') or base.get('name') or name)
    elif model:
        for suffix in (' · '+model, ' — '+model, ' ('+model+')'):
            if name.casefold().endswith(suffix.casefold()):
                name = name[:-len(suffix)].strip()
                break
    backdrop = trait_name(row.get('fragment_backdrop'))
    match = re.search(r'\s*\((Black|Onyx(?: Black)?)\)\s*$', name, re.I)
    if match:
        backdrop = backdrop or match.group(1)
        name = name[:match.start()].strip()
    name = re.sub(r'\s*#\s*\d+\s*$', '', name).strip()
    return name, model, backdrop


def item_trait(item, kind):
    direct = trait_name(item.get(kind))
    if direct:
        return direct
    attrs = item.get('attributes') or []
    if isinstance(attrs, dict):
        return trait_name(attrs.get(kind))
    for attr in attrs:
        if isinstance(attr, dict) and str(attr.get('type') or attr.get('trait_type') or '').casefold() == kind:
            return trait_name(attr.get('value') or attr.get('name'))
    return ''


def item_collection(item):
    collection = item.get('collection') or item.get('collection_name')
    if isinstance(collection, dict):
        collection = collection.get('name') or collection.get('title')
    return re.sub(r'\s*#\s*\d+\s*$', '', str(collection or item.get('name') or item.get('title') or '')).strip()


def exact_portal_items(items, row, catalog=()):
    collection, model, backdrop = requested_traits(row, catalog)
    return [x for x in items if isinstance(x, dict) and norm(item_collection(x)) == norm(collection)
            and (not model or norm(item_trait(x, 'model')) == norm(model))
            and (not backdrop or norm(item_trait(x, 'backdrop')) == norm(backdrop))]


def initialize_schema(db):
    # Run under app.py's existing cross-worker schema migration lock.
    db.executescript('''
        CREATE TABLE IF NOT EXISTS crypto_deposit_orders (
            id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,amount BIGINT NOT NULL,
            invoice_id TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL,
            token_fingerprint TEXT NOT NULL,testnet BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'creating',pay_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,expires_at TEXT NOT NULL,
            credited_at TEXT,paid_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS crypto_invoice_unique ON crypto_deposit_orders(token_fingerprint,invoice_id) WHERE invoice_id<>'';
        CREATE INDEX IF NOT EXISTS crypto_user_status ON crypto_deposit_orders(user_id,status,created_at);
        CREATE INDEX IF NOT EXISTS deposits_verified_user ON deposits(user_id,admin_id,created_at);
        CREATE INDEX IF NOT EXISTS deposits_verified_date ON deposits(admin_id,created_at);
        CREATE INDEX IF NOT EXISTS nft_deposit_status_date ON relayer_gift_events(status,credited_at);
        CREATE INDEX IF NOT EXISTS withdrawal_status_date ON withdrawals(status,created_at);
        CREATE TABLE IF NOT EXISTS withdrawal_delivery_leases (
            withdrawal_id BIGINT PRIMARY KEY,owner TEXT NOT NULL,expires_at BIGINT NOT NULL
        );
    ''')


def serialized_withdrawal(fn, ns):
    """Only one process can transfer/buy for a pending withdrawal at a time."""
    import time
    from functools import wraps
    @wraps(fn)
    async def guarded(withdrawal_id, *args, **kwargs):
        owner = secrets.token_hex(16); now = int(time.time())
        with ns['connect']() as db:
            cur = db.execute('''INSERT INTO withdrawal_delivery_leases(withdrawal_id,owner,expires_at)
                                VALUES(?,?,?) ON CONFLICT(withdrawal_id) DO UPDATE
                                SET owner=excluded.owner,expires_at=excluded.expires_at
                                WHERE withdrawal_delivery_leases.expires_at<?''',
                             (withdrawal_id,owner,now+900,now))
            acquired = bool(cur.rowcount)
        if not acquired:
            return dict(ok=True,status='in_progress',manual_required=False)
        try:
            return await fn(withdrawal_id,*args,**kwargs)
        finally:
            with ns['connect']() as db:
                db.execute('DELETE FROM withdrawal_delivery_leases WHERE withdrawal_id=? AND owner=?',(withdrawal_id,owner))
    return guarded


def register(app, ns):
    """Called after app.py's providers are defined; share its auth and DB adapter."""
    connect = ns['connect']
    admin = ns['admin_required']
    login = ns['login_required']
    error = ns['error']
    parse_amount = ns['parse_amount']
    fold = 'LOWER' if ns['DATABASE_URL'] else 'CASEFOLD'

    def config():
        doc = dict(ns['read_document']('crypto_pay_settings') or {})
        env = str(os.environ.get('CRYPTO_PAY_API_TOKEN') or '').strip()
        doc['token'] = env or str(doc.get('token') or '').strip()
        doc['testnet'] = bool(doc.get('testnet', False))
        doc['enabled'] = bool(doc.get('enabled')) and bool(doc['token'])
        doc['env_token'] = bool(env)
        return doc

    def fingerprint(cfg):
        return hashlib.sha256((('test:' if cfg['testnet'] else 'main:') + cfg['token']).encode()).hexdigest()

    def crypto_request(method, payload=None, cfg=None):
        cfg = cfg or config()
        if not cfg.get('token'):
            raise RuntimeError('Crypto Pay API token не настроен.')
        host = 'https://testnet-pay.crypt.bot' if cfg['testnet'] else 'https://pay.crypt.bot'
        try:
            response = requests.post(host+'/api/'+method, json=payload or {},
                                     headers={'Crypto-Pay-API-Token': cfg['token']}, timeout=(3.5, 12))
            data = response.json()
            if response.status_code != 200 or not isinstance(data, dict) or not data.get('ok'):
                raise RuntimeError('Crypto Pay отклонил запрос. Проверьте токен и параметры приложения.')
            return data['result']
        except (requests.RequestException, ValueError, KeyError) as exc:
            # Do not put authenticated request headers or remote bodies into logs/UI.
            raise RuntimeError('Crypto Pay не ответил. Повторите проверку.') from exc

    def public_crypto(cfg=None):
        cfg = cfg or config()
        out = dict(configured=bool(cfg['token']), enabled=cfg['enabled'], testnet=cfg['testnet'],
                   app_name=cfg.get('app_name', ''), app_id=cfg.get('app_id'), env_token=cfg['env_token'])
        if cfg['token']:
            secret = hmac.new(cfg['token'].encode(), b'gemdrop-crypto-webhook-v1', hashlib.sha256).hexdigest()
            base = ns['WEBAPP_URL'] or request.url_root.rstrip('/')
            out['webhook_url'] = base+'/api/cryptobot/webhook/'+secret
        return out

    @app.get('/admin')
    @admin
    def operator_console():
        response = send_file(Path(ns['BASE'])/'templates'/'admin.html', max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    @app.get('/admin/tools')
    @admin
    def operator_tools():
        from flask import Response
        html = (Path(ns['BASE'])/'templates'/'index.html').read_text(encoding='utf-8')
        addition = '<link rel="stylesheet" href="/static/css/admin-tools.css"><script src="/static/js/admin-tools.js" defer></script>'
        html = html.replace('</head>', addition+'</head>', 1)
        html = html.replace('</body>','<link rel="stylesheet" href="/static/css/admin-tools.css"></body>',1)
        response = Response(html, mimetype='text/html')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    @app.get('/api/admin/dashboard')
    @admin
    def dashboard():
        days = max(1, min(365, request.args.get('days', 30, type=int) or 30))
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days-1)
        stamp = start.isoformat()+' 00:00:00'
        with connect() as db:
            ledger = f'({PAYMENT_LEDGER}) p'
            paid = db.execute(f'''SELECT SUBSTR(REPLACE(created_at,'T',' '),1,10) AS day,
                  SUM(amount) AS total,COUNT(*) AS count,
                  SUM(CASE WHEN method='nft' THEN amount ELSE 0 END) AS nft
                  FROM {ledger} WHERE REPLACE(SUBSTR(created_at,1,19),'T',' ')>=?
                  GROUP BY SUBSTR(REPLACE(created_at,'T',' '),1,10)''', (stamp,)).fetchall()
            drawn = db.execute('''SELECT SUBSTR(REPLACE(processed_at,'T',' '),1,10) AS day,
                  SUM(floor_price) AS total,COUNT(*) AS count FROM withdrawals
                  WHERE status='approved' AND REPLACE(SUBSTR(processed_at,1,19),'T',' ')>=?
                  GROUP BY SUBSTR(REPLACE(processed_at,'T',' '),1,10)''', (stamp,)).fetchall()
            split = db.execute(f'''SELECT method,SUM(amount) AS total,COUNT(*) AS count
                    FROM {ledger} WHERE REPLACE(SUBSTR(created_at,1,19),'T',' ')>=? GROUP BY method''', (stamp,)).fetchall()
            users = db.execute('SELECT COUNT(*) AS count,COALESCE(SUM(balance),0) AS balance FROM users').fetchone()
            pending = db.execute("SELECT COUNT(*) AS count,COALESCE(SUM(floor_price),0) AS total FROM withdrawals WHERE status='pending'").fetchone()
            grants = db.execute('''SELECT COALESCE(SUM(amount),0) AS total,COUNT(*) AS count FROM deposits
                    WHERE admin_id<>0 AND REPLACE(SUBSTR(created_at,1,19),'T',' ')>=?''', (stamp,)).fetchone()
        pmap = {r['day']: dict(r) for r in paid}; wmap = {r['day']: dict(r) for r in drawn}
        series = []
        for offset in range(days):
            day = (start+timedelta(days=offset)).isoformat()
            p = pmap.get(day, {}); w = wmap.get(day, {})
            series.append(dict(day=day, deposits=int(p.get('total') or 0)/100, nft=int(p.get('nft') or 0)/100,
                               deposit_count=int(p.get('count') or 0), withdrawals=int(w.get('total') or 0)/100,
                               withdrawal_count=int(w.get('count') or 0)))
        return jsonify(days=days, timezone='UTC', series=series,
                       methods=[dict(method=r['method'], total=int(r['total'] or 0)/100,count=int(r['count'])) for r in split],
                       totals=dict(deposits=sum(r['deposits'] for r in series),withdrawals=sum(r['withdrawals'] for r in series),
                                   deposit_count=sum(r['deposit_count'] for r in series),withdrawal_count=sum(r['withdrawal_count'] for r in series),
                                   users=int(users['count']),balance=int(users['balance'])/100,
                                   pending_count=int(pending['count']),pending_value=int(pending['total'])/100,
                                   admin_grants=int(grants['total'])/100))

    def page_args():
        return max(0, request.args.get('offset', 0, type=int) or 0), max(1, min(100, request.args.get('limit', 50, type=int) or 50))

    @app.get('/api/admin/players')
    @admin
    def players():
        offset, limit = page_args()
        term = request.args.get('q','').strip()[:100].casefold()
        params = []; where = '1=1'
        if term:
            where = "LOWER(COALESCE(u.name,'') || ' ' || COALESCE(u.username,'') || ' ' || CAST(u.id AS TEXT)) LIKE ? ESCAPE '\\'"
            params.append('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        where=where.replace('LOWER(',fold+'(')
        with connect() as db:
            count = db.execute('SELECT COUNT(*) AS count FROM users u WHERE '+where,tuple(params)).fetchone()['count']
            rows = db.execute(f'''WITH selected_players AS (
                   SELECT u.* FROM users u WHERE {where} ORDER BY u.id DESC LIMIT ? OFFSET ?
                   ) SELECT u.id,u.name,u.username,u.balance,u.bonus_balance,u.banned,
                   u.withdrawal_enabled,u.withdrawal_block_reason,u.withdrawal_wager_required,
                   u.withdrawal_wager_progress,u.stars_withdrawal_until,u.withdrawal_min_deposit_override,
                   COALESCE(p.total,0) AS deposits
                   FROM selected_players u LEFT JOIN (SELECT user_id,SUM(amount) AS total FROM ({PAYMENT_LEDGER}) ledger
                   WHERE user_id IN (SELECT id FROM selected_players) GROUP BY user_id) p ON p.user_id=u.id
                   ORDER BY u.id DESC''',tuple(params+[limit,offset])).fetchall()
        minimum = ns['withdrawal_settings']()['min_ton_connect_deposit_cents']
        items = []
        for row in rows:
            r = dict(row)
            for key in ('balance','bonus_balance','deposits'):
                r[key] = int(r[key] or 0)/100
            r['wager_remaining'] = max(0,int(row['withdrawal_wager_required'] or 0)-int(row['withdrawal_wager_progress'] or 0))/100
            needed = minimum if row['withdrawal_min_deposit_override'] is None else int(row['withdrawal_min_deposit_override'])
            until = ns['parse_datetime_utc'](row['stars_withdrawal_until'])
            r['withdrawal_blocked'] = not row['withdrawal_enabled'] or r['wager_remaining']>0 or int(row['deposits'])<needed or bool(until and until>datetime.now(timezone.utc))
            items.append(r)
        return jsonify(items=items,total=int(count),offset=offset,limit=limit)

    @app.get('/api/admin/payments')
    @admin
    def payments():
        offset, limit = page_args()
        method = request.args.get('method', '')
        user_id = request.args.get('user_id', 0, type=int)
        term = request.args.get('q', '').strip()[:100].casefold()
        conditions = ['1=1']; params = []
        if method in ('ton', 'stars', 'cryptobot', 'nft'):
            conditions.append('p.method=?'); params.append(method)
        if user_id:
            conditions.append('p.user_id=?'); params.append(user_id)
        if term:
            conditions.append("LOWER(COALESCE(u.name,'') || ' ' || COALESCE(u.username,'') || ' ' || CAST(p.user_id AS TEXT) || ' ' || p.gift_name || ' ' || p.reference) LIKE ? ESCAPE '\\'")
            params.append('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        for argument,operator in (('from','>='),('to','<=')):
            value = request.args.get(argument,'')
            if value:
                try:
                    datetime.strptime(value,'%Y-%m-%d')
                except ValueError:
                    return error('Некорректная дата.',400)
                conditions.append(f"SUBSTR(REPLACE(p.created_at,'T',' '),1,10){operator}?")
                params.append(value)
        where = ' AND '.join(conditions).replace('LOWER(',fold+'(')
        source = f'FROM ({PAYMENT_LEDGER}) p LEFT JOIN users u ON u.id=p.user_id WHERE {where}'
        with connect() as db:
            total = db.execute('SELECT COUNT(*) AS count '+source, tuple(params)).fetchone()['count']
            rows = db.execute('SELECT p.*,u.name,u.username '+source+' ORDER BY p.created_at DESC,p.id DESC LIMIT ? OFFSET ?', tuple(params+[limit, offset])).fetchall()
        items = []
        for row in rows:
            item = dict(row); item['amount'] = int(item['amount'])/100
            try:
                raw = json.loads(item.pop('raw_json') or '{}')
            except (ValueError, TypeError):
                raw = {}
            item['model'] = item.get('model') or (trait_name(raw.get('fragment_model') or raw.get('model')) if isinstance(raw,dict) else '')
            items.append(item)
        return jsonify(items=items,total=int(total),offset=offset,limit=limit)

    @app.get('/api/admin/nft-receipts')
    @admin
    def nft_receipts():
        offset, limit = page_args()
        term = request.args.get('q','').strip()[:100].casefold()
        params = []; where = '1=1'
        if term:
            where = "LOWER(e.gift_name || ' ' || e.external_key || ' ' || CAST(e.sender_user_id AS TEXT) || ' ' || COALESCE(u.username,'') || ' ' || e.sender_name) LIKE ? ESCAPE '\\'"
            params.append('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        where=where.replace('LOWER(',fold+'(')
        with connect() as db:
            source = f'FROM relayer_gift_events e LEFT JOIN users u ON u.id=e.sender_user_id WHERE {where}'
            count = db.execute('SELECT COUNT(*) AS count '+source,tuple(params)).fetchone()['count']
            rows = db.execute('SELECT e.id,e.sender_user_id,e.sender_name,e.gift_name,e.fragment_number,e.fragment_model,e.external_url,e.floor_price,e.portal_price,e.status,e.created_at,e.credited_at,u.username '+source+' ORDER BY e.id DESC LIMIT ? OFFSET ?',tuple(params+[limit,offset])).fetchall()
        return jsonify(items=[dict(r, floor_price=int(r['floor_price'])/100,portal_price=int(r['portal_price'])/100) for r in rows],total=int(count),offset=offset,limit=limit)

    @app.get('/api/admin/grants')
    @admin
    def grants():
        offset, limit = page_args()
        with connect() as db:
            actions = "('admin_deposit','main_balance_add','main_balance_set','bonus_balance_add','bonus_balance_set','referral_balance_add','referral_balance_set')"
            rows = db.execute(f'''SELECT a.id,a.user_id,u.name,u.username,a.action,a.details,a.admin_id,a.created_at
                   FROM admin_log a LEFT JOIN users u ON u.id=a.user_id
                   WHERE a.action IN {actions} ORDER BY a.id DESC LIMIT ? OFFSET ?''',(limit,offset)).fetchall()
            count = db.execute(f'SELECT COUNT(*) AS count FROM admin_log WHERE action IN {actions}').fetchone()['count']
        items=[]
        for r in rows:
            try:
                amount=int(r['details'])/100
            except (TypeError,ValueError):
                amount=None
            items.append(dict(r,amount=amount))
        return jsonify(items=items,total=int(count),offset=offset,limit=limit)

    @app.get('/api/admin/users/<int:user_id>/withdrawal-state')
    @admin
    def user_withdrawal_state(user_id):
        with connect() as db:
            state = withdrawal_state(db,user_id,ns['withdrawal_settings']())
        return jsonify(**state) if state else error('Пользователь не найден.',404)

    @app.post('/api/admin/users/<int:user_id>/wager')
    @admin
    def user_wager(user_id):
        data = request.get_json(silent=True) or {}
        mode = data.get('mode', 'set_remaining')
        try:
            amount = 0 if mode == 'clear' else parse_amount(data.get('amount'))
        except (TypeError,ValueError,InvalidOperation):
            return error('Введите остаток отыгрыша с точностью до 0.01 TON.')
        if mode not in ('clear','set_remaining','add') or not 0 <= amount <= 100000000:
            return error('Некорректная операция или сумма отыгрыша.')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Lock against concurrent game stakes and deposit confirmations on PostgreSQL.
            row = db.execute('SELECT withdrawal_wager_required,withdrawal_wager_progress FROM users WHERE id=?'+(' FOR UPDATE' if ns['DATABASE_URL'] else ''),(user_id,)).fetchone()
            if not row:
                return error('Пользователь не найден.',404)
            progress = max(0,int(row['withdrawal_wager_progress'] or 0))
            before = max(0,int(row['withdrawal_wager_required'] or 0)-progress)
            remaining = before+amount if mode == 'add' else amount
            db.execute('UPDATE users SET withdrawal_wager_required=? WHERE id=?',(progress+remaining,user_id))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'],user_id,'withdrawal_wager',json.dumps(dict(mode=mode,before=before,after=remaining))))
            db.commit()
            state = withdrawal_state(db,user_id,ns['withdrawal_settings']())
        return jsonify(ok=True,**state)

    @app.get('/api/cryptobot/settings')
    @login
    def crypto_settings_public():
        cfg = config()
        return jsonify(enabled=cfg['enabled'],testnet=cfg['testnet'])

    @app.get('/api/admin/cryptobot')
    @admin
    def crypto_settings_admin():
        return jsonify(**public_crypto())

    @app.post('/api/admin/cryptobot')
    @admin
    def crypto_settings_save():
        data = request.get_json(silent=True) or {}
        if any(key in data and not isinstance(data[key],bool) for key in ('enabled','testnet')):
            return error('enabled и testnet должны быть true или false.')
        cfg = config()
        token = str(data.get('token') or '').strip()
        if len(token)>500 or '\n' in token or '\r' in token:
            return error('Некорректный токен Crypto Pay.')
        proposed = dict(cfg,token=cfg['token'] if cfg['env_token'] or not token else token,
                        testnet=bool(data.get('testnet',cfg['testnet'])),enabled=bool(data.get('enabled',cfg['enabled'])))
        if proposed['enabled'] and not proposed['token']:
            return error('Добавьте Crypto Pay API token.')
        if proposed['token']:
            try:
                identity = crypto_request('getMe',cfg=proposed)
            except RuntimeError as exc:
                return error(str(exc),502)
            proposed.update(app_id=identity.get('app_id'),app_name=str(identity.get('name') or '')[:100])
        if fingerprint(proposed) != fingerprint(cfg):
            with connect() as db:
                pending = db.execute("SELECT COUNT(*) AS count FROM crypto_deposit_orders WHERE status='active' AND expires_at>?", (datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),)).fetchone()
            if pending['count']:
                return error('Есть неоплаченные счета. Смените токен или сеть после их истечения (до 30 минут).',409)
        saved = {k:v for k,v in proposed.items() if k not in ('env_token',)}
        if cfg['env_token']:
            saved.pop('token',None)
        ns['save_document']('crypto_pay_settings',saved)
        with connect() as db:
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,0,?,?)',
                       (session['uid'],'crypto_pay_settings',json.dumps(dict(enabled=proposed['enabled'],testnet=proposed['testnet'],app_id=proposed.get('app_id')))))
        return jsonify(ok=True,**public_crypto())

    def credit_invoice(invoice, cfg):
        if not isinstance(invoice,dict) or invoice.get('status') != 'paid':
            return False
        invoice_id = str(invoice.get('invoice_id') or '')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM crypto_deposit_orders WHERE invoice_id=? AND token_fingerprint=?'+(' FOR UPDATE' if ns['DATABASE_URL'] else ''),(invoice_id,fingerprint(cfg))).fetchone()
            # A webhook can arrive before createInvoice's HTTP response, or after
            # the HTTP response was lost. Attach it by the server-generated payload.
            if not row and invoice_id.isdigit() and int(invoice_id)>0:
                row = db.execute("SELECT * FROM crypto_deposit_orders WHERE payload=? AND token_fingerprint=? AND invoice_id='' AND status IN ('creating','failed')"+(' FOR UPDATE' if ns['DATABASE_URL'] else ''),(str(invoice.get('payload') or ''),fingerprint(cfg))).fetchone()
            if not row:
                raise ValueError('Неизвестный счёт.')
            try:
                amount = Decimal(str(invoice.get('amount')))
            except (TypeError,ValueError,InvalidOperation):
                raise ValueError('Неверная сумма счёта.')
            if not amount.is_finite() or amount != Decimal(int(row['amount']))/100 or invoice.get('asset') != 'TON' or invoice.get('currency_type','crypto') != 'crypto' or not hmac.compare_digest(str(invoice.get('payload') or ''),row['payload']):
                raise ValueError('Параметры счёта не совпадают.')
            if row['status'] in ('credited','test_paid'):
                return False
            if row['status'] not in ('active','expired') and not (not row['invoice_id'] and row['status'] in ('creating','failed')):
                raise ValueError('Счёт ещё не зарегистрирован.')
            if not row['invoice_id']:
                db.execute('UPDATE crypto_deposit_orders SET invoice_id=? WHERE id=?',(invoice_id,row['id']))
            uid = int(row['user_id']); cents = int(row['amount'])
            if not row['testnet']:
                # Unique provider key and order lock make webhook + polling races safe.
                key = 'crypto:'+row['id']
                inserted = db.execute('''INSERT INTO deposits(user_id,amount,admin_id,request_key)
                                         VALUES(?,?,0,?) ON CONFLICT(request_key) DO NOTHING''',(uid,cents,key))
                if inserted.rowcount:
                    db.execute('UPDATE users SET balance=balance+? WHERE id=?',(cents,uid))
                    ns['add_withdrawal_wager_requirement'](db,uid,cents)
                    ns['apply_bonus_gift_deposit_progress'](db,uid,cents)
                    active = db.execute("SELECT code FROM promo_redemptions WHERE user_id=? AND reward_type='deposit_bonus' AND consumed_at IS NULL AND deactivated_at IS NULL ORDER BY created_at DESC LIMIT 1",(uid,)).fetchone()
                    if active:
                        bonus,promo = ns['_consume_deposit_bonus'](db,uid,cents,active['code'])
                        if bonus:
                            db.execute('UPDATE users SET bonus_balance=bonus_balance+? WHERE id=?',(bonus,uid))
                            ns['record_transaction'](db,uid,'deposit_promo_bonus',bonus,'crypto_invoice',invoice_id,'Бонус промокода '+promo['code'])
                    ns['record_transaction'](db,uid,'crypto_deposit',cents,'crypto_invoice',invoice_id,'Пополнение через CryptoBot · TON')
                    ns['hw_deposit_pumpkins'](db,uid,cents,'crypto_invoice',invoice_id)
                    ns['log_event'](db,uid,'deposit_confirmed',amount=cents/100,payment='cryptobot',order_id=row['id'])
            db.execute('UPDATE crypto_deposit_orders SET status=?,credited_at=CURRENT_TIMESTAMP,paid_at=? WHERE id=?',('test_paid' if row['testnet'] else 'credited',str(invoice.get('paid_at') or '')[:100],row['id']))
            db.commit()
        return True

    @app.post('/api/cryptobot/deposit/create')
    @login
    def crypto_create():
        cfg = config()
        if not cfg['enabled']:
            return error('CryptoBot временно недоступен.',503)
        if ns['creator_demo_active'](session['uid']):
            return error('Пополнение доступно в реальном режиме.',409)
        data = request.get_json(silent=True) or {}
        try:
            cents = parse_amount(data.get('amount'))
        except (TypeError,ValueError,InvalidOperation):
            return error('Введите сумму с точностью до 0.01 TON.')
        if not 10 <= cents <= 1000000:
            return error('Сумма CryptoBot: от 0.10 до 10 000 TON.')
        uid = int(session['uid']); order_id = secrets.token_urlsafe(24); payload = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('SELECT id FROM users WHERE id=?'+(' FOR UPDATE' if ns['DATABASE_URL'] else ''),(uid,)).fetchone()
            active = db.execute("SELECT * FROM crypto_deposit_orders WHERE user_id=? AND status IN ('creating','active') AND expires_at>? ORDER BY created_at DESC LIMIT 1",(uid,datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
            if active:
                if active['status']=='creating':
                    return error('Счёт создаётся. Повторите через несколько секунд.',409)
                return jsonify(ok=True,order_id=active['id'],invoice_url=active['pay_url'],amount=int(active['amount'])/100,testnet=bool(active['testnet']),reused=True)
            count = db.execute("SELECT COUNT(*) AS count FROM crypto_deposit_orders WHERE user_id=? AND created_at>?",(uid,(datetime.now(timezone.utc)-timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'))).fetchone()['count']
            if count>=5:
                return error('Слишком много счетов. Повторите через несколько минут.',429)
            db.execute('''INSERT INTO crypto_deposit_orders(id,user_id,amount,payload,token_fingerprint,testnet,expires_at)
                          VALUES(?,?,?,?,?,?,?)''',(order_id,uid,cents,payload,fingerprint(cfg),int(cfg['testnet']),expires))
            db.commit()
        settings = ns['ton_settings']()
        invoice_data = dict(currency_type='crypto',asset='TON',amount=f'{cents/100:.2f}',payload=payload,
                            description=f'{settings["site_name"]} · пополнение {cents/100:.2f} TON · ID {uid}',
                            allow_comments=False,allow_anonymous=False,expires_in=1800,
                            paid_btn_name='openBot',paid_btn_url='https://t.me/'+ns['BOT_USERNAME'])
        try:
            invoice = crypto_request('createInvoice',invoice_data,cfg)
            invoice_id = str(int(invoice['invoice_id']))
            url = str(invoice.get('bot_invoice_url') or invoice.get('pay_url') or '')
            if not url.startswith(('https://t.me/','https://pay.crypt.bot/','https://testnet-pay.crypt.bot/')):
                raise ValueError('Неверная ссылка счёта.')
            with connect() as db:
                db.execute("UPDATE crypto_deposit_orders SET invoice_id=?,pay_url=?,status=CASE WHEN status='creating' THEN 'active' ELSE status END WHERE id=?",(invoice_id,url,order_id))
            # Close the small create/save race if a provider already marks it paid.
            if invoice.get('status') == 'paid':
                credit_invoice(invoice,cfg)
        except (RuntimeError,ValueError,KeyError,TypeError) as exc:
            with connect() as db:
                db.execute("UPDATE crypto_deposit_orders SET status='failed' WHERE id=? AND status='creating'",(order_id,))
            return error(str(exc) if isinstance(exc,RuntimeError) else 'CryptoBot не создал корректный счёт.',502)
        return jsonify(ok=True,order_id=order_id,invoice_url=url,amount=cents/100,testnet=cfg['testnet'])

    @app.post('/api/cryptobot/deposit/<order_id>/verify')
    @login
    def crypto_verify(order_id):
        with connect() as db:
            row = db.execute('SELECT * FROM crypto_deposit_orders WHERE id=? AND user_id=?',(order_id,session['uid'])).fetchone()
        if not row:
            return error('Счёт не найден.',404)
        if row['status'] in ('credited','test_paid'):
            return jsonify(status=row['status'],user=ns['profile']())
        if not row['invoice_id']:
            return error('Счёт ещё не создан.',409)
        cfg = config()
        if fingerprint(cfg) != row['token_fingerprint']:
            return error('Настройки CryptoBot изменились. Обратитесь в поддержку.',409)
        try:
            result = crypto_request('getInvoices',dict(invoice_ids=row['invoice_id']),cfg)
            invoices = result.get('items',[]) if isinstance(result,dict) else result
            invoice = next((x for x in invoices if str(x.get('invoice_id')) == row['invoice_id']),None)
            if invoice:
                credit_invoice(invoice,cfg)
                if invoice.get('status') == 'expired':
                    with connect() as db:
                        db.execute("UPDATE crypto_deposit_orders SET status='expired' WHERE id=? AND status='active'",(order_id,))
        except (RuntimeError,ValueError,TypeError) as exc:
            return error(str(exc),502)
        with connect() as db:
            status = db.execute('SELECT status FROM crypto_deposit_orders WHERE id=?',(order_id,)).fetchone()['status']
        return jsonify(status=status,user=ns['profile']() if status in ('credited','test_paid') else None)

    @app.post('/api/cryptobot/webhook/<secret>')
    def crypto_webhook(secret):
        cfg = config()
        if not cfg['token']:
            return error('Webhook недоступен.',404)
        expected_path = hmac.new(cfg['token'].encode(),b'gemdrop-crypto-webhook-v1',hashlib.sha256).hexdigest()
        body = request.get_data(cache=True)
        expected = hmac.new(hashlib.sha256(cfg['token'].encode()).digest(),body,hashlib.sha256).hexdigest()
        if len(body)>65536 or not hmac.compare_digest(secret,expected_path) or not hmac.compare_digest(request.headers.get('crypto-pay-api-signature',''),expected):
            return error('Неверная подпись.',403)
        data = request.get_json(silent=True) or {}
        try:
            sent = datetime.fromisoformat(str(data.get('request_date') or '').replace('Z','+00:00'))
            if sent.tzinfo is None:
                raise ValueError()
            age = (datetime.now(timezone.utc)-sent).total_seconds()
            if age < -60 or age > 600:
                raise ValueError()
        except (ValueError,TypeError):
            return error('Неверное время webhook.',400)
        if data.get('update_type') != 'invoice_paid':
            return jsonify(ok=True,ignored=True)
        try:
            credited = credit_invoice(data.get('payload'),cfg)
        except ValueError as exc:
            # A retry can succeed once a just-created invoice has been persisted.
            return error(str(exc),409)
        return jsonify(ok=True,credited=credited)

    @app.get('/api/admin/withdrawals/<int:withdrawal_id>/procurement')
    @admin
    def procurement(withdrawal_id):
        row = ns['_relayer_withdrawal_target'](withdrawal_id)
        if not row:
            return error('Заявка не найдена.',404)
        name,model,backdrop = requested_traits(row,ns['read_catalog']().get('gifts',[]))
        return jsonify(id=withdrawal_id,collection=name,model=model,backdrop=backdrop,
                       recipient_id=int(row['user_id']),number=row.get('fragment_number') or '',
                       exact_url=row.get('external_url') or '',portal_url='https://t.me/portals_market_bot',
                       web_url='https://portals.tg/',relay_username=ns['ton_settings']()['relay_username'],
                       partner_configured=bool(ns['portal_partner_token']()),
                       steps=['Откройте Portal под своим аккаунтом. Найдите коллекцию и выберите указанную модель.',
                              'Купите NFT и выведите его на подключённый Telegram-аккаунт Relayer.',
                              'Нажмите «Повторить доставку». Relayer проверит коллекцию и модель и передаст NFT получателю.'])

    @app.post('/api/admin/withdrawals/<int:withdrawal_id>/retry')
    @admin
    def retry_withdrawal(withdrawal_id):
        row = ns['_relayer_withdrawal_target'](withdrawal_id)
        if not row or row['status'] != 'pending':
            return error('Ожидающая заявка не найдена.',404)
        from threading import Thread
        Thread(target=ns['_auto_withdrawal_thread'],args=(withdrawal_id,),daemon=True).start()
        with connect() as db:
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'],row['user_id'],'withdrawal_retry',str(withdrawal_id)))
        return jsonify(ok=True)
