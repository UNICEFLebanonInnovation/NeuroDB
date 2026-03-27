from django.contrib.auth.models import AbstractUser
from django.db.models import CharField
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.db import models


class Section(models.Model):
    name = models.CharField(max_length=256)
    logo = models.CharField(max_length=256, null=True, blank=True)
    code = models.CharField(max_length=10, null=True, blank=True)
    color = models.CharField(max_length=50, null=True, blank=True)
    have_hpm_indicator = models.BooleanField(default=False)
    etools = models.BooleanField(default=False)
    powerbi_url = models.TextField(null=True, blank=True)

    def __str__(self):
        return self.name


class Office(models.Model):
    name = models.CharField(max_length=256)

    def __str__(self):
        return self.name


class User(AbstractUser):

    # First Name and Last Name do not cover name patterns
    # around the globe.
    name = models.CharField(_('Name of User'), blank=True, max_length=255)
    skype_account = models.CharField(blank=True, max_length=255)
    section = models.ForeignKey(
        Section, on_delete=models.SET_NULL,
        null=True, blank=True
    )
    backup_user = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True
    )

    def __str__(self):
        return '{} {}'.format(self.first_name, self.last_name)

    def get_absolute_url(self):
        return reverse('users:detail', kwargs={'username': self.username})

# class User(AbstractUser):
#     """
#     Default custom user model for Neuro-DB .
#     If adding fields that need to be filled at user signup,
#     check forms.SignupForm and forms.SocialSignupForms accordingly.
#     """

#     #: First and last name do not cover name patterns around the globe
#     name = CharField(_("Name of User"), blank=True, max_length=255)
#     first_name = None  # type: ignore
#     last_name = None  # type: ignore

#     def get_absolute_url(self):
#         """Get url for user's detail view.

#         Returns:
#             str: URL for user detail.

#         """
#         return reverse("users:detail", kwargs={"username": self.username})
