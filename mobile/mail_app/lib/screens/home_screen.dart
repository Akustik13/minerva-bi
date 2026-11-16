// Головний екран: нижня навігація під великий палець — Пошта / Важливі / Папки / Налаштування, кнопка
// «Написати» праворуч унизу. Повернулись у застосунок — список і лічильники оновлюються самі.
import 'package:flutter/material.dart';

import '../l10n.dart';
import '../state.dart';
import 'compose_screen.dart';
import 'folders_page.dart';
import 'mail_list.dart';
import 'settings_page.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  int tab = 0;
  MailQuery current = const MailQuery();
  final refreshTick = ValueNotifier<int>(0);
  late final Ticker ticker;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    ticker = Ticker(const Duration(minutes: 2), _tick)..start();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final app = AppScope.read(context);
      app.refreshMeta().catchError((_) {});
      app.refreshNav().catchError((_) {});
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    ticker.stop();
    refreshTick.dispose();
    super.dispose();
  }

  void _tick() {
    AppScope.read(context).refreshCounts();
    refreshTick.value++;
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      ticker.start();
      _tick();
    } else if (state == AppLifecycleState.paused) {
      ticker.stop();
    }
  }

  void _pick(MailQuery q) => setState(() {
        FocusManager.instance.primaryFocus?.unfocus();
        current = q;
        tab = q.folder == 'starred' && !q.filtered ? 1 : 0;
      });

  Future<void> _compose() async {
    final sent = await Navigator.of(context).push<bool>(MaterialPageRoute(builder: (_) => const ComposeScreen()));
    if (sent == true) refreshTick.value++;
  }

  @override
  Widget build(BuildContext context) {
    final app = AppScope.of(context);
    final inbox = app.unread('inbox');
    final starred = app.unread('starred');
    final pages = [
      MailListPage(
        key: ValueKey('mail:${current.key}'),
        query: current,
        refreshTick: refreshTick,
        onClearFilter: () => setState(() => current = const MailQuery()),
      ),
      MailListPage(key: const ValueKey('starred'), query: const MailQuery(folder: 'starred'), refreshTick: refreshTick),
      FoldersPage(onPick: _pick, current: current),
      const SettingsPage(),
    ];
    return PopScope(
      canPop: tab == 0 && !current.filtered && current.folder == 'inbox',
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        setState(() {
          if (tab != 0) {
            tab = 0;
          } else {
            current = const MailQuery();
          }
        });
      },
      child: Scaffold(
        body: IndexedStack(index: tab, children: pages),
        floatingActionButton: tab <= 1
            ? FloatingActionButton.extended(
                onPressed: _compose,
                icon: const Icon(Icons.edit_outlined),
                label: Text(context.t('compose')),
              )
            : null,
        bottomNavigationBar: NavigationBar(
          selectedIndex: tab,
          onDestinationSelected: (i) => setState(() {
            FocusManager.instance.primaryFocus?.unfocus(); // фокус не лишається на схованій вкладці
            if (i == 0 && tab == 0 && (current.filtered || current.folder != 'inbox')) {
              current = const MailQuery(); // ще раз на «Пошта» — назад у Вхідні
            }
            tab = i;
          }),
          destinations: [
            NavigationDestination(
              icon: Badge(isLabelVisible: inbox > 0, label: Text('$inbox'), child: const Icon(Icons.mail_outline)),
              selectedIcon: Badge(isLabelVisible: inbox > 0, label: Text('$inbox'), child: const Icon(Icons.mail)),
              label: context.t('mail'),
            ),
            NavigationDestination(
              icon: Badge(isLabelVisible: starred > 0, label: Text('$starred'), child: const Icon(Icons.star_outline)),
              selectedIcon: Badge(isLabelVisible: starred > 0, label: Text('$starred'), child: const Icon(Icons.star)),
              label: context.t('starred'),
            ),
            NavigationDestination(icon: const Icon(Icons.folder_outlined), selectedIcon: const Icon(Icons.folder), label: context.t('folders')),
            NavigationDestination(
              icon: Badge(isLabelVisible: app.updateAvailable, child: const Icon(Icons.settings_outlined)),
              selectedIcon: const Icon(Icons.settings),
              label: context.t('settings'),
            ),
          ],
        ),
      ),
    );
  }
}
