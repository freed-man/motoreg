from django.urls import path, re_path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('lookup/', views.lookup, name='lookup'),
    path('unlock/', views.unlock, name='unlock'),
    # a reg box and a button that copies the reg and opens askMID
    path('insurance/', views.insurance_page, name='insurance_page'),
    # /AB12CDE shows that vehicle. Only single path segments containing a
    # digit get here (every UK reg has one), so /admin and bots' guesses 404.
    re_path(r'^(?=[^/]*\d)(?P<reg>[^/]+)/?$', views.result, name='result'),
]
