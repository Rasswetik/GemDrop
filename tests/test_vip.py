import unittest
import test_regressions as base
import vip

m = base.m


class VipTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp

    def test_tier_state(self):
        s = vip.tier_state(0)
        self.assertEqual((s['index'], s['reached'], s['next']['name']), (0, False, 'Кварц'))
        s = vip.tier_state(173341)
        self.assertEqual((s['index'], s['reached'], s['next']['name']), (0, True, 'Янтарь'))
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


if __name__ == '__main__':
    unittest.main()
