// Що телефон пам'ятає: адреса Minerva й ключ (ключ — у захищеному сховищі Android), вибір людини (тема, мова,
// групування, свайпи) і останній список кожної папки — щоб пошта відкривалась одразу, ще до відповіді сервера.
import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

class Store {
  Store._(this._prefs, this._secure, this.token);

  final SharedPreferences _prefs;
  final FlutterSecureStorage _secure;
  String token;

  static const _tokenKey = 'mail_token';

  /// reload — перечитати з диска (фонова перевірка пошти: значення міг змінити відкритий застосунок).
  static Future<Store> open({bool reload = false}) async {
    final prefs = await SharedPreferences.getInstance();
    if (reload) await prefs.reload();
    const secure = FlutterSecureStorage(aOptions: AndroidOptions(encryptedSharedPreferences: true));
    var token = '';
    try {
      token = await secure.read(key: _tokenKey) ?? '';
    } catch (_) {
      token = '';
    }
    return Store._(prefs, secure, token);
  }

  String get base => _prefs.getString('base') ?? '';
  bool get paired => token.isNotEmpty && base.isNotEmpty;

  Future<void> savePairing(String base, String token, Map<String, dynamic> info) async {
    this.token = token;
    await _secure.write(key: _tokenKey, value: token);
    await _prefs.setString('base', base);
    await _prefs.setString('pair_info', jsonEncode(info));
  }

  Future<void> forget() async {
    token = '';
    try {
      await _secure.delete(key: _tokenKey);
    } catch (_) {}
    final keep = {
      'theme', 'lang', 'grouped', 'swipe_left', 'signature', 'text_scale', //
      'notify_on', 'notify_every', 'notify_asked', 'ui_lang', 'speak_lang', 'write_lang',
    };
    for (final k in _prefs.getKeys().toList()) {
      if (!keep.contains(k)) await _prefs.remove(k);
    }
  }

  // ── вибір людини ──
  String get theme => _prefs.getString('theme') ?? 'system'; // system | light | dark
  set theme(String v) => _prefs.setString('theme', v);
  String get lang => _prefs.getString('lang') ?? 'system'; // system | uk | en | de
  set lang(String v) => _prefs.setString('lang', v);
  bool get grouped => _prefs.getBool('grouped') ?? true;
  set grouped(bool v) => _prefs.setBool('grouped', v);

  // ── 🔔 сповіщення (lib/notify.dart) ──
  bool get notifyOn => _prefs.getBool('notify_on') ?? true;
  set notifyOn(bool v) => _prefs.setBool('notify_on', v);
  int get notifyEvery => _prefs.getInt('notify_every') ?? 15; // хв: 15 | 30 | 60 | 120 (Android — не частіше 15)
  set notifyEvery(int v) => _prefs.setInt('notify_every', v);
  bool get notifyAsked => _prefs.getBool('notify_asked') ?? false; // дозвіл на сповіщення вже питали
  set notifyAsked(bool v) => _prefs.setBool('notify_asked', v);
  int get notifyLast => _prefs.getInt('notify_last') ?? 0; // найновіший лист, про який уже знаємо (pk)
  Future<void> setNotifyLast(int v) async {
    if (v > notifyLast) await _prefs.setInt('notify_last', v);
  }

  int get foregroundAt => _prefs.getInt('fg_at') ?? 0; // застосунок відкритий (мс) — фонова перевірка мовчить
  set foregroundAt(int v) => _prefs.setInt('fg_at', v);
  String get uiLang => _prefs.getString('ui_lang') ?? 'uk'; // мова написів — для сповіщень у фоні
  set uiLang(String v) => _prefs.setString('ui_lang', v);

  // ── 🎤 голосовий набір ──
  String get speakLang => _prefs.getString('speak_lang') ?? ''; // '' — мова застосунку
  set speakLang(String v) => _prefs.setString('speak_lang', v);
  String get writeLang => _prefs.getString('write_lang') ?? ''; // '' — тією ж мовою (без перекладу)
  set writeLang(String v) => _prefs.setString('write_lang', v);
  String get swipeLeft => _prefs.getString('swipe_left') ?? 'archive'; // archive | delete
  set swipeLeft(String v) => _prefs.setString('swipe_left', v);
  bool get signature => _prefs.getBool('signature') ?? true;
  set signature(bool v) => _prefs.setBool('signature', v);

  // ── кеш ──
  Map<String, dynamic>? readJson(String key) {
    final raw = _prefs.getString('cache:$key');
    if (raw == null) return null;
    try {
      final v = jsonDecode(raw);
      return v is Map<String, dynamic> ? v : null;
    } catch (_) {
      return null;
    }
  }

  Future<void> writeJson(String key, Map<String, dynamic> value) => _prefs.setString('cache:$key', jsonEncode(value));
}
