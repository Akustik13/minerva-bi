import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:minerva_mail/api.dart';
import 'package:minerva_mail/l10n.dart';
import 'package:minerva_mail/screens/mail_list.dart';
import 'package:minerva_mail/util.dart';

void main() {
  test('адреса Minerva з QR / ручного вводу', () {
    expect(normalizeBase('minerva.example.com/'), 'https://minerva.example.com');
    expect(normalizeBase(' https://m.example.com// '), 'https://m.example.com');
    expect(cleartextAllowed('https://m.example.com'), isTrue);
    expect(cleartextAllowed('http://192.168.2.123:81'), isFalse);
    expect(cleartextAllowed('http://10.0.2.2:8000'), isTrue);
  });

  test('код зі сторінки Minerva: «адреса#код», текст QR, лише код', () {
    const code = 'Q9xejc3Nu1Iw_KGbT-OV9LUbrcPQ3wU-';
    expect(parsePairText(' https://m.example.com#$code\n'), ('https://m.example.com', code));
    expect(parsePairText('{"minerva":"mail","v":1,"url":"https://m.example.com","code":"$code"}'),
        ('https://m.example.com', code));
    expect(parsePairText(code), ('', code));
    expect(parsePairText('https://m.example.com#short'), isNull);
    expect(parsePairText('Hallo Welt'), isNull);
    expect(parsePairText(''), isNull);
  });

  test('адреси листа з поля «Кому»', () {
    expect(splitAddresses('a@x.de, b@y.com; ,c'), ['a@x.de', 'b@y.com']);
  });

  test('ключ списку: папка й фільтри', () {
    expect(const MailQuery().key, 'inbox');
    expect(const MailQuery(filters: {'wcat': '3'}).key, 'inbox&wcat=3');
    expect(const MailQuery(folder: 'drafts').isDrafts, isTrue);
    expect(const MailQuery(folder: 'drafts', filters: {'quote': '1'}).isDrafts, isFalse);
  });

  testWidgets('мови й числа (українська — три форми)', (tester) async {
    late BuildContext ctx;
    Future<void> pump(Locale locale) => tester.pumpWidget(Localizations(
          locale: locale,
          delegates: const [DefaultWidgetsLocalizations.delegate],
          child: Builder(builder: (c) {
            ctx = c;
            return const SizedBox();
          }),
        ));
    await pump(const Locale('uk'));
    expect(ctx.t('inbox'), 'Вхідні');
    expect(ctx.tn('days_n', 1), '1 день');
    expect(ctx.tn('days_n', 3), '3 дні');
    expect(ctx.tn('days_n', 5), '5 днів');
    expect(ctx.tn('days_n', 11), '11 днів');
    expect(ctx.t('Meine Order'), 'Meine Order');
    await pump(const Locale('de'));
    expect(ctx.t('inbox'), 'Posteingang');
    expect(ctx.tn('days_n', 2), '2 Tage');
    expect(resolveLocale(const Locale('ru'), supportedLocales), const Locale('uk'));
    expect(resolveLocale(const Locale('fr'), supportedLocales), const Locale('en'));
  });
}
