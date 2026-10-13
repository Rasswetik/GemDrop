import json
import unittest
from unittest.mock import patch
import test_regressions as base

m=base.m


class OperatorInsightTests(unittest.TestCase):
    def setUp(self):
        base.RegressionTests.setUp(self)
        self.admin=patch.object(m,'ADMIN_IDS',{self.uid});self.admin.start();self.addCleanup(self.admin.stop)

    def mine(self,state='lost',bet=1000,won=0,bonus=0,kind='ton'):
        with m.connect() as db:
            return db.execute("""INSERT INTO rounds(user_id,bet,mines,positions,opened,state,win_total,bonus_used,bet_type,lost_cell)
                                 VALUES(?, ?,3,'[1,8,21]','[0,5]',?,?,?,?,8)""",(self.uid,bet,state,won,bonus,kind)).lastrowid

    def get(self,route):
        r=self.client.get(route);self.assertEqual(r.status_code,200,r.get_data(as_text=True));return r.get_json()

    def test_overview_rtp_is_settled_value_return_excluding_promos_and_bonus(self):
        self.mine(state='won',won=1500);self.mine();self.mine(state='active',bet=5000)
        self.mine(state='won',won=40000,bonus=1000);self.mine(state='won',won=20000,kind='promo_gift')
        with m.connect() as db:
            for suffix,result,bonus in [('normal',{},0),('promo',{'reward_type':'wager_progress'},0),('bonus',{},500)]:
                db.execute("INSERT INTO upgrade_spins(id,user_id,source_name,source_price,target_name,target_price,chance_bp,won,result_json,bonus_used) VALUES(?,?,'TON',500,'Gift',1500,4000,1,?,?)",(str(self.uid)+suffix,self.uid,json.dumps(result),bonus))
            db.execute('INSERT INTO deposits(user_id,amount,admin_id,request_key) VALUES(?,1000,0,?)',(self.uid,'ton:insight'+str(self.uid)))
            db.execute('INSERT INTO deposits(user_id,amount,admin_id,request_key) VALUES(?,50000,?,?)',(self.uid,self.uid,'ADMIN_INSIGHT'+str(self.uid)))
        d=self.get(f'/api/admin/users/{self.uid}/overview')
        self.assertEqual(d['totals']['stake'],25)
        self.assertEqual(d['totals']['payout'],30)
        self.assertEqual(d['totals']['rtp'],120)
        self.assertEqual(d['totals']['games'],3)
        self.assertEqual(d['totals']['deposits'],10)
        self.assertEqual(d['counts']['logs'],8)

    def test_paginated_logs_filter_before_paging_and_show_settled_board_only(self):
        rid=self.mine();self.mine(state='active')
        with m.connect() as db:
            m.record_transaction(db,self.uid,'crypto_deposit',1000,'invoice','test','CryptoBot')
            m.record_transaction(db,self.uid,'gift_sale',500,'inventory','test','Gift')
            m.log_event(db,self.uid,'login',dummy=True)
        prefix=f'/api/admin/users/{self.uid}/timeline'
        d=self.get(prefix+'?category=mines&limit=1&offset=1')
        self.assertEqual(d['total'],2);self.assertEqual(len(d['items']),1)
        boards=self.get(prefix+'?category=mines')['items']
        closed=next(x for x in boards if x['id']=='mines:'+str(rid))
        self.assertEqual(closed['details']['board']['mines'],[1,8,21])
        self.assertEqual(closed['details']['board']['opened'],[0,5])
        self.assertEqual(closed['details']['board']['lost_cell'],8)
        self.assertEqual(next(x for x in boards if x['details']['state']=='active')['details']['board']['mines'],[])
        payments=self.get(prefix+'?category=payments')
        self.assertEqual(payments['total'],1);self.assertEqual(payments['items'][0]['title'],'Пополнение через CryptoBot')
        self.assertEqual(self.get(prefix+'?category=sales')['total'],1)
        self.assertEqual(self.get(prefix)['total'],4)
        self.assertEqual(self.client.get(prefix+'?category=unknown').status_code,400)

    def test_all_current_games_appear_and_arena_fee_counts_only_ton(self):
        with m.connect() as db:
            db.execute("INSERT INTO crash_rounds(id,crash_x100,open_at,launch_at,crash_at,state) VALUES(?,150,0,0,1,'crashed')",(self.uid,))
            db.execute("INSERT INTO crash_bets(round_id,user_id,bet,state,payout) VALUES(?,?,1000,'won',1500)",(self.uid,self.uid))
            db.execute("INSERT INTO limbo_bets(user_id,bet,chance_bp,multiplier_x100,roll,won,payout) VALUES(?,1000,5000,190,1,1,1900)",(self.uid,))
            db.execute("INSERT INTO road_games(user_id,bet,state,payout) VALUES(?,1000,'lost',0)",(self.uid,))
            db.execute("INSERT INTO hilo_games(user_id,bet,cur_rank,state,payout) VALUES(?,1000,6,'cashed',1200)",(self.uid,))
            db.execute("INSERT INTO hilo_room_bets(user_id,round_no,direction,amount,settled,payout) VALUES(?,?,'higher',1000,1,1200)",(self.uid,self.uid))
            cur=db.execute("INSERT INTO arena_rounds(state,open_at,close_at,winner_user_id,total_pool) VALUES('settled',0,0,?,6000)",(self.uid,));ar=cur.lastrowid
            db.execute("INSERT INTO arena_bets(round_id,user_id,amount,gift_amount) VALUES(?,?,6000,1000)",(ar,self.uid))
        d=self.get(f'/api/admin/users/{self.uid}/timeline')
        self.assertEqual({r['category'] for r in d['items']},{'crash','limbo','road','hilo','arena'})
        self.assertEqual(len(d['items']),6)
        arena=next(r for r in d['items'] if r['category']=='arena')
        self.assertEqual(arena['details']['payout'],55)
        totals=self.get(f'/api/admin/users/{self.uid}/overview')['totals']
        self.assertEqual(totals['stake'],110)
        self.assertEqual(totals['payout'],113)

    def test_inventory_and_withdrawals_paginate_and_include_exact_model(self):
        with m.connect() as db:
            for i in range(3):
                db.execute("INSERT INTO inventory(user_id,gift_id,gift_name,fragment_model,floor_price,source) VALUES(?,'pepe','Plush Pepe','Pumpkin',850,'gift_deposit')",(self.uid,))
            db.execute("INSERT INTO withdrawals(user_id,inventory_id,gift_id,gift_name,fragment_model,floor_price,status,processed_at) VALUES(?,1,'pepe','Plush Pepe','Pumpkin',850,'approved',CURRENT_TIMESTAMP)",(self.uid,))
        d=self.get(f'/api/admin/users/{self.uid}/inventory-view?limit=1&offset=2&q=pumpkin')
        self.assertEqual(d['total'],3);self.assertEqual(len(d['items']),1)
        self.assertEqual(d['items'][0]['fragment_model'],'Pumpkin')
        self.assertEqual(self.get(f'/api/admin/users/{self.uid}/overview')['totals']['withdrawn'],8.5)
        self.assertEqual(self.get(f'/api/admin/users/{self.uid}/withdrawals-view')['items'][0]['fragment_model'],'Pumpkin')

    def test_links_are_referrals_or_shared_ips_and_deduplicated(self):
        other=self.uid+1000000
        with m.connect() as db:
            db.execute('INSERT INTO users(id,name) VALUES(?,?)',(other,'Связанный'))
            db.execute('INSERT INTO referrals(referred_id,referrer_id) VALUES(?,?)',(other,self.uid))
            for uid in [other,self.uid]:db.execute("INSERT INTO user_ips(user_id,ip) VALUES(?,'192.0.2.25')",(uid,))
        d=self.get(f'/api/admin/users/{self.uid}/linked')
        self.assertEqual(d['total'],1);self.assertEqual(set(d['items'][0]['relations']),{'referral','shared_ip'})
        self.assertEqual(self.get(f'/api/admin/users/{self.uid}/overview')['counts']['linked'],1)

    def test_dashboard_adds_actual_game_and_audience_series(self):
        before=self.get('/api/admin/dashboard?days=7')['totals']
        self.mine(state='won',won=1500)
        d=self.get('/api/admin/dashboard?days=7')
        self.assertAlmostEqual(d['totals']['stake']-before['stake'],10)
        self.assertAlmostEqual(d['totals']['payout']-before['payout'],15)
        self.assertTrue(all(set(['stake','payout','new_users','depositors','active_players'])<=set(r) for r in d['series']))

    def test_admin_can_issue_and_remove_exact_catalog_model_without_counting_it_as_deposit(self):
        m.save_document('portal_catalog',{'gifts':[{'id':'insight-pepe','name':'Plush Pepe','price_ton':10,'image_url':'/static/img/gift.png'}]})
        with patch.object(m,'gift_model_price',return_value=(4000,'/static/img/gift.png')):
            r=self.client.post(f'/api/admin/users/{self.uid}/inventory',json={'gift_id':'insight-pepe@m:Pumpkin','balance_target':'bonus'})
            self.assertEqual(r.status_code,200,r.get_json())
            item=r.get_json()['item']
            self.assertEqual(item['gift_id'],'insight-pepe');self.assertEqual(item['fragment_model'],'Pumpkin')
            self.assertEqual(item['price_ton'],40);self.assertTrue(item['bonus_origin'])
            drop=self.client.post(f'/api/admin/users/{self.uid}/max-drop',json={'gift_id':'insight-pepe@m:Pumpkin'})
            self.assertEqual(drop.status_code,200,drop.get_json());self.assertEqual(drop.get_json()['max_drop']['price_ton'],40)
            self.assertIn('Pumpkin',drop.get_json()['max_drop']['name'])
        self.assertEqual(self.get(f'/api/admin/users/{self.uid}/overview')['totals']['deposits'],0)
        with m.connect() as db:
            row=db.execute('SELECT gift_id,fragment_model,floor_price FROM inventory WHERE id=?',(item['id'],)).fetchone()
            self.assertEqual((row['gift_id'],row['fragment_model'],row['floor_price']),('insight-pepe','Pumpkin',4000))
        with patch.object(m,'gift_model_price',return_value=(0,'')):
            self.assertEqual(self.client.post(f'/api/admin/users/{self.uid}/inventory',json={'gift_id':'insight-pepe@m:Missing'}).status_code,400)
        self.assertEqual(self.client.delete(f'/api/admin/users/{self.uid}/inventory/{item["id"]}').status_code,200)
        self.assertEqual(self.get(f'/api/admin/users/{self.uid}/inventory-view')['total'],0)

    def test_inspection_routes_are_admin_only_and_unknown_users_are_not_found(self):
        routes=['overview','timeline','inventory-view','withdrawals-view','linked']
        for route in routes:self.assertEqual(self.client.get(f'/api/admin/users/9999999999/{route}').status_code,404)
        with patch.object(m,'ADMIN_IDS',set()):
            for route in routes:self.assertEqual(self.client.get(f'/api/admin/users/{self.uid}/{route}').status_code,403)


if __name__=='__main__':unittest.main()
