// Список листів (або чернеток): папка / своя категорія / клієнт / проєкт / запити пропозицій, пошук по всій пошті,
// свайпи (праворуч — прочитано / непрочитано, ліворуч — в архів або в кошик), вибір кількох довгим натиском.
// Перша сторінка кожного списку зберігається в телефоні — відкривається одразу, а свіжі листи догружаються.
import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../util.dart';
import '../widgets/common.dart';
import 'compose_screen.dart';
import 'thread_screen.dart';

class MailQuery {
  const MailQuery({this.folder = 'inbox', this.filters = const {}, this.title = '', this.icon = ''});

  final String folder;
  final Map<String, String> filters;
  final String title;
  final String icon;

  bool get filtered => filters.isNotEmpty;
  bool get isDrafts => folder == 'drafts' && filters.isEmpty;
  String get key => [folder, ...filters.entries.map((e) => '${e.key}=${e.value}')].join('&');
}

class MailListPage extends StatefulWidget {
  const MailListPage({super.key, required this.query, required this.refreshTick, this.onClearFilter});

  final MailQuery query;
  final ValueNotifier<int> refreshTick;
  final VoidCallback? onClearFilter;

  @override
  State<MailListPage> createState() => _MailListPageState();
}

class _MailListPageState extends State<MailListPage> {
  final List<Map<String, dynamic>> items = [];
  final Set<int> selected = {};
  final scroll = ScrollController();
  final searchCtl = TextEditingController();
  bool loading = false;
  bool loadingMore = false;
  bool hasNext = false;
  bool fromCache = false;
  bool searching = false;
  String q = '';
  int page = 1;
  String error = '';
  int _gen = 0;

  AppState get app => AppScope.read(context);
  Api? get api => app.api;
  bool get draftsMode => widget.query.isDrafts && q.isEmpty;

  String get cacheKey => 'list:${widget.query.key}:${app.store.grouped ? 1 : 0}';

