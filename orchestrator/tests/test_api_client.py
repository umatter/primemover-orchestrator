"""The HTTP client every other module talks to the API through.

It sat at 28% covered, which is thin for the layer that decides whether a
request is authenticated, which endpoint answers, and what an empty body means.
The tests below are about behaviours that would be silent if they broke -- a
token quietly not sent, a fallback endpoint quietly not tried, an empty 204
quietly becoming a crash -- rather than about the number.
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.clients.api import APIClient


def a_client(handler, **kwargs) -> APIClient:
    """A client whose transport is a function, so no socket is involved."""
    client = APIClient(
        base_url=kwargs.pop("base_url", "http://api.test/api/v1"),
        email=kwargs.pop("email", "someone@example.test"),
        password=kwargs.pop("password", "secret"),
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def login_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok"})


class TestAuthentication:
    def test_it_logs_in_before_the_first_request(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"data": []})

        a_client(handler).list_queues_for_crawler(1)

        assert seen[0].endswith("/login"), "the first call out must be the login"

    def test_the_token_is_sent_on_the_real_request(self):
        headers = {}

        def handler(request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            headers.update(request.headers)
            return httpx.Response(200, json={"data": []})

        a_client(handler).list_queues_for_crawler(1)

        assert headers["authorization"] == "Bearer tok"

    def test_it_falls_back_to_auth_login(self):
        # Some deployments answer on /auth/login. A 404 on the first must not be
        # the end of it.
        tried = []

        def handler(request):
            tried.append(request.url.path)
            if request.url.path.endswith("/auth/login"):
                return httpx.Response(200, json={"token": "tok"})
            if request.url.path.endswith("/login"):
                return httpx.Response(404)
            return httpx.Response(200, json={"data": []})

        client = a_client(handler)

        assert client.is_authenticated() is True
        assert any(p.endswith("/auth/login") for p in tried)

    def test_a_transport_error_on_the_first_endpoint_is_not_fatal(self):
        def handler(request):
            if request.url.path.endswith("/api/v1/login"):
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"access_token": "tok"})

        assert a_client(handler).is_authenticated() is True

    def test_it_authenticates_once_and_reuses_the_token(self):
        logins = []

        def handler(request):
            if request.url.path.endswith("/login"):
                logins.append(1)
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"data": []})

        client = a_client(handler)
        client.list_queues_for_crawler(1)
        client.list_queues_for_crawler(2)

        assert len(logins) == 1, "re-authenticating per request would be a login storm"

    def test_missing_credentials_say_which_settings_to_set(self):
        client = a_client(login_ok, email=None, password=None)

        with pytest.raises(ValueError, match="PM_API_EMAIL"):
            client._authenticate()

    def test_a_login_that_returns_no_token_is_a_failure(self):
        # 200 with an unexpected body is not success; it used to be enough for
        # the code to carry on with self._token still None.
        def handler(request):
            return httpx.Response(200, json={"message": "ok"})

        with pytest.raises(RuntimeError, match="could not obtain access token"):
            a_client(handler)._authenticate()

    def test_is_authenticated_answers_false_rather_than_raising(self):
        def handler(request):
            return httpx.Response(500)

        assert a_client(handler).is_authenticated() is False


class TestRequests:
    def test_an_empty_body_is_an_empty_dict_not_a_json_error(self):
        # DELETE answers 204 with no content; json() on that raises.
        def handler(request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(204)

        assert a_client(handler)._request("DELETE", "queues/1") == {}

    def test_an_http_error_is_raised_not_swallowed(self):
        def handler(request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(404, json={"message": "gone"})

        with pytest.raises(httpx.HTTPStatusError):
            a_client(handler)._request("GET", "queues/999")

    def test_a_leading_slash_does_not_double_up(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={})

        a_client(handler)._request("GET", "/queues")

        assert "//queues" not in seen[-1].replace("http://", "")

    def test_params_reach_the_query_string(self):
        seen = []

        def handler(request):
            seen.append(request.url)
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"data": []})

        a_client(handler)._request("GET", "queues", params={"filter[crawler]": 7})

        assert seen[-1].params["filter[crawler]"] == "7"

    def test_json_requests_declare_their_content_type(self):
        headers = {}

        def handler(request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            headers.update(request.headers)
            return httpx.Response(200, json={})

        a_client(handler)._request("POST", "queues", json_data={"name": "x"})

        assert headers["content-type"] == "application/json"

    def test_a_multipart_upload_does_not_claim_to_be_json(self):
        # httpx sets multipart/form-data itself; overriding it with
        # application/json makes the API reject the upload.
        headers = {}

        def handler(request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            headers.update(request.headers)
            return httpx.Response(200, json={})

        a_client(handler)._request(
            "POST", "experiments/1/upload", json_data={"a": 1}, files={"json": ("s.json", b"[]")}
        )

        assert "multipart/form-data" in headers["content-type"]


class TestBaseUrl:
    def test_a_trailing_slash_is_stripped(self):
        client = a_client(login_ok, base_url="http://api.test/api/v1/")

        assert client.base_url == "http://api.test/api/v1"

    def test_it_closes_cleanly_as_a_context_manager(self):
        with a_client(login_ok) as client:
            assert client.base_url

        assert client._client.is_closed
