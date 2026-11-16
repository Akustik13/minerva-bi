// «Minerva Пошта» — пошта Minerva в телефоні (Android). Підключення — QR-кодом зі сторінки «📱 Пошта на телефоні»
// у Minerva (/email/phone/); усе інше — API /email/phone/api/… (email_assistant/app_api.py).
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'l10n.dart';
import 'screens/home_screen.dart';
import 'screens/pair_screen.dart';
import 'state.dart';
import 'store.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initializeDateFormatting();
  final store = await Store.open();
  runApp(MinervaMailApp(state: AppState(store)));
}

const _seed = Color(0xFF2D6CDF);

ThemeData buildTheme(Brightness brightness) {
  final scheme = ColorScheme.fromSeed(seedColor: _seed, brightness: brightness);
  final dark = brightness == Brightness.dark;
  return ThemeData(
    useMaterial3: true,
    colorScheme: dark ? scheme.copyWith(surface: const Color(0xFF10192A), surfaceContainerLow: const Color(0xFF162236)) : scheme,
    scaffoldBackgroundColor: dark ? const Color(0xFF10192A) : null,
    visualDensity: VisualDensity.standard,
    listTileTheme: const ListTileThemeData(minVerticalPadding: 10),
    snackBarTheme: const SnackBarThemeData(behavior: SnackBarBehavior.floating),
    appBarTheme: AppBarTheme(
      centerTitle: false,
      scrolledUnderElevation: 1,
      backgroundColor: dark ? const Color(0xFF10192A) : null,
    ),
    navigationBarTheme: NavigationBarThemeData(
      height: 68,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
      backgroundColor: dark ? const Color(0xFF162236) : null,
    ),
  );
}

class MinervaMailApp extends StatelessWidget {
  const MinervaMailApp({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return AppScope(
      state: state,
      child: ListenableBuilder(
        listenable: state,
        builder: (context, _) => MaterialApp(
          title: 'Minerva Mail',
          navigatorKey: AppState.navigatorKey,
          debugShowCheckedModeBanner: false,
          theme: buildTheme(Brightness.light),
          darkTheme: buildTheme(Brightness.dark),
          themeMode: state.themeMode,
          locale: state.locale,
          supportedLocales: supportedLocales,
          localizationsDelegates: const [
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          localeResolutionCallback: resolveLocale,
          home: state.store.paired ? const HomeScreen() : const PairScreen(),
        ),
      ),
    );
  }
}
