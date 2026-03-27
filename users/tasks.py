from django.contrib.auth import get_user_model

User = get_user_model()

def get_users_count():
    """A pointless Celery task to demonstrate usage."""
    return User.objects.count()
