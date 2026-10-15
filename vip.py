"""VIP statuses (turnover tiers) and the «recent wins» feed.

The old numeric levels stay in the database (transfer unlocks etc.) but the player-facing progress is now the
seven VIP tiers below. Tier benefits are display text only: edit VIP_TIERS to change what the window shows.
"""
GAME_OF_KIND = {
    'game_win_ton': 'Mines', 'gift_win': 'Mines',
    'hilo_win': 'Hi-Lo', 'hilo_gift_win': 'Hi-Lo',
    'crash_win': 'Crash', 'crash_gift_win': 'Crash',
    'limbo_win': 'Limbo', 'limbo_gift_win': 'Limbo',
    'road_win': 'Hamster Road', 'road_gift_win': 'Hamster Road',
    'plinko_win': 'Plinko', 'plinko_gift_win': 'Plinko',
    'arena_win': 'Арена', 'arena_gift_win': 'Арена',
}
MIN_FEED_CENTS = 50          # smaller wins are not worth a feed row (gifts always are)
FEED_SIZE = 16
FEED_KEEP = 400

def _tier(key, name, ton, tickets, bonus_ton, gift_min, gift_max, playbonus=False):
    perks = ['NFT-подарок %s–%s TON' % (gift_min, gift_max), '%s билетов' % format(tickets, ',').replace(',', ' '), '%s TON на баланс' % bonus_ton]
    if playbonus:
        perks.append('PLAYBONUS')
    return dict(key=key, name=name, ton=ton, tickets=tickets, bonus_ton=bonus_ton, gift_min=gift_min, gift_max=gift_max,
                playbonus=playbonus, perks=perks)


PLAYBONUS_TIER = 2      # index of the first tier that lets the player pay PLAYBONUS out (Opal)

# Rewards are given automatically the moment a tier is reached: a random catalog gift priced between gift_min and gift_max TON,
# tickets and TON on the main balance. Edit this table to change the economy.
VIP_TIERS = [
    _tier('iron', 'Quartz', 500, 500, 2, 1, 3),
    _tier('bronze', 'Amber', 2500, 1500, 5, 3, 8),
    _tier('silver', 'Opal', 10000, 5000, 15, 8, 20, True),
    _tier('gold', 'Citrine', 30000, 15000, 40, 20, 60, True),
    _tier('platinum', 'Aquamarine', 100000, 50000, 120, 60, 150, True),
    _tier('diamond', 'Amethyst', 300000, 150000, 400, 150, 400, True),
    _tier('obsidian', 'Onyx', 1000000, 500000, 1500, 400, 1000, True),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS vip_claims (
    user_id INTEGER NOT NULL, tier_key TEXT NOT NULL, reward_json TEXT NOT NULL DEFAULT '{}',
    seen INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, tier_key)
);
CREATE TABLE IF NOT EXISTS recent_wins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, game TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0, prize TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_ns = {}
_img = {'at': 0.0, 'map': {}}


def gift_image(name, db=None):
    """Picture of a catalog gift by its name (cached for a minute)."""
    import time
    if time.time() - _img['at'] > 60:
        try:
            doc = _ns['read_document']('portal_catalog') or {}
            _img['map'] = {str(g.get('name') or '').strip().lower(): str(g.get('image_url') or '') for g in (doc.get('gifts') or [])}
        except Exception:
            _img['map'] = {}
        _img['at'] = time.time()
    found = _img['map'].get(str(name or '').strip().lower(), '')
    if not found and db is not None and name:
        try:
            row = db.execute("SELECT image_url FROM inventory WHERE gift_name=? AND image_url<>'' ORDER BY id DESC LIMIT 1", (str(name),)).fetchone()
            found = str(row['image_url'] or '') if row else ''
        except Exception:
            found = ''
    return found


def tier_state(turnover_cents):
    ton = int(turnover_cents or 0) / 100
    idx = -1
    for i, t in enumerate(VIP_TIERS):
        if ton >= t['ton']:
            idx = i
    nxt = VIP_TIERS[idx + 1] if idx + 1 < len(VIP_TIERS) else None
    base = VIP_TIERS[idx]['ton'] if idx >= 0 else 0
    progress = 100.0 if not nxt else max(0.0, min(100.0, (ton - base) * 100 / (nxt['ton'] - base)))
    return dict(index=max(idx, 0), reached=idx >= 0, turnover=round(ton, 2), progress=round(progress, 1),
                next=(dict(name=nxt['name'], ton=nxt['ton'], left=round(nxt['ton'] - ton, 2)) if nxt else None))


