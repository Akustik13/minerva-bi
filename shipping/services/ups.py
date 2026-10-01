"""
shipping/services/ups.py — UPS API інтеграція.
Підтримує: Shipment API, Paperless Document API, Track API.
"""
import json
import logging
import requests
from base64 import b64encode
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from .base import BaseCarrierService, ShipmentResult

logger = logging.getLogger(__name__)


class UPSAuthToken:
    """Управління OAuth токеном для UPS."""

    def __init__(self, carrier):
        self.carrier = carrier
        self.token = None
        self.token_expires = None

    def get_token(self) -> str:
        """Повертає валідний OAuth токен, оновлює за потребою."""
        if self.token and self.token_expires and datetime.now() < self.token_expires:
            return self.token

        self.token = self._request_token()
        self.token_expires = datetime.now() + timedelta(hours=1)
        return self.token

    def _request_token(self) -> str:
        """Запитує новий токен з UPS OAuth."""
        url = self._get_oauth_url() + "/oauth/authorize"
        auth_str = f"{self.carrier.api_key}:{self.carrier.api_secret}"
        auth_b64 = b64encode(auth_str.encode()).decode()

        headers = {
            "Authorization": f"Basic {auth_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials"}

        try:
            resp = requests.post(url, headers=headers, data=data, timeout=10)
            resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception as e:
            logger.error(f"UPS OAuth failed: {e}")
            raise

    def _get_oauth_url(self) -> str:
        """Повертає URL OAuth сервера."""
        if self.carrier.api_url == "sandbox":
            return "https://wwwcie.ups.com"
        return "https://onlinetools.ups.com"


