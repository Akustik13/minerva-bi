// Екран листа: уся гілка (нові внизу, старі згорнуті), текст без історії листування (розгортається), вкладення,
// оригінал HTML, зв'язки з замовленнями, запит пропозиції, робота агента. Внизу — великі кнопки для пальця:
// Відповісти / Усім / Переслати / ✨ AI.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../util.dart';
import '../widgets/common.dart';
import 'agent_sheet.dart';
import 'ai_sheet.dart';
import 'compose_screen.dart';
import 'html_screen.dart';
import 'images.dart';

class ThreadScreen extends StatefulWidget {
  const ThreadScreen({super.key, required this.messageId, this.title = ''});

  final int messageId;
  final String title;

  @override
  State<ThreadScreen> createState() => _ThreadScreenState();
}

class _ThreadScreenState extends State<ThreadScreen> {
  Map<String, dynamic>? data;
  String error = '';
  bool changed = false;
  final Set<int> expanded = {};
  final Set<int> quoteOpen = {};

  AppState get app => AppScope.read(context);
  Api get api => app.api!;
  List<Map<String, dynamic>> get messages => ((data?['messages'] as List?) ?? []).cast<Map<String, dynamic>>();
  Map<String, dynamic> get last => messages.last;

  @override
  void initState() {
    super.initState();
    final cached = AppScope.read(context).store.readJson('thread:${widget.messageId}');
    if (cached != null) _apply(cached);
    _load();
  }

  void _apply(Map<String, dynamic> j) {
    data = j;
    final msgs = messages;
    if (msgs.isNotEmpty) {
      expanded.add(msgs.last['id'] as int);
      for (final m in msgs) {
        if (m['read'] != true || m['id'] == widget.messageId) expanded.add(m['id'] as int);
      }
    }
  }

  Future<void> _load() async {
    try {
      final j = await api.get('messages/${widget.messageId}/');
      if (!mounted) return;
      setState(() {
        _apply(j);
        error = '';
      });
      app.store.writeJson('thread:${widget.messageId}', j);
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    }
  }

  Future<void> _post(String path, {Map<String, dynamic>? body, String? done, bool close = false}) async {
    try {
      final r = await api.postJson(path, body);
      if (r['ok'] == false) throw ApiException(400, (r['error'] ?? '').toString());
      changed = true;
      app.refreshCounts();
      if (!mounted) return;
      if (done != null) snack(context, done);
      if (close) {
        Navigator.of(context).pop(true);
      } else {
        _load();
      }
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
    }
  }

  List<int> get _pks => messages.map((m) => m['id'] as int).toList();

  Future<void> _archive() async {
    final thread = (data?['thread'] as Map?)?['id'];
    if (thread == null) return _delete();
    await _post('bulk/', body: {'action': 'archive', 'pks': _pks}, done: context.t('archived_done'), close: true);
  }

  Future<void> _delete() => _post('bulk/', body: {'action': 'delete', 'pks': _pks}, done: context.t('deleted_done'), close: true);

  Future<void> _markUnread() =>
      _post('bulk/', body: {'action': 'unread', 'pks': [last['id']]}, done: context.t('marked_unread'), close: true);

  Future<void> _spam() async {
    final done = context.t('spam_done');
    if (!await confirm(context, context.t('spam_confirm'))) return;
    await _post('messages/${last['id']}/spam/', done: done, close: true);
  }

