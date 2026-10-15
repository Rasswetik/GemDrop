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
