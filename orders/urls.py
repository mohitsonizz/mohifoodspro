from django.urls import path
from . import views

urlpatterns = [
    path('place_order/', views.place_order, name='place_order'),
    path('payments/', views.payments, name='payments'),
    path('order_complete/', views.order_complete, name='order_complete'),
    path('start_payment/', views.start_payment, name='start_payment'),
    path('verify_payment/', views.verify_payment, name='verify_payment'),
]
