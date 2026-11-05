#!/usr/bin/env python3
"""Estimate GCP task runtime costs from historical public Spot SKU prices."""

import argparse
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import subprocess
import sys
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from price_azure import number, cost_columns, COST_FIELDS


CATALOG_URL = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"
FIELDS = ["gcp_spot_usd_per_hour", "gcp_runtime_estimate_usd",
          "gcp_rate_source",
          "gcp_rate_components", "gcp_price_note"]
# shortcut: documented sizes used by this push, add verified specs for other sizes.
# Memory quantities use the GiB values in Google's Spot pricing tables.
SPECS = {
    "c2-standard-16": (16, 64, "c2"),
    "c3-standard-8": (8, 32, "c3"),
    "c3d-standard-8": (8, 32, "c3d"),
    "c3d-standard-8-lssd": (8, 32, "c3d"),
    "c3d-standard-16-lssd": (16, 64, "c3d"),
    "c3d-standard-60-lssd": (60, 240, "c3d"),
    "c4d-standard-8-lssd": (8, 31, "c4d"),
}
SKUS = {
    "c2": ("93E3-1A16-2F5C", "ECAE-177C-FE40"),
    "c3": ("4F6C-A177-846C", "EBA2-EC70-D742"),
    "c3d": ("DCB1-2557-F8CB", "F33C-AA89-DE6D"),
    "c4d": ("98F9-9C75-24C5", "06A4-288A-B3FF"),
}


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result


def fetch_catalog(rows):
    start = min(timestamp(row["started"]) for row in rows)
    end = max(timestamp(row["resolved"]) for row in rows)
    zone = ZoneInfo("America/Los_Angeles")
    if start.astimezone(zone).strftime("%Y-%m") != end.astimezone(zone).strftime("%Y-%m"):
        raise ValueError("Catalog request must stay within one Los Angeles calendar month; split the input by month")
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True,
                           text=True, check=True, timeout=60).stdout.strip()
    if not token:
        raise ValueError("gcloud returned no access token")
    needed = {sku for triple in SKUS.values() for sku in triple if sku}
    result = {"requested_start": start.isoformat(), "requested_end": end.isoformat(), "skus": []}
    params = {"currencyCode": "USD", "pageSize": 5000,
              "startTime": start.isoformat(), "endTime": end.isoformat()}
    while True:
        request = Request(CATALOG_URL + "?" + urlencode(params),
                          headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=60) as response:
            page = json.load(response)
        result["skus"].extend(sku for sku in page.get("skus", []) if sku["skuId"] in needed)
        if not page.get("nextPageToken"):
            return result
        params["pageToken"] = page["nextPageToken"]


def hourly_price(info, component, quantity):
    expression = info["pricingExpression"]
    tiers = expression["tieredRates"]
    if len(tiers) != 1 or number(tiers[0]["startUsageAmount"]) != 0:
        raise ValueError("Tiered pricing is unsupported for these VM components")
    money = tiers[0]["unitPrice"]
    if money["currencyCode"] != "USD":
        raise ValueError("GCP catalog currency must be USD")
    unit_price = number(Decimal(money.get("units", "0")) +
                        Decimal(money.get("nanos", 0)) / Decimal(10**9))
    expected_base = "s" if component == "cpu" else "By.s"
    if expression["baseUnit"] != expected_base:
        raise ValueError(f"Unexpected {component} billing unit: {expression['baseUnit']}")
    factor = number(expression["baseUnitConversionFactor"])
    if not factor:
        raise ValueError("Price unit conversion factor must be positive")
    base_quantity = Decimal(quantity) if component == "cpu" else Decimal(quantity) * 2**30
    return base_quantity * Decimal(3600) / factor * unit_price


