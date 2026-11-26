"""Інструменти агента для проєктів, постачальників і робочого часу (agent/tools_projects.py, tools_suppliers.py,
tools_worktime.py): читання з правами людини, приватність пошти й робочого часу, пропозиції → ✓ людини
(📌 момент проєкту — `add_highlight`, ⏱ час в askDANTE — `book_project`; askDANTE імітується).

python manage.py test agent.tests_tools_projects --settings=tabele.settings_test
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from agent import decide, tools, tools_projects, tools_suppliers, tools_worktime, toolsets
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base
from crm.models import Customer
from email_assistant.models import EmailAccount, EmailSettings
from email_assistant.sync import save_message
from inventory.models import PurchaseOrder, Supplier
from projects import services as project_services
from projects.models import Project, ProjectHighlight, ProjectRisk
from suppliers.models import SupplierAnalysis, SupplierCategory, SupplierContact, SupplierEvent
from worktime import services as wt_services
from worktime.client import DanteError
from worktime.models import Booking, DanteActivity, DanteProject, DanteSettings, DanteUser, TimeRecord, WorkDay
from worktime.tests import FakeClient


class ToolsBase(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self._uid = 0

    def ctx(self, owner=None):
        task = AgentTask.objects.create(owner=owner or self.user, title='t', instruction='t')
        return tools.Context(task)

    def call(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertFalse(err, out)
        return out

    def refused(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertTrue(err, out)
        return out

    def person(self, name, *modules, ops=None):
        """Співробітник з роллю «лише перегляд» і ручним списком розділів (без них — жодних прав)."""
        u = User.objects.create_user(name, f'{name}@x.y', 'p', is_staff=True)
        u.profile.role, u.profile.allowed_modules, u.profile.module_operations = 'readonly', list(modules), ops
        u.profile.save()
        return User.objects.get(pk=u.pk)            # свіжий об'єкт — без кешу прав

    def mailbox(self, user, address):
        es = EmailSettings.get_for_user(user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        return EmailAccount.objects.create(user=user, email_address=address, imap_username=address, imap_password='x',
                                           smtp_username=address, smtp_password='x', imap_folder_inbox='INBOX',
                                           imap_folder_sent='Sent')

    def mail(self, account, subject, from_email, days_ago=5):
        self._uid += 1
        d = self.today - timedelta(days=days_ago)
        data = {'uid': self._uid, 'message_id': f'<t{self._uid}@x.example>', 'in_reply_to': '', 'references': [],
                'subject': subject, 'from_email': from_email, 'from_name': '', 'to_emails': [account.email_address],
                'cc_emails': [], 'body_text': 'Hallo', 'body_html': '', 'attachments': [], 'is_read': True,
                'sent_at': timezone.make_aware(datetime(d.year, d.month, d.day, 9, 0))}
        return save_message(account, data, 'INBOX')


# ── 📁 Проєкти ───────────────────────────────────────────────────────────────

class ProjectToolTests(ToolsBase):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(name='Max Muster', company='ACME GmbH', email='buyer@acme.example')
        self.project = Project.objects.create(name='5G Antenne', customer=self.customer, owner=self.user,
                                              due_date=self.today + timedelta(days=40), next_action='Muster schicken')
        self.closed = Project.objects.create(name='Filter alt', status=Project.DONE)
        ProjectHighlight.objects.create(project=self.project, date=self.today - timedelta(days=2),
                                        kind=ProjectHighlight.DECISION, title='Kunde wählt Variante B',
                                        quote='Wir nehmen B')
        ProjectRisk.objects.create(project=self.project, title='Lieferverzug Stecker', probability=3, impact=3,
                                   owner=self.user)

    def test_kinds_match_model(self):
        self.assertEqual(set(tools_projects.KIND_VALUES), {k for k, _ in ProjectHighlight.KIND_CHOICES})

    def test_find_projects(self):
        out = self.call('find_projects', query='acme')
        self.assertIn(self.project.code, out)
        self.assertIn('ACME GmbH', out)
        self.assertIn('Muster schicken', out)
        self.assertTrue(out.startswith('<<<ПРОЄКТИ'))
        self.assertNotIn(self.closed.code, self.call('find_projects'))        # за замовчуванням — відкриті
        self.assertIn(self.closed.code, self.call('find_projects', status='all'))
        bob = User.objects.create_superuser('bob', 'b@x.y', 'p')
        theirs = Project.objects.create(name='Bob Projekt', owner=bob)
        mine = self.call('find_projects', mine=True)
        self.assertIn(self.project.code, mine)
        self.assertNotIn(theirs.code, mine)

    def test_find_only_attention(self):
        late = Project.objects.create(name='Verspätet', due_date=self.today - timedelta(days=3),
                                      next_action='Anrufen')
        calm = Project.objects.create(name='Ruhig', next_action='Warten')
        out = self.call('find_projects', attention=True)
        self.assertIn(late.code, out)
        self.assertIn('🔴', out)
        self.assertNotIn(calm.code, out)

    def test_get_project_hides_other_mailbox(self):
        bob = User.objects.create_superuser('bob', 'b@x.y', 'p')
        secret = self.mail(self.mailbox(bob, 'bob@sevskiy.de'), 'Geheim: Preisverhandlung', 'buyer@acme.example')
        project_services.link_thread(self.project, secret.thread, user=bob)
        out = self.call('get_project', project=self.project.code)
        for text in ('ACME GmbH', 'Kunde wählt Variante B', 'Wir nehmen B', 'Lieferverzug Stecker', 'Muster schicken',
                     'лист з іншої скриньки'):
            self.assertIn(text, out)
        self.assertNotIn('Geheim', out)
        self.assertTrue(out.startswith('<<<ПРОЄКТ'))
        theirs = self.call('get_project', self.ctx(bob), project=f'#{self.project.pk}')
        self.assertIn('Geheim: Preisverhandlung', theirs)
        self.assertIn('немає', self.refused('get_project', project='P-1999-999'))

    def test_get_project_shows_plan(self):
        from projects import plan
        plan.apply_template(self.project, plan.templates()[0], start=self.today - timedelta(days=10), user=self.user)
        out = self.call('get_project', project=self.project.code)
        for text in ('Фази плану:', '← поточна', 'Відкриті кроки'):
            self.assertIn(text, out)
        self.assertNotIn('прогноз за планом: —', out)

    def test_wired_into_toolsets(self):
        names = {t['name'] for t in toolsets.tools_for(self.user)}
        self.assertTrue({'find_projects', 'get_project', 'propose_project_moment', 'find_suppliers', 'get_supplier'}
                        <= names)
        self.assertIs(toolsets.handler('get_project'), tools_projects.get_project)
        self.assertIs(toolsets.module_for_kind(AgentProposal.MOMENT), tools_projects)
        self.assertIs(toolsets.module_for_kind(AgentProposal.WORKTIME), tools_worktime)

    def test_propose_moment_and_apply(self):
        ctx = self.ctx()
        day = self.today - timedelta(days=3)
        args = dict(project=self.project.code, title='Muster freigegeben', date=day.isoformat(), kind='approval',
                    quote='Muster OK', evidence='Muster OK, bitte liefern', source='Mail vom Kunden')
        self.call('propose_project_moment', ctx, **args)
        p = ctx.task.proposals.get()
        self.assertEqual((p.kind, p.field, p.new_value),
                         (AgentProposal.MOMENT, self.project.code, 'Muster freigegeben'))
        self.assertEqual(p.data['project_id'], self.project.pk)
        what, old, new = decide.describe(p)
        self.assertEqual((what, old), ('📌 Момент проєкту', self.project.code))
        self.assertIn('Muster freigegeben', new)
        self.assertIn('в цьому завданні', self.refused('propose_project_moment', ctx, **args))
        res = decide.apply_selected(ctx.task, [p.pk], self.user)
        self.assertEqual(res['applied'], 1, res)
        h = ProjectHighlight.objects.get(title='Muster freigegeben')
        self.assertEqual((h.project, h.date, h.kind, h.source, h.created_by, h.quote),
                         (self.project, day, 'approval', ProjectHighlight.AI, self.user, 'Muster OK'))
        p.refresh_from_db()
        self.assertEqual(p.status, AgentProposal.APPLIED)
        self.assertEqual(p.result_url, f'/admin/projects/project/{self.project.pk}/change/')
        self.assertIn('уже є', self.refused('propose_project_moment', **args))

    def test_propose_validation(self):
        self.assertIn('kind', self.refused('propose_project_moment', project=self.project.code, title='X',
                                           kind='party', evidence='e'))
        self.assertIn('YYYY-MM-DD', self.refused('propose_project_moment', project=self.project.code, title='X',
                                                 date='gestern', evidence='e'))
        self.assertIn('evidence', self.refused('propose_project_moment', project=self.project.code, title='X'))

    def test_rights(self):
        nobody = self.person('nobody')
        self.assertEqual(tools_projects.tools_for(nobody), [])
        self.assertIn('немає права', self.refused('get_project', self.ctx(nobody), project=self.project.code))
        viewer = self.person('viewer', 'projects')
        self.assertEqual([t['name'] for t in tools_projects.tools_for(viewer)], ['find_projects', 'get_project'])
        self.assertIn(self.project.code, self.call('find_projects', self.ctx(viewer)))
        self.assertIn('немає права', self.refused('propose_project_moment', self.ctx(viewer),
                                                  project=self.project.code, title='X', evidence='e'))
        ctx = self.ctx()
        self.call('propose_project_moment', ctx, project=self.project.code, title='Zahlung erhalten', evidence='e')
        res = decide.apply_selected(ctx.task, list(ctx.task.proposals.values_list('pk', flat=True)), viewer)
        self.assertIn('немає права змінювати проєкти', res['failed'][0])
        self.assertFalse(ProjectHighlight.objects.filter(title='Zahlung erhalten').exists())


# ── 🏭 Постачальники ─────────────────────────────────────────────────────────

class SupplierToolTests(ToolsBase):
    def setUp(self):
        super().setUp()
        self.cat = SupplierCategory.objects.create(name='Leiterplatten')
        self.s = Supplier.objects.create(name='PCB Way Ltd', email='sales@pcbway.example',
                                         website='https://pcbway.example', addr_country='CN')
        self.s.categories.add(self.cat)
        SupplierContact.objects.create(supplier=self.s, name='Lily Chen', position='Sales',
                                       email='lily@pcbway.example', phone='+86 755 1', is_primary=True)
        self.open_po = PurchaseOrder.objects.create(supplier=self.s, status='ordered', total_price=Decimal('120.50'),
                                                    currency='USD', order_date=self.today - timedelta(days=5))
        PurchaseOrder.objects.create(supplier=self.s, status='received', total_price=Decimal('80'), currency='USD',
                                     order_date=self.today - timedelta(days=60),
                                     received_date=self.today - timedelta(days=50))
        self.other = Supplier.objects.create(name='Kabel AG', email='info@kabel.example')

    def test_find_suppliers(self):
        out = self.call('find_suppliers', query='pcbway')
        for text in ('PCB Way Ltd', 'Leiterplatten', 'CN', 'відкритих закупівель 1', f'#{self.s.pk}'):
            self.assertIn(text, out)
        self.assertNotIn('Kabel AG', out)
        by_cat = self.call('find_suppliers', category='Leiter')
        self.assertIn('PCB Way Ltd', by_cat)
        self.assertNotIn('Kabel AG', by_cat)
        self.assertIn('PCB Way Ltd', self.call('find_suppliers', query='Lily'))      # контактна особа
        self.assertIn('Kabel AG', self.call('find_suppliers'))

    def test_get_supplier_hides_other_mailbox(self):
        bob = User.objects.create_superuser('bob', 'b@x.y', 'p')
        msg = self.mail(self.mailbox(bob, 'bob@sevskiy.de'), 'Preisliste geheim', 'lily@pcbway.example')
        SupplierEvent.objects.create(supplier=self.s, kind=SupplierEvent.MAIL, message=msg, user=bob,
                                     text='Лист «Preisliste geheim» від lily@pcbway.example')
        SupplierEvent.objects.create(supplier=self.s, kind=SupplierEvent.NOTE, user=self.user, text='Muster bestellt')
        SupplierAnalysis.objects.create(supplier=self.s, kind=SupplierAnalysis.WEB, owner=bob,
                                        result={'summary': 'Chinese PCB maker'})
        SupplierAnalysis.objects.create(supplier=self.s, kind=SupplierAnalysis.AI, owner=bob,
                                        result={'summary': 'Bob private eval'})
        SupplierAnalysis.objects.create(supplier=self.s, kind=SupplierAnalysis.AI, owner=self.user,
                                        result={'summary': 'My eval ok', 'mails': 4})
        out = self.call('get_supplier', supplier='lily@pcbway.example')
        for text in ('Lily Chen', 'Sales', '+86 755 1', 'Muster bestellt', 'Chinese PCB maker', 'My eval ok',
                     self.open_po.code, '120.50 USD', 'лист з іншої скриньки', 'Leiterplatten'):
            self.assertIn(text, out)
        self.assertNotIn('Preisliste geheim', out)
        self.assertNotIn('Bob private eval', out)
        theirs = self.call('get_supplier', self.ctx(bob), supplier=f'#{self.s.pk}')
        self.assertIn('Preisliste geheim', theirs)
        self.assertIn('Bob private eval', theirs)
        self.assertNotIn('My eval ok', theirs)

    def test_lookup_and_rights(self):
        self.assertIn('PCB Way Ltd', self.call('get_supplier', supplier='pcb way ltd'))
        Supplier.objects.create(name='PCB Way Europe')
        self.assertIn('кілька', self.refused('get_supplier', supplier='PCB Way'))
        nobody = self.person('nobody')
        self.assertEqual(tools_suppliers.tools_for(nobody), [])
        self.assertIn('немає права', self.refused('find_suppliers', self.ctx(nobody)))
        viewer = self.person('viewer', 'inventory')
        self.assertEqual([t['name'] for t in tools_suppliers.tools_for(viewer)],     # 🧵 історія — право бачити
                         ['find_suppliers', 'supplier_filters', 'get_supplier', 'propose_supplier_check'])
        self.assertIn('PCB Way Ltd', self.call('get_supplier', self.ctx(viewer), supplier=f'#{self.s.pk}'))


# ── ⏱ Робочий час askDANTE ───────────────────────────────────────────────────

class WorktimeToolTests(ToolsBase):
    def setUp(self):
        super().setUp()
        st = DanteSettings.get()
        st.enabled, st.api_key = True, 'secret-token-123456'
        st.save()
        self.fake = FakeClient()
        patcher = mock.patch('worktime.services.client', return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.du = DanteUser.objects.create(dante_id='1108', first_name='Slava', email='s@x.y', user=self.user)
        self.dp = DanteProject.objects.create(dante_id='1', name='Support')
        self.sub = DanteProject.objects.create(dante_id='2', name='Sub', parent=self.dp)
        self.old = DanteProject.objects.create(dante_id='3', name='Altprojekt', archived=True)
        self.act = DanteActivity.objects.create(dante_id='190', name='Entwicklung')
        self.day = self.today - timedelta(days=1)
        WorkDay.objects.create(dante_user=self.du, date=self.day, planned_state=WorkDay.BUSINESS, minutes=510,
                               target_minutes=480, fetched_at=timezone.now(),
                               times=[{'coming': '08:00', 'leaving': '16:30', 'source': 'MANUAL', 'minutes': 510}])
        TimeRecord.objects.create(dante_id='r1', dante_user=self.du, day=self.day, duration=90, start='09:00',
                                  stop='10:30', dante_project=self.dp, project_name='Support',
                                  description='Kunde ACME Support')
        self.anna = User.objects.create_superuser('anna', 'a@x.y', 'p')
        self.du_anna = DanteUser.objects.create(dante_id='2000', first_name='Anna', email='a@x.y', user=self.anna)
        WorkDay.objects.create(dante_user=self.du_anna, date=self.day, minutes=480,
                               times=[{'coming': '07:05', 'leaving': '15:05', 'source': 'MANUAL', 'minutes': 480}])
        TimeRecord.objects.create(dante_id='r2', dante_user=self.du_anna, day=self.day, duration=60,
                                  dante_project=self.dp, project_name='Support', description='Anna private note')

    def test_my_worktime_only_own(self):
        d = self.day.isoformat()
        out = self.call('my_worktime', date_from=d, date_to=d)
        for text in ('08:00–16:30', 'норма 8:00', 'на проєкти 1:30', 'Kunde ACME Support', '<<<РОБОЧИЙ ЧАС'):
            self.assertIn(text, out)
        self.assertNotIn('Anna private note', out)
        self.assertNotIn('07:05', out)
        theirs = self.call('my_worktime', self.ctx(self.anna), date_from=d, date_to=d)
        self.assertIn('Anna private note', theirs)
        self.assertNotIn('Kunde ACME Support', theirs)
        self.assertIn('31', self.refused('my_worktime', date_from=(self.today - timedelta(days=40)).isoformat(),
                                         date_to=d))
        week = self.call('my_worktime')                               # без дат — поточний тиждень
        monday = self.today - timedelta(days=self.today.weekday())
        self.assertIn(f'{monday:%d.%m.%Y} – {monday + timedelta(days=6):%d.%m.%Y}', week)

    def test_refresh_reads_askdante(self):
        self.fake.times[self.day] = [{'coming': '07:30', 'leaving': '15:00', 'source': 'MANUAL', 'minutes': 450}]
        self.fake.records = [{'id': 'r9', 'person_id': '1108', 'day': self.day, 'duration': 30, 'start': '', 'stop': '',
                              'item_id': '', 'project_id': '1', 'item_name': '', 'project_name': 'Support',
                              'activity_name': '', 'description': 'Frisch aus askDANTE', 'hourly_rate': None}]
        d = self.day.isoformat()
        out = self.call('my_worktime', date_from=d, date_to=d, refresh=True)
        self.assertIn('07:30–15:00', out)
        self.assertIn('Frisch aus askDANTE', out)
        self.assertNotIn('Kunde ACME Support', out)          # askDANTE його більше не повертає

    def test_no_dante_user_no_tools(self):
        bob = User.objects.create_superuser('bob', 'b@x.y', 'p')
        self.assertEqual(tools_worktime.tools_for(bob), [])
        self.assertIn('не прив', self.refused('my_worktime', self.ctx(bob)))
        nobody = self.person('nobody')
        DanteUser.objects.create(dante_id='3000', user=nobody)
        self.assertEqual(tools_worktime.tools_for(nobody), [])
        self.assertIn('немає права', self.refused('my_worktime', self.ctx(nobody)))
        self.assertEqual([t['name'] for t in tools_worktime.tools_for(self.user)],
                         ['my_worktime', 'worktime_projects', 'propose_worktime'])

    def test_worktime_projects(self):
        out = self.call('worktime_projects')
        self.assertIn(f'#{self.dp.pk} Support', out)
        self.assertIn('Support → Sub', out)
        self.assertIn('Entwicklung', out)
        self.assertNotIn('Altprojekt', out)
        self.assertIn('Altprojekt', self.call('worktime_projects', include_archived=True))
        self.dp.pinned_by.add(self.user)
        mine = self.call('worktime_projects', mine=True)
        self.assertIn('☆ ваш', mine)
        self.assertNotIn('Sub', mine)

    def propose(self, ctx, **args):
        base = dict(date=self.day.isoformat(), project=f'#{self.dp.pk}', comment='Angebot A-2026-001 vorbereitet',
                    evidence='Завдання: запиши 45 хв на Support')
        base.update(args)
        return self.call('propose_worktime', ctx, **base)

    def test_propose_and_apply_books_like_calendar(self):
        ctx = self.ctx()
        out = self.propose(ctx, minutes=45, activity='Entwicklung')
        self.assertIn('уже є час на цей проєкт', out)          # копія askDANTE: 1:30 на Support того дня
        p = ctx.task.proposals.get()
        self.assertEqual((p.kind, p.data['minutes'], p.data['activity']), (AgentProposal.WORKTIME, 45, self.act.pk))
        what, old, new = decide.describe(p)
        self.assertEqual(what, '⏱ Час в askDANTE')
        self.assertIn('0:45', new)
        with mock.patch('worktime.services.book_project', wraps=wt_services.book_project) as book:
            res = decide.apply_selected(ctx.task, [p.pk], self.user)
        self.assertEqual(res['applied'], 1, res)
        args, kw = book.call_args
        self.assertEqual(args[:6], (self.du, self.day, self.dp, 45, self.act, 'Angebot A-2026-001 vorbereitet'))
        self.assertEqual((kw['user'], kw['source'], kw['start'], kw['stop']), (self.user, Booking.MANUAL, None, None))
        self.assertEqual(self.fake.calls[-1], {'date': self.day.isoformat(), 'duration': 45, 'projectData': {
            'projectId': '1', 'activityId': '190', 'comment': 'Angebot A-2026-001 vorbereitet'}})
        self.assertTrue(Booking.objects.filter(dante_user=self.du, kind=Booking.PROJECT, status=Booking.SENT).exists())
        p.refresh_from_db()
        self.assertEqual(p.status, AgentProposal.APPLIED)
        self.assertEqual(p.result_url, f'/admin/worktime/workday/day/?u={self.du.pk}&d={self.day:%Y-%m-%d}')

    def test_start_stop_on_subproject(self):
        ctx = self.ctx()
        self.propose(ctx, project='Sub', start='11:00', stop='12:15')
        p = ctx.task.proposals.get()
        self.assertEqual(p.data['minutes'], 75)
        res = decide.apply_selected(ctx.task, [p.pk], self.user)
        self.assertEqual(res['applied'], 1, res)
        self.assertEqual(self.fake.calls[-1], {'date': self.day.isoformat(), 'start': '11:00', 'stop': '12:15',
                                               'projectData': {'projectItemId': '2',
                                                               'comment': 'Angebot A-2026-001 vorbereitet'}})

    def test_only_owner_applies_and_refusals_are_reported(self):
        ctx = self.ctx()
        self.propose(ctx, minutes=30)
        p = ctx.task.proposals.get()
        res = decide.apply_selected(ctx.task, [p.pk], self.anna)            # інша людина — навіть суперкористувач
        self.assertIn('лише сам працівник', res['failed'][0])
        self.assertEqual(self.fake.calls, [])
        ctx = self.ctx()
        self.propose(ctx, minutes=30, comment='Zweiter Versuch')
        p = ctx.task.proposals.get()
        self.fake.fail[self.day] = DanteError('bad_request', 'Zeitraum ist gesperrt', 400)
        res = decide.apply_selected(ctx.task, [p.pk], self.user)
        self.assertIn('Zeitraum ist gesperrt', res['failed'][0])
        self.assertTrue(Booking.objects.filter(status=Booking.FAILED).exists())

    def test_propose_validation(self):
        self.assertIn('майбутній', self.refused('propose_worktime', date=(self.today + timedelta(days=1)).isoformat(),
                                                project=f'#{self.dp.pk}', minutes=30, comment='c', evidence='e'))
        self.assertIn('архів', self.refused('propose_worktime', date=self.day.isoformat(), project=f'#{self.old.pk}',
                                            minutes=30, comment='c', evidence='e'))
        self.assertIn('від 5 хв', self.refused('propose_worktime', date=self.day.isoformat(),
                                               project=f'#{self.dp.pk}', minutes=2, comment='c', evidence='e'))
        self.assertIn('stop', self.refused('propose_worktime', date=self.day.isoformat(), project=f'#{self.dp.pk}',
                                           start='10:00', comment='c', evidence='e'))
        self.assertIn('пізніше', self.refused('propose_worktime', date=self.day.isoformat(), project=f'#{self.dp.pk}',
                                              start='10:00', stop='09:00', comment='c', evidence='e'))
        self.assertIn('Entwicklung', self.refused('propose_worktime', date=self.day.isoformat(),
                                                  project=f'#{self.dp.pk}', minutes=30, activity='Tanzen',
                                                  comment='c', evidence='e'))
        self.assertIn('немає', self.refused('propose_worktime', date=self.day.isoformat(), project='Nirgendwo',
                                            minutes=30, comment='c', evidence='e'))
