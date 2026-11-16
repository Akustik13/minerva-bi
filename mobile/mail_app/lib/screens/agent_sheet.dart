// 🤖 «Доручити агенту» з листа — те саме вікно, що в скриньці Minerva (agent/views.py): на такі листи відповідай
// так / пересилай / позначай важливими / пиши клієнту замовлення / завдання агенту. «🧪 Спробувати» нічого не
// зберігає й не надсилає; «💾 Зберегти» — доручення (і чернетка для цього листа, якщо спробували).
import 'package:flutter/material.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../util.dart';

Future<void> showAgentSheet(BuildContext context, int messageId) {
  return showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    useSafeArea: true,
    builder: (c) => DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.9,
      minChildSize: 0.5,
      maxChildSize: 0.95,
      builder: (c, scroll) => AgentSheet(messageId: messageId, scroll: scroll),
    ),
  );
}

class AgentSheet extends StatefulWidget {
  const AgentSheet({super.key, required this.messageId, required this.scroll});

  final int messageId;
  final ScrollController scroll;

  @override
  State<AgentSheet> createState() => _AgentSheetState();
}

class _AgentSheetState extends State<AgentSheet> {
  Map<String, dynamic>? info;
  String error = '';
  bool busy = false;
  String kind = 'mail_reply';
  String mode = 'draft';
  String senderScope = 'sender';
  String? category;
  final name = TextEditingController();
  final senders = TextEditingController();
  final subject = TextEditingController();
  final body = TextEditingController();
  final instruction = TextEditingController();
  final forwardTo = TextEditingController();
  final cc = TextEditingController();
  Map<String, dynamic>? tried;