def log_win(db, user_id, kind, amount, details=''):
    """Called from record_transaction for every game win; never raises."""
    game = GAME_OF_KIND.get(kind)
    if not game:
        return
    amount = int(amount or 0)
    gift = kind.endswith('gift_win') or kind == 'gift_win'
    if not gift and amount < MIN_FEED_CENTS:
        return
    if gift and amount <= 0 and not details:
        return
    try:
        prize = str(details or '')[:80] if gift else ''
        db.execute('INSERT INTO recent_wins(user_id,game,amount,prize,image) VALUES(?,?,?,?,?)',
                   (int(user_id), game, max(0, amount), prize, gift_image(prize, db) if prize else ''))
    except Exception:
        pass


def pick_gift(db, tier, seed):
    """A random catalog gift priced inside the tier's range (closest one when nothing fits)."""
    import random
    try:
        gifts = [g for g in (_ns['read_document']('portal_catalog') or {}).get('gifts', []) if float(g.get('price_ton') or 0) > 0]
    except Exception:
        gifts = []
    if not gifts:
        return None
    pool = [g for g in gifts if tier['gift_min'] <= float(g['price_ton']) <= tier['gift_max']]
    if not pool:
        mid = (tier['gift_min'] + tier['gift_max']) / 2
        best = min(abs(float(g['price_ton']) - mid) for g in gifts)
        pool = [g for g in gifts if abs(float(g['price_ton']) - mid) == best]
    return random.SystemRandom().choice(pool)


def grant_due(db, user_id, turnover_cents):
    """Gives every reached, not yet rewarded tier (inside the caller's transaction). Returns the list of new rewards."""
    ton = int(turnover_cents or 0) / 100
    reached = [t for t in VIP_TIERS if ton >= t['ton']]
    if not reached:
        return []
    have = {r['tier_key'] for r in db.execute('SELECT tier_key FROM vip_claims WHERE user_id=?', (int(user_id),)).fetchall()}
    if len(have) >= len(reached):
        return []
    if _ns['creator_demo_active'](int(user_id)):
        return []
    out = []
    for i, t in enumerate(VIP_TIERS):
        if t['key'] in have or ton < t['ton']:
            continue
        reward = dict(name=t['name'], tickets=t['tickets'], balance=t['bonus_ton'], gift=None,
                      playbonus_unlocked=bool(i == PLAYBONUS_TIER))
        g = pick_gift(db, t, '%s:%s:%s' % (user_id, t['key'], ton))
        if g:
            cents = int(round(float(g['price_ton']) * 100))
            db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'vip')",
                       (int(user_id), str(g.get('id') or ''), str(g.get('name') or ''), str(g.get('image_url') or ''), cents))
            reward['gift'] = dict(name=str(g.get('name') or ''), image_url=str(g.get('image_url') or ''), price=cents / 100)
        db.execute('UPDATE users SET tickets=tickets+?, balance=balance+? WHERE id=?', (t['tickets'], t['bonus_ton'] * 100, int(user_id)))
        _ns['record_tickets'](db, int(user_id), t['tickets'], 'vip', 'vip', t['key'], 'VIP-статус ' + t['name'])
        _ns['record_transaction'](db, int(user_id), 'vip_bonus', t['bonus_ton'] * 100, 'vip', t['key'], 'VIP-статус ' + t['name'])
        db.execute('INSERT INTO vip_claims(user_id,tier_key,reward_json,seen) VALUES(?,?,?,0)',
                   (int(user_id), t['key'], _ns['json'].dumps(reward, ensure_ascii=False)))
        lines = ['Новый VIP-статус: %s' % t['name'], '']
        if reward['gift']:
            lines.append('🎁 Подарок: %s (%s TON)' % (reward['gift']['name'], format(reward['gift']['price'], 'g')))
        lines.append('🎟 Билеты: %d' % t['tickets'])
        lines.append('💎 Баланс: +%s TON' % t['bonus_ton'])
        if reward['playbonus_unlocked']:
            lines.append('⚡ Открыт PLAYBONUS: кэшбэк можно забирать на основной баланс')
        _ns['add_user_notification'](db, int(user_id), 'vip_level', '\n'.join(lines))
        out.append(reward)
    return out


