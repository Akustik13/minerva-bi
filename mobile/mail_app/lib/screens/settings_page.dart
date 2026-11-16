// Налаштування застосунку: скринька й телефон, тема, мова, групування гілок, що робить свайп ліворуч, підпис,
// оновлення застосунку, відключення телефону.
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
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
