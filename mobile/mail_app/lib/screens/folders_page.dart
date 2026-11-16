// «Папки»: усе з лівої колонки скриньки Minerva — папки з непрочитаними, свої категорії, запити пропозицій,
// «⏰ Чекаю реакції», хто пише, клієнти, проєкти. Дотик — список цих листів на вкладці «Пошта».
import 'package:flutter/material.dart';

import '../l10n.dart';
import '../state.dart';
import '../util.dart';
import 'mail_list.dart';

class FoldersPage extends StatefulWidget {
  const FoldersPage({super.key, required this.onPick, required this.current});

  final void Function(MailQuery q) onPick;
  final MailQuery current;

  @override
  State<FoldersPage> createState() => _FoldersPageState();
}

class _FoldersPageState extends State<FoldersPage> {
  String error = '';
  bool showAllCustomers = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _refresh());
  }

  Future<void> _refresh() async {
    final app = AppScope.read(context);
    try {
      await Future.wait([app.refreshMeta(), app.refreshNav()]);
      if (mounted) setState(() => error = '');
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    }
  }

  static const _icons = {
    'inbox': Icons.inbox_outlined,
    'sent': Icons.send_outlined,
    'drafts': Icons.drafts_outlined,
    'starred': Icons.star_outline,
    'spam': Icons.report_outlined,
    'archived': Icons.archive_outlined,
    'trash': Icons.delete_outline,
  };

  @override
  Widget build(BuildContext context) {
    final app = AppScope.of(context);
    final scheme = Theme.of(context).colorScheme;
    final folders = ((app.meta?['folders'] as List?) ?? []).cast<Map<String, dynamic>>();
    final nav = app.nav ?? {};
    final cats = ((nav['categories'] as List?) ?? []).cast<Map<String, dynamic>>();
    final kinds = ((nav['kinds'] as List?) ?? []).cast<Map<String, dynamic>>();
    final customers = ((nav['customers'] as List?) ?? []).cast<Map<String, dynamic>>();
    final projects = ((nav['projects'] as List?) ?? []).cast<Map<String, dynamic>>();
    final quotes = (nav['quotes'] as num?)?.toInt() ?? 0;
    final fu = (nav['followups'] as Map?) ?? {};
    final fuOpen = (fu['open'] as num?)?.toInt() ?? 0;
    final fuBad = (fu['bad'] as num?)?.toInt() ?? 0;
    final cur = widget.current.key;

    Widget section(String title) => Padding(
          padding: const EdgeInsets.fromLTRB(20, 18, 16, 4),
          child: Text(title.toUpperCase(),
              style: TextStyle(fontSize: 12, letterSpacing: 0.8, fontWeight: FontWeight.w700, color: scheme.primary)),
        );

    Widget tile({required Widget leading, required String title, String? subtitle, int count = 0, bool strong = false,
        required MailQuery q}) {
      final selected = q.key == cur;
      return ListTile(
        selected: selected,
        selectedTileColor: scheme.secondaryContainer,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
        contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        minTileHeight: 52,
        leading: leading,
        title: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: subtitle == null ? null : Text(subtitle, maxLines: 1, overflow: TextOverflow.ellipsis),
        trailing: count > 0
            ? Text('$count', style: TextStyle(fontWeight: strong ? FontWeight.w700 : FontWeight.w400, color: strong ? scheme.primary : null))
            : null,
        onTap: () => widget.onPick(q),
      );
    }

    return Scaffold(
      appBar: AppBar(title: Text(context.t('folders'))),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(padding: const EdgeInsets.only(bottom: 24), children: [
          if (error.isNotEmpty)
            ListTile(leading: const Icon(Icons.cloud_off), title: Text(error), trailing: TextButton(onPressed: _refresh, child: Text(context.t('retry')))),
          for (final f in folders)
            tile(
              leading: Icon(_icons[f['key']] ?? Icons.folder_outlined),
              title: context.t(f['key'] as String),
              count: (f['unread'] as num?)?.toInt() ?? 0,
              strong: f['key'] != 'drafts',
              q: MailQuery(folder: f['key'] as String),
            ),
          if (quotes > 0 || fuOpen > 0) section(context.t('section_work')),
          if (quotes > 0)
            tile(
              leading: const Text('📝', style: TextStyle(fontSize: 20)),
              title: context.t('quotes'),
              count: quotes,
              strong: true,
              q: MailQuery(filters: const {'quote': '1'}, title: context.t('quotes'), icon: '📝'),
            ),
          if (fuOpen > 0)
            tile(
              leading: const Text('⏰', style: TextStyle(fontSize: 20)),
              title: context.t('followups'),
              subtitle: fuBad > 0 ? '⚠️ ${context.t('no_reaction')}: $fuBad' : null,
              count: fuOpen,
              q: MailQuery(filters: const {'fu': '1'}, title: context.t('followups'), icon: '⏰'),
            ),
          if (cats.isNotEmpty) section(context.t('my_categories')),
          for (final c in cats)
            tile(
              leading: Container(
                width: 24,
                alignment: Alignment.center,
                child: (c['icon'] as String? ?? '').isNotEmpty
                    ? Text(c['icon'] as String, style: const TextStyle(fontSize: 20))
                    : Container(
                        width: 14,
                        height: 14,
                        decoration: BoxDecoration(color: hexColor(c['color'] as String?, scheme.secondary), shape: BoxShape.circle)),
              ),
              title: c['name'] as String,
              count: (c['unread'] as num?)?.toInt() ?? 0,
              strong: true,
              q: MailQuery(filters: {'wcat': '${c['id']}'}, title: c['name'] as String, icon: c['icon'] as String? ?? ''),
            ),
          if (cats.isNotEmpty && ((nav['no_category'] as num?) ?? 0) > 0)
            tile(
              leading: const Icon(Icons.label_off_outlined),
              title: context.t('no_category'),
              q: MailQuery(filters: const {'wcat': '0'}, title: context.t('no_category')),
            ),
          if (kinds.isNotEmpty) section(context.t('who_writes')),
          for (final k in kinds)
            tile(
              leading: const Icon(Icons.people_outline),
              title: k['label'] as String,
              count: (k['unread'] as num?)?.toInt() ?? 0,
              q: MailQuery(filters: {'cat': k['key'] as String}, title: k['label'] as String),
            ),
          if (projects.isNotEmpty) section(context.t('projects')),
          for (final p in projects)
            tile(
              leading: Text((p['icon'] as String? ?? '').isEmpty ? '📁' : p['icon'] as String, style: const TextStyle(fontSize: 20)),
              title: p['name'] as String? ?? '',
              subtitle: p['code'] as String?,
              q: MailQuery(filters: {'project': '${p['id']}'}, title: p['name'] as String? ?? '', icon: '📁'),
            ),
          if (customers.isNotEmpty) section(context.t('customers')),
          for (final c in (showAllCustomers ? customers : customers.take(8)))
            tile(
              leading: const Icon(Icons.person_outline),
              title: c['name'] as String? ?? '',
              count: (c['unread'] as num?)?.toInt() ?? 0,
              strong: true,
              q: MailQuery(filters: {'customer': '${c['id']}'}, title: c['name'] as String? ?? '', icon: '👤'),
            ),
          if (customers.length > 8 && !showAllCustomers)
            TextButton(onPressed: () => setState(() => showAllCustomers = true), child: Text(context.t('show_all'))),
        ]),
      ),
    );
  }
}
