// 🎤 Панель під листом у «Написати»: «Надиктувати» (текст іде туди, де курсор — у тему або текст листа) і мови
// «Говорю → Писати». Мови різні — після «стоп» Minerva перекладає надиктоване (translate-text/, та сама функція,
// що «🌐» у веб-пошті) і вставляє переклад; «Оригінал» повертає сказане. 🌐 праворуч — перекласти весь текст.
import 'package:flutter/material.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../system.dart';
import '../util.dart';
import '../voice.dart';

/// Поле, куди йде надиктоване: контролер і чи однорядкове (тема — «новий рядок» стає пробілом).
typedef VoiceTarget = (TextEditingController, bool);

class VoiceBar extends StatefulWidget {
  const VoiceBar({super.key, required this.target, required this.replyLang, required this.onTranslateText});

  final VoiceTarget Function() target;
  final String replyLang; // мова листа, на який відповідаємо ('' — новий лист / не впізнали)
  final VoidCallback onTranslateText;

  @override
  State<VoiceBar> createState() => _VoiceBarState();
}

class _VoiceBarState extends State<VoiceBar> {
  late final Dictation dict = Dictation(onFinal: _onFinal, onDone: _onDone, onError: _onError);
  String speak = 'uk';
  String write = ''; // '' — тією ж мовою
  bool writeChosen = false; // людина сама вибрала «Писати» на цьому екрані
  final List<String> heard = []; // надиктоване, що чекає перекладу
  VoiceTarget? into; // поле, куди писати цей раз
  bool translating = false;

  AppState get app => AppScope.read(context);
  bool get translate => write.isNotEmpty && write != speak;

  @override
  void initState() {
    super.initState();
    dict.addListener(_changed);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final store = app.store;
    final ui = context.lang;
    speak = speakLangs.contains(store.speakLang) ? store.speakLang : (speakLangs.contains(ui) ? ui : 'uk');
    if (!writeChosen) _defaultWrite();
  }

  @override
  void didUpdateWidget(VoiceBar old) {
    super.didUpdateWidget(old);
    if (old.replyLang != widget.replyLang && !writeChosen) setState(_defaultWrite);
  }

  /// Відповідь — мовою листа, на який відповідаємо; новий лист — як вибрали востаннє.
  void _defaultWrite() {
    final w = widget.replyLang.isNotEmpty ? widget.replyLang : app.store.writeLang;
    write = writeLangs.contains(w) && w != speak ? w : '';
  }

  @override
  void dispose() {
    dict.removeListener(_changed);
    dict.dispose();
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  Future<void> _toggle() async {
    if (translating) return;
    if (dict.active) {
      await dict.stop();
      return;
    }
    into = widget.target();
    heard.clear();
    final err = await dict.start(speak);
    if (err != null && mounted) _onError(err);
  }

  void _onFinal(String words) {
    final t = into ?? widget.target();
    if (translate) {
      heard.add(words);
      setState(() {});
      return;
    }
    insertAtCursor(t.$1, spokenPunctuation(words, speak, single: t.$2));
  }

  Future<void> _onDone() async {
    if (!dict.heardAnything) {
      if (mounted) snack(context, context.t('speech_nothing'));
      return;
    }
    if (!translate || heard.isEmpty) return;
    final t = into ?? widget.target();
    final original = spokenPunctuation(heard.join(' '), speak, single: t.$2);
    heard.clear();
    final failed = context.t('translate_failed_original');
    final inserted = context.t('translated_inserted');
    final origLabel = context.t('voice_original');
    setState(() => translating = true);
    try {
      final j = await app.api!.postJson('translate-text/', {'text': original, 'lang': write}, Api.longTimeout);
      final translation = (j['translation'] ?? '').toString().trim();
      if (j['ok'] != true || translation.isEmpty) throw ApiException(400, (j['error'] ?? failed).toString());
      final text = t.$2 ? translation.replaceAll(RegExp(r'\s*\n\s*'), ' ') : translation;
      final range = insertAtCursor(t.$1, text);
      if (mounted && range != null) {
        final placed = t.$1.text.substring(range.$1, range.$2);
        snack(context, '$inserted · ${write.toUpperCase()}', action: origLabel, seconds: 8, onAction: () {
          replaceInserted(t.$1, range, placed, placed.replaceFirst(placed.trim(), original));
        });
      }
    } catch (e) {
      insertAtCursor(t.$1, original);
      if (mounted) snack(context, '$failed: ${errorText(context, e)}', seconds: 6);
    } finally {
      if (mounted) setState(() => translating = false);
    }
  }

  void _onError(String key) {
    if (!mounted) return;
    final denied = key == 'mic_denied';
    snack(context, context.t(key), seconds: 6, action: denied ? context.t('android_settings') : null,
        onAction: denied ? System.openAppSettings : null);
  }

  /// Мови діють одразу, щойно вибрали (без «OK» унизу довгого списку); закрити — потягнути вниз або «Назад».
  Future<void> _pickLangs() async {
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (c) => _LangSheet(
        speak: speak,
        write: write,
        replyLang: widget.replyLang,
        onChanged: (s, w) {
          setState(() {
            speak = s;
            write = w == s ? '' : w;
            writeChosen = true;
          });
          app.store.speakLang = speak;
          app.store.writeLang = write;
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final on = dict.active;
    final busy = on || translating || heard.isNotEmpty;
    final status = translating
        ? context.t('translating')
        : [
            ...heard,
            if (dict.partial.isNotEmpty) dict.partial,
          ].join(' ');
    final langs = translate ? '${speak.toUpperCase()} → ${write.toUpperCase()}' : speak.toUpperCase();
    return Material(
      color: scheme.surfaceContainer,
      elevation: 3,
      child: SafeArea(
        top: false,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          if (busy) LinearProgressIndicator(minHeight: 2, color: translating ? null : scheme.error),
          if (busy)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
              child: Row(children: [
                if (on)
                  AnimatedContainer(
                    duration: const Duration(milliseconds: 120),
                    width: 10 + (dict.level.clamp(0, 10) * 1.2),
                    height: 10 + (dict.level.clamp(0, 10) * 1.2),
                    decoration: BoxDecoration(color: scheme.error, shape: BoxShape.circle),
                  ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    status.isEmpty ? context.t('listening') : status,
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontStyle: status.isEmpty ? FontStyle.normal : FontStyle.italic, color: scheme.onSurfaceVariant),
                  ),
                ),
              ]),
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 6, 8, 6),
            child: Row(children: [
              FilledButton.icon(
                style: FilledButton.styleFrom(
                  minimumSize: const Size(0, 48),
                  backgroundColor: on ? scheme.error : null,
                  foregroundColor: on ? scheme.onError : null,
                ),
                onPressed: translating ? null : _toggle,
                icon: Icon(on ? Icons.stop_rounded : Icons.mic_none_rounded),
                label: Text(on ? context.t('voice_stop') : context.t('dictate')),
              ),
              const SizedBox(width: 8),
              Tooltip(
                message: context.t('voice_langs'),
                child: ActionChip(
                  avatar: Icon(translate ? Icons.translate : Icons.record_voice_over_outlined, size: 18),
                  label: Text(langs),
                  onPressed: on || translating ? null : _pickLangs,
                ),
              ),
              const Spacer(),
              IconButton(
                tooltip: context.t('translate_text'),
                icon: const Icon(Icons.g_translate_outlined),
                onPressed: on || translating ? null : widget.onTranslateText,
              ),
            ]),
          ),
        ]),
      ),
    );
  }
}

