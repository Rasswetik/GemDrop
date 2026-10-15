"""Plinko: a ball falls through 16 rows of pins into one of 17 multiplier slots.

Provably fair: one HMAC draw in [0, 65536) gives 16 bits, bit (15 - i) is the ball's direction on row i (row 1 = highest bit)
(0 = left, 1 = right), the slot is the number of right turns. That is exactly a binomial fall,
and the client animates the very same path. Payouts follow the same rules as Limbo
(gift first, the rest in balance, cashback on a lost bet).
"""
import time
from math import comb

ROWS = 16                      # draw size: the hardest board; easier boards use the first rows of the same draw
RISK_ROWS = {'low': 8, 'medium': 12, 'high': 16}   # harder risk = more pins
RANGE = 1 << ROWS
MAX_PAYOUT_CENTS = 30000000    # 300 000 TON: effectively "max bet x top multiplier"
FEED_SIZE = 14
RISKS = ('low', 'medium', 'high')
# Half of the symmetric slot table (centre first, edge last). The edge multiplier is fixed
# (16x / 110x / 1000x); the site RTP only tunes the other slots.
SHAPES = {
    'low': (0.5, 0.9, 1.2, 1.8, 5.6),
    'medium': (0.3, 0.6, 1.0, 1.6, 3.0, 8.0, 33.0),
    'high': (0.2, 0.2, 0.2, 2.0, 4.0, 9.0, 26.0, 130.0, 1000.0),
}
PROB = {r: [comb(n, k) / (1 << n) for k in range(n + 1)] for r, n in RISK_ROWS.items()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS plinko_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, bet INTEGER NOT NULL,
    risk TEXT NOT NULL DEFAULT 'medium', path INTEGER NOT NULL DEFAULT 0, slot INTEGER NOT NULL DEFAULT 0,
    multiplier_x100 INTEGER NOT NULL DEFAULT 0, payout INTEGER NOT NULL DEFAULT 0,
    prize_name TEXT NOT NULL DEFAULT '', prize_image TEXT NOT NULL DEFAULT '', prize_price INTEGER NOT NULL DEFAULT 0,
    bonus_used INTEGER NOT NULL DEFAULT 0,
    fairness_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plinko_bets_user ON plinko_bets(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_plinko_bets_mult ON plinko_bets(multiplier_x100, id DESC);
"""

_ns = {}


def rows_of(risk):
    return RISK_ROWS.get(risk, RISK_ROWS['medium'])


def slot_of(path, rows=ROWS):
    """Row i uses bit (15 - i) of the draw, so a board with fewer rows reads only its top bits."""
    return bin((int(path) & (RANGE - 1)) >> (ROWS - rows)).count('1')


def multipliers(risk, rtp):
    """x100 multipliers for the slots of that risk's board, floored so the real RTP never exceeds the configured one."""
    risk = risk if risk in SHAPES else 'medium'
    half = SHAPES[risk]
    rows = rows_of(risk)
    prob = PROB[risk]
    mid = rows // 2
    table = [half[abs(k - mid)] for k in range(rows + 1)]
    edge_p = prob[0] + prob[rows]
    rest = sum(p * m for k, (p, m) in enumerate(zip(prob, table)) if abs(k - mid) != mid)
    scale = (float(rtp) - edge_p * half[mid]) / rest
    if scale <= 0.05:   # very low RTP: scale everything, the edge included
        scale = float(rtp) / (rest + edge_p * half[mid])
        return [max(10, int(m * scale * 100)) for m in table]
    return [int(round(m * 100)) if abs(k - mid) == mid else max(10, int(m * scale * 100)) for k, m in enumerate(table)]


def result_view(row):
    row = dict(row)
    ns = _ns
    prize = ns['crash_gift_view'](row.get('prize_name'), row.get('prize_image'), row.get('prize_price', 0)) if row.get('prize_name') else None
    return dict(id=int(row['id']), bet=int(row['bet']) / 100, risk=row['risk'], rows=rows_of(row['risk']), path=int(row['path']), slot=int(row['slot']),
                multiplier=int(row['multiplier_x100']) / 100, payout=int(row['payout']) / 100,
                won=int(row['payout']) >= int(row['bet']), prize=prize,
                remainder=max(0, int(row['payout']) - int(row.get('prize_price') or 0)) / 100,
                created_at=str(row['created_at'] or ''))


def register(app, ns):
    _ns.update(ns)
    connect = ns['connect']
    login = ns['login_required']
    error = ns['error']
    from flask import jsonify, request, session

    try:
        with connect() as db:
            db.executescript(SCHEMA)
    except Exception:
        app.logger.exception('Plinko schema setup failed')

    def config():
        rtp = ns['plinko_rtp']()
        return dict(rows=RISK_ROWS, risks=list(RISKS), rtp=rtp, min_bet=ns['MIN_BET_CENTS'] / 100, max_bet=ns['MAX_BET_CENTS'] / 100,
                    max_payout=MAX_PAYOUT_CENTS / 100, multipliers={r: [m / 100 for m in multipliers(r, rtp)] for r in RISKS})

    def feed(db, uid):
        wins = db.execute("""SELECT b.id,b.user_id,b.bet,b.risk,b.path,b.slot,b.multiplier_x100,b.payout,b.created_at,
                                    b.prize_name,b.prize_image,b.prize_price,u.name,u.photo_url
                             FROM plinko_bets b JOIN users u ON u.id=b.user_id
                             WHERE b.payout>b.bet AND b.id>? ORDER BY b.id DESC LIMIT ?""", (ns['wins_feed_cutoff'](db, 'plinko')[1], FEED_SIZE)).fetchall()
        mine = db.execute("""SELECT id,bet,risk,path,slot,multiplier_x100,payout,created_at,prize_name,prize_image,prize_price
                             FROM plinko_bets WHERE user_id=? ORDER BY id DESC LIMIT 20""", (uid,)).fetchall()
        out = []
        for r in wins:
            item = result_view(r)
            item.update(user_id=int(r['user_id']), profit=(int(r['payout']) - int(r['bet'])) / 100,
                        name=str(r['name'] or '')[:40], photo_url=str(r['photo_url'] or ''))
            out.append(item)
        return out, [result_view(r) for r in mine]

    @app.get('/api/plinko/state')
    @login
    def plinko_state():
        uid = session['uid']
        with connect() as db:
            wins, mine = feed(db, uid)
        return jsonify(ok=True, config=config(), wins=wins, history=mine, user=ns['profile'](),
                       available=ns['game_available']('plinko'))

    @app.post('/api/plinko/play')
    @login
    def plinko_play():
        data = request.get_json(silent=True) or {}
        uid = session['uid']
        if ns['creator_demo_active'](uid):
            return error('Plinko недоступна в демо-режиме.', 409)
        try:
            bet = ns['parse_amount'](data.get('bet'))
        except Exception:
            return error('Укажите корректную ставку.')
        if not (ns['MIN_BET_CENTS'] <= bet <= ns['MAX_BET_CENTS']):
            return error('Ставка от 0.10 до 300 TON.')
        risk = str(data.get('risk') or 'medium')
        if risk not in RISKS:
            return error('Выберите уровень риска.')
        table = multipliers(risk, ns['plinko_rtp']())
        proof = ns['fairness_make']('plinko', uid, data.get('client_seed'))
        path, fair_cursor, _ = ns['fairness_draw'](proof, RANGE, 0)
        slot = slot_of(path, rows_of(risk))
        mult_x100 = table[slot]
        payout = min(MAX_PAYOUT_CENTS, bet * mult_x100 // 100)
        db = connect()
        new_level = None
        try:
            db.execute('BEGIN IMMEDIATE')
            if ns['DATABASE_URL']:
                db.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (uid,))
            spent = ns['spend_game_balance'](db, uid, bet)
            if not spent:
                db.rollback()
                return error('Недостаточно средств.')
            bonus_used = int(spent[1])
            cur = db.execute("""INSERT INTO plinko_bets(user_id,bet,risk,path,slot,multiplier_x100,payout,bonus_used)
                                VALUES(?,?,?,?,?,?,?,?)""", (uid, bet, risk, path, slot, mult_x100, payout, bonus_used))
            bet_id = int(cur.lastrowid)
            ns['record_transaction'](db, uid, 'plinko_bet', -bet, 'plinko', bet_id, 'Plinko · %s' % risk)
            new_level = ns['increase_turnover'](db, uid, bet, withdrawal_wager=not bool(bonus_used),
                                                xp_factor=(0.2 if bonus_used else 1.0))
            if payout:
                remainder = payout
                prize = ns['crash_prize_preview'](payout, seed=f'plinko:{uid}:{bet_id}', db=db)
                if prize:
                    ns['award_rotated_gift'](db, uid, prize, bet_id, bool(bonus_used))
                    db.execute('UPDATE plinko_bets SET prize_name=?,prize_image=?,prize_price=? WHERE id=?',
                               (prize['name'], prize['image_url'], prize['price_cents'], bet_id))
                    remainder -= prize['price_cents']
                    ns['record_transaction'](db, uid, 'plinko_gift_win', 0, 'plinko', bet_id, prize['name'])
                if remainder:
                    ns['credit_game_balance'](db, uid, remainder, bool(bonus_used))
                    ns['record_transaction'](db, uid, 'plinko_win', remainder, 'plinko', bet_id, 'Plinko · x%.2f' % (mult_x100 / 100))
            if payout < bet and not bonus_used:
                ns['credit_main_loss_cashback'](db, uid, bet - payout, 'plinko', bet_id, 'Plinko · x%.2f' % (mult_x100 / 100))
            outcome = dict(upper=RANGE, ticket=path, path=path, slot=slot, risk=risk, multiplier_x100=mult_x100)
            ns['fairness_store'](db, proof, game_ref=bet_id, cursor=fair_cursor, outcome=outcome, state='settled')
            db.execute('UPDATE plinko_bets SET fairness_id=? WHERE id=?', (proof['id'], bet_id))
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception('Plinko drop failed')
            return error('Не удалось провести бросок. Попробуйте ещё раз.', 500)
        finally:
            db.close()
        if new_level:
            ns['notify_level_up_async'](uid, new_level)
        with connect() as db:
            row = db.execute("""SELECT id,bet,risk,path,slot,multiplier_x100,payout,created_at,prize_name,prize_image,prize_price
                                FROM plinko_bets WHERE id=?""", (bet_id,)).fetchone()
            fair = ns['fairness_public'](ns['fairness_get'](db, proof_id=proof['id']), True)
        return jsonify(ok=True, result=dict(result_view(row), bonus_origin=bool(bonus_used)), fairness=fair,
                       user=ns['profile'](), new_level=new_level)
