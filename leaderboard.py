"""Weekly turnover leaderboard: ranking, per-place prizes, settlement.

Turnover is fed from app.increase_turnover (the single place every game goes through), so the
board needs no per-game queries. A period is settled lazily (first request or game after it ends),
by a background loop, or early by an admin. Prizes land in the winner's inventory.
"""
import os
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
MAX_SHOWN_MEDALS = 3
BASE_MEDALS = dict(MEDALS)
DEFAULT_EVENTS = {k: 'Хэллоуин' for k in ('hw1', 'hw2', 'hw3', 'hw4', 'hw5')}
DEFAULT_EVENTS.update({k: 'Коллекция авторов' for k in ('goshan', 'kess', 'nickolay')})
DEFAULT_EVENTS['kuzmatagin'] = 'Амбассадоры'
DEFAULT_EVENTS.update({k: 'Музыка' for k in ('kashtan', 'jealous', 'warhol', 'poets', 'vklube')})   # event collections: medals tied to a seasonal event
MEDAL_DOC, MEDAL_SET_DOC, PROMO_MEDAL_DOC = 'custom_medals', 'medal_settings', 'promo_medals'
RARITIES = {'common': 'Обычная', 'rare': 'Редкая', 'epic': 'Эпическая', 'legendary': 'Легендарная'}
DEFAULT_RARITY = {'hw1': 'epic', 'hw2': 'rare', 'hw3': 'rare', 'hw4': 'rare', 'hw5': 'rare', 'vip': 'epic', 'gem': 'epic', 'champ': 'epic'}
SEED_MEDALS = [('kashtan', 'Миша Каштан', 'kashtan.jpg', 'gold'), ('jealous', 'Jealous', 'jealous.jpg', 'black'), ('goshan', 'Goshan WOW', 'goshan.jpg', 'gold'), ('kess', 'Kess', 'kess.jpg', 'silver'), ('nickolay', 'NickolayGifts', 'nickolay.jpg', 'blue'), ('kuzmatagin', 'KUZMATAGIN', 'kuzmatagin.jpg', 'bronze'), ('warhol', 'Andy Warhol', 'warhol.jpg', 'blue'), ('poets', 'Общество мёртвых поэтов', 'poets.jpg', 'red'), ('vklube', 'В клубе', 'vklube.jpg', 'black')]
SEED_RARITY = {'kess': 'epic', 'nickolay': 'epic', 'kuzmatagin': 'rare', 'poets': 'epic'}
DEFAULT_NOTIFY = '🏅 <b>Вам выдана медаль</b>\n«{medal}» — {title}\nОна уже в профиле, раздел «Мои медали».'
DEFAULT_SEASON_NOTE = '🏅 Медаль «{medal}» — в профиле, раздел «Мои медали».'


def custom_medals():
    doc = dict(_ns['read_document'](MEDAL_DOC) or {})
    items = doc.get('items')
    if not isinstance(items, dict):
        items = {}
    done = list(doc.get('seeded_keys') or ([] if not doc.get('seeded') else ['kashtan']))
    todo = [d for d in SEED_MEDALS if d[0] not in done]
    if todo:
        try:
            import base64
            for key, name, fname, pal in todo:
                path = os.path.join(_ns['app'].root_path, 'static', 'img', 'medals', fname)
                if key not in items and os.path.exists(path):
                    with open(path, 'rb') as fh:
                        items[key] = dict(name=name, manual_only=True, pal=pal, rarity=SEED_RARITY.get(key, 'legendary'), v=1,
                                          image='data:image/jpeg;base64,' + base64.b64encode(fh.read()).decode())
                done.append(key)
            _ns['save_document'](MEDAL_DOC, dict(items=items, seeded=True, seeded_keys=done))
        except Exception:
            _ns['app'].logger.exception('Medal seed failed')
    return items


PAL_KEYS = ('gold', 'silver', 'bronze', 'violet', 'teal', 'cyan', 'blue', 'green', 'red', 'pink', 'orange', 'night', 'obsidian', 'black')


def promo_medals():
    doc = dict(_ns['read_document'](PROMO_MEDAL_DOC) or {})
    return dict(doc.get('codes') or {}) if isinstance(doc.get('codes'), dict) else {}


def medal_settings():
    doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
    if not (_ns['read_document']('migr_hide_kashtan') or {}).get('done'):   # one-time: hide the Kashtan medal for now (admin can unhide it later)
        h0 = [x for x in (doc.get('hidden') or []) if x != 'kashtan'] + ['kashtan']
        doc['hidden'] = h0
        _ns['save_document'](MEDAL_SET_DOC, doc)
        _ns['save_document']('migr_hide_kashtan', {'done': True})
    names = doc.get('names') if isinstance(doc.get('names'), dict) else {}
    hidden = doc.get('hidden') if isinstance(doc.get('hidden'), list) else []
    rar = doc.get('rarity') if isinstance(doc.get('rarity'), dict) else {}
    ev = doc.get('events') if isinstance(doc.get('events'), dict) else {}
    cols = doc.get('collections') if isinstance(doc.get('collections'), dict) else {}
    clean = {}
    for scope, c in cols.items():
        if not isinstance(c, dict):
            continue
        try:
            clean[str(scope)[:40]] = dict(percent=max(1, min(100, int(c.get('percent') or 100))), tickets=max(0, int(c.get('tickets') or 0)),
                                          ton=max(0.0, min(1000000.0, float(c.get('ton') or 0))), medal=str(c.get('medal') or ''),
                                          gift=c.get('gift') if isinstance(c.get('gift'), dict) and c['gift'].get('name') else None)
        except (TypeError, ValueError):
            continue
    return dict(names={str(k): str(v)[:40] for k, v in names.items() if v}, hidden=[str(x) for x in hidden], rarity={str(k): v for k, v in rar.items() if v in RARITIES},
                events={str(k): str(v)[:30] for k, v in ev.items()}, collections=clean,
                notify=str(doc.get('notify') or DEFAULT_NOTIFY)[:600], season=str(doc.get('season') or DEFAULT_SEASON_NOTE)[:300])


