import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import 'api.dart';
import 'l10n.dart';

String _lang(BuildContext c) => Localizations.localeOf(c).languageCode;

/// Дата в списку: сьогодні — час, цього року — «12 лист.», інакше — 12.11.25.
String fmtShortDate(BuildContext c, String iso) {
  final d = DateTime.tryParse(iso)?.toLocal();
  if (d == null) return '';
  final now = DateTime.now();
  final lang = _lang(c);
  if (d.year == now.year && d.month == now.month && d.day == now.day) return DateFormat.Hm(lang).format(d);
  final yesterday = now.subtract(const Duration(days: 1));
  if (d.year == yesterday.year && d.month == yesterday.month && d.day == yesterday.day) return c.t('yesterday');
  if (d.year == now.year) return DateFormat.MMMd(lang).format(d);
  return DateFormat('dd.MM.yy').format(d);
}

String fmtFullDate(BuildContext c, String iso) {
  final d = DateTime.tryParse(iso)?.toLocal();
  if (d == null) return '';
  return DateFormat.yMMMd(_lang(c)).add_Hm().format(d);
}

Color hexColor(String? s, Color fallback) {
  final v = (s ?? '').replaceAll('#', '');
  if (v.length != 6 && v.length != 8) return fallback;
  final n = int.tryParse(v.length == 6 ? 'FF$v' : v, radix: 16);
  return n == null ? fallback : Color(n);
}

const _avatarColors = [
  Color(0xFF1E88E5), Color(0xFF43A047), Color(0xFFE53935), Color(0xFF8E24AA), Color(0xFFF4511E),
  Color(0xFF00897B), Color(0xFF3949AB), Color(0xFFD81B60), Color(0xFF6D4C41), Color(0xFF546E7A),
];

Color avatarColor(String seed) {
  var h = 0;
  for (final ch in seed.toLowerCase().codeUnits) {
    h = (h * 31 + ch) & 0x7fffffff;
  }
  return _avatarColors[h % _avatarColors.length];
}

String initial(String name) {
  final s = name.trim().replaceAll(RegExp(r'^["\x27<]+'), '');
  if (s.isEmpty) return '?';
  return String.fromCharCode(s.runes.first).toUpperCase();
}

String fmtSize(num bytes) {
  if (bytes <= 0) return '';
  if (bytes < 1024) return '$bytes B';
  if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(0)} KB';
  return '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
}

String errorText(BuildContext c, Object e) {
  if (e is ApiException) {
    if (e.offline) {
      return switch (e.message) {
        'tls' => c.t('err_tls'),
        'timeout' => c.t('err_timeout'),
        _ => c.t('err_offline'),
      };
    }
    return e.message;
  }
  return '${c.t('error')}: $e';
}

void snack(BuildContext c, String text, {String? action, VoidCallback? onAction, int seconds = 4}) {
  final m = ScaffoldMessenger.maybeOf(c);
  if (m == null) return;
  m.hideCurrentSnackBar();
  m.showSnackBar(SnackBar(
    content: Text(text),
    duration: Duration(seconds: seconds),
    behavior: SnackBarBehavior.floating,
    action: action == null ? null : SnackBarAction(label: action, onPressed: onAction ?? () {}),
  ));
}

List<String> splitAddresses(String raw) =>
    raw.split(RegExp(r'[,;\s]+')).map((e) => e.trim()).where((e) => e.contains('@')).toList();
