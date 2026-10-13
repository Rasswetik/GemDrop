"""Read-only, paginated operator views; amounts come from settled game records."""
import json
from collections import defaultdict
from flask import jsonify, request


GAME_LABELS = {'mines':'Mines','upgrade':'Upgrade','crash':'Crash','arena':'Arena',
               'hilo':'Hi-Lo','limbo':'Limbo','road':'Hamster Road'}


def game_ledger(ns):
    fee = int(ns['ARENA_FEE_PERCENT'])
    return f"""
    SELECT 'mines:' || CAST(id AS TEXT) AS ref,user_id,'mines' AS game,state,
           bet AS stake,CASE WHEN state='won' THEN COALESCE(win_total,payout,0) ELSE 0 END AS payout,
           created_at AS date,CASE WHEN bet_type<>'promo_gift' AND bonus_used=0 AND state IN ('won','lost') THEN 1 ELSE 0 END AS eligible
    FROM rounds
    UNION ALL
    SELECT 'upgrade:' || id,user_id,'upgrade',CASE WHEN won=1 THEN 'won' ELSE 'lost' END,
           source_price,CASE WHEN won=1 THEN target_price ELSE 0 END,created_at,
           CASE WHEN bonus_used=0 AND REPLACE(result_json,' ','') NOT LIKE '%"reward_type":"wager_progress"%'
                     AND REPLACE(result_json,' ','') NOT LIKE '%"wager_target":%' THEN 1 ELSE 0 END
    FROM upgrade_spins
    UNION ALL
    SELECT 'crash:' || CAST(round_id AS TEXT),user_id,'crash',state,bet,payout,created_at,
           CASE WHEN bonus_used=0 AND bet_type<>'promo_gift' AND state IN ('won','lost') THEN 1 ELSE 0 END
    FROM crash_bets
    UNION ALL
    SELECT 'arena:' || CAST(b.round_id AS TEXT),b.user_id,'arena',
           CASE WHEN r.state<>'settled' THEN 'active' WHEN r.winner_user_id=b.user_id THEN 'won' ELSE 'lost' END,
           b.amount,CASE WHEN r.state='settled' AND r.winner_user_id=b.user_id THEN
           r.total_pool-COALESCE(p.ton_pool,0)*{fee}/100 ELSE 0 END,b.created_at,
           CASE WHEN b.bonus_used=0 AND r.state='settled' THEN 1 ELSE 0 END
    FROM arena_bets b JOIN arena_rounds r ON r.id=b.round_id
    LEFT JOIN (SELECT round_id,SUM(amount-gift_amount) AS ton_pool FROM arena_bets GROUP BY round_id) p ON p.round_id=r.id
    UNION ALL
    SELECT 'hilo:' || CAST(id AS TEXT),user_id,'hilo',
           CASE WHEN state IN ('cashed','won') THEN 'won' ELSE state END,bet,payout,created_at,
           CASE WHEN bonus_used=0 AND bet_type<>'promo_gift' AND state IN ('cashed','won','lost') THEN 1 ELSE 0 END
    FROM hilo_games
    UNION ALL
    SELECT 'hilo_room:' || CAST(id AS TEXT),user_id,'hilo',
           CASE WHEN settled=0 THEN 'active' WHEN payout>0 THEN 'won' ELSE 'lost' END,amount,payout,created_at,
           CASE WHEN bonus_used=0 AND settled=1 THEN 1 ELSE 0 END
    FROM hilo_room_bets
    UNION ALL
    SELECT 'limbo:' || CAST(id AS TEXT),user_id,'limbo',CASE WHEN won=1 THEN 'won' ELSE 'lost' END,
           bet,payout,created_at,CASE WHEN bonus_used=0 THEN 1 ELSE 0 END FROM limbo_bets
    UNION ALL
    SELECT 'road:' || CAST(id AS TEXT),user_id,'road',CASE WHEN state IN ('cashed','won') THEN 'won' ELSE state END,
           bet,payout,created_at,CASE WHEN bonus_used=0 AND state IN ('cashed','won','lost') THEN 1 ELSE 0 END
    FROM road_games
    """


