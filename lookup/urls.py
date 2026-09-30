from django.urls import path, re_path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('lookup/', views.lookup, name='lookup'),
    path('unlock/', views.unlock, name='unlock'),
    # /AB12CDE/insurance: the on-demand askMID check
    re_path(r'^(?=[^/]*\d)(?P<reg>[^/]+)/insurance$', views.insurance,
            name='insurance'),
    # /AB12CDE shows that vehicle. Only single path segments containing a
    # digit get here (every UK reg has one), so /admin and bots' guesses 404.
    re_path(r'^(?=[^/]*\d)(?P<reg>[^/]+)/?$', views.result, name='result'),
]