  Api get api => AppScope.read(context).api!;
  String get path => 'messages/${widget.messageId}/agent/';

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    for (final c in [name, senders, subject, body, instruction, forwardTo, cc]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final j = await api.get(path);
      if (!mounted) return;
      setState(() {
        info = j;
        name.text = (j['name'] ?? '').toString();
        senders.text = (j['sender'] ?? '').toString();
        subject.text = (j['subject'] ?? '').toString();
        category = j['category']?.toString();
      });
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    }
  }

  Map<String, dynamic> _data() => {
        'kind': kind,
        'name': name.text.trim(),
        'senders': senders.text.trim(),
        'subject_words': subject.text.trim(),
        'body_words': body.text.trim(),
        'instruction': instruction.text.trim(),
        'mode': mode,
        'category': category ?? '',
        'forward_to': forwardTo.text.trim(),
        'cc': cc.text.trim(),
      };

  Future<void> _try() async {
    setState(() {
      busy = true;
      error = '';
      tried = null;
    });
    try {
      final j = await api.postJson('${path}try/', _data(), Api.longTimeout);
      if (j['ok'] != true) throw ApiException(400, (j['error'] ?? '').toString());
      if (mounted) setState(() => tried = j);
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> _save() async {
    setState(() {
      busy = true;
      error = '';
    });
    final reply = (tried?['reply'] ?? '').toString();
    try {
      final j = await api.postJson('${path}save/', {
        ..._data(),
        if (reply.isNotEmpty && tried?['skip'] != true) 'draft_now': true,
        if (reply.isNotEmpty) 'reply': reply,
        if ((tried?['subject'] ?? '').toString().isNotEmpty) 'subject': tried!['subject'],
      });
      if (j['ok'] != true) throw ApiException(400, (j['error'] ?? '').toString());
      if (!mounted) return;
      Navigator.of(context).pop();
      snack(context, j['draft'] != null ? context.t('agent_saved_draft') : context.t('agent_saved'));
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  void _scope(String s) {
    final i = info;
    if (i == null) return;
    setState(() {
      senderScope = s;
      senders.text = switch (s) {
        'domain' => (i['domain'] ?? '').toString(),
        'none' => '',
        _ => (i['sender'] ?? '').toString(),
      };
    });
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final i = info;
    final app = AppScope.of(context);
    final kinds = ((i?['kinds'] as List?) ?? []).cast<Map<String, dynamic>>()
        .where((k) => k['value'] != 'mail_task' || i?['can_task'] == true)
        .toList();
    final rules = ((i?['rules'] as List?) ?? []).cast<Map<String, dynamic>>();
    final cats = ((i?['categories'] as List?) ?? []).cast<Map<String, dynamic>>();
    final needsInstruction = kind != 'mail_mark' && kind != 'mail_forward';
    InputDecoration dec(String label, [String? hint]) =>
        InputDecoration(labelText: label, hintText: hint, border: const OutlineInputBorder(), isDense: true);
    return ListView(controller: widget.scroll, padding: const EdgeInsets.fromLTRB(16, 0, 16, 32), children: [
      Text('🤖 ${context.t('agent_title')}', style: Theme.of(context).textTheme.titleLarge),
      const SizedBox(height: 4),
      Text(context.t('agent_lead'), style: TextStyle(color: scheme.onSurfaceVariant)),
      const SizedBox(height: 12),
      if (i == null && error.isEmpty) const Center(child: Padding(padding: EdgeInsets.all(24), child: CircularProgressIndicator())),
      if (i != null) ...[
        if (rules.isNotEmpty)
          Card(
            color: scheme.secondaryContainer,
            child: Padding(
              padding: const EdgeInsets.all(10),
              child: Text('${context.t('agent_rules_exist')}\n${rules.map((r) => '• ${r['name']} — ${r['mode']}').join('\n')}'),
            ),
          ),
        if ((i['blocked'] ?? '').toString().isNotEmpty && kind == 'mail_reply')
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text('ℹ️ ${i['blocked']}', style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 13)),
          ),
        DropdownButtonFormField<String>(
          value: kind,
          isExpanded: true,
          decoration: dec(context.t('agent_kind')),
          items: [
            for (final k in kinds)
              DropdownMenuItem(value: k['value'] as String, child: Text(k['label'] as String, overflow: TextOverflow.ellipsis)),
          ],
          onChanged: (v) => setState(() {
            kind = v ?? 'mail_reply';
            tried = null;
          }),
        ),
        const SizedBox(height: 12),
        TextField(controller: name, decoration: dec(context.t('agent_name'))),
        const SizedBox(height: 16),
        Text(context.t('agent_when'), style: const TextStyle(fontWeight: FontWeight.w700)),
        const SizedBox(height: 6),
        Wrap(spacing: 6, children: [
          ChoiceChip(label: Text(context.t('agent_this_sender')), selected: senderScope == 'sender', onSelected: (_) => _scope('sender')),
          if ((i['domain'] ?? '').toString().isNotEmpty)
            ChoiceChip(label: Text(i['domain'].toString()), selected: senderScope == 'domain', onSelected: (_) => _scope('domain')),
          ChoiceChip(label: Text(context.t('agent_any_sender')), selected: senderScope == 'none', onSelected: (_) => _scope('none')),
        ]),
        const SizedBox(height: 8),
        TextField(controller: senders, decoration: dec(context.t('agent_senders'))),
        const SizedBox(height: 8),
        TextField(controller: subject, decoration: dec(context.t('agent_subject'))),
        const SizedBox(height: 8),
        TextField(controller: body, decoration: dec(context.t('agent_body'))),
        const SizedBox(height: 8),
        DropdownButtonFormField<String>(
          value: category != null && cats.any((c) => '${c['id']}' == category) ? category : '',
          isExpanded: true,
          decoration: dec(context.t('category')),
          items: [
            DropdownMenuItem(value: '', child: Text(context.t('agent_any_category'))),
            for (final c in cats) DropdownMenuItem(value: '${c['id']}', child: Text(c['name'] as String)),
          ],
          onChanged: (v) => setState(() => category = (v ?? '').isEmpty ? null : v),
        ),
        const SizedBox(height: 16),
        Text(context.t('agent_what'), style: const TextStyle(fontWeight: FontWeight.w700)),
        const SizedBox(height: 6),
        if (kind == 'mail_forward') ...[
          TextField(controller: forwardTo, keyboardType: TextInputType.emailAddress, decoration: dec(context.t('agent_forward_to'))),
          const SizedBox(height: 8),
        ],
        if (needsInstruction || kind == 'mail_forward')
          TextField(
            controller: instruction,
            minLines: 3,
            maxLines: 8,
            decoration: dec(context.t(kind == 'mail_forward' ? 'agent_forward_note' : 'agent_instruction'), context.t('agent_instruction_hint')),
          ),
        if (kind == 'mail_reply' || kind == 'mail_customer' || kind == 'mail_forward') ...[
          const SizedBox(height: 8),
          TextField(controller: cc, keyboardType: TextInputType.emailAddress, decoration: dec(context.t('cc'))),
          const SizedBox(height: 12),
          SegmentedButton<String>(
            segments: [
              ButtonSegment(value: 'draft', label: Text(context.t('agent_mode_draft'))),
              ButtonSegment(value: 'send', label: Text(context.t('agent_mode_send')), enabled: app.perm('agent_send')),
            ],
            selected: {mode},
            onSelectionChanged: (s) => setState(() => mode = s.first),
          ),
        ],
        const SizedBox(height: 16),
        Row(children: [
          if (kind != 'mail_task')
            Expanded(
              child: OutlinedButton.icon(
                onPressed: busy ? null : _try,
                icon: const Text('🧪'),
                label: Text(context.t('agent_try')),
              ),
            ),
          if (kind != 'mail_task') const SizedBox(width: 10),
          Expanded(
            child: FilledButton.icon(
              onPressed: busy ? null : _save,
              icon: const Icon(Icons.save_outlined),
              label: Text(context.t('agent_save')),
            ),
          ),
        ]),
        if (busy) const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator())),
        if (tried != null) _triedCard(context, tried!),
      ],
      if (error.isNotEmpty) Padding(padding: const EdgeInsets.all(8), child: Text(error, style: TextStyle(color: scheme.error))),
    ]);
  }

  Widget _triedCard(BuildContext context, Map<String, dynamic> t) {
    final scheme = Theme.of(context).colorScheme;
    final lines = <String>[
      if (t['matches'] == false) '⚠️ ${context.t('agent_not_matching')} ${t['why'] ?? ''}',
      if ((t['blocked'] ?? '').toString().isNotEmpty) 'ℹ️ ${t['blocked']}',
      if (t['skip'] == true) '↷ ${context.t('agent_would_skip')}: ${t['skip_reason'] ?? ''}',
      if ((t['to'] ?? '').toString().isNotEmpty) '✉️ ${t['to']}',
      if ((t['doubts'] ?? '').toString().isNotEmpty) '🤔 ${t['doubts']}',
    ];
    return Card(
      margin: const EdgeInsets.only(top: 12),
      color: scheme.surfaceContainerHigh,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('🧪 ${context.t('agent_try_result')}', style: const TextStyle(fontWeight: FontWeight.w700)),
          for (final l in lines) Padding(padding: const EdgeInsets.only(top: 4), child: Text(l)),
          if ((t['subject'] ?? '').toString().isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 6), child: Text(t['subject'].toString(), style: const TextStyle(fontWeight: FontWeight.w600))),
          if ((t['reply'] ?? '').toString().isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 6), child: SelectableText(t['reply'].toString())),
        ]),
      ),
    );
  }
}
