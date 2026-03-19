from django.urls import path
from . import views

app_name = "strategy"

urlpatterns = [
    path("<int:pk>/canvas/",       views.canvas_view,      name="canvas"),
    path("<int:pk>/canvas/data/",  views.canvas_data_view, name="canvas_data"),
    path("<int:pk>/log-step/",     views.log_step_view,    name="log_step"),
]
