"""
Inventory Copilot: answers operational questions from live app data.

Two engines share one set of read-only tools:
- live: Claude (claude-opus-5-5 by default) runs a streaming tool-use loop
- demo: a keyword intent router calls the same tools and fills answer
  templates, so the app demos with real numbers and no API key

Neither engine can place an order. draft_restock_order only prices a draft;
the user confirms it in the UI, which calls POST /api/restocking/orders.
"""
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Literal, Optional, Tuple

import anthropic
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

MODEL = os.environ.get("COPILOT_MODEL", "claude-opus-5-5")
MAX_TOOL_ROUNDS = 6
USD_TO_JPY = 150  # Same rate as client/src/utils/currency.js

SYSTEM_PROMPT = """You are the Inventory Copilot for Catalyst Components, a factory inventory management app.
You help operations staff understand stock levels, orders, demand, backlog and performance, and plan restocking.

Ground every number you state in a tool result from this conversation; never estimate figures you could look up.
Prefer one or two well-chosen tool calls over many. Keep answers short: a one-line conclusion first, then at most
five bullet points with the supporting numbers. Use **bold** for the key figure. No tables, no headings, no emojis.

Each user message starts with a [context] line giving the UI locale and the filters active in the app.
Answer in Japanese when locale is ja, otherwise in English. When locale is ja, show money in yen at 1 USD = 150 JPY;
tool results are always in USD. Apply the active filters to your tool calls unless the user asks about something else.

When the user wants to restock or asks what to buy, call get_restock_plan with their budget and then
draft_restock_order with the recommended lines. The app shows the draft with a Confirm button; you cannot place
orders yourself, so say the draft is ready for them to review rather than claiming it was ordered."""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@dataclass
class CopilotDeps:
    """App data access injected by main.py, so this module never imports main"""
    inventory_items: list
    orders: list
    demand_forecasts: list
    get_backlog: Callable[[], list]
    apply_filters: Callable[..., list]
    filter_by_month: Callable[[list, Optional[str]], list]
    build_restock_plan: Callable[[float], dict]
    price_restock_lines: Callable[[List[Tuple[str, int]]], tuple]
    get_quarterly_reports: Callable[..., list]
    get_monthly_trends: Callable[..., list]


def _nullable(type_name: str, description: str) -> dict:
    return {"type": [type_name, "null"], "description": description}


FILTER_PROPS = {
    "warehouse": _nullable("string", "San Francisco, London or Tokyo; null for all"),
    "category": _nullable("string", "Product category, e.g. sensors, circuit boards; null for all"),
}
ORDER_FILTER_PROPS = {
    **FILTER_PROPS,
    "status": _nullable("string", "Delivered, Shipped, Processing or Backordered; null for all"),
    "month": _nullable("string", "YYYY-MM or a quarter like Q4-2025; null for all"),
}


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("get_inventory", "List inventory items with quantity on hand, reorder point and unit cost.", FILTER_PROPS),
    _tool("get_low_stock", "List items at or below their reorder point, most urgent first.", FILTER_PROPS),
    _tool("get_order_summary", "Order counts and value by status for the given filters.", ORDER_FILTER_PROPS),
    _tool("get_demand_forecasts", "Current vs forecasted 30-day demand per SKU, with trend, unit cost and lead time.", {}),
    _tool("get_backlog", "Backlog items: orders that cannot be fulfilled from current stock.", {}),
    _tool("get_reports", "Quarterly performance (revenue, fulfillment rate) and monthly revenue trends.", ORDER_FILTER_PROPS),
    _tool("get_restock_plan", "Recommend restocking within a budget (USD), prioritising increasing demand.",
          {"budget": {"type": "number", "description": "Budget in USD"}}),
    _tool("draft_restock_order",
          "Price a restocking order draft for the user to confirm in the app. Does not place the order.",
          {"items": {
              "type": "array",
              "items": {
                  "type": "object",
                  "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}},
                  "required": ["sku", "quantity"],
                  "additionalProperties": False,
              },
          }}),
]
TOOL_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}

