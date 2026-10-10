"""Run with python -m unittest discover -s tests -v. Uses an isolated SQLite DB."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from urllib.parse import urlencode

_data = tempfile.TemporaryDirectory(prefix='gemdrop-tests-')
os.environ['DATA_DIR'] = _data.name
for _key in ('BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'WEBAPP_URL', 'RENDER_EXTERNAL_URL', 'DATABASE_URL'):
    os.environ[_key] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as m


class RegressionTests(unittest.TestCase):
    serial = 800000

    def setUp(self):
        RegressionTests.serial += 1
        self.uid = RegressionTests.serial
        self.client = m.app.test_client()
        m.app.config['TESTING'] = True
        m.fragment_preview_cache.clear()
        with m.connect() as db:
            db.execute('INSERT INTO users(id,name,username,balance) VALUES(?,?,?,?)',
                       (self.uid, 'Test', f'qa{self.uid}', 10000))
            db.execute("DELETE FROM app_documents WHERE name IN ('section_settings','portal_catalog','game_modes')")
        with self.client.session_transaction() as session:
            session['uid'] = self.uid
        m.save_document('gift_display_settings', {'black_backgrounds_enabled': True})

    def test_black_background_switch_defaults_off_and_restores_inventory(self):
        with m.connect() as db:
            db.execute("DELETE FROM app_documents WHERE name='gift_display_settings'")
            db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,floor_price,source,price_source) VALUES(?, 'qa:background:black', 'QA (Black)', 1200, 'test', 'Portal · фон')", (self.uid,))
        gifts = [{'id':'qa','name':'QA','price_ton':4},
                 {'id':'qa:background:black','name':'QA (Black)','background_label':'Black','price_ton':12,'price_source':'Portal · фон'}]
        m.save_document('portal_catalog', {'gifts':gifts})
        self.assertFalse(self.client.get('/api/ui/settings').get_json()['black_backgrounds_enabled'])
        self.assertEqual(len(m.read_catalog()['gifts']),1)
        self.assertEqual(len(m.read_catalog(include_hidden=True)['gifts']),2)
        self.assertEqual(self.client.get('/api/inventory').get_json()['items'],[])
        self.post('/api/admin/section-settings', {'black_backgrounds_enabled':True},403)
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post('/api/admin/section-settings', {'black_backgrounds_enabled':'true'},400)
            self.post('/api/admin/section-settings', {'black_backgrounds_enabled':True})
        self.assertEqual(len(self.client.get('/api/inventory').get_json()['items']),1)
        self.assertEqual(len(m.read_catalog()['gifts']),2)
        self.assertTrue(m.section_settings()['mines'])

    def test_unconfirmed_owned_black_price_is_hidden_even_when_enabled(self):
        self.assertEqual(m.visible_gifts([{'gift_id':'old:background:black','name':'Old (Black)',
                        'price_ton':4,'price_source':'Portal · коллекция'}]),[])

    def test_black_override_drop_is_hidden_in_public_profile(self):
        with m.connect() as db:
            db.execute("UPDATE users SET max_drop_override_name='QA (Onyx Black)',max_drop_override_price=1800 WHERE id=?",(self.uid,))
        m.save_document('gift_display_settings', {'black_backgrounds_enabled':False})
        self.assertIsNone(self.client.get(f'/api/users/{self.uid}/profile').get_json()['max_drop'])
        m.save_document('gift_display_settings', {'black_backgrounds_enabled':True})
        self.assertEqual(self.client.get(f'/api/users/{self.uid}/profile').get_json()['max_drop']['price_ton'],18)

    def test_level_black_preview_is_hidden_while_admin_configuration_survives(self):
        reward = {'type':'gift','gift_id':'qa:background:black','gift_name':'QA (Black)','gift_price':1200}
        m.save_document('gift_display_settings', {'black_backgrounds_enabled':False})
        self.assertEqual(m.public_level_reward(reward,include_hidden=False),{'type':'none'})
        self.assertEqual(m.public_level_reward(reward)['gift_price_ton'],12)
        self.assertEqual(reward['gift_price'],1200)

    def test_switch_hides_giveaway_prizes_and_winners_without_deleting_data(self):
        m.save_document('portal_catalog', {'gifts':[{'id':'qa:background:black','name':'QA (Black)',
            'background_label':'Black','price_ton':12,'price_source':'Portal · фон','image_url':'https://example.com/gift.png'}]})
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            item = self.post('/api/admin/giveaways', {'title':'QA','duration_minutes':60,
                             'prizes':[{'gift_id':'qa:background:black'}]})['item']
            with m.connect() as db:
                db.execute('INSERT INTO giveaway_entries(giveaway_id,user_id,tickets) VALUES(?,?,7)',(item['id'],self.uid))
            self.post(f"/api/admin/giveaways/{item['id']}/finish")
            self.post('/api/admin/section-settings', {'black_backgrounds_enabled':False})
            hidden = self.client.get('/api/admin/giveaways').get_json()['items'][0]
            self.assertEqual(hidden['prizes'],[])
            self.assertEqual(hidden['winners'],[])
            self.assertEqual(self.client.get(f"/api/giveaways/{item['id']}").status_code,404)
            self.post('/api/admin/section-settings', {'black_backgrounds_enabled':True})
            restored = self.client.get(f"/api/giveaways/{item['id']}").get_json()['item']
            self.assertEqual(len(restored['prizes']),1)
            self.assertEqual(restored['winners'][0]['tickets'],7)

    def post(self, path, data=None, status=200):
        response = self.client.post(path, json=data or {})
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def balance(self):
        return self.client.get('/api/me').get_json()['user']['balance']

    def test_public_assets_and_auth_guard(self):
        guest = m.app.test_client()
        for path in ('/', '/static/css/gemdrop-studio.css'):
            with guest.get(path) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(guest.get('/api/me').status_code, 401)
        self.assertEqual(self.client.get('/api/admin/users').status_code, 403)

    def test_background_variants_require_current_separate_prices(self):
        base = {'id': 'pepe', 'name': 'Plush Pepe', 'price_ton': '4.00'}
        filters = {'data': {'floor_prices': {'backdrops': [
            {'name': 'Black', 'floor_price': '12.34'},
            {'name': 'Onyx Black', 'floor_price': '18.90'}]}}}
        entries = m.portal_catalog_entries(base, {}, {}, filters)
        self.assertEqual([g['price_ton'] for g in entries], ['4.00', '12.34', '18.90'])
        previous = {g['id']: g for g in entries}
        refreshed = m.portal_catalog_entries(base, {}, previous, {'backdrops': {'Black': 0}})
        self.assertEqual([g['price_ton'] for g in refreshed], ['4.00'])

    def test_portal_live_filter_shape_without_backdrop_prices(self):
        from unittest.mock import Mock
        response = Mock(status_code=200)
        response.json.return_value = {'collections': {'plushpepe': {
            'models': [{'name':'Black', 'floor_price':'9500'}],
            'backdrops': [{'name':'Black','rarityPermille':2}, {'name':'Onyx Black','rarityPermille':2}]}},
            'collection_floor_price': '4000'}
        http = Mock()
        http.get.return_value = response
        filters = m.portal_get_collection_filters(http, '', ['Plush Pepe'])
        self.assertIn('plushpepe', filters)
        base = {'id':'pepe','name':'Plush Pepe','price_ton':'4000'}
        self.assertEqual(m.portal_catalog_entries(base, {}, {}, filters['plushpepe']), [base])

    def test_legacy_collection_price_variants_are_hidden(self):
        base = {'id':'pepe','name':'Plush Pepe','price_ton':'4'}
        bad = dict(base,id='pepe:background:black',background_label='Black',price_source='Portal · коллекция')
        good = dict(base,id='pepe:background:onyx-black',background_label='Onyx Black',price_ton='18',price_source='Portal · фон')
        m.save_document('portal_catalog', {'gifts':[base,bad,good]})
        self.assertEqual(m.read_catalog()['gifts'], [base,good])
        self.assertIsNone(m.upgrade_target(bad['id']))

    def test_fragment_preserves_exact_image_and_animation(self):
        from unittest.mock import Mock
        exact = 'https://nft.fragment.com/gift/plushpepe-123.webp'
        metadata = Mock(ok=True, content=b'{}')
        metadata.json.return_value = {'name':'Plush Pepe #123','image':exact,
                                     'animation_url':'https://example.com/gift.mp4','price':12.34}
        telegram = Mock(ok=True, text='<meta property="og:image" content="https://example.com/social.jpg">')
        with patch.object(m.requests, 'get', side_effect=[metadata,telegram]):
            gift = m.fragment_gift_from_url('https://t.me/nft/PlushPepe-123')
        self.assertEqual(gift['image_url'], exact)
        self.assertEqual(gift['animation_url'], 'https://example.com/gift.mp4')

    def test_black_fragment_cannot_use_model_or_collection_price(self):
        filters = {'models':[{'name':'Pumpkin','floor_price':9500}], 'backdrops':[{'name':'Onyx Black'}]}
        self.assertEqual(m._portal_filter_trait_floor(filters,'Pumpkin','Onyx Black'), (0,''))
        with patch.object(m,'portal_get_collection_filters',return_value={'plushpepe':filters}), \
             patch.object(m,'read_catalog',return_value={'gifts':[{'name':'Plush Pepe','price_ton':4000}]}):
            self.assertEqual(m._fragment_portal_fallback_price('Plush Pepe','Pumpkin','Onyx Black'),(0,''))
        self.assertEqual(m._portal_filter_trait_floor({'backdrops':[{'name':'Onyx','floor_price':'18.90'}]},
                         'Pumpkin','Onyx Black'),(1890,'Portal · фон'))

    def test_fragment_without_black_price_is_rejected(self):
        from unittest.mock import Mock
        metadata = Mock(ok=True,content=b'{}')
        metadata.json.return_value = {'attributes':[{'trait_type':'Backdrop','value':'Black'}]}
        page = Mock(ok=True,content=b'<html>',text='<html></html>')
        with patch.object(m.requests,'get',side_effect=[metadata,page,page]), \
             patch.object(m,'_fragment_portal_fallback_price',return_value=(0,'')):
            with self.assertRaises(ValueError):
                m.fragment_gift_from_url('https://t.me/nft/PlushPepe-123')

    def test_black_and_onyx_aliases_in_wrapped_backdrop_prices(self):
        payload = {'data': {'backdrops': [
            {'backdrop_name': 'BLACK', 'stats': {'floor_price': '12.34'}},
            {'backdrop': {'name': 'Onyx'}, 'pricing': {'min_price': '18.90'}}]}}
        self.assertEqual(m.portal_background_variants(payload), {'Black': '12.34', 'Onyx Black': '18.90'})
        for name in ('Onyx', 'onyx_black', 'Onyx Black', 'ONYX-BLACK'):
            self.assertEqual(m.normalize_portal_background(name), 'Onyx Black')
        self.assertEqual(m.portal_background_variants({'attributes':[
            {'trait_type':'Model','value':'Black','floor_price':100},
            {'trait_type':'Backdrop','value':'Black','floor_price':12}]}), {'Black':'12.00'})

    def test_admin_edit_giveaway_preserves_prizes_and_validates_end(self):
        from datetime import datetime, timedelta, timezone
        m.save_document('portal_catalog', {'gifts':[{'id':'qa-edit','name':'QA','price_ton':2,
                        'image_url':'https://example.com/gift.png'}]})
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            giveaway = self.post('/api/admin/giveaways', {'title':'Before', 'duration_minutes':60,
                                'prizes':[{'gift_id':'qa-edit'}]})['item']
            path = f"/api/admin/giveaways/{giveaway['id']}"
            payload = {'title':'After', 'description':'Updated',
                       'ends_at':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()}
            response = self.client.put(path,json=payload)
            self.assertEqual(response.status_code,200,response.get_json())
            updated = response.get_json()['item']
            self.assertEqual(updated['title'],'After')
            self.assertEqual(updated['prizes'],giveaway['prizes'])
            payload['ends_at']='invalid'
            self.assertEqual(self.client.put(path,json=payload).status_code,400)
        self.assertEqual(self.client.put(path,json=payload).status_code,403)

    def test_fragment_fallback_matches_background_and_collection(self):
        gifts = [
            {'id':'black', 'name':'Plush Pepe (Black)', 'background_label':'Black', 'price_ton':12},
            {'id':'base', 'name':'Plush Pepe', 'price_ton':4},
            {'id':'onyx', 'name':'Plush Pepe (Onyx Black)', 'background_label':'Onyx Black', 'price_ton':18}]
        with patch.object(m, 'portal_get_collection_filters', return_value={}), \
             patch.object(m, 'read_catalog', return_value={'gifts':gifts}):
            self.assertEqual(m._fragment_portal_fallback_price('plushpepe', backdrop='Onyx Black'),
                             (1800, 'Portal · фон'))
            self.assertEqual(m._fragment_portal_fallback_price('plushpepe'), (400, 'Portal · коллекция'))

    def test_fragment_reads_listing_even_when_animation_exists(self):
        from unittest.mock import Mock
        metadata = Mock(ok=True, content=b'{}')
        metadata.json.return_value = {'name':'Plush Pepe #123', 'animation_url':'https://example.com/gift.mp4'}
        telegram = Mock(ok=True, text='<html></html>')
        fragment = Mock(ok=True, content=b'<html>', text='<div>Buy now 12.34 TON</div>')
        with patch.object(m.requests, 'get', side_effect=[metadata, telegram, fragment]), \
             patch.object(m, '_fragment_portal_fallback_price') as fallback:
            gift = m.fragment_gift_from_url('https://fragment.com/gift/PlushPepe-123')
        self.assertEqual(gift['fragment_number'], '123')
        self.assertEqual(gift['floor_price'], 1234)
        self.assertEqual(gift['price_source'], 'Fragment')
        fallback.assert_not_called()

    def test_admin_refresh_prizes_keeps_known_price_on_provider_failure(self):
        m.save_document('portal_catalog', {'gifts':[{'id':'qa-gift','name':'QA gift','price_ton':2,
                        'image_url':'https://example.com/gift.png'}]})
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            giveaway = self.post('/api/admin/giveaways', {'title':'QA', 'duration_minutes':60,
                                'prizes':[{'gift_id':'qa-gift'}]})['item']
            path = f"/api/admin/giveaways/{giveaway['id']}/refresh-prizes"
            refreshed = {'gift_name':'QA updated','image_url':'https://example.com/new.png', 'floor_price':0}
            with patch.object(m, 'catalog_giveaway_prize', return_value=refreshed):
                result = self.post(path)
            self.assertEqual(result['item']['prizes'][0]['price_ton'], 2)
            self.assertEqual(result['item']['prizes'][0]['name'], 'QA updated')
            self.post(f"/api/admin/giveaways/{giveaway['id']}/finish")
            self.post(path, status=400)
        self.post(path, status=403)

    def test_malformed_json_is_client_error(self):
        for body in ('[1]', 'null', '"text"', '{bad'):
            r = self.client.post('/api/game/start', data=body, content_type='application/json')
            self.assertEqual(r.status_code, 400)
        self.assertEqual(self.balance(), 100)

    def test_money_validation(self):
        for value in ('NaN', 'Infinity', '0.001', '1e100'):
            with self.assertRaises((ValueError, m.InvalidOperation)):
                m.parse_amount(value)
        self.assertEqual(m.parse_amount('12.34'), 1234)

    def test_invalid_bets_do_not_debit(self):
        for value in ('NaN', '0.001', '-1', '100.01', '1e100'):
            self.post('/api/game/start', {'mines': 3, 'bet': value}, 400)
        self.assertEqual(self.balance(), 100)

    def test_mines_cashout_and_duplicate_protection(self):
        result = self.post('/api/game/start', {'mines': 3, 'bet': '1.00'})
        self.assertEqual(result['round']['positions'], [])
        self.assertEqual(self.balance(), 99)
        self.post('/api/game/start', {'mines': 3, 'bet': '1.00'}, 400)
        self.post('/api/game/cashout', status=400)
        with m.connect() as db:
            row = db.execute("SELECT * FROM rounds WHERE user_id=? AND state='active'", (self.uid,)).fetchone()
            safe = next(i for i in range(25) if i not in json.loads(row['positions']))
        self.post('/api/game/open', {'cell': safe})
        won = self.post('/api/game/cashout')
        balance = self.balance()
        self.assertGreater(balance, 99)
        self.assertNotEqual(won['round']['state'], 'active')
        self.post('/api/game/cashout', status=400)
        self.assertEqual(self.balance(), balance)

    def test_mines_loss_and_restore(self):
        self.post('/api/game/start', {'mines': 3, 'bet': '1.00'})
        self.assertEqual(self.client.get('/api/me').get_json()['round']['state'], 'active')
        with m.connect() as db:
            row = db.execute("SELECT positions FROM rounds WHERE user_id=?", (self.uid,)).fetchone()
        result = self.post('/api/game/open', {'cell': json.loads(row['positions'])[0]})
        self.assertNotEqual(result['round']['state'], 'active')
        self.assertEqual(self.balance(), 99)

    def test_disabled_modes_allow_finishing_existing_round(self):
        self.post('/api/game/start', {'mines': 3, 'bet': '1.00'})
        m.save_document('section_settings', {'mines': False, 'giveaways': False})
        self.assertEqual(self.client.get('/api/giveaways').status_code, 403)
        self.assertEqual(self.client.get('/api/giveaways/1').status_code, 403)
        self.assertEqual(self.client.get('/api/game/ladder').status_code, 403)
        with m.connect() as db:
            row = db.execute('SELECT positions FROM rounds WHERE user_id=?', (self.uid,)).fetchone()
        safe = next(i for i in range(25) if i not in json.loads(row['positions']))
        self.post('/api/game/open', {'cell': safe})
        self.post('/api/game/cashout')

    def test_invalid_saved_section_settings_recover(self):
        m.save_document('section_settings', [1, 2])
        self.assertTrue(self.client.get('/api/ui/settings').get_json()['sections']['mines'])

    def test_sqlite_connection_closes_after_context(self):
        with m.connect() as db:
            db.execute('SELECT 1')
        with self.assertRaises(m.sqlite3.ProgrammingError):
            db.execute('SELECT 1')

    def test_sqlite_rollback_on_error(self):
        with self.assertRaises(ValueError):
            with m.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE users SET balance=0 WHERE id=?', (self.uid,))
                raise ValueError('Simulated failure')
        self.assertEqual(self.balance(), 100)

    def test_section_settings_require_real_booleans(self):
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/section-settings', {'mines': 'false'}, 400)
            self.post('/api/admin/section-settings', {'mines': False})
        self.assertFalse(m.section_settings()['mines'])

    def test_inventory_ownership_and_single_sale(self):
        with m.connect() as db:
            row = db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,floor_price,source) VALUES(?, 'qa', 'QA gift', 250, 'test')", (self.uid,))
            item = row.lastrowid
        stranger = m.app.test_client()
        with stranger.session_transaction() as session:
            session['uid'] = self.uid + 100000
        self.assertEqual(stranger.post(f'/api/inventory/{item}/sell', json={}).status_code, 404)
        self.post(f'/api/inventory/{item}/sell')
        self.assertEqual(self.balance(), 102.5)
        self.post(f'/api/inventory/{item}/sell', status=404)

    def test_transfer_is_idempotent(self):
        recipient = self.uid + 100000
        with m.connect() as db:
            db.execute('INSERT INTO users(id,name,username,balance) VALUES(?,?,?,0)', (recipient, 'Receiver', f'receiver{self.uid}'))
            db.execute('INSERT INTO level_claims(user_id,level,reward_json) VALUES(?,1,?)', (self.uid, json.dumps({'type':'transfer_unlock'})))
        payload = {'request_id':f'test-transfer-{self.uid}', 'username':f'receiver{self.uid}', 'amount':'10.00'}
        first = self.post('/api/transfers/send', payload)
        balance = self.balance()
        second = self.post('/api/transfers/send', payload)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.balance(), balance)
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT balance FROM users WHERE id=?', (recipient,)).fetchone()['balance'], 1000)

    def test_upgrade_is_idempotent(self):
        m.save_document('portal_catalog', {'gifts':[{'id':'qa-gift','name':'QA gift','price_ton':2,'image_url':'https://example.com/gift.png'}]})
        payload = {'request_id':f'test-upgrade-{self.uid}', 'amount':'1.00', 'gift_id':'qa-gift'}
        self.assertEqual(self.client.get('/api/upgrade/preview?amount=1&gift_id=qa-gift').status_code, 200)
        result = self.post('/api/upgrade/spin', payload)
        second = self.post('/api/upgrade/spin', payload)
        self.assertEqual(result['won'], second['won'])
        self.assertEqual(self.balance(), 99)

    def test_reward_claim_is_single_use(self):
        with m.connect() as db:
            row = db.execute("INSERT INTO reward_tasks(title,category,metric,goal,tickets,action_page,created_at) VALUES('QA','once','referral',1,5,'profilePage','2000-01-01')")
            task = row.lastrowid
            db.execute('INSERT INTO referrals(referred_id,referrer_id) VALUES(?,?)', (self.uid + 200000,self.uid))
        self.post(f'/api/reward-tasks/{task}/claim')
        self.post(f'/api/reward-tasks/{task}/claim', status=409)
        self.assertEqual(self.client.get('/api/me').get_json()['user']['tickets'], 5)

    def create_upgrade_task(self, **values):
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            return self.post('/api/admin/reward-tasks', dict(metric='upgrade_play', goal=10, tickets=7, **values))['id']

    def task_progress(self, task_id):
        return next(x for x in self.client.get('/api/reward-tasks').get_json()['items'] if x['id'] == task_id)

    def add_upgrade_result(self, number, chance, won=1, when='2026-09-30 12:00:00.123', reward='gift', uid=None):
        with m.connect() as db:
            db.execute('''INSERT INTO upgrade_spins(id,user_id,source_name,source_price,target_name,target_price,chance_bp,won,result_json,created_at)
                          VALUES(?,?, 'TON',100,'QA',1000,?,?,?,?)''',
                       (f'quest-{self.uid}-{number}',uid or self.uid,chance,won,json.dumps({'reward_type':reward}),when))

    def test_upgrade_task_chance_boundaries_and_gift_wins(self):
        tasks = []
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            for metric,operator in [('upgrade_play','any'),('upgrade_win','lt'),('upgrade_gift','lt'),('upgrade_win','gt')]:
                tasks.append(self.post('/api/admin/reward-tasks', {'metric':metric,'goal':100,'tickets':7,
                    'chance_operator':operator,'chance_percent':50 if operator=='gt' else 25})['id'])
        with m.connect() as db:
            for task in tasks: db.execute("UPDATE reward_tasks SET created_at='2026-09-30T10:00:00+00:00' WHERE id=?",(task,))
        for i,(chance,won,reward) in enumerate([(2499,1,'gift'),(2500,1,'gift'),(2499,0,'none'),(2499,1,'wager_progress'),(5000,1,'gift'),(5001,1,'gift')]):
            self.add_upgrade_result(i,chance,won,reward=reward)
        self.add_upgrade_result('old',2499,when='2026-09-30 09:59:59')
        self.add_upgrade_result('other',2499,uid=self.uid+9999)
        self.assertEqual([self.task_progress(t)['progress'] for t in tasks],[6,2,1,1])
        self.assertIn('меньше 25%',self.task_progress(tasks[1])['title'])

    def test_upgrade_task_counts_actual_spin_once_and_preserves_claim_on_edit(self):
        task = self.create_upgrade_task()
        m.save_document('portal_catalog', {'gifts':[{'id':'quest-gift','name':'QA','price_ton':2,'image_url':'https://example.com/gift.png'}]})
        payload = {'request_id':f'quest-spin-{self.uid}','amount':'1.00','gift_id':'quest-gift'}
        with patch.object(m.secrets, 'randbelow', return_value=0):
            self.post('/api/upgrade/spin',payload)
            self.post('/api/upgrade/spin',payload)
        self.assertEqual(self.task_progress(task)['progress'],1)
        messages=self.client.get('/api/notifications').get_json()['items']
        self.assertEqual(sum(x['kind']=='upgrade' for x in messages),0)
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            response = self.client.put(f'/api/admin/reward-tasks/{task}',json={'goal':1})
            self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(self.task_progress(task)['title'],'Сыграть в апгрейд 1 раз')
        self.post(f'/api/reward-tasks/{task}/claim')
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.assertEqual(self.client.put(f'/api/admin/reward-tasks/{task}',json={'title':'Моё задание'}).status_code,200)
            self.assertEqual(self.client.put(f'/api/admin/reward-tasks/{task}',json={'category':'daily'}).status_code,400)
        self.post(f'/api/reward-tasks/{task}/claim',status=409)
        self.assertEqual(self.client.get('/api/me').get_json()['user']['tickets'],7)

    def test_upgrade_task_daily_and_expiry_use_event_time(self):
        task = self.create_upgrade_task(category='daily')
        today=m.datetime.now(m.timezone.utc).date().isoformat()
        yesterday=(m.datetime.now(m.timezone.utc)-m.timedelta(days=1)).date().isoformat()
        with m.connect() as db:
            db.execute("UPDATE reward_tasks SET created_at='2000-01-01' WHERE id=?",(task,))
        self.add_upgrade_result(1,2500,when=yesterday+'T23:59:59+00:00')
        self.add_upgrade_result(2,2500,when=today+' 00:00:00.100')
        self.add_upgrade_result(3,2500,when=today+'T00:00:01+00:00')
        self.assertEqual(self.task_progress(task)['progress'],2)
        with m.connect() as db:
            db.execute("UPDATE reward_tasks SET category='limited',ends_at=? WHERE id=?",(today+'T00:00:01+00:00',task))
        self.assertEqual(self.task_progress(task)['progress'],2)

    def test_reward_task_validation_and_admin_permissions(self):
        self.post('/api/admin/reward-tasks',{'metric':'upgrade_win'},403)
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            for fields in [{'goal':1.5},{'goal':True},{'chance_operator':'nope'},
                           {'chance_operator':'lt','chance_percent':'NaN'},
                           {'chance_operator':'gt','chance_percent':100.01},
                           {'chance_operator':'lt','chance_percent':25.001}]:
                self.post('/api/admin/reward-tasks',dict(metric='upgrade_win',**fields),400)
        task=self.create_upgrade_task()
        self.assertEqual(self.client.put(f'/api/admin/reward-tasks/{task}',json={'goal':1}).status_code,403)

    def test_archive_giveaways_hides_only_completed_and_keeps_awards(self):
        m.save_document('portal_catalog',{'gifts':[{'id':'archive-gift','name':'QA','price_ton':2,'image_url':'https://example.com/gift.png'}]})
        self.post('/api/admin/giveaways/clear-completed',status=403)
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post('/api/admin/giveaways/clear-completed')
            finished=self.post('/api/admin/giveaways',{'title':'Old','duration_minutes':60,'prizes':[{'gift_id':'archive-gift'}]})['item']['id']
            active=self.post('/api/admin/giveaways',{'title':'Active','duration_minutes':60,'prizes':[{'gift_id':'archive-gift'}]})['item']['id']
            with m.connect() as db:
                db.execute('INSERT INTO giveaway_entries(giveaway_id,user_id,tickets) VALUES(?,?,3)',(finished,self.uid))
            self.post(f'/api/admin/giveaways/{finished}/finish')
            with m.connect() as db:
                award=db.execute('SELECT inventory_id FROM giveaway_winners WHERE giveaway_id=?',(finished,)).fetchone()['inventory_id']
            self.assertEqual(self.post('/api/admin/giveaways/clear-completed')['hidden'],1)
            self.assertEqual(self.post('/api/admin/giveaways/clear-completed')['hidden'],0)
            self.assertNotIn(finished,[x['id'] for x in self.client.get('/api/admin/giveaways').get_json()['items']])
        self.assertEqual(self.client.get(f'/api/giveaways/{finished}').status_code,404)
        self.assertEqual(self.client.get(f'/api/giveaways/{active}').status_code,200)
        with m.connect() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM inventory WHERE id=?',(award,)).fetchone())
            self.assertEqual(db.execute('SELECT tickets FROM giveaway_entries WHERE giveaway_id=?',(finished,)).fetchone()['tickets'],3)

    def test_game_history_filters_and_permissions(self):
        self.add_upgrade_result('history1',1234)
        self.add_upgrade_result('history2',6789,won=0)
        self.post('/api/game/start',{'bet':'1.00','mines':3})
        self.assertEqual(self.client.get('/api/admin/game-history').status_code,403)
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            rows=self.client.get(f'/api/admin/game-history?user_id={self.uid}').get_json()['items']
            self.assertEqual(len(rows),3)
            self.assertEqual({x['game'] for x in rows},{'mines','upgrade'})
            upgrades=self.client.get(f'/api/admin/game-history?user_id={self.uid}&game=upgrade').get_json()['items']
            self.assertEqual(len(upgrades),2)
            self.assertEqual({x['chance'] for x in upgrades},{12.34,67.89})
            self.assertEqual(self.client.get('/api/admin/game-history?user_id=bad').status_code,400)
            self.assertEqual(self.client.get('/api/admin/game-history?game=bad').status_code,400)

    def test_notifications_are_private_and_read_is_scoped(self):
        with m.connect() as db:
            m.log_event(db,self.uid,'transfer_received',amount=25)
            m.log_event(db,self.uid+9999,'transfer_received',amount=5)
        rows=self.client.get('/api/notifications').get_json()
        self.assertEqual(rows['unread'],1)
        self.assertEqual(len(rows['items']),1)
        self.assertIn('25.00 TON',rows['items'][0]['text'])
        self.post('/api/notifications/read',{'upto':rows['items'][0]['id']+100})
        self.assertEqual(self.client.get('/api/notifications').get_json()['unread'],0)
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT is_read FROM user_notifications WHERE user_id=?',(self.uid+9999,)).fetchone()['is_read'],0)
        self.assertEqual(m.app.test_client().get('/api/notifications').status_code,401)

    def test_notification_queue_only_delivers_committed_rows_once(self):
        with patch.object(m,'BOT_TOKEN','qa-token'):
            with m.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                m.log_event(db,self.uid,'transfer_received',amount=5)
                db.rollback()
            with m.connect() as db:
                m.log_event(db,self.uid,'transfer_received',amount=5)
            with patch.object(m,'send_user_notification',return_value=True) as send:
                m.deliver_activity_notifications()
                m.deliver_activity_notifications()
                self.assertEqual(send.call_count,1)
                self.assertEqual(send.call_args.args[0],self.uid)

    def test_levels_load_background_settings_once(self):
        with patch.object(m,'black_backgrounds_enabled',return_value=False) as setting:
            response=self.client.get('/api/levels')
            self.assertEqual(response.status_code,200)
            self.assertGreater(len(response.get_json()['levels']),1)
            self.assertEqual(setting.call_count,1)

    def test_notification_delivery_retries_without_resending_success(self):
        with patch.object(m,'BOT_TOKEN','qa-token'):
            with m.connect() as db:
                m.log_event(db,self.uid,'transfer_received',amount=5)
            with patch.object(m,'send_user_notification',return_value=False) as failed:
                m.deliver_activity_notifications()
                m.deliver_activity_notifications()
                self.assertEqual(failed.call_count,1)
            with m.connect() as db:
                db.execute('UPDATE user_notifications SET delivery_next_at=0 WHERE user_id=?',(self.uid,))
            with patch.object(m,'send_user_notification',return_value=True) as sent:
                m.deliver_activity_notifications()
                m.deliver_activity_notifications()
                self.assertEqual(sent.call_count,1)

    def test_routine_steps_stay_in_audit_without_notifications(self):
        with patch.object(m,'BOT_TOKEN','qa-token'):
            with m.connect() as db:
                for kind,data in [('login',{}),('mines_start',{'bet':1,'mines':3}),
                                  ('mines_cell',{'cell':2,'lost':False}),('mines_cell',{'cell':3,'lost':True}),
                                  ('upgrade',{'won':False}),('upgrade',{'won':True,'promo_wager':True})]:
                    m.log_event(db,self.uid,kind,**data)
                # Old releases already queued these messages. They must remain silent too.
                db.execute("INSERT INTO user_notifications(user_id,kind,text,delivery_state) VALUES(?,'mines_cell','Клетка 1','pending')",(self.uid,))
                db.execute("INSERT INTO user_notifications(user_id,kind,text,delivery_state) VALUES(?,'upgrade','Апгрейд · проигрыш','pending')",(self.uid,))
                m.log_event(db,self.uid,'transfer_received',amount=5)
            with patch.object(m,'send_user_notification',return_value=True) as send:
                m.deliver_activity_notifications()
                self.assertEqual(send.call_count,1)
            response=self.client.get('/api/notifications').get_json()
            self.assertEqual(len(response['items']),1)
            self.assertEqual(response['unread'],1)
            self.assertEqual(response['items'][0]['kind'],'transfer_received')
            with m.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM user_events WHERE user_id=?',(self.uid,)).fetchone()['n'],7)

    def test_admin_display_is_private_and_persistent(self):
        self.post('/api/admin/display', {'visible':False}, status=403)
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/display', {'visible':'false'}, status=400)
            self.assertFalse(self.post('/api/admin/display', {'visible':False})['user']['admin_button_visible'])
            self.assertTrue(self.client.get('/api/me').get_json()['user']['admin'])
            self.assertFalse(self.client.get('/api/me').get_json()['user']['admin_button_visible'])
            self.assertTrue(self.post('/api/admin/display', {'visible':True})['user']['admin_button_visible'])

    def test_promo_and_login_remain_silent_and_deposit_has_amount_and_balance(self):
        with m.connect() as db:
            for kind in ('login','promo_redeem','freebet_redeem','deposit_created'):
                m.log_event(db,self.uid,kind,code='SECRET',amount=5)
                db.execute('INSERT INTO user_notifications(user_id,kind,text) VALUES(?,?,?)',(self.uid,kind,'Старое сообщение'))
            m.record_transaction(db,self.uid,'deposit',500,'deposit','qa','Пополнение администратором')
        items=self.client.get('/api/notifications').get_json()['items']
        self.assertEqual([(x['kind'],x['text']) for x in items],[('deposit','✅ Ваш баланс пополнен на 5.00 TON.\n\nТекущий баланс: 100.00 TON')])
        self.assertNotIn('администратор',items[0]['text'].lower())

    def test_admin_deposit_bot_receipt_contains_amount_balance_and_open(self):
        with patch.object(m,'ADMIN_IDS',{self.uid}), patch.object(m,'WEBAPP_URL','https://gemdrop.example'), patch.object(m,'notify_user_async') as notify:
            data={'amount':'5.25','request_key':f'clear-receipt-{self.uid}'}
            self.post(f'/api/admin/users/{self.uid}/deposit',data)
            self.post(f'/api/admin/users/{self.uid}/deposit',data)
            self.assertEqual(notify.call_count,1)
            uid,text,markup,mode=notify.call_args.args
            self.assertEqual(uid,self.uid)
            self.assertIn('✅ <b>Ваш баланс пополнен на 5.25 TON.</b>',text)
            self.assertIn('Текущий баланс: <b>105.25 TON</b>',text)
            self.assertNotIn('администратор',text.lower())
            self.assertEqual(mode,'HTML')
            self.assertEqual(markup['inline_keyboard'][0][0]['text'],'Открыть')
            notice=self.client.get('/api/notifications').get_json()['items'][0]
            self.assertIn('5.25 TON',notice['text'])
            self.assertIn('105.25 TON',notice['text'])

    def test_notification_delivery_escapes_names_and_has_open_button(self):
        with patch.object(m,'BOT_TOKEN','qa-token'):
            with m.connect() as db:
                m.add_user_notification(db,self.uid,'withdrawal_request','⏳ Заявка на вывод\n<b>QA & gift</b>')
            with patch.object(m,'WEBAPP_URL','https://gemdrop.example'),patch.object(m,'send_user_notification',return_value=True) as send:
                m.deliver_activity_notifications()
                self.assertIn('&lt;b&gt;QA &amp; gift&lt;/b&gt;',send.call_args.args[1])
                self.assertEqual(send.call_args.args[2]['inline_keyboard'][0][0]['text'],'Открыть')
                self.assertEqual(send.call_args.args[3],'HTML')

    def test_fragment_manual_price_announcement_refresh_and_archive(self):
        gift=dict(source_type='fragment',gift_id='fragment:qa-1',gift_name='QA #1',
                  image_url='https://example.com/qa.webp',floor_price=300,fragment_url='https://t.me/nft/QA-1',
                  fragment_number='1',fragment_model='',fragment_backdrop='',fragment_symbol='',price_source='Fragment')
        with patch.object(m,'ADMIN_IDS',{self.uid}), patch.object(m,'fragment_gift_from_url',side_effect=lambda *a,**k:dict(gift)):
            data={'title':'Новый','description':'Подарки','duration_minutes':60,
                  'prizes':[{'source_type':'fragment','fragment_url':gift['fragment_url'],'price_ton':'12.34'}]}
            item=self.post('/api/admin/giveaways',data)['item']
            prize=item['prizes'][0]
            self.assertEqual(prize['price_ton'],12.34)
            notice=self.client.get('/api/notifications').get_json()['items'][0]
            self.assertEqual(notice['giveaway_id'],item['id'])
            self.assertIn('QA #1',notice['text'])
            self.assertIn(gift['fragment_url'],notice['text'])
            self.post(f'/api/admin/giveaways/{item["id"]}/archive',status=400)
            path=f'/api/admin/giveaways/{item["id"]}/prizes/{prize["id"]}/price'
            self.post(path,{'price_ton':'-1'},status=400)
            self.post(path,{'price_ton':'8,50'})
            self.post(f'/api/admin/giveaways/{item["id"]}/refresh-prizes')
            current=self.client.get(f'/api/giveaways/{item["id"]}').get_json()['item']['prizes'][0]
            self.assertEqual(current['price_ton'],8.5)
            self.assertEqual(current['price_source'],'Ручная цена')
            with m.connect() as db:
                db.execute('INSERT INTO giveaway_entries(giveaway_id,user_id,tickets) VALUES(?,?,1)',(item['id'],self.uid))
            self.post(f'/api/admin/giveaways/{item["id"]}/finish')
            self.post(path,{'price_ton':'9'},status=400)
            self.post(f'/api/admin/giveaways/{item["id"]}/archive')
            self.assertEqual(self.client.get(f'/api/giveaways/{item["id"]}').status_code,404)
            with m.connect() as db:
                self.assertEqual(db.execute("SELECT floor_price FROM inventory WHERE user_id=? AND source='giveaway'",(self.uid,)).fetchone()['floor_price'],850)

    def test_giveaway_delivery_has_direct_button_and_is_sent_once(self):
        with m.connect() as db:
            db.execute("INSERT INTO user_notifications(user_id,kind,text,giveaway_id,delivery_state) VALUES(?,'giveaway_started','Новый розыгрыш',42,'pending')",(self.uid,))
        with patch.object(m,'BOT_TOKEN','qa-token'), patch.object(m,'WEBAPP_URL','https://gemdrop.example'), patch.object(m,'send_user_notification',return_value=True) as send:
            m.deliver_activity_notifications()
            m.deliver_activity_notifications()
            self.assertEqual(send.call_count,1)
            button=send.call_args.args[2]['inline_keyboard'][0][0]
            self.assertEqual(button['text'],'Открыть розыгрыш')
            self.assertEqual(button['web_app']['url'],'https://gemdrop.example/?open=giveaways&giveaway=42')

    def test_only_essential_notifications_are_visible_and_delivered(self):
        muted=['upgrade','craft_play','level_claim','admin_level','gift_sale','game_win_ton',
               'gift_win','giveaway_enter','reward_task_claim','transfer_sent','admin_gift_add',
               'admin_gift_remove','promo_redeem','freebet_redeem','login','admin_balance']
        with patch.object(m,'BOT_TOKEN','qa-token'):
            with m.connect() as db:
                for kind in muted:
                    m.log_event(db,self.uid,kind,won=True,gift_name='QA',chance=25)
                    db.execute("INSERT INTO user_notifications(user_id,kind,text,delivery_state) VALUES(?,?,?,'pending')",(self.uid,kind,'Апгрейд · выигрыш'))
                m.log_event(db,self.uid,'transfer_received',amount=3)
            with patch.object(m,'send_user_notification',return_value=True) as send:
                m.deliver_activity_notifications()
                self.assertEqual(send.call_count,1)
            response=self.client.get('/api/notifications').get_json()
            self.assertEqual(response['unread'],1)
            self.assertEqual([x['kind'] for x in response['items']],['transfer_received'])
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM user_events WHERE user_id=?',(self.uid,)).fetchone()['n'],len(muted)+1)

    def test_admin_can_delete_normal_and_wager_inventory_without_notifying(self):
        with m.connect() as db:
            normal=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,floor_price,source) VALUES(?,'qa','QA',500,'test')",(self.uid,)).lastrowid
            locked=db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,floor_price,source,promo_locked) VALUES(?,'qa','QA Locked',500,'promo',1)",(self.uid,)).lastrowid
        path=f'/api/admin/users/{self.uid}/inventory/{normal}'
        self.assertEqual(self.client.delete(path).status_code,403)
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.assertEqual(self.client.delete(f'/api/admin/users/{self.uid+1}/inventory/{normal}').status_code,404)
            response=self.client.delete(path,content_type='application/json')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.get_json()['removed_id'],normal)
            self.assertEqual(self.client.delete(path).status_code,404)
            self.assertEqual(self.client.delete(f'/api/admin/users/{self.uid}/inventory/{locked}').status_code,200)
            self.assertEqual(self.client.get(f'/api/admin/users/{self.uid}').get_json()['items'],[])
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM admin_log WHERE user_id=? AND action=\'gift_remove\'',(self.uid,)).fetchone()['n'],2)
        self.assertEqual(self.client.get('/api/notifications').get_json()['items'],[])

    def test_premium_notification_never_guesses_custom_emoji_from_unicode(self):
        m.remember_emojis([{'id':'12345678901','emoji':'🎉'}])
        response=Mock()
        response.raise_for_status.return_value=None
        response.json.return_value={'ok':True}
        try:
            with patch.object(m,'BOT_TOKEN','qa-token'),patch.object(m.requests,'post',return_value=response) as post:
                self.assertTrue(m.send_user_notification(self.uid,'🎉 <b>Новый розыгрыш</b>',parse_mode='HTML'))
            self.assertEqual(post.call_args.kwargs['json']['text'],'🎉 <b>Новый розыгрыш</b>')
        finally:
            with m.connect() as db:
                db.execute("DELETE FROM app_documents WHERE name='saved_emoji:12345678901'")

    def test_freebet_gift_uses_exact_gift_custom_emoji_mapping(self):
        m.save_document('gift_emoji:qa-gift', {
            'gift_id':'qa-gift','gift_name':'QA gift',
            'emoji_id':'12345678901','emoji':'🔥'
        })
        reward={'type':'gift','gift':{'gift_id':'qa-gift','name':'QA gift','price_ton':2}}
        html=m.freebet_reward_html(reward)
        self.assertIn('<tg-emoji emoji-id="12345678901">🔥</tg-emoji>',html)
        self.assertNotIn('🎁 <b>QA gift</b>',html)

    def test_creator_program_add_and_remove_send_user_notifications(self):
        with patch.object(m,'ADMIN_IDS',{self.uid}), patch.object(m,'WEBAPP_URL','https://gemdrop.example'), patch.object(m,'notify_user_async') as send:
            self.post(f'/api/admin/creators/{self.uid}',{})
            self.assertEqual(send.call_count,1)
            add_args=send.call_args.args
            self.assertEqual(add_args[0],self.uid)
            self.assertIn('Вы подключены к программе авторов GemDrop',add_args[1])
            self.assertEqual(add_args[2]['inline_keyboard'][0][0]['text'],'Открыть программу')
            self.assertIn('?open=creator',add_args[2]['inline_keyboard'][0][0]['web_app']['url'])
            self.client.delete(f'/api/admin/creators/{self.uid}')
            self.assertEqual(send.call_count,2)
            remove_args=send.call_args.args
            self.assertIn('К сожалению, вы были отключены от программы авторов GemDrop.',remove_args[1])
            self.assertIsNone(remove_args[2])

    def test_stars_payment_notification_uses_real_newlines(self):
        m.save_document('notice_templates', {'texts': {}})
        spec = m.notice_defs.NOTICE_BY_KEY['stars_paid']
        self.assertIn('Оплата Telegram Stars подтверждена.</b>\n\n', spec['text'])
        self.assertNotIn('\\n', spec['text'])

    def test_creator_panel_can_hide_and_reveal_with_secret_code(self):
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post(f'/api/admin/creators/{self.uid}',{})
        me=self.client.get('/api/me').get_json()['user']
        self.assertTrue(me['creator'])
        self.assertTrue(me['creator_button_visible'])
        hidden=self.post('/api/creator/panel-visibility',{'hidden':True})
        self.assertTrue(hidden['panel_hidden'])
        self.assertFalse(hidden['user']['creator_button_visible'])
        self.post('/api/creator/reveal',{'code':'665'},403)
        shown=self.post('/api/creator/reveal',{'code':'666'})
        self.assertFalse(shown['panel_hidden'])
        self.assertTrue(shown['user']['creator_button_visible'])

    def test_creator_personal_freebet_is_visible_read_only(self):
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post(f'/api/admin/creators/{self.uid}',{})
            self.post('/api/admin/freebets', {
                'code':'CREATOR_FB','reward_type':'balance','amount':'1.00',
                'max_uses':5,'author_user_id':self.uid
            })
        data=self.client.get('/api/creator/freebets').get_json()
        self.assertEqual(len(data['items']),1)
        self.assertEqual(data['items'][0]['code'],'CREATOR_FB')
        self.assertEqual(data['items'][0]['max_uses'],5)
        self.assertEqual(data['items'][0]['uses_count'],0)
        with m.connect() as db:
            promo = db.execute('SELECT author_user_id FROM promo_codes WHERE code=?',('CREATOR_FB',)).fetchone()
        self.assertEqual(int(promo['author_user_id']), self.uid)

    def test_creator_personal_promocode_and_chat(self):
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post(f'/api/admin/creators/{self.uid}',{})
            promo=self.post('/api/admin/promocodes',{
                'code':'AUTHOR_PROMO','reward_type':'balance','amount':'1.00',
                'max_uses':10,'author_user_id':self.uid
            })
        self.assertEqual(promo['code'],'AUTHOR_PROMO')
        promos=self.client.get('/api/creator/promocodes').get_json()['items']
        self.assertEqual(len(promos),1)
        self.assertEqual(promos[0]['code'],'AUTHOR_PROMO')
        sent=self.post('/api/creator/chat/messages',{'text':'Привет, авторы'})
        self.assertEqual(sent['item']['text'],'Привет, авторы')
        self.assertTrue(sent['item']['mine'])
        chat=self.client.get('/api/creator/chat/messages').get_json()['items']
        self.assertEqual(chat[-1]['text'],'Привет, авторы')

    def test_creator_chat_rejects_non_creator(self):
        m.save_creator_record(self.uid,{'active':False})
        response=self.client.get('/api/creator/chat/messages')
        self.assertEqual(response.status_code,403)

    def test_youtube_channel_snapshot_filters_gemdrop_videos(self):
        def fake_api(path, params):
            if path=='channels':
                return {'items':[{'id':'UC1234567890123456789012','snippet':{
                    'title':'Creator','customUrl':'@creator',
                    'thumbnails':{'high':{'url':'https://example.com/a.jpg'}}},
                    'statistics':{'subscriberCount':'1234','hiddenSubscriberCount':False},
                    'contentDetails':{'relatedPlaylists':{'uploads':'UU_TEST'}}}]}
            if path=='playlistItems':
                return {'items':[
                    {'snippet':{'title':'GemDrop update','description':'','publishedAt':'2026-01-01',
                                'thumbnails':{'high':{'url':'https://example.com/1.jpg'}},
                                'resourceId':{'videoId':'v1'}},
                     'contentDetails':{'videoId':'v1'}},
                    {'snippet':{'title':'Other','description':'#GemDrop test','publishedAt':'2026-01-02',
                                'thumbnails':{'high':{'url':'https://example.com/2.jpg'}},
                                'resourceId':{'videoId':'v2'}},
                     'contentDetails':{'videoId':'v2'}},
                    {'snippet':{'title':'Unrelated','description':'nothing','publishedAt':'2026-01-03',
                                'thumbnails':{'high':{'url':'https://example.com/3.jpg'}},
                                'resourceId':{'videoId':'v3'}},
                     'contentDetails':{'videoId':'v3'}}]}
            if path=='videos':
                return {'items':[{'id':'v1','statistics':{'viewCount':'10'}},
                                 {'id':'v2','statistics':{'viewCount':'20'}}]}
            return {}
        with patch.object(m,'YOUTUBE_API_KEY','key'), patch.object(m,'youtube_api_get',side_effect=fake_api):
            snap=m.youtube_channel_snapshot('https://youtube.com/@creator')
        self.assertEqual(snap['title'],'Creator')
        self.assertEqual(snap['subscribers'],1234)
        self.assertEqual([x['video_id'] for x in snap['videos']],['v1','v2'])
        self.assertEqual([x['views'] for x in snap['videos']],[10,20])

    def test_youtube_public_fallback_without_api_key(self):
        page = '''
        <html><head>
          <meta property="og:title" content="Creator Public">
          <meta property="og:image" content="https://example.com/avatar.jpg">
          <link rel="canonical" href="https://www.youtube.com/@creator">
          <meta itemprop="channelId" content="UC1234567890123456789012">
        </head>
        <body>{"subscriberCountText":{"simpleText":"1.2K subscribers"}}</body></html>
        '''
        feed = b'''<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:yt="http://www.youtube.com/xml/schemas/2015"
              xmlns:media="http://search.yahoo.com/mrss/">
          <entry>
            <yt:videoId>abcDEF12345</yt:videoId>
            <title>GemDrop public video</title>
            <published>2026-01-01T00:00:00+00:00</published>
            <link rel="alternate" href="https://www.youtube.com/watch?v=abcDEF12345"/>
            <media:group>
              <media:description>test</media:description>
              <media:thumbnail url="https://example.com/v.jpg"/>
              <media:community><media:statistics views="321"/></media:community>
            </media:group>
          </entry>
          <entry>
            <yt:videoId>zzzYYY12345</yt:videoId>
            <title>Other video</title>
            <published>2026-01-02T00:00:00+00:00</published>
            <media:group><media:description>nothing</media:description></media:group>
          </entry>
        </feed>'''

        class FakeResponse:
            def __init__(self, text='', content=b''):
                self.text = text
                self.content = content

        def fake_public_get(url, params=None):
            return FakeResponse(content=feed) if 'feeds/videos.xml' in url else FakeResponse(text=page)

        with patch.object(m,'YOUTUBE_API_KEY',''), patch.object(m,'youtube_public_get',side_effect=fake_public_get):
            snap=m.youtube_channel_snapshot('https://youtube.com/@creator')
        self.assertEqual(snap['source'],'public')
        self.assertEqual(snap['title'],'Creator Public')
        self.assertEqual(snap['subscribers'],1200)
        self.assertEqual(snap['channel_id'],'UC1234567890123456789012')
        self.assertEqual(len(snap['videos']),1)
        self.assertEqual(snap['videos'][0]['views'],321)

    def test_arena_weighted_round_settles_and_conserves_pool(self):
        other = self.uid + 1000000
        with m.connect() as db:
            db.execute('DELETE FROM arena_bets')
            db.execute('DELETE FROM arena_rounds')
            db.execute('INSERT INTO users(id,name,username,balance) VALUES(?,?,?,?)',
                       (other, 'Other', f'qa{other}', 10000))
        m.save_document('game_modes', {'mines':'on','upgrade':'on','crash':'off','arena':'on'})
        first = self.post('/api/arena/bet', {'bet':'1.00'})
        self.assertEqual(first['state']['my_bet']['bet'], 1.0)
        other_client = m.app.test_client()
        with other_client.session_transaction() as session:
            session['uid'] = other
        response = other_client.post('/api/arena/bet', json={'bet':'3.00'})
        self.assertEqual(response.status_code, 200, response.get_json())
        with m.connect() as db:
            round_id = db.execute('SELECT id FROM arena_rounds ORDER BY id DESC LIMIT 1').fetchone()['id']
            db.execute('UPDATE arena_rounds SET close_at=0 WHERE id=?', (round_id,))
        state = self.client.get('/api/arena/state').get_json()
        self.assertEqual(state['round']['state'], 'settled')
        self.assertEqual(state['round']['total_pool'], 4.0)
        self.assertIn(state['round']['winner_user_id'], [self.uid, other])
        chances = {x['user_id']: round(x['chance'], 2) for x in state['players']}
        self.assertEqual(chances[self.uid], 25.0)
        self.assertEqual(chances[other], 75.0)
        with m.connect() as db:
            total = sum(int(db.execute('SELECT balance FROM users WHERE id=?',(uid,)).fetchone()['balance'])
                        for uid in (self.uid, other))
        self.assertEqual(total, 20000)

    def test_demo_gifts_are_usable_in_mines_upgrade_and_crash(self):
        gifts = [
            {'id':'demo-a','name':'Demo A','price_ton':2,'image_url':'https://example.com/a.png'},
            {'id':'demo-b','name':'Demo B','price_ton':2,'image_url':'https://example.com/b.png'},
            {'id':'demo-c','name':'Demo C','price_ton':2,'image_url':'https://example.com/c.png'},
            {'id':'demo-target','name':'Demo Target','price_ton':4,'image_url':'https://example.com/t.png'},
        ]
        m.save_document('portal_catalog', {'gifts':gifts})
        m.save_document('game_modes', {'mines':'on','upgrade':'on','crash':'on','arena':'off'})
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post(f'/api/admin/creators/{self.uid}',{})
        self.post('/api/creator/demo-balance', {'amount':'50'})
        added = []
        for gift_id in ('demo-a','demo-b','demo-c'):
            added.append(self.post('/api/creator/demo-inventory', {'gift_id':gift_id})['items'][0]['id'])
        self.post('/api/creator/demo-mode', {'enabled':True})
        inv = self.client.get('/api/inventory').get_json()
        self.assertTrue(inv['demo'])
        self.assertGreaterEqual(len(inv['items']), 3)

        mines = self.post('/api/game/start', {'mines':3,'inventory_id':added[0]})
        self.assertEqual(mines['round']['bet_type'], 'gift')

        preview = self.client.get(f'/api/upgrade/preview?inventory_id={added[1]}&gift_id=demo-target')
        self.assertEqual(preview.status_code, 200, preview.get_json())
        spin = self.post('/api/upgrade/spin', {
            'inventory_id':added[1], 'gift_id':'demo-target',
            'request_id':'demo_upgrade_1234567890'
        })
        self.assertIn('won', spin)

        crash = self.post('/api/crash/bet', {'inventory_id':added[2]})
        self.assertEqual(crash['state']['my_bet']['bet_type'], 'gift')
        self.assertTrue(crash['state']['demo'])

    def test_arena_ui_and_author_shortcuts_are_wired(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="gameCardArena"', page)
        self.assertIn('id="arenaPage"', page)
        self.assertIn('data-game-key="arena"', page)
        self.assertIn("show('promocodesPage')", page)
        self.assertIn("creator-demo-delete", page)
        self.assertNotIn("show('promoAdminPage')", page)

    def test_creator_demo_is_isolated_from_real_game_mutations(self):
        m.save_document('portal_catalog', {'gifts':[{
            'id':'qa-demo-gift','name':'Demo Gift','price_ton':2,'image_url':'https://example.com/gift.png'
        }]})
        with patch.object(m,'ADMIN_IDS',{self.uid}):
            self.post(f'/api/admin/creators/{self.uid}',{})
        state=self.post('/api/creator/demo-balance',{'amount':'123.45'})
        self.assertEqual(state['demo_balance'],123.45)
        added=self.post('/api/creator/demo-inventory',{'gift_id':'qa-demo-gift'})
        self.assertEqual(len(added['items']),1)
        enabled=self.post('/api/creator/demo-mode',{'enabled':True})
        self.assertTrue(enabled['user']['creator_demo'])
        self.assertEqual(enabled['user']['balance'],123.45)
        inv=self.client.get('/api/inventory').get_json()
        self.assertTrue(inv['demo'])
        self.assertEqual(inv['items'][0]['name'],'Demo Gift')
        demo_round=self.post('/api/game/start',{'mines':3,'bet':'1.00'})
        self.assertEqual(demo_round['round']['bet_type'],'ton')
        self.assertEqual(demo_round['user']['balance'],122.45)
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT balance FROM users WHERE id=?',(self.uid,)).fetchone()['balance'],10000)

    def test_level_changes_do_not_send_bot_notifications(self):
        with patch.object(m,'notify_user_async') as send,patch.object(m,'send_user_notification') as direct:
            m.notify_level_up_async(self.uid,2)
            with patch.object(m,'ADMIN_IDS',{self.uid}):
                self.post(f'/api/admin/users/{self.uid}/level',{'level':2})
                self.post(f'/api/admin/users/{self.uid}/level',{'level':1})
            send.assert_not_called()
            direct.assert_not_called()

    def test_telegram_signature(self):
        token = 'test-token'
        values = {'auth_date':str(int(time.time())), 'user':json.dumps({'id':self.uid,'first_name':'Test'})}
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        values['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k,v in sorted(values.items())).encode(), hashlib.sha256).hexdigest()
        with patch.object(m, 'BOT_TOKEN', token):
            self.assertEqual(m.verified_user(urlencode(values))['id'], self.uid)
            values['user'] = json.dumps({'id':1})
            self.assertIsNone(m.verified_user(urlencode(values)))

    def test_web_login_challenge_single_use(self):
        with patch.object(m, 'BOT_TOKEN', 'test-token'), patch.object(m, 'current_bot_username', return_value='test_bot'):
            self.post('/api/web-auth/start')
            with self.client.session_transaction() as session:
                challenge = session['web_auth_id']
            self.assertEqual(self.client.get('/api/web-auth/status').get_json()['status'], 'pending')
            with m.connect() as db:
                db.execute('UPDATE web_login_challenges SET user_id=? WHERE id=?', (self.uid,challenge))
            self.assertEqual(self.client.get('/api/web-auth/status').get_json()['status'], 'approved')
            self.assertEqual(self.client.get('/api/web-auth/status').get_json()['status'], 'missing')


    def test_fairness_draw_is_deterministic_and_commitment_matches(self):
        proof = dict(id='x', game='upgrade', user_id=self.uid, server_seed='11' * 32,
                     server_hash=hashlib.sha256(('11' * 32).encode()).hexdigest(),
                     client_seed='client-seed-qa', nonce=7, cursor=0)
        self.assertEqual(m.fairness_draw(proof, 100000, 0), m.fairness_draw(proof, 100000, 0))
        self.assertEqual(proof['server_hash'], hashlib.sha256(proof['server_seed'].encode()).hexdigest())

    def test_mines_fairness_commit_reveal_and_replay(self):
        started = self.post('/api/game/start', {'bet':'1.00','mines':20,'client_seed':'qa-client-seed-1234'})
        proof = started['round']['fairness']
        self.assertEqual(proof['state'], 'committed')
        self.assertNotIn('server_seed', proof)
        with m.connect() as db:
            mine = json.loads(db.execute('SELECT positions FROM rounds WHERE id=?',(started['round']['id'],)).fetchone()['positions'])[0]
        finished = self.post('/api/game/open', {'cell':mine})
        revealed = finished['round']['fairness']
        self.assertEqual(revealed['state'], 'revealed')
        self.assertTrue(revealed['commitment_valid'])
        recreated = dict(game='mines', server_seed=revealed['server_seed'],
                         client_seed=revealed['client_seed'], nonce=revealed['nonce'], cursor=0)
        positions, _ = m.fairness_positions(recreated, 20)
        self.assertEqual(positions, revealed['outcome']['positions'])

    def test_upgrade_prepared_fairness_is_consumed(self):
        m.save_document('portal_catalog', {'gifts':[{
            'id':'qa-target','name':'QA Target','price_ton':'2.00',
            'image_url':'/static/img/gift.svg','image_match':True
        }]})
        proof = self.post('/api/fairness/prepare', {'game':'upgrade','client_seed':'qa-upgrade-seed-123'})['fairness']
        self.assertEqual(proof['state'], 'committed')
        spin = self.post('/api/upgrade/spin', {'amount':'1.00','gift_id':'qa-target',
                         'request_id':'qa_upgrade_request_12345','fairness_id':proof['id'],
                         'client_seed':'qa-upgrade-seed-123'})
        self.assertEqual(spin['fairness']['id'], proof['id'])
        self.assertEqual(spin['fairness']['state'], 'revealed')
        self.assertTrue(spin['fairness']['commitment_valid'])


    def test_shared_hilo_room_is_precommitted_and_replayable(self):
        slot = 987654321
        with m.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            base, result, row = m.hilo_room_ranks(db, slot)
            committed = m.fairness_public(row, False)
            db.commit()
        self.assertEqual(committed['state'], 'committed')
        self.assertNotIn('server_seed', committed)
        self.assertNotEqual(base, result)
        with m.connect() as db:
            row = m.fairness_mark_settled(db, 'hilo_room', slot)
            revealed = m.fairness_public(row, True)
        self.assertTrue(revealed['commitment_valid'])
        draw, _, _ = m.fairness_draw(revealed, revealed['outcome']['upper'], 0)
        candidate = draw + 1
        replay = candidate if candidate < base else candidate + 1
        self.assertEqual(replay, result)

    def test_proof_of_fairness_ui_is_wired_to_active_modes(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="fairnessBtn"', page)
        self.assertIn('id="fairnessModal"', page)
        self.assertIn('/api/fairness/prepare', page)
        self.assertIn('client_seed:fairClientSeed()', page)
        self.assertIn("p.game==='hilo_room'", page)

    def test_road_is_admin_only_provably_fair_and_settles_balance(self):
        self.assertEqual(m.game_modes()['road'], 'admin')
        self.post('/api/road/start', {'bet': '1.00', 'difficulty': 'easy'}, 403)
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.assertEqual(m.road_multiplier_x100('easy', 0), 100)
            self.assertGreater(m.road_multiplier_x100('impossible', 10), m.road_multiplier_x100('easy', 10))
            cfg = self.client.get('/api/road/state').get_json()['config']
            self.assertEqual(cfg['lanes'], 16)
            self.assertEqual(len(cfg['difficulties']['hard']['multipliers']), 14)
            self.assertLess(cfg['difficulties']['impossible']['multipliers'][-1], 200)
            self.post('/api/road/start', {'bet': '1.00', 'difficulty': 'nope'}, 400)
            self.post('/api/road/cashout', {}, 409)
            before = self.balance()
            started = self.post('/api/road/start', {'bet': '1.00', 'difficulty': 'medium'})
            self.assertEqual(started['active']['steps'], 0)
            self.assertAlmostEqual(self.balance(), before - 1.0, places=2)
            self.assertIsNone(started['active_fairness'].get('server_seed'))
            self.post('/api/road/start', {'bet': '1.00', 'difficulty': 'medium'}, 409)
            self.post('/api/road/cashout', {}, 409)
            with patch.dict(m.ROAD_DIFFICULTIES, {'medium': 10000}):
                first = self.post('/api/road/step')
                self.assertFalse(first['step']['caught'])
                done = self.post('/api/road/cashout')
            self.assertEqual(done['game']['state'], 'cashed')
            self.assertGreater(done['payout'], 1.0)
            self.assertTrue(done['fairness']['commitment_valid'])
            self.assertAlmostEqual(self.balance(), before - 1.0 + done['payout'], places=2)
            second = self.post('/api/road/start', {'bet': '1.00', 'difficulty': 'impossible'})
            with patch.dict(m.ROAD_DIFFICULTIES, {'impossible': 0}):
                lost = self.post('/api/road/step')
            self.assertTrue(lost['step']['caught'])
            self.assertEqual(lost['game']['state'], 'lost')
            self.assertTrue(lost['fairness']['commitment_valid'])
            self.assertIsNone(self.client.get('/api/road/state').get_json()['active'])


    def test_xhunt_time_and_target_events_pay_rewards(self):
        def row(uid, name):
            with m.connect() as db:
                db.execute('INSERT OR IGNORE INTO users(id,name,username,balance) VALUES(?,?,?,0)', (uid, name, name))
        winner, other = self.uid + 5000, self.uid + 6000
        row(winner, 'Winner'); row(other, 'Other')
        with m.connect() as db:
            db.execute("UPDATE xhunt_events SET state='finished' WHERE state='active'")
        cfg = {'modes': ['limbo', 'road'], 'finish_by': 'time', 'minutes': 5, 'min_bet': '0.50',
               'telegram': True, 'notify_end': True,
               'rewards': [{'type': 'ton', 'amount': '5'}, {'type': 'promo', 'promo_kind': 'bonus', 'amount': '2'}]}
        self.post('/api/admin/xhunt/start', cfg, 403)
        patcher = patch.object(m, 'ADMIN_IDS', {self.uid})   # the background loop shares this module state
        patcher.start(); self.addCleanup(patcher.stop)
        if True:
            self.post('/api/admin/xhunt/start', dict(cfg, modes=[]), 400)
            self.post('/api/admin/xhunt/start', dict(cfg, rewards=[{'type': 'none'}]), 400)
            started = self.post('/api/admin/xhunt/start', cfg)
            self.post('/api/admin/xhunt/start', cfg, 409)
        ev = started['event']
        self.assertEqual(ev['state'], 'active')
        state = self.client.get('/api/xhunt/state').get_json()
        self.assertTrue(state['active'])
        self.assertEqual([x['key'] for x in state['event']['modes']], ['limbo', 'road'])
        with m.connect() as db:
            now = m._daily_top_db_string(m._xhunt_now())
            db.execute('INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at) VALUES(?,100,100,500,1,1,500,?)', (winner, now))
            db.execute('INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at) VALUES(?,100,100,300,1,1,300,?)', (other, now))
            db.execute('INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at) VALUES(?,10,100,9000,1,1,900,?)', (other, now))
            db.execute('INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at) VALUES(?,100,100,100000,1,1,900,?)', (self.uid, now))
        if True:
            live = self.client.get('/api/xhunt/state').get_json()['event']
        self.assertEqual(live['leader']['user_id'], winner)       # min bet and admin filtered out
        self.assertAlmostEqual(live['leader']['x'], 5.0)
        with m.connect() as db:
            db.execute("UPDATE xhunt_events SET end_at=? WHERE id=?", (m._daily_top_db_string(m._xhunt_now() + m.timedelta(seconds=1)), ev['id']))
            time.sleep(2)
            m.xhunt_tick(db)
        with m.connect() as db:
            paid = json.loads(db.execute('SELECT winners_json FROM xhunt_events WHERE id=?', (ev['id'],)).fetchone()['winners_json'])['winners']
        self.assertEqual([p['user_id'] for p in paid], [winner, other])
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT balance FROM users WHERE id=?', (winner,)).fetchone()['balance'], 500)
            promo = db.execute("SELECT * FROM promo_codes WHERE assigned_user_id=? AND source_label='X-Hunt'", (other,)).fetchone()
            self.assertEqual((promo['reward_type'], promo['amount'], promo['balance_target']), ('balance', 200, 'bonus'))
            self.assertIsNone(m.xhunt_tick(db))
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM promo_codes WHERE source_label=?', ('X-Hunt',)).fetchone()['n'], 1)
        after = self.client.get('/api/xhunt/state').get_json()
        self.assertFalse(after['active'])
        self.assertEqual(after['last']['winners'][0]['name'], 'Winner')
        self.assertTrue(after['last']['notify_end'])
        self.assertTrue(all('code' not in w for w in after['last']['winners']))   # promo codes are private
        self.assertIsNone(after['my_win'])
        other_client = m.app.test_client()
        with other_client.session_transaction() as sess:
            sess['uid'] = other
        mine = other_client.get('/api/xhunt/state').get_json()['my_win']
        self.assertEqual(mine['place'], 2)
        self.assertTrue(mine['code'].startswith('XH-'))
        with m.connect() as db:
            texts = [r['text'] for r in db.execute('SELECT text FROM broadcasts').fetchall()]
        self.assertTrue(any('X-Hunt завершён' in t for t in texts))
        self.assertTrue(any('X-Hunt начался' in t for t in texts))

        tcfg = {'modes': ['limbo'], 'finish_by': 'target', 'target_x': '10', 'min_bet': '0',
                'rewards': [{'type': 'bonus', 'amount': '3'}]}
        if True:
            self.post('/api/admin/xhunt/start', dict(tcfg, target_x='1'), 400)
            started = self.post('/api/admin/xhunt/start', tcfg)
        with m.connect() as db:
            self.assertIsNone(m.xhunt_tick(db))
            now = m._daily_top_db_string(m._xhunt_now())
            db.execute('INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at) VALUES(?,100,100,800,1,1,800,?)', (other, now))
            self.assertIsNone(m.xhunt_tick(db))
            db.execute('INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout,created_at) VALUES(?,100,100,1200,1,1,1200,?)', (winner, now))
            m.xhunt_tick(db)
        with m.connect() as db:
            paid = json.loads(db.execute('SELECT winners_json FROM xhunt_events WHERE id=?', (started['event']['id'],)).fetchone()['winners_json'])['winners']
            self.assertEqual([p['user_id'] for p in paid], [winner])
            self.assertEqual(db.execute('SELECT bonus_balance FROM users WHERE id=?', (winner,)).fetchone()['bonus_balance'], 300)


    # ---------------- notification templates / premium emoji / channel posts ----------------
    def test_notice_defaults_overrides_and_escaping(self):
        m.save_document('notice_templates', {'texts': {}})
        default = m.render_notice('deposit', {'amount': '25.00', 'balance': '31.40'}, {'bonus_line': ''})
        self.assertIn('25.00', default)
        m.save_document('notice_templates', {'texts': {'deposit': '<b>Привет</b> {amount} [emoji:5438496463044752972:⭐]'}})
        custom = m.render_notice('deposit', {'amount': '<i>7</i>', 'balance': '1'}, {'bonus_line': ''})
        self.assertIn('&lt;i&gt;7&lt;/i&gt;', custom)          # placeholder values are escaped
        self.assertIn('emoji-id="5438496463044752972"', m.notification_premium_html(custom))  # token becomes a premium emoji when sent
        # a broken admin text never breaks delivery: the default is used
        m.save_document('notice_templates', {'texts': {'deposit': '<b>не закрыто {amount}'}})
        self.assertEqual(m.render_notice('deposit', {'amount': '25.00', 'balance': '31.40'}, {'bonus_line': ''}), default)
        m.save_document('notice_templates', {'texts': {}})

    def test_every_notice_default_renders_and_validates(self):
        for spec in m.notice_defs.NOTICE_DEFS:
            html = m.notice_validate_text(spec['key'], spec['text'])
            self.assertTrue(html)
            self.assertNotRegex(m.notice_preview(spec['key']), r'\{[a-z_]+\}')   # every placeholder has an example value
        keys = [d['key'] for d in m.notice_defs.NOTICE_DEFS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreaterEqual(len(m.notice_defs.STARTER_EMOJIS), 20)

    def test_gift_html_binds_emoji_to_the_gift_by_id_and_name(self):
        m.save_document('portal_catalog', {'gifts': [{'id': 'qa-pepe', 'name': 'QA Pepe', 'price_ton': 3}]})
        m.save_document('gift_emoji:qa-pepe', dict(gift_id='qa-pepe', gift_name='QA Pepe', emoji_id='5438496463044752972', emoji='⭐'))
        by_id = m.gift_html('qa-pepe', 'QA Pepe')
        self.assertIn('emoji-id="5438496463044752972"', by_id)
        by_name = m.gift_html('', 'QA Pepe #1234')            # inventory names carry the serial number
        self.assertIn('emoji-id="5438496463044752972"', by_name)
        self.assertNotIn('emoji-id', m.gift_html('', 'Unknown gift'))

    def test_notice_admin_api_saves_previews_and_resets(self):
        self.assertEqual(self.client.get('/api/admin/notice-templates').status_code, 403)
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            data = self.client.get('/api/admin/notice-templates').get_json()
            self.assertTrue(data['groups'])
            self.post('/api/admin/notice-templates', {'key': 'deposit', 'text': '<b>Готово</b> {amount} [emoji:5368324170671202286:👍]'})
            item = next(i for g in self.client.get('/api/admin/notice-templates').get_json()['groups'] for i in g['items'] if i['key'] == 'deposit')
            self.assertTrue(item['custom'])
            self.post('/api/admin/notice-templates', {'key': 'deposit', 'text': '<b>сломано'}, 400)
            self.post('/api/admin/notice-templates', {'key': 'nope', 'text': 'x'}, 404)
            preview = self.post('/api/admin/notice-templates/preview', {'key': 'deposit', 'text': 'Баланс {amount}'})
            self.assertIn('25', preview['html'])
            self.post('/api/admin/notice-templates/reset', {'key': '*'})
            self.assertEqual(self.client.get('/api/admin/notice-templates').get_json()['custom_count'], 0)

    def test_channel_posts_are_remembered_merged_and_copied(self):
        m.save_document(m.CHANNEL_POSTS_DOC, {'items': []})
        chat = {'id': -100777000111, 'title': 'QA channel', 'username': 'qa_channel'}
        m.record_channel_post(chat, 5, {'text': 'Привет', 'entities': [{'type': 'custom_emoji', 'custom_emoji_id': '5424972470023104089'}]})
        m.record_channel_post(chat, 6, {'photo': [{}], 'caption': 'Альбом', 'media_group_id': 'G1'})
        m.record_channel_post(chat, 7, {'photo': [{}], 'media_group_id': 'G1'})
        items = m.read_document(m.CHANNEL_POSTS_DOC)['items']
        self.assertEqual(len(items), 2)
        album = next(i for i in items if i['media_group_id'] == 'G1')
        self.assertEqual(album['message_ids'], [6, 7])
        self.assertTrue(next(i for i in items if i['message_ids'] == [5])['premium'])
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            listed = self.client.get('/api/admin/broadcast/channel-posts').get_json()['items']
            self.assertEqual(listed[0]['link'].split('/')[2:4], ['t.me', 'qa_channel'])
        calls = []
        def fake(method, payload, files=None):
            calls.append((method, payload)); return True, {}
        broadcast = {'id': 1, 'text': '', 'photos': '[]', 'buttons': '[]', 'source_chat': chat['id'], 'source_ids': json.dumps([5])}
        with patch.object(m, '_bc_call', fake), patch.object(m, 'BOT_TOKEN', 'x'):
            ok, _result, _cached = m._bc_send_one(broadcast, 12345)
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], 'copyMessage')
        self.assertEqual(calls[0][1]['from_chat_id'], chat['id'])
        broadcast['source_ids'] = json.dumps([6, 7])
        calls.clear()
        with patch.object(m, '_bc_call', fake), patch.object(m, 'BOT_TOKEN', 'x'):
            m._bc_send_one(broadcast, 12345)
        self.assertEqual(calls[0][0], 'copyMessages')

    def test_halloween_effects_flag_and_public_state(self):
        now = int(time.time() * 1000)
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/halloween', {'enabled': True, 'effects': False, 'starts_at': 0, 'ends_at': now + 600000})
        pub = self.client.get('/api/halloween')
        data = pub.get_json()
        self.assertTrue(data['active'])
        self.assertFalse(data['effects'])
        self.assertEqual(pub.headers.get('Cache-Control'), 'no-store')
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/halloween', {'enabled': False, 'starts_at': 0, 'ends_at': 0})
        data = self.client.get('/api/halloween').get_json()
        self.assertFalse(data['active'])
        self.assertTrue(data['effects'])

    def test_loader_defaults_to_builtin_svg_and_maps_legacy_gif(self):
        m.save_document('loader_settings', {})
        self.assertEqual(m.loader_settings()['path'], 'builtin')
        m.save_document('loader_settings', {'path': '/static/gifs/shard.gif'})
        self.assertEqual(m.loader_settings()['path'], 'builtin')
        m.save_document('loader_settings', {'path': 'http://evil.test/x.gif'})
        self.assertEqual(m.loader_settings()['path'], 'builtin')
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.assertEqual(self.post('/api/admin/loader-settings', {'path': 'https://cdn.test/a.gif'})['path'], 'https://cdn.test/a.gif')
            self.assertEqual(self.post('/api/admin/loader-settings', {'path': 'builtin'})['path'], 'builtin')
            self.post('/api/admin/loader-settings', {'path': 'http://x.test/a.gif'}, 400)
        self.assertEqual(self.client.get('/api/ui/settings').get_json()['loader_gif'], 'builtin')

    def test_mobile_fullscreen_and_desktop_compact_viewport_logic_shipped(self):
        html = self.client.get('/').get_data(as_text=True)
        for needle in ('id="gd-viewport"', 'function gdInitViewport', 'tg.requestFullscreen', 'tg.exitFullscreen',
                       'html.tg-fs body.studio .studio-header', 'contentSafeAreaInset'):
            self.assertIn(needle, html)

    def test_webhook_remembers_channel_post_and_forwarded_copypost(self):
        m.save_document(m.CHANNEL_POSTS_DOC, {'items': []})
        chat = {'id': -100888000222, 'title': 'Hook channel', 'type': 'channel'}
        with patch.object(m, 'BOT_TOKEN', 'x'), patch.object(m, 'WEBAPP_URL', 'https://example.test'):
            headers = {'X-Telegram-Bot-Api-Secret-Token': m.WEBHOOK_SECRET}
            r = self.client.post('/telegram/webhook', json={'channel_post': {'message_id': 9, 'chat': chat, 'text': 'hello'}}, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m.read_document(m.CHANNEL_POSTS_DOC)['items'][0]['message_ids'], [9])
        admin = 777000111
        with patch.object(m, 'ADMIN_IDS', {admin}):
            m.handle_admin_emoji_message({'from': {'id': admin}, 'chat': {'type': 'private'}, 'text': '/copypost'})
            reply = m.handle_admin_emoji_message({'from': {'id': admin}, 'chat': {'type': 'private'}, 'text': 'old',
                                                  'forward_origin': {'type': 'channel', 'chat': chat, 'message_id': 3}})
        self.assertIn('сохранён', reply['text'])
        ids = [i for it in m.read_document(m.CHANNEL_POSTS_DOC)['items'] for i in it['message_ids']]
        self.assertIn(3, ids)


if __name__ == '__main__':
    unittest.main()
