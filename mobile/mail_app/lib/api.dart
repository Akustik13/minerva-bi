// Клієнт API пошти Minerva (/email/phone/api/…): ключ телефону в заголовку Authorization, JSON у відповідях.
// Серверна частина — email_assistant/app_api.py; вона викликає ті самі функції, що веб-пошта.
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

const appVersion = '1.0.3';

class ApiException implements Exception {
  ApiException(this.status, this.message);
  final int status;
  final String message;
  bool get unauthorized => status == 401;
  bool get offline => status == 0;
  @override
  String toString() => message;
}

typedef Json = Map<String, dynamic>;

/// Адреса Minerva з QR / ручного вводу: без «/» у кінці; http — лише для емулятора й цього ж телефона.
String normalizeBase(String raw) {
  var s = raw.trim();
  if (s.isEmpty) return s;
  if (!s.startsWith('http://') && !s.startsWith('https://')) s = 'https://$s';
  while (s.endsWith('/')) {
    s = s.substring(0, s.length - 1);
  }
  return s;
}

final _codeRe = RegExp(r'^[A-Za-z0-9_-]{16,64}$');

/// Код підключення, скопійований зі сторінки «📱 Пошта на телефоні»: «адреса#код» (кнопка «📋 Копіювати код»),
/// текст QR {"minerva": "mail", "url", "code"} або лише код → (адреса або '', код); інше — null.
(String, String)? parsePairText(String raw) {
  final s = raw.trim();
  if (s.isEmpty) return null;
  if (s.startsWith('{')) {
    try {
      final j = jsonDecode(s);
      if (j is Map && j['code'] is String && _codeRe.hasMatch(j['code'] as String)) {
        return ((j['url'] ?? '').toString(), j['code'] as String);
      }
    } catch (_) {}
    return null;
  }
  final hash = s.lastIndexOf('#');
  if (hash > 0) {
    final code = s.substring(hash + 1).trim();
    if (_codeRe.hasMatch(code)) return (s.substring(0, hash).trim(), code);
  }
  return _codeRe.hasMatch(s) ? ('', s) : null;
}

bool cleartextAllowed(String base) {
  final u = Uri.tryParse(base);
  if (u == null) return false;
  return u.scheme == 'https' || const ['10.0.2.2', 'localhost', '127.0.0.1'].contains(u.host);
}

class Api {
  Api(this.base, this.token, {this.onUnauthorized});

  final String base;
  final String token;
  void Function()? onUnauthorized;
  final http.Client _client = http.Client();

  static const timeout = Duration(seconds: 40);
  static const longTimeout = Duration(seconds: 180);

  Map<String, String> get headers => {
        'Authorization': 'Bearer $token',
        'X-Minerva-App': appVersion,
        'Accept': 'application/json',
      };

  Uri uri(String path, [Map<String, String>? query]) {
    final q = <String, String>{};
    query?.forEach((k, v) {
      if (v.isNotEmpty) q[k] = v;
    });
    return Uri.parse('$base/email/phone/api/$path').replace(queryParameters: q.isEmpty ? null : q);
  }

  /// Адреса значка відправника (той самий /email/logo/<ключ>/, що в скриньці).
  String logoUrl(String key, String version) => '$base/email/phone/api/logo/${Uri.encodeComponent(key)}/?v=$version';

  static Future<Json> pair(String base, String code, String device) async {
    try {
      final r = await http
          .post(Uri.parse('$base/email/phone/api/pair/'),
              headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
              body: jsonEncode({'code': code, 'device': device, 'version': appVersion}))
          .timeout(timeout);
      return _decode(r);
    } on SocketException {
      throw ApiException(0, 'offline');
    } on HandshakeException {
      throw ApiException(0, 'tls');
    } on http.ClientException catch (e) {
      throw ApiException(0, e.message);
    } on FormatException {
      throw ApiException(0, 'format');
    }
  }

  static Json _decode(http.Response r) {
    Json body;
    try {
      final decoded = jsonDecode(utf8.decode(r.bodyBytes));
      body = decoded is Map<String, dynamic> ? decoded : {'data': decoded};
    } catch (_) {
      // не JSON: коротке пояснення сервера текстом («Файл не знайдено. Пересинхронізуйте пошту.») — його й показати
      final text = utf8.decode(r.bodyBytes, allowMalformed: true).trim();
      body = r.statusCode >= 400 && text.isNotEmpty && text.length < 300 && !text.startsWith('<') ? {'error': text} : {};
    }
    if (r.statusCode >= 400) {
      throw ApiException(r.statusCode, (body['error'] ?? 'HTTP ${r.statusCode}').toString());
    }
    return body;
  }

  Future<T> _guard<T>(Future<T> Function() run) async {
    try {
      return await run();
    } on ApiException catch (e) {
      if (e.unauthorized) onUnauthorized?.call();
      rethrow;
    } on SocketException {
      throw ApiException(0, 'offline');
    } on HandshakeException {
      throw ApiException(0, 'tls');
    } on http.ClientException catch (e) {
      throw ApiException(0, e.message);
    } on HttpException catch (e) {
      throw ApiException(0, e.message);
    } on Exception catch (e) {
      if (e.toString().contains('TimeoutException')) throw ApiException(0, 'timeout');
      rethrow;
    }
  }

  Future<Json> get(String path, {Map<String, String>? query, Duration? wait}) => _guard(() async {
        final r = await _client.get(uri(path, query), headers: headers).timeout(wait ?? timeout);
        return _decode(r);
      });

  Future<Json> postJson(String path, [Json? body, Duration? wait]) => _guard(() async {
        final r = await _client
            .post(uri(path), headers: {...headers, 'Content-Type': 'application/json'}, body: jsonEncode(body ?? {}))
            .timeout(wait ?? timeout);
        return _decode(r);
      });

  Future<Json> postForm(String path, Map<String, String> form, [Duration? wait]) => _guard(() async {
        final r = await _client.post(uri(path), headers: headers, body: form).timeout(wait ?? timeout);
        return _decode(r);
      });

  /// Форма листа (як «Відправити» у скриньці): поля, повторювані поля (att_ref) і файли з телефона.
  Future<Json> postMultipart(String path, Map<String, String> fields,
          {Map<String, List<String>> repeated = const {}, List<LocalFile> files = const [], Duration? wait}) =>
      _guard(() async {
        final req = http.MultipartRequest('POST', uri(path));
        req.headers.addAll(headers);
        req.fields.addAll(fields);
        repeated.forEach((name, values) {
          for (final v in values) {
            // частина без імені файлу — звичайне поле форми (Django), так передаються кілька значень одного поля
            req.files.add(http.MultipartFile.fromString(name, v));
          }
        });
        for (final f in files) {
          req.files.add(await http.MultipartFile.fromPath('attachments', f.path, filename: f.name));
        }
        final streamed = await _client.send(req).timeout(wait ?? longTimeout);
        return _decode(await http.Response.fromStream(streamed));
      });

  /// Сира відповідь (HTML листа, вкладення).
  Future<http.Response> raw(String path, {Map<String, String>? query}) => _guard(() async {
        final r = await _client.get(uri(path, query), headers: headers).timeout(longTimeout);
        if (r.statusCode >= 400) _decode(r);
        return r;
      });

  void close() => _client.close();
}

class LocalFile {
  LocalFile(this.path, this.name, this.size);
  final String path;
  final String name;
  final int size;
}
