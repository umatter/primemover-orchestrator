"""Finding one crawler's queue without searching a page of everyone else's.

The e2e uploads an experiment and then has to find the queue it just created.
It used to do that by pulling /queues-unprocessed and filtering client-side,
which was correct while that list returned every unprocessed queue in the
installation. Since the queues-* lists were paginated it returns the newest 15,
so the client-side filter searches a window that happens to contain the new
queue only because the e2e created it moments earlier.

Sixteen newer unprocessed queues between upload and lookup -- a second run, a
real study starting -- and the lookup fails with "No unprocessed queues found
for crawler N" about a crawler whose queue exists and is perfectly fine.

`filter[crawler]` narrows before the page is cut, so the window stops mattering.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.clients.api import APIClient


def a_client(payload: dict | list) -> tuple[APIClient, MagicMock]:
    client = APIClient.__new__(APIClient)
    request = MagicMock(return_value=payload)
    client._request = request  # type: ignore[method-assign]
    return client, request


class TestListQueuesForCrawler:
    def test_it_asks_the_api_to_filter(self):
        client, request = a_client({"data": [{"id": 9, "crawler_id": 7}]})

        client.list_queues_for_crawler(7)

        method, endpoint = request.call_args.args
        assert method == "GET"
        assert endpoint == "queues"
        assert request.call_args.kwargs["params"] == {"filter[crawler]": 7}

    def test_it_returns_the_rows(self):
        client, _ = a_client({"data": [{"id": 9, "crawler_id": 7}]})

        assert client.list_queues_for_crawler(7) == [{"id": 9, "crawler_id": 7}]

    def test_an_empty_page_is_an_empty_list_not_a_crash(self):
        client, _ = a_client({"data": []})

        assert client.list_queues_for_crawler(7) == []

    def test_a_bare_list_response_still_works(self):
        # Older API builds answered some list endpoints without the data wrapper.
        client, _ = a_client([{"id": 9, "crawler_id": 7}])

        assert client.list_queues_for_crawler(7) == [{"id": 9, "crawler_id": 7}]

    def test_it_does_not_hit_a_paginated_queues_star_endpoint(self):
        # The regression guard: any queues-<status> endpoint is a page of the
        # whole installation, which is what this method exists to avoid.
        client, request = a_client({"data": []})

        client.list_queues_for_crawler(7)

        assert "queues-" not in request.call_args.args[1]
