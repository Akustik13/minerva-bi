// «Написати»: новий лист, відповідь, відповідь усім, пересилання, чернетка. Надсилає скринька Minerva (той самий
// /email/send/), підпис — з Minerva, історія листування під відповіддю — як у скриньці. «💾 Зберегти» — чернетка,
// яку видно й на комп'ютері; «⏰ Надіслати пізніше» — черга відправки Minerva.
import 'dart:async';
import 'dart:convert';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../util.dart';

class ComposeScreen extends StatefulWidget {
  const ComposeScreen({super.key, this.mode = 'new', this.messageId, this.draftId, this.initialBody = ''});

  final String mode; // new | reply | all | forward
  final int? messageId;
  final int? draftId;
  final String initialBody;

  @override
  State<ComposeScreen> createState() => _ComposeScreenState();
}

class _ComposeScreenState extends State<ComposeScreen> {
  final to = TextEditingController();
  final cc = TextEditingController();
  final subject = TextEditingController();
  final body = TextEditingController();
  final List<LocalFile> files = [];
  final List<Map<String, dynamic>> refs = []; // {ref, name, size} — вкладення пересланого листа / чернетки
  int? replyTo;
  int? draftId;
  bool withQuote = false;
  String quote = '';
  bool showCc = false;
  bool signature = true;
  bool important = false;
  bool receipt = false;
  int remindDays = 0;
  bool loading = false;
  bool sending = false;
  bool dirty = false;
  String error = '';

  AppState get app => AppScope.read(context);
  Api get api => app.api!;

  @override
  void initState() {
    super.initState();
    draftId = widget.draftId;
    signature = AppScope.read(context).store.signature;
    final meta = AppScope.read(context).meta;
    if ((meta?['signature'] as Map?)?['auto'] == false) signature = false;
    body.text = widget.initialBody;
    _prefill();
    for (final c in [to, cc, subject, body]) {
      c.addListener(() => dirty = true);
    }
  }

