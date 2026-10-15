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
LIST_SIZE = 100
DEFAULTS = dict(enabled=True, period_hours=168, auto_repeat=False)
MEDALS = {'hw1': 'Король Хэллоуина', 'hw2': 'Призрачный мастер', 'hw3': 'Повелитель теней', 'hw4': 'Ведьмин избранник', 'hw5': 'Хранитель костей',
          'c1': 'Золотая медаль', 'c2': 'Серебряная медаль', 'c3': 'Бронзовая медаль',
          'vip': 'VIP игрок', 'gem': 'Кристаллик', 'champ': 'Чемпион', 'pioneer': 'Первопроходец', 'insider': 'Инсайдер', 'friend': 'Верный друг',
          'guard': 'Защитник', 'collector': 'Коллекционер', 'ton': 'Миллионер TON', 'lucky': 'Счастливчик', 'fire': 'Горячая серия', 'bolt': 'Молния',
          'rocket': 'Ракета Plinko', 'gambler': 'Азартный', 'miner': 'Сапёр', 'cards': 'Мастер карт', 'night': 'Ночной игрок'}   # drawn in static/js/medals.js
MAX_SHOWN_MEDALS = 5

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
CREATE TABLE IF NOT EXISTS user_medals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,medal_key TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',
    place INTEGER NOT NULL DEFAULT 0,period_id INTEGER NOT NULL DEFAULT 0,shown INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS user_medals_user ON user_medals(user_id,id);
