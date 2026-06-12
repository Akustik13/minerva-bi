"""
bots/services/digikey_messages.py — DigiKey Marketplace Messages API

Endpoints:
  GET  /Sales/Marketplace2/Messages/v1/messages           — list topics
  GET  /Sales/Marketplace2/Messages/v1/messages/{id}      — full topic + conversation
  POST /Sales/Marketplace2/Messages/v1/messages           — create topic
  POST /Sales/Marketplace2/Messages/v1/messages/{id}/Conversation — reply
"""
import logging

logger = logging.getLogger(__name__)

_MESSAGES_BASE = "/Sales/Marketplace2/Messages/v1/messages"


def _base_url(config) -> str:
    from .digikey import _PROD_BASE, _SANDBOX_BASE
    return _SANDBOX_BASE if config.use_sandbox else _PROD_BASE


def _headers(config, token: str) -> dict:
    return {
        "Authorization":       f"Bearer {token}",
        "X-DIGIKEY-Client-Id": config.client_id,
        "Content-Type":        "application/json",
    }


def get_topics(config, token: str, order_id: str = None,
               offset: int = 0, max_results: int = 50) -> dict:
    """
    GET /messages — список тем (розмов).
    order_id: UUID замовлення DigiKey (OrderId query param).
    """
    import requests as req
    params: dict = {"Offset": offset, "Max": min(max_results, 100)}
    if order_id:
        params["OrderId"] = order_id
    url = f"{_base_url(config)}{_MESSAGES_BASE}"
    try:
        resp = req.get(url, headers=_headers(config, token), params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.exception("[DK Messages] get_topics failed")
        raise


def get_topic(config, token: str, topic_id: str) -> dict:
    """GET /messages/{topicId} — повна розмова."""
    import requests as req
    url = f"{_base_url(config)}{_MESSAGES_BASE}/{topic_id}"
    try:
        resp = req.get(url, headers=_headers(config, token), timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.exception("[DK Messages] get_topic failed: %s", topic_id)
        raise


def reply(config, token: str, topic_id: str, content: str,
          sender: str = "Supplier", recipient: str = "Customer") -> dict:
    """POST /messages/{topicId}/Conversation — надіслати повідомлення."""
    import requests as req
    url = f"{_base_url(config)}{_MESSAGES_BASE}/{topic_id}/Conversation"
    body = {"content": content[:2500], "sender": sender, "recipient": recipient}
    try:
        resp = req.post(url, headers=_headers(config, token), json=body, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.exception("[DK Messages] reply failed: %s", topic_id)
        raise


def create_topic(config, token: str, order_id: str, topic_title: str,
                 content: str, sender: str = "Supplier",
                 recipient: str = "Customer") -> dict:
    """POST /messages — створити нову тему розмови."""
    import requests as req
    url = f"{_base_url(config)}{_MESSAGES_BASE}"
    body = {
        "topic":   topic_title[:50],
        "orderId": order_id,
        "owner":   "Supplier",
        "initMessage": {
            "content":   content[:2500],
            "sender":    sender,
            "recipient": recipient,
        },
    }
    try:
        resp = req.post(url, headers=_headers(config, token), json=body, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.exception("[DK Messages] create_topic failed")
        raise


def get_all_topics_paginated(config, token: str, max_total: int = 200) -> list:
    """Завантажує всі теми (з пагінацією), до max_total."""
    all_topics = []
    offset = 0
    page_size = 50
    while len(all_topics) < max_total:
        data = get_topics(config, token, offset=offset, max_results=page_size)
        items = data.get("messageTopicItems", []) if isinstance(data, dict) else data
        if not items:
            break
        all_topics.extend(items)
        if len(items) < page_size:
            break
        offset += page_size
    return all_topics[:max_total]
