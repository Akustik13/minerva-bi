"""
email_assistant/app_api.py — JSON API для 📱 додатка «Minerva Пошта» (/email/phone/api/…, mobile/mail_app) і сторінка
підключення телефону «📱 Пошта на телефоні» (/email/phone/, page_view).

Вхід — ключ телефону «Authorization: Bearer mvm_…» (email_assistant/mail_app.py), не сесія й не CSRF. Телефон
працює від імені людини, під якою його підключено: скринька — та сама, що у веб-пошті (основна активна, лише своя),
права — ролі Minerva людини. Більшість адрес викликає ту саму функцію веб-пошти (views.py, agent/views.py,
offers/views.py) — логіка й перевірки однакові, тут лише вхід ключем телефону.

  POST pair/                         {code, device, version} → {token, user, account} — QR зі сторінки /email/phone/;
  GET  me/                           — хто, скринька, підпис, права, папки з непрочитаними, версія додатка;
  POST unpair/                       — відключити цей телефон (ключ перестає діяти);
  GET  nav/                          — свої категорії, типи, клієнти, проєкти, запити, «⏰ Чекаю реакції»;
  GET  counts/?new_after=<pk>        — непрочитані (як /email/unread-count/) і нові листи «Вхідних» після pk
                                       ([{pk, from, subject}]) — фонова перевірка додатка (🔔, позначка на іконці);
  POST translate-text/ {text, lang}  — переклад тексту (🎤 надиктоване в «Написати»), як «🌐» у формі відповіді;
  GET  messages/                     — список: folder, q, cat, wcat, customer, project, quote, fu, grouped, page;
  GET  messages/<pk>/                — гілка листа (позначає прочитаним); html/ — HTML листа для перегляду;
  GET  messages/<pk>/attachments/<i>/ — вкладення; compose/?mode=reply|all|forward — поля форми;
  POST messages/<pk>/read|unread|star|importance|spam|delete|restore|category|followup/ — як у скриньці;
  POST messages/<pk>/ai-reply|ai-prepare|ai-ask|ai-review/, GET brief/ | history/ | translate/ — AI листа;
  GET  messages/<pk>/agent/, POST agent/try/ | agent/save/ — «🤖 Доручити агенту»;
  GET  briefs/<pk>/, POST briefs/<pk>/compose|rewrite/ — «📋 Підготувати відповідь»;
  POST threads/<pk>/unarchive/, bulk/ {action, pks}, send/, schedule/, sync/, ai-generate/, followups/<pk>/,
       quote/<pk>/dismiss/; drafts/ (GET список), drafts/<pk>/ (GET), drafts/save/, drafts/<pk>/delete/;
  GET  contacts/?q=, logo/<ключ>/ — підказки адрес, значок відправника.
"""
import functools
import json
import logging

from django.contrib import messages as django_messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from email_assistant import mail_app

logger = logging.getLogger('email_assistant')


def _ip(request):
    from core.utils import client_ip
    try:
        return client_ip(request)
    except Exception:  # noqa: BLE001
        return request.META.get('REMOTE_ADDR')


def _err(text, status=400):
    return JsonResponse({'ok': False, 'error': str(text)}, status=status)


def api(methods=('GET',)):
    """Ключ телефону → request.user (людина телефону), request.device, request.account (її скринька); інший метод —
    405; Http404 / немає права / збій — JSON (збій — у журналі)."""
    def wrap(view):
        @csrf_exempt
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            if request.method not in methods:
                resp = _err('Метод не дозволено', 405)
                resp['Allow'] = ', '.join(methods)
                return resp
            device = mail_app.device_for(request.META.get('HTTP_AUTHORIZATION'), _ip(request),
                                         request.META.get('HTTP_X_MINERVA_APP', ''))
            if device is None:
                return _err('Телефон не підключено або відключено в Minerva — підключіть знову QR-кодом.', 401)
            account = mail_app.account_for(device.user)
            if account is None:
                return _err('У вашому акаунті Minerva немає активної скриньки пошти.', 403)
            request.user = request._cached_user = device.user
            request.device, request.account = device, account
            try:
                resp = view(request, *args, **kwargs)
            except Http404:
                return _err('Не знайдено', 404)
            except (PermissionDenied, PermissionError) as e:
                return _err(str(e) or 'Немає права', 403)
            except Exception:  # noqa: BLE001
                logger.exception('mail app api failed: %s', request.path)
                return _err('Помилка на сервері Minerva — спробуйте ще раз.', 500)
            if resp.status_code in (301, 302):   # функція веб-пошти відправила б на вхід — для телефону це 403
                return _err('Немає доступу', 403)
            return resp
        return inner
    return wrap


def _message(request, pk):
    from email_assistant.models import EmailMessage
    msg = EmailMessage.objects.filter(pk=pk, account=request.account).select_related('thread').first()
    if msg is None:
        raise Http404
    return msg


# ── 🔗 Підключення ──

@csrf_exempt
def pair(request):
    if request.method != 'POST':
        return _err('Метод не дозволено', 405)
    try:
        d = json.loads(request.body or b'{}') if (request.content_type or '').startswith('application/json') \
            else request.POST
    except ValueError:
        d = {}
    try:
        device, token = mail_app.pair(d.get('code'), d.get('device'), d.get('version'), _ip(request))
    except mail_app.PairError as e:
        return _err(str(e), e.status)
    u = device.user
    account = mail_app.account_for(u)
    return JsonResponse({'ok': True, 'token': token, 'device': device.pk,
                         'user': {'name': u.get_full_name() or u.username, 'email': u.email},
                         'account': {'email': account.email_address if account else ''}})


