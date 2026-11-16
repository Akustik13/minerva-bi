// ✨ AI для листа — ті самі функції, що в скриньці Minerva: AI-відповідь, «📋 Підготувати відповідь» (дані → бриф →
// лист), «❓ Запитати про лист», «🧭 Що обіцяли / зробили», переклад, «📜 Історія».
import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../util.dart';

Future<void> showAiSheet(BuildContext context, int messageId, {required void Function(String text) onUseReply}) {
  return showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    useSafeArea: true,
    builder: (c) => DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.75,
      minChildSize: 0.4,
      maxChildSize: 0.95,
      builder: (c, scroll) => AiSheet(messageId: messageId, scroll: scroll, onUseReply: onUseReply),
    ),
  );
}

class AiSheet extends StatefulWidget {
  const AiSheet({super.key, required this.messageId, required this.scroll, required this.onUseReply});

  final int messageId;
  final ScrollController scroll;
  final void Function(String text) onUseReply;

  @override
  State<AiSheet> createState() => _AiSheetState();
}

class _AiSheetState extends State<AiSheet> {
  String tool = '';
  bool busy = false;
  String error = '';
  final input = TextEditingController();
  String prepMode = 'letter';
  Map<String, dynamic>? result;
  Timer? poll;

  Api get api => AppScope.read(context).api!;
  String get base => 'messages/${widget.messageId}/';

  @override
  void dispose() {
    poll?.cancel();
    input.dispose();
    super.dispose();
  }

  void _pick(String t) {
    poll?.cancel();
    setState(() {
      tool = t;
      result = null;
      error = '';
      input.clear();
    });
    if (t == 'review') _run();
    if (t == 'history') _history(false);
    if (t == 'translate') _translate();
  }

  Future<void> _wrap(Future<void> Function() job) async {
    setState(() {
      busy = true;
      error = '';
    });
    try {
      await job();
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    } finally {
      if (mounted && poll == null) setState(() => busy = false);
    }
  }

  Future<void> _run() => _wrap(() async {
        switch (tool) {
          case 'reply':
            final j = await api.postForm('${base}ai-reply/', {'prompt': input.text.trim()}, Api.longTimeout);
            if (j['ok'] != true) throw ApiException(400, (j['error'] ?? '').toString());
            setState(() => result = j);
          case 'prepare':
            _startPoll(await api.postJson('${base}ai-prepare/', {'wish': input.text.trim(), 'mode': prepMode}));
          case 'ask':
            if (input.text.trim().isEmpty) return;
            _startPoll(await api.postJson('${base}ai-ask/', {'question': input.text.trim()}));
          case 'review':
            _startPoll(await api.postJson('${base}ai-review/'));
        }
      });

  void _startPoll(Map<String, dynamic> brief) {
    setState(() => result = brief);
    if (brief['status'] != 'running') return;
    final id = brief['id'];
    var waited = 0;
    poll = Timer.periodic(const Duration(seconds: 2), (t) async {
      waited += 2;
      try {
        final j = await api.get('briefs/$id/');
        if (!mounted) return t.cancel();
        setState(() => result = j);
        if (j['status'] != 'running' || waited > 300) {
          t.cancel();
          poll = null;
          setState(() => busy = false);
        }
      } catch (e) {
        if (waited > 300) {
          t.cancel();
          poll = null;
          if (mounted) setState(() => busy = false);
        }
      }
    });
  }

  Future<void> _translate() => _wrap(() async {
        final lang = Localizations.localeOf(context).languageCode;
        final j = await api.get('${base}translate/', query: {'lang': lang}, wait: Api.longTimeout);
        setState(() => result = j);
      });

  Future<void> _history(bool ai) => _wrap(() async {
        final j = await api.get('${base}history/', query: ai ? {'ai': '1'} : null, wait: Api.longTimeout);
        setState(() => result = j);
      });

