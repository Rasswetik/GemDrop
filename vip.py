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

VIP_TIERS = [
    dict(key='iron', name='Iron', ton=1000, perks=['Разовое задание: 1 000 билетов', 'Бонусный NFT Gift за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков']),
    dict(key='bronze', name='Bronze', ton=5000, perks=['Разовое задание: 2 500 билетов', 'Бонус 10 TON за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков', 'Эксклюзивные VIP-розыгрыши']),
    dict(key='silver', name='Silver', ton=10000, perks=['Разовое задание: 5 000 билетов', 'Бонус 25 TON за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков', 'Эксклюзивные VIP-розыгрыши']),
    dict(key='gold', name='Gold', ton=25000, perks=['Разовое задание: 10 000 билетов', 'Бонус 50 TON за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков', 'Эксклюзивные VIP-розыгрыши']),
    dict(key='platinum', name='Platinum', ton=50000, perks=['Разовое задание: 25 000 билетов', 'Бонус 100 TON за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков', 'Эксклюзивные VIP-розыгрыши', 'Персональный VIP-менеджер', 'Бонусы от VIP-менеджера']),
    dict(key='diamond', name='Diamond', ton=250000, perks=['Разовое задание: 100 000 билетов', 'Бонус 500 TON за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков', 'Эксклюзивные VIP-розыгрыши', 'Персональный VIP-менеджер', 'Бонусы от VIP-менеджера']),
    dict(key='obsidian', name='Obsidian', ton=1000000, perks=['Разовое задание: 500 000 билетов', 'Бонус 2500 TON за достижение уровня', 'Эксклюзивные промокоды', 'Спонтанные раздачи подарков', 'Эксклюзивные VIP-розыгрыши', 'Персональный VIP-менеджер', 'Бонусы от VIP-менеджера']),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS recent_wins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, game TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0, prize TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_ns = {}


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
        db.execute('INSERT INTO recent_wins(user_id,game,amount,prize) VALUES(?,?,?,?)',
                   (int(user_id), game, max(0, amount), str(details or '')[:80] if gift else ''))
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
            rows = db.execute("""SELECT w.id,w.user_id,w.game,w.amount,w.prize,w.created_at,u.name,u.username,u.photo_url
                                 FROM recent_wins w JOIN users u ON u.id=w.user_id
                                 ORDER BY w.id DESC LIMIT ?""", (FEED_SIZE,)).fetchall()
            if rows and int(rows[0]['id']) % 25 == 0:      # cheap, occasional trim of the feed table
                db.execute('DELETE FROM recent_wins WHERE id<?', (int(rows[0]['id']) - FEED_KEEP,))
        items = [dict(id=int(r['id']), user_id=int(r['user_id']), game=r['game'], amount=int(r['amount']) / 100,
                      prize=r['prize'], created_at=str(r['created_at'] or ''),
                      name=leaderboard.public_handle(r['username'], r['name']), photo_url=str(r['photo_url'] or ''))
                 for r in rows]
        return jsonify(ok=True, items=items)
