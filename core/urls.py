from django.urls import path
from . import views

urlpatterns = [
    path('my-settings/', views.my_settings_view, name='my_settings'),
    path('set-language/<str:lang_code>/', views.set_language_view, name='set_language'),
    path('ui-dict/<str:lang>.js', views.ui_dict_view, name='ui_dict'),
    path('mobile-nav/', views.mobile_nav_view, name='mobile_nav'),
    path('menu/', views.main_menu_view, name='main_menu'),
    path('fx-online/', views.fx_online_view, name='core_fx_online'),
    path('panel/<str:model>/prefs/', views.panel_prefs_view, name='side_panel_prefs'),
    path('panel/<str:model>/<int:pk>/block/<str:key>/', views.panel_block_view, name='side_panel_block'),
    path('panel/<str:model>/<int:pk>/edit/', views.panel_edit_view, name='side_panel_edit'),
    path('panel/<str:model>/<int:pk>/', views.panel_view, name='side_panel'),
]
