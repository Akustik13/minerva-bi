import 'package:flutter/material.dart';

import '../l10n.dart';
import '../state.dart';
import '../util.dart';

/// Кружок відправника: логотип / свій значок з Minerva (той самий, що в скриньці) або перша буква.
class SenderAvatar extends StatelessWidget {
  const SenderAvatar({super.key, required this.name, this.logo = '', this.logoV = '', this.size = 44, this.selected = false});

  final String name;
  final String logo;
  final String logoV;
  final double size;
  final bool selected;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    if (selected) {
      return CircleAvatar(
        radius: size / 2,
        backgroundColor: scheme.primary,
        child: Icon(Icons.check, color: scheme.onPrimary, size: size * 0.55),
      );
    }
    final letter = CircleAvatar(
      radius: size / 2,
      backgroundColor: avatarColor(name),
      child: Text(initial(name),
          style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: size * 0.42)),
    );
    final api = AppScope.read(context).api;
    if (logo.isEmpty || api == null) return letter;
    return SizedBox(
      width: size,
      height: size,
      child: ClipOval(
        child: Container(
          color: logoV.startsWith('i') ? Colors.transparent : Colors.white,
          child: Image.network(
            api.logoUrl(logo, logoV),
            headers: api.headers,
            width: size,
            height: size,
            fit: BoxFit.contain,
            errorBuilder: (_, __, ___) => letter,
          ),
        ),
      ),
    );
  }
}

/// Мітка своєї категорії / клієнта / проєкту.
class TagChip extends StatelessWidget {
  const TagChip({super.key, required this.label, this.color, this.icon = ''});

  final String label;
  final Color? color;
  final String icon;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final c = color ?? scheme.secondary;
    return Container(
      margin: const EdgeInsets.only(right: 4, top: 2),
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: c.withValues(alpha: 0.45), width: 0.8),
      ),
      child: Text(icon.isEmpty ? label : '$icon $label',
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: 11.5, color: scheme.onSurface.withValues(alpha: 0.85))),
    );
  }
}

class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.icon, required this.text, this.action, this.onAction});

  final IconData icon;
  final String text;
  final String? action;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 56, color: scheme.outline),
          const SizedBox(height: 12),
          Text(text, textAlign: TextAlign.center, style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 15)),
          if (action != null) ...[
            const SizedBox(height: 16),
            FilledButton.tonal(onPressed: onAction, child: Text(action!)),
          ],
        ]),
      ),
    );
  }
}

/// Кнопка з підписом під значком — великі цілі для пальця в нижній панелі листа.
class BarButton extends StatelessWidget {
  const BarButton({super.key, required this.icon, required this.label, required this.onTap});

  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Icon(icon, size: 24),
            const SizedBox(height: 3),
            Text(label, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12)),
          ]),
        ),
      ),
    );
  }
}

Future<bool> confirm(BuildContext context, String text, {String? ok}) async {
  final r = await showDialog<bool>(
    context: context,
    builder: (c) => AlertDialog(
      content: Text(text),
      actions: [
        TextButton(onPressed: () => Navigator.pop(c, false), child: Text(c.t('cancel'))),
        FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(ok ?? c.t('ok'))),
      ],
    ),
  );
  return r == true;
}
