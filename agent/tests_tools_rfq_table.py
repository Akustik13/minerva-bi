"""📊 Таблиця пропозицій запиту постачальникам у MCP і REST (agent/tools_rfq_table.py, api/suppliers.py): rfq_table,
➕ свої колонки (додати зі значеннями / перейменувати / прибрати), значення колонок у рядку (src agent — 🧠 AI не
перезаписує), 🏷 позначки й 📌 місце рядків → ✓ людини тими самими функціями, що сторінка; застаріла пропозиція
відмовляє. Ще — кругле число в своїй колонці (450, 1000) більше не стає «4.5E+2» / «1E+3».

python manage.py test agent.tests_tools_rfq_table --settings=tabele.settings_test
"""
from django.urls import reverse

from agent import tools_rfq, toolsets
from agent.models import AgentProposal
from agent.tests_suppliers_mcp import SupBase
from agent.tests_tools_rfq import RFQBase
from suppliers.models import SupplierRequestRecipient as R
from suppliers.services import rfq_columns, rfq_marks


class TableBase(RFQBase):
    def cols(self):
        self.req.refresh_from_db()
        return rfq_columns.columns(self.req)

    def extra(self, r):
        r.refresh_from_db()
        return r.extra


class RightsTests(TableBase):
    def test_tools_by_right(self):
        names = {t['name'] for t in toolsets.tools_for(self.user)}
        for n in ('rfq_table', 'propose_rfq_columns', 'propose_rfq_column_values', 'propose_rfq_marks'):
            self.assertIn(n, names)
        viewer = self.person('viewer', 'suppliers', 'inventory')
        names = {t['name'] for t in toolsets.tools_for(viewer)}
        self.assertIn('rfq_table', names)
        self.assertFalse(names & {'propose_rfq_columns', 'propose_rfq_column_values', 'propose_rfq_marks'})
        self.refused('propose_rfq_marks', self.ctx(owner=viewer), request=self.req.code, suppliers=[self.jlc.name],
                     color='red')                                      # інструмент перевіряє право ще раз
        self.assertNotIn('rfq_table', {t['name'] for t in toolsets.tools_for(self.person('nobody', 'sales'))})
        self.assertIs(toolsets.module_for_kind(AgentProposal.RFQ), tools_rfq)


class ColumnsTests(TableBase):
    def test_add_with_values_edit_delete(self):
        ctx = self.ctx()
        out = self.call('propose_rfq_columns', ctx, request=self.req.code, add=[
            {'name': 'Сума з ENIG', 'kind': 'price', 'hint': 'ENIG 1u',
             'values': {self.supplier.name: '100: 450; 500: 1.291,69', 'mia@jlcpcb.com': {'100': 1000}}},
            {'name': 'Покриття', 'kind': 'text', 'values': {f'#{self.jlc.pk}': 'ENIG 2u'}}])
        self.assertIn('людина підтвердить', out)
        self.assertEqual(self.cols(), [])                              # без ✓ — нічого
        p, res = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, res)
        self.assertEqual([(c['key'], c['name'], c['kind']) for c in self.cols()],
                         [('c1', 'Сума з ENIG', 'price'), ('c2', 'Покриття', 'text')])
        x1, x2 = self.extra(self.r1), self.extra(self.r2)
        self.assertEqual(x1['c1']['v'], {'100': '450', '500': '1291.69'})  # не «4.5E+2»
        self.assertEqual(x1['c1']['src'], 'agent')
        self.assertEqual((x2['c1']['v'], x2['c2']['v']), ({'100': '1000'}, 'ENIG 2u'))
        # 🧠 AI Minerva вписане агентом не перезаписує
        rfq_columns.apply_ai(self.r1, {'c1': {'100': '999'}})
        self.assertEqual(self.extra(self.r1)['c1']['v']['100'], '450')
        # перейменувати й прибрати
        ctx = self.ctx()
        self.call('propose_rfq_columns', ctx, request=self.req.code, edit=[{'column': 'c1', 'name': 'ENIG total'}],
                  delete=['Покриття'])
        p, res = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, res)
        self.assertEqual([c['name'] for c in self.cols()], ['ENIG total'])
        self.assertNotIn('c2', self.extra(self.r2))

    def test_refusals(self):
        rfq_columns.add(self.req, 'ENIG', 'price')
        self.refused('propose_rfq_columns', request=self.req.code, add=[{'name': 'enig'}])          # така вже є
        self.refused('propose_rfq_columns', request=self.req.code, edit=[{'column': 'c9', 'name': 'X'}])
        self.refused('propose_rfq_columns', request=self.req.code,
                     add=[{'name': f'K{i}'} for i in range(rfq_columns.MAX_COLUMNS)])            # понад 8
        self.refused('propose_rfq_columns', request=self.req.code,
                     add=[{'name': 'X', 'values': {self.supplier.name: '777: 5'}}])              # такої кількості немає
        self.refused('propose_rfq_columns', request=self.req.code, add=[{'name': 'X', 'kind': 'number'}])
        self.assertIn('нічого', self.call('propose_rfq_columns', request=self.req.code,
                                          edit=[{'column': 'ENIG', 'name': 'ENIG'}]))

    def test_stale_column_refused(self):
        rfq_columns.add(self.req, 'ENIG', 'price')
        ctx = self.ctx()
        self.call('propose_rfq_columns', ctx, request=self.req.code, edit=[{'column': 'ENIG', 'name': 'ENIG 1u'}])
        rfq_columns.edit(self.req, 'c1', name='Hard gold')             # людина змінила тим часом
        p, res = self.apply_last(ctx)
        self.assertNotEqual(p.status, AgentProposal.APPLIED)
        self.assertEqual(self.cols()[0]['name'], 'Hard gold')