  @override
  void initState() {
    super.initState();
    scroll.addListener(_onScroll);
    widget.refreshTick.addListener(_refresh);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final cached = app.store.readJson(cacheKey);
      if (cached != null && items.isEmpty) {
        setState(() {
          items.addAll((cached['items'] as List).cast<Map<String, dynamic>>());
          hasNext = cached['has_next'] == true;
          fromCache = true;
        });
      }
      _load();
    });
  }

  @override
  void dispose() {
    widget.refreshTick.removeListener(_refresh);
    scroll.dispose();
    searchCtl.dispose();
    super.dispose();
  }

  /// Тихе оновлення (таймер, повернення в застосунок): не збиває, коли людина гортає далі або вибирає листи.
  void _refresh() {
    if (selected.isNotEmpty || searching || page > 1 || (scroll.hasClients && scroll.offset > 300)) return;
    _load();
  }

  void _onScroll() {
    if (scroll.position.pixels > scroll.position.maxScrollExtent - 600 && hasNext && !loadingMore && !loading) {
      _loadMore();
    }
  }

  Map<String, String> _params(int p) => {
        'folder': widget.query.folder,
        ...widget.query.filters,
        if (q.isNotEmpty) 'q': q,
        if (app.store.grouped && !draftsMode) 'grouped': '1',
        'page': '$p',
      };

  Future<void> _load() async {
    final a = api;
    if (a == null) return;
    final gen = ++_gen;
    setState(() {
      loading = true;
      error = '';
    });
    try {
      final j = await a.get('messages/', query: _params(1));
      if (!mounted || gen != _gen) return;
      setState(() {
        items
          ..clear()
          ..addAll((j['items'] as List).cast<Map<String, dynamic>>());
        hasNext = j['has_next'] == true;
        page = 1;
        fromCache = false;
        selected.clear();
      });
      if (q.isEmpty) app.store.writeJson(cacheKey, {'items': items, 'has_next': hasNext});
    } catch (e) {
      if (!mounted || gen != _gen) return;
      setState(() => error = errorText(context, e));
    } finally {
      if (mounted && gen == _gen) setState(() => loading = false);
    }
  }

  Future<void> _loadMore() async {
    final a = api;
    if (a == null) return;
    setState(() => loadingMore = true);
    try {
      final j = await a.get('messages/', query: _params(page + 1));
      if (!mounted) return;
      setState(() {
        final known = items.map((e) => e['id']).toSet();
        items.addAll((j['items'] as List).cast<Map<String, dynamic>>().where((e) => !known.contains(e['id'])));
        hasNext = j['has_next'] == true;
        page += 1;
      });
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
    } finally {
      if (mounted) setState(() => loadingMore = false);
    }
  }

  List<int> _pks(Map<String, dynamic> row) => ((row['pks'] as List?) ?? [row['id']]).map((e) => (e as num).toInt()).toList();

  Future<bool> _bulk(String action, List<int> pks) async {
    try {
      final r = await api!.postJson('bulk/', {'action': action, 'pks': pks});
      if (r['ok'] == false) throw ApiException(400, (r['error'] ?? '').toString());
      app.refreshCounts();
      return true;
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
      return false;
    }
  }

  // ── дії рядка ──

  Future<void> _toggleRead(Map<String, dynamic> row) async {
    final read = !(row['read'] == true && (row['thread_unread'] ?? 0) == 0);
    setState(() {
      row['read'] = read;
      row['thread_unread'] = read ? 0 : 1;
    });
    if (!await _bulk(read ? 'read' : 'unread', _pks(row)) && mounted) {
      setState(() => row['read'] = !read);
    }
  }

  /// Що робить свайп ліворуч у цій папці: (дія, підпис, значок, колір).
  (String, String, IconData, Color) _leftAction(BuildContext context) {
    final folder = widget.query.filtered ? '' : widget.query.folder;
    return switch (folder) {
      'trash' => ('restore', context.t('restore'), Icons.restore_from_trash, Colors.teal),
      'spam' => ('unspam', context.t('not_spam'), Icons.report_off, Colors.teal),
      'archived' => ('unarchive', context.t('unarchive'), Icons.move_to_inbox, Colors.teal),
      'drafts' => ('draft_delete', context.t('delete'), Icons.delete_outline, Colors.red),
      _ => app.store.swipeLeft == 'delete'
          ? ('delete', context.t('delete'), Icons.delete_outline, Colors.red)
          : ('archive', context.t('archive'), Icons.archive_outlined, const Color(0xFF2E7D32)),
    };
  }

  Future<void> _swipeLeft(Map<String, dynamic> row, String action) async {
    final index = items.indexOf(row);
    setState(() => items.remove(row));
    final pks = _pks(row);
    final tr = {for (final k in const ['deleted_done', 'archived_done', 'restored_done', 'unspam_done', 'unarchived_done', 'draft_deleted', 'undo']) k: context.t(k)};
    bool ok;
    String done;
    VoidCallback? undo;
    switch (action) {
      case 'archive':
        if (row['thread'] == null) {
          ok = await _bulk('delete', pks);
          done = tr['deleted_done']!;
          undo = () => _undo('restore', pks, row, index);
        } else {
          ok = await _bulk('archive', pks);
          done = tr['archived_done']!;
          undo = () => _undoUnarchive(row, index);
        }
      case 'delete':
        ok = await _bulk('delete', pks);
        done = tr['deleted_done']!;
        undo = () => _undo('restore', pks, row, index);
      case 'restore':
        ok = await _bulk('restore', pks);
        done = tr['restored_done']!;
      case 'unspam':
        ok = await _bulk('unspam', pks);
        done = tr['unspam_done']!;
      case 'unarchive':
        ok = await _unarchive(row);
        done = tr['unarchived_done']!;
      case 'draft_delete':
        ok = await _deleteDraft(row);
        done = tr['draft_deleted']!;
      default:
        return;
    }
    if (!mounted) return;
    if (!ok) {
      setState(() => items.insert(index.clamp(0, items.length), row));
      return;
    }
    snack(context, done, action: undo == null ? null : tr['undo'], onAction: undo);
  }

  Future<void> _undo(String action, List<int> pks, Map<String, dynamic> row, int index) async {
    if (await _bulk(action, pks) && mounted) setState(() => items.insert(index.clamp(0, items.length), row));
  }

  Future<bool> _unarchive(Map<String, dynamic> row) async {
    final t = row['thread'];
    if (t == null) return false;
    try {
      await api!.postJson('threads/$t/unarchive/');
      app.refreshCounts();
      return true;
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
      return false;
    }
  }

  Future<void> _undoUnarchive(Map<String, dynamic> row, int index) async {
    if (await _unarchive(row) && mounted) setState(() => items.insert(index.clamp(0, items.length), row));
  }

  Future<bool> _deleteDraft(Map<String, dynamic> row) async {
    try {
      await api!.postJson('drafts/${row['id']}/delete/');
      return true;
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
      return false;
    }
  }

  // ── вибір кількох ──

  Future<void> _selectionAction(String action) async {
    final rows = items.where((r) => selected.contains(r['id'] as int)).toList();
    final pks = rows.expand(_pks).toList();
    if (pks.isEmpty) return;
    if (action == 'spam' && !await confirm(context, context.t('spam_confirm'))) return;
    if (await _bulk(action, pks) && mounted) {
      setState(() {
        if (action == 'read' || action == 'unread') {
          for (final r in rows) {
            r['read'] = action == 'read';
            r['thread_unread'] = action == 'read' ? 0 : 1;
          }
        } else {
          items.removeWhere((r) => selected.contains(r['id']));
        }
        selected.clear();
      });
    }
  }

  Future<void> _open(Map<String, dynamic> row) async {
    if (selected.isNotEmpty) {
      setState(() => selected.contains(row['id']) ? selected.remove(row['id']) : selected.add(row['id'] as int));
      return;
    }
    if (draftsMode) {
      await Navigator.of(context).push(MaterialPageRoute(builder: (_) => ComposeScreen(draftId: row['id'] as int)));
      _load();
      return;
    }
    setState(() {
      row['read'] = true;
      row['thread_unread'] = 0;
    });
    final changed = await Navigator.of(context).push<bool>(
        MaterialPageRoute(builder: (_) => ThreadScreen(messageId: row['id'] as int, title: row['subject'] ?? '')));
    app.refreshCounts();
    if (changed == true) _load();
  }

  void _submitSearch(String value) {
    setState(() => q = value.trim());
    _load();
  }

  Future<void> _syncNow() async {
    snack(context, context.t('sync_running'), seconds: 2);
    try {
      await api!.postForm('sync/', {}, Api.longTimeout);
      if (!mounted) return;
      snack(context, context.t('sync_done'));
      _load();
      app.refreshCounts();
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
    }
  }

  String _title(BuildContext context) {
    if (widget.query.title.isNotEmpty) return widget.query.title;
    return context.t(widget.query.folder);
  }

  PreferredSizeWidget _appBar(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    if (selected.isNotEmpty) {
      return AppBar(
        backgroundColor: scheme.secondaryContainer,
        leading: IconButton(icon: const Icon(Icons.close), onPressed: () => setState(selected.clear)),
        title: Text('${selected.length}'),
        actions: [
          IconButton(
              tooltip: context.t('mark_read'), icon: const Icon(Icons.mark_email_read_outlined), onPressed: () => _selectionAction('read')),
          IconButton(
              tooltip: context.t('mark_unread'), icon: const Icon(Icons.mark_email_unread_outlined), onPressed: () => _selectionAction('unread')),
          IconButton(tooltip: context.t('archive'), icon: const Icon(Icons.archive_outlined), onPressed: () => _selectionAction('archive')),
          IconButton(tooltip: context.t('delete'), icon: const Icon(Icons.delete_outline), onPressed: () => _selectionAction('delete')),
          PopupMenuButton<String>(
            onSelected: _selectionAction,
            itemBuilder: (c) => [
              PopupMenuItem(value: 'spam', child: Text(c.t('to_spam'))),
              if (widget.query.folder == 'trash') PopupMenuItem(value: 'restore', child: Text(c.t('restore'))),
              if (widget.query.folder == 'spam') PopupMenuItem(value: 'unspam', child: Text(c.t('not_spam'))),
            ],
          ),
        ],
      );
    }
    if (searching) {
      return AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () {
            searchCtl.clear();
            setState(() {
              searching = false;
              q = '';
            });
            _load();
          },
        ),
        title: TextField(
          controller: searchCtl,
          autofocus: true,
          textInputAction: TextInputAction.search,
          decoration: InputDecoration(hintText: context.t('search_hint'), border: InputBorder.none),
          onSubmitted: _submitSearch,
        ),
        actions: [
          IconButton(icon: const Icon(Icons.search), onPressed: () => _submitSearch(searchCtl.text)),
        ],
      );
    }
    return AppBar(
      title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('${widget.query.icon.isEmpty ? '' : '${widget.query.icon} '}${_title(context)}',
            maxLines: 1, overflow: TextOverflow.ellipsis),
        if (app.accountEmail.isNotEmpty)
          Text(app.accountEmail, style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)),
      ]),
      actions: [
        IconButton(tooltip: context.t('search'), icon: const Icon(Icons.search), onPressed: () => setState(() => searching = true)),
        PopupMenuButton<String>(
          onSelected: (v) {
            if (v == 'sync') _syncNow();
            if (v == 'group') {
              app.setGrouped(!app.store.grouped);
              _load();
            }
          },
          itemBuilder: (c) => [
            PopupMenuItem(value: 'sync', child: ListTile(leading: const Icon(Icons.sync), title: Text(c.t('sync_now')))),
            CheckedPopupMenuItem(value: 'group', checked: app.store.grouped, child: Text(c.t('group_threads'))),
          ],
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    AppScope.of(context); // перебудувати, коли змінились налаштування
    final scheme = Theme.of(context).colorScheme;
    final left = _leftAction(context);
    return Scaffold(
      appBar: _appBar(context),
      body: Column(children: [
        if (widget.query.filtered && selected.isEmpty)
          Container(
            width: double.infinity,
            color: scheme.surfaceContainerHighest,
            padding: const EdgeInsets.fromLTRB(12, 4, 4, 4),
            child: Row(children: [
              const Icon(Icons.filter_alt_outlined, size: 18),
              const SizedBox(width: 6),
              Expanded(child: Text(context.t('filter_all_folders'), style: const TextStyle(fontSize: 13))),
              TextButton.icon(
                onPressed: widget.onClearFilter,
                icon: const Icon(Icons.close, size: 18),
                label: Text(context.t('clear_filter')),
              ),
            ]),
          ),
        if (q.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            child: Row(children: [
              Expanded(child: Text('${context.t('search_results')}: «$q»', style: const TextStyle(fontWeight: FontWeight.w600))),
            ]),
          ),
        if (fromCache && error.isNotEmpty)
          Material(
            color: scheme.tertiaryContainer,
            child: ListTile(
              dense: true,
              leading: const Icon(Icons.cloud_off),
              title: Text(context.t('shown_cached')),
              trailing: TextButton(onPressed: _load, child: Text(context.t('retry'))),
            ),
          ),
        if (loading && items.isNotEmpty) const LinearProgressIndicator(minHeight: 2),
        Expanded(child: _body(context, left)),
      ]),
    );
  }

  Widget _body(BuildContext context, (String, String, IconData, Color) left) {
    if (items.isEmpty) {
      if (loading) return const Center(child: CircularProgressIndicator());
      if (error.isNotEmpty) {
        return EmptyState(icon: Icons.cloud_off, text: error, action: context.t('retry'), onAction: _load);
      }
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          SizedBox(
            height: 360,
            child: EmptyState(icon: Icons.inbox_outlined, text: q.isEmpty ? context.t('empty_list') : context.t('nothing_found')),
          ),
        ]),
      );
    }
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.builder(
        controller: scroll,
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.only(bottom: 96),
        itemCount: items.length + (hasNext ? 1 : 0),
        itemBuilder: (context, i) {
          if (i >= items.length) {
            return const Padding(padding: EdgeInsets.all(20), child: Center(child: CircularProgressIndicator()));
          }
          final row = items[i];
          final tile = draftsMode
              ? DraftTile(row: row, onTap: () => _open(row))
              : MailTile(
                  row: row,
                  selected: selected.contains(row['id']),
                  onTap: () => _open(row),
                  onLongPress: () => setState(() => selected.add(row['id'] as int)),
                  onAvatarTap: () => setState(
                      () => selected.contains(row['id']) ? selected.remove(row['id']) : selected.add(row['id'] as int)),
                );
          if (selected.isNotEmpty) return tile;
          return Dismissible(
            key: ValueKey('row-${row['id']}'),
            direction: draftsMode ? DismissDirection.endToStart : DismissDirection.horizontal,
            background: _swipeBg(
                context,
                Alignment.centerLeft,
                row['read'] == true && (row['thread_unread'] ?? 0) == 0 ? Icons.mark_email_unread : Icons.mark_email_read,
                row['read'] == true && (row['thread_unread'] ?? 0) == 0 ? context.t('mark_unread') : context.t('mark_read'),
                Theme.of(context).colorScheme.primary),
            secondaryBackground: _swipeBg(context, Alignment.centerRight, left.$3, left.$2, left.$4),
            confirmDismiss: (dir) async {
              if (dir == DismissDirection.startToEnd) {
                _toggleRead(row);
                return false;
              }
              if (left.$1 == 'draft_delete') return confirm(context, context.t('draft_delete_confirm'));
              return true;
            },
            onDismissed: (_) => _swipeLeft(row, left.$1),
            child: tile,
          );
        },
      ),
    );
  }

  Widget _swipeBg(BuildContext context, Alignment align, IconData icon, String label, Color color) {
    final left = align == Alignment.centerLeft;
    return Container(
      color: color,
      alignment: align,
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (!left) Text(label, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
        if (!left) const SizedBox(width: 10),
        Icon(icon, color: Colors.white, size: 28),
        if (left) const SizedBox(width: 10),
        if (left) Text(label, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
      ]),
    );
  }
}

