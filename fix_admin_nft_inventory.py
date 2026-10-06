from pathlib import Path

root = Path(__file__).resolve().parent
app_path = root / "app.py"
tpl_path = root / "templates" / "index.html"
app = app_path.read_text(encoding="utf-8")
tpl = tpl_path.read_text(encoding="utf-8")

start_marker = "@app.post('/api/admin/users/<int:user_id>/inventory')\n@admin_required\ndef admin_add_inventory(user_id):"
end_marker = "\n\n@app.delete('/api/admin/users/<int:user_id>/inventory/<int:item_id>')"
start = app.find(start_marker)
end = app.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit("admin inventory endpoint markers not found")

endpoint = r'''@app.post('/api/admin/users/<int:user_id>/inventory')
@admin_required
def admin_add_inventory(user_id):
    data = request.get_json(silent=True) or {}
    nft_url = str(data.get('fragment_url') or data.get('telegram_url') or '').strip()

    if nft_url:
        telegram_match = re.fullmatch(r'https://t\.me/nft/([A-Za-z0-9_-]+-\d+)/?', nft_url, re.I)
        if telegram_match:
            nft_url = 'https://fragment.com/gift/' + telegram_match.group(1)
        try:
            nft = fragment_gift_from_url(nft_url, True, refresh=True, allow_missing_price=True)
            portal_price, portal_source = _fragment_portal_fallback_price(
                nft.get('collection_name') or re.sub(r'\s*#\s*\d+.*$', '', str(nft.get('gift_name') or '')).strip(),
                nft.get('fragment_model') or '',
                nft.get('fragment_backdrop') or ''
            )
        except (ValueError, TypeError, requests.RequestException) as exc:
            return error(str(exc) or 'Не удалось получить данные NFT.')

        portal_price = max(0, int(portal_price or 0))
        if portal_price <= 0:
            return error('Portal не вернул цену для этого NFT. Подарок не добавлен.')

        external_url = str(nft.get('fragment_url') or '')
        gift_id = str(nft.get('gift_id') or '')
        gift_name = str(nft.get('gift_name') or 'Telegram NFT')[:140]
        image_url = safe_image(nft.get('image_url'))
        number = str(nft.get('fragment_number') or '')
        model = str(nft.get('fragment_model') or '')
        backdrop = str(nft.get('fragment_backdrop') or '')
        symbol = str(nft.get('fragment_symbol') or '')
        animation_url = safe_image(nft.get('animation_url'))

        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                db.rollback()
                return error('Пользователь не найден.', 404)
            if external_url and db.execute(
                'SELECT 1 FROM inventory WHERE user_id=? AND external_url=? LIMIT 1',
                (user_id, external_url)).fetchone():
                db.rollback()
                return error('Этот конкретный NFT уже есть у пользователя.', 409)

            cur = db.execute("""INSERT INTO inventory(
                user_id,gift_id,gift_name,image_url,floor_price,source,external_url,
                fragment_number,fragment_model,fragment_backdrop,fragment_symbol,
                price_source,animation_url,source_label,deposit_mirror)
                VALUES(?,?,?,?,?,'admin_nft',?,?,?,?,?,?,?,?,0)""",
                (user_id, gift_id, gift_name, image_url, portal_price, external_url,
                 number, model, backdrop, symbol, str(portal_source or 'Portal'),
                 animation_url, 'Выдано администратором · Telegram NFT'))
            item_id = cur.lastrowid
            db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                       (session['uid'], user_id, 'gift_add_nft',
                        json.dumps({'inventory_id': item_id, 'gift': gift_name, 'url': external_url,
                                    'price': portal_price, 'price_source': portal_source}, ensure_ascii=False)))
            log_event(db, user_id, 'admin_gift_add', gift_name=gift_name,
                      fragment_number=number, price_ton=portal_price/100,
                      price_source=portal_source, admin_id=session['uid'])
            db.commit()
            row = db.execute('SELECT * FROM inventory WHERE id=?', (item_id,)).fetchone()
        return jsonify(ok=True, item=inventory_item(row), source='nft')

    gift_id = str(data.get('gift_id', '')).strip()
    try:
        gift = next((gift for gift in read_catalog()['gifts'] if str(gift.get('id')) == gift_id), None)
    except (OSError, ValueError, TypeError):
        gift = None
    if not gift:
        return error('Выберите подарок из каталога Portal.')
    try:
        price = parse_amount(gift['price_ton']) if gift.get('price_ton') is not None else 0
    except (KeyError, ValueError, TypeError, InvalidOperation):
        price = 0
    if price <= 0:
        return error('Portal не вернул цену этого подарка.')
    with connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            return error('Пользователь не найден.', 404)
        cur = db.execute("""INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source,price_source,source_label)
                            VALUES(?,?,?,?,?,'admin',?,'Выдано администратором')""",
                         (user_id, gift_id, str(gift['name']),
                          safe_image(gift.get('image_url') or gift.get('portal_image_url')),
                          price, str(gift.get('price_source') or 'Portal')))
        db.execute('INSERT INTO admin_log(admin_id,user_id,action,details) VALUES(?,?,?,?)',
                   (session['uid'], user_id, 'gift_add', gift_id))
        log_event(db,user_id,'admin_gift_add',gift_name=str(gift['name']),price_ton=price/100,admin_id=session['uid'])
        row = db.execute('SELECT * FROM inventory WHERE id=?', (cur.lastrowid,)).fetchone()
    return jsonify(ok=True, item=inventory_item(row), source='catalog')


@app.post('/api/admin/users/<int:user_id>/inventory/nft-preview')
@admin_required
def admin_user_nft_preview(user_id):
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or '').strip()
    telegram_match = re.fullmatch(r'https://t\.me/nft/([A-Za-z0-9_-]+-\d+)/?', url, re.I)
    if telegram_match:
        url = 'https://fragment.com/gift/' + telegram_match.group(1)
    try:
        gift = fragment_gift_from_url(url, True, refresh=True, allow_missing_price=True)
        portal_price, portal_source = _fragment_portal_fallback_price(
            gift.get('collection_name') or re.sub(r'\s*#\s*\d+.*$', '', str(gift.get('gift_name') or '')).strip(),
            gift.get('fragment_model') or '',
            gift.get('fragment_backdrop') or ''
        )
    except (ValueError, TypeError, requests.RequestException) as exc:
        return error(str(exc) or 'Не удалось получить данные NFT.')
    portal_price = max(0, int(portal_price or 0))
    if portal_price <= 0:
        return error('Portal не вернул цену для этого NFT.')
    return jsonify(ok=True, gift=dict(
        name=gift.get('gift_name') or 'Telegram NFT',
        image_url=safe_image(gift.get('image_url')),
        price_ton=portal_price/100,
        price_source=portal_source or 'Portal',
        fragment_url=gift.get('fragment_url') or '',
        fragment_number=gift.get('fragment_number') or '',
        model=gift.get('fragment_model') or '',
        backdrop=gift.get('fragment_backdrop') or '',
        symbol=gift.get('fragment_symbol') or ''
    ))
'''
app = app[:start] + endpoint + app[end:]

