#!/usr/bin/env python3
"""Estimate Azure runtime costs from 24-hour cached subscription rates or public Spot fallback."""

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import hashlib
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import subprocess
import sys
from uuid import UUID


PRICE_FIELDS = ["azure_spot_usd_per_hour", "azure_runtime_estimate_usd",
                "azure_rate_date", "azure_price_note", "azure_rate_source",
                "azure_rate_retrieved_at", "azure_rate_cache_status"]
COST_FIELDS = ["compute_cost_usd", "total_known_cost_usd", "total_cost_status"]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the ARM bearer token through a redirect.


urlopen = build_opener(NoRedirect()).open


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Expected a finite nonnegative number: {value}") from exc
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


def billing_query(rows):
    days = [datetime.fromisoformat(r["started"]).date() for r in rows if r.get("started")]
    if not days:
        return None
    groups = sorted({"rg-tc-" + r["worker_pool"].replace("/", "-") for r in rows})
    return {
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


def request_json(url, body=None, token=None):
    expected_host = "management.azure.com" if token else "prices.azure.com"
    if urlsplit(url).scheme != "https" or urlsplit(url).netloc not in (expected_host, expected_host + ":443"):
        raise ValueError("Unexpected Azure pricing URL")
    headers = {"User-Agent": "try-cost-inspector/0.1", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    waited = 0
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 2:
                raise
            delays = []
            for name, value in (exc.headers or {}).items():
                if name.lower() == "retry-after" or name.lower().startswith("x-ms-ratelimit-") and name.lower().endswith("retry-after"):
                    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value.strip()):
                        delays.append(float(value))
                    elif name.lower() == "retry-after":
                        try:
                            delays.append(max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
                        except (ValueError, TypeError):
                            pass
            delay = max(delays + [5 * 2**attempt]) + 1
            if waited + delay > 240:
                raise  # Do not retry earlier than the server allows or wait without a bound.
            print(f"Azure pricing HTTP {exc.code}; retrying in {delay:g} seconds", file=sys.stderr)
            exc.close()
            time.sleep(delay)
            waited += delay


def fetch_billing(rows, subscription):
    subscription = str(UUID(subscription))
    body = billing_query(rows)
    if body is None:
        return {"properties": {"columns": [], "rows": []}}
    token = subprocess.run(["az", "account", "get-access-token", "--resource", "https://management.azure.com/",
                            "--query", "accessToken", "--output", "tsv"],
                           capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    if not token:
        raise ValueError("Azure CLI returned no access token")
    url = (f"https://management.azure.com/subscriptions/{subscription}"
           "/providers/Microsoft.CostManagement/query?api-version=2025-03-01")
    combined = None
    while url:
        page = request_json(url, body, token)
        if combined is None:
            combined = page
        else:
            if page["properties"]["columns"] != combined["properties"]["columns"]:
                raise ValueError("Billing columns changed between pages")
            combined["properties"]["rows"].extend(page["properties"]["rows"])
        url = page["properties"].get("nextLink")
    return combined


def cached_price(key, fetch=None, cache_dir=None):
    directory = Path(cache_dir or os.environ.get("AZURE_PRICE_CACHE_DIR", ".azure-price-cache"))
    path = directory / (hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest() + ".json")
    try:
        entry = json.loads(path.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["retrieved_at"])).total_seconds()
        if entry["key"] == key and 0 <= age < 86400:
            return entry["data"], entry["retrieved_at"], "hit"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if fetch is None:
        return None
    data = fetch()
    retrieved = datetime.now(timezone.utc).isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"key": key, "retrieved_at": retrieved, "data": data}))
    temporary.replace(path)
    return data, retrieved, "miss"


def billing_cache_key(rows, subscription):
    return {"version": 1, "source": "subscription", "subscription": str(UUID(subscription)), "query": billing_query(rows)}


def cached_billing_rates(rows, subscription, cache_dir=None):
    key = billing_cache_key(rows, subscription)
    def fetch():
        rates = billing_rates(fetch_billing(rows, subscription))
        if not rates:
            raise ValueError("No subscription Spot rates available")
        return [{"meter": meter, "region": region, "date": str(day), "rate": str(rate)}
                for (meter, region, day), rate in rates.items()]
    data, retrieved, status = cached_price(key, fetch, cache_dir)
    rates = {(r["meter"], r["region"], datetime.fromisoformat(r["date"]).date()): number(r["rate"]) for r in data}
    return rates, retrieved, status


def retail_cache_key(size, region, price_type):
    return {"version": 1, "source": "retail", "size": size, "region": region, "os": "Windows", "price_type": price_type}