class ValuesTests(TableBase):
    def setUp(self):
        super().setUp()
        rfq_columns.add(self.req, 'Сума з ENIG', 'price')
        rfq_columns.add(self.req, 'Покриття', 'text')

    def test_values_rows_and_apply(self):
        ctx = self.ctx()
        out = self.call('propose_rfq_column_values', ctx, request=self.req.code, rows=[
            {'supplier': self.supplier.name, 'values': {'c1': '100 шт: 12,5; 500: 10', 'Покриття': 'ENIG'}},
            {'supplier': self.jlc.name, 'values': {'Сума з ENIG': {'500': 9.8}}}], evidence='Total ENIG 12.5')
        self.assertIn('людина підтвердить', out)
        self.assertEqual(ctx.task.proposals.count(), 2)                # рядок — окрема пропозиція
        from agent import decide
        decide.apply_selected(ctx.task, list(ctx.task.proposals.values_list('pk', flat=True)), self.user)
        x1, x2 = self.extra(self.r1), self.extra(self.r2)
        self.assertEqual((x1['c1']['v'], x1['c2']['v'], x1['c1']['src']), ({'100': '12.5', '500': '10'}, 'ENIG', 'agent'))
        self.assertEqual(x2['c1']['v'], {'500': '9.8'})
        # кількість, якої не назвали, лишається; "" — прибрати одну кількість
        ctx = self.ctx()
        self.call('propose_rfq_column_values', ctx, request=self.req.code, supplier=self.supplier.name,
                  values={'c1': {'100': ''}})
        self.apply_last(ctx)
        self.assertEqual(self.extra(self.r1)['c1']['v'], {'500': '10'})
        self.assertIn('уже так', self.call('propose_rfq_column_values', request=self.req.code,
                                           supplier=self.supplier.name, values={'c1': '500: 10,00'}))

    def test_refusals_and_stale(self):
        self.refused('propose_rfq_column_values', request=self.req.code, supplier=self.supplier.name,
                     values={'c1': 450})                               # дві кількості — без «кількість: число»
        self.refused('propose_rfq_column_values', request=self.req.code, supplier=self.supplier.name,
                     values={'c1': '100: abc'})
        self.refused('propose_rfq_column_values', request=self.req.code, supplier=self.supplier.name,
                     values={'Колір маски': 'green'})                  # такої колонки немає
        self.refused('propose_rfq_column_values', request=self.req.code)
        ctx = self.ctx()
        self.call('propose_rfq_column_values', ctx, request=self.req.code, supplier=self.supplier.name,
                  values={'c1': '100: 12'})
        rfq_columns.set_value(self.r1, 'c1', 100, '13', self.user)     # людина вписала своє
        p, res = self.apply_last(ctx)
        self.assertNotEqual(p.status, AgentProposal.APPLIED)
        self.assertEqual(self.extra(self.r1)['c1']['v'], {'100': '13'})

    def test_single_quantity_plain_number(self):
        self.req.quantities = '2500'
        self.req.save(update_fields=['quantities'])
        ctx = self.ctx()
        self.call('propose_rfq_column_values', ctx, request=self.req.code, supplier=self.supplier.name,
                  values={'c1': 1000})
        self.apply_last(ctx)
        self.assertEqual(self.extra(self.r1)['c1']['v'], {'2500': '1000'})


