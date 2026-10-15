import unittest
from unittest.mock import patch
import test_regressions as base

m = base.m


class LeaderboardTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp
    post = base.RegressionTests.post

    def reset(self):
        with m.connect() as db:
            db.execute("DELETE FROM lb_awards"); db.execute("DELETE FROM lb_turnover"); db.execute("DELETE FROM lb_periods")
            db.execute("DELETE FROM app_documents WHERE name='leaderboard_config'")
        m.leaderboard._cache.update(id=0, ends=0.0, at=0.0)

    def test_turnover_feeds_ranking_and_hides_prize_panels_without_rewards(self):
        self.reset()
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            with m.connect() as db:
                m.increase_turnover(db, self.uid, 5000)
            idle = self.client.get('/api/leaderboard/weekly').get_json()
            self.assertIsNone(idle['period'])
            self.assertEqual(idle['top'], [])
            self.post('/api/admin/leaderboard/start', {'hours': 24})
            self.post('/api/admin/leaderboard/start', {'hours': 24}, 409)
            with m.connect() as db:
                m.increase_turnover(db, self.uid, 5000)
            data = self.client.get('/api/leaderboard/weekly').get_json()
            self.assertTrue(data['enabled'])
            self.assertEqual(data['rewards'], {})
            self.assertEqual(data['me']['place'], 1)
            self.assertEqual(data['top'][0]['turnover'], 50)
            self.assertNotEqual(data['top'][0]['name'], 'Test')
            profile = self.client.get(f'/api/users/{self.uid}/profile').get_json()
            self.assertEqual(profile['leaderboard']['place'], 1)

    def test_admin_rewards_and_early_finish_award_inventory(self):
        self.reset()
        m.save_document('portal_catalog', {'gifts': [{'id': 'lb-gift', 'name': 'LB Gift', 'price_ton': 3, 'image_url': 'https://example.com/g.png', 'image_match': True}]})
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/leaderboard/start', {'hours': 24})
            saved = self.post('/api/admin/leaderboard/rewards/1', {'prizes': [{'_new': True, 'gift_id': 'lb-gift'}]})
            self.assertEqual(len(saved['prizes']), 1)
            with m.connect() as db:
                m.increase_turnover(db, self.uid, 1000)
            self.assertEqual(self.client.get('/api/leaderboard/weekly').get_json()['rewards']['1'][0]['name'], 'LB Gift')
            self.post('/api/admin/leaderboard/finish', {'award': True})
            with m.connect() as db:
                n = db.execute("SELECT COUNT(*) FROM inventory WHERE user_id=? AND source='leaderboard'", (self.uid,)).fetchone()[0]
            self.assertEqual(n, 1)
            admin = self.client.get('/api/admin/leaderboard').get_json()
            self.assertEqual(admin['history'][0]['reason'], 'early')
            self.assertEqual(self.client.get('/api/leaderboard/weekly').get_json()['top'], [])
            self.assertIsNone(self.client.get('/api/leaderboard/weekly').get_json()['period'])

    def test_season_title_ton_prize_and_standings_reset(self):
        self.reset()
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/leaderboard/start', {'hours': 24, 'title': 'Осенний сезон'})
            self.assertEqual(self.client.get('/api/leaderboard/weekly').get_json()['period']['title'], 'Осенний сезон')
            self.post('/api/admin/leaderboard/period', {'title': 'Сезон 7'})
            self.assertEqual(self.client.get('/api/leaderboard/weekly').get_json()['period']['title'], 'Сезон 7')
            self.post('/api/admin/leaderboard/rewards/1', {'prizes': [{'_new': True, 'source_type': 'ton', 'price_ton': '25'}]})
            self.post('/api/admin/leaderboard/rewards/2', {'prizes': [{'_new': True, 'source_type': 'ton', 'price_ton': '0'}]}, 400)
            with m.connect() as db:
                m.increase_turnover(db, self.uid, 1000)
            self.assertEqual(len(self.client.get('/api/leaderboard/weekly').get_json()['top']), 1)
            self.assertEqual(self.post('/api/admin/leaderboard/reset-standings', {})['removed'], 1)
            self.assertEqual(self.client.get('/api/leaderboard/weekly').get_json()['top'], [])
            with m.connect() as db:
                m.increase_turnover(db, self.uid, 1000)
                before = db.execute('SELECT balance FROM users WHERE id=?', (self.uid,)).fetchone()[0]
            self.post('/api/admin/leaderboard/finish', {'award': True})
            with m.connect() as db:
                after = db.execute('SELECT balance FROM users WHERE id=?', (self.uid,)).fetchone()[0]
            self.assertEqual(after - before, 2500)

    def test_medals_are_granted_listed_and_shown_on_profile(self):
        self.reset()
        with m.connect() as db:
            db.execute('DELETE FROM user_medals')
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/leaderboard/medals', {'place': 1, 'medal': 'nope'}, 400)
            self.assertEqual(self.post('/api/admin/leaderboard/medals', {'place': 1, 'medal': 'hw1'})['medals'], {'1': 'hw1'})
            self.post('/api/admin/leaderboard/start', {'hours': 24, 'title': 'Тест'})
            with m.connect() as db:
                m.increase_turnover(db, self.uid, 1000)
            self.post('/api/admin/leaderboard/finish', {'award': True})
        mine = self.client.get('/api/medals').get_json()['medals']
        self.assertEqual((mine[0]['key'], mine[0]['place'], mine[0]['title'], mine[0]['shown']), ('hw1', 1, 'Тест', True))
        self.post('/api/medals/%d/shown' % mine[0]['id'], {'shown': False})
        self.assertEqual(self.client.get('/api/users/%d/profile' % self.uid).get_json()['medals'], [])
        self.post('/api/medals/%d/shown' % mine[0]['id'], {'shown': True})
        self.assertEqual(self.client.get('/api/users/%d/profile' % self.uid).get_json()['medals'][0]['name'], 'Король Хэллоуина')

    def test_admin_grants_and_revokes_manual_medal(self):
        with m.connect() as db:
            db.execute('DELETE FROM user_medals')
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.post('/api/admin/medals/grant', {'user_id': self.uid, 'medal': 'nope'}, 400)
            self.post('/api/admin/medals/grant', {'user_id': 99999999, 'medal': 'vip'}, 404)
            mid = self.post('/api/admin/medals/grant', {'user_id': self.uid, 'medal': 'vip', 'title': 'За верность'})['id']
            got = self.client.get('/api/admin/medals/user/%d' % self.uid).get_json()['medals']
            self.assertEqual((got[0]['key'], got[0]['name'], got[0]['place']), ('vip', 'VIP игрок', 0))
            mine = self.client.get('/api/medals').get_json()['medals']
            self.assertEqual(mine[0]['title'], 'За верность')
            self.assertEqual(self.client.get('/api/users/%d/profile' % self.uid).get_json()['medals'][0]['key'], 'vip')
            pend = self.client.get('/api/medals/pending').get_json()['medals']
            self.assertEqual((pend[0]['id'], pend[0]['name']), (mid, 'VIP игрок'))
            self.post('/api/medals/%d/claim' % mid, {})
            self.assertEqual(self.client.get('/api/medals/pending').get_json()['medals'], [])
            self.post('/api/admin/medals/%d/revoke' % mid, {})
        self.assertEqual(self.client.get('/api/medals').get_json()['medals'], [])

    def test_medal_editor_custom_upload_and_texts(self):
        import base64, io
        from PIL import Image
        buf = io.BytesIO(); Image.new('RGB', (40, 40), (200, 30, 30)).save(buf, 'PNG')
        img = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            cat = self.client.get('/api/medals/catalog').get_json()
            k = [c for c in cat['custom'] if c['key'] == 'kashtan']
            self.assertTrue(k and k[0]['name'] == 'Миша Каштан' and k[0]['manual_only'])
            self.assertEqual(self.client.get('/api/medals/img/kashtan').status_code, 200)
            self.post('/api/admin/leaderboard/medals', {'place': 1, 'medal': 'kashtan'}, 400)
            self.post('/api/admin/medals/create', {'name': 'x', 'image': 'bad'}, 400)
            key = self.post('/api/admin/medals/create', {'name': 'Новая', 'image': img})['key']
            self.post('/api/admin/medals/rename', {'key': 'vip', 'name': 'Супер VIP'})
            self.post('/api/admin/medals/texts', {'notify': 'Привет, {medal} / {title}', 'season': 'S {medal}'})
            self.post('/api/admin/medals/grant', {'user_id': self.uid, 'medal': key, 'title': 'T'})
            self.assertEqual(self.client.get('/api/medals').get_json()['medals'][0]['name'], 'Новая')
            self.post('/api/admin/medals/%s/delete' % key, {}, 409)
            allm = self.client.get('/api/admin/medals/all').get_json()
            self.assertEqual([x['name'] for x in allm['medals'] if x['key'] == 'vip'], ['Супер VIP'])
            self.assertEqual(m.leaderboard.render_note('<b>{medal}</b> <u>x</u>', medal='A<'), '<b>A&lt;</b> &lt;u&gt;x&lt;/u&gt;') if hasattr(m, 'leaderboard') else None
            self.post('/api/admin/medals/rename', {'key': 'vip', 'name': 'VIP игрок'})
            self.post('/api/admin/medals/texts', {'notify': '', 'season': ''})

    def test_hilo_prize_is_base_gift_with_hidden_random_model(self):
        gifts = [{'id': 'hb', 'name': 'Hilo Base', 'price_ton': 5, 'image_url': 'https://example.com/b.png', 'image_match': True}]
        m.save_document('portal_catalog', {'gifts': gifts})
        with m.connect() as db:
            db.execute("DELETE FROM gift_models WHERE gift_id='hb'")
            for name, price in (('Alpha', 500), ('Beta', 600)):
                db.execute('INSERT INTO gift_models(gift_id,model,price) VALUES(?,?,?)', ('hb', name, price))
        m._mines_pool_cache['expires'] = 0
        prize = m.hilo_prize_preview(700, seed='x')
        self.assertEqual(prize['name'], 'Hilo Base')
        self.assertIn(prize['model'], ('Alpha', 'Beta'))
        self.assertEqual(prize['price_cents'], 500)


if __name__ == '__main__':
    unittest.main()
