import re
from django import template

register = template.Library()

# Flexible regex using a lookahead assertion to ensure the video ID is exactly 11 characters
# and not followed by additional alphanumeric/dash/underscore characters.
YOUTUBE_REGEX = re.compile(
    r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})(?![a-zA-Z0-9_-])',
    re.IGNORECASE
)

@register.filter
def youtube_video_id(url):
    """
    Extracts the 11-character YouTube video ID from a valid URL.
    Returns None if the URL is invalid or empty to enforce security and graceful fallback.
    """
    if not url:
        return None
    # Search finds the match anywhere in the URL string
    match = YOUTUBE_REGEX.search(url)
    if match:
        return match.group(1)
    return None
