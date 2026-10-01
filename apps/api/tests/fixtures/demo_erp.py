"""Deterministic demo ERP generator for SQLite (dev/tests) and PostgreSQL (compose demo).

Layout
------
* Raw transactional tables (PostgreSQL schema ``erp``, SQLite ``main``): regions,
  branches, customers (with sensitive raw columns that are *never* exposed in views),
  products, warehouses, sales orders + lines, inventory balances + movements,
  invoices, payments, suppliers, purchase orders.
* Reporting views (PostgreSQL schema ``analytics``, SQLite ``main``) named
  ``analytics_*``. The semantic layer (``semantic-layer/``) binds only to these.

Every row carries ``tenant_code`` (the tenant slug), which the semantic layer
uses as a mandatory row filter.

Determinism: all data comes from ``random.Random(seed)`` and is laid out relative
to ``anchor_date`` (18 months back). Views compute ages (``days_open``,
``days_overdue``) against the same anchor instead of ``CURRENT_DATE``, so a given
(seed, anchor) always produces identical answers.

Built-in storylines (used by the example questions):
* West region sales drop over the last ~90 days ("why did sales decrease in West").
* A product literally named "Product A" with movements in the anchor's quarter.
* Some open orders older than seven days, products below minimum stock,
  overdue invoices and late purchase orders.
"""

# View SQL below is assembled only from module constants and a validated date
# object (no user input), so string-built SQL is safe here.
from __future__ import annotations

import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

DEFAULT_SEED: Final = 42
HISTORY_DAYS: Final = 540  # ~18 months
BATCH_SIZE: Final = 1000
_TENANT_CODE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

metadata = sa.MetaData()


def _table(name: str, *columns: sa.Column[Any]) -> sa.Table:
    return sa.Table(
        name, metadata, *columns, sa.Column("tenant_code", sa.String(63), nullable=False)
    )


def _money(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Numeric(14, 2), nullable=nullable)


