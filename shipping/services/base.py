"""
shipping/services/base.py — Базовий клас для всіх сервісів доставки.
Кожен перевізник (Jumingo, DHL, UPS, FedEx) реалізує цей інтерфейс.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ShipmentResult:
    """Результат створення відправлення від перевізника."""
    success: bool
    carrier_shipment_id: str = ""
    tracking_number: str     = ""
    label_url: str           = ""
    carrier_price: float     = 0.0
    carrier_currency: str    = "EUR"
    carrier_service: str     = ""
    raw_request: dict        = field(default_factory=dict)
    raw_response: dict       = field(default_factory=dict)
    error_message: str       = ""


class BaseCarrierService:
    """
    Базовий клас сервісу перевізника.
    Підклас реалізує метод create_shipment().
    """

    def __init__(self, carrier):
        self.carrier = carrier

    def create_shipment(self, shipment) -> ShipmentResult:
        """
        Надсилає дані в API перевізника та повертає ShipmentResult.
        Перевизначте в підкласі.
        """
        raise NotImplementedError

    def get_label(self, shipment) -> Optional[bytes]:
        """Завантажує PDF-етикетку. Опціонально."""
        return None

    def track(self, tracking_number: str) -> dict:
        """Перевіряє статус відправлення. Опціонально."""
        return {}

    def cancel(self, shipment) -> bool:
        """Скасовує відправлення. Опціонально."""
        return False