def retail_rate(size, region, price_type, cache_dir=None):
    if not re.fullmatch(r"Standard_[A-Za-z0-9_]+", size) or not re.fullmatch(r"[a-z0-9]+", region):
        raise ValueError("Invalid retail VM size or region")
    key = retail_cache_key(size, region, price_type)
    def fetch():
        query = (f"serviceName eq 'Virtual Machines' and armSkuName eq '{size}' and "
                 f"armRegionName eq '{region}' and contains(meterName, 'Spot')")
        url = "https://prices.azure.com/api/retail/prices?" + urlencode({"api-version": "2023-01-01-preview", "$filter": query})
        matches = []
        while url:
            page = request_json(url)
            for item in page["Items"]:
                if (item.get("armSkuName") == size and item.get("armRegionName") == region
                        and item.get("currencyCode") == "USD" and item.get("unitOfMeasure") == "1 Hour"
                        and item.get("type") == price_type and "Windows" in item.get("productName", "")
                        and item.get("meterName", "").endswith(" Spot")
                        and item.get("isPrimaryMeterRegion") is True and number(item.get("tierMinimumUnits", 0)) == 0):
                    effective = datetime.fromisoformat(item["effectiveStartDate"])
                    if effective.tzinfo and effective <= datetime.now(timezone.utc):
                        number(item["retailPrice"])
                        matches.append(item)
            url = page.get("NextPageLink")
        if not matches:
            raise ValueError("No matching Windows Spot retail rate")
        latest = max(item["effectiveStartDate"] for item in matches)
        matches = [item for item in matches if item["effectiveStartDate"] == latest]
        if len({number(item["retailPrice"]) for item in matches}) != 1:
            raise ValueError("Ambiguous Windows Spot retail rates")
        item = matches[0]
        return {k: item[k] for k in ("retailPrice", "meterName", "productName", "type", "effectiveStartDate", "armRegionName", "armSkuName")}
    return cached_price(key, fetch, cache_dir)


def estimate_rows(rows, subscription, warnings, cache_dir=None, retail_price_type="DevTestConsumption"):
    azure_rows = [r for r in rows if r["cloud_provider"] == "azure"]
    def fallback_eligible(row):
        return (row["cloud_provider"] == "azure" and row.get("started") and row.get("resolved")
                and row.get("duration_seconds") not in (None, "") and row.get("vm_size") and row.get("region")
                and re.search(r"(?:^|[/-])(?:win|windows)[0-9]", (row.get("worker_pool") or "") + "/" + (row.get("name") or "")))
    eligible = [r for r in azure_rows if fallback_eligible(r)]
    retail_cached = (eligible and len(eligible) == len(azure_rows)
                     and all(cached_price(retail_cache_key(r["vm_size"], r["region"], retail_price_type), cache_dir=cache_dir)
                             for r in eligible))
    subscription_cached = subscription and cached_price(billing_cache_key(azure_rows, subscription), cache_dir=cache_dir)
    rates, retrieved, status = {}, "", ""
    if retail_cached and not subscription_cached:
        warnings.append("Fresh 24-hour public Spot fallback cache covers these Azure tasks; no subscription query was sent.")
    elif azure_rows and subscription:
        try:
            rates, retrieved, status = cached_billing_rates(azure_rows, subscription, cache_dir)
            warnings.append(f"Azure subscription-rate cache {status}; retrieved {retrieved}; expires 24 hours after retrieval.")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            warnings.append(f"Azure subscription query unavailable ({detail}); checking cached/current public Spot fallback.")
    result = price_rows(rows, rates)
    attempted = {}
    for row in result:
        if row.get("azure_runtime_estimate_usd"):
            row.update(azure_rate_source="Azure Cost Management", azure_rate_retrieved_at=retrieved, azure_rate_cache_status=status)
        elif fallback_eligible(row):
            key = (row["vm_size"], row["region"])
            if key not in attempted:
                try:
                    attempted[key] = retail_rate(*key, retail_price_type, cache_dir)
                except (OSError, ValueError, KeyError) as exc:
                    attempted[key] = None
                    detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
                    warnings.append(f"Azure public Spot fallback unavailable for {key[0]}/{key[1]} ({detail}); cost remains Not determined.")
            if attempted[key]:
                item, fetched, hit = attempted[key]
                rate = number(item["retailPrice"])
                row.update(azure_spot_usd_per_hour=format(rate, ".8f"),
                           azure_runtime_estimate_usd=format(number(row["duration_seconds"]) / 3600 * rate, ".8f"),
                           azure_rate_date="", azure_rate_source="https://prices.azure.com/api/retail/prices",
                           azure_rate_retrieved_at=fetched, azure_rate_cache_status=hit,
                           azure_price_note=f"FALLBACK: current public Windows {retail_price_type} Spot price retrieved {fetched}; cache {hit}; NOT historical, negotiated or a 24-hour average; runtime only")
                row.update(cost_columns(row))
    if any(r.get("azure_rate_source", "").startswith("https://prices.azure.com/") for r in result):
        warnings.append("Azure includes CURRENT public Windows Spot retail fallback estimates, not subscription-specific historical prices or 24-hour averages. Cache TTL is 24 hours from retrieval; DevTest pricing excludes separate Windows license charges.")
    return result


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
        warnings = []
        result = estimate_rows(rows, args.subscription, warnings)
        for warning in warnings:
            print(warning, file=sys.stderr)
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
