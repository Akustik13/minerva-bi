"""Підказки в пошуку сайту магазину — логіка в shop/search_assist.py.

GET /shop/search/assist/?config=1             → {mode}: off | rules | ai | rag | ai_rag (режим з картки магазину)
GET /shop/search/assist/?q=…&n=…&skus=…&lang=…&ai=0|1&rag=0|1&log=0|1
    → {mode, together[{sku, orders}], ai{status, tips[{sku, reason}], cached}?, rag{status, tips, note, cached}?}
    n — скільки знайшов сайт (журнал «що шукають»), skus — перші результати сайту (для «часто купують разом»).
GET /shop/search/assist/?q=…&lang=…&rag=1&poll=1 → {mode, rag} — опитування відповіді RAG (status pending
    {stage, elapsed} → ok | busy | unavailable …), без журналу, «разом» і AI.
Лише ключ, прив'язаний до магазину (права products:read).
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .site_admin import _bound_shop


class ShopSearchAssistView(APIView):
    resource_scope = "products"

    def get(self, request):
        from shop import search_assist
        from shop.models import Shop
        shop = _bound_shop(request)
        if shop is None:
            return Response({"detail": "Ключ сайту не прив'язаний до магазину.", "code": "no_shop"},
                            status=status.HTTP_403_FORBIDDEN)
        p = request.query_params
        out = {"mode": shop.search_assist}
        if shop.search_assist == Shop.SEARCH_OFF or p.get("config") == "1":
            return Response(out)
        q, skus = p.get("q", ""), search_assist.clean_skus(p.get("skus", ""))
        n = p.get("n", "")
        if p.get("poll") != "1":
            if p.get("log") != "0" and n.isdigit():
                search_assist.record(shop, q, int(n))
            out["together"] = search_assist.together(shop, skus)
            if p.get("ai") == "1":
                out["ai"] = search_assist.ai_tips(shop, q, skus, p.get("lang", ""))
        if p.get("rag") == "1":
            out["rag"] = search_assist.rag_tips(shop, q, p.get("lang", ""))
        return Response(out)