def register(app, ns):
    _ns.update(ns)
    connect = ns['connect']
    login = ns['login_required']
    from flask import jsonify, session
    import leaderboard

    try:
        with connect() as db:
            db.executescript(SCHEMA)
        try:
            with connect() as db:
                db.execute("ALTER TABLE recent_wins ADD COLUMN image TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass        # already there
        try:
            with connect() as db:
                db.execute("ALTER TABLE vip_claims ADD COLUMN seen INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
    except Exception:
        app.logger.exception('VIP schema setup failed')

    def user_turnover(db, uid):
        creator = ns['creator_record'](uid)
        if creator.get('demo_enabled'):
            return int(creator.get('demo_turnover_cents') or 0), True
        row = db.execute('SELECT turnover_cents FROM users WHERE id=?', (uid,)).fetchone()
        return (int(row['turnover_cents'] or 0) if row else 0), False

    def reward_view(r):
        d = ns['json'].loads(r['reward_json'] or '{}')
        d['tier'] = r['tier_key']
        return d

    @app.get('/api/vip')
    @login
    def vip_state_api():
        uid = session['uid']
        with connect() as db:
            cents, demo = user_turnover(db, uid)
            rows = db.execute('SELECT tier_key,reward_json,seen FROM vip_claims WHERE user_id=?', (uid,)).fetchall()
            pb = db.execute('SELECT playbonus FROM users WHERE id=?', (uid,)).fetchone()
        got = {r['tier_key']: reward_view(r) for r in rows}
        unseen = [got[r['tier_key']] for r in rows if not int(r['seen'])]
        state = tier_state(cents)
        tiers = []
        for i, t in enumerate(VIP_TIERS):
            unlocked = state['reached'] and i <= state['index']
            tiers.append(dict(key=t['key'], name=t['name'], ton=t['ton'], perks=t['perks'], tickets=t['tickets'], bonus_ton=t['bonus_ton'],
                              playbonus=t['playbonus'], unlocked=unlocked, claimed=t['key'] in got, reward=got.get(t['key'])))
        pb_open = state['reached'] and state['index'] >= PLAYBONUS_TIER
        return jsonify(ok=True, state=state, tiers=tiers, pending=len(unseen), unseen=unseen,
                       playbonus=dict(amount=int(pb['playbonus'] or 0) / 100 if pb and not demo else 0, unlocked=bool(pb_open),
                                      tier=VIP_TIERS[PLAYBONUS_TIER]['name'], percent=ns['LOSS_CASHBACK_PERCENT']))

    @app.post('/api/vip/seen')
    @login
    def vip_seen():
        with connect() as db:
            db.execute('UPDATE vip_claims SET seen=1 WHERE user_id=?', (session['uid'],))
        return jsonify(ok=True)

    @app.post('/api/playbonus/claim')
    @login
    def playbonus_claim():
        uid = session['uid']
        if ns['creator_demo_active'](uid):
            return ns['error']('В демо-режиме PLAYBONUS недоступен.', 409)
        db = connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT turnover_cents,playbonus FROM users WHERE id=?' + (' FOR UPDATE' if ns['DATABASE_URL'] else ''), (uid,)).fetchone()
            if not row:
                db.rollback()
                return ns['error']('Пользователь не найден.', 404)
            if tier_state(int(row['turnover_cents'] or 0))['index'] < PLAYBONUS_TIER or int(row['turnover_cents'] or 0) < VIP_TIERS[PLAYBONUS_TIER]['ton'] * 100:
                db.rollback()
                return ns['error']('PLAYBONUS открывается со статуса %s.' % VIP_TIERS[PLAYBONUS_TIER]['name'], 403)
            amount = int(row['playbonus'] or 0)
            if amount <= 0:
                db.rollback()
                return ns['error']('PLAYBONUS пока пуст.', 409)
            db.execute('UPDATE users SET balance=balance+?, playbonus=0 WHERE id=?', (amount, uid))
            ns['record_transaction'](db, uid, 'playbonus_claim', amount, 'vip', 'playbonus', 'PLAYBONUS')
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('PLAYBONUS claim failed')
            return ns['error']('Не удалось забрать PLAYBONUS.', 500)
        finally:
            db.close()
        return jsonify(ok=True, amount=amount / 100, user=ns['profile']())

    @app.get('/api/recent-wins')
    @login
    def vip_recent_wins():
        with connect() as db:
            rows = db.execute("""SELECT w.id,w.user_id,w.game,w.amount,w.prize,w.image,w.created_at,u.name,u.username,u.photo_url
                                 FROM recent_wins w JOIN users u ON u.id=w.user_id
                                 ORDER BY w.id DESC LIMIT ?""", (FEED_SIZE,)).fetchall()
            if rows and int(rows[0]['id']) % 25 == 0:      # cheap, occasional trim of the feed table
                db.execute('DELETE FROM recent_wins WHERE id<?', (int(rows[0]['id']) - FEED_KEEP,))
        items = [dict(id=int(r['id']), user_id=int(r['user_id']), game=r['game'], amount=int(r['amount']) / 100,
                      prize=r['prize'], image=str(r['image'] or ''), created_at=str(r['created_at'] or ''),
                      name=leaderboard.public_handle(r['username'], r['name']), photo_url=str(r['photo_url'] or ''))
                 for r in rows]
        return jsonify(ok=True, items=items)
