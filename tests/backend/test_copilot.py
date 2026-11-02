"""
Tests for the Inventory Copilot (demo engine, tools, SSE endpoint, and the
live tool loop against a mocked Anthropic client - no network calls).
"""
import json
from types import SimpleNamespace

import pytest

import copilot
import main


@pytest.fixture(autouse=True)
def fast_demo(monkeypatch):
    """Demo mode without streaming delays, and a clean order/conversation state"""
    monkeypatch.setenv("COPILOT_MODE", "demo")
    monkeypatch.setenv("COPILOT_DEMO_DELAY", "0")
    main.submitted_orders.clear()
    copilot.conversations.clear()
    yield
    main.submitted_orders.clear()
    copilot.conversations.clear()


@pytest.fixture
def deps():
    return copilot.CopilotDeps(
        inventory_items=main.inventory_items, orders=main.orders, demand_forecasts=main.demand_forecasts,
        get_backlog=main.get_backlog, apply_filters=main.apply_filters, filter_by_month=main.filter_by_month,
        build_restock_plan=main.build_restock_plan, price_restock_lines=main.price_restock_lines,
        get_quarterly_reports=main.get_quarterly_reports, get_monthly_trends=main.get_monthly_trends)


def parse_sse(body):
    """SSE text -> list of (event, data)"""
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def chat(client, message, locale="en", **extra):
    response = client.post("/api/copilot/chat", json={"message": message, "locale": locale, **extra})
    assert response.status_code == 200
    events = parse_sse(response.text)
    text = "".join(d["delta"] for e, d in events if e == "text")
    return events, text


class TestCopilotTools:
    """Tools must agree with the REST endpoints they wrap."""

    def test_inventory_matches_endpoint(self, client, deps):
        tool = copilot.run_tool(deps, "get_inventory", {"warehouse": "Tokyo", "category": None})
        api = client.get("/api/inventory?warehouse=Tokyo").json()
        assert [i["sku"] for i in tool] == [i["sku"] for i in api]

    def test_low_stock_is_at_or_below_reorder_point(self, deps):
        low = copilot.run_tool(deps, "get_low_stock", {"warehouse": None, "category": None})
        assert low
        for item in low:
            assert item["quantity_on_hand"] <= item["reorder_point"]

    def test_order_summary_matches_endpoint(self, client, deps):
        summary = copilot.run_tool(deps, "get_order_summary",
                                   {"warehouse": "London", "category": None, "status": None, "month": "Q1-2025"})
        orders = client.get("/api/orders?warehouse=London&month=Q1-2025").json()
        assert summary["total_orders"] == len(orders)
        assert sum(summary["by_status"].values()) == len(orders)

    def test_restock_plan_matches_endpoint(self, client, deps):
        tool = copilot.run_tool(deps, "get_restock_plan", {"budget": 3000})
        api = client.get("/api/restocking/recommendations?budget=3000").json()
        assert tool == api

    def test_draft_order_prices_without_placing(self, client, deps):
        draft = copilot.run_tool(deps, "draft_restock_order",
                                 {"items": [{"sku": "WDG-001", "quantity": 10}, {"sku": "CTL-330", "quantity": 1}]})
        assert draft["total_value"] == round(10 * 18.5 + 149.0, 2)
        assert draft["lead_time_days"] == 21
        assert client.get("/api/restocking/orders").json() == []

    def test_draft_order_unknown_sku(self, deps):
        with pytest.raises(KeyError):
            copilot.run_tool(deps, "draft_restock_order", {"items": [{"sku": "NOPE-1", "quantity": 1}]})

    def test_validate_input(self):
        assert copilot.validate_input("get_restock_plan", {"budget": 100}) is None
        assert copilot.validate_input("get_restock_plan", {}) == "Missing field budget"
        assert "must be number" in copilot.validate_input("get_restock_plan", {"budget": "lots"})
        assert "must be number" in copilot.validate_input("get_restock_plan", {"budget": True})
        assert "Unexpected" in copilot.validate_input("get_restock_plan", {"budget": 1, "x": 2})
        assert copilot.validate_input("nope", {}) == "Unknown tool nope"