class MailTile extends StatelessWidget {
  const MailTile({super.key, required this.row, required this.onTap, this.onLongPress, this.onAvatarTap, this.selected = false});

  final Map<String, dynamic> row;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;
  final VoidCallback? onAvatarTap;
  final bool selected;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final unread = row['read'] != true || ((row['thread_unread'] ?? 0) as num) > 0;
    final own = row['own'] == true;
    final to = (row['to'] as List?)?.cast<String>() ?? [];
    final who = own ? '${context.t('to_prefix')} ${to.isEmpty ? '—' : to.first}' : (row['from'] as String? ?? '');
    final total = ((row['thread_total'] ?? 1) as num).toInt();
    final atts = (row['attachments'] as List?)?.cast<String>() ?? [];
    final cats = (row['categories'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final projects = (row['projects'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final customer = row['customer'] as Map<String, dynamic>?;
    final fu = row['followup'] as Map<String, dynamic>?;
    final importance = row['importance'] as String? ?? '';
    final bold = unread ? FontWeight.w700 : FontWeight.w400;
    final tags = <Widget>[
      if ((row['folder_tag'] as String? ?? '').isNotEmpty) TagChip(label: row['folder_tag'] as String),
      for (final c in cats) TagChip(label: c['name'] as String, icon: c['icon'] as String? ?? '', color: hexColor(c['color'] as String?, scheme.secondary)),
      if (customer != null) TagChip(label: customer['name'] as String? ?? '', icon: '👤'),
      for (final p in projects) TagChip(label: p['code'] as String? ?? '', icon: (p['icon'] as String?)?.isNotEmpty == true ? p['icon'] as String : '📁'),
      if (row['quote'] == true) TagChip(label: context.t('quote_tag'), icon: '📝', color: Colors.orange),
      if (fu != null) TagChip(label: fu['short'] as String? ?? '', color: fu['tone'] == 'bad' ? Colors.red : Colors.blueGrey),
      if (row['agent'] == true) const TagChip(label: '🤖', color: Colors.purple),
    ];
    return Material(
      color: selected ? scheme.secondaryContainer : (unread ? scheme.surfaceContainerLow : scheme.surface),
      child: InkWell(
        onTap: onTap,
        onLongPress: onLongPress,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 10, 14, 10),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            GestureDetector(
              onTap: onAvatarTap,
              child: SenderAvatar(
                  name: own && to.isNotEmpty ? to.first : (row['from'] as String? ?? '?'),
                  logo: own ? '' : row['logo'] as String? ?? '',
                  logoV: row['logo_v'] as String? ?? '',
                  selected: selected),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  if (unread)
                    Container(
                      width: 8,
                      height: 8,
                      margin: const EdgeInsets.only(right: 6),
                      decoration: BoxDecoration(color: scheme.primary, shape: BoxShape.circle),
                    ),
                  Expanded(
                    child: Text(total > 1 ? '$who ($total)' : who,
                        maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 15.5, fontWeight: bold)),
                  ),
                  if (importance == 'urgent') const Text('‼️ ') else if (importance == 'important') const Text('❗ '),
                  Text(fmtShortDate(context, row['date'] as String? ?? ''),
                      style: TextStyle(fontSize: 12.5, color: unread ? scheme.primary : scheme.onSurfaceVariant, fontWeight: bold)),
                ]),
                const SizedBox(height: 2),
                Row(children: [
                  Expanded(
                    child: Text((row['subject'] as String? ?? '').isEmpty ? context.t('no_subject') : row['subject'] as String,
                        maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 14.5, fontWeight: bold)),
                  ),
                  if (row['starred'] == true) const Icon(Icons.star, size: 18, color: Colors.amber),
                ]),
                const SizedBox(height: 2),
                Text(row['snippet'] as String? ?? '',
                    maxLines: 2, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 13.5, color: scheme.onSurfaceVariant)),
                if (atts.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Row(children: [
                      Icon(Icons.attach_file, size: 15, color: scheme.onSurfaceVariant),
                      Expanded(
                        child: Text(atts.join(', ') + ((row['att_more'] ?? 0) > 0 ? ' +${row['att_more']}' : ''),
                            maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 12.5, color: scheme.onSurfaceVariant)),
                      ),
                    ]),
                  ),
                if (tags.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Wrap(children: tags)),
              ]),
            ),
          ]),
        ),
      ),
    );
  }
}