JSON_TYPES = {"string": str, "number": (int, float), "integer": int, "array": list, "object": dict, "null": type(None)}


def validate_input(name: str, args) -> Optional[str]:
    """Check tool input against its schema; returns an error message or None.

    Eager input streaming skips server-side validation and the SDK's tolerant
    parser can return a truncated object, so every call is checked here.
    """
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return f"Unknown tool {name}"
    if not isinstance(args, dict):
        return "Input must be an object"
    for key, prop in schema["properties"].items():
        if key not in args:
            return f"Missing field {key}"
        types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
        value = args[key]
        # bool is an int subclass in Python but never a valid number here
        if isinstance(value, bool) or not any(isinstance(value, JSON_TYPES[t]) for t in types):
            return f"Field {key} must be {' or '.join(types)}"
    extra = set(args) - set(schema["properties"])
    return f"Unexpected fields {sorted(extra)}" if extra else None


def run_tool(deps: CopilotDeps, name: str, args: dict):
    """Execute a validated tool call; returns JSON-serialisable data"""
    if name == "get_inventory":
        items = deps.apply_filters(deps.inventory_items, args["warehouse"], args["category"])
        return [{k: i[k] for k in ("sku", "name", "category", "warehouse", "quantity_on_hand",
                                   "reorder_point", "unit_cost")} for i in items]
    if name == "get_low_stock":
        items = deps.apply_filters(deps.inventory_items, args["warehouse"], args["category"])
        low = [i for i in items if i["quantity_on_hand"] <= i["reorder_point"]]
        low.sort(key=lambda i: i["quantity_on_hand"] / max(i["reorder_point"], 1))
        return [{"sku": i["sku"], "name": i["name"], "warehouse": i["warehouse"],
                 "quantity_on_hand": i["quantity_on_hand"], "reorder_point": i["reorder_point"]} for i in low]
    if name == "get_order_summary":
        found = deps.filter_by_month(
            deps.apply_filters(deps.orders, args["warehouse"], args["category"], args["status"]), args["month"])
        by_status: Dict[str, int] = {}
        for o in found:
            by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        total = round(sum(o["total_value"] for o in found), 2)
        return {"total_orders": len(found), "total_value_usd": total, "by_status": by_status,
                "avg_order_value_usd": round(total / len(found), 2) if found else 0}
    if name == "get_demand_forecasts":
        return deps.demand_forecasts
    if name == "get_backlog":
        return deps.get_backlog()
    if name == "get_reports":
        filters = (args["warehouse"], args["category"], args["status"], args["month"])
        return {"quarterly": deps.get_quarterly_reports(*filters), "monthly": deps.get_monthly_trends(*filters)}
    if name == "get_restock_plan":
        if args["budget"] < 0:
            raise ValueError("Budget must be zero or more")
        return deps.build_restock_plan(float(args["budget"]))
    if name == "draft_restock_order":
        lines = [(line["sku"], line["quantity"]) for line in args["items"] if line["quantity"] > 0]
        if not lines:
            raise ValueError("A draft needs at least one line with a positive quantity")
        items, total, lead = deps.price_restock_lines(lines)
        return {"items": items, "total_value": total, "lead_time_days": lead}
    raise ValueError(f"Unknown tool {name}")


# ---------------------------------------------------------------------------
# Conversations and events
# ---------------------------------------------------------------------------

@dataclass
class Conversation:
    # Append-only: preserved thinking rejects edited history on current models
    messages: list = field(default_factory=list)


conversations: Dict[str, Conversation] = {}

Event = Tuple[str, dict]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def copilot_mode() -> str:
    """live when credentials are configured, demo otherwise; COPILOT_MODE overrides"""
    forced = os.environ.get("COPILOT_MODE", "auto").lower()
    if forced in ("live", "demo"):
        return forced
    has_credentials = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    return "live" if has_credentials else "demo"