  @override
  void dispose() {
    for (final c in [to, cc, subject, body]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _prefill() async {
    if (widget.draftId == null && (widget.mode == 'new' || widget.messageId == null)) return;
    setState(() => loading = true);
    try {
      if (widget.draftId != null) {
        final d = await api.get('drafts/${widget.draftId}/');
        to.text = ((d['to'] as List?) ?? []).join(', ');
        cc.text = ((d['cc'] as List?) ?? []).join(', ');
        subject.text = (d['subject'] ?? '').toString();
        body.text = (d['body'] ?? '').toString();
        replyTo = d['reply_to'] as int?;
        refs.addAll(((d['attachments'] as List?) ?? []).cast<Map<String, dynamic>>());
        final opts = (d['options'] as Map?) ?? {};
        important = opts['importance'] == 'important';
        receipt = opts['read_receipt'] == true;
        remindDays = (opts['remind_days'] as num?)?.toInt() ?? 0;
        // чернетка, у тексті якої підпис уже є (написана на комп'ютері), — без другого підпису
        final first = (_sig()['text'] ?? '').split('\n').map((l) => l.trim()).firstWhere((l) => l.isNotEmpty, orElse: () => '');
        if (first.isNotEmpty && body.text.contains(first)) signature = false;
      } else {
        final j = await api.get('messages/${widget.messageId}/compose/', query: {'mode': widget.mode});
        to.text = (j['to'] ?? '').toString();
        cc.text = (j['cc'] ?? '').toString();
        subject.text = (j['subject'] ?? '').toString();
        if (widget.initialBody.isEmpty) body.text = (j['body'] ?? '').toString();
        replyTo = j['reply_to'] as int?;
        withQuote = j['with_quote'] == true;
        quote = (j['quote'] ?? '').toString();
        refs.addAll(((j['attachments'] as List?) ?? []).cast<Map<String, dynamic>>());
      }
      showCc = cc.text.isNotEmpty;
      dirty = widget.initialBody.isNotEmpty;
    } catch (e) {
      if (mounted) error = errorText(context, e);
    } finally {
      if (mounted) setState(() => loading = false);
    }
  }

  Map<String, String> _sig() {
    final s = (app.meta?['signature'] as Map?) ?? {};
    return {'html': (s['html'] ?? '').toString(), 'text': (s['text'] ?? '').toString()};
  }

  String _escape(String s) => const HtmlEscape(HtmlEscapeMode.element).convert(s).replaceAll('\n', '<br>');

  /// Текст — без підпису: текстову частину листа Minerva підписує сама (smtp_client.SMTPClient.send), як із
  /// форми в браузері; HTML — з підписом, як його ставить форма «Написати».
  Map<String, String> _fields() {
    final text = body.text.trimRight();
    var html = '<div style="font-family:Arial,sans-serif;font-size:14px">${_escape(text)}</div>';
    final sig = _sig();
    if (signature && (sig['text']!.isNotEmpty || sig['html']!.isNotEmpty)) {
      html += '<br>${sig['html']!.isNotEmpty ? sig['html'] : _escape(sig['text']!)}';
    }
    return {
      'to': splitAddresses(to.text).join(', '),
      'cc': splitAddresses(cc.text).join(', '),
      'subject': subject.text.trim(),
      'body': text,
      'body_html': html,
      if (replyTo != null) 'reply_to_pk': '$replyTo',
      if (replyTo != null && withQuote) 'with_quote': '1',
      if (draftId != null) 'draft_pk': '$draftId',
      if (important) 'opt_important': '1',
      if (receipt) 'opt_receipt': '1',
      if (remindDays > 0) 'opt_remind': '1',
      if (remindDays > 0) 'remind_days': '$remindDays',
    };
  }

  Map<String, List<String>> get _repeated => {
        'att_ref': refs.map((r) => r['ref'].toString()).toList(),
        'att_ref_name': refs.map((r) => (r['name'] ?? '').toString()).toList(),
      };

  Future<void> _send({DateTime? at}) async {
    final f = _fields();
    if (f['to']!.isEmpty) {
      setState(() => error = context.t('need_recipient'));
      return;
    }
    if (f['subject']!.isEmpty && replyTo == null) {
      if (!await _confirmNoSubject()) return;
      f['subject'] = '—';
    }
    if (!mounted) return;
    setState(() {
      sending = true;
      error = '';
    });
    final failed = context.t('send_failed');
    try {
      final j = await api.postMultipart(at == null ? 'send/' : 'schedule/', {
        ...f,
        if (at != null) 'scheduled_at': at.toUtc().toIso8601String(),
      }, repeated: _repeated, files: files);
      if (j['ok'] != true) throw ApiException(400, (j['error'] ?? failed).toString());
      if (!mounted) return;
      dirty = false;
      Navigator.of(context).pop(true);
      snack(context, at == null ? context.t('sent_done') : '${context.t('scheduled_done')} ${fmtFullDate(context, at.toIso8601String())}');
      app.refreshCounts();
    } catch (e) {
      if (mounted) setState(() => error = errorText(context, e));
    } finally {
      if (mounted) setState(() => sending = false);
    }
  }

  Future<bool> _confirmNoSubject() async {
    final r = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        content: Text(c.t('no_subject_confirm')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(c.t('cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(c.t('send'))),
        ],
      ),
    );
    return r == true;
  }

  Future<bool> _saveDraft({bool quiet = false}) async {
    try {
      final j = await api.postMultipart('drafts/save/', _fields(), repeated: _repeated, files: files);
      if (j['ok'] != true) throw ApiException(400, (j['error'] ?? '').toString());
      draftId = j['draft_pk'] as int?;
      files.clear();
      refs
        ..clear()
        ..addAll(((j['attachments'] as List?) ?? []).cast<Map<String, dynamic>>());
      dirty = false;
      if (mounted) {
        setState(() {});
        if (!quiet) snack(context, '${context.t('draft_saved')} ${j['saved_at'] ?? ''}');
      }
      return true;
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
      return false;
    }
  }

  Future<void> _sendLater() async {
    final now = DateTime.now();
    final day = await showDatePicker(
        context: context, firstDate: now, lastDate: now.add(const Duration(days: 365)), initialDate: now);
    if (day == null || !mounted) return;
    final time = await showTimePicker(context: context, initialTime: TimeOfDay.fromDateTime(now.add(const Duration(hours: 1))));
    if (time == null) return;
    final at = DateTime(day.year, day.month, day.day, time.hour, time.minute);
    if (at.isBefore(DateTime.now())) {
      if (mounted) snack(context, context.t('time_in_past'));
      return;
    }
    await _send(at: at);
  }

  Future<void> _pickFiles() async {
    final r = await FilePicker.pickFiles(allowMultiple: true);
    if (r == null) return;
    setState(() {
      for (final f in r.files) {
        if (f.path != null) files.add(LocalFile(f.path!, f.name, f.size));
      }
      dirty = true;
    });
  }

  Future<void> _aiGenerate() async {
    final ctl = TextEditingController();
    final prompt = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text('✨ ${c.t('ai_generate')}'),
        content: TextField(
          controller: ctl,
          autofocus: true,
          minLines: 3,
          maxLines: 6,
          decoration: InputDecoration(hintText: c.t('ai_generate_hint'), border: const OutlineInputBorder()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: Text(c.t('cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, ctl.text.trim()), child: Text(c.t('ai_go'))),
        ],
      ),
    );
    if (prompt == null || prompt.isEmpty) return;
    setState(() => loading = true);
    try {
      final j = await api.postJson('ai-generate/', {'prompt': prompt}, Api.longTimeout);
      if (j['ok'] != true) throw ApiException(400, (j['error'] ?? '').toString());
      if (subject.text.trim().isEmpty) subject.text = (j['subject'] ?? '').toString();
      body.text = (j['body'] ?? '').toString();
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
    } finally {
      if (mounted) setState(() => loading = false);
    }
  }

  Future<void> _onPop() async {
    if (!dirty || sending) {
      if (mounted) Navigator.of(context).pop(false);
      return;
    }
    final choice = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        content: Text(c.t('leave_compose')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, 'discard'), child: Text(c.t('discard'))),
          TextButton(onPressed: () => Navigator.pop(c, 'stay'), child: Text(c.t('cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, 'save'), child: Text(c.t('save_draft'))),
        ],
      ),
    );
    if (!mounted) return;
    if (choice == 'discard') Navigator.of(context).pop(false);
    if (choice == 'save' && await _saveDraft(quiet: true) && mounted) {
      Navigator.of(context).pop(true);
      snack(context, context.t('draft_saved'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final title = switch (widget.mode) {
      'reply' => context.t('reply'),
      'all' => context.t('reply_all'),
      'forward' => context.t('forward'),
      _ => widget.draftId != null ? context.t('draft') : context.t('new_mail'),
    };
    final sig = _sig();
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _onPop();
      },
      child: Scaffold(
        appBar: AppBar(
          title: Text(title),
          actions: [
            IconButton(tooltip: context.t('attach'), icon: const Icon(Icons.attach_file), onPressed: sending ? null : _pickFiles),
            IconButton(
              tooltip: context.t('send'),
              icon: sending
                  ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.5))
                  : const Icon(Icons.send),
              onPressed: sending || loading ? null : () => _send(),
            ),
            PopupMenuButton<String>(
              onSelected: (v) {
                if (v == 'later') _sendLater();
                if (v == 'draft') _saveDraft();
                if (v == 'ai') _aiGenerate();
              },
              itemBuilder: (c) => [
                PopupMenuItem(value: 'later', child: ListTile(leading: const Icon(Icons.schedule_send), title: Text(c.t('send_later')))),
                PopupMenuItem(value: 'draft', child: ListTile(leading: const Icon(Icons.save_outlined), title: Text(c.t('save_draft')))),
                if (widget.mode == 'new' || widget.mode == 'forward')
                  PopupMenuItem(value: 'ai', child: ListTile(leading: const Icon(Icons.auto_awesome), title: Text(c.t('ai_generate')))),
              ],
            ),
          ],
        ),
        body: Column(children: [
          if (loading) const LinearProgressIndicator(minHeight: 2),
          Expanded(
            child: ListView(padding: const EdgeInsets.fromLTRB(16, 8, 16, 32), children: [
              _RecipientField(
                controller: to,
                label: context.t('to'),
                trailing: IconButton(
                  icon: Icon(showCc ? Icons.expand_less : Icons.expand_more),
                  tooltip: context.t('cc'),
                  onPressed: () => setState(() => showCc = !showCc),
                ),
              ),
              if (showCc) _RecipientField(controller: cc, label: context.t('cc')),
              TextField(
                controller: subject,
                textCapitalization: TextCapitalization.sentences,
                decoration: InputDecoration(labelText: context.t('subject')),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: body,
                minLines: 8,
                maxLines: null,
                keyboardType: TextInputType.multiline,
                textCapitalization: TextCapitalization.sentences,
                style: const TextStyle(fontSize: 16, height: 1.4),
                decoration: InputDecoration(hintText: context.t('body_hint'), border: InputBorder.none),
              ),
              if (sig['text']!.isNotEmpty || sig['html']!.isNotEmpty)
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  value: signature,
                  onChanged: (v) => setState(() => signature = v),
                  title: Text(context.t('add_signature')),
                  subtitle: Text(sig['text']!.replaceAll('\n', ' · '), maxLines: 2, overflow: TextOverflow.ellipsis),
                ),
              if (replyTo != null && quote.isNotEmpty)
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  value: withQuote,
                  onChanged: (v) => setState(() => withQuote = v),
                  title: Text(context.t('with_quote')),
                  subtitle: Text(quote.replaceAll(RegExp(r'\s+'), ' '), maxLines: 2, overflow: TextOverflow.ellipsis),
                ),
              if (files.isNotEmpty || refs.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text(context.t('attachments'), style: const TextStyle(fontWeight: FontWeight.w600)),
                Wrap(spacing: 6, runSpacing: 4, children: [
                  for (final r in refs)
                    InputChip(
                      avatar: const Icon(Icons.attach_file, size: 18),
                      label: Text('${r['name']}', overflow: TextOverflow.ellipsis),
                      onDeleted: () => setState(() {
                        refs.remove(r);
                        dirty = true;
                      }),
                    ),
                  for (final f in files)
                    InputChip(
                      avatar: const Icon(Icons.upload_file, size: 18),
                      label: Text('${f.name}  ${fmtSize(f.size)}', overflow: TextOverflow.ellipsis),
                      onDeleted: () => setState(() => files.remove(f)),
                    ),
                ]),
              ],
              const Divider(height: 28),
              Text(context.t('options'), style: const TextStyle(fontWeight: FontWeight.w600)),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                value: important,
                onChanged: (v) => setState(() => important = v),
                title: Text('❗ ${context.t('opt_important')}'),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                value: receipt,
                onChanged: (v) => setState(() => receipt = v),
                title: Text('📬 ${context.t('opt_receipt')}'),
              ),
              Row(children: [
                Expanded(child: Text('⏰ ${context.t('opt_remind')}')),
                DropdownButton<int>(
                  value: remindDays,
                  items: [
                    DropdownMenuItem(value: 0, child: Text(context.t('no'))),
                    for (final d in const [1, 2, 3, 5, 7, 14]) DropdownMenuItem(value: d, child: Text(context.tn('days_n', d))),
                  ],
                  onChanged: (v) => setState(() => remindDays = v ?? 0),
                ),
              ]),
              if (error.isNotEmpty)
                Padding(padding: const EdgeInsets.only(top: 12), child: Text(error, style: TextStyle(color: scheme.error))),
            ]),
          ),
        ]),
      ),
    );
  }
}

