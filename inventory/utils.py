"""Утиліти для роботи з БОМ та складом."""
from decimal import Decimal
from django.db.models import Sum
from .models import Product, ProductComponent, InventoryTransaction


def get_product_stock(product, location=None):
    """Отримати залишок товару на складі (або на всіх, якщо location не вказана)."""
    query = InventoryTransaction.objects.filter(product=product).exclude(
        tx_type=InventoryTransaction.TxType.RESERVED
    )
    if location:
        query = query.filter(location=location)

    result = query.aggregate(total=Sum('qty'))
    return Decimal(str(result['total'] or 0))


def get_buildable_qty(product):
    """Скільки одиниць товару можна зібрати на основі наявних компонентів."""
    if product.bom_type != Product.BomType.KEY:
        return None

    components = ProductComponent.objects.filter(
        parent=product,
        optional=False
    ).select_related('component')

    if not components.exists():
        return None

    buildable = None
    for comp in components:
        stock = get_product_stock(comp.component)
        possible = int(stock / Decimal(str(comp.qty_per or 1)))
        buildable = possible if buildable is None else min(buildable, possible)

    return buildable or 0


def get_bom_analysis(product):
    """Детальний аналіз БОМ: які компоненти потрібні, скільки є, скільки можна зібрати."""
    if product.bom_type != Product.BomType.KEY:
        return {'has_bom': False}

    components = ProductComponent.objects.filter(
        parent=product
    ).select_related('component')

    analysis = {
        'has_bom': True,
        'components': [],
        'buildable_qty': 0,
        'bottleneck': None,  # компонент який обмежує кількість
    }

    buildable = None
    for comp in components:
        stock = get_product_stock(comp.component)
        possible = int(stock / Decimal(str(comp.qty_per or 1)))

        analysis['components'].append({
            'product': comp.component,
            'qty_per': comp.qty_per,
            'stock': float(stock),
            'buildable': possible,
            'optional': comp.optional,
        })

        if not comp.optional:
            if buildable is None or possible < buildable:
                buildable = possible
                analysis['bottleneck'] = comp.component

    analysis['buildable_qty'] = buildable or 0
    return analysis


# Списання компонентів BOM під замовлення — лише inventory/services/stock.py (sync_order: разом із позицією,
# за order_target; скасування / видалення повертають). Окремої функції списання тут більше немає.


def get_assembly_status(product, qty):
    """Отримати статус: чи можна зібрати задану кількість."""
    analysis = get_bom_analysis(product)

    if not analysis['has_bom']:
        return {'can_assemble': False, 'reason': 'Товар не має BOM'}

    if analysis['buildable_qty'] < qty:
        comp = analysis['bottleneck']
        return {
            'can_assemble': False,
            'reason': f'Обмежено компонентом {comp.sku if comp else "?"}: можна зібрати тільки {analysis["buildable_qty"]}',
            'buildable': analysis['buildable_qty']
        }

    # Розрахувати залишок компонентів після збирання
    leftover = {}
    for comp_info in analysis['components']:
        stock = Decimal(str(comp_info['stock']))
        qty_per = Decimal(str(comp_info['qty_per']))
        used = qty_per * Decimal(str(qty))
        after = int(stock - used)
        if after < 0:
            after = 0
        leftover[comp_info['product'].sku] = {
            'before': int(comp_info['stock']),
            'after': after,
            'used': int(used)
        }

    return {
        'can_assemble': True,
        'buildable': analysis['buildable_qty'],
        'after_assembly': leftover
    }
