import pytest

from users.tasks import get_users_count
from users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_user_count(settings):
    """A basic test to execute the get_users_count Celery task."""
    UserFactory.create_batch(3)
    task_result = get_users_count.delay()
    assert task_result.result == 3
