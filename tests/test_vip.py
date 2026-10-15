import unittest
import test_regressions as base
import vip

m = base.m


class VipTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp
    post = base.RegressionTests.post

    def test_tier_state(self):
        s = vip.tier_state(0)
        self.assertEqual((s['index'], s['reached'], s['next']['name']), (0, False, 'Quartz'))
        s = vip.tier_state(173341)
        self.assertEqual((s['index'], s['reached'], s['next']['name']), (0, True, 'Amber'))
        self.assertAlmostEqual(s['next']['left'], 766.59, places=2)
        s = vip.tier_state(100000000 * 100)
        self.assertEqual(s['index'], 6)
        self.assertIsNone(s['next'])

    def test_api_and_feed(self):
        data = self.client.get('/api/vip').get_json()
        self.assertEqual(len(data['tiers']), 7)
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

    def test_claim_gives_tickets_and_bonus_once(self):
        with m.connect() as db:
            db.execute('UPDATE users SET turnover_cents=?, tickets=0, bonus_balance=0 WHERE id=?', (60000, self.uid))
            db.execute('DELETE FROM vip_claims')
        self.post('/api/vip/claim', {'tier': 'bronze'}, 403)
        self.post('/api/vip/claim', {'tier': 'nope'}, 404)
        data = self.client.get('/api/vip').get_json()
        self.assertEqual(data['pending'], 1)
        res = self.post('/api/vip/claim', {'tier': 'iron'})
        self.assertEqual((res['reward']['tickets'], res['reward']['bonus_ton']), (500, 2))
        self.post('/api/vip/claim', {'tier': 'iron'}, 409)
        with m.connect() as db:
            row = db.execute('SELECT tickets,bonus_balance FROM users WHERE id=?', (self.uid,)).fetchone()
        self.assertEqual((row['tickets'], row['bonus_balance']), (500, 200))
        self.assertEqual(self.client.get('/api/vip').get_json()['pending'], 0)

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
