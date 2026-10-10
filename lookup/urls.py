from django.urls import path, re_path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('lookup/', views.lookup, name='lookup'),
    path('unlock/', views.unlock, name='unlock'),
    re_path(r'^(?P<reg>(?=[A-Z0-9]*\d)[A-Z0-9]{2,7})/insurance$',
            views.insurance_check, name='insurance'),
    re_path(r'^(?=[^/]*\d)(?P<reg>[^/]+)/?$', views.result, name='result'),
]
