from pathlib import Path
import re

root = Path(__file__).resolve().parent
app_path = root / "app.py"
tpl_path = root / "templates" / "index.html"

app = app_path.read_text(encoding="utf-8")
tpl = tpl_path.read_text(encoding="utf-8")

start = app.find("def apply_upgrade_loss_compensation(db, user_id, source_price, target_price=None):")
end = app.find("\n\n@app.get('/api/rewards/pending')", start)
if start < 0 or end < 0:
    raise SystemExit("Upgrade compensation backend markers not found")

backend = r'''def apply_upgrade_loss_compensation(db, user_id, source_price, target_price=None):
    """Select one compensation prize without crediting it yet.

    The prize is persisted in upgrade_spins and is credited only after the
    client reel finishes and calls /api/upgrade/compensation/claim.
    """
    empty = dict(cashback=0, cashback_percent=0, promo=None, reward=None, reel=[],
                 deferred=False, claimed=False)
    source_price = max(0, int(source_price or 0))
    if source_price < MIN_BET_CENTS:
        return empty

    large = source_price >= 10000
    medium = source_price >= 2500
    budget = max(1, round(source_price * (.20 if large else .16)))
    catalog_candidates = craft_catalog_candidates()

    # Small losses always get the compensation window, but gifts start only
    # from 5 TON so a tiny bet can never roll an oversized catalog reward.
    allow_gifts = source_price >= 500
    gifts = [g for g in catalog_candidates if g['price'] <= budget] if allow_gifts else []

    visual_budget = max(budget, round(source_price * (.75 if medium or large else .42)))
    if target_price:
        try:
            visual_budget = max(visual_budget, round(int(target_price) * .18))
        except (TypeError, ValueError):
            pass
    visual_gifts = ([g for g in catalog_candidates if g['price'] <= visual_budget]
                    if allow_gifts else [])
    if not gifts and source_price >= 1000 and visual_gifts:
        gifts = visual_gifts[:max(1, min(18, len(visual_gifts)))]

    pool = []
    for percent in (1, 2, 3, 5):
        amount_cents = max(1, round(source_price * percent / 100))
        pool.append(dict(type='balance', amount=amount_cents / 100,
                         image_url='/static/img/ton.png', name='TON'))
    ticket_count = max(1, min(250, round(source_price / 500)))
    pool.append(dict(type='tickets', tickets=ticket_count, image_url='', name='Билеты'))

    gift_options = []
    for gift in gifts:
        for kind in ('gift', 'wager_gift', 'promo'):
            multiplier = secrets.choice([5, 8, 10] if large else
                                        [8, 10, 12] if medium else
                                        [10, 15, 20])
            gift_options.append(dict(
                type=kind, gift_id=gift['id'], name=gift['name'],
                image_url=gift['image_url'], price_ton=gift['price'] / 100,
                wager_multiplier=multiplier if kind == 'wager_gift' else 0
            ))

    if gift_options:
        weights = [('balance', 8 if large else 12 if medium else 16),
                   ('tickets', 10), ('wager_gift', 37), ('gift', 25),
                   ('promo', 20 if large else 16 if medium else 12)]
        roll = secrets.randbelow(sum(weight for _, weight in weights))
        kind = 'balance'
        for candidate, weight in weights:
            if roll < weight:
                kind = candidate
                break
            roll -= weight
        if kind in ('balance', 'tickets'):
            candidates = [x for x in pool if x['type'] == kind]
        else:
            candidates = [g for g in gift_options if g['type'] == kind]
        reward = dict(secrets.choice(candidates or pool))
    else:
        reward = dict(secrets.choice(pool))

    reel_options = pool + gift_options
    if visual_gifts:
        for gift in visual_gifts[-18:]:
            for kind in ('gift', 'wager_gift'):
                multiplier = secrets.choice([5, 8, 10] if large else
                                            [8, 10, 12] if medium else
                                            [10, 15, 20])
                reel_options.append(dict(
                    type=kind, gift_id=gift['id'], name=gift['name'],
                    image_url=gift['image_url'], price_ton=gift['price'] / 100,
                    wager_multiplier=multiplier if kind == 'wager_gift' else 0
                ))

    return dict(empty,
                reward=reward,
                reel=[secrets.choice(reel_options or pool) for _ in range(36)],
                deferred=True,
                claimed=False)


def claim_upgrade_loss_compensation(db, user_id, spin_id, result):
    """Credit a deferred Upgrade compensation exactly once."""
    comp = dict((result or {}).get('compensation') or {})
    reward = dict(comp.get('reward') or {})
    if not reward or not comp.get('deferred'):
        return result, False
    if comp.get('claimed'):
        return result, False

    kind = str(reward.get('type') or '')
    if kind == 'balance':
        amount = ton_to_cents(reward.get('amount') or 0)
        if amount > 0:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
            record_transaction(db, user_id, 'upgrade_cashback', amount, 'upgrade', spin_id,
                               'Компенсация Upgrade')
            comp['cashback'] = amount / 100
    elif kind == 'tickets':
        tickets = max(1, int(reward.get('tickets') or 1))
        db.execute('UPDATE users SET tickets=tickets+? WHERE id=?', (tickets, user_id))
        db.execute('''INSERT INTO ticket_ledger(user_id,amount,kind,reference_type,reference_id,details)
                      VALUES(?,?,?,?,?,?)''',
                   (user_id, tickets, 'upgrade_compensation', 'upgrade', spin_id,
                    f'Компенсация Upgrade: {tickets} билет(ов)'))
    elif kind in ('gift', 'wager_gift'):
        locked = kind == 'wager_gift'
        price = ton_to_cents(reward.get('price_ton') or 0)
        multiplier = float(reward.get('wager_multiplier') or 0)
        cur = db.execute('''INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,
                         promo_locked,promo_wager_multiplier,promo_wager_target,promo_wager_progress)
                         VALUES(?,?,?,?,?,'upgrade_compensation',?,?,?,0)''',
                         (user_id, reward.get('gift_id') or '', reward.get('name') or 'Подарок',
                          reward.get('image_url') or '', price, int(locked), multiplier,
                          round(price * multiplier)))
        reward['inventory_id'] = cur.lastrowid
        reward['wager_target'] = round(price * multiplier) / 100
        record_transaction(db, user_id, 'upgrade_compensation_gift', 0, 'inventory',
                           cur.lastrowid, reward.get('name') or 'Подарок')
    elif kind == 'promo':
        code = unique_promo_code(db, 'UPG')
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.execute("""INSERT INTO promo_codes(code,reward_type,amount,gift_id,gift_name,gift_image_url,
                     gift_price,wager_multiplier,max_uses,created_by,assigned_user_id,source_label,description,
                     reward_json,expires_at) VALUES(?,'gift',0,?,?,?,?,0,1,0,?,?,?,?,?)""",
                   (code, reward.get('gift_id') or '', reward.get('name') or 'Подарок',
                    reward.get('image_url') or '', ton_to_cents(reward.get('price_ton') or 0),
                    user_id, 'Компенсация Upgrade', 'Персональный промокод на подарок',
                    json.dumps(dict(compensation=True, owner_id=user_id), ensure_ascii=False), expires))
        comp['promo'] = promo_view(db.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone())
        reward['code'] = code
    else:
        raise ValueError('Неизвестный тип компенсации.')

    comp['reward'] = reward
    comp['claimed'] = True
    comp['claimed_at'] = datetime.now(timezone.utc).isoformat()
    result = dict(result or {})
    result['compensation'] = comp
    return result, True


@app.post('/api/upgrade/compensation/claim')
@login_required
def upgrade_compensation_claim():
    data = request.get_json(silent=True) or {}
    spin_id = str(data.get('spin_id') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', spin_id):
        return error('Некорректная компенсация.', 400)
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT user_id,won,result_json FROM upgrade_spins WHERE id=?' +
                         (' FOR UPDATE' if DATABASE_URL else ''), (spin_id,)).fetchone()
        if not row or int(row['user_id']) != int(session['uid']):
            db.rollback()
            return error('Компенсация не найдена.', 404)
        try:
            result = json.loads(row['result_json'] or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            db.rollback()
            return error('Данные компенсации повреждены.', 409)
        if bool(row['won']):
            db.rollback()
            return error('У выигрышного апгрейда нет компенсации.', 409)
        comp = dict(result.get('compensation') or {})
        if not comp.get('reward') or not comp.get('deferred'):
            db.rollback()
            return error('Для этой игры компенсация недоступна.', 409)
        result, newly_claimed = claim_upgrade_loss_compensation(
            db, session['uid'], spin_id, result)
        db.execute('UPDATE upgrade_spins SET result_json=? WHERE id=?',
                   (json.dumps(result, ensure_ascii=False), spin_id))
        db.commit()
    finally:
        db.close()

    promo_code = (((result.get('compensation') or {}).get('promo') or {}).get('code'))
    if newly_claimed and promo_code:
        notify_promo_async(session['uid'], promo_code, 'bonuses')
    return jsonify(ok=True, newly_claimed=newly_claimed,
                   compensation=result.get('compensation') or {}, user=profile())
'''

