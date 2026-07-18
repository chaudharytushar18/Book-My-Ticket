from django.apps import AppConfig


class MoviesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'movies'

    def ready(self):
        import os
        # Only launch background threads in the main process (avoids double launch with auto-reloader) and skip on Vercel serverless
        if (os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('DJANGO_SETTINGS_MODULE')) and os.environ.get('VERCEL') != '1':
            from . import scheduler
            from . import email_worker
            
            scheduler.start()
            email_worker.start_email_worker()