def sql_values(values):
    return ','.join("'"+str(v).replace("'","''")+"'" for v in sorted(values))


def timeline_ledger(ns):
    allowed = set(ns['_TX_TITLES']) | {'stars_deposit','crypto_deposit','gift_deposit','gift_deposit_inventory'}
    hidden = set(ns['_EVENT_HIDDEN']) | {'limbo','road_start','road_step','road_cashout','hilo_start','hilo_bet','hilo_guess','hilo_cashout','arena_bet','crash_bet','crash_cashout'}
    return f"""
    SELECT ref,user_id,game AS category,'game' AS source,date FROM ({game_ledger(ns)}) games
    UNION ALL
    SELECT 'transaction:' || CAST(id AS TEXT),user_id,
      CASE WHEN kind IN ('ton_deposit','stars_deposit','crypto_deposit','gift_deposit','gift_deposit_inventory') THEN 'payments'
           WHEN kind='gift_sale' THEN 'sales' WHEN kind LIKE 'withdrawal_%' THEN 'withdrawals'
           WHEN kind LIKE 'referral_%' THEN 'referrals' WHEN kind IN ('admin_balance','deposit') THEN 'admin'
           ELSE 'events' END,'transaction',created_at
    FROM transactions WHERE kind IN ({sql_values(allowed)}) OR kind LIKE 'promo_%' OR kind LIKE 'freebet_%'
    UNION ALL
    SELECT 'event:' || CAST(id AS TEXT),user_id,'events','event',created_at FROM user_events
    WHERE kind NOT IN ({sql_values(hidden)})
    UNION ALL
    SELECT 'admin:' || CAST(id AS TEXT),user_id,'admin','admin',created_at FROM admin_log
    WHERE action NOT IN ({sql_values(ns['_ADMIN_ACTION_HIDDEN'])})
    """


def integer_cells(raw):
    try:
        values=json.loads(raw or '[]')
    except (TypeError,ValueError):
        return []
    return sorted({v for v in values if type(v) is int and 0<=v<25}) if isinstance(values,list) else []


def game_event(row, record, ns):
    prefix=record['ref'].split(':',1)[0]
    builder={'mines':'_activity_mines','upgrade':'_activity_upgrade','crash':'_activity_crash','arena':'_activity_arena'}.get(prefix)
    if builder:
        item=ns[builder](row)
    else:
        label=GAME_LABELS[record['category']]
        image=str(row.get('prize_image') or '')
        lines=[]
        if prefix=='limbo':
            lines=[f'Шанс: {int(row["chance_bp"])/100:.2f}% · цель ×{int(row["multiplier_x100"])/100:.2f}']
        elif prefix=='road':
            lines=[f'Сложность: {row["difficulty"]} · пройдено шагов: {row["steps"]} · ×{int(row["mult_x100"])/100:.2f}']
        elif prefix=='hilo':
            lines=[f'Шагов: {row["steps"]} · ×{int(row["mult_micro"])/1000000:.2f}']
        elif prefix=='hilo_room':
            lines=[f'Раунд #{row["round_no"]} · выбор: {row["direction"]}']
        if row.get('prize_name'):
            lines.append('Подарок: '+row['prize_name'])
        state=record['state']
        item=ns['_ev'](record['ref'],record['date'],prefix,label+' · '+{'won':'выигрыш','lost':'проигрыш','active':'в игре'}.get(state,state),lines,
                       tone='pending' if state=='active' else 'win' if state=='won' else 'loss',image=image,group='game')
    item['details'].update(game=record['category'],state=record['state'],stake=record['stake']/100,
                           payout=record['payout']/100,ordinary=bool(record['eligible']))
    if prefix=='mines':
        item['details']['board']=dict(opened=integer_cells(row['opened']),
              mines=integer_cells(row['positions']) if row['state']!='active' else [],
              lost_cell=row['lost_cell'] if row['state']=='lost' else None,count=int(row['mines']))
    return item


