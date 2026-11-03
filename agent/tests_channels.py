"""🤖 Агент і канали продажу (agent/channels.py): магазини й DigiKey — лише пропозиції, застосовує людина.
Мережу DigiKey підмінено (розрахунок «за правилами» — `dk_row`, надсилання — `create_run` / `start` / `start_push`).

python manage.py test agent.tests_channels --settings=tabele.settings_test
"""
import json
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest import mock

from agent import channels, decide
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base, resp, text, tool
from bots.models import DigiKeyListing
from shop.models import Shop, ShopListing

NOW = [(1, Decimal('12.0000')), (10, Decimal('11.0000'))]
NEW = [(1, Decimal('13.5000')), (10, Decimal('12.2000'))]


def fake_row(new=NEW, dk=NOW, state='ok'):
    return NS(state=state, state_text='', notes=['за правилами'], dk=list(dk), new=list(new),
              differs=state == 'ok' and list(new) != list(dk), change_pct=Decimal('12.5'),
              max_change_pct=Decimal('12.5'))


class ChannelTests(Base):
    def setUp(self):
        super().setUp()
        self.shop = Shop.objects.create(name='Web', slug='webshop', currency='EUR')
        self.listing = ShopListing.objects.create(shop=self.shop, product=self.ant, is_visible=True,
                                                  price=Decimal('19.90'))
        self.dk = DigiKeyListing.objects.create(product=self.ant, dk_offer_id='o-1', sync_status='published',
                                                dk_title='DK AN', dk_prices=[{'qty': 1, 'price': '12'}],
                                                dk_lead_time_days=11)
        self.patches = [mock.patch('agent.channels.dk_row', side_effect=lambda listing: fake_row()),
                        mock.patch('agent.channels._dk_currency', return_value='USD')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_reads_channels_and_proposes(self):
        task = self.task('Сховай антену в магазині webshop, онови ціни на DigiKey за правилами й постав строк 14 днів')
        fake = self.go(task, [
            resp('tool_use', tool('t1', 'get_sales_channels', sku='AN220213-01C')),
            resp('tool_use',
                 tool('t2', 'propose_shop', sku='AN220213-01C', shop='webshop', visible=False, evidence='завдання'),
                 tool('t3', 'propose_dk_prices', sku='AN220213-01C', evidence='завдання'),
                 tool('t4', 'propose_dk_lead', sku='AN220213-01C', days=14, evidence='завдання')),
            resp('end_turn', text('Готово.')),
        ])
        info = json.loads(fake.calls[1]['messages'][-1]['content'][0]['content'])
        self.assertEqual(info['shops'][0]['shop'], 'webshop')
        self.assertTrue(info['shops'][0]['visible'])
        self.assertEqual(info['digikey']['by_rules']['prices'], [[1, '13.5'], [10, '12.2']])
        kinds = sorted(task.proposals.values_list('kind', flat=True))
        self.assertEqual(kinds, sorted([AgentProposal.SHOP, AgentProposal.DK_PRICES, AgentProposal.DK_LEAD]))
        self.assertTrue(ShopListing.objects.get(pk=self.listing.pk).is_visible)        # без ✓ — нічого

    def _proposal(self, kind, **data):
        task = AgentTask.objects.create(owner=self.user, title='x', instruction='x', status=AgentTask.REVIEW)
        from agent.tools import Context
        ctx = Context(task)
        handler = channels.HANDLERS[kind]
        handler(ctx, {'sku': 'AN220213-01C', 'evidence': 'завдання', **data})
        return task, task.proposals.get()

    def test_shop_hide_and_changed_meanwhile(self):
        task, p = self._proposal('propose_shop', shop='webshop', visible=False)
        self.assertEqual(decide.apply_selected(task, [p.pk], self.user)['applied'], 1)
        self.assertFalse(ShopListing.objects.get(pk=self.listing.pk).is_visible)
        task2, p2 = self._proposal('propose_shop', shop='webshop', price=15)
        ShopListing.objects.filter(pk=self.listing.pk).update(price=Decimal('18'))
        out = decide.apply_selected(task2, [p2.pk], self.user)
        self.assertIn('змінилось', out['failed'][0])

    def test_shop_add_with_price(self):
        other = Shop.objects.create(name='Market', slug='market', currency='EUR')
        task, p = self._proposal('propose_shop', shop='market', visible=True, price=12.5)
        decide.apply_selected(task, [p.pk], self.user)
        listing = ShopListing.objects.get(shop=other, product=self.ant)
        self.assertTrue(listing.is_visible)
        self.assertEqual(listing.price, Decimal('12.50'))

    def test_dk_prices_go_through_price_run(self):
        task, p = self._proposal('propose_dk_prices')
        self.assertEqual((p.data['new'], p.data['currency']), ([[1, '13.5'], [10, '12.2']], 'USD'))
        run = NS(pk=7)
        with mock.patch('bots.services.dk_prices.create_run', return_value=run) as create, \
                mock.patch('bots.services.dk_prices.start') as start:
            out = decide.apply_selected(task, [p.pk], self.user)
        self.assertEqual(out['applied'], 1)
        self.assertEqual(create.call_args[0][0][0].new, NEW)
        start.assert_called_once_with(run)
        p.refresh_from_db()
        self.assertIn('/7/', p.result_url)

    def test_dk_prices_recomputed_differently_is_refused(self):
        task, p = self._proposal('propose_dk_prices')
        with mock.patch('agent.channels.dk_row', return_value=fake_row(new=[(1, Decimal('14'))])), \
                mock.patch('bots.services.dk_prices.create_run') as create:
            out = decide.apply_selected(task, [p.pk], self.user)
        self.assertIn('змінились', out['failed'][0])
        create.assert_not_called()

    def test_dk_prices_same_or_unavailable(self):
        from agent.tools import Context, ToolError
        task = AgentTask.objects.create(owner=self.user, title='x', instruction='x')
        with mock.patch('agent.channels.dk_row', return_value=fake_row(new=NOW)):
            self.assertIn('уже як за правилами', channels.propose_dk_prices(Context(task), {'sku': 'AN220213-01C',
                                                                                         'evidence': 'x'}))
        with mock.patch('agent.channels.dk_row', return_value=fake_row(state='no_rate')):
            with self.assertRaises(ToolError):
                channels.propose_dk_prices(Context(task), {'sku': 'AN220213-01C', 'evidence': 'x'})
        self.assertFalse(task.proposals.exists())

    def test_dk_lead(self):
        task, p = self._proposal('propose_dk_lead', days=14)
        with mock.patch('bots.services.dk_lead.start_push', return_value=True) as push:
            self.assertEqual(decide.apply_selected(task, [p.pk], self.user)['applied'], 1)
        self.assertEqual(DigiKeyListing.objects.get(pk=self.dk.pk).dk_lead_time_days, 14)
        push.assert_called_once()

    def test_review_page_shows_channels(self):
        task, p = self._proposal('propose_shop', shop='webshop', visible=False)
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/agent/agenttask/{task.pk}/review/')
        self.assertContains(page, 'Магазин webshop')
        self.assertContains(page, 'сховати')


class RealPlanTests(Base):
    def test_real_rules_path_without_prices(self):
        """Справжній `dk_prices.plan` (без підміни): у товару немає цін Minerva — пропозиції немає, пояснення є."""
        from agent.tools import Context, ToolError
        DigiKeyListing.objects.create(product=self.ant, dk_offer_id='o-1', sync_status='published', dk_title='DK',
                                      dk_prices=[{'qty': 1, 'price': '12'}])
        task = AgentTask.objects.create(owner=self.user, title='x', instruction='x')
        info = json.loads(channels.sales_channels(Context(task), {'sku': 'AN220213-01C'}))
        self.assertEqual(info['digikey']['by_rules']['state'], 'no_prices')
        with self.assertRaises(ToolError):
            channels.propose_dk_prices(Context(task), {'sku': 'AN220213-01C', 'evidence': 'x'})