old_panel = '<div class="panel stack"><h2>Добавить подарок</h2><select id="adminGift" class="text-input"></select><button id="addGift" class="mini-btn">Добавить</button></div>'
new_panel = '<div class="panel stack admin-gift-add-panel"><h2>Добавить подарок</h2><label class="caption" for="adminGift">Из каталога Portal</label><select id="adminGift" class="text-input"></select><button id="addGift" class="mini-btn" type="button">Добавить из Portal</button><div class="admin-gift-or"><span>или</span></div><label class="caption" for="adminNftUrl">Telegram / Fragment NFT</label><input id="adminNftUrl" class="text-input" type="url" autocomplete="off" placeholder="https://t.me/nft/PlushPepe-12345"><button id="previewAdminNft" class="secondary" type="button">Определить NFT и цену</button><div id="adminNftPreview" class="admin-nft-preview hidden"></div><button id="addAdminNft" class="primary hidden" type="button">Добавить NFT в инвентарь</button><small class="muted">Название, PNG, номер и характеристики берутся из Telegram/Fragment. Цена определяется только по Portal.</small></div>'
if old_panel not in tpl:
    raise SystemExit("admin gift panel marker not found")
tpl = tpl.replace(old_panel, new_panel, 1)

old_handler = "$('addGift').onclick=async()=>{try{await api(`/api/admin/users/${selectedUser}/inventory`,{method:'POST',body:JSON.stringify({gift_id:$('adminGift').value})});loadUser();if(selectedUser===user.id)loadInventory();toast('Подарок добавлен')}catch(e){toast(e.message)}};"
new_handler = "$('addGift').onclick=async()=>{let b=$('addGift');b.disabled=true;try{await api(`/api/admin/users/${selectedUser}/inventory`,{method:'POST',body:JSON.stringify({gift_id:$('adminGift').value})});await loadUser();if(selectedUser===user.id)await loadInventory();toast('Подарок добавлен')}catch(e){toast(e.message)}finally{b.disabled=false}};let adminNftResolved=null;function resetAdminNftPreview(){adminNftResolved=null;$('adminNftPreview').replaceChildren();$('adminNftPreview').classList.add('hidden');$('addAdminNft').classList.add('hidden')}$('adminNftUrl').oninput=resetAdminNftPreview;$('previewAdminNft').onclick=async()=>{let b=$('previewAdminNft'),url=$('adminNftUrl').value.trim();if(!url)return toast('Вставьте ссылку Telegram или Fragment');b.disabled=true;resetAdminNftPreview();try{let d=await api(`/api/admin/users/${selectedUser}/inventory/nft-preview`,{method:'POST',body:JSON.stringify({url})});adminNftResolved=d.gift;let p=$('adminNftPreview');let img=new Image;img.src=d.gift.image_url||'/static/img/gift.png';img.alt=d.gift.name;img.onerror=()=>{img.onerror=null;img.src='/static/img/gift.svg'};let meta=document.createElement('div'),name=document.createElement('strong'),price=document.createElement('b'),info=document.createElement('small');name.textContent=d.gift.name;price.textContent=money(d.gift.price_ton)+' TON · '+(d.gift.price_source||'Portal');info.textContent=[d.gift.fragment_number?'#'+d.gift.fragment_number:'',d.gift.model,d.gift.backdrop].filter(Boolean).join(' · ');meta.append(name,price);if(info.textContent)meta.append(info);p.append(img,meta);p.classList.remove('hidden');$('addAdminNft').classList.remove('hidden')}catch(e){toast(e.message)}finally{b.disabled=false}};$('addAdminNft').onclick=async()=>{let b=$('addAdminNft'),url=$('adminNftUrl').value.trim();if(!adminNftResolved||!url)return toast('Сначала определите NFT');b.disabled=true;try{let d=await api(`/api/admin/users/${selectedUser}/inventory`,{method:'POST',body:JSON.stringify({fragment_url:url})});$('adminNftUrl').value='';resetAdminNftPreview();await loadUser();if(selectedUser===user.id)await loadInventory();toast(`NFT добавлен · ${money(d.item?.price_ton||0)} TON`)}catch(e){toast(e.message)}finally{b.disabled=false}};"
if old_handler not in tpl:
    raise SystemExit("addGift handler marker not found")