def hydrate(db, records, ns):
    groups=defaultdict(list)
    for r in records:
        prefix,key=r['ref'].split(':',1);groups[prefix].append(key)
    queries={
      'mines':('SELECT * FROM rounds','user_id','id'),
      'upgrade':('SELECT * FROM upgrade_spins','user_id','id'),
      'crash':('SELECT b.*,r.crash_x100,r.state AS round_state FROM crash_bets b LEFT JOIN crash_rounds r ON r.id=b.round_id','b.user_id','b.round_id'),
      'arena':('SELECT b.*,r.state AS round_state,r.winner_user_id,r.total_pool,(SELECT COALESCE(SUM(x.amount-x.gift_amount),0) FROM arena_bets x WHERE x.round_id=r.id) AS round_ton_pool FROM arena_bets b JOIN arena_rounds r ON r.id=b.round_id','b.user_id','b.round_id'),
      'hilo':('SELECT * FROM hilo_games','user_id','id'),
      'hilo_room':('SELECT * FROM hilo_room_bets','user_id','id'),
      'limbo':('SELECT * FROM limbo_bets','user_id','id'),
      'road':('SELECT * FROM road_games','user_id','id'),
      'transaction':('SELECT * FROM transactions','user_id','id'),
      'event':('SELECT * FROM user_events','user_id','id'),
      'admin':('SELECT * FROM admin_log','user_id','id')}
    found={}
    for prefix,keys in groups.items():
        sql,user_column,id_column=queries[prefix]
        rows=db.execute(sql+f' WHERE {user_column}=? AND {id_column} IN ({",".join("?" for _ in keys)})',
                        (records[0]['user_id'],*(keys if prefix=='upgrade' else [int(k) for k in keys]))).fetchall()
        for row in rows:
            found[prefix+':'+str(row[id_column.split('.')[-1]])]=dict(row)
    items=[]
    for record in records:
        row=found.get(record['ref'])
        if not row:
            continue
        if record['source']=='game':
            item=game_event(row,record,ns)
        else:
            item=ns[{'transaction':'_activity_transaction','event':'_activity_event','admin':'_activity_admin'}[record['source']]](row)
            if item is None:
                title={'crypto_deposit':'Пополнение через CryptoBot','stars_deposit':'Пополнение Stars','gift_deposit':'Пополнение подарком','gift_deposit_inventory':'NFT-пополнение в инвентарь'}.get(row.get('kind'),'Операция баланса')
                item=ns['_ev'](record['ref'],record['date'],row.get('kind',''),title,[row.get('details','')],amount=int(row.get('amount') or 0)/100,tone='win',group='money')
        item.update(id=record['ref'],category=record['category'])
        items.append(item)
    return items


def period_insights(db, ns, stamp):
    day="SUBSTR(REPLACE(date,'T',' '),1,10)"
    ledger=game_ledger(ns)
    games=db.execute(f'''SELECT {day} AS day,COALESCE(SUM(stake),0) AS stake,
                         COALESCE(SUM(payout),0) AS payout,COUNT(*) AS count,COUNT(DISTINCT user_id) AS players
                         FROM ({ledger}) games WHERE eligible=1 AND REPLACE(SUBSTR(date,1,19),'T',' ')>=? GROUP BY {day}''',(stamp,)).fetchall()
    registrations=db.execute("SELECT SUBSTR(REPLACE(created_at,'T',' '),1,10) AS day,COUNT(*) AS count FROM users WHERE REPLACE(SUBSTR(created_at,1,19),'T',' ')>=? GROUP BY SUBSTR(REPLACE(created_at,'T',' '),1,10)",(stamp,)).fetchall()
    return {r['day']:dict(r) for r in games},{r['day']:int(r['count']) for r in registrations}