def context_line(locale: str, filters: dict) -> str:
    active = {k: v for k, v in filters.items() if v and v != "all"}
    return f"[context] locale={locale}; filters={json.dumps(active) if active else 'none'}"


# ---------------------------------------------------------------------------
# Live engine: Claude with a streaming manual tool loop
# ---------------------------------------------------------------------------

def run_live(deps: CopilotDeps, conv: Conversation, text: str, locale: str, filters: dict,
             client: Optional[anthropic.Anthropic] = None) -> Iterator[Event]:
    client = client or anthropic.Anthropic()
    # Volatile context rides in the user turn so the system prompt + tools prefix stays cacheable
    conv.messages.append({"role": "user", "content": f"{context_line(locale, filters)}\n{text}"})

    json_retries = 0
    rounds = 0
    while True:
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=conv.messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        yield "text", {"delta": event.text}
                response = stream.get_final_message()
            json_retries = 0
        except ValueError:
            # Tool input JSON the SDK could not parse at all; no tool_use id to answer, so re-issue the turn
            json_retries += 1
            if json_retries > 2:
                raise
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        # A refused or truncated turn may hold a tool_use we won't answer; recording it would make the
        # conversation's next request invalid, so it is left out (nothing after it exists yet to invalidate)
        if response.stop_reason == "refusal":
            yield "error", {"code": "refusal"}
            return
        if response.stop_reason == "max_tokens" and tool_uses:
            yield "error", {"code": "max_tokens"}
            return

        conv.messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        if not tool_uses:
            return
        rounds += 1
        if rounds > MAX_TOOL_ROUNDS:
            yield "error", {"code": "too_many_steps"}
            return

        # All results go back in one user message so Claude keeps making parallel calls
        results = []
        for block in tool_uses:
            yield "tool", {"name": block.name}
            problem = validate_input(block.name, block.input)
            if problem:
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"INVALID_INPUT: {problem}"})
                continue
            try:
                data = run_tool(deps, block.name, block.input)
            except (KeyError, ValueError) as e:
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"{type(e).__name__}: {e}"})
                continue
            if block.name == "draft_restock_order":
                yield "draft_order", data
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(data)})
        conv.messages.append({"role": "user", "content": results})


# ---------------------------------------------------------------------------
# Demo engine: keyword intents over the same tools, EN/JA templates
# ---------------------------------------------------------------------------

WAREHOUSES = {"san francisco": "San Francisco", "サンフランシスコ": "San Francisco",
              "london": "London", "ロンドン": "London", "tokyo": "Tokyo", "東京": "Tokyo"}

INTENTS = [
    # low_stock before restock: "reorder point" would otherwise match "reorder"
    ("low_stock", ("low stock", "low on stock", "running low", "shortage", "reorder point", "在庫不足", "在庫切れ", "少ない")),
    ("restock", ("restock", "reorder", "replenish", "buy", "budget", "purchase", "補充", "発注", "予算", "仕入")),
    ("backlog", ("backlog", "delay", "delayed", "behind", "バックログ", "遅延")),
    ("reports", ("quarter", "q1", "q2", "q3", "q4", "revenue", "fulfil", "fulfill", "performance",
                 "四半期", "収益", "売上", "フルフィルメント", "業績")),
    ("demand", ("demand", "forecast", "trend", "需要", "予測", "トレンド")),
    ("orders", ("order", "status", "shipped", "delivered", "注文", "出荷")),
]


def detect_intent(text: str) -> str:
    lowered = text.lower()
    for intent, words in INTENTS:
        if any(w in lowered for w in words):
            return intent
    return "help"