class MarksTests(TableBase):
    def test_color_stickers_text_and_rank(self):
        ctx = self.ctx()
        self.call('propose_rfq_marks', ctx, request=self.req.code, suppliers=[self.supplier.name, self.jlc.name],
                  color='green', stickers_on=['star', 'ask'], text='зразки до 05.12')
        p, res = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, res)
        for r in (self.r1, self.r2):
            v = rfq_marks.view(R.objects.get(pk=r.pk).label)
            self.assertEqual((v['color'], [s['key'] for s in v['stickers']], v['text']),
                             ('green', ['star', 'ask'], 'зразки до 05.12'))
        ctx = self.ctx()
        self.call('propose_rfq_marks', ctx, request=self.req.code, suppliers=[self.jlc.name], rank=1,
                  stickers_off=['ask'])
        self.apply_last(ctx)
        v = rfq_marks.view(R.objects.get(pk=self.r2.pk).label)
        self.assertEqual((v['rank'], [s['key'] for s in v['stickers']]), (1, ['star']))
        out = self.call('rfq_table', request=self.req.code)
        self.assertLess(out.index(self.jlc.name), out.index(self.supplier.name))   # 📌 закріплений — першим
        ctx = self.ctx()
        self.call('propose_rfq_marks', ctx, request=self.req.code, suppliers=[self.jlc.name], clear=True)
        self.apply_last(ctx)
        v = rfq_marks.view(R.objects.get(pk=self.r2.pk).label)
        self.assertEqual((v['color'], v['stickers'], v['rank']), ('', [], 1))   # 🧹 місце лишається

    def test_refusals(self):
        self.refused('propose_rfq_marks', request=self.req.code, suppliers=[self.jlc.name], color='pink')
        self.refused('propose_rfq_marks', request=self.req.code, suppliers=[self.jlc.name], stickers_on=['wow'])
        self.refused('propose_rfq_marks', request=self.req.code, suppliers=[self.jlc.name, self.supplier.name],
                     rank=2)                                           # 📌 — одному рядку
        self.refused('propose_rfq_marks', request=self.req.code, suppliers=[self.jlc.name])  # що змінити?
        self.refused('propose_rfq_marks', request=self.req.code, suppliers=[self.jlc.name], text='x' * 61)