def register(app, ns, accounting):
    connect=ns['connect'];admin=ns['admin_required'];error=ns['error']
    games=game_ledger(ns);timeline=timeline_ledger(ns)
    categories={'all',*GAME_LABELS,'payments','sales','withdrawals','referrals','admin','events'}
    fold='LOWER' if ns['DATABASE_URL'] else 'CASEFOLD'

    def paging():
        return max(0,request.args.get('offset',0,type=int) or 0),max(1,min(100,request.args.get('limit',50,type=int) or 50))

    def exists(db,uid):
        return db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone()

    def related_sql():
        return '''SELECT r.referred_id AS id,'referral' AS relation FROM referrals r WHERE r.referrer_id=?
                  UNION SELECT r.referrer_id,'referrer' FROM referrals r WHERE r.referred_id=?
                  UNION SELECT b.user_id,'shared_ip' FROM user_ips a JOIN user_ips b ON b.ip=a.ip
                  WHERE a.user_id=? AND b.user_id<>a.user_id'''

    @app.get('/api/admin/users/<int:uid>/overview')
    @admin
    def overview(uid):
        with connect() as db:
            row=db.execute('''SELECT id,name,username,photo_url,created_at,balance,bonus_balance,ref_balance,
                              withdrawal_enabled,withdrawal_block_reason,withdrawal_min_deposit_override,banned,ban_reason
                              FROM users WHERE id=?''',(uid,)).fetchone()
            if not row:return error('Пользователь не найден.',404)
            gaming=db.execute(f'SELECT SUM(stake) AS stake,SUM(payout) AS payout,COUNT(*) AS count FROM ({games}) games WHERE user_id=? AND eligible=1',(uid,)).fetchone()
            deposits=db.execute(f'SELECT SUM(amount) AS total,COUNT(*) AS count FROM ({accounting.PAYMENT_LEDGER}) p WHERE user_id=?',(uid,)).fetchone()
            withdrawn=db.execute("SELECT SUM(CASE WHEN status='approved' THEN floor_price ELSE 0 END) AS total,COUNT(*) AS count FROM withdrawals WHERE user_id=?",(uid,)).fetchone()
            inv=db.execute('SELECT COUNT(*) AS count,COALESCE(SUM(floor_price),0) AS total FROM inventory WHERE user_id=?',(uid,)).fetchone()
            log_count=db.execute(f'SELECT COUNT(*) AS count FROM ({timeline}) history WHERE user_id=?',(uid,)).fetchone()['count']
            related=db.execute('SELECT COUNT(DISTINCT id) AS count FROM ('+related_sql()+') linked',(uid,uid,uid)).fetchone()['count']
            state=accounting.withdrawal_state(db,uid,ns['withdrawal_settings']())
        stake=int(gaming['stake'] or 0);payout=int(gaming['payout'] or 0)
        return jsonify(user=dict(row,balance=int(row['balance'])/100,bonus_balance=int(row['bonus_balance'])/100,ref_balance=int(row['ref_balance'])/100,
                                 withdrawal_min_deposit_override=None if row['withdrawal_min_deposit_override'] is None else int(row['withdrawal_min_deposit_override'])/100),
                       totals=dict(deposits=int(deposits['total'] or 0)/100,withdrawn=int(withdrawn['total'] or 0)/100,
                                   stake=stake/100,payout=payout/100,rtp=round(payout*100/stake,2) if stake>0 else None,
                                   games=int(gaming['count']),inventory_value=int(inv['total'])/100),
                       counts=dict(logs=int(log_count),inventory=int(inv['count']),payments=int(deposits['count']),withdrawals=int(withdrawn['count']),linked=int(related)),withdrawal_state=state)

    @app.get('/api/admin/users/<int:uid>/timeline')
    @admin
    def user_timeline(uid):
        offset,limit=paging();category=request.args.get('category','all')
        if category not in categories:return error('Неизвестный фильтр истории.')
        where='user_id=?';params=[uid]
        if category!='all':where+=' AND category=?';params.append(category)
        with connect() as db:
            if not exists(db,uid):return error('Пользователь не найден.',404)
            count=db.execute(f'SELECT COUNT(*) AS count FROM ({timeline}) history WHERE {where}',tuple(params)).fetchone()['count']
            refs=[dict(r) for r in db.execute(f"SELECT * FROM ({timeline}) history WHERE {where} ORDER BY REPLACE(SUBSTR(date,1,19),'T',' ') DESC,ref DESC LIMIT ? OFFSET ?",tuple(params+[limit,offset])).fetchall()]
            game_refs=[r['ref'] for r in refs if r['source']=='game']
            if game_refs:
                full=db.execute(f'SELECT * FROM ({games}) games WHERE user_id=? AND ref IN ({",".join("?" for _ in game_refs)})',(uid,*game_refs)).fetchall()
                amounts={r['ref']:dict(r) for r in full}
                for r in refs:
                    if r['ref'] in amounts:r.update(amounts[r['ref']])
            items=hydrate(db,refs,ns) if refs else []
        return jsonify(items=items,total=int(count),offset=offset,limit=limit)

    @app.get('/api/admin/users/<int:uid>/inventory-view')
    @admin
    def user_inventory(uid):
        offset,limit=paging();term=request.args.get('q','').strip().casefold()[:100]
        where='user_id=?';params=[uid]
        if term:
            where+=f" AND {fold}(gift_name || ' ' || fragment_model || ' ' || fragment_number) LIKE ? ESCAPE '\\'"
            params.append('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        with connect() as db:
            if not exists(db,uid):return error('Пользователь не найден.',404)
            count=db.execute('SELECT COUNT(*) AS count FROM inventory WHERE '+where,tuple(params)).fetchone()['count']
            rows=db.execute('SELECT * FROM inventory WHERE '+where+' ORDER BY id DESC LIMIT ? OFFSET ?',tuple(params+[limit,offset])).fetchall()
        return jsonify(items=[ns['inventory_item'](r) for r in rows],total=int(count),offset=offset,limit=limit)

    @app.get('/api/admin/users/<int:uid>/withdrawals-view')
    @admin
    def user_withdrawals(uid):
        offset,limit=paging()
        with connect() as db:
            if not exists(db,uid):return error('Пользователь не найден.',404)
            count=db.execute('SELECT COUNT(*) AS count FROM withdrawals WHERE user_id=?',(uid,)).fetchone()['count']
            rows=db.execute('''SELECT w.id,w.gift_name,w.fragment_model,w.image_url,w.floor_price,w.status,w.external_url,
                     w.created_at,w.processed_at,COALESCE(p.error,l.error,'') AS delivery_error
                     FROM withdrawals w LEFT JOIN portal_withdrawal_logs p ON p.withdrawal_id=w.id
                     LEFT JOIN relayer_withdrawal_logs l ON l.withdrawal_id=w.id
                     WHERE w.user_id=? ORDER BY w.id DESC LIMIT ? OFFSET ?''',(uid,limit,offset)).fetchall()
        return jsonify(items=[dict(r,price_ton=int(r['floor_price'])/100) for r in rows],total=int(count),offset=offset,limit=limit)

    @app.get('/api/admin/users/<int:uid>/linked')
    @admin
    def user_linked(uid):
        offset,limit=paging()
        with connect() as db:
            if not exists(db,uid):return error('Пользователь не найден.',404)
            source=related_sql();params=(uid,uid,uid)
            count=db.execute('SELECT COUNT(DISTINCT id) AS count FROM ('+source+') linked',params).fetchone()['count']
            ids=db.execute('SELECT DISTINCT id FROM ('+source+') linked ORDER BY id DESC LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
            items=[]
            if ids:
                chosen=[int(r['id']) for r in ids];holders=','.join('?' for _ in chosen)
                relations=db.execute('SELECT * FROM ('+source+f') linked WHERE id IN ({holders})',(*params,*chosen)).fetchall()
                labels=defaultdict(list)
                for r in relations:labels[int(r['id'])].append(r['relation'])
                users=db.execute(f'SELECT id,name,username,photo_url,created_at FROM users WHERE id IN ({holders}) ORDER BY id DESC',tuple(chosen)).fetchall()
                items=[dict(r,relations=labels[int(r['id'])]) for r in users]
        return jsonify(items=items,total=int(count),offset=offset,limit=limit)
