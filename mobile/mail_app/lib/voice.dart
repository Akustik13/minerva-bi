// 🎤 Голосовий набір: розпізнає мову сам Android (служба розпізнавання Google на телефоні — як 🎤 на клавіатурі),
// текст іде туди, де курсор. Розділові знаки словами — як у Minerva (static/admin/js/mv_voice.js): «кома»,
// «крапка», «знак питання», «новий рядок»… Переклад надиктованого мовою листа — screens/voice_bar.dart.
import 'package:flutter/widgets.dart';
import 'package:speech_to_text/speech_recognition_error.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart';

/// Мови, якими можна говорити (розпізнавання) і писати (переклад — та сама функція Minerva, що «🌐» у пошті).
const speakLangs = ['uk', 'de', 'en', 'pl', 'ru'];
const writeLangs = ['uk', 'de', 'en', 'pl', 'fr', 'es', 'it', 'cs', 'ru'];
const _regions = {'uk': 'UA', 'de': 'DE', 'en': 'US', 'pl': 'PL', 'ru': 'RU'};

// слова → розділові знаки (ціле слово); «новий рядок» в однорядковому полі (тема) — пробіл
const _words = <String, List<(String, String)>>{
  'uk': [('новий абзац', '\n\n'), ('новий рядок', '\n'), ('знак питання', '?'), ('знак оклику', '!'),
         ('двокрапка', ':'), ('крапка з комою', ';'), ('кома', ','), ('крапка', '.')],
  'ru': [('новый абзац', '\n\n'), ('новая строка', '\n'), ('вопросительный знак', '?'), ('восклицательный знак', '!'),
         ('двоеточие', ':'), ('точка с запятой', ';'), ('запятая', ','), ('точка', '.')],
  'de': [('neuer absatz', '\n\n'), ('neue zeile', '\n'), ('fragezeichen', '?'), ('ausrufezeichen', '!'),
         ('doppelpunkt', ':'), ('semikolon', ';'), ('komma', ','), ('punkt', '.')],
  'en': [('new paragraph', '\n\n'), ('new line', '\n'), ('question mark', '?'), ('exclamation mark', '!'),
         ('colon', ':'), ('semicolon', ';'), ('comma', ','), ('full stop', '.'), ('period', '.')],
  'pl': [('nowy akapit', '\n\n'), ('nowa linia', '\n'), ('znak zapytania', '?'), ('wykrzyknik', '!'),
         ('dwukropek', ':'), ('średnik', ';'), ('przecinek', ','), ('kropka', '.')],
};

final _mark = RegExp(r'[.,;:?!]');
final _sentenceCap = RegExp(r'([.!?]\s+|\n)(\p{L})', unicode: true);
final _firstLetter = RegExp(r'^(\s*)(\p{L})', unicode: true);

/// Надиктоване → текст: слова-знаки мовою lang, зайві пробіли, велика літера після «.», «!», «?» і нового рядка.
String spokenPunctuation(String text, String lang, {bool single = false}) {
  for (final (word, sign) in _words[lang] ?? const <(String, String)>[]) {
    final mark = _mark.hasMatch(sign);
    final to = single && sign.contains('\n') ? ' ' : sign;
    // розпізнавання часом саме ставить знак і ще пише слово («день, кома, як») — знак поруч зі словом поглинається
    final re = RegExp(
      '[ \\t]*${mark ? '[.,;:?!]?[ \\t]*' : ''}(?<![\\p{L}\\p{N}])${RegExp.escape(word).replaceAll(' ', r'\s+')}'
      '(?![\\p{L}\\p{N}])[.,;:?!]?[ \\t]*',
      caseSensitive: false,
      unicode: true,
    );
    text = text.replaceAllMapped(re, (_) => mark ? '$to ' : to);
  }
  return text
      .replaceAll(RegExp(r'[ \t]+\n'), '\n')
      .replaceAll(RegExp(r'\n[ \t]+'), '\n')
      .replaceAll(RegExp(r'[ \t]{2,}'), ' ')
      .replaceAllMapped(_sentenceCap, (m) => '${m[1]}${m[2]!.toUpperCase()}')
      .replaceFirst(RegExp(r'^[ \t]+'), '')
      .replaceFirst(RegExp(r'[ \t]+$'), '');
}

/// Вставити текст там, де курсор (виділене — замінити): пробіл перед ним, велика літера на початку речення.
/// → (початок, кінець) вставленого — щоб «Оригінал» міг його замінити; порожній текст — null.
(int, int)? insertAtCursor(TextEditingController c, String text) {
  if (text.trim().isEmpty) return null;
  final value = c.value;
  final len = value.text.length;
  var start = value.selection.isValid ? value.selection.start : len;
  var end = value.selection.isValid ? value.selection.end : len;
  if (start > end) (start, end) = (end, start);
  start = start.clamp(0, len);
  end = end.clamp(0, len);
  final before = value.text.substring(0, start);
  final after = value.text.substring(end);
  if (before.trim().isEmpty || RegExp(r'[.!?]\s*$').hasMatch(before) || before.endsWith('\n')) {
    text = text.replaceFirstMapped(_firstLetter, (m) => '${m[1]}${m[2]!.toUpperCase()}');
  }
  if (before.isNotEmpty && !RegExp(r'\s$').hasMatch(before) && !RegExp(r'^[\s.,;:?!]').hasMatch(text)) {
    text = ' $text';
  }
  if (after.isNotEmpty && !RegExp(r'^[\s.,;:?!]').hasMatch(after) && !RegExp(r'\s$').hasMatch(text)) {
    text = '$text ';
  }
  c.value = TextEditingValue(
    text: before + text + after,
    selection: TextSelection.collapsed(offset: start + text.length),
  );
  return (start, start + text.length);
}