CREATE INDEX IF NOT EXISTS lb_turnover_rank ON lb_turnover(period_id,cents);
CREATE INDEX IF NOT EXISTS lb_awards_period ON lb_awards(period_id,place);
"""

_ns = {}
_cache = dict(id=0, ends=0.0, at=0.0)


def mask_name(name):
    """Two middle characters become stars: «@us**rname». Without a username the display name is used the same way."""
    text = re.sub(r'\s+', '', str(name or '')) or 'Игрок'
    n = len(text)
    if n <= 1:
        return text + '*'
    if n == 2:
        return text[:1] + '*'
    start = max(1, (n - 2) // 2)
    return text[:start] + '**' + text[start + 2:]


def public_handle(username, name):
    handle = str(username or '').lstrip('@').strip()
    return '@' + mask_name(handle) if handle else mask_name(name)


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
    medals = {}
    for key, medal in (doc.get('medals') if isinstance(doc.get('medals'), dict) else {}).items():
        try:
            place = int(key)
        except (TypeError, ValueError):
            continue
        if 1 <= place <= MAX_PLACES and medal in MEDALS:
            medals[str(place)] = medal
    out['medals'] = medals
    return out


def save_config(cfg):
    _ns['save_document'](CONFIG_DOC, dict(enabled=bool(cfg['enabled']), auto_repeat=bool(cfg['auto_repeat']),
                                          period_hours=int(cfg['period_hours']), rewards=cfg['rewards'], medals=cfg.get('medals') or {}))


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
def active_period(db, create=False):
    """The running season, or None. A season never starts by itself: only an admin starts one."""
    return db.execute("SELECT * FROM lb_periods WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()


def start_period(db, hours, start=None):
    start = start or now_utc()
    db.execute("INSERT INTO lb_periods(starts_at,ends_at,status) VALUES(?,?,'active')",
               (iso(start), iso(start + timedelta(hours=hours))))
    return db.execute("SELECT * FROM lb_periods WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()


def season_title(db, period):
    """Admin-given name of the season, or «Сезон N» by its order."""
    try:
        custom = str(period['title'] or '').strip()
    except (KeyError, IndexError, TypeError):
        custom = ''
    if custom:
        return custom
    n = db.execute('SELECT COUNT(*) AS n FROM lb_periods WHERE id<=?', (int(period['id']),)).fetchone()['n']
    return 'Сезон %d' % int(n)


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
    if prize.get('source_type') == 'ton':
        cents = int(round(float(prize.get('price_ton') or 0) * 100))
        if cents <= 0:
            raise ValueError('Сумма TON должна быть больше нуля.')
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (cents, user_id))
        _ns['record_transaction'](db, user_id, 'leaderboard_prize', cents, 'leaderboard', 0, 'Лидерборд · награда')
        v.update(gift_name='%s TON' % ('%g' % (cents / 100)), image_url='/static/img/ton.png', price=cents)
        return None, v
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
    prow = db.execute('SELECT * FROM lb_periods WHERE id=?', (period_id,)).fetchone()
    title = season_title(db, prow) if prow else 'Сезон'
    medal_notes = {}
    if award and (cfg['rewards'] or cfg['medals']):
        last = max([int(k) for k in cfg['rewards']] + [int(k) for k in cfg['medals']])
        for place, row in enumerate(top_rows(db, period_id, last), 1):
            medal = cfg['medals'].get(str(place))
            if medal:
                db.execute('INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown) VALUES(?,?,?,?,?,1)',
                           (int(row['user_id']), medal, title, place, period_id))
                medal_notes[int(row['user_id'])] = (place, MEDALS[medal])
            for prize in cfg['rewards'].get(str(place), []):
                try:
                    inv_id, v = award_prize(db, int(row['user_id']), prize)
                except Exception:  # one broken prize must not block the others
                    _ns['app'].logger.exception('Leaderboard prize failed: place %s', place)
                    continue
                db.execute('INSERT INTO lb_awards(period_id,user_id,place,turnover,gift_name,image_url,floor_price,inventory_id) '
                           'VALUES(?,?,?,?,?,?,?,?)', (period_id, int(row['user_id']), place, int(row['cents']),
                                                       v['gift_name'], v['image_url'], v['price'], inv_id))
                won.append((int(row['user_id']), place, v['gift_name'], prize.get('source_type') == 'ton', v['gift_id']))
    db.execute("UPDATE lb_periods SET status='finished',finished_at=?,finish_reason=? WHERE id=?",
               (iso(now_utc()), reason, period_id))
    notify = _ns.get('notify_user_async')
    if notify:
        by_user = {}
        for uid, place, name, is_ton, gid in won:
            by_user.setdefault(uid, []).append((place, name, is_ton, gid))
        for uid in medal_notes:
            by_user.setdefault(uid, [])
        for uid, items in by_user.items():
            def line(place, name, is_ton, gid):
                if is_ton:
                    amount = name.rsplit(' ', 1)[0]
                    return f'<b>{place} место</b> — {_ns["escape"](amount)} [[ton]]'
                return f'<b>{place} место</b> — {_ns["gift_custom_emoji_html"](gid, name)} {_ns["escape"](name)}'
            lines = '\n'.join(line(*it) for it in items[:10])
            tail = ('Подарки уже в инвентаре, TON — на балансе.' if any(i[2] for i in items) else 'Подарки уже в инвентаре.') if items else ''
            if uid in medal_notes:
                tail = (tail + '\n' if tail else '') + '🏅 Медаль «%s» — в профиле, раздел «Мои медали».' % _ns['escape'](medal_notes[uid][1])
            if not lines:
                lines = '<b>%d место</b>' % medal_notes[uid][0]
            try:
                notify(uid, f'🏆 <b>{_ns["escape"](title)} завершён</b>\nВы заняли призовое место!\n{lines}\n{tail}', None, 'HTML', db=db)
            except Exception:
                _ns['app'].logger.exception('Leaderboard notification failed')
    if cfg['enabled'] and cfg['auto_repeat'] and reason == 'ended':
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
    if source == 'ton':
        try:
            amount = round(float(str(raw.get('price_ton') or raw.get('amount') or 0).replace(',', '.')), 2)
        except (TypeError, ValueError):
            amount = 0
        if not 0.01 <= amount <= 100000:
            raise ValueError('Сумма TON: от 0.01 до 100 000.')
        out = dict(source_type='ton', gift_id='', name='%g TON' % amount, base_name='TON', model='', image_url='/static/img/ton.png', price_ton=amount)
        out['id'] = _ns['secrets'].token_hex(5)
        return out
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

    try:
        with connect() as db:
            # executescript translates AUTOINCREMENT/INTEGER for PostgreSQL; a failure must never stop the app booting.
            db.executescript(SCHEMA)
    except Exception:
        app.logger.exception('Leaderboard schema setup failed')
    try:
        with connect() as db:
            have = {r['name'] for r in db.execute('PRAGMA table_info(lb_periods)').fetchall()} if not ns['DATABASE_URL'] else set()
            if 'title' not in have:
                db.execute("ALTER TABLE lb_periods ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass   # column already there (PostgreSQL) or the table is not reachable right now

    def period_view(db, period):
        if not period:
            return None
        stats = db.execute('SELECT COUNT(*) AS n,COALESCE(SUM(cents),0) AS total FROM lb_turnover WHERE period_id=? AND cents>0',
                           (period['id'],)).fetchone()
        return dict(id=int(period['id']), title=season_title(db, period), starts_at=period['starts_at'], ends_at=period['ends_at'],
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
            rewards = rewards_view(cfg) if period else {}
            top = []
            for place, row in enumerate(rows, 1):
                u = users.get(int(row['user_id']))
                top.append(dict(place=place, user_id=int(row['user_id']), name=public_handle(u['username'] if u else '', u['name'] if u else ''),
                                photo_url=(u['photo_url'] if u else '') or '', turnover=int(row['cents']) / 100, plays=int(row['plays']),
                                rewards=rewards.get(str(place), []), me=int(row['user_id']) == uid))
            place, cents = place_of(db, uid, period['id']) if period else (None, 0)
            me_row = db.execute('SELECT name,username,photo_url FROM users WHERE id=?', (uid,)).fetchone()
            plays = db.execute('SELECT plays FROM lb_turnover WHERE period_id=? AND user_id=?', (period['id'], uid)).fetchone() if period else None
            view = period_view(db, period)
        return jsonify(enabled=True, period=view, rewards=rewards, top=top,
                       me=dict(user_id=uid, place=place, turnover=cents / 100, plays=int(plays['plays']) if plays else 0,
                               name=('@'+me_row['username'] if me_row and me_row['username'] else (me_row['name'] if me_row else '')) or 'Игрок', photo_url=(me_row['photo_url'] if me_row else '') or '',
                               rewards=rewards.get(str(place), []) if place else []))

    # ---------------------------------------------------------------- admin
    @app.get('/api/admin/leaderboard')
    @admin
    def admin_leaderboard():
        cfg = config()
        with connect() as db:
            period = active_period(db)
            view = period_view(db, period)
            rows = top_rows(db, period['id'], 10) if period else []
            names = {int(r['id']): r for r in db.execute('SELECT id,name,username FROM users WHERE id IN (%s)' % (','.join('?' for _ in rows) or '0'),
                                                       [int(x['user_id']) for x in rows]).fetchall()} if rows else {}
            top = [dict(place=i, user_id=int(r['user_id']), name=(names[int(r['user_id'])]['name'] if int(r['user_id']) in names else ''),
                        username=(names[int(r['user_id'])]['username'] if int(r['user_id']) in names else ''),
                        turnover=int(r['cents']) / 100) for i, r in enumerate(rows, 1)]
            history = [dict(id=int(r['id']), title=season_title(db, r), starts_at=r['starts_at'], ends_at=r['ends_at'], finished_at=r['finished_at'], reason=r['finish_reason'])
                       for r in db.execute("SELECT * FROM lb_periods WHERE status='finished' ORDER BY id DESC LIMIT 8").fetchall()]
            awards = [dict(period_id=int(r['period_id']), place=int(r['place']), user_id=int(r['user_id']), gift_name=r['gift_name'],
                           image_url=r['image_url'], price_ton=int(r['floor_price'] or 0) / 100, created_at=r['created_at'])
                      for r in db.execute('SELECT * FROM lb_awards ORDER BY id DESC LIMIT 30').fetchall()]
        return jsonify(config=dict(enabled=cfg['enabled'], period_hours=cfg['period_hours'], auto_repeat=cfg['auto_repeat']), medals=cfg['medals'], medal_names=MEDALS,
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

    @app.post('/api/admin/leaderboard/medals')
    @admin
    def admin_leaderboard_medals():
        data = request.get_json(silent=True) or {}
        try:
            place = int(data.get('place'))
        except (TypeError, ValueError):
            return error('Выберите место.')
        if not 1 <= place <= MAX_PLACES:
            return error(f'Места: от 1 до {MAX_PLACES}.')
        medal = str(data.get('medal') or '')
        if medal and medal not in MEDALS:
            return error('Такой медали нет.')
        cfg = config()
        if medal:
            cfg['medals'][str(place)] = medal
        else:
            cfg['medals'].pop(str(place), None)
        save_config(cfg)
        return jsonify(ok=True, medals=cfg['medals'])

    @app.post('/api/admin/medals/grant')
    @admin
    def admin_medal_grant():
        data = request.get_json(silent=True) or {}
        try:
            uid = int(data.get('user_id'))
        except (TypeError, ValueError):
            return error('Укажите ID игрока.')
        medal = str(data.get('medal') or '')
        if medal not in MEDALS:
            return error('Выберите медаль.')
        title = str(data.get('title') or '').strip()[:60] or 'Особая награда'
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                return error('Игрок не найден.', 404)
            shown = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE user_id=? AND shown=1', (uid,)).fetchone()['n']
            cur = db.execute('INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown) VALUES(?,?,?,?,?,?)',
                             (uid, medal, title, 0, 0, 1 if shown < MAX_SHOWN_MEDALS else 0))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)', (session['uid'], uid, 'medal_grant', medal))
            db.commit()
            notify = _ns.get('notify_user_async')
            if notify and data.get('notify', True):
                try:
                    notify(uid, '🏅 <b>Вам выдана медаль</b>\n«%s» — %s\nОна уже в профиле, раздел «Мои медали».' % (_ns['escape'](MEDALS[medal]), _ns['escape'](title)), None, 'HTML', db=db)
                except Exception:
                    _ns['app'].logger.exception('Medal notification failed')
        return jsonify(ok=True, id=int(cur.lastrowid))

    @app.get('/api/admin/medals/user/<int:uid>')
    @admin
    def admin_medals_of_user(uid):
        with connect() as db:
            u = db.execute('SELECT id,name,username FROM users WHERE id=?', (uid,)).fetchone()
            if not u:
                return error('Игрок не найден.', 404)
            rows = db.execute('SELECT id,medal_key,title,place,created_at FROM user_medals WHERE user_id=? ORDER BY id DESC', (uid,)).fetchall()
        return jsonify(ok=True, user=dict(id=int(u['id']), name=u['name'] or '', username=u['username'] or ''),
                       medals=[dict(id=int(r['id']), key=r['medal_key'], name=MEDALS.get(r['medal_key'], 'Медаль'), title=r['title'],
                                    place=int(r['place']), created_at=str(r['created_at'] or '')) for r in rows])

    @app.post('/api/admin/medals/<int:medal_id>/revoke')
    @admin
    def admin_medal_revoke(medal_id):
        with connect() as db:
            row = db.execute('SELECT user_id,medal_key FROM user_medals WHERE id=?', (medal_id,)).fetchone()
            if not row:
                return error('Медаль не найдена.', 404)
            db.execute('DELETE FROM user_medals WHERE id=?', (medal_id,))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)', (session['uid'], int(row['user_id']), 'medal_revoke', row['medal_key']))
        return jsonify(ok=True)

    @app.get('/api/medals')
    @login
    def my_medals():
        with connect() as db:
            rows = db.execute('SELECT id,medal_key,title,place,shown,created_at FROM user_medals WHERE user_id=? ORDER BY id DESC', (int(session['uid']),)).fetchall()
        return jsonify(ok=True, max_shown=MAX_SHOWN_MEDALS, medals=[dict(id=int(r['id']), key=r['medal_key'], name=MEDALS.get(r['medal_key'], 'Медаль'),
                       title=r['title'], place=int(r['place']), shown=bool(r['shown']), created_at=str(r['created_at'] or '')) for r in rows])

    @app.post('/api/medals/<int:medal_id>/shown')
    @login
    def medal_shown(medal_id):
        want = bool((request.get_json(silent=True) or {}).get('shown'))
        uid = int(session['uid'])
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM user_medals WHERE id=? AND user_id=?', (medal_id, uid)).fetchone()
            if not row:
                return error('Медаль не найдена.', 404)
            if want:
                n = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE user_id=? AND shown=1 AND id<>?', (uid, medal_id)).fetchone()['n']
                if n >= MAX_SHOWN_MEDALS:
                    return error(f'В профиле можно показать не больше {MAX_SHOWN_MEDALS} медалей.', 409)
            db.execute('UPDATE user_medals SET shown=? WHERE id=?', (1 if want else 0, medal_id))
        return jsonify(ok=True, shown=want)

    @app.post('/api/admin/leaderboard/start')
    @admin
    def admin_leaderboard_start():
        data = request.get_json(silent=True) or {}
        cfg = config()
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if active_period(db):
                return error('Сезон уже идёт. Завершите его, чтобы начать новый.', 409)
            start = now_utc()
            if data.get('ends_at'):
                end = parse(data['ends_at'])
            else:
                try:
                    end = start + timedelta(hours=float(data.get('hours') or cfg['period_hours']))
                except (TypeError, ValueError):
                    return error('Проверьте длительность.')
            if not end or end <= start:
                return error('Окончание должно быть в будущем.')
            if end - start > timedelta(days=90):
                return error('Сезон не может быть длиннее 90 дней.')
            db.execute("INSERT INTO lb_periods(starts_at,ends_at,status,title) VALUES(?,?,'active',?)", (iso(start), iso(end), str(data.get('title') or '').strip()[:40]))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'leaderboard_start', iso(end)))
        _cache.update(id=0, ends=0.0, at=0.0)
        return jsonify(ok=True)

    @app.post('/api/admin/leaderboard/period')
    @admin
    def admin_leaderboard_period():
        data = request.get_json(silent=True) or {}
        with connect() as db:
            period = active_period(db)
            if not period:
                return error('Таблица лидеров выключена.', 409)
            if 'title' in data:
                db.execute('UPDATE lb_periods SET title=? WHERE id=?', (str(data.get('title') or '').strip()[:40], period['id']))
                if not data.get('ends_at') and not data.get('extend_hours'):
                    _cache.update(id=0, ends=0.0, at=0.0)
                    return jsonify(ok=True)
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

    @app.post('/api/admin/leaderboard/reset-standings')
    @admin
    def admin_leaderboard_reset_standings():
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            period = active_period(db)
            if not period:
                return error('Сезон не запущен.', 409)
            removed = db.execute('SELECT COUNT(*) AS n FROM lb_turnover WHERE period_id=?', (period['id'],)).fetchone()['n']
            db.execute('DELETE FROM lb_turnover WHERE period_id=?', (period['id'],))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], session['uid'], 'leaderboard_reset_standings', 'removed %s' % removed))
        _cache.update(id=0, ends=0.0, at=0.0)
        return jsonify(ok=True, removed=int(removed))

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
                    settle_due(db)
            except Exception:
                app.logger.exception('Leaderboard loop failed')
            time.sleep(30)

    return loop


def shown_medals(db, user_id):
    """Medals the player chose to show on the public profile (newest first)."""
    rows = db.execute('SELECT id,medal_key,title,place FROM user_medals WHERE user_id=? AND shown=1 ORDER BY id DESC LIMIT ?',
                      (int(user_id), MAX_SHOWN_MEDALS)).fetchall()
    return [dict(key=r['medal_key'], name=MEDALS.get(r['medal_key'], 'Медаль'), title=r['title'], place=int(r['place'])) for r in rows]
