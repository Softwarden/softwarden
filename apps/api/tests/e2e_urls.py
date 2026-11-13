"""A URLconf with the test-only routes as the e2e server routes them (api/urls.e2e_urlpatterns), beside the real API,
for tests that call a test-only route: `@pytest.mark.urls("tests.e2e_urls")`. Never the shipped URLconf."""

from django.urls import include, path

from api import urls as api_urls

urlpatterns = [path("api/v1/", include(api_urls.e2e_urlpatterns())), path("api/v1/", include("api.urls"))]