/// Поле адрес з підказками (адресна книга, клієнти CRM, з ким листувались — як у скриньці).
class _RecipientField extends StatefulWidget {
  const _RecipientField({required this.controller, required this.label, this.trailing});

  final TextEditingController controller;
  final String label;
  final Widget? trailing;

  @override
  State<_RecipientField> createState() => _RecipientFieldState();
}

class _RecipientFieldState extends State<_RecipientField> {
  List<Map<String, dynamic>> hints = [];
  Timer? debounce;
  final focus = FocusNode();

  @override
  void initState() {
    super.initState();
    focus.addListener(() {
      if (!focus.hasFocus && mounted) setState(() => hints = []);
    });
  }

  @override
  void dispose() {
    debounce?.cancel();
    focus.dispose();
    super.dispose();
  }

  String get _token {
    final parts = widget.controller.text.split(RegExp(r'[,;]'));
    return parts.isEmpty ? '' : parts.last.trim();
  }

  void _changed(String _) {
    debounce?.cancel();
    final q = _token;
    if (q.length < 2) {
      setState(() => hints = []);
      return;
    }
    debounce = Timer(const Duration(milliseconds: 300), () async {
      try {
        final j = await AppScope.read(context).api!.get('contacts/', query: {'q': q});
        if (!mounted || _token != q) return;
        setState(() => hints = ((j['contacts'] as List?) ?? []).cast<Map<String, dynamic>>().take(6).toList());
      } catch (_) {}
    });
  }

  void _pick(Map<String, dynamic> c) {
    final text = widget.controller.text;
    final cut = text.lastIndexOf(RegExp(r'[,;]'));
    final head = cut >= 0 ? '${text.substring(0, cut + 1).trimRight()} ' : '';
    widget.controller.text = '$head${c['email']}, ';
    widget.controller.selection = TextSelection.collapsed(offset: widget.controller.text.length);
    setState(() => hints = []);
  }

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      TextField(
        controller: widget.controller,
        focusNode: focus,
        keyboardType: TextInputType.emailAddress,
        autocorrect: false,
        onChanged: _changed,
        decoration: InputDecoration(labelText: widget.label, suffixIcon: widget.trailing),
      ),
      for (final c in hints)
        ListTile(
          dense: true,
          leading: const Icon(Icons.person_outline),
          title: Text((c['name'] ?? '').toString().isEmpty ? c['email'].toString() : c['name'].toString()),
          subtitle: Text('${c['email']}${(c['source'] ?? '').toString().isEmpty ? '' : ' · ${c['source']}'}'),
          onTap: () => _pick(c),
        ),
    ]);
  }
}
