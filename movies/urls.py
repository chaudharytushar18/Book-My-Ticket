from django.urls import path
from . import views

urlpatterns=[
    path('', views.movie_list, name='movie_list'),
    path('<int:movie_id>/theaters/', views.theater_list, name='theater_list'),
    path('theater/<int:theater_id>/seats/book/', views.book_seats, name='book_seats'),
    
    # Seat lock payment confirmation & checkout routes
    path('order/<str:order_id>/pay/', views.confirm_payment, name='confirm_payment'),
    path('order/<str:order_id>/create-checkout-session/', views.create_checkout_session, name='create_checkout_session'),
    path('order/<str:order_id>/mock-pay/', views.mock_payment, name='mock_payment'),
    
    # Success / Cancel page callbacks
    path('payment/success/', views.payment_success, name='payment_success'),
    path('payment/cancel/', views.payment_cancel, name='payment_cancel'),
    
    # Webhook endpoint
    path('payment/webhook/', views.stripe_webhook, name='stripe_webhook'),
    
    # Admin Analytics Dashboard
    path('admin/dashboard/', views.admin_analytics_dashboard, name='admin_analytics_dashboard'),
]