def estimate(row, catalog):
    spec = SPECS.get(row["vm_size"])
    if not spec:
        raise ValueError("Unsupported VM size; verified machine specifications required")
    start, end = timestamp(row["started"]), timestamp(row["resolved"])
    duration = number(row["duration_seconds"])
    if end < start or abs(Decimal(str((end - start).total_seconds())) - duration) > Decimal("0.001"):
        raise ValueError("Duration does not match the execution timestamps")
    if start < timestamp(catalog["requested_start"]) or end > timestamp(catalog["requested_end"]):
        raise ValueError("Task run is outside the catalog's requested historical window")
    by_id = {sku["skuId"]: sku for sku in catalog["skus"]}
    cpu, memory, family = spec
    total = Decimal(0)
    evidence = []
    for component, quantity, sku_id in zip(("cpu", "ram"), (cpu, memory), SKUS[family]):
        if not quantity:
            continue
        sku = by_id.get(sku_id)
        if not sku or row["region"] not in sku["serviceRegions"]:
            raise ValueError(f"No matching regional {component} Spot SKU")
        if sku.get("category", {}).get("usageType") != "Preemptible":
            raise ValueError("Expected a Spot/Preemptible SKU")
        timeline = sorted(((timestamp(info["effectiveTime"]), info) for info in sku["pricingInfo"]),
                          key=lambda pair: pair[0])
        if not timeline or timeline[0][0] > start:
            raise ValueError(f"No {component} price effective at execution start")
        boundaries = sorted({start, end} | {time for time, _ in timeline if start < time < end})
        for left, right in zip(boundaries, boundaries[1:]):
            effective, info = max(((time, info) for time, info in timeline if time <= left),
                                  key=lambda pair: pair[0])
            rate = hourly_price(info, component, quantity)
            seconds = Decimal(str((right - left).total_seconds()))
            component_cost = rate * seconds / 3600
            total += component_cost
            evidence.append({"component": component, "sku_id": sku_id,
                             "effective_at": effective.isoformat(),
                             "usd_per_hour": format(rate, ".10f"),
                             "runtime_seconds": str(seconds), "estimated_usd": str(component_cost)})
    rate = total * 3600 / duration if duration else Decimal(0)
    return rate, total, evidence


def price_rows(rows, catalog):
    result = []
    for original in rows:
        row = dict(original)
        row.update(dict.fromkeys(FIELDS, ""))
        if row["cloud_provider"] != "gcp":
            row["gcp_price_note"] = "Not priced: not identified as GCP"
        elif not row.get("started") or not row.get("resolved") or row.get("duration_seconds") in (None, ""):
            row["gcp_price_note"] = "Not priced: final execution duration unavailable"
        else:
            try:
                rate, cost, evidence = estimate(row, catalog)
            except ValueError as exc:
                row["gcp_price_note"] = "Not priced: " + str(exc)
            else:
                row.update(gcp_spot_usd_per_hour=format(rate, ".8f"),
                           gcp_runtime_estimate_usd=format(cost, ".8f"),
                           gcp_rate_source=CATALOG_URL,
                           gcp_rate_components=json.dumps(evidence, separators=(",", ":")),
                           gcp_price_note="Spot assumed; historical public list prices; CPU/RAM only; storage is not yet included; runtime only; no negotiated discounts/credits")
        row.update(cost_columns(row))
        result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", help="Inspection CSV; existing Azure estimates are preserved")
    parser.add_argument("--catalog", help="Saved historical catalog JSON; otherwise query using gcloud auth")
    args = parser.parse_args()
    try:
        with open(args.tasks, newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            required = {"cloud_provider", "vm_size", "region", "started", "resolved", "duration_seconds"}
            if not fields or not required.issubset(fields):
                raise ValueError("Input must be a task inspection CSV")
            rows = list(reader)
        eligible = [row for row in rows if row["cloud_provider"] == "gcp"
                    and row.get("started") and row.get("resolved") and row["vm_size"] in SPECS]
        if args.catalog:
            with open(args.catalog) as stream:
                catalog = json.load(stream)
        else:
            catalog = fetch_catalog(eligible) if eligible else {}
        result = price_rows(rows, catalog)
    except (OSError, ValueError, KeyError, InvalidOperation, URLError, subprocess.SubprocessError) as exc:
        # gcloud errors may contain local configuration details, but never print its token stdout.
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        parser.exit(1, f"GCP pricing failed: {detail}\n")
    writer = csv.DictWriter(sys.stdout, fieldnames=fields + [field for field in FIELDS + COST_FIELDS if field not in fields])
    writer.writeheader()
    writer.writerows(result)
    costs = [number(row["gcp_runtime_estimate_usd"]) for row in result if row["gcp_runtime_estimate_usd"]]
    subtotal = f"${sum(costs, Decimal(0)):.6f} USD" if costs else "Not determined"
    print(f"GCP public Spot runtime estimate: {subtotal}; priced {len(costs)}/"
          f"{sum(row['cloud_provider'] == 'gcp' for row in rows)} GCP task runs", file=sys.stderr)


if __name__ == "__main__":
    main()
