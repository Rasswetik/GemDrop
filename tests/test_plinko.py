import unittest
from unittest.mock import patch
import test_regressions as base

m = base.m


class PlinkoTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp
    post = base.RegressionTests.post
    balance = base.RegressionTests.balance

    def test_tables_never_exceed_rtp_and_are_symmetric(self):
        for risk in m.plinko.RISKS:
            table = m.plinko.multipliers(risk, 0.92)
            self.assertEqual(table, table[::-1])
            rtp = sum(p * x / 100 for p, x in zip(m.plinko.PROB, table))
            self.assertLessEqual(rtp, 0.92 + 1e-9)
            self.assertGreater(rtp, 0.88)
        self.assertEqual(sum(m.plinko.PROB), 1)

    def test_path_bits_decide_slot_and_payout(self):
        m.save_document('game_modes', {'plinko': 'on'})
        before = self.balance()
        # all twelve bits set -> ball goes right on every row -> far right slot
        with patch.object(m, 'fairness_draw', return_value=(4095, 1, 'qa')), patch.object(m, 'crash_prize_preview', return_value=None):
            res = self.post('/api/plinko/play', {'bet': '10', 'risk': 'high'})['result']
        self.assertEqual(res['slot'], 12)
        table = m.plinko.multipliers('high', m.game_rtp())
        self.assertEqual(res['multiplier'], table[12] / 100)
        self.assertEqual(res['payout'], min(1000, 10 * table[12] / 100))
        self.assertAlmostEqual(self.balance(), before - 10 + res['payout'])
        self.assertEqual(self.client.get('/api/plinko/state').get_json()['history'][0]['slot'], 12)

    def test_centre_slot_returns_less_than_the_bet(self):
        m.save_document('game_modes', {'plinko': 'on'})
        with patch.object(m, 'fairness_draw', return_value=(0b101010101010, 1, 'qa')), patch.object(m, 'crash_prize_preview', return_value=None):
            res = self.post('/api/plinko/play', {'bet': '10', 'risk': 'low'})['result']
        self.assertEqual(res['slot'], 6)
        self.assertLess(res['payout'], 10)
        self.assertFalse(res['won'])

    def test_validation_and_game_switch(self):
        m.save_document('game_modes', {'plinko': 'on'})
        self.post('/api/plinko/play', {'bet': '10', 'risk': 'crazy'}, 400)
        self.post('/api/plinko/play', {'bet': '0.01', 'risk': 'low'}, 400)
        self.post('/api/plinko/play', {'bet': '99999', 'risk': 'low'}, 400)
        m.save_document('game_modes', {'plinko': 'off'})
        self.post('/api/plinko/play', {'bet': '1', 'risk': 'low'}, 403)


if __name__ == '__main__':
    unittest.main()
