"""Server settings for pages that load in parts: compression, worker restarts and the lazy user role."""

import gzip
import runpy
from pathlib import Path

import pytest
from django.conf import settings
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.template import Context, Template
from django.test import RequestFactory

from neurodb.accounts.roles import VIEWER
from neurodb.web.context_processors import site
from neurodb.web.middleware import CompressMiddleware

PAGE = (
    "<!doctype html><html><body>"
    + "<p>The country overview, its tiles and charts.</p>" * 20
    + "</body></html>"
)


def _compressed(response, accept="gzip, deflate, br"):
    request = RequestFactory().get("/", HTTP_ACCEPT_ENCODING=accept)
    return CompressMiddleware(lambda request: response)(request)


# ---------------------------------------------------------------------------------------------- gunicorn
def test_workers_restart_after_8000_requests_give_or_take_800():
    conf = runpy.run_path(str(Path(settings.BASE_DIR) / "config" / "gunicorn.conf.py"))
    assert conf["max_requests"] == 8000
    assert conf["max_requests_jitter"] == 800


# ------------------------------------------------------------------------------------------- compression
def test_compression_sits_right_after_whitenoise():
    middleware = settings.MIDDLEWARE
    assert middleware.index("neurodb.web.middleware.CompressMiddleware") == (
        middleware.index("whitenoise.middleware.WhiteNoiseMiddleware") + 1
    )


def test_html_over_200_bytes_is_compressed_for_a_browser_that_accepts_gzip():
    response = _compressed(HttpResponse(PAGE))
    assert response["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response["Vary"]
    assert gzip.decompress(response.content).decode() == PAGE
    assert int(response["Content-Length"]) == len(response.content) < len(PAGE)


def test_what_is_not_compressed():
    # a browser that does not accept gzip (the answer still varies on it), and a short answer
    plain = _compressed(HttpResponse(PAGE), accept="identity")
    assert not plain.has_header("Content-Encoding") and "Accept-Encoding" in plain["Vary"]
    assert plain.content.decode() == PAGE
    assert not _compressed(HttpResponse("<p>short</p>")).has_header("Content-Encoding")

    # event streams (the chats), attachments (exports) and streamed responses are left as they are
    stream = HttpResponse(PAGE, content_type="text/event-stream")
    attachment = HttpResponse(PAGE, content_type="text/csv")
    attachment["Content-Disposition"] = 'attachment; filename="figures.csv"'
    streamed = StreamingHttpResponse(iter([PAGE.encode()] * 3), content_type="text/html")
    for response in (stream, attachment, streamed):
        answer = _compressed(response)
        assert not answer.has_header("Content-Encoding")
        assert "Accept-Encoding" not in answer.get("Vary", "")
    assert b"".join(streamed.streaming_content) == PAGE.encode() * 3
    assert stream.content.decode() == PAGE and attachment.content.decode() == PAGE

    # a file (a download, or a page someone saved) is streamed too
    download = FileResponse(iter([b"x" * 500]), as_attachment=False)
    assert not _compressed(download).has_header("Content-Encoding")

    # what is compressed already (a library cover, an inline PDF) is never gzipped again: gzip would spend
    # the CPU on the whole body for nothing, and the answer would vary on Accept-Encoding for nothing
    for content_type in ("image/jpeg", "image/png", "image/webp", "application/pdf", "font/woff2"):
        already = HttpResponse(PAGE.encode(), content_type=content_type)  # (compressible, as a test only)
        answer = _compressed(already)
        assert not answer.has_header("Content-Encoding"), content_type
        assert "Accept-Encoding" not in answer.get("Vary", ""), content_type
        assert answer.content == PAGE.encode(), content_type
    svg = _compressed(HttpResponse(PAGE, content_type="image/svg+xml"))  # an SVG image is text
    assert svg["Content-Encoding"] == "gzip"


def test_an_inline_file_name_is_not_an_attachment():
    response = HttpResponse(PAGE)
    response["Content-Disposition"] = 'inline; filename="attachment.html"'
    assert _compressed(response)["Content-Encoding"] == "gzip"


@pytest.mark.django_db
def test_a_page_through_the_whole_stack_comes_back_compressed(client):
    response = client.get("/accounts/login/", HTTP_ACCEPT_ENCODING="gzip")
    assert response.status_code == 200
    assert response["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response["Vary"]
    assert "<form" in gzip.decompress(response.content).decode()


# ------------------------------------------------------------------------------------------- user role
@pytest.mark.django_db
def test_the_user_role_is_read_only_when_a_template_uses_it(viewer, django_assert_num_queries):
    request = RequestFactory().get("/")
    request.user = viewer
    with django_assert_num_queries(0):
        context = site(request)
    with django_assert_num_queries(1):  # the groups, once
        assert str(context["user_role"]) == VIEWER
        assert context["user_role"] == VIEWER

    rendered = Template(
        '{% if user_role == "Viewer" %}viewer{% endif %}|{% if user_role == "Administrator" %}admin{% endif %}|'
        "{{ user_role }}"
    ).render(Context(site(request)))
    assert rendered == "viewer||Viewer"


@pytest.mark.django_db
def test_an_anonymous_visitor_has_no_role():
    from django.contrib.auth.models import AnonymousUser

    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    rendered = Template("{% if user_role %}role{% else %}none{% endif %}").render(Context(site(request)))
    assert rendered == "none"
    assert site(RequestFactory().get("/"))["user_role"] is None  # no user at all (an error page)
