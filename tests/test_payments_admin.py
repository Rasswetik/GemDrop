import hashlib
import asyncio
import hmac
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_regressions as base
import management

m = base.m


class PaymentAdminTests(unittest.TestCase):
    post = base.RegressionTests.post
    balance = base.RegressionTests.balance
    _seed_models = base.RegressionTests._seed_models

    def setUp(self):
        base.RegressionTests.setUp(self)
        for key in ('crypto_pay_settings','withdrawal_settings','maintenance'):
            old = m.read_document(key)
            self.addCleanup(m.save_document,key,old or {})
        self.token = 'TEST_ONLY_'+str(self.uid)
        m.save_document('crypto_pay_settings',dict(enabled=True,token=self.token,testnet=False))
        m.save_document('withdrawal_settings',dict(min_ton_connect_deposit_cents=300))
        self.admin = patch.object(m,'ADMIN_IDS',{self.uid}); self.admin.start(); self.addCleanup(self.admin.stop)

    def provider(self, result):
        return Mock(status_code=200,json=Mock(return_value=dict(ok=True,result=result)))

    def create_invoice(self,testnet=False):
        if testnet:
            m.save_document('crypto_pay_settings',dict(enabled=True,token=self.token,testnet=True))
        self.invoice = dict(invoice_id=self.uid,asset='TON',currency_type='crypto',amount='10.00',
                            status='active',bot_invoice_url='https://t.me/CryptoBot?start=TEST_ONLY')
        def respond(url,**kw):
            self.invoice['payload'] = kw['json']['payload']
            self.assertEqual(kw['headers']['Crypto-Pay-API-Token'],self.token)
            self.assertIn('GemDrop',kw['json']['description'])
            self.assertTrue(url.startswith('https://testnet-pay.crypt.bot/' if testnet else 'https://pay.crypt.bot/'))
            return self.provider(self.invoice)
        with patch.object(management.requests,'post',side_effect=respond):
            self.order = self.post('/api/cryptobot/deposit/create',{'amount':'10'})
        return self.order

    def webhook(self,invoice=None,signature=None,age=None):
        data = dict(update_type='invoice_paid',request_date=age or datetime.now(timezone.utc).isoformat(),payload=invoice or dict(self.invoice,status='paid'))
        body = json.dumps(data,ensure_ascii=False,indent=2).encode()
        secret = hmac.new(self.token.encode(),b'gemdrop-crypto-webhook-v1',hashlib.sha256).hexdigest()
        signature = signature or hmac.new(hashlib.sha256(self.token.encode()).digest(),body,hashlib.sha256).hexdigest()
        return self.client.post('/api/cryptobot/webhook/'+secret,data=body,content_type='application/json',headers={'crypto-pay-api-signature':signature})

    def nft(self,key='gift',mode='inventory',price=1000):
        info=dict(external_key=f'tg:{self.uid}:{key}',sender_user_id=self.uid,gift_id='qa-pepe',
                  gift_name='Plush Pepe',slug=f'PlushPepe-{self.uid}',fragment_number=str(self.uid),
                  fragment_model='Pumpkin',image_url='https://example.com/pepe.png',floor_price=price,
                  external_url=f'https://t.me/nft/PlushPepe-{self.uid}',raw_json='{}')
        with patch.object(m,'relayer_settings',return_value=dict(delivery_mode=mode)),patch.object(m,'notify_user_async'):
            status=m._relayer_credit(info)
        return info,status

    def test_nft_inventory_counts_as_confirmed_deposit_and_is_idempotent(self):
        info,status=self.nft()
        self.assertEqual(status,'credited')
        self.assertEqual(self.balance(),100)
        with m.connect() as db:
            self.assertEqual(m.confirmed_deposit_total(db,self.uid),850)
            self.assertEqual(m.confirmed_deposit_last_24h_cents(db,self.uid),850)
            state=management.withdrawal_state(db,self.uid,m.withdrawal_settings())
        self.assertEqual(state['wager_remaining'],17)
        self.assertEqual([r['code'] for r in state['reasons']],['wager'])
        with patch.object(m,'relayer_settings',return_value=dict(delivery_mode='balance')):
            self.assertEqual(m._relayer_credit(info),'duplicate')
        payments=self.client.get('/api/admin/payments?user_id='+str(self.uid)).get_json()
        self.assertEqual(payments['total'],1)
        self.assertEqual(payments['items'][0]['model'],'Pumpkin')
        self.assertEqual(payments['items'][0]['amount'],8.5)

    def test_nft_balance_mode_counts_once(self):
        self.nft(mode='balance')
        self.assertEqual(self.balance(),108.5)
        with m.connect() as db:self.assertEqual(m.confirmed_deposit_total(db,self.uid),850)

    def test_admin_credit_never_opens_withdrawal_or_enters_payment_history(self):
        self.post(f'/api/admin/users/{self.uid}/deposit',dict(amount='50',request_key='admin_ONLY_'+str(self.uid)))
        self.post(f'/api/admin/users/{self.uid}/balance',dict(amount='20',kind='main',mode='add'))
        with m.connect() as db:self.assertEqual(m.confirmed_deposit_total(db,self.uid),0)
        data=self.client.get(f'/api/admin/users/{self.uid}').get_json()['user']['withdrawal_state']
        self.assertEqual(data['deposit_remaining'],3)
        self.assertEqual(data['wager_remaining'],0)
        self.assertEqual(self.client.get('/api/admin/payments?user_id='+str(self.uid)).get_json()['total'],0)
        grants=self.client.get('/api/admin/grants').get_json()['items']
        self.assertEqual(len([r for r in grants if r['user_id']==self.uid]),2)

    def test_nft_stake_advances_real_wager_but_admin_gift_does_not(self):
        self._seed_models('qa-pepe');self.nft()
        m.save_document('game_modes',{'mines':'on'})
        with m.connect() as db:
            item=db.execute('SELECT id FROM inventory WHERE user_id=?',(self.uid,)).fetchone()['id']
        self.post('/api/game/start',{'inventory_id':item,'mines':3})
        with m.connect() as db:
            state=management.withdrawal_state(db,self.uid,m.withdrawal_settings())
            self.assertEqual(state['wager_remaining'],8.5)
        self.assertFalse(m.gift_counts_for_withdrawal_wager(dict(source='admin',promo_locked=0)))
        self.assertFalse(m.gift_counts_for_withdrawal_wager(dict(source='promo_claimed',promo_locked=0)))

    def test_signed_cryptobot_payment_credits_once_across_webhook_and_polling(self):
        self.create_invoice();before=self.balance()
        first=self.webhook();self.assertEqual(first.status_code,200,first.get_data(as_text=True))
        self.assertTrue(first.get_json()['credited'])
        self.assertFalse(self.webhook().get_json()['credited'])
        verify=self.post('/api/cryptobot/deposit/'+self.order['order_id']+'/verify',{})
        self.assertEqual(verify['status'],'credited')
        self.assertEqual(self.balance(),before+10)
        with m.connect() as db:
            self.assertEqual(m.confirmed_deposit_total(db,self.uid),1000)
            self.assertEqual(management.withdrawal_state(db,self.uid,m.withdrawal_settings())['wager_remaining'],20)

    def test_polling_uses_provider_confirmation_and_webhook_cannot_double_credit(self):
        self.create_invoice()
        with patch.object(management.requests,'post',return_value=self.provider(dict(items=[dict(self.invoice,status='paid')]))):
            self.assertEqual(self.post('/api/cryptobot/deposit/'+self.order['order_id']+'/verify',{})['status'],'credited')
        self.assertFalse(self.webhook().get_json()['credited'])
        self.assertEqual(self.balance(),110)

    def test_webhook_before_invoice_http_response_does_not_regress_paid_order(self):
        self.invoice=dict(invoice_id=self.uid,asset='TON',currency_type='crypto',amount='10.00',status='paid',bot_invoice_url='https://t.me/CryptoBot?start=TEST_ONLY')
        def response(url,**kw):
            self.invoice['payload']=kw['json']['payload']
            self.assertEqual(self.webhook().status_code,200)
            return self.provider(dict(self.invoice,status='active'))
        with patch.object(management.requests,'post',side_effect=response):
            self.order=self.post('/api/cryptobot/deposit/create',{'amount':'10'})
        self.assertEqual(self.post('/api/cryptobot/deposit/'+self.order['order_id']+'/verify',{})['status'],'credited')
        self.assertEqual(self.balance(),110)

    def test_signed_webhook_recovers_lost_invoice_creation_response(self):
        self.invoice=dict(invoice_id=self.uid,asset='TON',currency_type='crypto',amount='10.00',status='paid')
        def timeout(url,**kw):
            self.invoice['payload']=kw['json']['payload']
            raise management.requests.Timeout()
        with patch.object(management.requests,'post',side_effect=timeout):
            self.post('/api/cryptobot/deposit/create',{'amount':'10'},502)
        self.assertEqual(self.webhook().status_code,200)
        self.assertEqual(self.balance(),110)
        self.assertFalse(self.webhook().get_json()['credited'])

    def test_overlapping_delivery_attempts_are_serialized_in_database(self):
        calls=[]
        async def transfer(wid):
            calls.append(wid)
            await asyncio.sleep(.02)
            return dict(status='completed')
        guarded=management.serialized_withdrawal(transfer,dict(connect=m.connect))
        async def run():
            return await asyncio.gather(guarded(self.uid),guarded(self.uid))
        results=asyncio.run(run())
        self.assertEqual(calls,[self.uid])
        self.assertEqual({r['status'] for r in results},{'completed','in_progress'})
        with m.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM withdrawal_delivery_leases WHERE withdrawal_id=?',(self.uid,)).fetchone())

    def test_cryptobot_rejects_forged_amount_asset_payload_signature_and_stale_updates(self):
        self.create_invoice()
        for field,value in [('amount','20.00'),('amount','NaN'),('asset','USDT'),('payload','foreign'),('currency_type','fiat')]:
            self.assertEqual(self.webhook(dict(self.invoice,status='paid',**{field:value})).status_code,409)
        self.assertEqual(self.webhook(signature='0'*64).status_code,403)
        self.assertEqual(self.webhook(age='2020-01-01T00:00:00Z').status_code,400)
        self.assertEqual(self.balance(),100)

    def test_testnet_payments_cannot_credit_real_balance_or_qualify_for_withdrawal(self):
        self.create_invoice(testnet=True)
        self.assertEqual(self.webhook().status_code,200)
        self.assertEqual(self.balance(),100)
        self.assertEqual(self.post('/api/cryptobot/deposit/'+self.order['order_id']+'/verify',{})['status'],'test_paid')
        with m.connect() as db:self.assertEqual(m.confirmed_deposit_total(db,self.uid),0)

    def test_pending_invoice_is_reused_and_another_user_cannot_verify_it(self):
        self.create_invoice()
        second=self.post('/api/cryptobot/deposit/create',{'amount':'20'})
        self.assertEqual(second['order_id'],self.order['order_id'])
        self.assertEqual(second['amount'],10)
        with self.client.session_transaction() as s:s['uid']=self.uid+100000
        self.post('/api/cryptobot/deposit/'+self.order['order_id']+'/verify',{},404)

    def test_settings_verify_app_identity_and_never_return_api_token(self):
        with patch.object(management.requests,'post',return_value=self.provider(dict(app_id=123,name='GemDrop'))):
            data=self.post('/api/admin/cryptobot',dict(token=self.token,enabled=True,testnet=False))
        self.assertEqual(data['app_id'],123)
        self.assertNotIn(self.token,json.dumps(data))
        self.assertTrue(data['webhook_url'].startswith('http://localhost/api/cryptobot/webhook/'))
        self.create_invoice()
        with patch.object(management.requests,'post',return_value=self.provider(dict(app_id=124,name='GemDrop'))):
            self.post('/api/admin/cryptobot',dict(token='DIFFERENT_TEST_ONLY',enabled=True),409)

    def test_wager_clear_add_and_set_are_audited_without_faking_deposits(self):
        with m.connect() as db:m.add_withdrawal_wager_requirement(db,self.uid,1000)
        path=f'/api/admin/users/{self.uid}/wager'
        self.assertEqual(self.post(path,dict(mode='add',amount='5'))['wager_remaining'],25)
        self.assertEqual(self.post(path,dict(mode='set_remaining',amount='12'))['wager_remaining'],12)
        data=self.post(path,dict(mode='clear'))
        self.assertEqual(data['wager_remaining'],0)
        self.assertEqual(data['deposited'],0)
        self.assertFalse(data['allowed'])
        with m.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM admin_log WHERE user_id=? AND action='withdrawal_wager'",(self.uid,)).fetchone()['n'],3)

    def test_standalone_admin_and_finance_routes_are_private_and_history_search_is_paginated(self):
        with self.client.get('/admin') as r:
            self.assertEqual(r.status_code,200);html=r.get_data(as_text=True)
        self.assertIn('/static/js/admin.js',html)
        self.assertNotIn('minesPage',html)
        self.nft();self.nft(key='gift2')
        self.assertEqual(len(self.client.get(f'/api/admin/payments?user_id={self.uid}&limit=1&offset=1').get_json()['items']),1)
        self.assertEqual(self.client.get('/api/admin/payments?q=%25').get_json()['total'],0)
        self.assertEqual(self.client.get('/api/admin/payments?from=bad').status_code,400)
        data=self.client.get('/api/admin/players?q='+str(self.uid)).get_json()
        self.assertEqual(data['total'],1);self.assertEqual(data['items'][0]['wager_remaining'],34)
        with patch.object(m,'ADMIN_IDS',set()):
            for path in ('/admin','/admin/tools','/api/admin/payments','/api/admin/dashboard','/api/admin/cryptobot','/api/admin/players'):
                self.assertEqual(self.client.get(path).status_code,403,path)

    def test_dashboard_excludes_admin_and_counts_accepted_nft_value(self):
        before=self.client.get('/api/admin/dashboard?days=7').get_json()['totals']
        self.nft()
        self.post(f'/api/admin/users/{self.uid}/deposit',dict(amount='200',request_key='admin_DASH_'+str(self.uid)))
        after=self.client.get('/api/admin/dashboard?days=7').get_json()['totals']
        self.assertAlmostEqual(after['deposits']-before['deposits'],8.5)
        self.assertEqual(after['deposit_count']-before['deposit_count'],1)
        self.assertAlmostEqual(after['admin_grants']-before['admin_grants'],200)

    def test_user_and_payment_search_handles_cyrillic_case_insensitively(self):
        with m.connect() as db:
            db.execute("UPDATE users SET name='Илья' WHERE id=?",(self.uid,))
        self.nft()
        from urllib.parse import quote
        self.assertEqual(self.client.get('/api/admin/players?q='+quote('илья')).get_json()['total'],1)
        self.assertEqual(self.client.get('/api/admin/payments?q='+quote('ИЛЬЯ')).get_json()['total'],1)

    def test_exact_model_matching_in_portal_and_telegram_account(self):
        self._seed_models('qa-pepe')
        row=dict(gift_id='qa-pepe',gift_name='MM Pepe · Classic',fragment_model='Classic')
        exact=dict(id='good',name='MM Pepe',model=dict(name='Classic'),price='5')
        wrong=dict(id='bad',name='MM Pepe',model='Other',price='1')
        near=dict(id='near',name='MM Pepe Locket',model='Classic',price='2')
        self.assertEqual(management.exact_portal_items([wrong,near,exact],row,m.read_catalog()['gifts']),[exact])
        Model=type('StarGiftAttributeModel',(),{})
        def gift(title,model,number):
            attr=Model();attr.name=model
            return SimpleNamespace(gift=SimpleNamespace(title=title,slug=title.replace(' ','')+'-'+str(number),num=number,attributes=[attr]))
        wrong_gift=gift('MM Pepe','Other',9);near_gift=gift('MM Pepe Locket','Classic',9);good=gift('MM Pepe','Classic',9)
        entry,info=m._relayer_match_withdrawal(row,[wrong_gift,near_gift,good])
        self.assertIs(entry,good)
        self.assertEqual(m._relayer_match_withdrawal(dict(row,fragment_number='9'),[near_gift,wrong_gift]),(None,None))
        self.assertEqual(m._relayer_match_withdrawal(dict(row,external_url='https://t.me/nft/MMPepe-9'),[wrong_gift]),(None,None))

    def test_procurement_has_exact_traits_and_retry_does_not_change_balance(self):
        self._seed_models('qa-pepe')
        with m.connect() as db:
            cur=db.execute("INSERT INTO withdrawals(user_id,inventory_id,gift_id,gift_name,fragment_model) VALUES(?,999,'qa-pepe','MM Pepe · Classic','Classic')",(self.uid,));wid=cur.lastrowid
        d=self.client.get(f'/api/admin/withdrawals/{wid}/procurement').get_json()
        self.assertEqual(d['collection'],'MM Pepe');self.assertEqual(d['model'],'Classic')
        with patch.object(m,'Thread'):
            # Route imports Thread directly; prevent a real provider call in this test.
            with patch('threading.Thread'):
                self.post(f'/api/admin/withdrawals/{wid}/retry',{})
        self.assertEqual(self.balance(),100)


if __name__=='__main__':
    unittest.main()
