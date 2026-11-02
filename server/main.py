from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from datetime import date, datetime, timedelta
from typing import List, Literal, Optional
from pydantic import BaseModel, Field
from copilot import CopilotDeps, create_router as create_copilot_router
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str
    unit_cost: float
    lead_time_days: int

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class PurchaseOrder(BaseModel):
    id: str
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    status: str
    created_date: str
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    notes: Optional[str] = None

class RestockRecommendation(BaseModel):
    sku: str
    name: str
    trend: str
    demand_gap: int
    quantity: int
    unit_cost: float
    line_total: float
    lead_time_days: int

class RestockPlan(BaseModel):
    budget: float
    total_cost: float
    remaining_budget: float
    full_restock_cost: float
    items: List[RestockRecommendation]

class RestockOrderLine(BaseModel):
    sku: str
    quantity: int = Field(gt=0)

class CreateRestockOrderRequest(BaseModel):
    budget: float = Field(ge=0)
    items: List[RestockOrderLine] = Field(min_length=1)

class SubmittedOrderItem(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price: float
    lead_time_days: int

class SubmittedOrder(BaseModel):
    id: str
    order_number: str
    items: List[SubmittedOrderItem]
    status: str
    order_date: str
    lead_time_days: int
    expected_delivery: str
    total_value: float

class QuarterlyReport(BaseModel):
    quarter: str
    total_orders: int
    total_revenue: float
    delivered_orders: int
    avg_order_value: float
    fulfillment_rate: float

class MonthlyTrend(BaseModel):
    month: str
    order_count: int
    revenue: float
    delivered_count: int

class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    priority: Literal['high', 'medium', 'low'] = 'medium'
    dueDate: date

class Task(CreateTaskRequest):
    id: str
    status: Literal['pending', 'completed'] = 'pending'

# Restocking orders live only in memory, like the rest of the mock data
submitted_orders: List[dict] = []

# User-created tasks, in memory too. IDs are strings ("TASK-1") so they never
# collide with the integer IDs of the mock tasks in client/src/composables/useAuth.js,
# which App.vue checks first when deleting or toggling.
tasks: List[dict] = []
task_sequence = 0

# Increasing demand is restocked first, decreasing last
TREND_PRIORITY = {'increasing': 0, 'stable': 1, 'decreasing': 2}

def demand_gap(forecast: dict) -> int:
    """Units needed to cover forecasted demand above current demand"""
    return max(forecast['forecasted_demand'] - forecast['current_demand'], 0)

def build_restock_plan(budget: float) -> dict:
    """Greedy restock plan: fill the highest-priority demand gaps until the budget runs out"""
    candidates = [f for f in demand_forecasts if demand_gap(f) > 0]
    candidates.sort(key=lambda f: (TREND_PRIORITY.get(f['trend'].lower(), 3), -demand_gap(f)))

    remaining = budget
    items = []
    for forecast in candidates:
        gap = demand_gap(forecast)
        # Buy the whole gap if affordable; otherwise take what the budget allows and stop,
        # so lower-priority items never jump ahead of a partially funded one
        quantity = min(gap, int(remaining // forecast['unit_cost']))
        if quantity > 0:
            line_total = round(quantity * forecast['unit_cost'], 2)
            items.append({
                'sku': forecast['item_sku'],
                'name': forecast['item_name'],
                'trend': forecast['trend'],
                'demand_gap': gap,
                'quantity': quantity,
                'unit_cost': forecast['unit_cost'],
                'line_total': line_total,
                'lead_time_days': forecast['lead_time_days']
            })
            remaining = round(remaining - line_total, 2)
        if quantity < gap:
            break

    total_cost = round(sum(item['line_total'] for item in items), 2)
    return {
        'budget': budget,
        'total_cost': total_cost,
        'remaining_budget': round(budget - total_cost, 2),
        'full_restock_cost': round(sum(demand_gap(f) * f['unit_cost'] for f in candidates), 2),
        'items': items
    }

def price_restock_lines(lines):
    """Price (sku, quantity) pairs from forecast data -> (items, total_value, lead_time_days).

    Raises KeyError(sku) for an unknown SKU. Shared by order submission and the
    copilot's draft orders so both quote identical prices.
    """
    forecasts_by_sku = {f['item_sku']: f for f in demand_forecasts}
    items = []
    for sku, quantity in lines:
        forecast = forecasts_by_sku.get(sku)
        if not forecast:
            raise KeyError(sku)
        items.append({
            'sku': sku,
            'name': forecast['item_name'],
            'quantity': quantity,
            'unit_price': forecast['unit_cost'],
            'lead_time_days': forecast['lead_time_days']
        })
    total_value = round(sum(item['quantity'] * item['unit_price'] for item in items), 2)
    # The order arrives when its slowest item does
    lead_time_days = max((item['lead_time_days'] for item in items), default=0)
    return items, total_value, lead_time_days

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order
        has_po = any(po["backlog_item_id"] == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

@app.get("/api/restocking/recommendations", response_model=RestockPlan)
def get_restocking_recommendations(budget: float = Query(ge=0)):
    """Recommend demand-forecast items to restock within the given budget"""
    return build_restock_plan(budget)

@app.post("/api/restocking/orders", response_model=SubmittedOrder, status_code=201)
def create_restocking_order(request: CreateRestockOrderRequest):
    """Submit a restocking order; prices and lead times come from server data, not the client"""
    try:
        items, total_value, lead_time_days = price_restock_lines(
            [(line.sku, line.quantity) for line in request.items])
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"SKU {e.args[0]} not found in demand forecast")
    if total_value > request.budget:
        raise HTTPException(status_code=400, detail=f"Order total {total_value} exceeds budget {request.budget}")

    order_date = datetime.now().replace(microsecond=0)
    sequence = len(submitted_orders) + 1
    order = {
        'id': f"RST-{sequence}",
        'order_number': f"RST-{order_date.year}-{sequence:04d}",
        'items': items,
        'status': 'Submitted',
        'order_date': order_date.isoformat(),
        'lead_time_days': lead_time_days,
        'expected_delivery': (order_date + timedelta(days=lead_time_days)).isoformat(),
        'total_value': total_value
    }
    submitted_orders.append(order)
    return order

@app.get("/api/restocking/orders", response_model=List[SubmittedOrder])
def get_restocking_orders():
    """Get submitted restocking orders, newest first"""
    return list(reversed(submitted_orders))

def find_task(task_id: str) -> dict:
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task

@app.get("/api/tasks", response_model=List[Task])
def get_tasks():
    """Get user-created tasks, newest first (matching how the UI adds them)"""
    return list(reversed(tasks))

@app.post("/api/tasks", response_model=Task, status_code=201)
def create_task(request: CreateTaskRequest):
    """Create a pending task"""
    global task_sequence
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Task title cannot be blank")
    task_sequence += 1
    task = {**request.model_dump(), 'title': title, 'id': f"TASK-{task_sequence}", 'status': 'pending'}
    tasks.append(task)
    return task

@app.patch("/api/tasks/{task_id}", response_model=Task)
def toggle_task(task_id: str):
    """Toggle a task between pending and completed; the UI sends no body"""
    task = find_task(task_id)
    task['status'] = 'completed' if task['status'] == 'pending' else 'pending'
    return task

@app.delete("/api/tasks/{task_id}", response_model=Task)
def delete_task(task_id: str):
    """Delete a task and return it"""
    task = find_task(task_id)
    tasks.remove(task)
    return task

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    return category_spending

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

def filtered_report_orders(warehouse, category, status, month) -> list:
    """Orders narrowed by the same global filters the other pages use"""
    return filter_by_month(apply_filters(orders, warehouse, category, status), month)

def quarter_of(order_date: str) -> Optional[str]:
    """'2025-05-14T...' -> 'Q2-2025'; None when the date is missing or malformed"""
    try:
        year, month = int(order_date[:4]), int(order_date[5:7])
    except (ValueError, TypeError):
        return None
    if not 1 <= month <= 12:
        return None
    return f"Q{(month - 1) // 3 + 1}-{year}"

@app.get("/api/reports/quarterly", response_model=List[QuarterlyReport])
def get_quarterly_reports(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get quarterly performance reports with optional filtering"""
    quarters = {}
    for order in filtered_report_orders(warehouse, category, status, month):
        quarter = quarter_of(order.get('order_date', ''))
        if not quarter:
            continue
        data = quarters.setdefault(quarter, {
            'quarter': quarter,
            'total_orders': 0,
            'total_revenue': 0,
            'delivered_orders': 0
        })
        data['total_orders'] += 1
        data['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            data['delivered_orders'] += 1

    result = []
    for data in quarters.values():
        data['total_revenue'] = round(data['total_revenue'], 2)
        data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
        data['fulfillment_rate'] = round(data['delivered_orders'] / data['total_orders'] * 100, 1)
        result.append(data)

    # 'Q1-2025' doesn't sort chronologically across years, so sort by (year, quarter)
    result.sort(key=lambda x: (x['quarter'][3:], x['quarter'][:2]))
    return result

@app.get("/api/reports/monthly-trends", response_model=List[MonthlyTrend])
def get_monthly_trends(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get month-over-month trends with optional filtering"""
    months = {}
    for order in filtered_report_orders(warehouse, category, status, month):
        order_date = order.get('order_date', '')
        if not order_date:
            continue
        key = order_date[:7]  # YYYY-MM
        data = months.setdefault(key, {
            'month': key,
            'order_count': 0,
            'revenue': 0,
            'delivered_count': 0
        })
        data['order_count'] += 1
        data['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            data['delivered_count'] += 1

    result = sorted(months.values(), key=lambda x: x['month'])
    for data in result:
        data['revenue'] = round(data['revenue'], 2)
    return result

# Inventory Copilot (demo mode without credentials, Claude with them)
app.include_router(create_copilot_router(CopilotDeps(
    inventory_items=inventory_items,
    orders=orders,
    demand_forecasts=demand_forecasts,
    get_backlog=get_backlog,
    apply_filters=apply_filters,
    filter_by_month=filter_by_month,
    build_restock_plan=build_restock_plan,
    price_restock_lines=price_restock_lines,
    get_quarterly_reports=get_quarterly_reports,
    get_monthly_trends=get_monthly_trends,
)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
