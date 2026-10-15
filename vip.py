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

def _perks(tickets, bonus, extra=()):
    out = ['Разовое задание: %s билетов' % tickets]
    out.append(bonus)
    out += ['Эксклюзивные промокоды', 'Спонтанные раздачи подарков']
    out += list(extra)
    return out


VIP_TIERS = [
    dict(key='iron', name='Quartz', ton=500, perks=_perks('500', 'Бонусный NFT Gift за достижение уровня')),
    dict(key='bronze', name='Amber', ton=2500, perks=_perks('1 500', 'Бонус 5 TON за достижение уровня', ['Эксклюзивные VIP-розыгрыши'])),
    dict(key='silver', name='Opal', ton=10000, perks=_perks('5 000', 'Бонус 15 TON за достижение уровня', ['Эксклюзивные VIP-розыгрыши'])),
    dict(key='gold', name='Citrine', ton=30000, perks=_perks('15 000', 'Бонус 40 TON за достижение уровня', ['Эксклюзивные VIP-розыгрыши'])),
    dict(key='platinum', name='Aquamarine', ton=100000, perks=_perks('50 000', 'Бонус 120 TON за достижение уровня', ['Эксклюзивные VIP-розыгрыши', 'Персональный VIP-менеджер'])),
    dict(key='diamond', name='Amethyst', ton=300000, perks=_perks('150 000', 'Бонус 400 TON за достижение уровня', ['Эксклюзивные VIP-розыгрыши', 'Персональный VIP-менеджер', 'Бонусы от VIP-менеджера'])),
    dict(key='obsidian', name='Onyx', ton=1000000, perks=_perks('500 000', 'Бонус 1500 TON за достижение уровня', ['Эксклюзивные VIP-розыгрыши', 'Персональный VIP-менеджер', 'Бонусы от VIP-менеджера'])),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS recent_wins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, game TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0, prize TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_ns = {}
_img = {'at': 0.0, 'map': {}}


def gift_image(name):
    """Picture of a catalog gift by its name (cached for a minute)."""
    import time
    if time.time() - _img['at'] > 60:
        try:
            doc = _ns['read_document']('portal_catalog') or {}
            _img['map'] = {str(g.get('name') or '').strip().lower(): str(g.get('image_url') or '') for g in (doc.get('gifts') or [])}
        except Exception:
            _img['map'] = {}
        _img['at'] = time.time()
    return _img['map'].get(str(name or '').strip().lower(), '')


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
                   (int(user_id), game, max(0, amount), prize, gift_image(prize) if prize else ''))
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

    @app.get('/api/vip')
    @login
    def vip_state_api():
        uid = session['uid']
        creator = ns['creator_record'](uid)
        if creator.get('demo_enabled'):
            cents = int(creator.get('demo_turnover_cents') or 0)
        else:
            with connect() as db:
                row = db.execute('SELECT turnover_cents FROM users WHERE id=?', (uid,)).fetchone()
            cents = int(row['turnover_cents'] or 0) if row else 0
        state = tier_state(cents)
        tiers = [dict(key=t['key'], name=t['name'], ton=t['ton'], perks=t['perks'], unlocked=state['reached'] and i <= state['index'])
                 for i, t in enumerate(VIP_TIERS)]
        return jsonify(ok=True, state=state, tiers=tiers)

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
