import unittest
import test_regressions as base

m = base.m


class FrameTests(unittest.TestCase):
    setUp = base.RegressionTests.setUp

    def test_select_frame_and_feed_lookup(self):
        lb = m.leaderboard
        with m.connect() as db:
            db.execute("DELETE FROM user_medals WHERE user_id=?", (self.uid,))
            db.execute("INSERT INTO user_medals(user_id,medal_key,title,place,period_id,shown) VALUES(?,?,?,?,?,1)", (self.uid, 'hw1', '', 0, 0))
        self.assertEqual(self.client.get('/api/frames').status_code, 200)
        self.assertEqual(self.client.post('/api/frames/select', json={'key': 'vip'}).status_code, 409)
        r = self.client.post('/api/frames/select', json={'key': 'hw1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with m.connect() as db:
            self.assertEqual(lb.frames_for(db, [self.uid, 999999]), {self.uid: 'hw1'})
        self.client.post('/api/frames/select', json={'key': ''})
        with m.connect() as db:
            self.assertEqual(lb.frames_for(db, [self.uid]), {})
            db.execute("DELETE FROM user_medals WHERE user_id=?", (self.uid,))


if __name__ == '__main__':
    unittest.main()
