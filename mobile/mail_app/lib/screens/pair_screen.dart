// Підключення телефону до Minerva: QR зі сторінки «📱 Пошта на телефоні» (/email/phone/) або код з неї — скопійований
// («📋 Копіювати код») і вставлений, чи набраний руками.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../api.dart';
import '../l10n.dart';
import '../state.dart';
import '../util.dart';

class PairScreen extends StatefulWidget {
  const PairScreen({super.key});

  @override
  State<PairScreen> createState() => _PairScreenState();
}

class _PairScreenState extends State<PairScreen> {
  bool busy = false;
  String error = '';

  Future<void> _connect(String base, String code) async {
    base = normalizeBase(base);
    if (base.isEmpty || code.trim().isEmpty) return;
    if (!cleartextAllowed(base)) {
      setState(() => error = context.t('need_https'));
      return;
    }
    setState(() {
      busy = true;
      error = '';
    });
    try {
      await AppScope.read(context).pair(base, code.trim());
    } catch (e) {
      if (!mounted) return;
      setState(() => error = errorText(context, e));
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> _scan() async {
    final text = await Navigator.of(context).push<String>(MaterialPageRoute(builder: (_) => const ScanScreen()));
    if (text == null || !mounted) return;
    try {
      final j = jsonDecode(text);
      if (j is Map && j['minerva'] == 'mail' && j['url'] is String && j['code'] is String) {
        await _connect(j['url'] as String, j['code'] as String);
        return;
      }
      if (j is Map && j['minerva'] == 'cards') {
        setState(() => error = context.t('qr_cards'));
        return;
      }
    } catch (_) {}
    final p = parsePairText(text); // QR з текстом «адреса#код»
    if (p != null && p.$1.isNotEmpty) {
      await _connect(p.$1, p.$2);
      return;
    }
    setState(() => error = context.t('qr_wrong'));
  }

  /// «⌨ Ввести або вставити код»: код, скопійований на сторінці Minerva («📋 Копіювати код»), підставляється з
  /// буфера сам; адреса потрібна лише тоді, коли вставили сам код без неї.
  Future<void> _manual() async {
    final r = await showDialog<(String, String)>(context: context, builder: (_) => const PasteCodeDialog());
    if (r != null) await _connect(r.$1, r.$2);
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(24, 32, 24, 24),
          children: [
            Center(
              child: ClipRRect(
                borderRadius: BorderRadius.circular(28),
                child: Image.asset('assets/icon.png', width: 112, height: 112),
              ),
            ),
            const SizedBox(height: 20),
            Text(context.t('app_name'),
                textAlign: TextAlign.center, style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 8),
            Text(context.t('pair_lead'),
                textAlign: TextAlign.center, style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 15)),
            if (state.lostPairing) ...[
              const SizedBox(height: 16),
              Card(
                color: scheme.errorContainer,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(context.t('pair_lost'), style: TextStyle(color: scheme.onErrorContainer)),
                ),
              ),
            ],
            const SizedBox(height: 24),
            _Step(n: '1', text: context.t('pair_step1')),
            _Step(n: '2', text: context.t('pair_step2')),
            _Step(n: '3', text: context.t('pair_step3')),
            const SizedBox(height: 24),
            SizedBox(
              height: 56,
              child: FilledButton.icon(
                onPressed: busy ? null : _scan,
                icon: const Icon(Icons.qr_code_scanner, size: 26),
                label: Text(context.t('scan_qr'), style: const TextStyle(fontSize: 17)),
              ),
            ),
            const SizedBox(height: 12),
            SizedBox(
              height: 48,
              child: OutlinedButton.icon(
                onPressed: busy ? null : _manual,
                icon: const Icon(Icons.keyboard),
                label: Text(context.t('manual_enter')),
              ),
            ),
            if (busy) ...[
              const SizedBox(height: 20),
              const Center(child: CircularProgressIndicator()),
            ],
            if (error.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(error, textAlign: TextAlign.center, style: TextStyle(color: scheme.error, fontSize: 15)),
            ],
            const SizedBox(height: 24),
            Text(context.t('pair_safety'),
                textAlign: TextAlign.center, style: TextStyle(color: scheme.outline, fontSize: 12.5)),
          ],
        ),
      ),
    );
  }
}

class PasteCodeDialog extends StatefulWidget {
  const PasteCodeDialog({super.key});

  @override
  State<PasteCodeDialog> createState() => _PasteCodeDialogState();
}

