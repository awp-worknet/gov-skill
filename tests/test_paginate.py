"""paginate_all — cursor-based auto-pagination helper.

Doesn't actually hit HTTP; uses a fake fetch_page to simulate server cursor
behavior. Key checks:
- Single page (next_cursor=None) → run once and stop
- Multi-page → relay cursor to the end
- max_pages truncation → add the truncated flag
- Doesn't crash when the server omits the pagination field
"""

import pytest

from lib.govnet_lib import paginate_all


def _make_pages(*page_data):
    """Construct a sequence of fake responses; the last page has next_cursor=None."""
    pages = []
    for i, items in enumerate(page_data):
        is_last = i == len(page_data) - 1
        pages.append({
            "data": items,
            "pagination": {
                "next_cursor": None if is_last else f"cursor-{i+1}",
                "has_more": not is_last,
                "limit": len(items),
            },
        })
    return pages


def _make_fetch(pages):
    """Generate a fetch_page that emits the preset pages in cursor order."""
    state = {"calls": 0, "last_cursor": None}

    def fetch_page(params):
        # Verify the cursor relay is correct
        if state["calls"] > 0:
            assert params.get("cursor") == f"cursor-{state['calls']}", (
                f"call {state['calls']}: expected cursor-{state['calls']}, got {params.get('cursor')!r}"
            )
        idx = state["calls"]
        state["calls"] += 1
        if idx >= len(pages):
            raise AssertionError(f"asked for page {idx} but only {len(pages)} pages defined")
        return pages[idx]

    return fetch_page, state


def test_single_page_stops_immediately():
    pages = _make_pages([{"id": 1}, {"id": 2}])
    fetch, state = _make_fetch(pages)
    result = paginate_all(fetch, initial_params={"limit": 50})
    assert state["calls"] == 1
    assert result["data"] == [{"id": 1}, {"id": 2}]
    assert result["page_count"] == 1
    assert "truncated_at_max_pages" not in result


def test_multiple_pages_concatenated():
    pages = _make_pages(
        [{"id": 1}, {"id": 2}],
        [{"id": 3}, {"id": 4}],
        [{"id": 5}],
    )
    fetch, state = _make_fetch(pages)
    result = paginate_all(fetch, initial_params={"limit": 2})
    assert state["calls"] == 3
    assert result["data"] == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]
    assert result["page_count"] == 3


def test_max_pages_truncation():
    pages = _make_pages(
        [{"id": 1}],
        [{"id": 2}],
        [{"id": 3}],
        [{"id": 4}],
    )
    fetch, _ = _make_fetch(pages)
    result = paginate_all(fetch, initial_params={}, max_pages=2)
    # Stops after 2 pages
    assert result["page_count"] == 2
    assert result["data"] == [{"id": 1}, {"id": 2}]
    assert result.get("truncated_at_max_pages") is True
    # The relay cursor is exposed to the caller
    assert result["next_cursor"] == "cursor-2"


def test_empty_data_pages_handled():
    # Empty data[] but with more pages (rare but possible: filtered listing)
    pages = [
        {"data": [], "pagination": {"next_cursor": "cursor-1", "has_more": True, "limit": 0}},
        {"data": [{"id": 1}], "pagination": {"next_cursor": None, "has_more": False, "limit": 1}},
    ]

    state = {"calls": 0}
    def fetch(params):
        idx = state["calls"]; state["calls"] += 1
        return pages[idx]

    result = paginate_all(fetch, initial_params={})
    assert result["data"] == [{"id": 1}]
    assert result["page_count"] == 2


def test_missing_pagination_field_treated_as_terminal():
    # When the server omits the pagination field → treat as the last page
    page = {"data": [{"id": 1}, {"id": 2}]}
    state = {"calls": 0}

    def fetch(params):
        state["calls"] += 1
        return page

    result = paginate_all(fetch, initial_params={})
    assert state["calls"] == 1
    assert result["data"] == [{"id": 1}, {"id": 2}]


def test_initial_params_preserved_across_pages():
    """All original parameters other than cursor should be included on every page request."""
    pages = _make_pages([{"id": 1}], [{"id": 2}])
    seen_params = []
    state = {"calls": 0}

    def fetch(params):
        seen_params.append(dict(params))
        idx = state["calls"]; state["calls"] += 1
        return pages[idx]

    paginate_all(fetch, initial_params={"status": "active", "worknet_id": 11, "limit": 1})
    assert state["calls"] == 2
    # First: original params
    assert seen_params[0] == {"status": "active", "worknet_id": 11, "limit": 1}
    # Second: original params + cursor
    assert seen_params[1] == {"status": "active", "worknet_id": 11, "limit": 1, "cursor": "cursor-1"}


def test_custom_keys():
    """Some endpoints may use items / next_page_token instead of data / next_cursor."""
    page = {
        "items": [{"x": 1}],
        "paging": {"next_page_token": None},
    }
    state = {"calls": 0}

    def fetch(params):
        state["calls"] += 1
        return page

    result = paginate_all(
        fetch,
        initial_params={},
        data_key="items",
        pagination_key="paging",
        cursor_key="next_page_token",
    )
    assert result["items"] == [{"x": 1}]
    assert result["page_count"] == 1


# --- F1: has_more is the authoritative stop signal -------------------------------------------


def test_has_more_false_stops_even_if_cursor_present():
    """OpenAPI marks has_more required and next_cursor nullable — has_more is the authoritative signal.
    If the server emits has_more=false while cursor still has a value (a boundary
    condition), we should stop."""
    pages = [
        {
            "data": [{"id": 1}],
            "pagination": {"next_cursor": "should-not-follow", "has_more": False, "limit": 1},
        },
    ]
    state = {"calls": 0}

    def fetch(params):
        state["calls"] += 1
        return pages[0]

    result = paginate_all(fetch, initial_params={})
    assert state["calls"] == 1, "has_more=false should stop immediately"
    assert result["data"] == [{"id": 1}]


def test_has_more_true_but_no_cursor_stops_safely():
    """has_more=true but next_cursor=null — contradictory input, can't advance, stop safely."""
    page = {
        "data": [{"id": 1}],
        "pagination": {"next_cursor": None, "has_more": True, "limit": 1},
    }
    state = {"calls": 0}

    def fetch(params):
        state["calls"] += 1
        return page

    result = paginate_all(fetch, initial_params={})
    assert state["calls"] == 1
    assert result["data"] == [{"id": 1}]


def test_has_more_omitted_falls_back_to_cursor():
    """When the server omits the has_more field, the presence of a cursor decides whether to continue."""
    pages = [
        {"data": [{"id": 1}], "pagination": {"next_cursor": "c1", "limit": 1}},
        {"data": [{"id": 2}], "pagination": {"next_cursor": None, "limit": 1}},
    ]
    state = {"calls": 0}

    def fetch(params):
        idx = state["calls"]; state["calls"] += 1
        return pages[idx]

    result = paginate_all(fetch, initial_params={})
    assert state["calls"] == 2
    assert result["data"] == [{"id": 1}, {"id": 2}]
