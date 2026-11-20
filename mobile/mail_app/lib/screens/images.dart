// 🖼 Картинки листа: рядок мініатюр під текстом (і вставлені в текст — скріншоти, фото з iPhone) і перегляд на весь
// екран зі збільшенням пальцями. Мініатюри й перегляд — зменшені на сервері (…/attachments/<i>/thumb/?w=), оригінал —
// «Відкрити файл» (програма телефона). Дрібні вставлені картинки (логотипи в підписі) — лише в «🌐 Оригінал листа».
import 'package:flutter/material.dart';

import '../api.dart';
import '../l10n.dart';

const _logoMax = 20000; // вставлена картинка, менша за 20 КБ, — логотип / значок підпису, не показуємо окремо

/// Картинка листа, яку показати мініатюрою.
bool galleryImage(Map<String, dynamic> a) {
  if (a['image'] != true) return false;
  final size = (a['size'] as num?)?.toInt() ?? 0;
  return a['inline'] != true || size == 0 || size >= _logoMax;
}

/// Вкладення, яке показати файлом-кнопкою: не мініатюра й не дрібна вставлена картинка (логотип). Вставлений
/// не-картинкою файл (PDF, вставлений у текст в Apple Mail) — теж файл, раніше його не було видно.
bool fileAttachment(Map<String, dynamic> a) => !galleryImage(a) && !(a['inline'] == true && a['image'] == true);

/// Назва файлу для телефона: без розширення програма телефона файл не відкриє — додати за типом.
String attachmentFileName(String name, String type) {
  var n = name.trim().isEmpty ? 'file' : name.trim();
  n = n.replaceAll(RegExp(r'[\\/:*?"<>|]'), '_');
  if (RegExp(r'\.[A-Za-z0-9]{1,5}$').hasMatch(n)) return n;
  const ext = {
    'application/pdf': 'pdf', 'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp',
    'image/heic': 'heic', 'image/heif': 'heif', 'image/tiff': 'tif', 'image/bmp': 'bmp',
    'message/rfc822': 'eml', 'text/plain': 'txt', 'text/csv': 'csv', 'text/html': 'html', 'text/calendar': 'ics',
    'application/zip': 'zip', 'application/x-zip-compressed': 'zip', 'application/msword': 'doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.ms-excel': 'xls', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.ms-powerpoint': 'ppt',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
  };
  final e = ext[type.split(';').first.trim().toLowerCase()];
  return e == null ? n : '$n.$e';
}

String _thumbUrl(Api api, int messageId, Map<String, dynamic> a, int width) =>
    api.uri('messages/$messageId/attachments/${a['i']}/thumb/', {'w': '$width'}).toString();

/// Рядок мініатюр під текстом листа.
class ImageStrip extends StatelessWidget {
  const ImageStrip({super.key, required this.api, required this.messageId, required this.images, required this.onOpenFile});

  final Api api;
  final int messageId;
  final List<Map<String, dynamic>> images;
  final void Function(Map<String, dynamic> a) onOpenFile;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final ratio = MediaQuery.of(context).devicePixelRatio;
    return SizedBox(
      height: 104,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
        itemCount: images.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (c, i) {
          final a = images[i];
          return Semantics(
            button: true,
            label: a['name'] as String? ?? '',
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: () => Navigator.of(c).push(MaterialPageRoute(
                  builder: (_) => ImageViewer(api: api, messageId: messageId, images: images, start: i, onOpenFile: onOpenFile))),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(10),
                child: Container(
                  width: 96,
                  height: 96,
                  color: scheme.surfaceContainerHighest,
                  child: Image.network(
                    _thumbUrl(api, messageId, a, 320),
                    headers: api.headers,
                    fit: BoxFit.cover,
                    cacheWidth: (96 * ratio).round(),
                    loadingBuilder: (_, child, p) =>
                        p == null ? child : const Center(child: SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2))),
                    errorBuilder: (_, __, ___) => _Unavailable(name: a['name'] as String? ?? ''),
                  ),
                ),
              ),
            ),
          );
        },
      ),
    );
  }
}

class _Unavailable extends StatelessWidget {
  const _Unavailable({required this.name});

  final String name;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.all(6),
      child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        Icon(Icons.image_outlined, color: scheme.onSurfaceVariant),
        const SizedBox(height: 4),
        Text(name, maxLines: 2, overflow: TextOverflow.ellipsis, textAlign: TextAlign.center, style: const TextStyle(fontSize: 10.5)),
      ]),
    );
  }
}

/// Картинки листа на весь екран: гортати, збільшувати пальцями, «Відкрити файл» — оригінал у програмі телефона.
class ImageViewer extends StatefulWidget {
  const ImageViewer(
      {super.key, required this.api, required this.messageId, required this.images, required this.start, required this.onOpenFile});

  final Api api;
  final int messageId;
  final List<Map<String, dynamic>> images;
  final int start;
  final void Function(Map<String, dynamic> a) onOpenFile;

  @override
  State<ImageViewer> createState() => _ImageViewerState();
}

class _ImageViewerState extends State<ImageViewer> {
  late final PageController pages = PageController(initialPage: widget.start);
  late int index = widget.start;

  @override
  void dispose() {
    pages.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final a = widget.images[index];
    final width = (MediaQuery.of(context).size.width * MediaQuery.of(context).devicePixelRatio * 1.5).round().clamp(800, 2400);
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.black,
        foregroundColor: Colors.white,
        title: Text(
          widget.images.length > 1 ? '${index + 1} / ${widget.images.length}  ·  ${a['name']}' : '${a['name']}',
          style: const TextStyle(fontSize: 15),
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          IconButton(
            tooltip: context.t('open_file'),
            icon: const Icon(Icons.open_in_new),
            onPressed: () => widget.onOpenFile(a),
          ),
        ],
      ),
      body: PageView.builder(
        controller: pages,
        itemCount: widget.images.length,
        onPageChanged: (i) => setState(() => index = i),
        itemBuilder: (c, i) => InteractiveViewer(
          maxScale: 6,
          child: Center(
            child: Image.network(
              _thumbUrl(widget.api, widget.messageId, widget.images[i], width),
              headers: widget.api.headers,
              fit: BoxFit.contain,
              loadingBuilder: (_, child, p) => p == null ? child : const CircularProgressIndicator(color: Colors.white),
              errorBuilder: (_, __, ___) => Padding(
                padding: const EdgeInsets.all(24),
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  const Icon(Icons.image_not_supported_outlined, color: Colors.white70, size: 48),
                  const SizedBox(height: 12),
                  Text(c.t('image_unavailable'), textAlign: TextAlign.center, style: const TextStyle(color: Colors.white70)),
                  const SizedBox(height: 12),
                  FilledButton.icon(
                    onPressed: () => widget.onOpenFile(widget.images[i]),
                    icon: const Icon(Icons.open_in_new),
                    label: Text(c.t('open_file')),
                  ),
                ]),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