@staff_member_required
def page_view(request):
    """«📱 Пошта на телефоні»: завантажити APK (QR на GitHub), «🔗 Показати QR для підключення» — одноразовий код
    під вашим акаунтом (сторінка сама бачить, що телефон підключився: ?state=), підключені телефони, «🚫 Відключити».
    Адміністратор Minerva бачить телефони всіх."""
    from core.utils import is_minerva_admin
    admin_all = is_minerva_admin(request.user)
    account = mail_app.account_for(request.user)
    if request.method == 'GET' and request.GET.get('state'):
        return JsonResponse(mail_app.pair_state(request.user, request.GET['state']))
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'pair':
            if account is None:
                return _err('У вашому акаунті немає скриньки пошти — спершу додайте її в «Email → Акаунти».', 400)
            return JsonResponse(mail_app.new_pairing(request.user, mail_app.site_base(request)))
        if action == 'revoke':
            dev = mail_app.devices_for(request.user, admin_all).filter(pk=request.POST.get('id') or 0).first()
            if dev is None:
                raise Http404
            mail_app.revoke(dev)
            django_messages.success(request, f'🚫 Телефон «{dev.name}» відключено — ключ більше не діє.')
        return redirect(request.path)
    devices = list(mail_app.devices_for(request.user, admin_all)[:100])
    return render(request, 'email_assistant/mail_app.html', {
        'title': '📱 Пошта на телефоні', 'account': account, 'devices': devices, 'admin_all': admin_all,
        'app_url': mail_app.APP_URL, 'releases_url': mail_app.RELEASES_URL, 'app_version': mail_app.APP_VERSION,
        'base': mail_app.site_base(request), 'pair_ttl': mail_app.PAIR_TTL // 60,
        'active': sum(1 for d in devices if d.revoked_at is None)})


# ── Дані додатка ──

@api()
def me(request):
    return JsonResponse({'ok': True, **mail_app.meta(request.user, request.account, request.device)})


@api(('POST',))
def unpair(request):
    """«Відключити цей телефон» у застосунку — ключ перестає діяти одразу (як «🚫 Відключити» на сторінці)."""
    mail_app.revoke(request.device)
    return JsonResponse({'ok': True})


@api()
def nav(request):
    return JsonResponse({'ok': True, **mail_app.nav(request.user, request.account)})


@api()
def message_list(request):
    return JsonResponse({'ok': True, **mail_app.messages(request.account, request.GET)})


@api()
def message_detail(request, pk):
    msg = _message(request, pk)
    return JsonResponse({'ok': True, **mail_app.conversation(request.account, msg, request.user,
                                                             mail_app.site_base(request))})


@api()
def message_html(request, pk):
    from email_assistant.views import _email_response
    msg = _message(request, pk)
    html = mail_app.app_html(request.account, msg)
    if not html:
        raise Http404
    return _email_response(html)


@api()
def message_compose(request, pk):
    mode = request.GET.get('mode') if request.GET.get('mode') in ('reply', 'all', 'forward') else 'reply'
    return JsonResponse({'ok': True, **mail_app.compose(request.account, _message(request, pk), mode)})


@api()
def draft_list(request):
    return JsonResponse({'ok': True, **mail_app.drafts(request.account, request.GET)})


@api()
def draft_detail(request, pk):
    from email_assistant import drafts as _drafts
    d = _drafts.get(request.account, pk)
    if d is None:
        raise Http404
    return JsonResponse({'ok': True, **mail_app.draft_full(request.account, d)})


# ── Ті самі функції, що у веб-пошті ──

def _views():
    from email_assistant import views
    return views


def _delegate(name, methods=('POST',), module='email'):
    @api(methods)
    def view(request, *args, **kwargs):
        if module == 'agent':
            from agent import views as mod
        elif module == 'offers':
            from offers import views as mod
        else:
            mod = _views()
        return getattr(mod, name)(request, *args, **kwargs)
    view.__name__ = f'app_{module}_{name}'
    return view


counts = _delegate('unread_count_view', ('GET',))
translate_text = _delegate('translate_text_view')
mark_read = _delegate('mark_read_api')
mark_unread = _delegate('mark_unread_api')
star = _delegate('toggle_star_view')
importance = _delegate('importance_view')
spam = _delegate('toggle_spam_view')
delete = _delegate('delete_message_view')
restore = _delegate('restore_message_view')
work_category = _delegate('set_work_category_view')
message_followup = _delegate('message_followup_view')
followup_action = _delegate('followup_action_view')
attachment = _delegate('attachment_download_view', ('GET',))
ai_reply = _delegate('ai_suggest_reply')
ai_prepare = _delegate('ai_prepare_view')
ai_ask = _delegate('ai_ask_view')
ai_review = _delegate('ai_review_view')
message_brief = _delegate('message_brief_view', ('GET',))
history = _delegate('history_view', ('GET',))
translate = _delegate('ai_translate', ('GET',))
brief = _delegate('reply_brief_view', ('GET',))
brief_compose = _delegate('reply_brief_compose_view')
brief_rewrite = _delegate('reply_brief_rewrite_view')
unarchive = _delegate('unarchive_thread_view')
bulk = _delegate('bulk_action_view')
send = _delegate('send_api')
schedule = _delegate('schedule_email_api')
sync = _delegate('sync_now')
ai_generate = _delegate('ai_generate_email')
contacts = _delegate('crm_contacts_view', ('GET',))
logo = _delegate('sender_logo_view', ('GET',))
draft_save = _delegate('draft_save_view')
draft_delete = _delegate('draft_delete_view')
agent_suggest = _delegate('suggest_view', ('GET',), module='agent')
agent_try = _delegate('try_view', module='agent')
agent_save = _delegate('save_view', module='agent')
quote_dismiss = _delegate('dismiss_quote_request_view', module='offers')
