import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:minerva_mail/api.dart';
import 'package:minerva_mail/l10n.dart';
import 'package:minerva_mail/screens/mail_list.dart';
import 'package:minerva_mail/util.dart';
import 'package:minerva_mail/voice.dart';

void main() {
  test('🎤 розділові знаки голосом — як у Minerva', () {
    expect(spokenPunctuation('добрий день кома дякую за лист крапка надішліть ціну знак питання', 'uk'),
        'добрий день, дякую за лист. Надішліть ціну?');
    expect(spokenPunctuation('vielen Dank Komma neue Zeile mit freundlichen Grüßen', 'de'),
        'vielen Dank,\nMit freundlichen Grüßen');
    expect(spokenPunctuation('день, кома, як', 'uk'), 'день, як'); // знак від розпізнавання + слово — один знак
    expect(spokenPunctuation('тема новий рядок далі', 'uk', single: true), 'тема далі');
    expect(spokenPunctuation('the period of time', 'uk'), 'the period of time'); // чужа мова — без заміни
  });

  test('🎤 вставка там, де курсор', () {
    final c = TextEditingController(text: 'Hallo Anna,');
    c.selection = const TextSelection.collapsed(offset: 11);
    final r = insertAtCursor(c, 'danke für die Anfrage.');
    expect(c.text, 'Hallo Anna, danke für die Anfrage.');
    expect(r, (11, 34));
    insertAtCursor(c, 'wir melden uns');
    expect(c.text, 'Hallo Anna, danke für die Anfrage. Wir melden uns'); // після крапки — з великої
    final empty = TextEditingController();
    insertAtCursor(empty, 'дякую');
    expect(empty.text, 'Дякую');
    expect(replaceInserted(c, r!, ' danke für die Anfrage.', ' Дякую за запит.'), isTrue);
    expect(c.text, 'Hallo Anna, Дякую за запит. Wir melden uns');
    expect(replaceInserted(c, r, 'щось інше', 'x'), isFalse); // змінили руками — не чіпаємо
    expect(insertAtCursor(c, '   '), isNull);
  });

  test('🔔 написи сповіщень без BuildContext', () {
    expect(trn('uk', 'notif_new_n', 1), '1 новий лист');
    expect(trn('uk', 'notif_new_n', 3), '3 нові листи');
    expect(trn('uk', 'notif_new_n', 7), '7 нових листів');
    expect(trn('de', 'notif_new_n', 2), '2 neue E-Mails');
    expect(tr('en', 'notif_channel'), 'New mail');
  });

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
