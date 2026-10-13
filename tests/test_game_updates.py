import hashlib
import json
import math
import unittest
from unittest.mock import patch
import test_regressions as base

m = base.m


class GameUpdateTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp
    post = base.RegressionTests.post
    balance = base.RegressionTests.balance
    _seed_models = base.RegressionTests._seed_models

    def crash_round(self, target=500):
        m.save_document('game_modes', {'crash': 'on', 'limbo': 'on', 'road': 'on', 'hilo': 'on'})
        with m.connect() as db, patch.object(m, 'crash_roll_x100', return_value=target):
            rid = int(db.execute('SELECT COALESCE(MAX(id),0)+1 FROM crash_rounds').fetchone()[0])
            m.crash_new_round(db, rid, 100000)
            return dict(m.crash_latest(db))

    def test_crash_samples_are_capped_and_do_not_reveal_future_outcome(self):
        row = self.crash_round(100)
        with patch.object(m, 'crash_ms', return_value=row['launch_at']+100), patch.object(m, 'crash_bot_maybe_play', return_value=False):
            sample = self.client.get('/api/crash/state').get_json()
        self.assertEqual(sample['multiplier_x100'], 100)
        self.assertNotIn('crash', sample['round'])
        self.assertNotIn('crash_at', sample['round'])
        self.assertNotIn('server_seed', sample['round']['fairness'])
        with patch.object(m, 'crash_ms', return_value=row['crash_at']+1), patch.object(m, 'crash_bot_maybe_play', return_value=False):
            settled = self.client.get('/api/crash/state').get_json()
        self.assertEqual(settled['round']['crash'], 1)
        self.assertEqual(settled['multiplier_x100'], 100)

    def test_crash_manual_cashout_uses_exact_auto_target_and_preserves_model(self):
        self._seed_models('cr-model')
        row = self.crash_round()
        with patch.object(m, 'crash_ms', return_value=row['launch_at']-1):
            self.post('/api/crash/bet', {'bet': '10', 'auto': '1.50'})
        now = row['launch_at']+int(math.log(2)/m.CRASH_GROWTH*1000)
        with patch.object(m, 'mines_model_roll', return_value=True), patch.object(m, 'crash_ms', return_value=now):
            preview = self.client.get('/api/crash/prize?amount=15').get_json()['prize']
            won = self.post('/api/crash/cashout', {'gift': True})
        self.assertEqual(won['multiplier'], 1.5)
        self.assertEqual(won['payout'], 15)
        self.assertEqual(won['prize']['model'], preview['model'])
        self.assertEqual(won['prize']['image_url'], preview['image_url'])
        self.assertEqual(won['remainder']+won['prize']['price_ton'], 15)
        with m.connect() as db:
            item = db.execute('SELECT * FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 1', (self.uid,)).fetchone()
        self.assertEqual(item['gift_id'], 'cr-model')
        self.assertEqual(item['fragment_model'], 'Classic')
        self.assertEqual(self.balance(), 90+won['remainder'])
        with patch.object(m, 'crash_ms', return_value=now):
            self.post('/api/crash/cashout', {'gift': True}, 409)

    def test_crash_auto_settlement_awards_once_at_configured_target(self):
        self._seed_models('cr-auto')
        row = self.crash_round()
        with patch.object(m, 'crash_ms', return_value=row['launch_at']-1):
            self.post('/api/crash/bet', {'bet': '10', 'auto': '1.50'})
        with patch.object(m, 'mines_model_roll', return_value=True), patch.object(m, 'crash_ms', return_value=row['crash_at']+1), patch.object(m, 'crash_bot_maybe_play', return_value=False):
            first = self.client.get('/api/crash/state').get_json()['my_bet']
            second = self.client.get('/api/crash/state').get_json()['my_bet']
        self.assertEqual(first, second)
        self.assertEqual(first['cashout'], 1.5)
        self.assertEqual(first['prize']['model'], 'Classic')
        self.assertEqual(first['prize']['price_ton']+first['remainder'], 15)
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inventory WHERE user_id=?', (self.uid,)).fetchone()[0], 1)
        self.assertEqual(self.balance(), 93)

    def test_road_and_limbo_award_real_models_and_only_credit_the_remainder(self):
        self._seed_models('all-model')
        m.save_document('game_modes', {'road': 'on', 'limbo': 'on'})
        started = self.post('/api/road/start', {'bet': '10', 'difficulty': 'easy'})
        with m.connect() as db:
            db.execute('UPDATE road_games SET steps=1,mult_x100=150 WHERE id=?', (started['active']['id'],))
        with patch.object(m, 'mines_model_roll', return_value=True):
            road = self.post('/api/road/cashout')
        self.assertEqual(road['prize']['model'], 'Classic')
        self.assertEqual(road['remainder']+road['prize']['price_ton'], 15)
        before = self.balance()
        with patch.object(m, 'mines_model_roll', return_value=True), patch.object(m, 'fairness_draw', return_value=(0,1,'qa')):
            limbo = self.post('/api/limbo/play', {'bet': '10', 'chance': 50})['result']
        self.assertTrue(limbo['won'])
        self.assertEqual(limbo['prize']['model'], 'Classic')
        self.assertEqual(limbo['prize']['price_ton']+limbo['remainder'], limbo['payout'])
        self.assertAlmostEqual(self.balance(), before-10+limbo['remainder'])
        with m.connect() as db:
            models = db.execute('SELECT fragment_model FROM inventory WHERE user_id=?', (self.uid,)).fetchall()
        self.assertEqual([r[0] for r in models], ['Classic','Classic'])
        self.assertTrue(any(r.get('prize') for r in self.client.get('/api/limbo/state').get_json()['history']))

    def test_public_max_drop_ignores_cash_and_arena_pool_but_keeps_nft_value(self):
        with m.connect() as db:
            db.execute("INSERT INTO rounds(user_id,bet,mines,positions,opened,state,payout,win_total,win_gift_price,win_gift_name,win_gift_image) VALUES(?,100,3,'[]','[]','won',999900,999900,0,'','')", (self.uid,))
            m.arena_create_round(db, now=0)
            rid = db.execute('SELECT MAX(id) FROM arena_rounds').fetchone()[0]
            db.execute("UPDATE arena_rounds SET state='settled',winner_user_id=?,total_pool=1000000 WHERE id=?", (self.uid,rid))
            db.execute("INSERT INTO arena_bets(round_id,user_id,amount) VALUES(?,?,100)", (rid,self.uid))
        self.assertIsNone(self.client.get(f'/api/users/{self.uid}/profile').get_json()['max_drop'])
        with m.connect() as db:
            db.execute("INSERT INTO rounds(user_id,bet,mines,positions,opened,state,payout,win_total,win_gift_price,win_gift_name,win_gift_image) VALUES(?,100,3,'[]','[]','won',999900,1003900,4000,'MM Pepe · Da Vinci','https://example.com/model.png')", (self.uid,))
        prize = self.client.get(f'/api/users/{self.uid}/profile').get_json()['max_drop']
        self.assertEqual(prize['name'], 'MM Pepe · Da Vinci')
        self.assertEqual(prize['price_ton'], 40)

    def test_browser_login_uses_auth_persists_cookie_and_records_device_hash(self):
        browser_id = 'stable-browser-identity-12345'
        self.post('/api/logout')
        with patch.object(m, 'BOT_TOKEN', 'qa-token'), patch.object(m, 'WEBAPP_URL', 'https://example.com'), patch.object(m, 'current_bot_username', return_value='qa_bot'):
            started = self.post('/api/web-auth/start', {'browser_id': browser_id})
            self.assertTrue(started['command'].startswith('/auth '))
            m.save_document('notice_templates', {'texts': {'web_login_approved':'<b>Вы вошли, возвращайтесь!</b>'}})
            webhook = self.client.post('/telegram/webhook', json={'update_id':self.uid*1000,'message':{'text':started['command'],'from':{'id':self.uid,'first_name':'QA'},'chat':{'id':self.uid,'type':'private'}}}, headers={'X-Telegram-Bot-Api-Secret-Token':m.WEBHOOK_SECRET})
            self.assertEqual(webhook.status_code, 200, webhook.get_json())
            self.assertEqual(webhook.get_json()['text'], '<b>Вы вошли, возвращайтесь!</b>')
            response = self.client.get('/api/web-auth/status')
        self.assertEqual(response.get_json()['status'], 'approved')
        self.assertIn('Expires=', response.headers.get('Set-Cookie',''))
        with self.client.session_transaction() as session:
            self.assertTrue(session.permanent)
            self.assertEqual(session['auth_kind'], 'browser')
            browser_hash = session['browser_hash']
        self.assertEqual(browser_hash, m.web_login_hash('browser:'+browser_id))
        self.assertNotEqual(browser_hash, browser_id)
        self.post('/api/game/start', {'bet':'1', 'mines':3})
        with m.connect() as db:
            events = db.execute('SELECT payload FROM user_events WHERE user_id=? ORDER BY id', (self.uid,)).fetchall()
        self.assertTrue(any(json.loads(r['payload']).get('browser_hash') == browser_hash for r in events))
        self.assertEqual(self.client.get('/api/me').get_json()['user']['auth_kind'], 'browser')
        stranger = m.app.test_client()
        self.assertEqual(stranger.get('/api/web-auth/status').get_json()['status'], 'missing')
        self.post('/api/logout')
        self.assertEqual(self.client.get('/api/me').status_code, 401)

    def test_all_bot_input_templates_are_valid_and_readable_from_admin(self):
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            response = self.client.get('/api/admin/notice-templates')
            self.assertEqual(response.status_code, 200)
            reply = m.handle_admin_emoji_message({'from':{'id':self.uid},'chat':{'type':'private'},'text':'/post'})
        self.assertEqual(reply['parse_mode'], 'HTML')
        self.assertIn('premium emoji', reply['text'])
        for spec in m.notice_defs.NOTICE_DEFS:
            m.notice_validate_text(spec['key'], spec['text'])
