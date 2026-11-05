#!/usr/bin/env python3
"""Estimate Azure task runtime costs using recent subscription billing rates."""

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import subprocess
import sys
from uuid import UUID


PRICE_FIELDS = ["azure_spot_usd_per_hour", "azure_runtime_estimate_usd",
                "azure_rate_date", "azure_price_note"]
COST_FIELDS = ["compute_cost_usd", "total_known_cost_usd", "total_cost_status"]


def number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f"Expected a finite nonnegative number: {value}")
    return result


def cost_columns(row):
    result = dict.fromkeys(COST_FIELDS, "")
    provider = row.get("cloud_provider")
    cost = row.get(f"{provider}_runtime_estimate_usd", "")
    if cost:
        result.update(compute_cost_usd=cost, total_known_cost_usd=cost,
                      total_cost_status="Partial: storage is not yet included")
    else:
        result["total_cost_status"] = "Not determined"
    return result


def billing_region(location):
    words = location.lower().split()
    if words and words[0] == "us":
        words = (words[1:-1] + ["us", words[-1]] if words[-1].isdigit()
                 else words[1:] + ["us"])
    # shortcut: US/UK billing names and ARM names, add other billing aliases when used.
    return "".join(words)


def billing_rates(payload):
    properties = payload["properties"]
    columns = [column["name"] for column in properties["columns"]]
    totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for values in properties["rows"]:
        row = dict(zip(columns, values, strict=True))
        if row["Currency"] != "USD":
            raise ValueError("Billing currency must be USD; no currency conversion is applied")
        if not row["Meter"].endswith(" Spot"):
            continue
        usage = number(row["UsageQuantity"])
        if not usage:
            continue
        day = datetime.strptime(str(row["UsageDate"]), "%Y%m%d").date()
        key = (row["Meter"], billing_region(row["ResourceLocation"]), day)
        totals[key][0] += number(row["PreTaxCost"])
        totals[key][1] += usage
    return {key: cost / usage for key, (cost, usage) in totals.items()}


def fetch_billing(rows, subscription):
    subscription = str(UUID(subscription))
    days = [datetime.fromisoformat(r["started"]).date() for r in rows if r.get("started")]
    if not days:
        return {"properties": {"columns": [], "rows": []}}
    groups = sorted({"rg-tc-" + r["worker_pool"].replace("/", "-") for r in rows})
    body = {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {"from": f"{min(days) - timedelta(days=7)}T00:00:00Z",
                       "to": f"{max(days)}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"cost": {"name": "PreTaxCost", "function": "Sum"},
                            "hours": {"name": "UsageQuantity", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "Meter"},
                         {"type": "Dimension", "name": "ResourceLocation"}],
            "filter": {"and": [
                {"dimensions": {"name": "ResourceGroupName", "operator": "In", "values": groups}},
                {"dimensions": {"name": "MeterCategory", "operator": "In", "values": ["Virtual Machines"]}}
            ]}
        }
    }
    url = (f"https://management.azure.com/subscriptions/{subscription}"
           "/providers/Microsoft.CostManagement/query?api-version=2025-03-01")
    combined = None
    while url:
        response = subprocess.run(
            ["az", "rest", "--method", "post", "--url", url,
             "--body", json.dumps(body), "--output", "json"],
            capture_output=True, text=True, check=True, timeout=180)
        page = json.loads(response.stdout)
        if combined is None:
            combined = page
        else:
            if page["properties"]["columns"] != combined["properties"]["columns"]:
                raise ValueError("Billing columns changed between pages")
            combined["properties"]["rows"].extend(page["properties"]["rows"])
        url = page["properties"].get("nextLink")
        if url and not url.startswith("https://management.azure.com/"):
            raise ValueError("Unexpected Azure billing pagination URL")
    return combined


def price_rows(rows, rates):
    priced = []
    for original in rows:
        row = dict(original)
        row.update(dict.fromkeys(PRICE_FIELDS, ""))
        if row["cloud_provider"] != "azure":
            row["azure_price_note"] = "Not priced: not identified as Azure"
        elif not row.get("duration_seconds") or not row.get("started"):
            row["azure_price_note"] = "Not priced: final duration unavailable"
        elif not row.get("vm_size") or not row.get("region"):
            row["azure_price_note"] = "Not priced: VM size or region unavailable"
        else:
            sku = row["vm_size"]
            if not sku.startswith("Standard_"):
                raise ValueError(f"Unsupported Azure VM size: {sku}")
            meter = sku.removeprefix("Standard_").replace("_", " ") + " Spot"
            day = datetime.fromisoformat(row["started"]).date()
            matches = [(rate_day, rate) for (name, region, rate_day), rate in rates.items()
                       if name == meter and region == row["region"].lower()
                       and 0 <= (day - rate_day).days <= 7]
            if matches:
                rate_day, rate = max(matches)
                row["azure_spot_usd_per_hour"] = format(rate, ".8f")
                row["azure_runtime_estimate_usd"] = format(
                    number(row["duration_seconds"]) / Decimal(3600) * rate, ".8f")
                row["azure_rate_date"] = str(rate_day)
                row["azure_price_note"] = "Spot assumed; recent daily blended subscription VM rate; runtime only"
            else:
                row["azure_price_note"] = "Not priced: no matching Spot billing rate in preceding 7 days"
        row.update(cost_columns(row))
        priced.append(row)
    return priced


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", help="CSV exported by inspect_try.py")
    parser.add_argument("--subscription", required=True, help="Azure subscription UUID; requires az login")
    args = parser.parse_args()
    try:
        with open(args.tasks, newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            required = {"cloud_provider", "vm_size", "region", "worker_pool", "started", "duration_seconds"}
            if not fields or not required.issubset(fields):
                raise ValueError("Input must be a task inspection CSV")
            rows = list(reader)
        azure = [row for row in rows if row["cloud_provider"] == "azure"]
        rates = billing_rates(fetch_billing(azure, args.subscription)) if azure else {}
        result = price_rows(rows, rates)
    except (OSError, ValueError, KeyError, InvalidOperation, subprocess.SubprocessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        parser.exit(1, f"Azure pricing failed: {detail}\n")
    writer = csv.DictWriter(sys.stdout, fieldnames=fields + [f for f in PRICE_FIELDS + COST_FIELDS if f not in fields])
    writer.writeheader()
    writer.writerows(result)
    costs = [number(row["azure_runtime_estimate_usd"]) for row in result if row["azure_runtime_estimate_usd"]]
    print(f"Azure runtime estimate: ${sum(costs, Decimal(0)):.4f} USD; "
          f"priced {len(costs)}/{len(azure)} Azure task runs", file=sys.stderr)
    failed_tests = [row for row in result if row.get("failure_category") == "Failed test"]
    if failed_tests:
        failed_costs = [number(row["azure_runtime_estimate_usd"]) for row in failed_tests
                        if row["azure_runtime_estimate_usd"]]
        subtotal = f"${sum(failed_costs, Decimal(0)):.6f} USD" if failed_costs else "Not determined"
        print(f"Failed tests: {len(failed_tests)} task runs; Azure runtime estimate {subtotal}; "
              f"priced {len(failed_costs)}/{len(failed_tests)}; included in totals above", file=sys.stderr)


if __name__ == "__main__":
    main()