class TestCopilotDemo:
    """Demo engine answers from real data in both locales."""

    @pytest.mark.parametrize("text,intent", [
        ("What should I restock with $3,000?", "restock"),
        ("Which items are low on stock in Tokyo?", "low_stock"),
        ("Show items below their reorder point", "low_stock"),
        ("Why was Q4 fulfillment so low?", "reports"),
        ("How's the backlog?", "backlog"),
        ("Which products have rising demand?", "demand"),
        ("How many orders shipped?", "orders"),
        ("予算30万円で何を補充すべき？", "restock"),
        ("東京で在庫不足の品目は？", "low_stock"),
        ("hello", "help"),
    ])
    def test_detect_intent(self, text, intent):
        assert copilot.detect_intent(text) == intent

    @pytest.mark.parametrize("text,locale,expected", [
        ("restock with $3,000", "en", 3000),
        ("budget 3k", "en", 3000),
        ("予算30万円", "ja", 2000),
        ("¥450,000で補充", "ja", 3000),
        ("what should I restock", "en", None),
    ])
    def test_parse_budget(self, text, locale, expected):
        assert copilot.parse_budget(text, locale) == expected

    def test_restock_answer_and_draft(self, client):
        events, text = chat(client, "What should I restock with $3,000?")
        names = [e for e, _ in events]
        assert names[0] == "meta" and names[-1] == "done"
        drafts = [d for e, d in events if e == "draft_order"]
        assert len(drafts) == 1
        plan = client.get("/api/restocking/recommendations?budget=3000").json()
        assert abs(drafts[0]["total_value"] - plan["total_cost"]) < 0.01
        assert "$3,000" in text and "WDG-001" in text
        assert client.get("/api/restocking/orders").json() == []

    def test_low_stock_answer_uses_warehouse(self, client, deps):
        _, text = chat(client, "Which items are low on stock in Tokyo?")
        low = copilot.run_tool(deps, "get_low_stock", {"warehouse": "Tokyo", "category": None})
        assert f"**{len(low)} items** in Tokyo" in text

    def test_reports_answer_explains_fulfillment(self, client):
        _, text = chat(client, "Why was Q4 fulfillment so low?")
        q4 = next(q for q in client.get("/api/reports/quarterly").json() if q["quarter"] == "Q4-2025")
        assert f"{q4['fulfillment_rate']}%" in text
        assert "not yet delivered" in text

    def test_japanese_answer_in_yen(self, client):
        _, text = chat(client, "予算30万円で何を補充すべき？", locale="ja")
        assert "¥300,000" in text and "$" not in text
        assert "注文を確定" in text

    def test_filters_from_ui_apply(self, client):
        _, text = chat(client, "Any low stock?", filters={"warehouse": "London"})
        assert "in London" in text

    def test_conversation_id_is_reused(self, client):
        events, _ = chat(client, "hello")
        conv_id = events[0][1]["conversation_id"]
        events2, _ = chat(client, "How's the backlog?", conversation_id=conv_id)
        assert events2[0][1]["conversation_id"] == conv_id
        assert len(copilot.conversations[conv_id].messages) == 4

    def test_rejects_empty_message(self, client):
        response = client.post("/api/copilot/chat", json={"message": "", "locale": "en"})
        assert response.status_code == 422

    def test_rejects_unknown_locale(self, client):
        response = client.post("/api/copilot/chat", json={"message": "hi", "locale": "fr"})
        assert response.status_code == 422


class TestCopilotStatus:
    """Mode selection from the environment."""

    def test_demo_without_credentials(self, client, monkeypatch):
        monkeypatch.setenv("COPILOT_MODE", "auto")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert client.get("/api/copilot/status").json() == {"mode": "demo", "model": None}

    def test_live_with_credentials(self, client, monkeypatch):
        monkeypatch.setenv("COPILOT_MODE", "auto")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
        assert client.get("/api/copilot/status").json() == {"mode": "live", "model": copilot.MODEL}


# ---------------------------------------------------------------------------
# Live engine against a scripted fake client
# ---------------------------------------------------------------------------

def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, tool_input, block_id="tu_1"):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(type="text", text=block.text)

    def get_final_message(self):
        return self.message


