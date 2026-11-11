"""Who may use the bench: address allow-list, trusted proxies, same-origin checks.

The bench has no logins yet (stage 1 gets one-time keys from Minerva), so it only answers
addresses listed in config and refuses cross-site requests from other pages in the browser.
"""
import ipaddress
from urllib.parse import urlsplit


def _networks(items):
    nets = []
    for item in items or ():
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            continue
    return nets


def _in(address, nets):
    try:
        ip = ipaddress.ip_address((address or "").strip().split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in nets)


def client_ip(remote, headers, trusted_proxies):
    """Address of the person: proxy headers only when the direct peer is a trusted proxy."""
    remote = remote or ""
    if _in(remote, _networks(trusted_proxies)):
        real = (headers.get("X-Real-IP") or "").strip()
        if real:
            return real
        forwarded = headers.get("X-Forwarded-For") or ""
        if forwarded.strip():
            return forwarded.split(",")[-1].strip()
    return remote


def allowed(address, allowed_clients):
    return _in(address, _networks(allowed_clients))


def same_origin(headers, scheme, via_proxy=False):
    """Origin header must name this very host (browsers always send it on POST and WebSocket).

    Behind a trusted proxy the public host may come as X-Forwarded-Host instead of Host.
    """
    origin = headers.get("Origin") or ""
    hosts = {(headers.get("Host") or "").lower()}
    if via_proxy and headers.get("X-Forwarded-Host"):
        hosts.add(headers["X-Forwarded-Host"].split(",")[0].strip().lower())
    hosts.discard("")
    if not origin or not hosts:
        return False
    parts = urlsplit(origin)
    return parts.scheme == scheme and parts.netloc.lower() in hosts