class UPSService(BaseCarrierService):
    """UPS API сервіс."""

    def __init__(self, carrier):
        super().__init__(carrier)
        self.auth = UPSAuthToken(carrier)

    def create_shipment(self, shipment) -> ShipmentResult:
        """Створює відправлення на UPS."""
        try:
            # Крок 1: Якщо використовується custom invoice — завантажити PDF
            document_id = None
            if shipment.use_custom_invoice and shipment.custom_invoice_pdf:
                document_id = self._upload_custom_invoice(shipment)
                if not document_id:
                    return ShipmentResult(
                        success=False,
                        error_message="Failed to upload custom invoice to UPS Paperless API"
                    )
                shipment.ups_document_id = document_id

            # Крок 2: Створити Shipment API запит
            payload = self._build_shipment_request(shipment, document_id)

            # Крок 3: Надіслати запит
            result = self._send_shipment_request(payload, shipment)

            # Крок 4: Зберегти результат
            if result.success:
                shipment.carrier_shipment_id = result.carrier_shipment_id
                shipment.tracking_number = result.tracking_number
                shipment.label_url = result.label_url
                shipment.customs_url = result.customs_url
                shipment.carrier_price = result.carrier_price
                shipment.carrier_currency = result.carrier_currency
                shipment.carrier_service = result.carrier_service
                shipment.status = "LABEL_READY"

            return result

        except Exception as e:
            logger.exception(f"UPS create_shipment failed: {e}")
            return ShipmentResult(
                success=False,
                error_message=str(e)
            )

    def _upload_custom_invoice(self, shipment) -> Optional[str]:
        """Завантажує свій PDF на UPS Paperless Document API."""
        if not shipment.custom_invoice_pdf:
            return None

        try:
            # Читаємо PDF файл
            pdf_data = shipment.custom_invoice_pdf.read()

            # Підготуємо запит до Paperless API
            token = self.auth.get_token()
            url = self._get_api_url() + "/paperlessDocuments"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "transactionId": self._generate_transaction_id(),
            }

            # Кодуємо PDF в base64
            pdf_b64 = b64encode(pdf_data).decode()

            payload = {
                "documents": [
                    {
                        "DocumentType": "07",  # Customer Generated Forms
                        "DocumentFormat": "PDF",
                        "DocumentSize": len(pdf_data),
                        "Document": pdf_b64,
                    }
                ]
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()

            response_data = resp.json()

            # UPS повертає Document ID в response
            if "documents" in response_data and len(response_data["documents"]) > 0:
                doc_id = response_data["documents"][0].get("DocumentID")
                logger.info(f"Uploaded custom invoice to UPS: {doc_id}")
                return doc_id
            else:
                logger.warning(f"No Document ID in UPS response: {response_data}")
                return None

        except Exception as e:
            logger.error(f"Failed to upload custom invoice: {e}")
            return None

    def _build_shipment_request(self, shipment, document_id: Optional[str]) -> dict:
        """Будує Shipment API запит."""

        # Отримуємо дані відправника та отримувача
        sender = self._get_sender_data(shipment)
        recipient = self._get_recipient_data(shipment)

        # Базовий структурований запит
        payload = {
            "ShipmentRequest": {
                "Request": {
                    "RequestOption": "nonvalidate",
                    "TransactionReference": {
                        "CustomerContext": f"Shipment for order {shipment.reference}",
                        "TransactionIdentifier": self._generate_transaction_id(),
                    }
                },
                "Shipment": {
                    "Description": shipment.description or "Package",
                    "Shipper": sender,
                    "ShipTo": recipient,
                    "InvoiceLineTotal": {
                        "CurrencyCode": shipment.declared_currency or "EUR",
                        "MonetaryValue": str(shipment.declared_value or 0)
                    },
                    "Service": {
                        "Code": "11",  # UPS Standard
                        "Description": shipment.carrier_service or "UPS Standard"
                    },
                    "Package": {
                        "PackagingType": {
                            "Code": "02",  # Package
                        },
                        "Dimensions": {
                            "UnitOfMeasurement": {
                                "Code": "CM"
                            },
                            "Length": str(shipment.length_cm or 30),
                            "Width": str(shipment.width_cm or 20),
                            "Height": str(shipment.height_cm or 15),
                        },
                        "PackageWeight": {
                            "UnitOfMeasurement": {
                                "Code": "KG"
                            },
                            "Weight": str(shipment.weight_kg)
                        },
                        "PackageServiceOptions": {}
                    },
                    "ShipmentServiceOptions": {
                        "Notification": {
                            "NotificationCode": "012",
                            "EMail": {
                                "EMailAddress": shipment.recipient_email
                            }
                        }
                    }
                }
            }
        }

        # Додаємо платника
        payload["ShipmentRequest"]["Shipment"]["PaymentInformation"] = self._get_payment_info(shipment)

        # Додаємо Customs Information для міжнародних посилок
        if self._is_international_shipment(shipment):
            payload["ShipmentRequest"]["Shipment"]["InternationalForms"] = {
                "FormType": ["01"],  # Invoice
                "InvoiceNumber": shipment.reference,
                "InvoiceDate": datetime.now().strftime("%Y%m%d"),
                "PurposeOfShipment": self._get_purpose_of_shipment(shipment),
                "Product": self._build_product_lines(shipment),
            }

            # Якщо є custom document ID — додаємо його
            if document_id:
                payload["ShipmentRequest"]["Shipment"]["InternationalForms"]["DocumentID"] = document_id

        return payload

    def _get_sender_data(self, shipment) -> dict:
        """Готує дані відправника."""
        # Приоритет: дані Shipment > дані Carrier
        name = shipment.sender_name or self.carrier.sender_name or "Shipper"
        company = shipment.sender_company or self.carrier.sender_company

        return {
            "Name": name,
            "CompanyName": company or name,
            "PhoneNumber": shipment.sender_phone or self.carrier.sender_phone,
            "EMailAddress": shipment.sender_email or self.carrier.sender_email,
            "Address": {
                "AddressLine": shipment.sender_street or self.carrier.sender_street,
                "City": shipment.sender_city or self.carrier.sender_city,
                "StateProvinceCode": shipment.sender_state or self.carrier.sender_state or "",
                "PostalCode": shipment.sender_zip or self.carrier.sender_zip,
                "CountryCode": shipment.sender_country or self.carrier.sender_country or "DE",
            }
        }

    def _get_recipient_data(self, shipment) -> dict:
        """Готує дані отримувача."""
        return {
            "Name": shipment.recipient_name,
            "CompanyName": shipment.recipient_company or shipment.recipient_name,
            "PhoneNumber": shipment.recipient_phone,
            "EMailAddress": shipment.recipient_email,
            "Address": {
                "AddressLine": shipment.recipient_street,
                "City": shipment.recipient_city,
                "StateProvinceCode": shipment.recipient_state or "",
                "PostalCode": shipment.recipient_zip,
                "CountryCode": shipment.recipient_country,
            }
        }

    def _get_payment_info(self, shipment) -> dict:
        """Повертає інформацію про платника."""
        if shipment.ups_billing == "shipper":
            return {
                "ShipmentCharge": {
                    "Type": "01",  # BillShipper
                    "BillShipper": {
                        "AccountNumber": self.carrier.connection_uuid
                    }
                }
            }
        elif shipment.ups_billing == "receiver":
            return {
                "ShipmentCharge": {
                    "Type": "02",  # BillReceiver
                    "BillReceiver": {
                        "AccountNumber": shipment.ups_billing_account,
                        "Address": {
                            "PostalCode": shipment.ups_billing_postal,
                            "CountryCode": shipment.ups_billing_country,
                        }
                    }
                }
            }
        else:  # third_party
            return {
                "ShipmentCharge": {
                    "Type": "03",  # BillThirdParty
                    "BillThirdPartyCharges": {
                        "AccountNumber": shipment.ups_billing_account,
                        "Address": {
                            "PostalCode": shipment.ups_billing_postal,
                            "CountryCode": shipment.ups_billing_country,
                        }
                    }
                }
            }

    def _is_international_shipment(self, shipment) -> bool:
        """Перевіряє чи це міжнародне відправлення."""
        sender_country = shipment.sender_country or self.carrier.sender_country or "DE"
        return sender_country != shipment.recipient_country

    def _get_purpose_of_shipment(self, shipment) -> str:
        """Повертає код мети експорту."""
        mapping = {
            "Commercial": "SALE",
            "Gift": "GIFT",
            "Sample": "SAMPLE",
            "Return": "RETURN",
            "Repair": "REPAIR",
            "Personal": "RETURN",
            "Other": "OTHER",
        }
        return mapping.get(shipment.export_reason, "SALE")

    def _build_product_lines(self, shipment) -> list:
        """Будує список товарів для Customs Form."""
        products = []

        if shipment.order:
            for line in shipment.order.lines.all():
                products.append({
                    "Description": line.product_name or line.sku,
                    "Unit": {
                        "Number": line.quantity,
                        "Value": float(line.unit_price or 0),
                    },
                    "CommodityCode": "",
                    "CountryOfOrigin": self.carrier.sender_country or "DE",
                })

        return products or [{
            "Description": shipment.description or "Mixed goods",
            "Unit": {
                "Number": 1,
                "Value": float(shipment.declared_value or 0),
            },
            "CountryOfOrigin": self.carrier.sender_country or "DE",
        }]

    def _send_shipment_request(self, payload: dict, shipment) -> ShipmentResult:
        """Надсилає запит до UPS Shipment API."""
        try:
            token = self.auth.get_token()
            url = self._get_api_url() + "/shipments"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "transactionId": self._generate_transaction_id(),
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()

            response_data = resp.json()
            logger.info(f"UPS Shipment API response: {json.dumps(response_data, indent=2)}")

            # Парсимо відповідь
            ship_resp = response_data.get("ShipmentResponse", {})

            if ship_resp.get("Response", {}).get("ResponseStatus", {}).get("Code") == "1":
                # Успіх
                results = ship_resp.get("ShipmentResults", {})

                return ShipmentResult(
                    success=True,
                    carrier_shipment_id=results.get("ShipmentIdentificationNumber"),
                    tracking_number=results.get("ShipmentIdentificationNumber"),
                    label_url=results.get("PackageResults", [{}])[0].get("LabelURL", ""),
                    carrier_price=float(results.get("ShipmentCharges", {}).get("TotalCharges", {}).get("MonetaryValue", 0)),
                    carrier_currency=results.get("ShipmentCharges", {}).get("TotalCharges", {}).get("CurrencyCode", "EUR"),
                    carrier_service=shipment.carrier_service or "UPS Standard",
                    raw_request=payload,
                    raw_response=response_data,
                )
            else:
                # Помилка від UPS
                error = ship_resp.get("Response", {}).get("Error", [{}])[0]
                error_msg = f"{error.get('ErrorCode')}: {error.get('ErrorDescription')}"

                return ShipmentResult(
                    success=False,
                    error_message=error_msg,
                    raw_request=payload,
                    raw_response=response_data,
                )

        except Exception as e:
            logger.exception(f"UPS Shipment API request failed: {e}")
            return ShipmentResult(
                success=False,
                error_message=str(e),
                raw_request=payload,
            )

    def _get_api_url(self) -> str:
        """Повертає базовий URL API."""
        if self.carrier.api_url == "sandbox":
            return "https://wwwcie.ups.com/ship/v1"
        return "https://onlinetools.ups.com/ship/v1"

    @staticmethod
    def _generate_transaction_id() -> str:
        """Генерує Transaction ID для UPS."""
        import uuid
        return str(uuid.uuid4())[:16]

    def track(self, tracking_number: str) -> dict:
        """Отримує інформацію про трекінг від UPS."""
        try:
            token = self.auth.get_token()
            url = f"{self._get_api_url().replace('/ship/v1', '/track/v1')}/details/{tracking_number}"

            headers = {
                "Authorization": f"Bearer {token}",
                "transactionId": self._generate_transaction_id(),
            }

            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()

            return resp.json()

        except Exception as e:
            logger.error(f"UPS tracking failed for {tracking_number}: {e}")
            return {}

    def cancel(self, shipment) -> bool:
        """Скасовує відправлення на UPS."""
        try:
            token = self.auth.get_token()
            url = f"{self._get_api_url()}/shipments/{shipment.carrier_shipment_id}/void"

            headers = {
                "Authorization": f"Bearer {token}",
                "transactionId": self._generate_transaction_id(),
            }

            payload = {
                "VoidShipmentRequest": {
                    "Request": {
                        "RequestOption": "nonvalidate",
                    }
                }
            }

            resp = requests.put(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()

            return True

        except Exception as e:
            logger.error(f"UPS void shipment failed: {e}")
            return False