/// Замінити вставлене раніше (from) на інше (to), якщо його ще не змінили руками → true.
bool replaceInserted(TextEditingController c, (int, int) range, String from, String to) {
  final (s, e) = range;
  final text = c.text;
  if (e > text.length || text.substring(s, e) != from) return false;
  c.value = TextEditingValue(
    text: text.replaceRange(s, e, to),
    selection: TextSelection.collapsed(offset: s + to.length),
  );
  return true;
}

/// Розпізнавання: одна служба на застосунок (плагін — одиночка), тож слухачі йдуть до поточного Dictation.
class Dictation extends ChangeNotifier {
  Dictation({required this.onFinal, this.onDone, this.onError});

  /// Готовий шматок надиктованого (сирі слова).
  final void Function(String words) onFinal;

  /// Людина натиснула «стоп» і всі шматки вже прийшли.
  final VoidCallback? onDone;

  /// Ключ напису l10n: mic_denied | speech_unavailable | speech_lang_missing | speech_network.
  final void Function(String key)? onError;

  static final SpeechToText _stt = SpeechToText();
  static Dictation? _current;
  static List<LocaleName>? _locales;

  String lang = 'uk';
  bool active = false; // людина хоче, щоб слухало (між паузами розпізнавання перезапускається)
  bool listening = false;
  String partial = ''; // ще не готовий шматок — видно під полем
  double level = 0; // гучність — «чи чує мікрофон»
  int _restarts = 0;
  bool _finishing = false;
  bool _gotAny = false;
  bool _disposed = false;

  static const _maxRestarts = 60; // ~ кілька хвилин диктування поспіль

  Future<bool> _init() async {
    final ok = await _stt.initialize(
      onError: (e) => _current?._error(e),
      onStatus: (s) => _current?._status(s),
      options: [SpeechToText.androidNoBluetooth],
    );
    if (ok) _locales ??= await _stt.locales();
    return ok;
  }

  String _localeId(String code) {
    final all = _locales ?? const <LocaleName>[];
    String norm(String id) => id.replaceAll('_', '-').toLowerCase();
    final want = '$code-${_regions[code] ?? code.toUpperCase()}'.toLowerCase();
    for (final l in all) {
      if (norm(l.localeId) == want) return l.localeId;
    }
    for (final l in all) {
      if (norm(l.localeId).split('-').first == code) return l.localeId;
    }
    return '${code}_${_regions[code] ?? code.toUpperCase()}';
  }

  /// Почати слухати мовою lang → null або ключ помилки (немає дозволу, немає розпізнавання).
  Future<String?> start(String lang) async {
    this.lang = lang;
    _current = this;
    if (!await _init()) {
      return await _stt.hasPermission ? 'speech_unavailable' : 'mic_denied';
    }
    active = true;
    _restarts = 0;
    _finishing = false;
    _gotAny = false;
    partial = '';
    _notify();
    await _listen();
    return null;
  }

  Future<void> _listen() async {
    if (!active || _disposed) return;
    try {
      await _stt.listen(
        onResult: _result,
        onSoundLevelChange: (v) {
          level = v;
          _notify();
        },
        listenOptions: SpeechListenOptions(
          localeId: _localeId(lang),
          partialResults: true,
          listenMode: ListenMode.dictation,
          cancelOnError: false,
          listenFor: const Duration(minutes: 3),
          pauseFor: const Duration(seconds: 8),
        ),
      );
    } catch (_) {
      active = false;
      _notify();
    }
  }

  void _result(SpeechRecognitionResult r) {
    if (_current != this) return;
    if (r.finalResult) {
      partial = '';
      final words = r.recognizedWords.trim();
      if (words.isNotEmpty) {
        _gotAny = true;
        onFinal(words);
      }
    } else {
      partial = r.recognizedWords;
    }
    _notify();
  }

  void _status(String s) {
    listening = s == SpeechToText.listeningStatus;
    if (s == SpeechToText.doneStatus) {
      if (active && _restarts < _maxRestarts) {
        _restarts++;
        Future.delayed(const Duration(milliseconds: 250), _listen); // пауза в мовленні — слухаємо далі
      } else {
        _finish();
      }
    }
    _notify();
  }

  void _error(SpeechRecognitionError e) {
    final msg = e.errorMsg;
    const again = {'error_no_match', 'error_speech_timeout', 'error_busy', 'error_client', 'error_server_disconnected'};
    if (again.contains(msg)) {
      if (!active) _finish();
      return; // далі прийде «done» — і розпізнавання перезапуститься
    }
    active = false;
    final key = switch (msg) {
      'error_permission' || 'error_insufficient_permissions' => 'mic_denied',
      'error_network' || 'error_network_timeout' || 'error_server' => 'speech_network',
      'error_language_not_supported' || 'error_language_unavailable' => 'speech_lang_missing',
      _ => 'speech_unavailable',
    };
    onError?.call(key);
    _finish();
  }

  /// «Стоп»: дослухати останній шматок і віддати все (onDone).
  Future<void> stop() async {
    if (!active) return;
    active = false;
    _notify();
    try {
      await _stt.stop();
    } catch (_) {}
    // служба часом не повідомляє «done» після стоп — тоді завершуємо самі
    Future.delayed(const Duration(seconds: 3), _finish);
  }

  void _finish() {
    if (_finishing || active || _disposed) return;
    _finishing = true;
    if (partial.trim().isNotEmpty) {
      _gotAny = true;
      onFinal(partial.trim()); // остання фраза без «готово» від служби
    }
    partial = '';
    listening = false;
    _notify();
    onDone?.call();
  }

  bool get heardAnything => _gotAny;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    if (_current == this) {
      _current = null;
      if (active) _stt.cancel();
    }
    super.dispose();
  }
}
