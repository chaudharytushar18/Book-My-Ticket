import uuid
import stripe
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache
from django.db.models import Sum, Count, Case, When, FloatField, F, Q, ExpressionWrapper
from django.db.models.functions import ExtractHour
from django.core.paginator import Paginator
from django.conf import settings
from django.urls import reverse

from .models import Movie, Theater, Seat, Booking, Order, Genre, Language
from .email_worker import queue_booking_email

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

def finalize_order(order_id, payment_intent_id=None):
    """
    Finalizes an order after payment verification.
    Includes database-level transaction lock and idempotency check.
    """
    with transaction.atomic():
        try:
            # Row lock to prevent race conditions during concurrent webhook requests
            order = Order.objects.select_for_update().get(order_id=order_id)
        except Order.DoesNotExist:
            logger.error(f"Finalization error: Order {order_id} not found.")
            return False

        # Idempotency check: prevent duplicate bookings
        if order.status == 'COMPLETED':
            logger.info(f"Order {order_id} is already completed. Skipping.")
            return True

        if order.status in ('PENDING', 'FAILED'):
            order.status = 'COMPLETED'
            order.stripe_session_id = payment_intent_id
            order.save()

            # Finalize booking seats
            for seat in order.seats.all():
                seat.is_booked = True
                seat.reserved_by = None
                seat.reserved_until = None
                seat.save()

                # Get or create Booking to handle duplicate calls gracefully
                Booking.objects.get_or_create(
                    user=order.user,
                    seat=seat,
                    movie=order.theater.movie,
                    theater=order.theater,
                    defaults={
                        'price': seat.price,
                        'payment_intent_id': payment_intent_id
                    }
                )

            # Asynchronously send email confirmation
            try:
                queue_booking_email(order.order_id)
            except Exception as e:
                logger.error(f"Failed to queue booking email for order {order_id}: {e}")

            # Clear metrics cache
            cache.delete('admin_analytics_data')
            
            logger.info(f"Successfully finalized Order {order_id}.")
            return True

    return False


def movie_list(request):
    """
    Renders the movie list page with optimized, server-side
    genre and language filtering, sorting, pagination, and dynamic facet counts.
    """
    search_query = request.GET.get('search', '').strip()
    selected_genres = request.GET.getlist('genres')
    selected_languages = request.GET.getlist('languages')
    sort_by = request.GET.get('sort', 'name')
    
    # Base optimized query
    movies = Movie.objects.all().prefetch_related('genres').select_related('language')
    
    if search_query:
        movies = movies.filter(name__icontains=search_query)
        
    # Calculate faceted counts before applying selected checkbox filters
    base_search_qs = Movie.objects.filter(name__icontains=search_query) if search_query else Movie.objects.all()
    
    # Genre counts match SEARCH + LANGUAGE filter
    qs_for_genres = base_search_qs
    if selected_languages:
        qs_for_genres = qs_for_genres.filter(language__id__in=selected_languages)
        
    # Language counts match SEARCH + GENRE filter
    qs_for_languages = base_search_qs
    if selected_genres:
        qs_for_languages = qs_for_languages.filter(genres__id__in=selected_genres).distinct()
        
    # Apply filters to final results
    if selected_languages:
        movies = movies.filter(language__id__in=selected_languages)
    if selected_genres:
        movies = movies.filter(genres__id__in=selected_genres).distinct()
        
    # Apply sorting
    if sort_by == 'rating_desc':
        movies = movies.order_by('-rating')
    elif sort_by == 'rating_asc':
        movies = movies.order_by('rating')
    elif sort_by == 'name_desc':
        movies = movies.order_by('-name')
    else:
        movies = movies.order_by('name')
        
    # Paginate results
    paginator = Paginator(movies, 6)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Fetch facet values with dynamic counts
    all_genres = Genre.objects.annotate(
        movie_count=Count('movies', filter=Q(movies__in=qs_for_genres))
    ).order_by('name')
    
    all_languages = Language.objects.annotate(
        movie_count=Count('movies', filter=Q(movies__in=qs_for_languages))
    ).order_by('name')
    
    context = {
        'page_obj': page_obj,
        'genres': all_genres,
        'languages': all_languages,
        'selected_genres': [int(g) for g in selected_genres if g.isdigit()],
        'selected_languages': [int(l) for l in selected_languages if l.isdigit()],
        'sort_by': sort_by,
        'search_query': search_query,
    }
    return render(request, 'movies/movie_list.html', context)