  Future<void> _importance() async {
    final level = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (c) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(title: Text(c.t('importance'), style: const TextStyle(fontWeight: FontWeight.w700))),
          ListTile(leading: const Text('—', style: TextStyle(fontSize: 20)), title: Text(c.t('imp_none')), onTap: () => Navigator.pop(c, '')),
          ListTile(leading: const Text('❗', style: TextStyle(fontSize: 20)), title: Text(c.t('imp_important')), onTap: () => Navigator.pop(c, 'important')),
          ListTile(leading: const Text('‼️', style: TextStyle(fontSize: 20)), title: Text(c.t('imp_urgent')), onTap: () => Navigator.pop(c, 'urgent')),
        ]),
      ),
    );
    if (level == null) return;
    await _post('messages/${last['id']}/importance/', body: {'level': level});
  }

  Future<void> _category() async {
    if (app.nav == null) {
      try {
        await app.refreshNav();
      } catch (_) {}
    }
    if (!mounted) return;
    final cats = ((app.nav?['categories'] as List?) ?? []).cast<Map<String, dynamic>>();
    final current = (((data?['thread'] as Map?)?['categories'] as List?) ?? []).map((c) => (c as Map)['id']).toSet();
    final picked = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (c) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: MediaQuery.of(c).size.height * 0.7),
          child: ListView(shrinkWrap: true, children: [
            ListTile(
              title: Text(c.t('category'), style: const TextStyle(fontWeight: FontWeight.w700)),
              subtitle: Text(c.t('category_hint')),
            ),
            for (final cat in cats)
              CheckboxListTile(
                value: current.contains(cat['id']),
                title: Text('${cat['icon'] ?? ''} ${cat['name']}'.trim()),
                onChanged: (_) => Navigator.pop(c, {'cat': cat, 'remove': current.contains(cat['id'])}),
              ),
            if (app.perm('category_add'))
              ListTile(
                leading: const Icon(Icons.add),
                title: Text(c.t('category_new')),
                onTap: () => Navigator.pop(c, {'new': true}),
              ),
          ]),
        ),
      ),
    );
    if (picked == null || !mounted) return;
    if (picked['new'] == true) {
      final name = await _ask(context.t('category_new'), context.t('category_name'));
      if (name == null || name.isEmpty) return;
      await _post('messages/${last['id']}/category/', body: {'category': 'new', 'name': name, 'scope': 'sender', 'mode': 'add'});
      app.refreshNav();
      return;
    }
    final cat = picked['cat'] as Map<String, dynamic>;
    await _post('messages/${last['id']}/category/',
        body: {'category': '${cat['id']}', 'scope': 'sender', 'mode': picked['remove'] == true ? 'remove' : 'add'});
  }

  Future<String?> _ask(String title, String label) {
    final ctl = TextEditingController();
    return showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(title),
        content: TextField(controller: ctl, autofocus: true, decoration: InputDecoration(labelText: label)),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: Text(c.t('cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, ctl.text.trim()), child: Text(c.t('ok'))),
        ],
      ),
    );
  }

  Future<void> _compose(String mode, {int? messageId, String body = ''}) async {
    final sent = await Navigator.of(context).push<bool>(MaterialPageRoute(
        builder: (_) => ComposeScreen(mode: mode, messageId: messageId ?? last['id'] as int, initialBody: body)));
    if (sent == true) {
      changed = true;
      _load();
    }
  }

  Future<void> _openUrl(String url) async {
    final u = Uri.tryParse(url);
    if (u != null) await launchUrl(u, mode: LaunchMode.externalApplication);
  }

  Future<void> _openAttachment(Map<String, dynamic> m, Map<String, dynamic> a) async {
    snack(context, '${context.t('downloading')} ${a['name']}…', seconds: 2);
    try {
      final r = await api.raw('messages/${m['id']}/attachments/${a['i']}/', query: {'download': '1'});
      final dir = await getTemporaryDirectory();
      // без розширення («Angebot», переслане «.eml») програма телефона файл не відкриє — додаємо за типом;
      // окрема тека на кожне вкладення — однакові назви в листі не перезаписують одна одну
      final safe = attachmentFileName(a['name'] as String? ?? '', a['type'] as String? ?? '');
      final folder = Directory('${dir.path}/att_${m['id']}_${a['i']}');
      await folder.create(recursive: true);
      final file = File('${folder.path}/$safe');
      await file.writeAsBytes(r.bodyBytes);
      final res = await OpenFilex.open(file.path);
      if (res.type != ResultType.done && mounted) snack(context, context.t('no_app_for_file'));
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
    }
  }

  void _menu(String v) {
    switch (v) {
      case 'unread':
        _markUnread();
      case 'star':
        _post('messages/${last['id']}/star/');
      case 'importance':
        _importance();
      case 'category':
        _category();
      case 'spam':
        _spam();
      case 'agent':
        showAgentSheet(context, last['id'] as int);
      case 'web':
        _openUrl(data?['web_url'] as String? ?? '');
      case 'unarchive':
        final t = (data?['thread'] as Map?)?['id'];
        if (t != null) _post('threads/$t/unarchive/', done: context.t('unarchived_done'));
      case 'restore':
        _post('bulk/', body: {'action': 'restore', 'pks': _pks}, done: context.t('restored_done'), close: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final thread = data?['thread'] as Map<String, dynamic>?;
    final archived = thread?['archived'] == true;
    final inTrash = messages.isNotEmpty && messages.every((m) => m['folder'] == 'trash');
    final starred = messages.isNotEmpty && last['starred'] == true;
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) Navigator.of(context).pop(changed);
      },
      child: Scaffold(
        appBar: AppBar(
          actions: [
            IconButton(tooltip: context.t('archive'), icon: const Icon(Icons.archive_outlined), onPressed: data == null ? null : _archive),
            IconButton(tooltip: context.t('delete'), icon: const Icon(Icons.delete_outline), onPressed: data == null ? null : _delete),
            IconButton(
                tooltip: context.t('mark_unread'), icon: const Icon(Icons.mark_email_unread_outlined), onPressed: data == null ? null : _markUnread),
            PopupMenuButton<String>(
              enabled: data != null,
              onSelected: _menu,
              itemBuilder: (c) => [
                PopupMenuItem(value: 'star', child: ListTile(leading: Icon(starred ? Icons.star : Icons.star_border), title: Text(starred ? c.t('unstar') : c.t('star')))),
                PopupMenuItem(value: 'importance', child: ListTile(leading: const Icon(Icons.priority_high), title: Text(c.t('importance')))),
                PopupMenuItem(value: 'category', child: ListTile(leading: const Icon(Icons.label_outline), title: Text(c.t('category')))),
                if (app.perm('agent_rule'))
                  PopupMenuItem(value: 'agent', child: ListTile(leading: const Text('🤖', style: TextStyle(fontSize: 20)), title: Text(c.t('agent_title')))),
                if (archived) PopupMenuItem(value: 'unarchive', child: ListTile(leading: const Icon(Icons.move_to_inbox), title: Text(c.t('unarchive')))),
                if (inTrash) PopupMenuItem(value: 'restore', child: ListTile(leading: const Icon(Icons.restore_from_trash), title: Text(c.t('restore')))),
                PopupMenuItem(value: 'spam', child: ListTile(leading: const Icon(Icons.report_outlined), title: Text(c.t('to_spam')))),
                PopupMenuItem(value: 'web', child: ListTile(leading: const Icon(Icons.open_in_new), title: Text(c.t('open_web')))),
              ],
            ),
          ],
        ),
        body: data == null
            ? (error.isEmpty
                ? const Center(child: CircularProgressIndicator())
                : EmptyState(icon: Icons.cloud_off, text: error, action: context.t('retry'), onAction: _load))
            : RefreshIndicator(
                onRefresh: _load,
                child: ListView(
                  padding: const EdgeInsets.only(bottom: 24),
                  children: [
                    _header(context, thread!),
                    ..._banners(context),
                    for (final m in messages) _messageCard(context, m),
                  ],
                ),
              ),
        bottomNavigationBar: data == null
            ? null
            : SafeArea(
                child: Container(
                  decoration: BoxDecoration(
                    color: scheme.surfaceContainer,
                    border: Border(top: BorderSide(color: scheme.outlineVariant, width: 0.5)),
                  ),
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  child: Row(children: [
                    BarButton(icon: Icons.reply, label: context.t('reply'), onTap: () => _compose('reply')),
                    BarButton(icon: Icons.reply_all, label: context.t('reply_all'), onTap: () => _compose('all')),
                    BarButton(icon: Icons.forward, label: context.t('forward'), onTap: () => _compose('forward')),
                    BarButton(
                      icon: Icons.auto_awesome,
                      label: 'AI',
                      onTap: () => showAiSheet(context, last['id'] as int,
                          onUseReply: (text) => _compose('reply', body: text)),
                    ),
                  ]),
                ),
              ),
      ),
    );
  }

  Widget _header(BuildContext context, Map<String, dynamic> thread) {
    final scheme = Theme.of(context).colorScheme;
    final cats = ((thread['categories'] as List?) ?? []).cast<Map<String, dynamic>>();
    final projects = ((thread['projects'] as List?) ?? []).cast<Map<String, dynamic>>();
    final customer = thread['customer'] as Map<String, dynamic>?;
    final subject = (thread['subject'] as String? ?? '').isEmpty ? widget.title : thread['subject'] as String;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SelectableText(subject.isEmpty ? context.t('no_subject') : subject,
            style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        if (cats.isNotEmpty || projects.isNotEmpty || customer != null || thread['archived'] == true)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Wrap(spacing: 6, runSpacing: 4, children: [
              if (thread['archived'] == true) TagChip(label: context.t('archived'), icon: '📦'),
              for (final c in cats)
                TagChip(label: c['name'] as String, icon: c['icon'] as String? ?? '', color: hexColor(c['color'] as String?, scheme.secondary)),
              if (customer != null)
                InkWell(
                    onTap: () => _openUrl(customer['url'] as String? ?? ''),
                    child: TagChip(label: customer['name'] as String? ?? '', icon: '👤')),
              for (final p in projects)
                InkWell(
                  onTap: () => _openUrl(p['url'] as String? ?? ''),
                  child: TagChip(label: '${p['code']} ${p['name']}', icon: (p['icon'] as String?)?.isNotEmpty == true ? p['icon'] as String : '📁'),
                ),
            ]),
          ),
      ]),
    );
  }

  List<Widget> _banners(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final out = <Widget>[];
    final quote = data?['quote_request'] as Map<String, dynamic>?;
    if (quote != null) {
      out.add(_banner(
        context,
        color: Colors.orange,
        icon: '📝',
        title: context.t('quote_banner'),
        text: quote['reason'] as String? ?? '',
        actions: [
          if (app.perm('offers_add'))
            TextButton(onPressed: () => _openUrl(quote['url'] as String? ?? ''), child: Text(context.t('quote_create'))),
          TextButton(
            onPressed: () => _post('quote/${quote['id']}/dismiss/', done: context.t('quote_dismissed')),
            child: Text(context.t('quote_dismiss')),
          ),
        ],
      ));
    }
    for (final l in ((data?['links'] as List?) ?? []).cast<Map<String, dynamic>>()) {
      final ships = ((l['shipments'] as List?) ?? []).cast<Map<String, dynamic>>();
      out.add(_banner(
        context,
        color: scheme.primary,
        icon: '🔗',
        title: '${context.t('order')} ${l['label']}${(l['customer'] as String? ?? '').isNotEmpty ? ' · ${l['customer']}' : ''}',
        text: ships.map((s) => '🚚 ${s['carrier']} ${s['tracking']} — ${s['status']}').join('\n'),
        actions: [TextButton(onPressed: () => _openUrl(l['url'] as String? ?? ''), child: Text(context.t('open_web')))],
      ));
    }
    for (final r in ((data?['agent_runs'] as List?) ?? []).cast<Map<String, dynamic>>()) {
      out.add(_banner(
        context,
        color: Colors.purple,
        icon: '🤖',
        title: '${context.t('agent_word')} · ${r['rule']}',
        text: [r['status'], r['reason'], r['done']].where((x) => (x as String? ?? '').isNotEmpty).join(' · '),
        actions: [
          if (r['draft'] != null)
            TextButton(
              onPressed: () async {
                await Navigator.of(context).push(MaterialPageRoute(builder: (_) => ComposeScreen(draftId: r['draft'] as int)));
                _load();
              },
              child: Text(context.t('open_draft')),
            ),
        ],
      ));
    }
    for (final f in ((data?['followups'] as List?) ?? []).cast<Map<String, dynamic>>()) {
      final open = f['status'] != 'done';
      out.add(_banner(
        context,
        color: f['tone'] == 'bad' ? Colors.red : Colors.blueGrey,
        icon: f['icon'] as String? ?? '⏰',
        title: context.t('followup_title'),
        text: f['text'] as String? ?? '',
        actions: [
          if (open)
            TextButton(
                onPressed: () => _post('followups/${f['id']}/', body: {'action': 'snooze', 'days': 2}),
                child: Text(context.t('followup_snooze'))),
          if (open)
            TextButton(onPressed: () => _post('followups/${f['id']}/', body: {'action': 'done'}), child: Text(context.t('followup_done'))),
        ],
      ));
    }
    return out;
  }

  Widget _banner(BuildContext context,
      {required Color color, required String icon, required String title, String text = '', List<Widget> actions = const []}) {
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 4),
      padding: const EdgeInsets.fromLTRB(12, 8, 8, 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: color.withValues(alpha: 0.35)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(icon, style: const TextStyle(fontSize: 18)),
          const SizedBox(width: 8),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
              if (text.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 2), child: Text(text, style: const TextStyle(fontSize: 13))),
            ]),
          ),
        ]),
        if (actions.isNotEmpty) Align(alignment: Alignment.centerRight, child: Wrap(children: actions)),
      ]),
    );
  }

  Widget _messageCard(BuildContext context, Map<String, dynamic> m) {
    final scheme = Theme.of(context).colorScheme;
    final id = m['id'] as int;
    final open = expanded.contains(id);
    final own = m['own'] == true;
    final to = ((m['to'] as List?) ?? []).cast<String>();
    final cc = ((m['cc'] as List?) ?? []).cast<String>();
    final all = ((m['attachments'] as List?) ?? []).cast<Map<String, dynamic>>();
    final images = all.where(galleryImage).toList();
    final atts = all.where(fileAttachment).toList();
    final name = (m['from'] as String? ?? '').isEmpty ? m['from_email'] as String? ?? '' : m['from'] as String;
    final head = InkWell(
      onTap: () => setState(() => open ? expanded.remove(id) : expanded.add(id)),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 10, 8, 6),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SenderAvatar(name: name, logo: own ? '' : m['logo'] as String? ?? '', logoV: m['logo_v'] as String? ?? '', size: 40),
          const SizedBox(width: 10),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Expanded(
                  child: Text(own ? '${context.t('me')} · $name' : name,
                      maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
                ),
                if ((m['importance'] as String? ?? '') == 'urgent') const Text('‼️') else if ((m['importance'] as String? ?? '') == 'important') const Text('❗'),
                if (m['starred'] == true) const Icon(Icons.star, size: 16, color: Colors.amber),
                const SizedBox(width: 4),
                Text(fmtShortDate(context, m['date'] as String? ?? ''), style: TextStyle(fontSize: 12.5, color: scheme.onSurfaceVariant)),
              ]),
              if (open)
                Text('${context.t('to_prefix')} ${to.join(', ')}${cc.isEmpty ? '' : '\n${context.t('cc')}: ${cc.join(', ')}'}',
                    style: TextStyle(fontSize: 12.5, color: scheme.onSurfaceVariant))
              else
                Text((m['text'] as String? ?? '').replaceAll(RegExp(r'\s+'), ' '),
                    maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 13, color: scheme.onSurfaceVariant)),
            ]),
          ),
          if (open)
            PopupMenuButton<String>(
              icon: const Icon(Icons.more_vert, size: 20),
              onSelected: (v) {
                if (v == 'reply' || v == 'all' || v == 'forward') _compose(v, messageId: id);
                if (v == 'html') _openHtml(m);
                if (v == 'agent') showAgentSheet(context, id);
              },
              itemBuilder: (c) => [
                PopupMenuItem(value: 'reply', child: Text(c.t('reply'))),
                PopupMenuItem(value: 'all', child: Text(c.t('reply_all'))),
                PopupMenuItem(value: 'forward', child: Text(c.t('forward'))),
                if (m['html'] == true) PopupMenuItem(value: 'html', child: Text(c.t('original'))),
                if (app.perm('agent_rule')) PopupMenuItem(value: 'agent', child: Text('🤖 ${c.t('agent_title')}')),
              ],
            ),
        ]),
      ),
    );
    if (!open) {
      return Card(margin: const EdgeInsets.fromLTRB(10, 4, 10, 4), child: head);
    }
    final quote = m['quote'] as String? ?? '';
    final showQuote = quoteOpen.contains(id);
    return Card(
      margin: const EdgeInsets.fromLTRB(10, 4, 10, 4),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        head,
        if ((m['agent'] as String? ?? '').isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 4),
            child: Text('🤖 ${m['agent']}', style: TextStyle(fontSize: 12.5, color: Colors.purple.shade300)),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(14, 4, 14, 8),
          child: SelectableText(
            (m['text'] as String? ?? '').trim().isEmpty ? context.t('empty_body') : (m['text'] as String).trim(),
            style: const TextStyle(fontSize: 15.5, height: 1.45),
          ),
        ),
        if (quote.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6),
            child: TextButton.icon(
              onPressed: () => setState(() => showQuote ? quoteOpen.remove(id) : quoteOpen.add(id)),
              icon: Icon(showQuote ? Icons.expand_less : Icons.more_horiz),
              label: Text(showQuote ? context.t('hide_quote') : context.t('show_quote')),
            ),
          ),
        if (quote.isNotEmpty && showQuote)
          Container(
            margin: const EdgeInsets.fromLTRB(14, 0, 14, 8),
            padding: const EdgeInsets.only(left: 10),
            decoration: BoxDecoration(border: Border(left: BorderSide(color: scheme.outlineVariant, width: 3))),
            child: SelectableText(quote, style: TextStyle(fontSize: 13.5, color: scheme.onSurfaceVariant, height: 1.4)),
          ),
        if (images.isNotEmpty) ImageStrip(api: api, messageId: id, images: images, onOpenFile: (a) => _openAttachment(m, a)),
        if (atts.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 0, 10, 8),
            child: Wrap(spacing: 6, runSpacing: 6, children: [
              for (final a in atts)
                ActionChip(
                  avatar: Icon(_fileIcon(a['type'] as String? ?? '', a['name'] as String? ?? ''), size: 18),
                  label: Text('${a['name']}${fmtSize(a['size'] as num? ?? 0).isEmpty ? '' : '  ${fmtSize(a['size'] as num)}'}',
                      overflow: TextOverflow.ellipsis),
                  onPressed: () => _openAttachment(m, a),
                ),
            ]),
          ),
        if (m['html'] == true)
          Padding(
            padding: const EdgeInsets.fromLTRB(6, 0, 6, 6),
            child: TextButton.icon(onPressed: () => _openHtml(m), icon: const Icon(Icons.language), label: Text(context.t('original'))),
          ),
      ]),
    );
  }

  void _openHtml(Map<String, dynamic> m) {
    Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => HtmlScreen(messageId: m['id'] as int, title: m['subject'] as String? ?? '')));
  }

  IconData _fileIcon(String type, String name) {
    final n = name.toLowerCase();
    if (type.startsWith('image/')) return Icons.image_outlined;
    if (type == 'application/pdf' || n.endsWith('.pdf')) return Icons.picture_as_pdf_outlined;
    if (n.endsWith('.xlsx') || n.endsWith('.xls') || n.endsWith('.csv')) return Icons.table_chart_outlined;
    if (n.endsWith('.zip') || n.endsWith('.rar') || n.endsWith('.7z')) return Icons.folder_zip_outlined;
    return Icons.insert_drive_file_outlined;
  }
}
