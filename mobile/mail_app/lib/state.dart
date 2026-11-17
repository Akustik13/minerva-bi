// Стан застосунку: підключення до Minerva, дані скриньки (me/, nav/) і вибір людини. Екрани читають його через
// AppScope.of(context) і перебудовуються, коли щось змінилось.
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/material.dart';

import 'api.dart';
import 'notify.dart';
import 'store.dart';

class AppState extends ChangeNotifier {
  AppState(this.store) {
    if (store.paired) _makeApi();
    meta = store.readJson('meta');
    nav = store.readJson('nav');
  }

  final Store store;
  static final navigatorKey = GlobalKey<NavigatorState>();
  Api? api;
  Json? meta;
  Json? nav;
  bool lostPairing = false; // ключ відключили в Minerva — показати пояснення на екрані підключення

  void _makeApi() => api = Api(store.base, store.token, onUnauthorized: _onUnauthorized);

  void _onUnauthorized() {
    if (!store.paired) return;
    lostPairing = true;
    logout();
  }

  ThemeMode get themeMode => switch (store.theme) {
        'light' => ThemeMode.light,
        'dark' => ThemeMode.dark,
        _ => ThemeMode.system,
      };

  Locale? get locale => store.lang == 'system' ? null : Locale(store.lang);

  void setTheme(String v) {
    store.theme = v;
    notifyListeners();
  }

  void setLang(String v) {
    store.lang = v;
    notifyListeners();
  }

  void setGrouped(bool v) {
    store.grouped = v;
    notifyListeners();
  }

  void setSwipeLeft(String v) {
    store.swipeLeft = v;
    notifyListeners();
  }

  void setSignature(bool v) {
    store.signature = v;
    notifyListeners();
  }

  /// 🔔 Сповіщення про нові листи: увімкнули — Android 13+ питає дозвіл, розклад фонової перевірки.
  Future<bool> setNotify(bool on) async {
    store.notifyOn = on;
    notifyListeners();
    var allowed = true;
    try {
      if (on) allowed = await MailNotifier.requestPermission();
      await MailNotifier.schedule(store);
      if (!on) await MailNotifier.clear();
    } catch (_) {}
    notifyListeners();
    return allowed;
  }

  Future<void> setNotifyEvery(int minutes) async {
    store.notifyEvery = minutes;
    notifyListeners();
    try {
      await MailNotifier.schedule(store);
    } catch (_) {}
  }

  /// Головний екран відкрився (після підключення теж): розклад фонової перевірки, а дозвіл на сповіщення —
  /// питаємо лише раз (далі — перемикач у «Налаштуваннях»).
  Future<void> startNotify() async {
    if (!store.paired) return;
    try {
      if (!store.notifyAsked && store.notifyOn) {
        store.notifyAsked = true;
        await MailNotifier.requestPermission();
      }
      await MailNotifier.schedule(store);
    } catch (_) {}
  }

  static Future<String> deviceName() async {
    try {
      final info = await DeviceInfoPlugin().androidInfo;
      final brand = info.manufacturer.isEmpty ? '' : '${info.manufacturer[0].toUpperCase()}${info.manufacturer.substring(1)} ';
      return '$brand${info.model}'.trim();
    } catch (_) {
      return 'Android';
    }
  }

  Future<void> pair(String base, String code) async {
    final j = await Api.pair(base, code, await deviceName());
    await store.savePairing(base, j['token'] as String, {
      'user': j['user'],
      'account': j['account'],
    });
    lostPairing = false;
    _makeApi();
    notifyListeners();
    try {
      await refreshMeta();
    } catch (_) {}
  }

  Future<void> refreshMeta() async {
    final a = api;
    if (a == null) return;
    meta = await a.get('me/');
    await store.writeJson('meta', meta!);
    notifyListeners();
  }

  Future<void> refreshNav() async {
    final a = api;
    if (a == null) return;
    nav = await a.get('nav/');
    await store.writeJson('nav', nav!);
    notifyListeners();
  }

  /// Непрочитані папок після дії (прочитано, архів…) — без повного me/.
  Future<void> refreshCounts() async {
    final a = api;
    if (a == null || meta == null) return;
    try {
      final c = await a.get('counts/');
      // що людина вже бачить у відкритому застосунку, фонова перевірка не оголошує вдруге
      await store.setNotifyLast((c['latest'] as num?)?.toInt() ?? 0);
      final folders = (meta!['folders'] as List? ?? []).cast<Map<String, dynamic>>();
      for (final f in folders) {
        final key = f['key'];
        if (c.containsKey(key)) f['unread'] = c[key];
      }
      notifyListeners();
    } catch (_) {}
  }

  int unread(String folder) {
    for (final f in (meta?['folders'] as List? ?? [])) {
      if (f is Map && f['key'] == folder) return (f['unread'] as num?)?.toInt() ?? 0;
    }
    return 0;
  }

  bool perm(String key) => (meta?['perms'] as Map?)?[key] == true;

  String get accountEmail => ((meta?['account'] as Map?)?['email'] ?? '') as String;

  bool get updateAvailable {
    final latest = ((meta?['app'] as Map?)?['version'] ?? '') as String;
    return latest.isNotEmpty && _newer(latest, appVersion);
  }

  static bool _newer(String a, String b) {
    final pa = a.split('.').map((x) => int.tryParse(x) ?? 0).toList();
    final pb = b.split('.').map((x) => int.tryParse(x) ?? 0).toList();
    for (var i = 0; i < 3; i++) {
      final x = i < pa.length ? pa[i] : 0, y = i < pb.length ? pb[i] : 0;
      if (x != y) return x > y;
    }
    return false;
  }

  /// «Відключити цей телефон»: ключ гаситься й у Minerva (якщо є зв'язок), потім усе забувається в телефоні.
  Future<void> unpair() async {
    try {
      await api?.postJson('unpair/');
    } catch (_) {}
    await logout();
  }

  Future<void> logout() async {
    navigatorKey.currentState?.popUntil((r) => r.isFirst);
    try {
      await MailNotifier.stop(); // без ключа фонова перевірка нічого не дізнається — і сповіщення прибрати
    } catch (_) {}
    api?.close();
    api = null;
    meta = null;
    nav = null;
    await store.forget();
    notifyListeners();
  }
}

class AppScope extends InheritedNotifier<AppState> {
  const AppScope({super.key, required AppState state, required super.child}) : super(notifier: state);

  static AppState of(BuildContext context) => context.dependOnInheritedWidgetOfExactType<AppScope>()!.notifier!;

  static AppState read(BuildContext context) => context.getInheritedWidgetOfExactType<AppScope>()!.notifier!;
}