def parse_budget(text: str, locale: str) -> Optional[float]:
    """'$3,000', '3000', '3k', '¥450,000', '45万円' -> USD"""
    lowered = text.lower().replace(",", "")
    m = re.search(r"([\d.]+)\s*万", lowered)
    if m:
        return float(m.group(1)) * 10000 / USD_TO_JPY
    m = re.search(r"[$¥￥]?\s*(\d+(?:\.\d+)?)\s*(k)?\s*(円)?", lowered)
    if not m:
        return None
    amount = float(m.group(1)) * (1000 if m.group(2) else 1)
    is_yen = bool(m.group(3)) or "¥" in lowered or "￥" in lowered
    return amount / USD_TO_JPY if is_yen else amount


def money(amount_usd: float, locale: str) -> str:
    if locale == "ja":
        return f"¥{round(amount_usd * USD_TO_JPY):,}"
    return f"${amount_usd:,.0f}" if abs(amount_usd) >= 1000 else f"${amount_usd:,.2f}"


def demo_answer(deps: CopilotDeps, text: str, locale: str, filters: dict) -> Iterator[Event]:
    """Yield tool/draft events and return the full answer text (via StopIteration value)"""
    ja = locale == "ja"
    intent = detect_intent(text)
    warehouse = next((v for k, v in WAREHOUSES.items() if k in text.lower()), None) \
        or (filters.get("warehouse") if filters.get("warehouse") not in (None, "all") else None)
    category = filters.get("category") if filters.get("category") not in (None, "all") else None

    def call(name, **args):
        return name, run_tool(deps, name, args)

    if intent == "restock":
        full = deps.build_restock_plan(0)["full_restock_cost"]
        budget = parse_budget(text, locale) or round(full / 2, -2)
        yield "tool", {"name": "get_restock_plan"}
        plan = run_tool(deps, "get_restock_plan", {"budget": budget})
        if not plan["items"]:
            return (f"{money(budget, locale)} では補充できる品目がありません。全需要ギャップの補充には **{money(full, locale)}** 必要です。"
                    if ja else f"A budget of {money(budget, locale)} can't cover any item yet. Filling every demand gap costs **{money(full, locale)}**.")
        yield "tool", {"name": "draft_restock_order"}
        draft = run_tool(deps, "draft_restock_order",
                         {"items": [{"sku": i["sku"], "quantity": i["quantity"]} for i in plan["items"]]})
        yield "draft_order", draft
        lines = [f"- {i['name']} ({i['sku']}): {i['quantity']}/{i['demand_gap']} → {money(i['line_total'], locale)}"
                 for i in plan["items"]]
        if ja:
            head = (f"予算 {money(budget, locale)} で **{len(plan['items'])}品目** の補充をおすすめします。"
                    f"合計 **{money(plan['total_cost'], locale)}**、残り {money(plan['remaining_budget'], locale)}、"
                    f"リードタイム {draft['lead_time_days']}日。")
            tail = "下のドラフトを確認し、問題なければ「注文を確定」を押してください。"
        else:
            head = (f"With {money(budget, locale)} I'd restock **{len(plan['items'])} items** for "
                    f"**{money(plan['total_cost'], locale)}**, leaving {money(plan['remaining_budget'], locale)}; "
                    f"delivery in {draft['lead_time_days']} days.")
            tail = "Increasing-demand items come first. Review the draft below and confirm it if it looks right."
        return "\n".join([head, *lines, tail])

    if intent == "low_stock":
        yield "tool", {"name": "get_low_stock"}
        low = run_tool(deps, "get_low_stock", {"warehouse": warehouse, "category": category})
        where = warehouse or ("全倉庫" if ja else "all warehouses")
        if not low:
            return f"{where}で再発注点を下回る品目はありません。" if ja else f"Nothing in {where} is at or below its reorder point."
        lines = [f"- {i['name']} ({i['sku']}, {i['warehouse']}): {i['quantity_on_hand']} / {i['reorder_point']}"
                 for i in low[:5]]
        head = (f"{where}で **{len(low)}品目** が再発注点以下です（在庫 / 再発注点）:" if ja
                else f"**{len(low)} items** in {where} are at or below their reorder point (on hand / reorder point):")
        return "\n".join([head, *lines])

    if intent == "backlog":
        yield "tool", {"name": "get_backlog"}
        backlog = run_tool(deps, "get_backlog", {})
        high = [b for b in backlog if b["priority"] == "high"]
        lines = [f"- {b['order_id']} {b['item_name']}: {b['quantity_needed'] - b['quantity_available']}"
                 f"{'個不足' if ja else ' short'}, {b['days_delayed']}"
                 f"{'日遅延' if ja else (' day late' if b['days_delayed'] == 1 else ' days late')}"
                 for b in sorted(backlog, key=lambda b: -b["days_delayed"])]
        head = (f"バックログは **{len(backlog)}件**（優先度高 {len(high)}件）です:" if ja
                else f"There are **{len(backlog)} backlog items**, {len(high)} high priority:")
        return "\n".join([head, *lines])

    if intent == "reports":
        yield "tool", {"name": "get_reports"}
        reports = run_tool(deps, "get_reports", {"warehouse": warehouse, "category": category,
                                                 "status": None, "month": None})
        quarters = reports["quarterly"]
        if not quarters:
            return "該当する注文がありません。" if ja else "No orders match those filters."
        best = max(quarters, key=lambda q: q["total_revenue"])
        worst = min(quarters, key=lambda q: q["fulfillment_rate"])
        yield "tool", {"name": "get_order_summary"}
        summary = run_tool(deps, "get_order_summary", {"warehouse": warehouse, "category": category,
                                                       "status": None, "month": worst["quarter"]})
        open_orders = {s: n for s, n in summary["by_status"].items() if s != "Delivered"}
        status_ja = {"Processing": "処理中", "Backordered": "入荷待ち", "Shipped": "出荷済み"}
        breakdown = ", ".join(f"{status_ja.get(s, s) if ja else s} {n}"
                              for s, n in sorted(open_orders.items(), key=lambda kv: -kv[1]))
        # When the top-revenue quarter is also the weakest on fulfillment, say so in one line
        same = best["quarter"] == worst["quarter"]
        if ja:
            return "\n".join([
                f"**{best['quarter']}** は収益が最も高い（{money(best['total_revenue'], locale)}）一方、"
                f"フルフィルメント率は最も低い **{worst['fulfillment_rate']}%** です。" if same else
                f"収益が最も高いのは **{best['quarter']}**（{money(best['total_revenue'], locale)}）です。",
                *([] if same else [f"- フルフィルメント率が最も低いのは {worst['quarter']} の **{worst['fulfillment_rate']}%**"]),
                f"- {worst['quarter']} の {worst['total_orders']}件中、未配達: {breakdown}",
                "- フルフィルメント率は配達済み ÷ 全注文なので、処理中・入荷待ち・輸送中の注文が多い直近の四半期は低く出ます。",
            ])
        return "\n".join([
            f"**{best['quarter']}** had the highest revenue ({money(best['total_revenue'], locale)}) "
            f"but the lowest fulfillment rate, **{worst['fulfillment_rate']}%**." if same else
            f"**{best['quarter']}** had the highest revenue at {money(best['total_revenue'], locale)}.",
            *([] if same else [f"- Lowest fulfillment: {worst['quarter']} at **{worst['fulfillment_rate']}%**"]),
            f"- Of its {worst['total_orders']} orders, not yet delivered: {breakdown}",
            "- Fulfillment is delivered ÷ all orders, so the latest quarter scores low while many orders are still processing, backordered or in transit.",
        ])

    if intent == "demand":
        yield "tool", {"name": "get_demand_forecasts"}
        forecasts = run_tool(deps, "get_demand_forecasts", {})
        rising = sorted((f for f in forecasts if f["trend"] == "increasing"),
                        key=lambda f: f["current_demand"] - f["forecasted_demand"])
        lines = [f"- {f['item_name']} ({f['item_sku']}): {f['current_demand']} → **{f['forecasted_demand']}** "
                 f"(+{round((f['forecasted_demand'] / f['current_demand'] - 1) * 100)}%)" for f in rising]
        head = (f"今後30日で需要が増える品目は **{len(rising)}件** です:" if ja
                else f"**{len(rising)} items** have rising demand over the next 30 days:")
        return "\n".join([head, *lines])

    if intent == "orders":
        yield "tool", {"name": "get_order_summary"}
        s = run_tool(deps, "get_order_summary", {"warehouse": warehouse, "category": category,
                                                 "status": None, "month": filters.get("month")})
        lines = [f"- {k}: {v}" for k, v in sorted(s["by_status"].items(), key=lambda kv: -kv[1])]
        head = (f"注文は **{s['total_orders']}件**、合計 {money(s['total_value_usd'], locale)} です:" if ja
                else f"**{s['total_orders']} orders** worth {money(s['total_value_usd'], locale)}:")
        return "\n".join([head, *lines])

    if ja:
        return ("在庫・注文・需要・バックログ・レポートについて質問できます。例:\n"
                "- 予算30万円で何を補充すべき？\n- 東京で在庫不足の品目は？\n- Q4のフルフィルメント率が低い理由は？")
    return ("I can answer questions about stock, orders, demand, backlog and performance, and draft restocking orders. Try:\n"
            "- What should I restock with $3,000?\n- Which items are low on stock in Tokyo?\n- Why was Q4 fulfillment so low?")