class TableTests(TableBase):
    def test_table_and_request_text(self):
        rfq_columns.add(self.req, 'Сума з ENIG', 'price', 'ENIG 1u')
        rfq_columns.add(self.req, 'Покриття', 'text')
        rfq_columns.set_value(self.r1, 'c1', 100, '150', self.user, src='agent')
        rfq_columns.apply_ai(self.r1, {'c2': 'ENIG'})
        rfq_marks.apply(self.req, [self.r1.pk], self.user, {'color': 'red', 'sticker': 'star', 'on': True})
        out = self.call('rfq_table', request=self.req.code)
        self.assertIn('c1 «Сума з ENIG» — число на кожну кількість', out)
        self.assertIn('підказка: ENIG 1u', out)
        self.assertIn('100 шт: 1,2', out)
        self.assertIn('c1 «Сума з ENIG»: 100 шт 150,00 USD', out)
        self.assertIn('(🤖 агент)', out)
        self.assertIn('c2 «Покриття»: «ENIG» (🧠 AI)', out)
        self.assertIn('🏷 колір red', out)
        self.assertIn('star ⭐', out)                                  # довідник для propose_rfq_marks
        one = self.call('rfq_table', request=self.req.code, qty=500, supplier=self.supplier.name)
        self.assertNotIn(self.jlc.name, one.split('Рядки')[1])
        self.assertNotIn('100 шт:', one)
        self.refused('rfq_table', request=self.req.code, qty=7)
        text = self.call('get_supplier_request', request=self.req.code)
        self.assertIn('Свої колонки таблиці', text)
        self.assertIn('свої колонки: Сума з ENIG: 100 шт 150,00 USD', text)
        self.assertIn('🏷 позначка: колір red', text)

    def test_work_page_shows_agent_value(self):
        rfq_columns.add(self.req, 'Сума з ENIG', 'price')
        rfq_columns.set_value(self.r1, 'c1', 100, '450', self.user, src='agent')
        page = self.client.get(reverse('admin:suppliers_supplierrequest_work', args=[self.req.pk])).content.decode()
        self.assertIn('Вписав 🤖 агент (MCP)', page)
        self.assertIn('data-raw="450"', page)

    def test_round_numbers_and_old_exponent(self):
        rfq_columns.add(self.req, 'ENIG', 'price')
        rfq_columns.set_value(self.r1, 'c1', 100, '1000', self.user)
        self.assertEqual(self.extra(self.r1)['c1']['v'], {'100': '1000'})
        R.objects.filter(pk=self.r1.pk).update(extra={'c1': {'v': {'500': '4.5E+2'}, 'src': 'user'}})  # як до 11.2026
        from suppliers.services import rfq_compare
        cells = rfq_columns.cells(rfq_compare.table(self.req), self.req)
        q500 = next(q for q in cells[self.r1.pk][0]['qs'] if q['qty'] == 500)
        self.assertEqual((q500['raw'], q500['text']), ('450', '450,00 USD'))


class RestTests(SupBase):
    def test_table_and_detail_json(self):
        rfq_columns.add(self.req, 'Сума з ENIG', 'price')
        rfq_columns.set_value(self.r1, 'c1', 100, '150', self.user, src='agent')
        rfq_marks.set_rank(self.req, self.r2.pk, 1, self.user)
        j = self.api(f'/api/v1/supplier-requests/{self.req.code}/table/').json()
        self.assertEqual(j['qtys'], [100, 500])
        self.assertEqual(j['columns']['own'][0]['key'], 'c1')
        self.assertEqual(j['rows'][0]['supplier']['id'], self.jlc.pk)          # 📌 першим
        row = next(x for x in j['rows'] if x['supplier']['id'] == self.supplier.pk)
        self.assertEqual(row['cells'][0]['unit'], 1.2)
        self.assertEqual(row['columns']['c1']['value'], {'100': 150.0})
        self.assertEqual(row['columns']['c1']['source'], 'agent')
        self.assertIn('star', [s['key'] for s in j['marks']['stickers']])
        d = self.api(f'/api/v1/supplier-requests/{self.req.code}/').json()
        self.assertEqual(d['columns'][0]['name'], 'Сума з ENIG')
        rec = next(x for x in d['recipients_list'] if x['supplier']['id'] == self.jlc.pk)
        self.assertEqual(rec['label']['rank'], 1)
        self.assertEqual(self.api('/api/v1/supplier-requests/RFQ-1999-999/table/').status_code, 404)

    def test_propose_over_rest_bridge(self):
        r = self.api('/api/v1/agent/tools/propose_rfq_columns/', method='post',
                     data={'request': self.req.code, 'add': [{'name': 'Зразки до', 'kind': 'text'}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(AgentProposal.objects.filter(kind=AgentProposal.RFQ, data__action='columns').count(), 1)
        self.assertEqual(self.api('/api/v1/agent/tools/propose_rfq_marks/', key=self.reader, method='post',
                                  data={'request': self.req.code, 'suppliers': [self.jlc.name],
                                        'color': 'red'}).status_code, 403)       # agent:read — лише читання
