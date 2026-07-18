import logging
from apscheduler.schedulers.background import BackgroundScheduler
from django.utils import timezone

logger = logging.getLogger(__name__)

def release_expired_reservations():
    from .models import Seat, Order
    now = timezone.now()
    try:
        # Unlock seats where reservation is expired and they haven't been booked
        expired_seats = Seat.objects.filter(is_booked=False, reserved_until__lt=now)
        count = expired_seats.count()
        if count > 0:
            expired_seats.update(reserved_by=None, reserved_until=None)
            logger.info(f"Released {count} expired seat reservations in background.")
            
        # Mark pending orders older than 2 minutes as EXPIRED
        from datetime import timedelta
        expired_time = now - timedelta(minutes=2)
        expired_orders = Order.objects.filter(status='PENDING', created_at__lt=expired_time)
        order_count = expired_orders.count()
        if order_count > 0:
            expired_orders.update(status='EXPIRED')
            logger.info(f"Marked {order_count} pending orders as EXPIRED in background.")
    except Exception as e:
        logger.error(f"Error in background seat release job: {e}")

def start():
    scheduler = BackgroundScheduler()
    # Check every 30 seconds
    scheduler.add_job(
        release_expired_reservations, 
        'interval', 
        seconds=30, 
        id='release_expired_reservations_job', 
        replace_existing=True
    )
    scheduler.start()
    logger.info("Background Seat Release Scheduler started.")