tpl = tpl.replace(old_handler, new_handler, 1)

if 'id="admin-nft-inventory-style"' not in tpl:
    css = '<style id="admin-nft-inventory-style">.admin-gift-or{display:flex;align-items:center;gap:10px;color:#748797;font-size:10px}.admin-gift-or:before,.admin-gift-or:after{content:"";height:1px;flex:1;background:#2e3440}.admin-nft-preview{display:grid;grid-template-columns:74px minmax(0,1fr);gap:12px;align-items:center;padding:10px;border:1px solid #343b47;border-radius:14px;background:#171a20}.admin-nft-preview img{width:74px;height:74px;object-fit:contain;border-radius:12px;background:#101217}.admin-nft-preview div{display:grid;gap:5px;min-width:0}.admin-nft-preview strong{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.admin-nft-preview b{font-size:12px;color:#7bd6b4}.admin-nft-preview small{font-size:10px;color:#8b9aa8;line-height:1.35}</style>'
    head_end = tpl.find('</head>')
    if head_end < 0:
        raise SystemExit("head marker missing")
    tpl = tpl[:head_end] + css + tpl[head_end:]

app_path.write_text(app, encoding="utf-8")
tpl_path.write_text(tpl, encoding="utf-8")
print("Admin NFT inventory grant patched")