  @override
  Widget build(BuildContext context) {
    return ListView(controller: widget.scroll, padding: const EdgeInsets.fromLTRB(16, 0, 16, 24), children: [
      Row(children: [
        if (tool.isNotEmpty)
          IconButton(
              icon: const Icon(Icons.arrow_back),
              onPressed: () {
                poll?.cancel();
                poll = null;
                setState(() {
                  tool = '';
                  busy = false;
                });
              }),
        Expanded(
          child: Text(tool.isEmpty ? '✨ ${context.t('ai_title')}' : context.t('ai_$tool'),
              style: Theme.of(context).textTheme.titleLarge),
        ),
      ]),
      const SizedBox(height: 8),
      if (tool.isEmpty) ..._menu(context) else ..._tool(context),
    ]);
  }

  List<Widget> _menu(BuildContext context) {
    Widget item(String key, String icon) => Card(
          child: ListTile(
            leading: Text(icon, style: const TextStyle(fontSize: 26)),
            title: Text(context.t('ai_$key'), style: const TextStyle(fontWeight: FontWeight.w600)),
            subtitle: Text(context.t('ai_${key}_hint')),
            onTap: () => _pick(key),
          ),
        );
    return [
      item('reply', '✍️'),
      item('prepare', '📋'),
      item('ask', '❓'),
      item('review', '🧭'),
      item('translate', '🌐'),
      item('history', '📜'),
    ];
  }

