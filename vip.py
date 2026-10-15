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

def _tier(key, name, ton, tickets, bonus_ton):
    return dict(key=key, name=name, ton=ton, tickets=tickets, bonus_ton=bonus_ton,
                perks=['%s билетов' % format(tickets, ',').replace(',', ' '), '%s TON на бонусный баланс' % bonus_ton])


# Rewards are real: claimed once per tier from the VIP window (tickets + bonus balance).
VIP_TIERS = [
    _tier('iron', 'Quartz', 500, 500, 2),
    _tier('bronze', 'Amber', 2500, 1500, 5),
    _tier('silver', 'Opal', 10000, 5000, 15),
    _tier('gold', 'Citrine', 30000, 15000, 40),
    _tier('platinum', 'Aquamarine', 100000, 50000, 120),
    _tier('diamond', 'Amethyst', 300000, 150000, 400),
    _tier('obsidian', 'Onyx', 1000000, 500000, 1500),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS vip_claims (
    user_id INTEGER NOT NULL, tier_key TEXT NOT NULL, reward_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, tier_key)
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
    except Exception:
        app.logger.exception('VIP schema setup failed')

    def user_turnover(db, uid):
        creator = ns['creator_record'](uid)
        if creator.get('demo_enabled'):
            return int(creator.get('demo_turnover_cents') or 0), True
        row = db.execute('SELECT turnover_cents FROM users WHERE id=?', (uid,)).fetchone()
        return (int(row['turnover_cents'] or 0) if row else 0), False

    @app.get('/api/vip')
    @login
    def vip_state_api():
        uid = session['uid']
        with connect() as db:
            cents, demo = user_turnover(db, uid)
            claimed = {r['tier_key'] for r in db.execute('SELECT tier_key FROM vip_claims WHERE user_id=?', (uid,)).fetchall()}
        state = tier_state(cents)
        tiers = []
        for i, t in enumerate(VIP_TIERS):
            unlocked = state['reached'] and i <= state['index']
            tiers.append(dict(key=t['key'], name=t['name'], ton=t['ton'], perks=t['perks'], tickets=t['tickets'], bonus_ton=t['bonus_ton'],
                              unlocked=unlocked, claimed=t['key'] in claimed, claimable=bool(unlocked and t['key'] not in claimed and not demo)))
        return jsonify(ok=True, state=state, tiers=tiers, pending=sum(1 for t in tiers if t['claimable']))

    @app.post('/api/vip/claim')
    @login
    def vip_claim():
        from flask import request
        uid = session['uid']
        key = str((request.get_json(silent=True) or {}).get('tier') or '')
        tier = next((t for t in VIP_TIERS if t['key'] == key), None)
        if not tier:
            return ns['error']('Уровень не найден.', 404)
        if ns['creator_demo_active'](uid):
            return ns['error']('В демо-режиме награды недоступны.', 409)
        db = connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT turnover_cents FROM users WHERE id=?' + (' FOR UPDATE' if ns['DATABASE_URL'] else ''), (uid,)).fetchone()
            if not row:
                db.rollback()
                return ns['error']('Пользователь не найден.', 404)
            if int(row['turnover_cents'] or 0) < tier['ton'] * 100:
                db.rollback()
                return ns['error']('Достигните уровня, чтобы получить награду.', 403)
            if db.execute('SELECT 1 FROM vip_claims WHERE user_id=? AND tier_key=?', (uid, key)).fetchone():
                db.rollback()
                return ns['error']('Награда уже получена.', 409)
            bonus = int(tier['bonus_ton']) * 100
            db.execute('UPDATE users SET tickets=tickets+?, bonus_balance=bonus_balance+? WHERE id=?', (tier['tickets'], bonus, uid))
            ns['record_tickets'](db, uid, tier['tickets'], 'vip', 'vip', key, 'VIP-уровень ' + tier['name'])
            ns['record_transaction'](db, uid, 'vip_bonus', bonus, 'vip', key, 'VIP-уровень ' + tier['name'])
            result = dict(tier=key, name=tier['name'], tickets=tier['tickets'], bonus_ton=tier['bonus_ton'])
            db.execute('INSERT INTO vip_claims(user_id,tier_key,reward_json) VALUES(?,?,?)', (uid, key, ns['json'].dumps(result, ensure_ascii=False)))
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('VIP claim failed')
            return ns['error']('Не удалось получить награду. Попробуйте ещё раз.', 500)
        finally:
            db.close()
        return jsonify(ok=True, reward=result, user=ns['profile'](),
                       message='+%d билетов и +%d TON на бонусный баланс' % (tier['tickets'], tier['bonus_ton']))

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