def all_names():
    out = dict(BASE_MEDALS)
    for k, v in custom_medals().items():
        out[k] = str(v.get('name') or 'Медаль')
    out.update({k: v for k, v in medal_settings()['names'].items() if k in out})
    return out


def rarity_map():
    out = {k: DEFAULT_RARITY.get(k, 'common') for k in BASE_MEDALS}
    for k, v in custom_medals().items():
        out[k] = v.get('rarity') if v.get('rarity') in RARITIES else 'common'
    out.update({k: v for k, v in medal_settings()['rarity'].items() if k in out})
    return out


def event_map():
    out = dict(DEFAULT_EVENTS)
    for k, v in custom_medals().items():
        if v.get('event'):
            out[k] = str(v['event'])[:30]
    for k, v in medal_settings()['events'].items():
        if v:
            out[k] = v
        else:
            out.pop(k, None)
    return {k: v for k, v in out.items() if k in all_names()}


def collection_scopes():
    """{scope: [medal keys]} — '*' is everything visible, every event name is its own collection."""
    hidden = set(medal_settings()['hidden'])
    keys = [k for k in all_names() if k not in hidden]
    out = {'*': keys}
    for k, ev in event_map().items():
        if k in keys:
            out.setdefault(ev, []).append(k)
    return out


def collections_state(db, uid):
    own = {r['medal_key'] for r in db.execute('SELECT DISTINCT medal_key FROM user_medals WHERE user_id=?', (int(uid),)).fetchall()}
    done = {r['scope'] for r in db.execute('SELECT scope FROM medal_collection_claims WHERE user_id=?', (int(uid),)).fetchall()}
    cfg = medal_settings()['collections']
    out = []
    for scope, keys in collection_scopes().items():
        c = cfg.get(scope)
        has_reward = bool(c and (c['tickets'] or c['ton'] or c['medal'] or c.get('gift')))
        got = sum(1 for k in keys if k in own)
        pct = c['percent'] if c else 100
        out.append(dict(scope=scope, label='Все медали' if scope == '*' else scope, total=len(keys), owned=got, percent=pct,
                        need=-(-len(keys) * pct // 100), reward=(dict(tickets=c['tickets'], ton=c['ton'], medal=c['medal'], medal_name=mname(c['medal']) if c['medal'] else '', gift=prize_view(c['gift']) if c.get('gift') else None) if has_reward else None),
                        claimed=scope in done, ready=bool(has_reward and keys and got * 100 >= pct * len(keys) and scope not in done)))
    return out


def mname(key):
    return all_names().get(key, 'Медаль')


def render_note(template, **values):
    esc = _ns['escape']
    text = esc(template)
    for tag in ('b', 'i'):
        text = text.replace('&lt;%s&gt;' % tag, '<%s>' % tag).replace('&lt;/%s&gt;' % tag, '</%s>' % tag)
    for k, v in values.items():
        text = text.replace('{%s}' % k, esc(str(v)))
    return text

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
CREATE TABLE IF NOT EXISTS medal_collection_claims (
    user_id INTEGER NOT NULL,scope TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id,scope)
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
        if 1 <= place <= MAX_PLACES and medal in all_names():
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
                n_on = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE user_id=? AND shown=1', (int(row['user_id']),)).fetchone()['n']
                db.execute('INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown,claimed) VALUES(?,?,?,?,?,?,0)',
                           (int(row['user_id']), medal, title, place, period_id, 1 if n_on < MAX_SHOWN_MEDALS else 0))
                medal_notes[int(row['user_id'])] = (place, mname(medal))
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
                tail = (tail + '\n' if tail else '') + render_note(medal_settings()['season'], medal=medal_notes[uid][1])
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
    try:
        with connect() as db:
            if ns['DATABASE_URL']:
                db.execute('ALTER TABLE user_medals ADD COLUMN IF NOT EXISTS claimed INTEGER NOT NULL DEFAULT 1')
            elif 'claimed' not in {r['name'] for r in db.execute('PRAGMA table_info(user_medals)').fetchall()}:
                db.execute('ALTER TABLE user_medals ADD COLUMN claimed INTEGER NOT NULL DEFAULT 1')   # medals issued before this stay claimed
    except Exception:
        app.logger.exception('user_medals.claimed migration failed')
    try:
        with connect() as db:
            if ns['DATABASE_URL']:
                db.execute('ALTER TABLE user_medals ADD COLUMN IF NOT EXISTS main INTEGER NOT NULL DEFAULT 0')
            elif 'main' not in {r['name'] for r in db.execute('PRAGMA table_info(user_medals)').fetchall()}:
                db.execute('ALTER TABLE user_medals ADD COLUMN main INTEGER NOT NULL DEFAULT 0')
    except Exception:
        app.logger.exception('user_medals.main migration failed')
    try:   # the profile now holds up to 3 medals: keep the newest three of those already shown
        with connect() as db:
            seen = {}
            for r in db.execute('SELECT id,user_id FROM user_medals WHERE shown=1 ORDER BY id DESC').fetchall():
                seen[r['user_id']] = seen.get(r['user_id'], 0) + 1
                if seen[r['user_id']] > MAX_SHOWN_MEDALS:
                    db.execute('UPDATE user_medals SET shown=0 WHERE id=?', (int(r['id']),))
    except Exception:
        app.logger.exception('user_medals trim failed')

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

    @app.get('/api/leaderboard/medals')
    @login
    def leaderboard_medals():
        """Medal collectors ranking + author releases ("N of M left")."""
        uid = int(session['uid'])
        pts = {'common': 1, 'rare': 2, 'epic': 3, 'legendary': 5}
        rar = rarity_map()
        names = all_names()
        with connect() as db:
            rows = db.execute('SELECT user_id,medal_key FROM user_medals').fetchall()
            per = {}
            for r in rows:
                if r['medal_key'] in names:
                    per.setdefault(int(r['user_id']), set()).add(r['medal_key'])
            board = sorted(((u, len(ks), sum(pts.get(rar.get(k, 'common'), 1) for k in ks)) for u, ks in per.items()), key=lambda x: (-x[1], -x[2], x[0]))
            top_rows_ = board[:20]
            ids = [u for u, _, _ in top_rows_]
            users = {}
            if ids:
                for r in db.execute('SELECT id,name,username,photo_url FROM users WHERE id IN (%s)' % ','.join('?' for _ in ids), ids).fetchall():
                    users[int(r['id'])] = r
            top = []
            for place, (u, n, sc) in enumerate(top_rows_, 1):
                r = users.get(u)
                top.append(dict(place=place, user_id=u, name=public_handle(r['username'] if r else '', r['name'] if r else ''),
                                photo_url=(r['photo_url'] if r else '') or '', count=n, score=sc, me=u == uid))
            mine = next(((i, n, sc) for i, (u, n, sc) in enumerate(board, 1) if u == uid), None)
            total_medals = len(names)
            # author releases
            releases = []
            for row in db.execute("SELECT name,payload FROM app_documents WHERE name LIKE 'creator:%'").fetchall():
                try:
                    author = int(str(row['name']).split(':', 1)[1])
                    rec = json.loads(row['payload'] or '{}')
                except (ValueError, TypeError, IndexError):
                    continue
                for c in (rec.get('medal_codes') or [])[-20:]:
                    if not c.get('release') or c.get('medal') not in names:
                        continue
                    code = c.get('code')
                    pr = db.execute('SELECT max_uses,uses_count FROM promo_codes WHERE code=?', (code,)).fetchone()
                    if not pr:
                        continue
                    left_total = int(pr['max_uses'] or 0)
                    used = int(pr['uses_count'] or 0)
                    fb = db.execute('SELECT active,max_uses,uses_count FROM freebets WHERE code=?', (code,)).fetchone()
                    if fb:
                        if not fb['active']:
                            continue
                        left_total, used = int(fb['max_uses'] or 0), int(fb['uses_count'] or 0)
                    if left_total <= 0:
                        continue
                    au = db.execute('SELECT name,username,photo_url FROM users WHERE id=?', (author,)).fetchone()
                    releases.append(dict(medal=c['medal'], medal_name=names[c['medal']], rarity=rar.get(c['medal'], 'common'),
                                         author=(('@' + au['username']) if au and au['username'] else (au['name'] if au else 'Автор')),
                                         total=left_total, used=used, left=max(0, left_total - used), at=c.get('at') or ''))
            releases = sorted(releases, key=lambda x: x['at'], reverse=True)[:6]
        return jsonify(ok=True, top=top, total_medals=total_medals, releases=releases,
                       me=dict(place=mine[0] if mine else None, count=mine[1] if mine else 0, score=mine[2] if mine else 0))

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
        return jsonify(config=dict(enabled=cfg['enabled'], period_hours=cfg['period_hours'], auto_repeat=cfg['auto_repeat']), medals=cfg['medals'], medal_names=all_names(),
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
        if medal and (medal not in all_names() or medal in medal_settings()['hidden'] or custom_medals().get(medal, {}).get('manual_only')):
            return error('Такой медали нет или она только для ручной выдачи.')
        cfg = config()
        if medal:
            cfg['medals'][str(place)] = medal
        else:
            cfg['medals'].pop(str(place), None)
        save_config(cfg)
        return jsonify(ok=True, medals=cfg['medals'])

    @app.get('/api/music/list')
    def music_list():
        import os
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'music')
        keys = {}
        try:
            for f in os.listdir(d):
                if f.lower().endswith('.mp3'):
                    base = f[:-4]
                    short = base.endswith('_short')
                    k = base[:-6] if short else base
                    keys.setdefault(k, {'long': False, 'short': False})['short' if short else 'long'] = True
        except OSError:
            pass
        names = all_names()
        return jsonify(ok=True, tracks=[dict(key=k, name=names[k], long=v['long'], short=v['short']) for k, v in keys.items() if k in names])

    @app.get('/api/medals/catalog')
    def medal_catalog():
        items = custom_medals()
        return jsonify(ok=True, names=all_names(), hidden=medal_settings()['hidden'], rarity=rarity_map(), rarities=RARITIES, events=event_map(),
                       custom=[dict(key=k, name=mname(k), hidden=k in medal_settings()['hidden'], manual_only=bool(v.get('manual_only')), pal=v.get('pal') or 'gold',
                                    img='/api/medals/img/%s?v=%d' % (k, int(v.get('v') or 0))) for k, v in items.items()])

    @app.get('/api/medals/img/<key>')
    def medal_img(key):
        import base64
        from flask import Response
        item = custom_medals().get(key)
        if not item or not item.get('image'):
            return error('Нет изображения.', 404)
        head, _, b64 = str(item['image']).partition(',')
        mime = 'image/png' if 'png' in head else 'image/webp' if 'webp' in head else 'image/jpeg'
        resp = Response(base64.b64decode(b64), mimetype=mime)
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp

    @app.get('/api/admin/medals/all')
    @admin
    def admin_medals_all():
        cust = custom_medals()
        st = medal_settings()
        rar = rarity_map()
        out = [dict(key=k, name=n, builtin=True, manual_only=False) for k, n in BASE_MEDALS.items()]
        for o in out:
            o['name'] = st['names'].get(o['key'], o['name'])
        out += [dict(key=k, name=mname(k), builtin=False, manual_only=bool(v.get('manual_only'))) for k, v in cust.items()]
        for o in out:
            o['hidden'] = o['key'] in st['hidden']
            o['rarity'] = rar.get(o['key'], 'common')
            o['pal'] = (cust.get(o['key']) or {}).get('pal', '')
            o['event'] = event_map().get(o['key'], '')
        cs = medal_settings()['collections']
        return jsonify(ok=True, medals=out, scopes=[dict(scope=sc, label='Все медали' if sc == '*' else sc, total=len(ks), cfg=dict(cs.get(sc) or dict(percent=100, tickets=0, ton=0, medal=''), gift=prize_view(cs[sc]['gift']) if cs.get(sc) and cs[sc].get('gift') else None)) for sc, ks in collection_scopes().items()], notify=st['notify'], season=st['season'], rarities=RARITIES,
                       codes=[dict(code=c, medal=v.get('medal'), title=v.get('title', '')) for c, v in promo_medals().items()],
                       defaults=dict(notify=DEFAULT_NOTIFY, season=DEFAULT_SEASON_NOTE))

    @app.post('/api/admin/medals/rename')
    @admin
    def admin_medal_rename():
        data = request.get_json(silent=True) or {}
        key, name = str(data.get('key') or ''), str(data.get('name') or '').strip()[:40]
        if key not in all_names() or not name:
            return error('Укажите медаль и название.')
        doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
        names = dict(doc.get('names') or {})
        names[key] = name
        doc['names'] = names
        _ns['save_document'](MEDAL_SET_DOC, doc)
        return jsonify(ok=True)

    @app.post('/api/admin/medals/hide')
    @admin
    def admin_medal_hide():
        data = request.get_json(silent=True) or {}
        key = str(data.get('key') or '')
        if key not in all_names():
            return error('Медали нет.')
        doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
        hidden = [x for x in (doc.get('hidden') or []) if x != key]
        if data.get('hidden', True):
            hidden.append(key)
        doc['hidden'] = hidden
        _ns['save_document'](MEDAL_SET_DOC, doc)
        return jsonify(ok=True)

    @app.post('/api/admin/medals/rarity')
    @admin
    def admin_medal_rarity():
        data = request.get_json(silent=True) or {}
        key, rar = str(data.get('key') or ''), str(data.get('rarity') or '')
        if key not in all_names() or rar not in RARITIES:
            return error('Укажите медаль и редкость.')
        doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
        r = dict(doc.get('rarity') or {})
        r[key] = rar
        doc['rarity'] = r
        _ns['save_document'](MEDAL_SET_DOC, doc)
        items = custom_medals()
        if key in items and data.get('pal') in PAL_KEYS:
            items[key]['pal'] = data['pal']
            _ns['save_document'](MEDAL_DOC, dict(items=items, seeded=True, seeded_keys=list((_ns['read_document'](MEDAL_DOC) or {}).get('seeded_keys') or [])))
        return jsonify(ok=True)

    @app.post('/api/admin/medals/codes')
    @admin
    def admin_medal_codes():
        data = request.get_json(silent=True) or {}
        code = str(data.get('code') or '').strip().upper()
        if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
            return error('Промокод: 3–32 символа, латиница и цифры.')
        codes = promo_medals()
        if data.get('remove'):
            codes.pop(code, None)
        else:
            medal = str(data.get('medal') or '')
            if medal not in all_names():
                return error('Выберите медаль.')
            codes[code] = dict(medal=medal, title=str(data.get('title') or '').strip()[:60])
        _ns['save_document'](PROMO_MEDAL_DOC, dict(codes=codes))
        return jsonify(ok=True)

    @app.after_request
    def grant_promo_medal(resp):
        try:
            if request.method == 'POST' and request.path == '/api/promocodes/redeem' and resp.status_code == 200 and session.get('uid'):
                code = str((request.get_json(silent=True) or {}).get('code') or '').strip().upper()
                hit = promo_medals().get(code)
                if hit and hit.get('medal') in all_names():
                    title = hit.get('title') or ('Промокод ' + code)
                    uid = int(session['uid'])
                    with connect() as db:
                        have = db.execute('SELECT 1 FROM user_medals WHERE user_id=? AND medal_key=? AND title=?', (uid, hit['medal'], title)).fetchone()
                        if not have:
                            n = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE user_id=? AND shown=1', (uid,)).fetchone()['n']
                            db.execute('INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown,claimed) VALUES(?,?,?,0,0,?,0)',
                                       (uid, hit['medal'], title, 1 if n < MAX_SHOWN_MEDALS else 0))
                            notify = _ns.get('notify_user_async')
                            if notify:
                                notify(uid, render_note(medal_settings()['notify'], medal=mname(hit['medal']), title=title), None, 'HTML', db=db)
        except Exception:
            app.logger.exception('Promo medal grant failed')
        return resp

    @app.post('/api/admin/medals/texts')
    @admin
    def admin_medal_texts():
        data = request.get_json(silent=True) or {}
        doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
        for k in ('notify', 'season'):
            if k in data:
                doc[k] = str(data.get(k) or '').strip()[:600]
        _ns['save_document'](MEDAL_SET_DOC, doc)
        return jsonify(ok=True)

    @app.post('/api/admin/medals/create')
    @admin
    def admin_medal_create():
        import base64, io, re as _re, time as _t
        from PIL import Image
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()[:40]
        raw = str(data.get('image') or '')
        if not name:
            return error('Введите название медали.')
        m_ = _re.match(r'^data:image/(png|jpeg|jpg|webp);base64,(.+)$', raw, _re.S)
        if not m_ or len(raw) > 3_000_000:
            return error('Загрузите PNG, JPG или WEBP до 2 МБ.')
        try:
            im = Image.open(io.BytesIO(base64.b64decode(m_.group(2))))
            im.load()
            im = im.convert('RGBA')
            im.thumbnail((384, 384))
            buf = io.BytesIO()
            im.save(buf, 'PNG', optimize=True)
        except Exception:
            return error('Не удалось прочитать изображение.')
        items = custom_medals()
        n = 1
        while 'u%d' % n in items or 'u%d' % n in BASE_MEDALS:
            n += 1
        key = 'u%d' % n
        items[key] = dict(name=name, manual_only=bool(data.get('manual_only', True)), pal=data.get('pal') if data.get('pal') in PAL_KEYS else 'gold', rarity=data.get('rarity') if data.get('rarity') in RARITIES else 'common', event=str(data.get('event') or '').strip()[:30], v=int(_t.time()),
                          image='data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode())
        _ns['save_document'](MEDAL_DOC, dict(items=items, seeded=True, seeded_keys=list((_ns['read_document'](MEDAL_DOC) or {}).get('seeded_keys') or [])))
        return jsonify(ok=True, key=key)

    @app.post('/api/admin/medals/<key>/delete')
    @admin
    def admin_medal_delete(key):
        items = custom_medals()
        if key not in items:
            return error('Можно удалять только загруженные медали.', 404)
        with connect() as db:
            held = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE medal_key=?', (key,)).fetchone()['n']
        if held:
            return error('Медаль есть у игроков (%d). Сначала заберите её.' % held, 409)
        items.pop(key)
        _ns['save_document'](MEDAL_DOC, dict(items=items, seeded=True, seeded_keys=list((_ns['read_document'](MEDAL_DOC) or {}).get('seeded_keys') or [])))
        return jsonify(ok=True)

    @app.post('/api/admin/medals/grant')
    @admin
    def admin_medal_grant():
        data = request.get_json(silent=True) or {}
        try:
            uid = int(data.get('user_id'))
        except (TypeError, ValueError):
            return error('Укажите ID игрока.')
        medal = str(data.get('medal') or '')
        if medal not in all_names() or medal in medal_settings()['hidden']:
            return error('Выберите медаль (скрытые выдавать нельзя).')
        title = str(data.get('title') or '').strip()[:60] or 'Особая награда'
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                return error('Игрок не найден.', 404)
            shown = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE user_id=? AND shown=1', (uid,)).fetchone()['n']
            cur = db.execute('INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown,claimed) VALUES(?,?,?,?,?,?,0)',
                             (uid, medal, title, 0, 0, 1 if shown < MAX_SHOWN_MEDALS else 0))
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)', (session['uid'], uid, 'medal_grant', medal))
            db.commit()
            notify = _ns.get('notify_user_async')
            if notify and data.get('notify', True):
                try:
                    notify(uid, render_note(medal_settings()['notify'], medal=mname(medal), title=title), None, 'HTML', db=db)
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
                       medals=[dict(id=int(r['id']), key=r['medal_key'], name=mname(r['medal_key']), title=r['title'],
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

    @app.get('/api/medals/pending')
    @login
    def medals_pending():
        with connect() as db:
            rows = db.execute('SELECT id,medal_key,title,place FROM user_medals WHERE user_id=? AND claimed=0 ORDER BY id LIMIT 20', (int(session['uid']),)).fetchall()
        return jsonify(ok=True, medals=[dict(id=int(r['id']), key=r['medal_key'], name=mname(r['medal_key']), rarity=rarity_map().get(r['medal_key'], 'common'), title=r['title'], place=int(r['place'])) for r in rows])

    @app.post('/api/medals/<int:medal_id>/claim')
    @login
    def medal_claim(medal_id):
        with connect() as db:
            db.execute('UPDATE user_medals SET claimed=1 WHERE id=? AND user_id=?', (medal_id, int(session['uid'])))
        return jsonify(ok=True)

    @app.get('/api/medals/info/<key>')
    def medal_info(key):
        names = all_names()
        if key not in names:
            return error('Такой медали нет.', 404)
        cm = custom_medals().get(key) or {}
        ev = event_map().get(key, '')
        placed = sorted(int(p) for p, k in config()['medals'].items() if k == key)
        promo = any(v.get('medal') == key if isinstance(v, dict) else v == key for v in promo_medals().values())
        with connect() as db:
            total = max(1, int(db.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']))
            owners = int(db.execute('SELECT COUNT(DISTINCT user_id) AS n FROM user_medals WHERE medal_key=?', (key,)).fetchone()['n'])
            mine = False
            if session.get('uid'):
                mine = bool(db.execute('SELECT 1 FROM user_medals WHERE user_id=? AND medal_key=?', (int(session['uid']), key)).fetchone())
        if cm.get('manual_only'):
            status, how = 'Уникальная', 'Особая медаль. Её нельзя получить в событиях или играх.'
        elif placed:
            status, how = 'Сезонная', 'Награда за %s место в сезоне лидерборда.' % ', '.join(str(x) for x in placed)
        elif ev:
            status, how = 'Событийная', 'Выдаётся за участие в событии «%s».' % ev
        elif promo:
            status, how = 'Особая', 'Выдаётся по промокоду или в акциях.'
        else:
            status, how = 'Обычная', 'Награда за достижения и активность на сайте.'
        pct = owners * 100.0 / total
        if request.args.get('hide') and not mine:        # unfound medal: only the collection is revealed
            return jsonify(ok=True, key=key, hidden=True, collection=ev or 'Коллекция медалей')
        return jsonify(ok=True, key=key, name=names[key], rarity=rarity_map().get(key, 'common'), collection=ev or ('Коллекция медалей'),
                       status=status, how=how, owners=owners, percent=round(pct, 2) if pct >= 0.01 or not owners else 0.01, mine=mine)

    @app.get('/api/frames')
    @login
    def my_frames():
        uid = int(session['uid'])
        with connect() as db:
            owned = owned_frames(db, uid)
            cur = frame_of(db, uid)
        return jsonify(ok=True, selected=cur, owned=owned,
                       frames=sorted([dict(key=k, name=mname(k), owned=k in owned) for k in all_names() if k not in medal_settings()['hidden'] or k in owned], key=lambda f: (not f['owned'])))

    @app.post('/api/frames/select')
    @login
    def select_frame():
        key = str((request.get_json(silent=True) or {}).get('key') or '')
        uid = int(session['uid'])
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _frame_table(db)
            if key and key not in owned_frames(db, uid):
                return error('Эта рамка ещё не получена.', 409)
            db.execute('INSERT INTO user_frame(user_id,frame) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET frame=excluded.frame', (uid, key))
        return jsonify(ok=True, selected=key)

    @app.get('/api/medals')
    @login
    def my_medals():
        with connect() as db:
            rows = db.execute('SELECT id,medal_key,title,place,shown,created_at FROM user_medals WHERE user_id=? ORDER BY id DESC', (int(session['uid']),)).fetchall()
            main = main_medals(db, [session['uid']]).get(int(session['uid']), '')
            cols = collections_state(db, session['uid'])
        return jsonify(ok=True, max_shown=MAX_SHOWN_MEDALS, main=main, collections=cols, events=event_map(),
                       medals=[dict(id=int(r['id']), key=r['medal_key'], name=mname(r['medal_key']), rarity=rarity_map().get(r['medal_key'], 'common'),
                       title=r['title'], place=int(r['place']), shown=bool(r['shown']), created_at=str(r['created_at'] or '')) for r in rows])

    BADGE_PATHS = ('/api/game/recent-wins', '/api/upgrade/recent-wins', '/api/hilo/recent-wins', '/api/craft/recent-wins', '/api/leaderboard/', '/api/arena/state',
                   '/api/crash/state', '/api/xhunt/', '/api/transfers/recipient', '/api/rolls', '/api/giveaways', '/api/wheel', '/api/users/')

    @app.after_request
    def attach_main_medals(resp):
        """Put each player's first-slot medal (`medal`) on every user object in the feeds, so it can be drawn next to the nickname."""
        try:
            if request.method != 'GET' or resp.status_code != 200 or not resp.mimetype == 'application/json' or not request.path.startswith(BADGE_PATHS):
                return resp
            data = resp.get_json(silent=True)
            if data is None:
                return resp
            found = []

            def walk(o, depth=0):
                if depth > 7:
                    return
                if isinstance(o, dict):
                    uid = o.get('user_id')
                    if isinstance(uid, int) and ('name' in o or 'username' in o or 'photo_url' in o):
                        found.append((o, uid))
                    elif isinstance(o.get('id'), int) and 'username' in o and ('photo_url' in o or 'name' in o):
                        found.append((o, o['id']))
                    for v in o.values():
                        if isinstance(v, (dict, list)):
                            walk(v, depth + 1)
                elif isinstance(o, list):
                    for v in o[:300]:
                        walk(v, depth + 1)
            walk(data)
            if not found:
                return resp
            with connect() as db:
                mm = main_medals(db, [u for _, u in found])
            if not mm:
                return resp
            for o, u in found:
                if u in mm:
                    o['medal'] = mm[u]
            resp.set_data(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        except Exception:
            _ns['app'].logger.exception('Main medal badge injection failed')
        return resp

    @app.post('/api/medals/main')
    @login
    def medal_make_main():
        key = str((request.get_json(silent=True) or {}).get('key') or '')
        uid = int(session['uid'])
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM user_medals WHERE user_id=? AND medal_key=? AND shown=1 ORDER BY id DESC LIMIT 1', (uid, key)).fetchone()
            if not row:
                return error('Сначала наденьте эту медаль.', 409)
            db.execute('UPDATE user_medals SET main=0 WHERE user_id=?', (uid,))
            db.execute('UPDATE user_medals SET main=1 WHERE id=?', (int(row['id']),))
        return jsonify(ok=True)

    @app.post('/api/medals/collection/claim')
    @login
    def medal_collection_claim():
        scope = str((request.get_json(silent=True) or {}).get('scope') or '*')
        uid = int(session['uid'])
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = next((c for c in collections_state(db, uid) if c['scope'] == scope), None)
            if not state or not state['reward']:
                return error('Для этой коллекции награда не назначена.', 404)
            if state['claimed']:
                return error('Награда за эту коллекцию уже получена.', 409)
            if not state['ready']:
                return error('Соберите больше медалей: нужно %d из %d.' % (state['need'], state['total']), 409)
            db.execute('INSERT INTO medal_collection_claims(user_id,scope) VALUES(?,?)', (uid, scope))
            rw = state['reward']
            state_gift = (medal_settings()['collections'].get(scope) or {}).get('gift')
            label = 'Коллекция «%s»' % state['label']
            if rw['tickets']:
                db.execute('UPDATE users SET tickets=tickets+? WHERE id=?', (rw['tickets'], uid))
                _ns['record_tickets'](db, uid, rw['tickets'], 'medal_collection', 'medals', scope, label)
            if rw['ton']:
                cents = int(round(rw['ton'] * 100))
                db.execute('UPDATE users SET balance=balance+? WHERE id=?', (cents, uid))
                _ns['record_transaction'](db, uid, 'medal_collection', cents, 'medals', scope, label)
            if rw['medal']:
                award_medal(db, uid, rw['medal'], label)
            if state_gift:
                inv_id, _v = award_prize(db, uid, state_gift)
                if inv_id:
                    db.execute("UPDATE inventory SET source_label=? WHERE id=?", (label, inv_id))
            db.commit()
        return jsonify(ok=True, reward=rw)

    @app.post('/api/admin/medals/event')
    @admin
    def admin_medal_event():
        data = request.get_json(silent=True) or {}
        key, ev = str(data.get('key') or ''), str(data.get('event') or '').strip()[:30]
        if key not in all_names():
            return error('Выберите медаль.')
        doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
        events = dict(doc.get('events') or {})
        events[key] = ev            # '' = explicitly not an event medal
        doc['events'] = events
        _ns['save_document'](MEDAL_SET_DOC, doc)
        return jsonify(ok=True)

    @app.post('/api/admin/medals/collection')
    @admin
    def admin_medal_collection():
        data = request.get_json(silent=True) or {}
        scope = str(data.get('scope') or '*')
        if scope != '*' and scope not in collection_scopes():
            return error('Такой коллекции нет.')
        try:
            percent = int(data.get('percent') if data.get('percent') not in (None, '') else 100)
            tickets = int(data.get('tickets') or 0)
            ton = float(str(data.get('ton') or 0).replace(',', '.'))
        except (TypeError, ValueError):
            return error('Проверьте числа.')
        medal = str(data.get('medal') or '')
        if not 1 <= percent <= 100 or tickets < 0 or not 0 <= ton <= 1000000:
            return error('Процент: 1–100, билеты и TON — не отрицательные.')
        if medal and medal not in all_names():
            return error('Такой медали нет.')
        doc = dict(_ns['read_document'](MEDAL_SET_DOC) or {})
        cols = dict(doc.get('collections') or {})
        gid = str(data.get('gift') or '')
        old = (cols.get(scope) or {}).get('gift')
        gift = old if gid == 'keep' else None
        if gid and gid != 'keep':
            try:
                gift = normalise_prize(dict(source_type='catalog', gift_id=gid))
            except (ValueError, OSError) as exc:
                return error(str(exc))
        if not (tickets or ton or medal or gift) and percent == 100:
            cols.pop(scope, None)
        else:
            cols[scope] = dict(percent=percent, tickets=tickets, ton=round(ton, 2), medal=medal, gift=gift)
        doc['collections'] = cols
        _ns['save_document'](MEDAL_SET_DOC, doc)
        return jsonify(ok=True)

    @app.post('/api/medals/equip')
    @login
    def medal_equip():
        body = request.get_json(silent=True) or {}
        key = str(body.get('key') or '')
        want = bool(body.get('on'))
        uid = int(session['uid'])
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,shown FROM user_medals WHERE user_id=? AND medal_key=? ORDER BY id DESC', (uid, key)).fetchall()
            if not rows:
                return error('Эта медаль ещё не получена.', 404)
            if want:
                on = db.execute('SELECT COUNT(DISTINCT medal_key) AS n FROM user_medals WHERE user_id=? AND shown=1 AND medal_key<>?', (uid, key)).fetchone()['n']
                if on >= MAX_SHOWN_MEDALS:
                    return error(f'Можно надеть не больше {MAX_SHOWN_MEDALS} медалей. Снимите другую.', 409)
                keep = int(next((r['id'] for r in rows if r['shown']), rows[0]['id']))
                db.execute('UPDATE user_medals SET shown=0 WHERE user_id=? AND medal_key=? AND id<>?', (uid, key, keep))
                db.execute('UPDATE user_medals SET shown=1 WHERE id=?', (keep,))
            else:
                db.execute('UPDATE user_medals SET shown=0 WHERE user_id=? AND medal_key=?', (uid, key))
        return jsonify(ok=True, on=want)

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


def award_medal(db, user_id, medal, title='Особая награда', notify=True):
    """Give a medal to a player from anywhere (admin, quest, promo code, season). Returns the new row id, or 0 if the key is unknown."""
    medal = str(medal or '')
    if not medal or medal not in all_names():
        return 0
    title = (str(title or '').strip()[:60]) or 'Особая награда'
    on = db.execute('SELECT COUNT(*) AS n FROM user_medals WHERE user_id=? AND shown=1', (int(user_id),)).fetchone()['n']
    cur = db.execute('INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown,claimed) VALUES(?,?,?,?,?,?,0)',
                     (int(user_id), medal, title, 0, 0, 1 if on < MAX_SHOWN_MEDALS else 0))
    if notify:
        fn = _ns.get('notify_user_async')
        if fn:
            try:
                fn(int(user_id), render_note(medal_settings()['notify'], medal=mname(medal), title=title), None, 'HTML', db=db)
            except Exception:
                _ns['app'].logger.exception('Medal notification failed')
    return int(cur.lastrowid)


def main_medals(db, user_ids):
    """{user_id: medal_key} — the medal in each player's first slot, shown next to the nickname everywhere."""
    ids = sorted({int(x) for x in user_ids if x})
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    rows = db.execute('SELECT user_id,medal_key FROM user_medals WHERE shown=1 AND user_id IN (%s) ORDER BY main ASC,id ASC' % marks, tuple(ids)).fetchall()
    out = {}
    for r in rows:   # ascending: the last row written wins -> main=1 first, otherwise the newest
        out[int(r['user_id'])] = r['medal_key']
    return out


FRAME_KEYS = None   # every medal gives its own 3D profile frame (frame key = medal key)


def _frame_table(db):
    db.execute('CREATE TABLE IF NOT EXISTS user_frame (user_id INTEGER PRIMARY KEY, frame TEXT NOT NULL DEFAULT \'\')')


def owned_frames(db, user_id):
    have = {r['medal_key'] for r in db.execute('SELECT DISTINCT medal_key FROM user_medals WHERE user_id=?', (int(user_id),)).fetchall()}
    names = all_names()
    return [k for k in names if k in have]


def frame_of(db, user_id):
    """The frame the player chose (only if they still own the medal that gives it)."""
    try:
        _frame_table(db)
        row = db.execute('SELECT frame FROM user_frame WHERE user_id=?', (int(user_id),)).fetchone()
        k = (row['frame'] if row else '') or ''
        return k if k in owned_frames(db, user_id) else ''
    except Exception:
        return ''


def shown_medals(db, user_id):
    """Medals the player chose to show on the public profile (newest first)."""
    rows = db.execute('SELECT id,medal_key,title,place FROM user_medals WHERE user_id=? AND shown=1 ORDER BY main DESC,id DESC LIMIT ?',
                      (int(user_id), MAX_SHOWN_MEDALS)).fetchall()
    return [dict(key=r['medal_key'], name=mname(r['medal_key']), rarity=rarity_map().get(r['medal_key'], 'common'), title=r['title'], place=int(r['place'])) for r in rows]