def theater_list(request, movie_id):
    """Renders theater list details for a movie with a dynamic date selector filter."""
    movie = get_object_or_404(Movie, id=movie_id)
    theaters_qs = Theater.objects.filter(movie=movie).order_by('time')
    
    # Extract unique dates from the showtimes
    unique_dates = sorted(list(set(t.time.date() for t in theaters_qs)))
    
    selected_date_str = request.GET.get('date')
    selected_date = None
    if selected_date_str:
        try:
            import datetime
            selected_date = datetime.datetime.strptime(selected_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass
            
    if not selected_date and unique_dates:
        selected_date = unique_dates[0]
        
    if selected_date:
        theaters = theaters_qs.filter(time__date=selected_date)
    else:
        theaters = theaters_qs
        
    context = {
        'movie': movie,
        'theaters': theaters,
        'unique_dates': unique_dates,
        'selected_date': selected_date,
    }
    return render(request, 'movies/theater_list.html', context)


@login_required(login_url='/login/')
def book_seats(request, theater_id):
    """
    Temporarily locks selected seats for 2 minutes inside a concurrency-safe atomic transaction.
    Redirects user to billing invoice page.
    """
    theaters = get_object_or_404(Theater, id=theater_id)
    
    # Lazy cleanup of expired locks before rendering availability
    Seat.objects.filter(theater=theaters, is_booked=False, reserved_until__lt=timezone.now()).update(reserved_by=None, reserved_until=None)
    seats = Seat.objects.filter(theater=theaters)
    
    if request.method == 'POST':
        selected_seat_ids = request.POST.getlist('seats')
        if not selected_seat_ids:
            return render(request, "movies/seat_selection.html", {'theaters': theaters, 'seats': seats, 'error': "No seat selected."})
        
        try:
            with transaction.atomic():
                # Lock rows using select_for_update
                locked_seats = list(Seat.objects.select_for_update().filter(theater=theaters, id__in=selected_seat_ids))
                
                # Double-check constraints
                if len(locked_seats) != len(selected_seat_ids):
                    raise ValueError("One or more selected seats were invalid.")
                    
                error_seats = []
                now = timezone.now()
                for seat in locked_seats:
                    if seat.is_booked:
                        error_seats.append(seat.seat_number)
                    elif seat.reserved_until and seat.reserved_until > now and seat.reserved_by != request.user:
                        error_seats.append(seat.seat_number)
                        
                if error_seats:
                    raise ValueError(f"The following seats are already locked or booked: {', '.join(error_seats)}")
                
                # Apply lock for 2 minutes
                lock_duration = timezone.now() + timezone.timedelta(minutes=2)
                for seat in locked_seats:
                    seat.reserved_by = request.user
                    seat.reserved_until = lock_duration
                    seat.save()
                    
                # Create pending order
                order_id = f"ORD-{uuid.uuid4().hex[:12].upper()}"
                total_price = sum(seat.price for seat in locked_seats)
                order = Order.objects.create(
                    order_id=order_id,
                    user=request.user,
                    theater=theaters,
                    total_price=total_price,
                    status='PENDING'
                )
                order.seats.set(locked_seats)
                
            return redirect('confirm_payment', order_id=order.order_id)
        except Exception as e:
            # Refresh seats queryset to show current status in template
            seats = Seat.objects.filter(theater=theaters)
            return render(request, "movies/seat_selection.html", {'theaters': theaters, 'seats': seats, 'error': str(e)})
            
    return render(request, 'movies/seat_selection.html', {'theaters': theaters, 'seats': seats})


@login_required(login_url='/login/')
def confirm_payment(request, order_id):
    """Displays order breakdown and countdown timer to complete payment."""
    order = get_object_or_404(Order, order_id=order_id, user=request.user)
    
    # Check if seat lock expired
    if order.status == 'PENDING' and (timezone.now() - order.created_at).total_seconds() > 120:
        order.status = 'EXPIRED'
        order.save()
        # Unlock seats
        for seat in order.seats.all():
            if not seat.is_booked:
                seat.reserved_by = None
                seat.reserved_until = None
                seat.save()
        return render(request, "movies/seat_selection.html", {
            'theaters': order.theater, 
            'seats': Seat.objects.filter(theater=order.theater),
            'error': "Your 2-minute booking lock expired. Please select seats again."
        })
        
    return render(request, 'movies/payment_confirmation.html', {'order': order})


@login_required(login_url='/login/')
def create_checkout_session(request, order_id):
    """
    Creates a Razorpay Order and renders the Razorpay Checkout form.
    Falls back gracefully to simulated mock payments if Razorpay credentials are not set.
    """
    order = get_object_or_404(Order, order_id=order_id, user=request.user)
    
    # Double-check lock expiration
    if (timezone.now() - order.created_at).total_seconds() > 120:
        order.status = 'EXPIRED'
        order.save()
        for seat in order.seats.all():
            if not seat.is_booked:
                seat.reserved_by = None
                seat.reserved_until = None
                seat.save()
        return redirect('book_seats', theater_id=order.theater.id)

    # Check if Razorpay keys are valid and not placeholder
    if settings.RAZORPAY_KEY_ID == 'rzp_test_placeholder' or not settings.RAZORPAY_KEY_ID:
        # Graceful fallback to mock payment gateway
        return redirect('mock_payment', order_id=order.order_id)

    try:
        import razorpay
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        
        # Create Razorpay Order
        data = {
            "amount": int(order.total_price * 100),  # price in paise
            "currency": "INR",
            "receipt": order.order_id,
            "payment_capture": 1
        }
        razorpay_order = client.order.create(data=data)
        
        # Save Razorpay Order ID in stripe_session_id column as a generic payment reference
        order.stripe_session_id = razorpay_order['id']
        order.save()
        
        context = {
            'order': order,
            'razorpay_order_id': razorpay_order['id'],
            'razorpay_key_id': settings.RAZORPAY_KEY_ID,
            'order_amount': int(order.total_price * 100),
            'user_name': request.user.username,
            'user_email': request.user.email or "customer@example.com",
        }
        return render(request, 'movies/razorpay_checkout.html', context)
    except Exception as e:
        logger.warning(f"Razorpay Order creation failed ({e}). Falling back to local Mock Payment Gateway.")
        return redirect('mock_payment', order_id=order.order_id)


@login_required(login_url='/login/')
def mock_payment(request, order_id):
    """Simulates payment gateway approval offline."""
    order = get_object_or_404(Order, order_id=order_id, user=request.user)
    
    if request.method == 'POST':
        action = request.POST.get('status')
        if action == 'success':
            mock_id = f"MOCK-PAY-{uuid.uuid4().hex[:10].upper()}"
            finalize_order(order.order_id, mock_id)
            return redirect(reverse('payment_success') + f"?order_id={order.order_id}")
        else:
            order.status = 'FAILED'
            order.save()
            for seat in order.seats.all():
                if not seat.is_booked:
                    seat.reserved_by = None
                    seat.reserved_until = None
                    seat.save()
            return redirect(reverse('payment_cancel') + f"?order_id={order.order_id}")
            
    seat_numbers = ", ".join(s.seat_number for s in order.seats.all())
    return render(request, 'movies/mock_payment.html', {'order': order, 'seat_numbers': seat_numbers})


@login_required(login_url='/login/')
def payment_success(request):
    """Renders payment receipt after successful ticket booking verification."""
    razorpay_payment_id = request.GET.get('razorpay_payment_id')
    razorpay_order_id = request.GET.get('razorpay_order_id')
    razorpay_signature = request.GET.get('razorpay_signature')
    order_id = request.GET.get('order_id')
    
    booking = None
    booking_seats = ""
    total_paid = 0
    payment_intent_id = ""

    # If it is Razorpay redirect, verify the signature!
    if razorpay_payment_id and razorpay_order_id and razorpay_signature:
        try:
            import razorpay
            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            params_dict = {
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': razorpay_payment_id,
                'razorpay_signature': razorpay_signature
            }
            # Verify signature securely on server-side
            client.utility.verify_payment_signature(params_dict)
            
            # Finalize order if verified
            finalize_order(order_id, razorpay_payment_id)
        except Exception as e:
            logger.error(f"Razorpay signature verification failed: {e}")
            return redirect('payment_cancel')

    if order_id:
        order = get_object_or_404(Order, order_id=order_id, user=request.user)
        bookings = Booking.objects.filter(
            user=request.user, 
            theater=order.theater,
            movie=order.theater.movie,
            seat__in=order.seats.all()
        )
        if bookings.exists():
            booking = bookings.first()
            booking_seats = ", ".join(b.seat.seat_number for b in bookings)
            total_paid = sum(b.price for b in bookings)
            payment_intent_id = booking.payment_intent_id

    if not booking:
        return redirect('profile')

    context = {
        'booking': booking,
        'booking_seats': booking_seats,
        'total_paid': total_paid,
        'payment_intent_id': payment_intent_id
    }
    return render(request, 'movies/payment_success.html', context)


@login_required(login_url='/login/')
def payment_cancel(request):
    """Cancels checkout flow and releases reserved seats."""
    order_id = request.GET.get('order_id')
    order = None
    if order_id:
        try:
            order = Order.objects.get(order_id=order_id, user=request.user)
            if order.status == 'PENDING':
                order.status = 'FAILED'
                order.save()
                for seat in order.seats.all():
                    if not seat.is_booked:
                        seat.reserved_by = None
                        seat.reserved_until = None
                        seat.save()
        except Order.DoesNotExist:
            pass
            
    return render(request, 'movies/payment_cancel.html', {'order': order})


@csrf_exempt
def stripe_webhook(request):
    """
    Secure endpoint validating signatures from Razorpay.
    Performs transaction locking and idempotency verification.
    """
    payload = request.body
    sig_header = request.META.get('HTTP_X_RAZORPAY_SIGNATURE')
    
    # Safe check in case webhook is triggered during tests with fake signature
    if settings.RAZORPAY_WEBHOOK_SECRET == 'placeholder_secret':
        # Skip validation during mock local development testing
        try:
            event = json.loads(payload.decode('utf-8'))
        except Exception:
            return HttpResponse(status=400)
    else:
        try:
            import razorpay
            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            # Verify Razorpay signature securely
            client.utility.verify_webhook_signature(payload, sig_header, settings.RAZORPAY_WEBHOOK_SECRET)
            event = json.loads(payload.decode('utf-8'))
        except Exception:
            return HttpResponse(status=400)

    # Handle completion event
    event_type = event.get('event')
    if event_type in ('order.paid', 'payment.captured'):
        payment_entity = event['payload']['payment']['entity']
        razorpay_order_id = payment_entity.get('order_id')
        razorpay_payment_id = payment_entity.get('id')
        
        try:
            order = Order.objects.get(stripe_session_id=razorpay_order_id)
            success = finalize_order(order.order_id, razorpay_payment_id)
            if not success:
                return HttpResponse(status=500)
        except Order.DoesNotExist:
            return HttpResponse(status=404)

    return HttpResponse(status=200)


@user_passes_test(lambda u: u.is_superuser)
def admin_analytics_dashboard(request):
    """
    Superuser-restricted real-time metrics dashboard.
    Implements optimized SQL queries and 5-minute caching.
    """
    cache_key = 'admin_analytics_data'
    data = cache.get(cache_key)
    
    if not data:
        now = timezone.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_week = start_of_day - timezone.timedelta(days=now.weekday())
        start_of_month = start_of_day.replace(day=1)
        
        # 1. Revenue aggregations entirely inside Database Sum function
        daily_rev = Booking.objects.filter(booked_at__gte=start_of_day, is_cancelled=False).aggregate(total=Sum('price'))['total'] or 0.0
        weekly_rev = Booking.objects.filter(booked_at__gte=start_of_week, is_cancelled=False).aggregate(total=Sum('price'))['total'] or 0.0
        monthly_rev = Booking.objects.filter(booked_at__gte=start_of_month, is_cancelled=False).aggregate(total=Sum('price'))['total'] or 0.0
        
        # 2. Movie popularity annotation
        popular_movies = Movie.objects.annotate(
            booking_count=Count('booking', filter=Q(booking__is_cancelled=False))
        ).order_by('-booking_count')[:5]
        
        # 3. Theater occupancy rate database computation
        busiest_theaters = Theater.objects.annotate(
            total_seats=Count('seats', distinct=True),
            booked_seats=Count('seats', filter=Q(seats__is_booked=True), distinct=True)
        ).annotate(
            occupancy_rate=ExpressionWrapper(
                Case(
                    When(total_seats=0, then=0.0),
                    default=(F('booked_seats') * 100.0) / F('total_seats'),
                    output_field=FloatField()
                ),
                output_field=FloatField()
            )
        ).order_by('-occupancy_rate')[:5]
        
        # 4. Hourly bookings distribution in DB
        peak_hours = Booking.objects.annotate(
            hour=ExtractHour('booked_at')
        ).values('hour').annotate(
            count=Count('id')
        ).order_by('-count')[:5]
        
        # 5. Cancellation Rate
        total_bookings = Booking.objects.count()
        cancelled_bookings = Booking.objects.filter(is_cancelled=True).count()
        cancellation_rate = 0.0
        if total_bookings > 0:
            cancellation_rate = (cancelled_bookings * 100.0) / total_bookings
            
        data = {
            'daily_revenue': daily_rev,
            'weekly_revenue': weekly_rev,
            'monthly_revenue': monthly_rev,
            'popular_movies': popular_movies,
            'busiest_theaters': busiest_theaters,
            'peak_hours': peak_hours,
            'cancellation_rate': round(cancellation_rate, 2),
            'total_bookings': total_bookings,
        }
        
        # Cache results for 5 minutes
        cache.set(cache_key, data, 300)
        
    return render(request, 'movies/admin_dashboard.html', data)