class DraftTile extends StatelessWidget {
  const DraftTile({super.key, required this.row, required this.onTap});

  final Map<String, dynamic> row;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final to = (row['to'] as List?)?.cast<String>() ?? [];
    return ListTile(
      onTap: onTap,
      contentPadding: const EdgeInsets.fromLTRB(16, 6, 16, 6),
      leading: CircleAvatar(
          backgroundColor: scheme.tertiaryContainer,
          child: Text(row['agent'] == true ? '🤖' : '📝', style: const TextStyle(fontSize: 18))),
      title: Text('${context.t('to_prefix')} ${to.isEmpty ? '—' : to.join(', ')}', maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text((row['subject'] as String? ?? '').isEmpty ? context.t('no_subject') : row['subject'] as String,
            maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600)),
        Text(row['snippet'] as String? ?? '', maxLines: 2, overflow: TextOverflow.ellipsis),
      ]),
      trailing: Text(fmtShortDate(context, row['date'] as String? ?? ''), style: const TextStyle(fontSize: 12.5)),
    );
  }
}

/// Таймер для тихого оновлення списку, поки застосунок відкритий.
class Ticker {
  Ticker(this.every, this.onTick);
  final Duration every;
  final VoidCallback onTick;
  Timer? _t;
  void start() {
    _t?.cancel();
    _t = Timer.periodic(every, (_) => onTick());
  }

  void stop() => _t?.cancel();
}
