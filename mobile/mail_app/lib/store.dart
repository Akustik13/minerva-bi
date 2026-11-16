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

  static Future<Store> open() async {
    final prefs = await SharedPreferences.getInstance();
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
    final keep = {'theme', 'lang', 'grouped', 'swipe_left', 'signature', 'text_scale'};
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