regions = _table(
    "regions",
    sa.Column("region_id", sa.Integer, primary_key=True),
    sa.Column("region_name", sa.String(50), nullable=False),
)
branches = _table(
    "branches",
    sa.Column("branch_id", sa.Integer, primary_key=True),
    sa.Column("branch_name", sa.String(80), nullable=False),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
)
customers = _table(
    "customers",
    sa.Column("customer_id", sa.Integer, primary_key=True),
    sa.Column("customer_name", sa.String(120), nullable=False),
    sa.Column("segment", sa.String(30), nullable=False),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
    # Sensitive raw columns - deliberately absent from every analytics view.
    sa.Column("email", sa.String(120), nullable=False),
    sa.Column("phone", sa.String(30), nullable=False),
    sa.Column("bank_account", sa.String(40), nullable=False),
    sa.Column("tax_id", sa.String(30), nullable=False),
    _money("credit_limit"),
    sa.Column("created_date", sa.Date, nullable=False),
)
products = _table(
    "products",
    sa.Column("product_id", sa.Integer, primary_key=True),
    sa.Column("sku", sa.String(30), nullable=False),
    sa.Column("product_name", sa.String(120), nullable=False),
    sa.Column("category", sa.String(50), nullable=False),
    _money("unit_price"),
    _money("unit_cost"),
)
warehouses = _table(
    "warehouses",
    sa.Column("warehouse_id", sa.Integer, primary_key=True),
    sa.Column("warehouse_code", sa.String(20), nullable=False),
    sa.Column("warehouse_name", sa.String(80), nullable=False),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
)
sales_orders = _table(
    "sales_orders",
    sa.Column("order_id", sa.Integer, primary_key=True),
    sa.Column("order_number", sa.String(20), nullable=False),
    sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.customer_id"), nullable=False),
    sa.Column("branch_id", sa.Integer, sa.ForeignKey("branches.branch_id"), nullable=False),
    sa.Column("order_date", sa.Date, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("closed_date", sa.Date, nullable=True),
    _money("gross_amount"),
    _money("discount_amount"),
    _money("net_amount"),
)
sales_order_lines = _table(
    "sales_order_lines",
    sa.Column("line_id", sa.Integer, primary_key=True),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("sales_orders.order_id"), nullable=False),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.product_id"), nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    _money("unit_price"),
    _money("discount_amount"),
    _money("net_amount"),
    _money("cost_amount"),
)
inventory_balances = _table(
    "inventory_balances",
    sa.Column("balance_id", sa.Integer, primary_key=True),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.product_id"), nullable=False),
    sa.Column("warehouse_id", sa.Integer, sa.ForeignKey("warehouses.warehouse_id"), nullable=False),
    sa.Column("on_hand_qty", sa.Integer, nullable=False),
    sa.Column("min_stock_qty", sa.Integer, nullable=False),
)
inventory_movements = _table(
    "inventory_movements",
    sa.Column("movement_id", sa.Integer, primary_key=True),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.product_id"), nullable=False),
    sa.Column("warehouse_id", sa.Integer, sa.ForeignKey("warehouses.warehouse_id"), nullable=False),
    sa.Column("movement_date", sa.Date, nullable=False),
    sa.Column("movement_type", sa.String(20), nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("reference", sa.String(30), nullable=False),
)
invoices = _table(
    "invoices",
    sa.Column("invoice_id", sa.Integer, primary_key=True),
    sa.Column("invoice_number", sa.String(20), nullable=False),
    sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.customer_id"), nullable=False),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("sales_orders.order_id"), nullable=False),
    sa.Column("invoice_date", sa.Date, nullable=False),
    sa.Column("due_date", sa.Date, nullable=False),
    _money("amount"),
    sa.Column("status", sa.String(20), nullable=False),
)
payments = _table(
    "payments",
    sa.Column("payment_id", sa.Integer, primary_key=True),
    sa.Column("invoice_id", sa.Integer, sa.ForeignKey("invoices.invoice_id"), nullable=False),
    sa.Column("payment_date", sa.Date, nullable=False),
    _money("amount"),
    sa.Column("payment_token", sa.String(40), nullable=False),  # sensitive, never exposed
)
suppliers = _table(
    "suppliers",
    sa.Column("supplier_id", sa.Integer, primary_key=True),
    sa.Column("supplier_name", sa.String(120), nullable=False),
    sa.Column("country", sa.String(60), nullable=False),
    sa.Column("bank_account", sa.String(40), nullable=False),
    sa.Column("tax_id", sa.String(30), nullable=False),
)
purchase_orders = _table(
    "purchase_orders",
    sa.Column("po_id", sa.Integer, primary_key=True),
    sa.Column("po_number", sa.String(20), nullable=False),
    sa.Column("supplier_id", sa.Integer, sa.ForeignKey("suppliers.supplier_id"), nullable=False),
    sa.Column("warehouse_id", sa.Integer, sa.ForeignKey("warehouses.warehouse_id"), nullable=False),
    sa.Column("order_date", sa.Date, nullable=False),
    sa.Column("expected_date", sa.Date, nullable=False),
    sa.Column("received_date", sa.Date, nullable=True),
    sa.Column("status", sa.String(20), nullable=False),
    _money("total_amount"),
)

TABLE_ORDER: Final = [
    regions,
    branches,
    customers,
    products,
    warehouses,
    sales_orders,
    sales_order_lines,
    inventory_balances,
    inventory_movements,
    invoices,
    payments,
    suppliers,
    purchase_orders,
]

VIEW_NAMES: Final = (
    "analytics_sales_orders",
    "analytics_sales_lines",
    "analytics_order_status",
    "analytics_inventory",
    "analytics_inventory_movements",
    "analytics_customers",
    "analytics_invoices",
    "analytics_purchase_orders",
)


@dataclass
class SeedReport:
    dialect: str
    tenant_code: str
    anchor_date: date
    seed: int
    row_counts: dict[str, int] = field(default_factory=dict)
    views: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- data
REGION_NAMES: Final = ("North", "South", "East", "West")
SEGMENTS: Final = ("Enterprise", "Mid-Market", "SMB")
CATEGORIES: Final = ("Components", "Fasteners", "Electrical", "Packaging", "Tools", "Safety")
_ADJ: Final = (
    "Apex",
    "Blue",
    "Cedar",
    "Delta",
    "Evergreen",
    "Falcon",
    "Granite",
    "Harbor",
    "Iron",
    "Juniper",
    "Keystone",
    "Lumen",
    "Maple",
    "Nova",
    "Orion",
    "Pioneer",
)
_NOUN: Final = (
    "Industries",
    "Manufacturing",
    "Logistics",
    "Retail",
    "Foods",
    "Energy",
    "Motors",
    "Textiles",
    "Pharma",
    "Builders",
)
_COUNTRIES: Final = ("Germany", "India", "United States", "China", "Mexico", "Poland")


def _r2(value: float) -> float:
    return round(value + 0.0, 2)


def _clip(day: date, anchor: date) -> date:
    return min(day, anchor)


def _quarter_start(anchor: date) -> date:
    return date(anchor.year, 3 * ((anchor.month - 1) // 3) + 1, 1)


def generate(  # noqa: PLR0912, PLR0915 - one linear, deterministic storyline
    *, anchor_date: date, tenant_code: str, scale: int = 1, seed: int = DEFAULT_SEED
) -> dict[str, list[dict[str, Any]]]:
    """Pure, deterministic data generation (no I/O)."""
    rng = random.Random(seed)  # noqa: S311 - demo data, not cryptography
    anchor = anchor_date
    tc = tenant_code
    data: dict[str, list[dict[str, Any]]] = {t.name: [] for t in TABLE_ORDER}

    for rid, name in enumerate(REGION_NAMES, 1):
        data["regions"].append({"region_id": rid, "region_name": name, "tenant_code": tc})
    branch_by_region: dict[int, list[int]] = {}
    bid = 0
    for rid, name in enumerate(REGION_NAMES, 1):
        for suffix in ("Central", "Metro"):
            bid += 1
            data["branches"].append(
                {
                    "branch_id": bid,
                    "branch_name": f"{name} {suffix}",
                    "region_id": rid,
                    "tenant_code": tc,
                }
            )
            branch_by_region.setdefault(rid, []).append(bid)

    customer_region: dict[int, int] = {}
    for cid in range(1, 60 * scale + 1):
        rid = rng.randint(1, len(REGION_NAMES))
        customer_region[cid] = rid
        name = f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} {cid:03d}"
        data["customers"].append(
            {
                "customer_id": cid,
                "customer_name": name,
                "segment": rng.choice(SEGMENTS),
                "region_id": rid,
                "email": f"accounts{cid}@customer{cid}.example",
                "phone": f"+1-555-{rng.randint(1000, 9999)}",
                "bank_account": f"DE{rng.randint(10**17, 10**18 - 1)}",
                "tax_id": f"TX-{rng.randint(10**7, 10**8 - 1)}",
                "credit_limit": _r2(rng.choice((5000, 10000, 25000, 50000, 100000))),
                "created_date": anchor - timedelta(days=rng.randint(200, 1500)),
                "tenant_code": tc,
            }
        )

    product_cost: dict[int, float] = {}
    product_price: dict[int, float] = {}
    for pid in range(1, 41):
        if pid == 1:
            name, sku, category = "Product A", "PA-0001", "Components"
        else:
            category = CATEGORIES[pid % len(CATEGORIES)]
            name = f"{category[:-1] if category.endswith('s') else category} Model {100 + pid}"
            sku = f"{category[:3].upper()}-{1000 + pid}"
        price = _r2(rng.uniform(5, 500))
        cost = _r2(price * rng.uniform(0.55, 0.8))
        product_price[pid], product_cost[pid] = price, cost
        data["products"].append(
            {
                "product_id": pid,
                "sku": sku,
                "product_name": name,
                "category": category,
                "unit_price": price,
                "unit_cost": cost,
                "tenant_code": tc,
            }
        )

    for wid, rname in enumerate(REGION_NAMES, 1):
        data["warehouses"].append(
            {
                "warehouse_id": wid,
                "warehouse_code": f"WH-{wid:02d}",
                "warehouse_name": f"{rname} DC",
                "region_id": wid,
                "tenant_code": tc,
            }
        )

    # ---------------------------------------------------------------- sales
    order_id = line_id = 0
    delivered: list[tuple[int, int, date, float]] = []  # order, customer, date, net
    for _ in range(900 * scale):
        days_ago = rng.randint(0, HISTORY_DAYS)
        cid = rng.randint(1, 60 * scale)
        rid = customer_region[cid] if rng.random() < 0.85 else rng.randint(1, len(REGION_NAMES))
        # Storyline: West loses business over the last ~90 days.
        if REGION_NAMES[rid - 1] == "West" and days_ago < 90 and rng.random() < 0.6:
            continue
        order_id += 1
        order_date = anchor - timedelta(days=days_ago)
        if days_ago <= 3:
            status = "open"
        elif days_ago <= 10:
            status = rng.choice(("open", "processing", "shipped"))
        elif days_ago <= 30:
            status = rng.choice(("open", "processing", "shipped", "delivered", "delivered"))
        else:
            roll = rng.random()
            status = "cancelled" if roll < 0.05 else "open" if roll < 0.07 else "delivered"
        closed = (
            _clip(order_date + timedelta(days=rng.randint(2, 20)), anchor)
            if status in {"delivered", "cancelled"}
            else None
        )
        gross = discount = net = 0.0
        for _line in range(rng.randint(1, 5)):
            line_id += 1
            pid = rng.randint(1, 40)
            qty = rng.randint(1, 50)
            line_gross = _r2(qty * product_price[pid])
            line_discount = _r2(line_gross * rng.choice((0, 0, 0, 0.05, 0.1)))
            line_net = _r2(line_gross - line_discount)
            gross, discount, net = gross + line_gross, discount + line_discount, net + line_net
            data["sales_order_lines"].append(
                {
                    "line_id": line_id,
                    "order_id": order_id,
                    "product_id": pid,
                    "quantity": qty,
                    "unit_price": product_price[pid],
                    "discount_amount": line_discount,
                    "net_amount": line_net,
                    "cost_amount": _r2(qty * product_cost[pid]),
                    "tenant_code": tc,
                }
            )
        data["sales_orders"].append(
            {
                "order_id": order_id,
                "order_number": f"SO-{100000 + order_id}",
                "customer_id": cid,
                "branch_id": rng.choice(branch_by_region[rid]),
                "order_date": order_date,
                "status": status,
                "closed_date": closed,
                "gross_amount": _r2(gross),
                "discount_amount": _r2(discount),
                "net_amount": _r2(net),
                "tenant_code": tc,
            }
        )
        if status in {"shipped", "delivered"}:
            delivered.append((order_id, cid, order_date, _r2(net)))

    # ------------------------------------------------------------- finance
    payment_id = 0
    for invoice_id, (oid, cid, odate, amount) in enumerate(delivered, 1):
        inv_date = _clip(odate + timedelta(days=rng.randint(1, 5)), anchor)
        due = inv_date + timedelta(days=30)
        roll = rng.random()
        paid = 0.0
        if due < anchor:
            paid = amount if roll < 0.8 else _r2(amount * 0.5) if roll < 0.9 else 0.0
        elif roll < 0.4:
            paid = amount
        status = "paid" if paid >= amount else "partially_paid" if paid > 0 else "open"
        data["invoices"].append(
            {
                "invoice_id": invoice_id,
                "invoice_number": f"INV-{200000 + invoice_id}",
                "customer_id": cid,
                "order_id": oid,
                "invoice_date": inv_date,
                "due_date": due,
                "amount": amount,
                "status": status,
                "tenant_code": tc,
            }
        )
        if paid > 0:
            payment_id += 1
            data["payments"].append(
                {
                    "payment_id": payment_id,
                    "invoice_id": invoice_id,
                    "payment_date": _clip(due + timedelta(days=rng.randint(-10, 20)), anchor),
                    "amount": paid,
                    "payment_token": f"tok_{rng.getrandbits(64):016x}",
                    "tenant_code": tc,
                }
            )

    # ----------------------------------------------------------- inventory
    balance_id = 0
    for pid in range(1, 41):
        for wid in range(1, len(REGION_NAMES) + 1):
            balance_id += 1
            min_stock = rng.randint(20, 200)
            on_hand = rng.randint(0, min_stock - 1) if rng.random() < 0.15 else rng.randint(0, 600)
            data["inventory_balances"].append(
                {
                    "balance_id": balance_id,
                    "product_id": pid,
                    "warehouse_id": wid,
                    "on_hand_qty": on_hand,
                    "min_stock_qty": min_stock,
                    "tenant_code": tc,
                }
            )

    movement_id = 0
    quarter_days = max(0, (anchor - _quarter_start(anchor)).days)

    def movement(pid: int, days_ago: int) -> None:
        nonlocal movement_id
        movement_id += 1
        kind = rng.choice(
            ("receipt", "shipment", "shipment", "adjustment", "transfer_in", "transfer_out")
        )
        qty = rng.randint(1, 120)
        sign = (
            1
            if kind in {"receipt", "transfer_in"}
            else -1
            if kind != "adjustment"
            else rng.choice((1, -1))
        )
        data["inventory_movements"].append(
            {
                "movement_id": movement_id,
                "product_id": pid,
                "warehouse_id": rng.randint(1, len(REGION_NAMES)),
                "movement_date": anchor - timedelta(days=days_ago),
                "movement_type": kind,
                "quantity": sign * qty,
                "reference": f"MV-{300000 + movement_id}",
                "tenant_code": tc,
            }
        )

    for _ in range(1500 * scale):
        movement(rng.randint(1, 40), rng.randint(0, HISTORY_DAYS))
    for _ in range(20):  # Storyline: Product A moves during the current quarter.
        movement(1, rng.randint(0, quarter_days))

    # ----------------------------------------------------------- purchasing
    for sid in range(1, 16):
        data["suppliers"].append(
            {
                "supplier_id": sid,
                "supplier_name": f"{rng.choice(_ADJ)} Supply {sid:02d}",
                "country": rng.choice(_COUNTRIES),
                "bank_account": f"GB{rng.randint(10**17, 10**18 - 1)}",
                "tax_id": f"VAT-{rng.randint(10**7, 10**8 - 1)}",
                "tenant_code": tc,
            }
        )
    for po in range(1, 200 * scale + 1):
        odate = anchor - timedelta(days=rng.randint(0, HISTORY_DAYS))
        expected = odate + timedelta(days=rng.randint(7, 30))
        received: date | None = None
        if expected < anchor and rng.random() < 0.85:
            slip = rng.randint(-3, 0) if rng.random() < 0.7 else rng.randint(1, 10)
            received = _clip(expected + timedelta(days=slip), anchor)
        data["purchase_orders"].append(
            {
                "po_id": po,
                "po_number": f"PO-{400000 + po}",
                "supplier_id": rng.randint(1, 15),
                "warehouse_id": rng.randint(1, len(REGION_NAMES)),
                "order_date": odate,
                "expected_date": expected,
                "received_date": received,
                "status": "received" if received else "open",
                "total_amount": _r2(rng.uniform(500, 50000)),
                "tenant_code": tc,
            }
        )
    return data


# -------------------------------------------------------------------- views
def view_definitions(dialect: str, anchor: date) -> list[tuple[str, str]]:
    """``(view_name, SELECT ...)`` pairs. Ages are computed against ``anchor``."""
    pg = dialect == "postgresql"
    raw = "erp." if pg else ""
    iso = anchor.isoformat()  # generated from a date object: safe to inline
    anchor_lit = {
        "postgresql": f"DATE '{iso}'",
        "mysql": f"DATE '{iso}'",
        "mssql": f"CAST('{iso}' AS DATE)",
    }.get(dialect, f"'{iso}'")

    def days(later: str, earlier: str) -> str:
        if pg:
            return f"({later} - {earlier})"
        if dialect == "mysql":
            return f"DATEDIFF({later}, {earlier})"
        if dialect == "mssql":
            return f"DATEDIFF(day, {earlier}, {later})"
        return f"CAST(julianday({later}) - julianday({earlier}) AS INTEGER)"

    outstanding = "(i.amount - COALESCE(p.paid_amount, 0))"
    overdue = f"({outstanding} > 0.005 AND i.due_date < {anchor_lit})"
    return [
        (
            "analytics_sales_orders",
            f"""SELECT o.order_id, o.order_number, o.tenant_code, o.order_date, o.status,
    r.region_name, b.branch_name, c.customer_id, c.customer_name,
    o.gross_amount, o.discount_amount, o.net_amount,
    CASE WHEN o.closed_date IS NULL THEN {days(anchor_lit, "o.order_date")}
         ELSE {days("o.closed_date", "o.order_date")} END AS days_open
FROM {raw}sales_orders o
JOIN {raw}branches b ON b.branch_id = o.branch_id AND b.tenant_code = o.tenant_code
JOIN {raw}regions r ON r.region_id = b.region_id AND r.tenant_code = o.tenant_code
JOIN {raw}customers c ON c.customer_id = o.customer_id AND c.tenant_code = o.tenant_code""",
        ),
        (
            "analytics_sales_lines",
            f"""SELECT l.line_id, l.order_id, o.order_number, l.tenant_code, o.order_date, o.status,
    r.region_name, b.branch_name, c.customer_name,
    p.product_id, p.product_name, p.sku, p.category,
    l.quantity, l.unit_price, l.net_amount, l.cost_amount,
    (l.net_amount - l.cost_amount) AS gross_profit
FROM {raw}sales_order_lines l
JOIN {raw}sales_orders o ON o.order_id = l.order_id AND o.tenant_code = l.tenant_code
JOIN {raw}branches b ON b.branch_id = o.branch_id AND b.tenant_code = o.tenant_code
JOIN {raw}regions r ON r.region_id = b.region_id AND r.tenant_code = o.tenant_code
JOIN {raw}customers c ON c.customer_id = o.customer_id AND c.tenant_code = o.tenant_code
JOIN {raw}products p ON p.product_id = l.product_id AND p.tenant_code = l.tenant_code""",
        ),
        (
            "analytics_order_status",
            f"""SELECT o.tenant_code, o.order_date, o.status,
    COUNT(*) AS order_count, SUM(o.net_amount) AS net_amount
FROM {raw}sales_orders o
GROUP BY o.tenant_code, o.order_date, o.status""",
        ),
        (
            "analytics_inventory",
            f"""SELECT ib.balance_id, ib.tenant_code, p.product_id, p.product_name, p.sku,
    p.category,
    w.warehouse_code, w.warehouse_name, r.region_name,
    ib.on_hand_qty, ib.min_stock_qty,
    CASE WHEN ib.on_hand_qty < ib.min_stock_qty THEN 1 ELSE 0 END AS below_min,
    ROUND(ib.on_hand_qty * p.unit_cost, 2) AS stock_value
FROM {raw}inventory_balances ib
JOIN {raw}products p ON p.product_id = ib.product_id AND p.tenant_code = ib.tenant_code
JOIN {raw}warehouses w ON w.warehouse_id = ib.warehouse_id AND w.tenant_code = ib.tenant_code
JOIN {raw}regions r ON r.region_id = w.region_id AND r.tenant_code = ib.tenant_code""",
        ),
        (
            "analytics_inventory_movements",
            f"""SELECT m.movement_id, m.tenant_code, m.movement_date, m.movement_type,
    p.product_name, p.sku, w.warehouse_name, m.quantity,
    CASE WHEN m.quantity > 0 THEN m.quantity ELSE 0 END AS inbound_qty,
    CASE WHEN m.quantity < 0 THEN -m.quantity ELSE 0 END AS outbound_qty,
    m.reference
FROM {raw}inventory_movements m
JOIN {raw}products p ON p.product_id = m.product_id AND p.tenant_code = m.tenant_code
JOIN {raw}warehouses w ON w.warehouse_id = m.warehouse_id AND w.tenant_code = m.tenant_code""",
        ),
        (
            "analytics_customers",
            f"""SELECT c.customer_id, c.tenant_code, c.customer_name, c.segment, r.region_name,
    c.credit_limit, c.created_date
FROM {raw}customers c
JOIN {raw}regions r ON r.region_id = c.region_id AND r.tenant_code = c.tenant_code""",
        ),
        (
            "analytics_invoices",
            f"""SELECT i.invoice_id, i.invoice_number, i.tenant_code, i.order_id,
    c.customer_id, c.customer_name, r.region_name,
    i.invoice_date, i.due_date, i.amount,
    COALESCE(p.paid_amount, 0) AS paid_amount,
    {outstanding} AS outstanding_amount,
    CASE WHEN {overdue} THEN {days(anchor_lit, "i.due_date")} ELSE 0 END AS days_overdue,
    CASE WHEN {overdue} THEN 1 ELSE 0 END AS is_overdue,
    CASE WHEN {overdue} THEN {outstanding} ELSE 0 END AS overdue_amount,
    i.status
FROM {raw}invoices i
JOIN {raw}customers c ON c.customer_id = i.customer_id AND c.tenant_code = i.tenant_code
JOIN {raw}regions r ON r.region_id = c.region_id AND r.tenant_code = i.tenant_code
LEFT JOIN (
    SELECT invoice_id, tenant_code, SUM(amount) AS paid_amount
    FROM {raw}payments GROUP BY invoice_id, tenant_code
) p ON p.invoice_id = i.invoice_id AND p.tenant_code = i.tenant_code""",
        ),
        (
            "analytics_purchase_orders",
            f"""SELECT po.po_id, po.po_number, po.tenant_code, s.supplier_name,
    s.country AS supplier_country, w.warehouse_name,
    po.order_date, po.expected_date, po.received_date, po.status, po.total_amount,
    CASE WHEN (po.received_date IS NOT NULL AND po.received_date > po.expected_date)
           OR (po.received_date IS NULL AND po.expected_date < {anchor_lit})
         THEN 1 ELSE 0 END AS is_late,
    {days("po.received_date", "po.order_date")} AS lead_time_days
FROM {raw}purchase_orders po
JOIN {raw}suppliers s ON s.supplier_id = po.supplier_id AND s.tenant_code = po.tenant_code
JOIN {raw}warehouses w ON w.warehouse_id = po.warehouse_id AND w.tenant_code = po.tenant_code""",
        ),
    ]


# -------------------------------------------------------------------- write
def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _to_decimal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # asyncpg expects Decimal for NUMERIC parameters.
    return [
        {k: Decimal(str(v)) if isinstance(v, float) else v for k, v in row.items()} for row in rows
    ]


async def _exists(conn: AsyncConnection, schema: str | None) -> bool:
    return await conn.run_sync(lambda c: sa.inspect(c).has_table("sales_orders", schema=schema))


async def _reset(conn: AsyncConnection, pg: bool) -> None:
    if pg:
        await conn.execute(sa.text("DROP SCHEMA IF EXISTS analytics CASCADE"))
        await conn.execute(sa.text("DROP SCHEMA IF EXISTS erp CASCADE"))
        return
    for name in VIEW_NAMES:
        await conn.execute(sa.text(f"DROP VIEW IF EXISTS {name}"))
    await conn.run_sync(metadata.drop_all)


async def seed_demo(
    url: str,
    *,
    anchor_date: date,
    tenant_code: str = "demo",
    scale: int = 1,
    reset: bool = False,
    seed: int = DEFAULT_SEED,
) -> SeedReport:
    """Create raw tables + analytics views and load deterministic demo data."""
    if not _TENANT_CODE.fullmatch(tenant_code):
        raise ValueError("tenant_code must be a tenant slug (lowercase letters, digits, '-')")
    if not 1 <= scale <= 100:
        raise ValueError("scale must be between 1 and 100")
    dialect = make_url(url).get_backend_name()
    if dialect not in {"postgresql", "sqlite", "mysql", "mssql"}:
        raise ValueError(f"unsupported database: {dialect}")
    pg = dialect == "postgresql"

    data = generate(anchor_date=anchor_date, tenant_code=tenant_code, scale=scale, seed=seed)
    report = SeedReport(
        dialect=dialect, tenant_code=tenant_code, anchor_date=anchor_date, seed=seed
    )
    engine = create_async_engine(url)
    try:
        async with engine.begin() as raw_conn:
            if reset:
                await _reset(raw_conn, pg)
            if pg:
                await raw_conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS erp"))
                await raw_conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS analytics"))
            if await _exists(raw_conn, "erp" if pg else None):
                raise RuntimeError(
                    "demo data already present; reset it (reset=True / --reset) to recreate"
                )
            conn = (
                await raw_conn.execution_options(schema_translate_map={None: "erp"})
                if pg
                else raw_conn
            )
            await conn.run_sync(metadata.create_all)
            for table in TABLE_ORDER:
                rows = data[table.name]
                for batch in _batches(rows, BATCH_SIZE):
                    await conn.execute(
                        table.insert(), _to_decimal(batch) if dialect != "sqlite" else batch
                    )
                report.row_counts[table.name] = len(rows)
            for name, select_sql in view_definitions(dialect, anchor_date):
                target = f"analytics.{name}" if pg else name
                await raw_conn.execute(sa.text(f"CREATE VIEW {target} AS {select_sql}"))
                report.views.append(name)
            if pg:
                has_role = await raw_conn.scalar(
                    sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'erp_readonly'")
                )
                if has_role:
                    await raw_conn.execute(
                        sa.text("GRANT USAGE ON SCHEMA analytics TO erp_readonly")
                    )
                    await raw_conn.execute(
                        sa.text("GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO erp_readonly")
                    )
    finally:
        await engine.dispose()
    return report


def clone_tenant_sqlite(db_path: str, tenant_code: str, id_offset: int = 1_000_000) -> None:
    """Copy every demo row to another tenant inside the same SQLite file (ids offset).

    Tests and the evaluation sandbox use it to put a second tenant's rows next to the
    first one, so tenant isolation is exercised on shared tables. Table and column
    names come from the static demo ``metadata``; the tenant code is a bound parameter.
    """
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        for table in metadata.sorted_tables:
            cols = [c.name for c in table.columns]
            select = [
                f"{c} + {int(id_offset)}" if c.endswith("_id") else "?" if c == "tenant_code" else c
                for c in cols
            ]
            conn.execute(
                f"INSERT INTO {table.name} ({', '.join(cols)}) "
                f"SELECT {', '.join(select)} FROM {table.name}",
                (tenant_code,),
            )
