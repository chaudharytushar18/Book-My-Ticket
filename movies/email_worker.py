import queue
import threading
import time
import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings

logger = logging.getLogger(__name__)

# Thread-safe queue for email tasks
email_queue = queue.Queue()
worker_thread = None

def email_worker_loop():
    """Background loop that processes incoming email jobs."""
    logger.info("Email background worker loop started.")
    while True:
        try:
            task = email_queue.get()
            if task is None:
                # Poison pill to stop thread
                break
            
            order_id = task.get('order_id')
            retries = task.get('retries', 0)
            
            success = process_email_task(order_id)
            if not success:
                if retries < 3:
                    backoff = 5 * (2 ** retries) # 5s, 10s, 20s
                    logger.warning(f"Email delivery failed for order {order_id}. Retrying in {backoff}s (Attempt {retries+1}/3)...")
                    # Schedule retry task by pushing it back to queue after a delay
                    task['retries'] = retries + 1
                    # Start a thread timer to push it back
                    t = threading.Timer(backoff, lambda: email_queue.put(task))
                    t.start()
                else:
                    logger.error(f"Email delivery completely failed for order {order_id} after 3 retries.")
            
            email_queue.task_done()
        except Exception as e:
            logger.error(f"Unexpected error in email worker loop: {e}")

def process_email_task(order_id):
    """Fetches order data, renders templates, and dispatches the email."""
    from .models import Order
    try:
        order = Order.objects.get(order_id=order_id)
        user = order.user
        
        # Prepare context details
        movie_name = order.theater.movie.name
        theater_name = order.theater.name
        show_time = order.theater.time.strftime('%a, %d %b %Y | %I:%M %p')
        seat_numbers = ", ".join(seat.seat_number for seat in order.seats.all())
        amount = str(order.total_price)
        payment_id = order.stripe_session_id or "Simulated/Completed"
        
        context = {
            'movie_name': movie_name,
            'theater_name': theater_name,
            'show_time': show_time,
            'seat_numbers': seat_numbers,
            'amount': amount,
            'payment_id': payment_id,
        }
        
        # Render HTML and text versions
        html_content = render_to_string('movies/email_confirmation.html', context)
        text_content = strip_tags(html_content)
        
        # Create Email object
        email = EmailMultiAlternatives(
            subject=f"Ticket Confirmed: {movie_name} at {theater_name}",
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL if hasattr(settings, 'DEFAULT_FROM_EMAIL') else 'webmaster@localhost',
            to=[user.email if user.email else 'customer@localhost']
        )
        email.attach_alternative(html_content, "text/html")
        
        # Send email
        email.send()
        logger.info(f"Booking confirmation email sent successfully to {user.username} for order {order_id}.")
        return True
    except Exception as e:
        logger.error(f"Failed to process email for order {order_id}: {e}")
        return False

def queue_booking_email(order_id):
    """API called from views/webhooks to queue email without blocking HTTP responses."""
    import os
    if os.environ.get('VERCEL') == '1':
        logger.info(f"Vercel serverless environment detected. Processing email task synchronously for order {order_id}.")
        process_email_task(order_id)
    else:
        logger.info(f"Queuing confirmation email for order {order_id}")
        email_queue.put({
            'order_id': order_id,
            'retries': 0
        })

def start_email_worker():
    """Starts the background worker thread if not already running."""
    global worker_thread
    if worker_thread is None or not worker_thread.is_alive():
        worker_thread = threading.Thread(target=email_worker_loop, daemon=True)
        worker_thread.start()
        logger.info("Background Email Worker started.")
