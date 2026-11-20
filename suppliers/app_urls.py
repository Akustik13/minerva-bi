"""/suppliers/app/api/… — 📱 додаток «Minerva Візитки» (suppliers/app_api.py)."""
from django.urls import path

from suppliers import app_api

urlpatterns = [
    path("api/pair/", app_api.pair, name="cards_app_pair"),
    path("api/pair/info/", app_api.pair_info, name="cards_app_pair_info"),
    path("", app_api.download_page, name="cards_app_download"),
    path("minerva-cards.apk", app_api.download_apk, name="cards_app_apk"),
    path("api/me/", app_api.me, name="cards_app_me"),
    path("api/meta/", app_api.meta, name="cards_app_meta"),
    path("api/categories/", app_api.categories, name="cards_app_categories"),
    path("api/categories/<int:pk>/", app_api.categories, name="cards_app_category"),
    path("api/presets/", app_api.presets, name="cards_app_presets"),
    path("api/cards/", app_api.cards, name="cards_app_cards"),
    path("api/cards/<int:pk>/", app_api.card, name="cards_app_card"),
    path("api/cards/<int:pk>/read/", app_api.card_read, name="cards_app_card_read"),
    path("api/cards/<int:pk>/to-supplier/", app_api.card_to_supplier, name="cards_app_card_to_supplier"),
    path("api/cards/<int:pk>/to-customer/", app_api.card_to_customer, name="cards_app_card_to_customer"),
    path("api/cards/<int:pk>/delete/", app_api.card_delete, name="cards_app_card_delete"),
    path("api/cards/<int:pk>/image/", app_api.card_image, name="cards_app_card_image"),
    path("api/cards/<int:pk>/side/", app_api.card_side, name="cards_app_card_side"),
    path("api/cards/<int:pk>/photos/", app_api.card_photos_add, name="cards_app_card_photos"),
    path("api/cards/<int:pk>/photos/<int:photo_pk>/delete/", app_api.card_photo_delete,
         name="cards_app_card_photo_delete"),
]