app = app[:start] + backend + app[end:]

js_start = tpl.find("let compensationResult=null,compensationPhase='idle',compensationResizeObserver=null;")
js_end = tpl.find("\n\nvar upgradeFast=false;", js_start)
if js_start < 0 or js_end < 0:
    raise SystemExit("Upgrade compensation frontend markers not found")

frontend = r'''let compensationResult=null,compensationPhase='idle',compensationResizeObserver=null;
function renderUpgradeCompensation(result){
  compensationResult=result;
  compensationPhase=(!result.won&&result.compensation?.reward)?'ready':'idle';
  compensationResizeObserver?.disconnect();
  $('upgradeResultCard').classList.remove('compensation-open');
  $('upgradeCompensation').replaceChildren();
  $('upgradeCompensation').classList.add('hidden');
  $('upgradeResultImage').classList.remove('hidden');
  $('upgradeResultText').classList.remove('hidden');
  $('closeUpgradeResult').disabled=false;
  $('closeUpgradeResult').textContent=compensationPhase==='ready'?'Открыть компенсацию':'Продолжить';
}
function compensationCell(prize){
  let cell=document.createElement('div');
  cell.className='comp-reel-cell '+(prize?.type||'gift');
  let img=new Image;
  img.src=prize?.image_url||TON;
  img.alt=prize?.name||'TON';
  img.onerror=()=>{img.onerror=null;img.src=TON};
  if(prize?.type==='balance'){
    let value=document.createElement('div');value.className='comp-ton-value';
    let amount=document.createElement('b');amount.textContent=money(prize.amount);
    value.append(amount,img);cell.append(value);
    cell.setAttribute('aria-label',money(prize.amount)+' TON');
  }else if(prize?.type==='tickets'){
    let tag=document.createElement('small');tag.textContent='Билеты';
    let value=document.createElement('div');value.className='comp-ton-value';
    let amount=document.createElement('b');amount.textContent=''+Number(prize.tickets||0);
    value.append(amount);cell.append(tag,value);
  }else{
    if(prize?.type==='wager_gift'||prize?.type==='promo'){
      let tag=document.createElement('small');
      tag.textContent=prize.type==='wager_gift'?('Отыгрыш ×'+prize.wager_multiplier):'Промокод';
      cell.append(tag);
    }
    let label=document.createElement('b');label.textContent=prize?.name||'Подарок';
    cell.append(img,label);
  }
  return cell;
}
function renderCompensationPrize(prize){
  let wrap=document.createElement('div');wrap.className='comp-final-prize '+(prize?.type||'gift');
  let visual=document.createElement('div');visual.className='comp-final-art';
  if(prize?.type==='balance'){
    let amount=document.createElement('b');amount.textContent=money(prize.amount);
    let icon=new Image;icon.src=TON;icon.alt='TON';visual.append(amount,icon);
  }else if(prize?.type==='tickets'){
    let amount=document.createElement('b');amount.textContent=''+Number(prize.tickets||0);visual.append(amount);
  }else{
    let img=new Image;img.src=prize?.image_url||TON;img.alt=prize?.name||'Подарок';
    img.onerror=()=>{img.onerror=null;img.src=TON};visual.append(img);
  }
  let text=document.createElement('div');text.className='comp-final-text';
  let title=document.createElement('strong'),detail=document.createElement('span');
  if(prize?.type==='balance'){title.textContent='Начислено на баланс';detail.textContent='+'+money(prize.amount)+' TON'}
  else if(prize?.type==='tickets'){title.textContent='Билеты розыгрыша';detail.textContent='+'+Number(prize.tickets||0).toLocaleString('ru-RU')+' билетов'}
  else if(prize?.type==='wager_gift'){title.textContent=prize.name||'Подарок';detail.textContent='Добавлено в инвентарь · отыгрыш ×'+prize.wager_multiplier}
  else if(prize?.type==='promo'){title.textContent=prize.name||'Промокод';detail.textContent='Персональный промокод на подарок'}
  else{title.textContent=prize?.name||'Подарок';detail.textContent='Добавлено в инвентарь'}
  text.append(title,detail);
  if(prize?.code){let code=document.createElement('code');code.textContent=prize.code;text.append(code)}
  wrap.append(visual,text);return wrap;
}
async function spinCompensation(){
  if(compensationPhase!=='ready'||!compensationResult?.compensation?.reward)return;
  compensationPhase='spinning';
  compensationResizeObserver?.disconnect();
  let card=$('upgradeResultCard'),comp=compensationResult.compensation,box=$('upgradeCompensation'),button=$('closeUpgradeResult');
  let reward=comp.reward;
  card.classList.add('compensation-open');
  button.disabled=true;button.textContent='Рулетка крутится…';
  $('upgradeResultImage').classList.add('hidden');$('upgradeResultText').classList.add('hidden');
  $('upgradeResultHeading').textContent='Ваша компенсация';
  box.classList.remove('hidden');
  box.style.opacity='1';box.style.transform='none';

  let viewport=document.createElement('div');viewport.className='comp-reel';
  viewport.setAttribute('aria-label','Линия по центру показывает выигрыш');
  let track=document.createElement('div');track.className='comp-reel-track';
  let items=(comp.reel?.length?comp.reel:[reward]).filter(Boolean);
  if(!items.length)items=[reward];
  let giftItems=items.filter(x=>x?.type&&x.type!=='balance'&&x.type!=='tickets');
  let stop=52+Math.floor(Math.random()*8);
  for(let i=0;i<stop+6;i++){
    let pool=(i%4===1&&giftItems.length)?giftItems:items;
    let prize=i===stop?reward:pool[Math.floor(Math.random()*pool.length)];
    let cell=compensationCell(prize);cell.dataset.stop=i===stop?'1':'0';track.append(cell);
  }
  viewport.append(track);box.replaceChildren(viewport);
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));

  let visibleCells=()=>window.matchMedia('(max-width: 520px)').matches?2:3;
  let cellWidth=0,targetPx=0;
  let measure=()=>{
    let width=Math.max(1,viewport.getBoundingClientRect().width);
    let count=visibleCells();
    cellWidth=width/count;
    track.style.setProperty('--cell-width',cellWidth+'px');
    targetPx=width/2-(stop+.5)*cellWidth;
  };
  measure();
  if(window.ResizeObserver){
    compensationResizeObserver=new ResizeObserver(measure);
    compensationResizeObserver.observe(viewport);
  }

  let reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  let duration=reduced?350:(upgradeFast?1350:4300);
  try{
    if(track.animate){
      let animation=track.animate(
        [{transform:'translate3d(0,0,0)'},{transform:'translate3d('+targetPx+'px,0,0)'}],
        {duration,easing:'cubic-bezier(.08,.68,.12,1)',fill:'forwards'}
      );
      await animation.finished.catch(()=>{});
      animation.cancel();
    }else{
      track.style.transition='transform '+duration+'ms cubic-bezier(.08,.68,.12,1)';
      requestAnimationFrame(()=>track.style.transform='translate3d('+targetPx+'px,0,0)');
      await new Promise(r=>setTimeout(r,duration+40));
    }
  }finally{
    measure();
    track.style.transition='none';
    track.style.transform='translate3d('+targetPx+'px,0,0)';
  }

  track.children[stop]?.classList.add('selected');
  await new Promise(r=>setTimeout(r,reduced?80:380));
  compensationResizeObserver?.disconnect();
  button.textContent='Начисляем…';

  try{
    let claimed=await api('/api/upgrade/compensation/claim',{
      method:'POST',
      body:JSON.stringify({spin_id:compensationResult.id}),
      timeoutMs:12000
    });
    compensationResult.compensation=claimed.compensation||comp;
    reward=compensationResult.compensation.reward||reward;
    setUser(claimed.user);
    box.replaceChildren(renderCompensationPrize(reward));
    box.style.opacity='1';box.style.transform='none';
    if(reward.type==='promo')loadMyPromos().catch(()=>{});
    if(reward.type==='gift'||reward.type==='wager_gift')loadInventory().catch(()=>{});
    compensationPhase='done';
    button.disabled=false;button.textContent='Готово';
  }catch(e){
    compensationPhase='ready';
    button.disabled=false;button.textContent='Повторить получение';
    toast(e.message||'Не удалось начислить компенсацию');
  }
}'''

