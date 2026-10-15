import unittest
from unittest.mock import patch
import test_regressions as base

m = base.m
TRAP = m.mines_trap_roll


class PlinkoTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp
    post = base.RegressionTests.post
    balance = base.RegressionTests.balance

    def test_tables_never_exceed_rtp_and_are_symmetric(self):
        for risk in m.plinko.RISKS:
            table = m.plinko.multipliers(risk, 0.95)
            self.assertEqual(table, table[::-1])
            rtp = sum(p * x / 100 for p, x in zip(m.plinko.PROB, table))
            self.assertLessEqual(rtp, 0.95 + 1e-9)
            self.assertGreater(rtp, 0.9)
        self.assertEqual(sum(m.plinko.PROB), 1)

    def test_path_bits_decide_slot_and_payout(self):
        m.save_document('game_modes', {'plinko': 'on'})
        before = self.balance()
        # all twelve bits set -> ball goes right on every row -> far right slot
        with patch.object(m, 'fairness_draw', return_value=(65535, 1, 'qa')), patch.object(m, 'crash_prize_preview', return_value=None):
            res = self.post('/api/plinko/play', {'bet': '10', 'risk': 'high'})['result']
        self.assertEqual(res['slot'], 16)
        table = m.plinko.multipliers('high', m.plinko_rtp())
        self.assertEqual(res['multiplier'], table[16] / 100)
        self.assertEqual(res['payout'], min(300000, 10 * table[16] / 100))
        self.assertAlmostEqual(self.balance(), before - 10 + res['payout'])
        self.assertEqual(self.client.get('/api/plinko/state').get_json()['history'][0]['slot'], 16)

    def test_centre_slot_returns_less_than_the_bet(self):
        m.save_document('game_modes', {'plinko': 'on'})
        with patch.object(m, 'fairness_draw', return_value=(0b1010101010101010, 1, 'qa')), patch.object(m, 'crash_prize_preview', return_value=None):
            res = self.post('/api/plinko/play', {'bet': '10', 'risk': 'low'})['result']
        self.assertEqual(res['slot'], 8)
        self.assertLess(res['payout'], 10)
        self.assertFalse(res['won'])

    def test_validation_and_game_switch(self):
        m.save_document('game_modes', {'plinko': 'on'})
        self.post('/api/plinko/play', {'bet': '10', 'risk': 'crazy'}, 400)
        self.post('/api/plinko/play', {'bet': '0.01', 'risk': 'low'}, 400)
        self.post('/api/plinko/play', {'bet': '99999', 'risk': 'low'}, 400)
        m.save_document('game_modes', {'plinko': 'off'})
        self.post('/api/plinko/play', {'bet': '1', 'risk': 'low'}, 403)

    def test_mines_coefficient_ignores_rtp_and_trap_rate_follows_it(self):
        self.assertEqual(m.multiplier_for(3, 2, 0.5), m.multiplier_for(3, 2, 0.99))
        self.assertAlmostEqual(m.multiplier_for(3, 1, 0.5), 25 / 22, places=4)
        self.assertFalse(any(TRAP(1.0) for _ in range(300)))
        self.assertTrue(all(TRAP(0.0) for _ in range(300)))


if __name__ == '__main__':
    unittest.main()
