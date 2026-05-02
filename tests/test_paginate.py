"""paginate_all — cursor-based 自动翻页 helper。

不实际打 HTTP；用 fake fetch_page 模拟服务端 cursor 行为。重点核对：
- 单页（next_cursor=None）→ 走一次就停
- 多页 → cursor 接力到底
- max_pages 截断 → 加 truncated 标志
- 服务端略掉 pagination 字段时不崩
"""

import pytest

from lib.govnet_lib import paginate_all


def _make_pages(*page_data):
    """构造一连串 fake 响应，最后一页 next_cursor=None。"""
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
    """生成一个能按 cursor 顺序吐出预设页面的 fetch_page。"""
    state = {"calls": 0, "last_cursor": None}

    def fetch_page(params):
        # 验证 cursor 接力对了
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
    # 取了 2 页就停
    assert result["page_count"] == 2
    assert result["data"] == [{"id": 1}, {"id": 2}]
    assert result.get("truncated_at_max_pages") is True
    # 接力 cursor 暴露给调用方
    assert result["next_cursor"] == "cursor-2"


def test_empty_data_pages_handled():
    # 空 data[] 但有更多页（罕见但可能：filtered listing）
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
    # 服务端不返回 pagination 字段 → 视为终页
    page = {"data": [{"id": 1}, {"id": 2}]}
    state = {"calls": 0}

    def fetch(params):
        state["calls"] += 1
        return page

    result = paginate_all(fetch, initial_params={})
    assert state["calls"] == 1
    assert result["data"] == [{"id": 1}, {"id": 2}]


def test_initial_params_preserved_across_pages():
    """除 cursor 外的所有原始参数应当在每页请求里都带上。"""
    pages = _make_pages([{"id": 1}], [{"id": 2}])
    seen_params = []
    state = {"calls": 0}

    def fetch(params):
        seen_params.append(dict(params))
        idx = state["calls"]; state["calls"] += 1
        return pages[idx]

    paginate_all(fetch, initial_params={"status": "active", "worknet_id": 11, "limit": 1})
    assert state["calls"] == 2
    # 第一次：原始参数
    assert seen_params[0] == {"status": "active", "worknet_id": 11, "limit": 1}
    # 第二次：原始参数 + cursor
    assert seen_params[1] == {"status": "active", "worknet_id": 11, "limit": 1, "cursor": "cursor-1"}


def test_custom_keys():
    """有些端点可能用 items / next_page_token 而非 data / next_cursor。"""
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


# --- F1: has_more 是权威停止信号 -------------------------------------------


def test_has_more_false_stops_even_if_cursor_present():
    """OpenAPI 规定 has_more 必填、next_cursor 可空 — has_more 是权威信号。
    如果服务端发 has_more=false 但 cursor 还有值（边界条件），我们应该停。"""
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
    """has_more=true 但 next_cursor=null — 矛盾输入，无法前进，安全停。"""
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
    """服务端不发 has_more 字段时，cursor 是否存在决定是否继续。"""
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
