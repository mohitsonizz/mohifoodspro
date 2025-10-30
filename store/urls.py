from django.urls import path
from . import views

urlpatterns = [
    path('', views.store, name='store'),
    
    # --- SPECIFIC URLs MOVED UP ---
    path('search/', views.search, name='search'),
    path('category/<slug:category_slug>/<slug:product_slug>/', views.product_detail, name='product_detail'),
    path('submit_review/<int:product_id>/', views.submit_review, name='submit_review'),

    # --- GENERAL SLUG URL MOVED TO THE END ---
    path('<slug:category_slug>/', views.store, name='products_by_category'),
]