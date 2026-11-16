// Підключення телефону до Minerva: QR зі сторінки «📱 Пошта на телефоні» (/email/phone/) або адреса й код вручну.
import 'dart:convert';

import 'package:flutter/material.dart';
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
    setState(() => error = context.t('qr_wrong'));
  }

  Future<void> _manual() async {
    final url = TextEditingController(), code = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(c.t('manual_title')),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(
            controller: url,
            keyboardType: TextInputType.url,
            decoration: InputDecoration(labelText: c.t('manual_url'), hintText: 'https://minerva.example.com'),
          ),
          const SizedBox(height: 8),
          TextField(controller: code, decoration: InputDecoration(labelText: c.t('manual_code'))),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(c.t('cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(c.t('connect'))),
        ],
      ),
    );
    if (ok == true) await _connect(url.text, code.text);
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
