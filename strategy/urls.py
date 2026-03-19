from django.urls import path
from . import views

app_name = "strategy"

urlpatterns = [
    path("<int:pk>/canvas/",          views.canvas_view,    name="canvas"),
    path("<int:pk>/canvas/data/",     views.canvas_data,    name="canvas_data"),
    path("step/<int:pk>/complete/",   views.step_complete,  name="step_complete"),
    path("step/<int:pk>/position/",   views.step_position,  name="step_position"),
]