class FakeClient:
    """Replays scripted responses and records each request"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs):
        # Snapshot: the loop appends to the same list after this call
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.responses.pop(0))


def reply(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


def run(deps, client, text="What should I restock with $3,000?"):
    conv = copilot.Conversation()
    events = list(copilot.run_live(deps, conv, text, "en", {"warehouse": "Tokyo"}, client=client))
    return conv, events


class TestCopilotLiveLoop:
    """The manual tool loop, without calling the API."""

    def test_runs_tools_and_streams_answer(self, deps):
        client = FakeClient([
            reply("tool_use", text_block("Checking. "), tool_block("get_restock_plan", {"budget": 3000})),
            reply("tool_use", tool_block("draft_restock_order",
                                         {"items": [{"sku": "WDG-001", "quantity": 150}]}, "tu_2")),
            reply("end_turn", text_block("Draft ready.")),
        ])
        conv, events = run(deps, client)
        assert [e for e, _ in events] == ["text", "tool", "tool", "draft_order", "text"]
        assert events[3][1]["total_value"] == 2775.0
        # user, assistant, tool_result, assistant, tool_result, assistant
        assert [m["role"] for m in conv.messages] == ["user", "assistant", "user", "assistant", "user", "assistant"]
        assert conv.messages[2]["content"][0]["tool_use_id"] == "tu_1"
        assert main.submitted_orders == []

    def test_request_shape(self, deps):
        client = FakeClient([reply("end_turn", text_block("Hi"))])
        conv, _ = run(deps, client, "hello")
        request = client.requests[0]
        assert request["model"] == copilot.MODEL
        assert request["thinking"] == {"type": "adaptive"}
        assert request["output_config"] == {"effort": "medium"}
        assert request["fallbacks"] == "default"
        assert request["system"] == copilot.SYSTEM_PROMPT
        # Volatile context stays out of the cached system prompt
        assert conv.messages[0]["content"].startswith('[context] locale=en; filters={"warehouse": "Tokyo"}')
        assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in request["tools"])

    def test_invalid_tool_input_returns_error_result(self, deps):
        client = FakeClient([
            reply("tool_use", tool_block("get_restock_plan", {"budget": "lots"})),
            reply("end_turn", text_block("Sorry.")),
        ])
        conv, _ = run(deps, client)
        result = conv.messages[2]["content"][0]
        assert result["is_error"] is True and "INVALID_INPUT" in result["content"]

    def test_tool_exception_returns_error_result(self, deps):
        client = FakeClient([
            reply("tool_use", tool_block("draft_restock_order", {"items": [{"sku": "NOPE", "quantity": 1}]})),
            reply("end_turn", text_block("That SKU doesn't exist.")),
        ])
        conv, events = run(deps, client)
        assert conv.messages[2]["content"][0]["is_error"] is True
        assert "draft_order" not in [e for e, _ in events]

    def test_parallel_results_in_one_message(self, deps):
        client = FakeClient([
            reply("tool_use", tool_block("get_backlog", {}, "a"), tool_block("get_demand_forecasts", {}, "b")),
            reply("end_turn", text_block("Done.")),
        ])
        conv, _ = run(deps, client)
        assert [r["tool_use_id"] for r in conv.messages[2]["content"]] == ["a", "b"]

    def test_stops_after_max_rounds(self, deps):
        responses = [reply("tool_use", tool_block("get_backlog", {}, f"t{i}"))
                     for i in range(copilot.MAX_TOOL_ROUNDS + 1)]
        client = FakeClient(responses)
        _, events = run(deps, client)
        assert events[-1] == ("error", {"code": "too_many_steps"})
        assert len(client.requests) == copilot.MAX_TOOL_ROUNDS + 1

    def test_refusal(self, deps):
        client = FakeClient([reply("refusal", tool_block("get_backlog", {}))])
        conv, events = run(deps, client)
        assert events == [("error", {"code": "refusal"})]
        assert [m["role"] for m in conv.messages] == ["user"]

    def test_truncated_tool_input_not_run(self, deps):
        client = FakeClient([reply("max_tokens", tool_block("get_restock_plan", {"budget": 3000}))])
        conv, events = run(deps, client)
        assert events == [("error", {"code": "max_tokens"})]
        # The unanswered tool_use is not recorded, so the next turn stays valid
        assert [m["role"] for m in conv.messages] == ["user"]

    def test_pause_turn_resumes(self, deps):
        client = FakeClient([reply("pause_turn", text_block("Working... ")), reply("end_turn", text_block("Done."))])
        _, events = run(deps, client)
        assert "".join(d["delta"] for e, d in events if e == "text") == "Working... Done."
        assert len(client.requests) == 2