tpl = tpl[:js_start] + frontend + tpl[js_end:]

# Add a final CSS guard so later legacy rules cannot squeeze or offset the reel.
css_guard = r'''
<style id="upgrade-compensation-repair">
#upgradeResultModal{z-index:65000!important}
#upgradeResultModal #upgradeResultCard.compensation-open{width:min(560px,calc(100vw - 24px))!important;max-width:560px!important;overflow:hidden!important;padding:22px 14px 16px!important}
#upgradeCompensation{display:block;width:100%!important;min-width:0!important;overflow:hidden!important;margin:0 0 14px!important}
#upgradeCompensation.hidden{display:none!important}
#upgradeCompensation .comp-reel{width:100%!important;max-width:100%!important;overflow:hidden!important}
#upgradeCompensation .comp-reel-track{display:flex!important;width:max-content!important;min-width:max-content!important;height:100%!important;gap:0!important}
#upgradeCompensation .comp-reel-cell{flex:0 0 var(--cell-width)!important;width:var(--cell-width)!important;min-width:var(--cell-width)!important;max-width:var(--cell-width)!important}
#upgradeResultCard.compensation-open #closeUpgradeResult{width:100%!important;margin-top:4px!important}
@media(max-width:520px){
  #upgradeResultModal{padding:10px!important;align-items:center!important}
  #upgradeResultModal #upgradeResultCard.compensation-open{width:100%!important;max-height:calc(100dvh - 20px)!important;padding:18px 10px 14px!important;border-radius:22px!important}
  #upgradeResultCard.compensation-open h2{margin-bottom:14px!important}
  #upgradeCompensation .comp-reel{height:174px!important}
}
</style>
'''
if 'id="upgrade-compensation-repair"' not in tpl:
    head_end = tpl.find('</head>')
    if head_end < 0:
        raise SystemExit("head marker not found")
    tpl = tpl[:head_end] + css_guard + tpl[head_end:]

app_path.write_text(app, encoding="utf-8")
tpl_path.write_text(tpl, encoding="utf-8")
print("Upgrade compensation repaired")
