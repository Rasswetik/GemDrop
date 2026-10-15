"""Weekly turnover leaderboard: ranking, per-place prizes, settlement.

Turnover is fed from app.increase_turnover (the single place every game goes through), so the
board needs no per-game queries. A period is settled lazily (first request or game after it ends),
by a background loop, or early by an admin. Prizes land in the winner's inventory.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

CONFIG_DOC = 'leaderboard_config'
MAX_PLACES = 20
MAX_PRIZES_PER_PLACE = 10
LIST_SIZE = 50
DEFAULTS = dict(enabled=True, period_hours=168, auto_repeat=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lb_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',finished_at TEXT,finish_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lb_turnover (
    period_id INTEGER NOT NULL,user_id INTEGER NOT NULL,cents INTEGER NOT NULL DEFAULT 0,plays INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(period_id,user_id)
);
CREATE TABLE IF NOT EXISTS lb_awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,period_id INTEGER NOT NULL,user_id INTEGER NOT NULL,place INTEGER NOT NULL,
    turnover INTEGER NOT NULL DEFAULT 0,gift_name TEXT NOT NULL DEFAULT '',image_url TEXT NOT NULL DEFAULT '',
    floor_price INTEGER NOT NULL DEFAULT 0,inventory_id INTEGER,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS lb_turnover_rank ON lb_turnover(period_id,cents);
CREATE INDEX IF NOT EXISTS lb_awards_period ON lb_awards(period_id,place);
"""

_ns = {}
_cache = dict(id=0, ends=0.0, at=0.0)