def run_demo(deps: CopilotDeps, conv: Conversation, text: str, locale: str, filters: dict) -> Iterator[Event]:
    delay = float(os.environ.get("COPILOT_DEMO_DELAY", "0.02"))
    conv.messages.append({"role": "user", "content": f"{context_line(locale, filters)}\n{text}"})
    gen = demo_answer(deps, text, locale, filters)
    while True:
        try:
            event = next(gen)
        except StopIteration as done:
            answer = done.value
            break
        yield event
        time.sleep(delay * 10)  # let the tool chip show briefly
    # Stream in small chunks so the UI behaves exactly as it does with Claude
    for chunk in re.findall(r"\S+\s*|\s+", answer):
        yield "text", {"delta": chunk}
        time.sleep(delay)
    conv.messages.append({"role": "assistant", "content": answer})


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ChatFilters(BaseModel):
    warehouse: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    month: Optional[str] = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = None
    locale: Literal["en", "ja"] = "en"
    filters: ChatFilters = ChatFilters()


def create_router(deps: CopilotDeps) -> APIRouter:
    router = APIRouter(prefix="/api/copilot")

    @router.get("/status")
    def copilot_status():
        mode = copilot_mode()
        return {"mode": mode, "model": MODEL if mode == "live" else None}

    @router.post("/chat")
    def copilot_chat(request: ChatRequest):
        conv_id = request.conversation_id if request.conversation_id in conversations else uuid.uuid4().hex
        conv = conversations.setdefault(conv_id, Conversation())
        mode = copilot_mode()
        filters = request.filters.model_dump()

        def events() -> Iterator[str]:
            yield sse("meta", {"conversation_id": conv_id, "mode": mode})
            engine = run_live if mode == "live" else run_demo
            try:
                for name, data in engine(deps, conv, request.message, request.locale, filters):
                    yield sse(name, data)
            except anthropic.AuthenticationError:
                yield sse("error", {"code": "auth"})
            except anthropic.RateLimitError:
                yield sse("error", {"code": "rate_limit"})
            except anthropic.APIStatusError as e:
                yield sse("error", {"code": "api", "status": e.status_code})
            except anthropic.APIConnectionError:
                yield sse("error", {"code": "connection"})
            yield sse("done", {})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