  List<Widget> _tool(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final out = <Widget>[];
    if (tool == 'reply' || tool == 'prepare' || tool == 'ask') {
      out.add(TextField(
        controller: input,
        minLines: 2,
        maxLines: 6,
        textInputAction: TextInputAction.newline,
        decoration: InputDecoration(
          border: const OutlineInputBorder(),
          labelText: context.t(tool == 'ask' ? 'ai_question' : 'ai_wish'),
          hintText: context.t(tool == 'ask' ? 'ai_question_hint' : 'ai_wish_hint'),
        ),
      ));
      if (tool == 'prepare') {
        out.add(Padding(
          padding: const EdgeInsets.only(top: 10),
          child: SegmentedButton<String>(
            segments: [
              ButtonSegment(value: 'data', label: Text(context.t('prep_data'))),
              ButtonSegment(value: 'brief', label: Text(context.t('prep_brief'))),
              ButtonSegment(value: 'letter', label: Text(context.t('prep_letter'))),
            ],
            selected: {prepMode},
            onSelectionChanged: (s) => setState(() => prepMode = s.first),
          ),
        ));
      }
      out.add(Padding(
        padding: const EdgeInsets.only(top: 12),
        child: SizedBox(
          height: 50,
          child: FilledButton.icon(
            onPressed: busy ? null : _run,
            icon: const Icon(Icons.auto_awesome),
            label: Text(context.t(tool == 'ask' ? 'ai_ask_go' : 'ai_go')),
          ),
        ),
      ));
    }
    if (tool == 'history' && result != null && result!['summary'] == null) {
      out.add(OutlinedButton.icon(
          onPressed: busy ? null : () => _history(true), icon: const Icon(Icons.auto_awesome), label: Text(context.t('history_ai'))));
    }
    if (busy) {
      out.add(const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator())));
    }
    if (error.isNotEmpty) {
      out.add(Padding(padding: const EdgeInsets.all(12), child: Text(error, style: TextStyle(color: scheme.error))));
    }
    final r = result;
    if (r != null) out.addAll(_result(context, r));
    return out;
  }

  List<Widget> _result(BuildContext context, Map<String, dynamic> r) {
    final scheme = Theme.of(context).colorScheme;
    final out = <Widget>[const SizedBox(height: 12)];
    Widget box(String text, {String? title}) => Card(
          color: scheme.surfaceContainerHigh,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (title != null) Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
              if (title != null) const SizedBox(height: 6),
              SelectableText(text, style: const TextStyle(fontSize: 15, height: 1.4)),
            ]),
          ),
        );
    switch (tool) {
      case 'reply':
        final reply = (r['reply'] ?? '') as String;
        out.add(box(reply));
        final used = ((r['used'] as List?) ?? []).cast<String>();
        if (used.isNotEmpty) {
          out.add(Text('${context.t('ai_used')}: ${used.join(' · ')}', style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)));
        }
        out.add(_useButton(context, reply));
      case 'prepare' || 'ask' || 'review':
        final stages = ((r['stages'] as List?) ?? []).cast<Map<String, dynamic>>();
        if (r['status'] == 'running' || tool == 'prepare') {
          for (final s in stages) {
            final icon = switch (s['status']) {
              'done' => '✅',
              'running' => '⏳',
              'failed' => '❌',
              'skipped' => '⏭',
              _ => '•',
            };
            out.add(Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Text('$icon ${s['label'] ?? ''}${(s['detail'] ?? '').toString().isEmpty ? '' : ' — ${s['detail']}'}',
                  style: const TextStyle(fontSize: 13)),
            ));
          }
        }
        if ((r['error'] ?? '').toString().isNotEmpty) {
          out.add(Text(r['error'].toString(), style: TextStyle(color: scheme.error)));
        }
        if (r['status'] == 'running') break;
        if (tool == 'review' && r['review'] is Map) {
          out.addAll(_review(context, (r['review'] as Map).cast<String, dynamic>(), box));
        } else if ((r['brief'] ?? '').toString().isNotEmpty) {
          out.add(box(r['brief'].toString(), title: tool == 'ask' ? context.t('ai_answer') : context.t('ai_brief')));
        }
        final draft = (r['draft'] ?? '').toString();
        if (tool == 'prepare' && draft.isNotEmpty) {
          out.add(box(draft, title: context.t('ai_draft')));
          out.add(_useButton(context, draft));
        }
      case 'translate':
        out.add(box((r['translation'] ?? '').toString()));
      case 'history':
        final state = ((r['state'] as List?) ?? []).map((e) => e.toString()).toList();
        if (r['summary'] != null) out.add(box(r['summary'].toString(), title: '🧠 ${context.t('history_ai')}'));
        if (state.isNotEmpty) out.add(box(state.join('\n')));
        for (final e in ((r['events'] as List?) ?? []).cast<Map<String, dynamic>>()) {
          out.add(ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Text(e['icon']?.toString() ?? '•', style: const TextStyle(fontSize: 18)),
            title: Text(e['text']?.toString() ?? ''),
            subtitle: Text(e['date']?.toString() ?? ''),
          ));
        }
    }
    return out;
  }

  List<Widget> _review(BuildContext context, Map<String, dynamic> v, Widget Function(String, {String? title}) box) {
    String line(Map i) => [
          i['status_icon'] ?? '',
          i['text'] ?? i['what'] ?? '',
          if ((i['who'] ?? '').toString().isNotEmpty) '(${i['who']})',
          i['date_fmt'] ?? '',
        ].where((x) => x.toString().isNotEmpty).join(' ');
    final out = <Widget>[];
    if ((v['summary'] ?? '').toString().isNotEmpty) out.add(box(v['summary'].toString()));
    for (final part in const ['promises', 'done', 'open_questions']) {
      final items = ((v[part] as List?) ?? []).cast<Map>();
      if (items.isEmpty) continue;
      out.add(box(items.map(line).join('\n'), title: context.t('review_$part')));
    }
    final next = v['next_action'];
    if (next is Map) {
      out.add(box('${next['text'] ?? next['what'] ?? ''} ${next['due_fmt'] ?? ''}'.trim(), title: context.t('review_next')));
    }
    return out;
  }

  Widget _useButton(BuildContext context, String text) => Padding(
        padding: const EdgeInsets.only(top: 8),
        child: SizedBox(
          height: 50,
          child: FilledButton.icon(
            onPressed: text.trim().isEmpty
                ? null
                : () {
                    Navigator.of(context).pop();
                    widget.onUseReply(text);
                  },
            icon: const Icon(Icons.reply),
            label: Text(context.t('ai_use')),
          ),
        ),
      );
}
