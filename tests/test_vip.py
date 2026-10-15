import unittest
import test_regressions as base
import vip

m = base.m


class VipTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp
    post = base.RegressionTests.post

    def test_tier_state(self):
        s = vip.tier_state(0)
        self.assertEqual((s['index'], s['reached'], s['next']['name']), (0, False, 'Jade'))
        s = vip.tier_state(173341)
        self.assertEqual((s['index'], s['reached'], s['next']['name']), (1, True, 'Amber'))
        self.assertAlmostEqual(s['next']['left'], 3266.59, places=2)
        s = vip.tier_state(100000000 * 100)
        self.assertEqual(s['index'], 7)
        self.assertEqual([vip.cashback_percent(x * 100) for x in (0, 10000, 25000, 50000, 250000, 1000000)], [1, 1, 2, 3, 4, 5])
        self.assertIsNone(s['next'])

    def test_api_and_feed(self):
        data = self.client.get('/api/vip').get_json()
        self.assertEqual(len(data['tiers']), 8)
        with m.connect() as db:
            db.execute('DELETE FROM recent_wins')
            m.record_transaction(db, self.uid, 'plinko_win', 1234, 'plinko', 1, 'x')
            m.record_transaction(db, self.uid, 'plinko_win', 10, 'plinko', 2, 'x')
            m.record_transaction(db, self.uid, 'hilo_gift_win', 0, 'hilo', 3, 'Cool Gift')
            m.record_transaction(db, self.uid, 'deposit', 9999, '', '', '')
        items = self.client.get('/api/recent-wins').get_json()['items']
        self.assertEqual([i['game'] for i in items], ['Hi-Lo', 'Plinko'])
        self.assertEqual(items[1]['amount'], 12.34)
        self.assertEqual(items[0]['prize'], 'Cool Gift')

    def test_rewards_are_granted_automatically_once(self):
        m.save_document('portal_catalog', {'gifts': [
            {'id': 'cheap', 'name': 'Cheap', 'price_ton': 2, 'image_url': 'https://example.com/c.png'},
            {'id': 'dear', 'name': 'Dear', 'price_ton': 500, 'image_url': 'https://example.com/d.png'}]})
        with m.connect() as db:
            db.execute('UPDATE users SET turnover_cents=0, tickets=0, balance=0 WHERE id=?', (self.uid,))
            db.execute('DELETE FROM vip_claims'); db.execute("DELETE FROM inventory WHERE source='vip'")
            db.execute("DELETE FROM user_notifications WHERE kind='vip_level'")
            m.increase_turnover(db, self.uid, 150000)
            m.increase_turnover(db, self.uid, 100)
            row = db.execute('SELECT tickets,balance FROM users WHERE id=?', (self.uid,)).fetchone()
            inv = db.execute("SELECT gift_name FROM inventory WHERE user_id=? AND source='vip'", (self.uid,)).fetchall()
            n = db.execute("SELECT COUNT(*) FROM user_notifications WHERE user_id=? AND kind='vip_level'", (self.uid,)).fetchone()[0]
        self.assertEqual((row['tickets'], row['balance']), (1500, 300))
        self.assertEqual([r['gift_name'] for r in inv], ['Cheap'])
        self.assertEqual(n, 2)
        data = self.client.get('/api/vip').get_json()
        self.assertEqual(data['pending'], 2)
        self.assertEqual(sorted(str(u['gift'] and u['gift']['name']) for u in data['unseen']), ['Cheap', 'None'])
        self.post('/api/vip/seen', {})
        self.assertEqual(self.client.get('/api/vip').get_json()['pending'], 0)

    def test_playbonus_cashback_and_claim(self):
        with m.connect() as db:
            db.execute('UPDATE users SET turnover_cents=0, playbonus=0, bonus_balance=0, balance=0, loss_cashback_residual=0 WHERE id=?', (self.uid,))
            m.credit_main_loss_cashback(db, self.uid, 10000, 'test', '1')
            row = db.execute('SELECT playbonus,bonus_balance FROM users WHERE id=?', (self.uid,)).fetchone()
        self.assertEqual((row['playbonus'], row['bonus_balance']), (100, 0))
        self.post('/api/playbonus/claim', {}, 403)
        with m.connect() as db:
            db.execute('UPDATE users SET turnover_cents=? WHERE id=?', (1000000, self.uid))
        pb = self.client.get('/api/vip').get_json()['playbonus']
        self.assertEqual((pb['amount'], pb['unlocked']), (1, True))
        self.assertEqual(self.post('/api/playbonus/claim', {})['amount'], 1)
        self.post('/api/playbonus/claim', {}, 409)
        with m.connect() as db:
            self.assertEqual(db.execute('SELECT balance FROM users WHERE id=?', (self.uid,)).fetchone()['balance'], 100)

    def test_admin_can_set_vip_tier_and_turnover(self):
        with m.connect() as db:
            db.execute('UPDATE users SET turnover_cents=0 WHERE id=?', (self.uid,))
        from unittest.mock import patch
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            r = self.post('/api/admin/users/%d/vip' % self.uid, {'tier': 'silver'})
            self.assertEqual(r['turnover'], 10000)
            self.assertEqual(self.post('/api/admin/users/%d/vip' % self.uid, {'turnover': '12.5'})['turnover'], 12.5)
            self.post('/api/admin/users/%d/vip' % self.uid, {'tier': 'nope'}, 404)
            card = self.client.get('/api/admin/users/%d' % self.uid).get_json()
            self.assertEqual(len(card['vip']['tiers']), 8)

    def test_admin_can_clear_live_feed(self):
        from unittest.mock import patch
        with m.connect() as db:
            db.execute('DELETE FROM recent_wins')
            m.record_transaction(db, self.uid, 'plinko_win', 1234, 'plinko', 1, 'x')
        with patch.object(m, 'ADMIN_IDS', {self.uid}):
            self.assertEqual(self.post('/api/admin/recent-wins/clear', {})['removed'], 1)
        self.assertEqual(self.client.get('/api/recent-wins').get_json()['items'], [])

    def test_gift_win_image_falls_back_to_inventory(self):
        with m.connect() as db:
            db.execute('DELETE FROM recent_wins')
            db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,image_url,floor_price,source) VALUES(?,?,?,?,?,'t')",
                       (self.uid, 'g1', 'Zed Gift', 'https://example.com/z.png', 100))
            m.record_transaction(db, self.uid, 'crash_gift_win', 0, 'crash', 9, 'Zed Gift')
        items = self.client.get('/api/recent-wins').get_json()['items']
        self.assertEqual(items[0]['image'], 'https://example.com/z.png')


if __name__ == '__main__':
    unittest.main()
