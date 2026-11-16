// Оригінал листа (HTML) — як віконце листа в скриньці: WebView завантажує його з Minerva з ключем телефону
// (картинки з підпису вже всередині — data:), скрипти листа не діють (CSP сервера й вимкнений JavaScript),
// посилання відкриваються в браузері.
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../l10n.dart';
import '../state.dart';
import '../widgets/common.dart';

class HtmlScreen extends StatefulWidget {
  const HtmlScreen({super.key, required this.messageId, this.title = ''});

  final int messageId;
  final String title;

  @override
  State<HtmlScreen> createState() => _HtmlScreenState();
}

class _HtmlScreenState extends State<HtmlScreen> {
  late final WebViewController controller;
  bool loading = true;
  String error = '';

  @override
  void initState() {
    super.initState();
    final api = AppScope.read(context).api;
    final url = api?.uri('messages/${widget.messageId}/html/');
    controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.disabled)
      ..setBackgroundColor(Colors.white)
      ..enableZoom(true)
      ..setNavigationDelegate(NavigationDelegate(
        onNavigationRequest: (req) {
          if (url != null && req.url == url.toString()) return NavigationDecision.navigate;
          final u = Uri.tryParse(req.url);
          if (u != null && const ['http', 'https', 'mailto', 'tel'].contains(u.scheme)) {
            launchUrl(u, mode: LaunchMode.externalApplication);
          }
          return NavigationDecision.prevent;
        },
        onPageFinished: (_) {
          if (mounted) setState(() => loading = false);
        },
        onHttpError: (e) {
          // лише сама сторінка листа (WebView ще питає /favicon.ico — його 404 не помилка)
          if (e.request?.uri.toString() == url.toString() && mounted) {
            setState(() => error = 'HTTP ${e.response?.statusCode ?? ''}');
          }
        },
        onWebResourceError: (e) {
          if (e.isForMainFrame != false && mounted) setState(() => error = e.description);
        },
      ));
    if (api != null && url != null) controller.loadRequest(url, headers: api.headers);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
          title: Text(widget.title.isEmpty ? context.t('original') : widget.title, maxLines: 1, overflow: TextOverflow.ellipsis)),
      body: error.isNotEmpty
          ? EmptyState(icon: Icons.cloud_off, text: '${context.t('err_offline')} ($error)')
          : Stack(children: [
              Positioned.fill(child: WebViewWidget(controller: controller)),
              if (loading) const LinearProgressIndicator(),
            ]),
    );
  }
}
