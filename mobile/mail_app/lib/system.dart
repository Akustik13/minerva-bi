// Сторінки налаштувань Android для цього застосунку (MainActivity.kt, канал minerva/system): дозволи (мікрофон),
// сповіщення, батарея — коли людина колись відмовила, Android більше не питає сам.
import 'package:flutter/services.dart';

class System {
  static const _ch = MethodChannel('minerva/system');

  /// «Про застосунок»: дозволи, батарея («Без обмежень»), сповіщення.
  static Future<void> openAppSettings() async {
    try {
      await _ch.invokeMethod('appSettings');
    } catch (_) {}
  }

  /// Сповіщення застосунку (Android 8+; старіший — «Про застосунок»).
  static Future<void> openNotificationSettings() async {
    try {
      await _ch.invokeMethod('notificationSettings');
    } catch (_) {}
  }
}