class _PasteCodeDialogState extends State<PasteCodeDialog> {
  final code = TextEditingController();
  final url = TextEditingController();
  String hint = '';

  (String, String)? get parsed => parsePairText(code.text);
  bool get needUrl => parsed == null || parsed!.$1.isEmpty;

  @override
  void initState() {
    super.initState();
    code.addListener(() => setState(() {}));
    url.addListener(() => setState(() {}));
    _paste(quiet: true); // скопіювали код на сторінці Minerva — одразу в полі
  }

  @override
  void dispose() {
    code.dispose();
    url.dispose();
    super.dispose();
  }

  Future<void> _paste({bool quiet = false}) async {
    final data = await Clipboard.getData(Clipboard.kTextPlain);
    final text = data?.text ?? '';
    if (!mounted) return;
    if (parsePairText(text) != null) {
      code.text = text.trim();
      setState(() => hint = context.t('code_pasted'));
    } else if (!quiet) {
      setState(() => hint = context.t('clipboard_no_code'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final p = parsed;
    return AlertDialog(
      title: Text(context.t('manual_title')),
      content: SingleChildScrollView(
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(context.t('manual_hint'), style: TextStyle(fontSize: 13.5, color: scheme.onSurfaceVariant)),
          const SizedBox(height: 12),
          TextField(
            controller: code,
            autocorrect: false,
            enableSuggestions: false,
            maxLines: 3,
            minLines: 1,
            decoration: InputDecoration(
              labelText: context.t('manual_code'),
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                tooltip: context.t('paste'),
                icon: const Icon(Icons.content_paste),
                onPressed: _paste,
              ),
            ),
          ),
          if (hint.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(hint, style: TextStyle(fontSize: 13, color: scheme.primary)),
            ),
          if (p != null && p.$1.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text('${context.t('manual_url')}: ${p.$1}', style: TextStyle(fontSize: 13, color: scheme.onSurfaceVariant)),
            ),
          if (code.text.trim().isNotEmpty && needUrl) ...[
            const SizedBox(height: 12),
            TextField(
              controller: url,
              keyboardType: TextInputType.url,
              autocorrect: false,
              decoration: InputDecoration(
                labelText: context.t('manual_url'),
                hintText: 'https://minerva.example.com',
                border: const OutlineInputBorder(),
              ),
            ),
          ],
        ]),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: Text(context.t('cancel'))),
        FilledButton(
          onPressed: p == null || (p.$1.isEmpty && url.text.trim().isEmpty)
              ? null
              : () => Navigator.pop(context, (p.$1.isNotEmpty ? p.$1 : url.text.trim(), p.$2)),
          child: Text(context.t('connect')),
        ),
      ],
    );
  }
}

class _Step extends StatelessWidget {
  const _Step({required this.n, required this.text});

  final String n;
  final String text;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        CircleAvatar(
          radius: 13,
          backgroundColor: scheme.primaryContainer,
          child: Text(n, style: TextStyle(fontSize: 13, color: scheme.onPrimaryContainer)),
        ),
        const SizedBox(width: 12),
        Expanded(child: Padding(padding: const EdgeInsets.only(top: 3), child: Text(text))),
      ]),
    );
  }
}

class ScanScreen extends StatefulWidget {
  const ScanScreen({super.key});

  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen> {
  final controller = MobileScannerController(formats: const [BarcodeFormat.qrCode]);
  bool done = false;

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(context.t('scan_qr')),
        actions: [
          IconButton(icon: const Icon(Icons.flash_on), onPressed: () => controller.toggleTorch()),
        ],
      ),
      body: Stack(children: [
        MobileScanner(
          controller: controller,
          onDetect: (capture) {
            if (done) return;
            for (final b in capture.barcodes) {
              final v = b.rawValue;
              if (v != null && v.isNotEmpty) {
                done = true;
                Navigator.of(context).pop(v);
                return;
              }
            }
          },
          errorBuilder: (c, e, child) => Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(c.t('camera_error'), textAlign: TextAlign.center),
            ),
          ),
        ),
        Center(
          child: Container(
            width: 250,
            height: 250,
            decoration: BoxDecoration(
              border: Border.all(color: Colors.white, width: 3),
              borderRadius: BorderRadius.circular(18),
            ),
          ),
        ),
        Positioned(
          left: 24,
          right: 24,
          bottom: 40,
          child: Text(context.t('scan_hint'),
              textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.white, fontSize: 15, shadows: [Shadow(blurRadius: 6)])),
        ),
      ]),
    );
  }
}
