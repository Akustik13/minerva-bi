"""📦 Коробки для агента й MCP (agent/tools_packaging.py): каталог, використання, схожі, пропозиції створити /
змінити / оприбуткувати / злити — застосовує людина ✓.

python manage.py test agent.tests_tools_packaging --settings=tabele.settings_test
"""
from decimal import Decimal

from django.core.cache import cache

from agent import decide, mcp, toolsets, tools
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base
from agent.tests_tools_sales import person
from sales.models import SalesOrder
from shipping.models import Carrier, PackagingMaterial, ProductPackaging, Shipment


class _Data(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(cache.clear)
        from core.shared_cache import cache as shared
        shared.delete_many([f'pack-usage:{d}' for d in (30, 90, 180, 365, 730)])
        self.big = PackagingMaterial.objects.create(name='Big', code='BX-1', length_cm=30, width_cm=20,
                                                    height_cm=10, track_stock=True)
        self.twin = PackagingMaterial.objects.create(name='Big twin', code='BX-2', length_cm=Decimal('30.5'),
                                                     width_cm=20, height_cm=10)
        carrier = Carrier.objects.create(name='UPS', carrier_type='ups', sender_country='DE')
        Shipment.objects.create(order=SalesOrder.objects.create(source='webshop', order_number='P-1'),
                                carrier=carrier, status=Shipment.Status.LABEL_READY, weight_kg=Decimal('1'),
                                length_cm=30, width_cm=20, height_cm=10)

    def ctx(self, user=None):
        return tools.Context(AgentTask.objects.create(owner=user or self.user, title='t', instruction='t'))

    def call(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertFalse(err, out)
        return out

    def fails(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertTrue(err, out)
        return out


class RightsTests(_Data):
    def names(self, user):
        return {t['name'] for t in toolsets.tools_for(user)}

    def test_by_role(self):
        everything = {'list_packaging', 'packaging_usage', 'similar_packaging', 'propose_packaging',
                      'propose_packaging_merge'}
        self.assertTrue(everything <= self.names(self.user))
        self.assertTrue(everything <= set(mcp.names(self.user)))
        self.assertFalse(everything & self.names(person('ro', 'readonly')))
        ro = person('ro2', 'readonly')
        if ro.has_perm('shipping.view_packagingmaterial'):            # лише перегляд — без пропозицій
            self.assertFalse({'propose_packaging', 'propose_packaging_merge'} & self.names(ro))


class ReadTests(_Data):
    def test_list_usage_similar(self):
        out = self.call('list_packaging', query='BX')
        self.assertIn('BX-1', out)
        self.assertIn('1 шт у 1 відправл.', out)
        out = self.call('packaging_usage', days=30)
        self.assertIn('Використано коробок', out)
        self.assertIn('Big twin', out)                                # активна, але не використовувалась
        out = self.call('similar_packaging', tolerance_cm=1)
        self.assertIn(f'злити в #{self.big.pk}', out)
        self.assertIn('Схожих коробок', self.call('similar_packaging', tolerance_cm=0.1))

    def test_low_stock_filter(self):
        PackagingMaterial.objects.filter(pk=self.big.pk).update(min_stock=5, stock_qty=1)
        out = self.call('list_packaging', low_stock=True)
        self.assertIn('BX-1', out)
        self.assertNotIn('BX-2', out)


class ProposalTests(_Data):
    def test_merge_applied_by_human(self):
        ctx = self.ctx()
        self.call('propose_packaging_merge', ctx, target='BX-1', merge=['#%d' % self.twin.pk], evidence='схожі')
        p = AgentProposal.objects.get(task=ctx.task)
        self.assertEqual(p.kind, AgentProposal.PACK_MERGE)
        self.assertTrue(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)    # до ✓ нічого не змінилось
        self.assertEqual(decide.apply_one(p, self.user), '')
        self.assertFalse(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)
        self.assertIn(f'/packagingmaterial/{self.big.pk}/', p.result_url)
        self.assertIn('Злити коробки', decide.describe(p)[0])

    def test_archived_box_merges_into_active_but_not_the_other_way(self):
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(is_active=False)
        ctx = self.ctx()
        self.call('propose_packaging_merge', ctx, target='BX-1', merge=['BX-2'], evidence='архівний дубль')
        p = AgentProposal.objects.get(task=ctx.task)
        self.assertEqual(decide.apply_one(p, self.user), '')
        self.assertIn(f'злито в #{self.big.pk}', PackagingMaterial.objects.get(pk=self.twin.pk).notes)
        self.assertIn('в архіві', self.fails('propose_packaging_merge', target='BX-2', merge=['BX-1'],
                                             evidence='x'))

    def test_merge_refuses_other_type(self):
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(box_type=PackagingMaterial.BoxType.ENVELOPE)
        self.assertIn('різні типи', self.fails('propose_packaging_merge', target='BX-1', merge=['BX-2'],
                                                evidence='x'))

    def test_create_update_receive_count(self):
        ctx = self.ctx()
        self.call('propose_packaging', ctx, action='create', evidence='лист постачальника',
                  fields={'name': 'Tube', 'code': 'TB-1', 'box_type': 'tube', 'length_cm': '60', 'width_cm': 8,
                          'height_cm': 8})
        self.call('propose_packaging', ctx, action='update', box='BX-1', fields={'max_weight_kg': '12,5'},
                  evidence='x')
        self.call('propose_packaging', ctx, action='receive', box='BX-1', qty=50, evidence='прийшли')
        self.call('propose_packaging', ctx, action='count', box='BX-1', qty=48, evidence='порахували')
        props = list(AgentProposal.objects.filter(task=ctx.task).order_by('pk'))
        self.assertEqual(len(props), 4)
        out = decide.apply_selected(ctx.task, [p.pk for p in props], self.user)
        self.assertEqual((out['applied'], out['failed']), (4, []), out)
        self.assertTrue(PackagingMaterial.objects.filter(code='TB-1', box_type='tube').exists())
        big = PackagingMaterial.objects.get(pk=self.big.pk)
        self.assertEqual((big.max_weight_kg, big.stock_qty), (Decimal('12.5'), 48))

    def test_stale_update_and_bad_input(self):
        ctx = self.ctx()
        self.call('propose_packaging', ctx, action='update', box='BX-1', fields={'name': 'Bigger'}, evidence='x')
        PackagingMaterial.objects.filter(pk=self.big.pk).update(name='Changed by human')
        p = AgentProposal.objects.get(task=ctx.task)
        self.assertIn('змінили після пропозиції', decide.apply_one(p, self.user))
        self.assertIn('не змінюється', self.fails('propose_packaging', action='update', box='BX-1',
                                                   fields={'stock_qty': 5}, evidence='x'))
        self.assertIn('length_cm', self.fails('propose_packaging', action='create', fields={'name': 'x'},
                                              evidence='x'))
        self.assertIn('не ведеться облік', self.fails('propose_packaging', action='receive', box='BX-2', qty=3,
                                                       evidence='x'))
        self.assertIn('немає', self.fails('propose_packaging', action='update', box='NOPE',
                                          fields={'name': 'x'}, evidence='x'))

    def test_viewer_cannot_apply(self):
        ctx = self.ctx()
        self.call('propose_packaging_merge', ctx, target='BX-1', merge=['BX-2'], evidence='x')
        p = AgentProposal.objects.get(task=ctx.task)
        self.assertIn('немає права', decide.apply_one(p, person('ro', 'readonly')))
        self.assertTrue(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)
        self.assertFalse(ProductPackaging.objects.exists())
