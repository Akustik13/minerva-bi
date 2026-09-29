"""Утиліти для роботи з БОМ та складом."""
from decimal import Decimal
from django.db.models import Sum
from .models import Product, ProductComponent, InventoryTransaction, Location
from django.utils import timezone


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


def deduct_components_for_assembly(product, qty, ref_doc="", order_pk=None):
    """
    Вилучити компоненти зі складу для збирання.

    Args:
        product: готовий товар з BOM
        qty: кількість готових товарів для збирання
        ref_doc: посилання (номер замовлення)
        order_pk: PK замовлення

    Returns:
        dict з результатом: {'ok': bool, 'error': str, 'transactions': [ids]}
    """
    if product.bom_type != Product.BomType.KEY:
        return {'ok': False, 'error': 'Товар не має BOM'}

    components = ProductComponent.objects.filter(
        parent=product,
        optional=False
    ).select_related('component')

    if not components.exists():
        return {'ok': False, 'error': 'BOM пусто'}

    # Перевірка: чи достатньо компонентів
    for comp in components:
        stock = get_product_stock(comp.component)
        needed = Decimal(str(comp.qty_per)) * Decimal(str(qty))
        if stock < needed:
            return {
                'ok': False,
                'error': f'Недостатньо {comp.component.sku}: є {float(stock)}, потрібно {float(needed)}'
            }

    # Вилучення компонентів
    transactions = []
    try:
        # Знайти склад компонентів або інший
        comp_location = Location.objects.filter(
            location_type=Location.LocationType.COMPONENTS,
            is_active=True
        ).first() or Location.objects.filter(
            is_active=True
        ).first()

        if not comp_location:
            return {'ok': False, 'error': 'Не знайдено активного складу'}

        for comp in components:
            qty_to_deduct = Decimal(str(comp.qty_per)) * Decimal(str(qty))

            # Створити транзакцію вилучення
            external_key = f"bom_deduction_{product.id}_{order_pk}_{comp.component.id}"

            tx = InventoryTransaction.objects.create(
                tx_type=InventoryTransaction.TxType.OUTGOING,
                product=comp.component,
                location=comp_location,
                qty=-qty_to_deduct,  # від'ємна для вилучення
                ref_doc=ref_doc or f"BOM для {product.sku}",
                external_key=external_key,
                tx_date=timezone.now(),
            )
            transactions.append(tx.id)

        return {'ok': True, 'transactions': transactions}

    except Exception as e:
        return {'ok': False, 'error': str(e)}


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
        after = int(comp_info['stock'] - (comp_info['qty_per'] * qty))
        if after < 0:
            after = 0
        leftover[comp_info['product'].sku] = {
            'before': int(comp_info['stock']),
            'after': after,
            'used': int(comp_info['qty_per'] * qty)
        }

    return {
        'can_assemble': True,
        'buildable': analysis['buildable_qty'],
        'after_assembly': leftover
    }
