"""
WSGI config for bookmyseat project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os
import sys
from django.core.wsgi import get_wsgi_application

# Resolve path mapping for Vercel deployment imports
current_path = os.path.dirname(os.path.abspath(__file__))
root_path = os.path.dirname(current_path)
sys.path.append(current_path)
sys.path.append(root_path)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookmyseat.settings')

application = get_wsgi_application()
app = application