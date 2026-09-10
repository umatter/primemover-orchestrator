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
        params = request.call_args.kwargs["params"]
        assert params["filter[crawler]"] == 7
        assert params["page"] == 1, "paging starts at the first page"

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

    def test_it_follows_the_pages(self):
        """The bug this caught: one page is not the answer.

        filter[crawler] narrows before the page is cut, but the result is still
        paginated. A caller that counts the rows it gets back counts 15 and
        believes it counted everything -- which is exactly how the canary runway
        check first reported 15 sessions left out of 27.
        """
        pages = {
            1: {"data": [{"id": i} for i in range(15)], "meta": {"last_page": 2, "per_page": 15}},
            2: {
                "data": [{"id": i} for i in range(15, 27)],
                "meta": {"last_page": 2, "per_page": 15},
            },
        }

        def handler(method, endpoint, params=None, **kwargs):
            return pages[(params or {}).get("page", 1)]

        client = APIClient.__new__(APIClient)
        client._request = handler

        assert len(client.list_queues_for_crawler(7)) == 27

    def test_a_full_last_page_still_terminates(self):
        # `len(data) < per_page` is NOT a valid stopping condition: the last page
        # can be exactly full. meta.last_page is the only reliable one.
        page = {"data": [{"id": i} for i in range(15)], "meta": {"last_page": 1, "per_page": 15}}
        client, request = a_client(page)

        assert len(client.list_queues_for_crawler(7)) == 15
        assert request.call_count == 1

    def test_an_api_without_meta_reads_one_page_and_stops(self):
        client, request = a_client({"data": [{"id": 1}]})

        assert client.list_queues_for_crawler(7) == [{"id": 1}]
        assert request.call_count == 1

    def test_it_does_not_hit_a_paginated_queues_star_endpoint(self):
        # The regression guard: any queues-<status> endpoint is a page of the
        # whole installation, which is what this method exists to avoid.
        client, request = a_client({"data": []})

        client.list_queues_for_crawler(7)

        assert "queues-" not in request.call_args.args[1]
