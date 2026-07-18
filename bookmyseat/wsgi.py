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

try:
    from django.core.wsgi import get_wsgi_application
    application = get_wsgi_application()
    app = application
except Exception as e:
    import traceback
    tb_str = traceback.format_exc()
    print(f"WSGI startup failed: {tb_str}")
    
    def app(environ, start_response):
        status = '500 Internal Server Error'
        headers = [('Content-type', 'text/html; charset=utf-8')]
        start_response(status, headers)
        html = f"""
        <html>
        <head><title>Django WSGI Startup Error</title></head>
        <body style="font-family: monospace; padding: 20px; background-color: #f8f9fa; color: #212529;">
            <h2 style="color: #dc3545; border-bottom: 2px solid #dc3545; padding-bottom: 10px;">Django WSGI Startup Exception</h2>
            <p>The server encountered an error during initialization. Traceback detail:</p>
            <pre style="background: #e9ecef; padding: 15px; border-radius: 5px; overflow-x: auto; border: 1px solid #ced4da; font-size: 14px; line-height: 1.5;">{tb_str}</pre>
        </body>
        </html>
        """
        return [html.encode('utf-8')]