"""
shipping/services/registry.py — Реєстр сервісів перевізників.
Повертає потрібний сервіс за типом перевізника.
"""
from .base import BaseCarrierService
from .jumingo import JumingoService
from .ups import UPSService


def get_service(carrier) -> BaseCarrierService:
    """Повертає сервіс для заданого Carrier."""
    services = {
        "jumingo": JumingoService,
        "ups": UPSService,
    }
    cls = services.get(carrier.carrier_type, BaseCarrierService)
    return cls(carrier)
