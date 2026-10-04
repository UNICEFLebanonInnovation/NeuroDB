from django.urls import path

from . import views

app_name = "watch"

urlpatterns = [
    path("", views.for_you, name="for_you"),
    path("count/", views.badge, name="badge"),
    path("card/", views.card, name="card"),
    path("<int:pk>/react/", views.react, name="react"),
    path("check-now/", views.check_now, name="check_now"),
]
