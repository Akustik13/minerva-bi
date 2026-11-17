// Налаштування застосунку: скринька й телефон, тема, мова, групування гілок, що робить свайп ліворуч, підпис,
// 🔔 сповіщення про нові листи, оновлення застосунку, відключення телефону.
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api.dart';
import '../l10n.dart';
import '../notify.dart';
import '../state.dart';
import '../system.dart';
import '../util.dart';
import '../widgets/common.dart';

class SettingsPage extends StatelessWidget {
  const SettingsPage({super.key});

  Future<void> _open(String url) async {
    final u = Uri.tryParse(url);
    if (u != null) await launchUrl(u, mode: LaunchMode.externalApplication);
  }

  @override
  Widget build(BuildContext context) {
    final app = AppScope.of(context);
    final scheme = Theme.of(context).colorScheme;
    final meta = app.meta ?? {};
    final user = (meta['user'] as Map?) ?? {};
    final account = (meta['account'] as Map?) ?? {};
    final device = (meta['device'] as Map?) ?? {};
    final appInfo = (meta['app'] as Map?) ?? {};

    Widget section(String title) => Padding(
          padding: const EdgeInsets.fromLTRB(20, 20, 16, 6),
          child: Text(title.toUpperCase(),
              style: TextStyle(fontSize: 12, letterSpacing: 0.8, fontWeight: FontWeight.w700, color: scheme.primary)),
        );

    return Scaffold(
      appBar: AppBar(title: Text(context.t('settings'))),
      body: ListView(padding: const EdgeInsets.only(bottom: 32), children: [
        if (app.updateAvailable)
          Card(
            margin: const EdgeInsets.all(12),
            color: scheme.primaryContainer,
            child: ListTile(
              leading: const Icon(Icons.system_update),
              title: Text('${context.t('update_available')} ${appInfo['version']}'),
              subtitle: Text(context.t('update_hint')),
              onTap: () => _open((appInfo['url'] ?? '').toString()),
            ),
          ),
        section(context.t('account')),
        ListTile(
          leading: const Icon(Icons.account_circle_outlined),
          title: Text((account['email'] ?? '').toString()),
          subtitle: Text('${user['name'] ?? ''} · ${app.store.base.replaceFirst(RegExp(r'^https?://'), '')}'),
        ),
        ListTile(
          leading: const Icon(Icons.phone_android),
          title: Text((device['name'] ?? '').toString().isEmpty ? context.t('this_phone') : device['name'].toString()),
          subtitle: Text('${context.t('app_version')} $appVersion'),
        ),
        ListTile(
          leading: const Icon(Icons.open_in_new),
          title: Text(context.t('open_minerva')),
          onTap: () => _open('${app.store.base}/email/'),
        ),
        section(context.t('look')),
        ListTile(
          leading: const Icon(Icons.dark_mode_outlined),
          title: Text(context.t('theme')),
          trailing: DropdownButton<String>(
            value: app.store.theme,
            underline: const SizedBox.shrink(),
            items: [
              DropdownMenuItem(value: 'system', child: Text(context.t('theme_system'))),
              DropdownMenuItem(value: 'light', child: Text(context.t('theme_light'))),
              DropdownMenuItem(value: 'dark', child: Text(context.t('theme_dark'))),
            ],
            onChanged: (v) => app.setTheme(v ?? 'system'),
          ),
        ),
        ListTile(
          leading: const Icon(Icons.language),
          title: Text(context.t('language')),
          trailing: DropdownButton<String>(
            value: app.store.lang,
            underline: const SizedBox.shrink(),
            items: [
              DropdownMenuItem(value: 'system', child: Text(context.t('lang_system'))),
              const DropdownMenuItem(value: 'uk', child: Text('Українська')),
              const DropdownMenuItem(value: 'en', child: Text('English')),
              const DropdownMenuItem(value: 'de', child: Text('Deutsch')),
            ],
            onChanged: (v) => app.setLang(v ?? 'system'),
          ),
        ),
        section(context.t('mail')),
        SwitchListTile(
          secondary: const Icon(Icons.forum_outlined),
          title: Text(context.t('group_threads')),
          subtitle: Text(context.t('group_threads_hint')),
          value: app.store.grouped,
          onChanged: app.setGrouped,
        ),
        ListTile(
          leading: const Icon(Icons.swipe_left_outlined),
          title: Text(context.t('swipe_left')),
          trailing: DropdownButton<String>(
            value: app.store.swipeLeft,
            underline: const SizedBox.shrink(),
            items: [
              DropdownMenuItem(value: 'archive', child: Text(context.t('archive'))),
              DropdownMenuItem(value: 'delete', child: Text(context.t('delete'))),
            ],
            onChanged: (v) => app.setSwipeLeft(v ?? 'archive'),
          ),
        ),
        SwitchListTile(
          secondary: const Icon(Icons.draw_outlined),
          title: Text(context.t('add_signature')),
          subtitle: Text(context.t('signature_hint')),
          value: app.store.signature,
          onChanged: app.setSignature,
        ),
        section(context.t('notifications')),
        const _NotifySection(),
        section(context.t('safety')),
        ListTile(
          leading: Icon(Icons.link_off, color: scheme.error),
          title: Text(context.t('unpair'), style: TextStyle(color: scheme.error)),
          subtitle: Text(context.t('unpair_hint')),
          onTap: () async {
            if (await confirm(context, context.t('unpair_confirm'), ok: context.t('unpair'))) {
              await app.unpair();
            }
          },
        ),
      ]),
    );
  }
}

/// 🔔 Сповіщення про нові листи: увімкнено, як часто Android будить перевірку, «Перевірити зараз», дозвіл.
class _NotifySection extends StatefulWidget {
  const _NotifySection();

