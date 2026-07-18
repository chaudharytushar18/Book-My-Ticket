import uuid
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from django.core.cache import cache

from .models import Movie, Theater, Seat, Booking, Order, Genre, Language
from .templatetags.movie_filters import youtube_video_id
from .views import finalize_order

class MovieBookingTests(TestCase):
    
    def setUp(self):
        # Create test user
        self.user = User.objects.create_user(username='testuser', password='password123')
        
        # Create test language and genre
        self.language = Language.objects.create(name='English')
        self.genre = Genre.objects.create(name='Action')
        
        # Create test movie
        self.movie = Movie.objects.create(
            name='Test Movie',
            rating=8.5,
            cast='Test Actor',
            description='Test description',
            trailer_url='https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            language=self.language
        )
        self.movie.genres.add(self.genre)
        
        # Create test theater
        self.theater = Theater.objects.create(
            name='Test Theater',
            movie=self.movie,
            time=timezone.now() + timezone.timedelta(days=1)
        )
        
        # Create test seats
        self.seat1 = Seat.objects.create(theater=self.theater, seat_number='A1', price=150.00)
        self.seat2 = Seat.objects.create(theater=self.theater, seat_number='A2', price=150.00)
        
        # Clear cache before tests
        cache.clear()

    def test_youtube_video_id_filter(self):
        """Verify YouTube trailer extraction & validation (XSS prevention)."""
        valid_urls = [
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'http://youtube.com/watch?v=dQw4w9WgXcQ&feature=related',
            'https://youtu.be/dQw4w9WgXcQ',
            'https://www.youtube.com/embed/dQw4w9WgXcQ',
        ]
        invalid_urls = [
            'https://www.google.com',
            'https://youtube.com/watch?v=short',
            'javascript:alert("XSS")',
            '<script>alert("XSS")</script>',
            'https://youtube.com/watch?v=dQw4w9WgXcQ_too_long_id'
        ]
        
        for url in valid_urls:
            self.assertEqual(youtube_video_id(url), 'dQw4w9WgXcQ')
            
        for url in invalid_urls:
            self.assertIsNone(youtube_video_id(url))

    def test_seat_concurrency_locking(self):
        """Verify concurrency seat locking mechanics."""
        self.client.login(username='testuser', password='password123')
        
        # Select seat1
        response = self.client.post(
            reverse('book_seats', kwargs={'theater_id': self.theater.id}),
            {'seats': [self.seat1.id]}
        )
        
        # Assert database locked state
        self.seat1.refresh_from_db()
        self.assertTrue(self.seat1.is_unavailable)
        self.assertEqual(self.seat1.reserved_by, self.user)
        self.assertIsNotNone(self.seat1.reserved_until)
        
        # Creating a second user trying to book the same locked seat
        user2 = User.objects.create_user(username='testuser2', password='password123')
        self.client.login(username='testuser2', password='password123')
        
        response2 = self.client.post(
            reverse('book_seats', kwargs={'theater_id': self.theater.id}),
            {'seats': [self.seat1.id]}
        )
        
        # Should render error and NOT lock/change reservation fields for user2
        self.assertIn("already locked or booked", response2.context['error'])
        self.seat1.refresh_from_db()
        self.assertEqual(self.seat1.reserved_by, self.user)

    def test_order_idempotency_finalization(self):
        """Verify that payment finalization is idempotent (prevents double bookings)."""
        order_id = f"ORD-TEST-{uuid.uuid4().hex[:6].upper()}"
        order = Order.objects.create(
            order_id=order_id,
            user=self.user,
            theater=self.theater,
            total_price=150.00,
            status='PENDING'
        )
        order.seats.add(self.seat1)
        
        # 1st processing call
        success = finalize_order(order_id, "pi_test_123")
        self.assertTrue(success)
        
        order.refresh_from_db()
        self.assertEqual(order.status, 'COMPLETED')
        self.assertEqual(Booking.objects.filter(user=self.user, seat=self.seat1).count(), 1)
        self.seat1.refresh_from_db()
        self.assertTrue(self.seat1.is_booked)
        
        # 2nd processing call (e.g. duplicate webhook trigger)
        success_duplicate = finalize_order(order_id, "pi_test_123")
        self.assertTrue(success_duplicate)
        
        # Ensure Booking count is STILL 1 (no duplicates)
        self.assertEqual(Booking.objects.filter(user=self.user, seat=self.seat1).count(), 1)

    def test_admin_analytics_aggregation_and_caching(self):
        """Verify admin analytics aggregates properly and caches results."""
        self.client.login(username='testuser', password='password123')
        
        # Make the user a superuser to access the analytics
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        
        # Create a confirmed booking
        Booking.objects.create(
            user=self.user,
            seat=self.seat2,
            movie=self.movie,
            theater=self.theater,
            price=150.00
        )
        self.seat2.is_booked = True
        self.seat2.save()
        
        # Fetch analytics page
        url = reverse('admin_analytics_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        
        # Inspect template variables computed from database aggregation
        self.assertEqual(response.context['daily_revenue'], 150.00)
        self.assertEqual(response.context['total_bookings'], 1)
        
        # Verify result is stored in cache
        cached_data = cache.get('admin_analytics_data')
        self.assertIsNotNone(cached_data)
        self.assertEqual(cached_data['daily_revenue'], 150.00)
