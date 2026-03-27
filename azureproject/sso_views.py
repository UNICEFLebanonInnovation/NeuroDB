import requests
import uuid
from django.shortcuts import redirect
from django.http import HttpResponseBadRequest
from django.contrib.auth import login, logout, get_user_model
from django.conf import settings

# Your OIDC config — move to settings.py later
CLIENT_ID = 'YOUR_CLIENT_ID'
CLIENT_SECRET = 'YOUR_CLIENT_SECRET'
TENANT = 'common'
REDIRECT_URI = 'https://neuro-db.org/sso/callback'
USER_INFO_URL = 'https://graph.microsoft.com/oidc/userinfo'
SCOPES = 'openid profile email'
AUTHORIZATION_URL = f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize'
TOKEN_URL = f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token'
LOGOUT_URL = f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/logout'

def sso_login(request):
    # Generate random state for CSRF protection
    state = str(uuid.uuid4())
    request.session['oauth_state'] = state

    # Build authorization URL
    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'response_mode': 'query',
        'scope': SCOPES,
        'state': state,
    }
    auth_url = f"{AUTHORIZATION_URL}?{'&'.join([f'{k}={v}' for k, v in params.items()])}"
    return redirect(auth_url)


def sso_callback(request):
    # Validate state
    state_sent = request.GET.get('state')
    state_session = request.session.get('oauth_state')
    if not state_sent or state_sent != state_session:
        return HttpResponseBadRequest('Invalid state parameter.')

    # Get authorization code
    code = request.GET.get('code')
    if not code:
        return HttpResponseBadRequest('No code provided.')

    # Exchange code for token
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
    }
    token_response = requests.post(TOKEN_URL, data=data)
    if token_response.status_code != 200:
        return HttpResponseBadRequest('Failed to fetch token.')

    tokens = token_response.json()
    access_token = tokens.get('access_token')
    if not access_token:
        return HttpResponseBadRequest('No access token in response.')

    # Fetch user info
    headers = {'Authorization': f'Bearer {access_token}'}
    user_response = requests.get(USER_INFO_URL, headers=headers)
    if user_response.status_code != 200:
        return HttpResponseBadRequest('Failed to fetch user info.')

    user_info = user_response.json()
    email = user_info.get('email') or user_info.get('preferred_username')
    name = user_info.get('name')

    if not email:
        return HttpResponseBadRequest('No email found in user info.')

    # Create or get user
    User = get_user_model()
    user, created = User.objects.get_or_create(email=email, defaults={
        'username': email,
        'first_name': name.split(' ')[0] if name else '',
        'last_name': ' '.join(name.split(' ')[1:]) if name and len(name.split(' ')) > 1 else '',
        'is_active': True,
        'is_staff': False,
        'is_superuser': False
    })

    login(request, user)
    return redirect('dashboard')  # or your home page


def sso_logout(request):
    logout(request)
    return redirect(f"{LOGOUT_URL}?post_logout_redirect_uri=https://neuro-db.org/")