  @override
  State<_NotifySection> createState() => _NotifySectionState();
}

class _NotifySectionState extends State<_NotifySection> with WidgetsBindingObserver {
  bool? allowed; // Android дозволяє сповіщення цьому застосунку
  bool checking = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _readAllowed();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _readAllowed(); // повернулись з налаштувань Android
  }

  Future<void> _readAllowed() async {
    try {
      final v = await MailNotifier.allowed();
      if (mounted) setState(() => allowed = v);
    } catch (_) {}
  }

  Future<void> _toggle(bool on) async {
    final app = AppScope.read(context);
    final ok = await app.setNotify(on);
    await _readAllowed();
    if (on && !ok && mounted) {
      snack(context, context.t('notify_denied'),
          seconds: 6, action: context.t('android_settings'), onAction: System.openNotificationSettings);
    }
  }

  Future<void> _checkNow() async {
    setState(() => checking = true);
    final none = context.t('notify_none_new');
    try {
      final n = await MailNotifier.check(force: true);
      if (!mounted) return;
      snack(context, n > 0 ? context.tn('notify_shown_n', n) : none);
    } catch (e) {
      if (mounted) snack(context, errorText(context, e));
    } finally {
      if (mounted) setState(() => checking = false);
    }
  }

  String _every(BuildContext c, int m) => m < 60 ? c.tn('every_min_n', m) : c.tn('every_hour_n', m ~/ 60);

  @override
  Widget build(BuildContext context) {
    final app = AppScope.of(context);
    final scheme = Theme.of(context).colorScheme;
    final on = app.store.notifyOn;
    return Column(children: [
      SwitchListTile(
        secondary: const Icon(Icons.notifications_active_outlined),
        title: Text(context.t('notify_on')),
        subtitle: Text(context.t('notify_hint')),
        isThreeLine: true,
        value: on,
        onChanged: _toggle,
      ),
      if (on && allowed == false)
        ListTile(
          leading: Icon(Icons.notifications_off_outlined, color: scheme.error),
          title: Text(context.t('notify_denied'), style: TextStyle(color: scheme.error)),
          trailing: const Icon(Icons.open_in_new),
          onTap: System.openNotificationSettings,
        ),
      if (on) ...[
        ListTile(
          leading: const Icon(Icons.schedule),
          title: Text(context.t('notify_every')),
          trailing: DropdownButton<int>(
            value: intervals.contains(app.store.notifyEvery) ? app.store.notifyEvery : 15,
            underline: const SizedBox.shrink(),
            items: [for (final m in intervals) DropdownMenuItem(value: m, child: Text(_every(context, m)))],
            onChanged: (v) => app.setNotifyEvery(v ?? 15),
          ),
        ),
        ListTile(
          leading: checking
              ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2.5))
              : const Icon(Icons.mark_email_unread_outlined),
          title: Text(context.t('notify_check_now')),
          onTap: checking ? null : _checkNow,
        ),
        ListTile(
          leading: const Icon(Icons.battery_saver_outlined),
          subtitle: Text(context.t('battery_hint')),
          trailing: TextButton(onPressed: System.openAppSettings, child: Text(context.t('android_settings'))),
        ),
      ],
    ]);
  }
}