class _LangSheet extends StatefulWidget {
  const _LangSheet({required this.speak, required this.write, required this.replyLang, required this.onChanged});

  final String speak;
  final String write;
  final String replyLang;
  final void Function(String speak, String write) onChanged;

  @override
  State<_LangSheet> createState() => _LangSheetState();
}

class _LangSheetState extends State<_LangSheet> {
  late String speak = widget.speak;
  late String write = widget.write;

  void _set({String? s, String? w}) {
    setState(() {
      speak = s ?? speak;
      write = w ?? write;
      if (write == speak) write = '';
    });
    widget.onChanged(speak, write.isEmpty ? speak : write);
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    Widget head(String text) => Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 16, 4),
          child: Text(text.toUpperCase(),
              style: TextStyle(fontSize: 12, letterSpacing: 0.8, fontWeight: FontWeight.w700, color: scheme.primary)),
        );
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: MediaQuery.of(context).size.height * 0.85),
        child: ListView(shrinkWrap: true, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
            child: Text(context.t('voice_langs'), style: Theme.of(context).textTheme.titleMedium),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
            child: Text(context.t('voice_langs_hint'), style: TextStyle(color: scheme.onSurfaceVariant)),
          ),
          head('🎤 ${context.t('voice_speak')}'),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Wrap(spacing: 8, runSpacing: 4, children: [
              for (final l in speakLangs)
                ChoiceChip(
                  label: Text(context.t('lang_$l')),
                  selected: speak == l,
                  onSelected: (_) => _set(s: l),
                ),
            ]),
          ),
          head('✍️ ${context.t('voice_write')}'),
          // список не змінюється з мовою «Говорю» (інакше рядки зсуваються під пальцем); та сама мова = без перекладу
          for (final l in ['', ...writeLangs])
            RadioListTile<String>(
              value: l,
              groupValue: write.isEmpty || write == speak ? '' : write,
              onChanged: (v) => _set(w: v ?? ''),
              title: Text(l.isEmpty ? context.t('voice_same') : context.t('lang_$l')),
              subtitle: l.isNotEmpty && l == widget.replyLang ? Text(context.t('voice_reply_lang')) : null,
            ),
          const SizedBox(height: 16),
        ]),
      ),
    );
  }
}

/// «Перекласти на…» — мова для перекладу всього тексту листа (або виділеного).
Future<String?> pickTranslateLang(BuildContext context, String current) => showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (c) => SafeArea(
        child: ListView(shrinkWrap: true, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
            child: Text(c.t('translate_to'), style: Theme.of(c).textTheme.titleMedium),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
            child: Text(c.t('translate_selection_hint'), style: TextStyle(color: Theme.of(c).colorScheme.onSurfaceVariant)),
          ),
          for (final l in writeLangs)
            ListTile(
              leading: Icon(l == current ? Icons.radio_button_checked : Icons.radio_button_unchecked),
              title: Text(c.t('lang_$l')),
              onTap: () => Navigator.pop(c, l),
            ),
        ]),
      ),
    );
