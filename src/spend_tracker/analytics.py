"""Deterministic views for the dashboard and transaction screens."""

import calendar
from collections import defaultdict
from datetime import datetime

from spend_tracker.domain import Category
from spend_tracker.services import money


def available_months(state: dict) -> list[str]:
    return sorted({item["date"][:7] for item in state["transactions"]}, reverse=True)


def transactions_for_month(state: dict, month: str | None = None) -> list[dict]:
    rows = state["transactions"]
    if month:
        rows = [item for item in rows if item["date"].startswith(month + "-")]
    return sorted(rows, key=lambda item: (item["date"], item["created_at"]), reverse=True)


def spending_over_time(state: dict) -> dict:
    """Monthly spending by category across the full imported date range."""
    rows = state["transactions"]
    categories = [category.value for category in Category]
    if not rows:
        return {
            "categories": categories,
            "months": [],
            "total_spending_cents": 0,
            "transaction_count": 0,
            "months_tracked": 0,
        }

    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in rows:
        if not item.get("excluded_from_spending", False):
            totals[item["date"][:7]][item["category"]] += item["amount_cents"]

    first = min(item["date"][:7] for item in rows)
    last = max(item["date"][:7] for item in rows)
    year, month = (int(part) for part in first.split("-"))
    months = []
    while f"{year:04d}-{month:02d}" <= last:
        key = f"{year:04d}-{month:02d}"
        by_category = {category: totals[key][category] for category in categories}
        months.append(
            {
                "month": key,
                "total_cents": sum(by_category.values()),
                "categories": by_category,
            }
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    return {
        "categories": categories,
        "months": months,
        "total_spending_cents": sum(item["total_cents"] for item in months),
        "transaction_count": len(rows),
        "months_tracked": len({item["date"][:7] for item in rows}),
    }


def dashboard(state: dict, month: str) -> dict:
    year, number = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, number)[1]
    abbreviation = datetime(year, number, 1).strftime("%b")
    previous_month = f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"
    rows = transactions_for_month(state, month)
    previous_rows = transactions_for_month(state, previous_month)
    expenses = [item for item in rows if not item.get("excluded_from_spending", False)]
    category_cents: dict[str, int] = defaultdict(int)
    merchant_cents: dict[str, int] = defaultdict(int)
    week_cents: dict[int, int] = defaultdict(int)
    for item in expenses:
        cents = item["amount_cents"]
        category_cents[item["category"]] += cents
        merchant_cents[item["normalized_merchant"]] += cents
        week_cents[(int(item["date"][-2:]) - 1) // 7 + 1] += cents
    total = sum(category_cents.values())
    categories = [
        {"category": category, "cents": cents, "amount": money(cents)}
        for category, cents in sorted(category_cents.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    previous_category_cents: dict[str, int] = defaultdict(int)
    for item in previous_rows:
        if not item.get("excluded_from_spending", False):
            previous_category_cents[item["category"]] += item["amount_cents"]
    previous_total = sum(previous_category_cents.values())
    comparison_categories = [
        {
            "category": category,
            "current_cents": category_cents[category],
            "previous_cents": previous_category_cents[category],
            "delta_cents": category_cents[category] - previous_category_cents[category],
        }
        for category in sorted(
            category_cents.keys() | previous_category_cents.keys(),
            key=lambda category: (
                -abs(category_cents[category] - previous_category_cents[category]),
                category,
            ),
        )
    ]
    merchants = [
        {
            "merchant": merchant,
            "name": state["merchant_mappings"]
            .get(merchant, {})
            .get("display_name", merchant.title()),
            "cents": cents,
            "amount": money(cents),
        }
        for merchant, cents in sorted(merchant_cents.items(), key=lambda pair: (-pair[1], pair[0]))[
            :5
        ]
    ]
    return {
        "month": month,
        "currency": "USD",
        "total_spending_cents": total,
        "total_spending": money(total),
        "transaction_count": len(rows),
        "review_count": sum(bool(item["needs_review"]) for item in rows),
        "merchant_count": len({item["normalized_merchant"] for item in rows}),
        "categories": categories,
        "comparison": {
            "previous_month": previous_month,
            "has_previous_data": bool(previous_rows),
            "current_total_cents": total,
            "previous_total_cents": previous_total,
            "delta_cents": total - previous_total,
            "categories": comparison_categories,
        },
        "weeks": [
            {
                "label": f"{abbreviation} {1 + 7 * (week - 1)}–{min(7 * week, days)}",
                "cents": week_cents[week],
            }
            for week in range(1, (days - 1) // 7 + 2)
        ],
        "top_merchants": merchants,
        "needs_review": [item for item in rows if item["needs_review"]][:5],
        "available_months": available_months(state),
    }