def mask_name(name):
    """«Da*ya», «w**t», «Cta***лав»: enough to recognise yourself, not enough to look anyone up."""
    text = re.sub(r'\s+', ' ', str(name or '').strip()) or 'Игрок'
    n = len(text)
    if n <= 2:
        return text[:1] + '*'
    k = max(1, min(3, n // 3))
    middle = max(1, min(5, n - 2 * k))
    return text[:k] + '*' * middle + text[-k:]


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def config():
    doc = dict(_ns['read_document'](CONFIG_DOC) or {})
    out = dict(DEFAULTS)
    out['enabled'] = bool(doc.get('enabled', DEFAULTS['enabled']))
    out['auto_repeat'] = bool(doc.get('auto_repeat', DEFAULTS['auto_repeat']))
    try:
        out['period_hours'] = max(1, min(24 * 90, int(doc.get('period_hours') or DEFAULTS['period_hours'])))
    except (TypeError, ValueError):
        out['period_hours'] = DEFAULTS['period_hours']
    rewards = {}
    raw = doc.get('rewards') if isinstance(doc.get('rewards'), dict) else {}
    for key, prizes in raw.items():
        try:
            place = int(key)
        except (TypeError, ValueError):
            continue
        if 1 <= place <= MAX_PLACES and isinstance(prizes, list) and prizes:
            rewards[str(place)] = [p for p in prizes if isinstance(p, dict)][:MAX_PRIZES_PER_PLACE]
    out['rewards'] = rewards
    return out


def save_config(cfg):
    _ns['save_document'](CONFIG_DOC, dict(enabled=bool(cfg['enabled']), auto_repeat=bool(cfg['auto_repeat']),
                                          period_hours=int(cfg['period_hours']), rewards=cfg['rewards']))


def enabled():
    try:
        return config()['enabled']
    except Exception:
        return False


def prize_view(prize):
    """What the player sees: picture, name, price and the ids the TGS hover needs."""
    return dict(id=str(prize.get('id') or ''), name=prize.get('name') or 'Подарок', base_name=prize.get('base_name') or '',
                model=prize.get('model') or '', gift_id=prize.get('gift_id') or '', image_url=prize.get('image_url') or '',
                price_ton=prize.get('price_ton') or 0, source_type=prize.get('source_type') or 'catalog',
                fragment_url=prize.get('fragment_url') or '', fragment_number=prize.get('fragment_number') or '',
                animation_url=prize.get('animation_url') or '')


# ---------------------------------------------------------------- periods
def active_period(db, create=True):
    row = db.execute("SELECT * FROM lb_periods WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    if row or not create:
        return row
    cfg = config()
    if not cfg['enabled']:
        return None
    return start_period(db, cfg['period_hours'])


def start_period(db, hours, start=None):
    start = start or now_utc()
    db.execute("INSERT INTO lb_periods(starts_at,ends_at,status) VALUES(?,?,'active')",
               (iso(start), iso(start + timedelta(hours=hours))))
    return db.execute("SELECT * FROM lb_periods WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()


def settle_due(db):
    row = db.execute("SELECT * FROM lb_periods WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        end = parse(row['ends_at'])
        if end and end <= now_utc():
            settle(db, int(row['id']), 'ended')
            _cache['at'] = 0.0
            return True
    return False


def current_period(db):
    """Active period, settling an overdue one first; None when the board is switched off."""
    settle_due(db)
    return active_period(db)


def top_rows(db, period_id, limit):
    return db.execute('SELECT user_id,cents,plays FROM lb_turnover WHERE period_id=? AND cents>0 '
                      'ORDER BY cents DESC,user_id ASC LIMIT ?', (period_id, limit)).fetchall()


def inventory_values(prize):
    gid = str(prize.get('gift_id') or '')
    sep = _ns['GIFT_MODEL_SEP']
    base, _, model = gid.partition(sep)
    return dict(gift_id=base or gid, gift_name=str(prize.get('name') or 'Подарок')[:180], image_url=prize.get('image_url') or '',
                price=int(round(float(prize.get('price_ton') or 0) * 100)),
                url=prize.get('fragment_url') or '', number=prize.get('fragment_number') or '',
                model=(prize.get('fragment_model') or prize.get('model') or model or ''),
                backdrop=prize.get('fragment_backdrop') or '', symbol=prize.get('fragment_symbol') or '',
                animation=prize.get('animation_url') or '')


def award_prize(db, user_id, prize):
    v = inventory_values(prize)
    cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,external_url,
                        fragment_number,fragment_model,fragment_backdrop,fragment_symbol,price_source,animation_url)
                        VALUES(?,?,?,?,?,'leaderboard',?,?,?,?,?,?,?)""",
                     (user_id, v['gift_id'], v['gift_name'], v['image_url'], v['price'], v['url'], v['number'],
                      v['model'], v['backdrop'], v['symbol'], 'Leaderboard', v['animation']))
    return cur.lastrowid, v


def settle(db, period_id, reason='ended', award=True):
    moved = db.execute("UPDATE lb_periods SET status='settling' WHERE id=? AND status='active'", (period_id,)).rowcount
    if not moved:
        return False
    cfg = config()
    won = []
    if award and cfg['rewards']:
        last = max(int(k) for k in cfg['rewards'])
        for place, row in enumerate(top_rows(db, period_id, last), 1):
            for prize in cfg['rewards'].get(str(place), []):
                try:
                    inv_id, v = award_prize(db, int(row['user_id']), prize)
                except Exception:  # one broken prize must not block the others
                    _ns['app'].logger.exception('Leaderboard prize failed: place %s', place)
                    continue
                db.execute('INSERT INTO lb_awards(period_id,user_id,place,turnover,gift_name,image_url,floor_price,inventory_id) '
                           'VALUES(?,?,?,?,?,?,?,?)', (period_id, int(row['user_id']), place, int(row['cents']),
                                                       v['gift_name'], v['image_url'], v['price'], inv_id))
                won.append((int(row['user_id']), place, v['gift_name']))
    db.execute("UPDATE lb_periods SET status='finished',finished_at=?,finish_reason=? WHERE id=?",
               (iso(now_utc()), reason, period_id))
    notify = _ns.get('notify_user_async')
    if notify:
        by_user = {}
        for uid, place, name in won:
            by_user.setdefault(uid, []).append((place, name))
        for uid, items in by_user.items():
            lines = '\n'.join(f'{place} место — {_ns["escape"](name)}' for place, name in items[:10])
            try:
                notify(uid, f'🏆 <b>Недельный топ завершён</b>\nВаши награды:\n{lines}\nПодарки уже в инвентаре.', None, 'HTML', db=db)
            except Exception:
                _ns['app'].logger.exception('Leaderboard notification failed')
    if cfg['enabled'] and (cfg['auto_repeat'] or reason != 'ended'):
        start_period(db, cfg['period_hours'])
    return True


def add_turnover(db, user_id, cents):
    """Called by increase_turnover for every wager. Never allowed to break a game."""
    if cents <= 0:
        return
    try:
        stamp = time.time()
        if _cache['id'] and _cache['ends'] > stamp and stamp - _cache['at'] < 30:
            pid = _cache['id']
        else:
            period = current_period(db)
            if not period:
                _cache.update(id=0, ends=0.0, at=stamp)
                return
            pid = int(period['id'])
            end = parse(period['ends_at'])
            _cache.update(id=pid, ends=end.timestamp() if end else 0.0, at=stamp)
        updated = db.execute('UPDATE lb_turnover SET cents=cents+?,plays=plays+1 WHERE period_id=? AND user_id=?',
                             (int(cents), pid, int(user_id))).rowcount
        if not updated:
            db.execute('INSERT INTO lb_turnover(period_id,user_id,cents,plays) VALUES(?,?,?,1) ON CONFLICT(period_id,user_id) '
                       'DO UPDATE SET cents=lb_turnover.cents+excluded.cents,plays=lb_turnover.plays+1',
                       (pid, int(user_id), int(cents)))
    except Exception:
        _ns['app'].logger.exception('Leaderboard turnover failed')


def place_of(db, user_id, period_id=None):
    """(place, cents) in the running period; place is None when the player has no turnover."""
    period = db.execute("SELECT id FROM lb_periods WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone() if period_id is None else {'id': period_id}
    if not period:
        return None, 0
    row = db.execute('SELECT cents FROM lb_turnover WHERE period_id=? AND user_id=?', (period['id'], int(user_id))).fetchone()
    cents = int(row['cents']) if row else 0
    if cents <= 0:
        return None, 0
    ahead = db.execute('SELECT COUNT(*) AS n FROM lb_turnover WHERE period_id=? AND (cents>? OR (cents=? AND user_id<?))',
                       (period['id'], cents, cents, int(user_id))).fetchone()['n']
    return int(ahead) + 1, cents


# ---------------------------------------------------------------- prize normalisation (admin)
def normalise_prize(raw):
    ns = _ns
    source = str(raw.get('source_type') or raw.get('type') or 'catalog')
    if source == 'fragment':
        gift = ns['fragment_gift_from_url'](raw.get('fragment_url') or raw.get('url'), True,
                                            allow_missing_price=raw.get('price_ton') not in (None, ''))
        ns['apply_fragment_price'](gift, raw.get('price_ton'))
        if raw.get('animation_url'):
            gift['animation_url'] = ns['safe_image'](raw.get('animation_url'))
        out = dict(source_type='fragment', gift_id=str(gift.get('gift_id') or ''), name=str(gift['gift_name'])[:180],
                   base_name=str(gift['gift_name'])[:140], model=str(gift.get('fragment_model') or ''), image_url=gift['image_url'],
                   price_ton=int(gift.get('floor_price') or 0) / 100, fragment_url=gift.get('fragment_url') or '',
                   fragment_number=gift.get('fragment_number') or '', fragment_model=gift.get('fragment_model') or '',
                   fragment_backdrop=gift.get('fragment_backdrop') or '', fragment_symbol=gift.get('fragment_symbol') or '',
                   animation_url=gift.get('animation_url') or '')
    elif source == 'catalog':
        gift = ns['catalog_gift_ref'](raw.get('gift_id'))
        if not gift:
            raise ValueError('Подарок или модель не найдены в каталоге.')
        gid = str(gift.get('id') or raw.get('gift_id'))
        base_id, _, model = gid.partition(ns['GIFT_MODEL_SEP'])
        image = ns['safe_image'](gift.get('image_url') or gift.get('portal_image_url'))
        if not image:
            raise ValueError('У подарка нет изображения.')
        price = ns['ton_to_cents'](gift.get('price_ton') or 0)
        out = dict(source_type='catalog', gift_id=gid, name=str(gift.get('name') or 'Подарок')[:180],
                   base_name=str(gift.get('base_name') or gift.get('name') or '')[:140].split(' · ')[0], model=model,
                   image_url=image, price_ton=price / 100)
    else:
        raise ValueError('Неизвестный источник подарка.')
    if ns['gift_black_background'](out) and not ns['black_backgrounds_enabled']():
        raise ValueError('Отображение Black и Onyx Black выключено.')
    out['id'] = _ns['secrets'].token_hex(5)
    return out


def register(app, ns):
    _ns.update(ns)
    _ns['app'] = app
    connect = ns['connect']
    login = ns['login_required']
    admin = ns['admin_required']
    error = ns['error']
    from flask import jsonify, request, session

    with connect() as db:
        for statement in [s.strip() for s in SCHEMA.split(';') if s.strip()]:
            db.execute(statement)

    def period_view(db, period):
        if not period:
            return None
        stats = db.execute('SELECT COUNT(*) AS n,COALESCE(SUM(cents),0) AS total FROM lb_turnover WHERE period_id=? AND cents>0',
                           (period['id'],)).fetchone()
        return dict(id=int(period['id']), starts_at=period['starts_at'], ends_at=period['ends_at'],
                    participants=int(stats['n']), turnover=int(stats['total']) / 100)

    def rewards_view(cfg):
        return {place: [prize_view(p) for p in prizes] for place, prizes in cfg['rewards'].items()}

    @app.get('/api/leaderboard/weekly')
    @login
    def leaderboard_weekly():
        cfg = config()
        if not cfg['enabled']:
            return jsonify(enabled=False)
        uid = int(session['uid'])
        with connect() as db:
            period = current_period(db)
            rows = top_rows(db, period['id'], LIST_SIZE) if period else []
            users = {}
            if rows:
                marks = ','.join('?' for _ in rows)
                for r in db.execute(f'SELECT id,name,username,photo_url FROM users WHERE id IN ({marks})', [int(x['user_id']) for x in rows]).fetchall():
                    users[int(r['id'])] = r
            rewards = rewards_view(cfg)
            top = []
            for place, row in enumerate(rows, 1):
                u = users.get(int(row['user_id']))
                top.append(dict(place=place, user_id=int(row['user_id']), name=mask_name(u['name'] if u else ''),
                                photo_url=(u['photo_url'] if u else '') or '', turnover=int(row['cents']) / 100, plays=int(row['plays']),
                                rewards=rewards.get(str(place), []), me=int(row['user_id']) == uid))
            place, cents = place_of(db, uid, period['id']) if period else (None, 0)
            me_row = db.execute('SELECT name,photo_url FROM users WHERE id=?', (uid,)).fetchone()
            plays = db.execute('SELECT plays FROM lb_turnover WHERE period_id=? AND user_id=?', (period['id'], uid)).fetchone() if period else None
            view = period_view(db, period)
        return jsonify(enabled=True, period=view, rewards=rewards, top=top,
                       me=dict(user_id=uid, place=place, turnover=cents / 100, plays=int(plays['plays']) if plays else 0,
                               name=(me_row['name'] if me_row else '') or 'Игрок', photo_url=(me_row['photo_url'] if me_row else '') or '',
                               rewards=rewards.get(str(place), []) if place else []))

    # ---------------------------------------------------------------- admin
    @app.get('/api/admin/leaderboard')
    @admin
    def admin_leaderboard():
        cfg = config()
        with connect() as db:
            period = active_period(db, create=cfg['enabled'])
            view = period_view(db, period)
            rows = top_rows(db, period['id'], 10) if period else []
            names = {int(r['id']): r for r in db.execute('SELECT id,name,username FROM users WHERE id IN (%s)' % (','.join('?' for _ in rows) or '0'),
                                                       [int(x['user_id']) for x in rows]).fetchall()} if rows else {}
            top = [dict(place=i, user_id=int(r['user_id']), name=(names[int(r['user_id'])]['name'] if int(r['user_id']) in names else ''),
                        username=(names[int(r['user_id'])]['username'] if int(r['user_id']) in names else ''),
                        turnover=int(r['cents']) / 100) for i, r in enumerate(rows, 1)]
            history = [dict(id=int(r['id']), starts_at=r['starts_at'], ends_at=r['ends_at'], finished_at=r['finished_at'], reason=r['finish_reason'])
                       for r in db.execute("SELECT * FROM lb_periods WHERE status='finished' ORDER BY id DESC LIMIT 8").fetchall()]
            awards = [dict(period_id=int(r['period_id']), place=int(r['place']), user_id=int(r['user_id']), gift_name=r['gift_name'],
                           image_url=r['image_url'], price_ton=int(r['floor_price'] or 0) / 100, created_at=r['created_at'])
                      for r in db.execute('SELECT * FROM lb_awards ORDER BY id DESC LIMIT 30').fetchall()]
        return jsonify(config=dict(enabled=cfg['enabled'], period_hours=cfg['period_hours'], auto_repeat=cfg['auto_repeat']),
                       rewards=rewards_view(cfg), period=view, top=top, history=history, awards=awards, max_places=MAX_PLACES)

    @app.post('/api/admin/leaderboard/config')
    @admin
    def admin_leaderboard_config():
        data = request.get_json(silent=True) or {}
        cfg = config()
        if 'enabled' in data:
            cfg['enabled'] = bool(data['enabled'])
        if 'auto_repeat' in data:
            cfg['auto_repeat'] = bool(data['auto_repeat'])
        if 'period_hours' in data:
            try:
                hours = int(data['period_hours'])
            except (TypeError, ValueError):
                return error('Длительность периода — целое число часов.')
            if not 1 <= hours <= 24 * 90:
                return error('Длительность периода: от 1 часа до 90 дней.')
            cfg['period_hours'] = hours
        save_config(cfg)
        _cache.update(id=0, ends=0.0, at=0.0)
        return jsonify(ok=True)

    @app.post('/api/admin/leaderboard/rewards/<int:place>')
    @admin
    def admin_leaderboard_rewards(place):
        if not 1 <= place <= MAX_PLACES:
            return error(f'Места: от 1 до {MAX_PLACES}.')
        raw = (request.get_json(silent=True) or {}).get('prizes')
        if not isinstance(raw, list) or len(raw) > MAX_PRIZES_PER_PLACE:
            return error(f'За одно место — не более {MAX_PRIZES_PER_PLACE} наград.')
        cfg = config()
        known = {p.get('id'): p for ps in cfg['rewards'].values() for p in ps if p.get('id')}
        try:
            prizes = []
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError('Проверьте список наград.')
                # Already normalised prizes keep their id and data; new ones are resolved from the catalog / Fragment.
                if item.get('id') in known and not item.get('_new'):
                    prizes.append(known[item['id']])
                else:
                    prizes.append(normalise_prize(item))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return error(str(exc))
        if prizes:
            cfg['rewards'][str(place)] = prizes
        else:
            cfg['rewards'].pop(str(place), None)
        save_config(cfg)
        return jsonify(ok=True, prizes=[prize_view(p) for p in prizes])

    @app.post('/api/admin/leaderboard/period')
    @admin
    def admin_leaderboard_period():
        data = request.get_json(silent=True) or {}
        with connect() as db:
            period = active_period(db)
            if not period:
                return error('Таблица лидеров выключена.', 409)
            if data.get('ends_at'):
                end = parse(data['ends_at'])
            else:
                try:
                    end = parse(period['ends_at']) + timedelta(hours=float(data.get('extend_hours') or 0))
                except (TypeError, ValueError):
                    return error('Проверьте срок.')
            if not end or end <= now_utc():
                return error('Окончание должно быть в будущем. Чтобы завершить сейчас, нажмите «Завершить досрочно».')
            db.execute('UPDATE lb_periods SET ends_at=? WHERE id=?', (iso(end), period['id']))
        _cache.update(id=0, ends=0.0, at=0.0)
        return jsonify(ok=True, ends_at=iso(end))

    @app.post('/api/admin/leaderboard/finish')
    @admin
    def admin_leaderboard_finish():
        data = request.get_json(silent=True) or {}
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            period = active_period(db, create=False)
            if not period:
                return error('Нет активного периода.', 409)
            settle(db, int(period['id']), 'early' if data.get('award', True) else 'reset', award=bool(data.get('award', True)))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'leaderboard_finish', 'award' if data.get('award', True) else 'reset'))
        _cache.update(id=0, ends=0.0, at=0.0)
        return jsonify(ok=True)

    def loop():
        while True:
            try:
                with connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if config()['enabled']:
                        current_period(db)
            except Exception:
                app.logger.exception('Leaderboard loop failed')
            time.sleep(30)

    return loop
