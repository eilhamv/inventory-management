"""
Tests for restocking API endpoints.
"""
import pytest

import main


@pytest.fixture(autouse=True)
def clear_submitted_orders():
    """Submitted orders are module-level state; isolate each test."""
    main.submitted_orders.clear()
    yield
    main.submitted_orders.clear()


class TestRestockingRecommendations:
    """Test suite for budget-based restocking recommendations."""

    def test_get_recommendations_structure(self, client):
        """Test the recommendation plan structure."""
        response = client.get("/api/restocking/recommendations?budget=1000")
        assert response.status_code == 200

        plan = response.json()
        for field in ["budget", "total_cost", "remaining_budget", "full_restock_cost", "items"]:
            assert field in plan

        for item in plan["items"]:
            for field in ["sku", "name", "trend", "demand_gap", "quantity",
                          "unit_cost", "line_total", "lead_time_days"]:
                assert field in item
            assert isinstance(item["quantity"], int)
            assert item["quantity"] > 0

    def test_recommendations_stay_within_budget(self, client):
        """Test that recommended totals never exceed the budget."""
        for budget in [0, 50, 1000, 2500, 100000]:
            plan = client.get(f"/api/restocking/recommendations?budget={budget}").json()
            assert plan["total_cost"] <= budget
            assert abs(plan["budget"] - plan["total_cost"] - plan["remaining_budget"]) < 0.01

    def test_line_total_calculation(self, client):
        """Test that each line total is quantity times unit cost."""
        plan = client.get("/api/restocking/recommendations?budget=5000").json()

        for item in plan["items"]:
            assert abs(item["line_total"] - item["quantity"] * item["unit_cost"]) < 0.01
            assert item["quantity"] <= item["demand_gap"]

    def test_increasing_trend_prioritized(self, client):
        """Test that increasing-demand items are recommended before stable ones."""
        plan = client.get("/api/restocking/recommendations?budget=100000").json()
        trends = [item["trend"].lower() for item in plan["items"]]

        assert "increasing" in trends
        last_increasing = max(i for i, t in enumerate(trends) if t == "increasing")
        assert all(t == "increasing" for t in trends[:last_increasing + 1])

    def test_decreasing_demand_not_recommended(self, client):
        """Test that items with no demand gap are never recommended."""
        plan = client.get("/api/restocking/recommendations?budget=100000").json()

        for item in plan["items"]:
            assert item["demand_gap"] > 0
            assert item["trend"].lower() != "decreasing"

    def test_full_budget_covers_all_gaps(self, client):
        """Test that a budget at the full restock cost funds every gap."""
        full_cost = client.get("/api/restocking/recommendations?budget=0").json()["full_restock_cost"]
        plan = client.get(f"/api/restocking/recommendations?budget={full_cost}").json()

        assert abs(plan["total_cost"] - full_cost) < 0.01
        for item in plan["items"]:
            assert item["quantity"] == item["demand_gap"]

    def test_zero_budget_recommends_nothing(self, client):
        """Test that a zero budget returns no items."""
        plan = client.get("/api/restocking/recommendations?budget=0").json()
        assert plan["items"] == []
        assert plan["total_cost"] == 0

    def test_negative_budget_rejected(self, client):
        """Test that a negative budget is a validation error."""
        response = client.get("/api/restocking/recommendations?budget=-1")
        assert response.status_code == 422

    def test_missing_budget_rejected(self, client):
        """Test that the budget parameter is required."""
        response = client.get("/api/restocking/recommendations")
        assert response.status_code == 422


class TestRestockingOrders:
    """Test suite for submitting and listing restocking orders."""

    def test_submit_order(self, client):
        """Test submitting the recommended order."""
        plan = client.get("/api/restocking/recommendations?budget=3000").json()
        lines = [{"sku": i["sku"], "quantity": i["quantity"]} for i in plan["items"]]

        response = client.post("/api/restocking/orders", json={"budget": 3000, "items": lines})
        assert response.status_code == 201

        order = response.json()
        assert order["status"] == "Submitted"
        assert order["order_number"].startswith("RST-")
        assert abs(order["total_value"] - plan["total_cost"]) < 0.01
        assert "T" in order["order_date"]
        assert "T" in order["expected_delivery"]

    def test_lead_time_is_slowest_item(self, client):
        """Test that order lead time is the max of its items' lead times."""
        response = client.post("/api/restocking/orders", json={
            "budget": 10000,
            "items": [{"sku": "FLT-405", "quantity": 10}, {"sku": "CTL-330", "quantity": 1}]
        })
        order = response.json()

        assert order["lead_time_days"] == max(i["lead_time_days"] for i in order["items"])
        assert order["lead_time_days"] == 21

    def test_server_prices_override_client(self, client):
        """Test that unit prices come from server data."""
        demand = {d["item_sku"]: d for d in client.get("/api/demand").json()}
        response = client.post("/api/restocking/orders", json={
            "budget": 10000,
            "items": [{"sku": "WDG-001", "quantity": 2, "unit_price": 0.01}]
        })
        item = response.json()["items"][0]
        assert item["unit_price"] == demand["WDG-001"]["unit_cost"]

    def test_submitted_orders_listed_newest_first(self, client):
        """Test that submitted orders appear in the list, newest first."""
        for qty in [1, 2]:
            client.post("/api/restocking/orders", json={
                "budget": 1000, "items": [{"sku": "GSK-203", "quantity": qty}]
            })

        response = client.get("/api/restocking/orders")
        assert response.status_code == 200

        data = response.json()
        assert len(data) == 2
        assert data[0]["items"][0]["quantity"] == 2
        assert data[1]["items"][0]["quantity"] == 1

    def test_order_over_budget_rejected(self, client):
        """Test that an order costing more than the budget is rejected."""
        response = client.post("/api/restocking/orders", json={
            "budget": 10, "items": [{"sku": "MTR-304", "quantity": 1}]
        })
        assert response.status_code == 400
        assert "exceeds budget" in response.json()["detail"]
        assert client.get("/api/restocking/orders").json() == []

    def test_unknown_sku_rejected(self, client):
        """Test that an unknown SKU returns 404."""
        response = client.post("/api/restocking/orders", json={
            "budget": 1000, "items": [{"sku": "NOPE-999", "quantity": 1}]
        })
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_invalid_order_rejected(self, client):
        """Test that empty orders and non-positive quantities are validation errors."""
        empty = client.post("/api/restocking/orders", json={"budget": 1000, "items": []})
        assert empty.status_code == 422

        zero_qty = client.post("/api/restocking/orders", json={
            "budget": 1000, "items": [{"sku": "GSK-203", "quantity": 0}]
        })
        assert zero_qty.status_code == 422
