// 🔔 Нові листи без відкритого застосунку: Android сам будить застосунок (WorkManager, не частіше ніж раз на 15 хв),
// фонова перевірка питає Minerva «що нового після листа N» (counts/?new_after=, той самий список, що сповіщення
// програми для Windows) і показує сповіщення «від кого — тема». Поки сповіщення не прибрано — крапка (або число,
// залежить від телефона) на іконці. Firebase не потрібен; ціна — затримка до інтервалу перевірки.
import 'package:flutter/widgets.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:workmanager/workmanager.dart';

import 'api.dart';
import 'l10n.dart';
import 'store.dart';

const mailCheckTask = 'minerva-mail-check';
const _channel = 'new_mail';
const _group = 'com.minervabi.mail.NEW';
const _summaryId = 0;
const _maxShown = 5; // окремих сповіщень за раз (решта — «+ ще N» у зведенні)
const _foregroundGrace = Duration(minutes: 3); // застосунок відкритий — список і так оновлюється
const intervals = [15, 30, 60, 120];

/// Що робити, коли людина торкнулась сповіщення: pk листа або null (зведення — «Вхідні»).
typedef OpenMail = void Function(int? messageId);

final FlutterLocalNotificationsPlugin _plugin = FlutterLocalNotificationsPlugin();
bool _ready = false;

/// Точка входу фонової перевірки — окремий Dart-ізолят без екранів (WorkManager).
@pragma('vm:entry-point')
void mailCheckDispatcher() {
  Workmanager().executeTask((task, input) async {
    WidgetsFlutterBinding.ensureInitialized();
    try {
      await MailNotifier.check();
    } catch (_) {
      // наступна перевірка — за розкладом; повтор одразу лише розрядив би батарею
    }
    return true;
  });
}

class MailNotifier {
  static Future<void> init({OpenMail? onOpen}) async {
    if (_ready) return;
    await _plugin.initialize(
      settings: const InitializationSettings(android: AndroidInitializationSettings('ic_stat_mail')),
      onDidReceiveNotificationResponse: onOpen == null ? null : (r) => onOpen(int.tryParse(r.payload ?? '')),
    );
    _ready = true;
  }

  /// Застосунок відкрили торканням сповіщення → (так, pk листа або null).
  static Future<(bool, int?)> launchedFrom() async {
    final d = await _plugin.getNotificationAppLaunchDetails();
    if (d == null || !d.didNotificationLaunchApp) return (false, null);
    return (true, int.tryParse(d.notificationResponse?.payload ?? ''));
  }

  static AndroidFlutterLocalNotificationsPlugin? get _android =>
      _plugin.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>();

  /// Android 13+ питає дозвіл на сповіщення (один раз; далі — лише в налаштуваннях Android).
  static Future<bool> requestPermission() async => await _android?.requestNotificationsPermission() ?? true;

  static Future<bool> allowed() async => await _android?.areNotificationsEnabled() ?? true;

  /// Розклад фонової перевірки за вибором людини (вимкнено / не підключено — розкладу немає).
  static Future<void> schedule(Store store) async {
    if (!store.paired || !store.notifyOn) {
      await Workmanager().cancelByUniqueName(mailCheckTask);
      return;
    }
    await Workmanager().registerPeriodicTask(
      mailCheckTask,
      mailCheckTask,
      frequency: Duration(minutes: store.notifyEvery),
      constraints: Constraints(networkType: NetworkType.connected),
      existingWorkPolicy: ExistingPeriodicWorkPolicy.update,
    );
  }

  static Future<void> stop() async {
    await Workmanager().cancelByUniqueName(mailCheckTask);
    await clear();
  }

  /// Прибрати сповіщення (і крапку на іконці) — людина відкрила пошту.
  static Future<void> clear() async {
    try {
      await _plugin.cancelAll();
    } catch (_) {}
  }

  /// Одна перевірка: фонова (WorkManager) або «Перевірити зараз» (force — навіть коли застосунок відкритий).
  /// → скільки нових листів показано (-1 — сповіщення вимкнені / не підключено).
  static Future<int> check({bool force = false}) async {
    final store = await Store.open(reload: true);
    if (!store.paired || (!store.notifyOn && !force)) return -1;
    await init();
    final api = Api(store.base, store.token);
    try {
      final last = store.notifyLast;
      final j = await api.get('counts/', query: {'new_after': '$last'});
      final latest = (j['latest'] as num?)?.toInt() ?? 0;
      final inbox = (j['inbox'] as num?)?.toInt() ?? 0;
      final fresh = ((j['new'] as List?) ?? const []).whereType<Map>().toList();
      await store.setNotifyLast(latest);
      if (inbox == 0) {
        await clear(); // усе прочитано (на комп'ютері теж) — крапка з іконки зникає
        return 0;
      }
      if (last == 0) return 0; // перша перевірка — лише запам'ятати, звідки рахувати нове
      final open = DateTime.now().millisecondsSinceEpoch - store.foregroundAt < _foregroundGrace.inMilliseconds;
      if (fresh.isEmpty || (open && !force)) return 0;
      await _show(fresh, inbox, store.uiLang);
      return fresh.length;
    } on ApiException catch (e) {
      if (e.unauthorized) await Workmanager().cancelByUniqueName(mailCheckTask); // телефон відключили в Minerva
      rethrow;
    } finally {
      api.close();
    }
  }

  static Future<void> _show(List<Map> fresh, int inbox, String lang) async {
    final channel = tr(lang, 'notif_channel');
    final desc = tr(lang, 'notif_channel_desc');
    AndroidNotificationDetails details({bool summary = false, StyleInformation? style, int? number}) =>
        AndroidNotificationDetails(
          _channel,
          channel,
          channelDescription: desc,
          importance: Importance.high,
          priority: Priority.high,
          category: AndroidNotificationCategory.email,
          icon: 'ic_stat_mail',
          color: const Color(0xFF2D6CDF),
          groupKey: _group,
          setAsGroupSummary: summary,
          // звук — один на перевірку (зведення), а не на кожен лист
          groupAlertBehavior: GroupAlertBehavior.summary,
          styleInformation: style,
          number: number,
          channelShowBadge: true,
          autoCancel: true,
        );
    final shown = fresh.take(_maxShown).toList();
    for (final m in shown.reversed) {
      final pk = (m['pk'] as num?)?.toInt() ?? 0;
      final from = (m['from'] ?? '').toString();
      final subject = (m['subject'] ?? '').toString();
      await _plugin.show(
        id: pk & 0x7fffffff,
        title: from.isEmpty ? tr(lang, 'notif_channel') : from,
        body: subject,
        notificationDetails: NotificationDetails(android: details(style: BigTextStyleInformation(subject))),
        payload: '$pk',
      );
    }
    final lines = [
      for (final m in shown) '${m['from'] ?? ''} — ${m['subject'] ?? ''}',
      if (fresh.length > shown.length) trn(lang, 'notif_more_n', fresh.length - shown.length),
    ];
    final title = trn(lang, 'notif_new_n', fresh.length);
    await _plugin.show(
      id: _summaryId,
      title: title,
      body: lines.first,
      notificationDetails: NotificationDetails(
        android: details(
          summary: true,
          number: inbox, // число на іконці там, де телефон його показує (непрочитані «Вхідних»)
          style: InboxStyleInformation(lines, contentTitle: title),
        ),
      ),
      payload: '',
    );
  }
}
