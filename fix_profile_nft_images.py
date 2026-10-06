from pathlib import Path

app_path = Path(__file__).with_name("app.py")
tpl_path = Path(__file__).with_name("templates") / "index.html"

app = app_path.read_text(encoding="utf-8")
tpl = tpl_path.read_text(encoding="utf-8")

old_backend = """    return dict(id=row['id'], gift_id=row['gift_id'], name=row['gift_name'],
                image_url=row['image_url'], price_ton=row['floor_price']/100,
                source=row['source'], created_at=row['created_at'],
                external_url=optional('external_url'), fragment_url=optional('external_url'),"""
new_backend = """    external_url = str(optional('external_url') or '')
    image_url = safe_image(row['image_url'])
    nft_match = re.search(r'/(?:nft|gift)/([A-Za-z0-9_-]+-\\d+)(?:/|$|[?#])', external_url, re.I)
    if nft_match:
        exact_nft_image = f"https://nft.fragment.com/gift/{nft_match.group(1).lower()}.webp"
        # Relayr deposits should always prefer the exact collectible artwork,
        # not a generic collection image from the Portal catalog.
        if str(row['source'] or '') == 'gift_deposit' or not image_url:
            image_url = exact_nft_image
    return dict(id=row['id'], gift_id=row['gift_id'], name=row['gift_name'],
                image_url=image_url, price_ton=row['floor_price']/100,
                source=row['source'], created_at=row['created_at'],
                external_url=external_url, fragment_url=external_url,"""

if old_backend in app:
    app = app.replace(old_backend, new_backend, 1)
elif new_backend not in app:
    raise SystemExit("inventory_item marker not found")

old_front = """function itemCard(item,clickable=false){let bgLabel=giftBackgroundLabel(item),bgClass=giftBackgroundClass(bgLabel);let card=document.createElement('article');card.className='gift '+giftTier(item.price_ton)+(clickable?' clickable':'')+(item.promo_locked?' promo-wager':'')+(item.deposit_mirror?' deposit-mirror':'')+(bgClass?' '+bgClass:'');if(bgLabel&&!blackBackgroundsEnabled)card.style.setProperty('display','none','important');let art=document.createElement('div');art.className='gift-art';art.textContent='✦';if(item.image_url){let img=new Image;img.loading='lazy';img.src=item.image_url;img.alt=giftDisplayName(item);img.onerror=()=>{img.onerror=null;img.src='/static/img/gift.svg'};art.replaceChildren(img)}"""
new_front = """function giftImageSources(item){let link=String(item?.external_url||item?.fragment_url||'');let slug=link.match(/\\/(?:nft|gift)\\/([a-z0-9_-]+-\\d+)(?:\\/|$|[?#])/i)?.[1]?.toLowerCase();return[slug?'https://nft.fragment.com/gift/'+slug+'.webp':'',item?.image_url,'/static/img/gift.png','/static/img/gift.svg'].filter((url,i,all)=>url&&all.indexOf(url)===i)}
function applyGiftImage(img,item){let sources=giftImageSources(item),next=0;img.onerror=()=>{if(next<sources.length)img.src=sources[next++];else img.onerror=null};img.src=sources[next++]||'/static/img/gift.png'}
function itemCard(item,clickable=false){let bgLabel=giftBackgroundLabel(item),bgClass=giftBackgroundClass(bgLabel);let card=document.createElement('article');card.className='gift '+giftTier(item.price_ton)+(clickable?' clickable':'')+(item.promo_locked?' promo-wager':'')+(item.deposit_mirror?' deposit-mirror':'')+(bgClass?' '+bgClass:'');if(bgLabel&&!blackBackgroundsEnabled)card.style.setProperty('display','none','important');let art=document.createElement('div');art.className='gift-art';art.textContent='✦';{let img=new Image;img.loading='lazy';img.alt=giftDisplayName(item);applyGiftImage(img,item);art.replaceChildren(img)}"""

if old_front in tpl:
    tpl = tpl.replace(old_front, new_front, 1)
elif "function giftImageSources(item)" not in tpl:
    raise SystemExit("itemCard marker not found")

old_modal = """function openGift(item){let seq=++giftModalSeq;selectedGift=item;$('giftModalImage').src=item.image_url||TON;$('giftModalImage').onerror=()=>{$('giftModalImage').onerror=null;$('giftModalImage').src='/static/img/gift.svg'};"""
new_modal = """function openGift(item){let seq=++giftModalSeq;selectedGift=item;applyGiftImage($('giftModalImage'),item);"""

if old_modal in tpl:
    tpl = tpl.replace(old_modal, new_modal, 1)
elif new_modal not in tpl:
    raise SystemExit("openGift marker not found")

app_path.write_text(app, encoding="utf-8")
tpl_path.write_text(tpl, encoding="utf-8")
print("NFT profile artwork fallback patched")
