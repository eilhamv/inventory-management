"""
Tests for reports API endpoints.
"""
import pytest


class TestQuarterlyReports:
    """Test suite for quarterly report endpoints."""

    def test_get_quarterly_reports(self, client):
        """Test getting quarterly reports."""
        response = client.get("/api/reports/quarterly")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

        for field in ["quarter", "total_orders", "total_revenue", "delivered_orders",
                      "avg_order_value", "fulfillment_rate"]:
            assert field in data[0]

    def test_quarterly_totals_match_orders(self, client):
        """Test that quarterly order counts add up to all orders."""
        quarters = client.get("/api/reports/quarterly").json()
        orders = client.get("/api/orders").json()

        assert sum(q["total_orders"] for q in quarters) == len(orders)
        assert abs(sum(q["total_revenue"] for q in quarters)
                   - sum(o["total_value"] for o in orders)) < 1

    def test_quarterly_calculations(self, client):
        """Test average order value and fulfillment rate per quarter."""
        for q in client.get("/api/reports/quarterly").json():
            assert abs(q["avg_order_value"] - q["total_revenue"] / q["total_orders"]) < 0.01
            assert abs(q["fulfillment_rate"] - q["delivered_orders"] / q["total_orders"] * 100) < 0.1
            assert 0 <= q["fulfillment_rate"] <= 100

    def test_quarterly_sorted(self, client):
        """Test that quarters are returned in chronological order."""
        quarters = [q["quarter"] for q in client.get("/api/reports/quarterly").json()]
        assert quarters == sorted(quarters, key=lambda q: (q[3:], q[:2]))

    def test_quarterly_by_warehouse(self, client):
        """Test filtering quarterly reports by warehouse."""
        quarters = client.get("/api/reports/quarterly?warehouse=Tokyo").json()
        tokyo_orders = client.get("/api/orders?warehouse=Tokyo").json()

        assert sum(q["total_orders"] for q in quarters) == len(tokyo_orders)

    def test_quarterly_by_month(self, client):
        """Test that a month filter narrows reports to that month's quarter."""
        quarters = client.get("/api/reports/quarterly?month=2025-05").json()
        assert [q["quarter"] for q in quarters] == ["Q2-2025"]

    def test_quarterly_multiple_filters(self, client):
        """Test combining category and status filters."""
        quarters = client.get("/api/reports/quarterly?category=sensors&status=Delivered").json()
        orders = client.get("/api/orders?category=sensors&status=Delivered").json()

        assert sum(q["total_orders"] for q in quarters) == len(orders)
        for q in quarters:
            assert q["fulfillment_rate"] == 100.0

    def test_quarterly_no_matches(self, client):
        """Test that a filter matching nothing returns an empty list."""
        response = client.get("/api/reports/quarterly?warehouse=Nowhere")
        assert response.status_code == 200
        assert response.json() == []


class TestMonthlyTrends:
    """Test suite for monthly trend endpoints."""

    def test_get_monthly_trends(self, client):
        """Test getting monthly trends."""
        response = client.get("/api/reports/monthly-trends")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0
        for field in ["month", "order_count", "revenue", "delivered_count"]:
            assert field in data[0]

    def test_monthly_sorted_and_formatted(self, client):
        """Test that months are YYYY-MM and in chronological order."""
        months = [m["month"] for m in client.get("/api/reports/monthly-trends").json()]
        assert months == sorted(months)
        for month in months:
            assert len(month) == 7 and month[4] == "-"

    def test_monthly_by_month(self, client):
        """Test that the month filter returns only that month."""
        data = client.get("/api/reports/monthly-trends?month=2025-03").json()
        orders = client.get("/api/orders?month=2025-03").json()

        assert [m["month"] for m in data] == ["2025-03"]
        assert data[0]["order_count"] == len(orders)

    def test_monthly_by_quarter(self, client):
        """Test that a quarter filter returns that quarter's months."""
        data = client.get("/api/reports/monthly-trends?month=Q1-2025").json()
        assert [m["month"] for m in data] == ["2025-01", "2025-02", "2025-03"]

    def test_monthly_by_category(self, client):
        """Test filtering monthly trends by category."""
        data = client.get("/api/reports/monthly-trends?category=Circuit Boards").json()
        orders = client.get("/api/orders?category=Circuit Boards").json()

        assert sum(m["order_count"] for m in data) == len(orders)
