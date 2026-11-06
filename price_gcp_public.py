#!/usr/bin/env python3
"""Current regional public Spot CPU/RAM prices for POC estimates, without billing access."""

from datetime import datetime, timezone
from html import unescape
import json
import re

from inspect_try import get
from price_azure import cost_columns, number
from price_gcp import FIELDS, SPECS


URL = "https://cloud.google.com/spot-vms/pricing"


def text_leaves(node):
    if isinstance(node, str):
        yield unescape(re.sub(r"<[^>]*>", "", node)).strip()
    elif isinstance(node, list):
        for child in node:
            yield from text_leaves(child)


def parse_rates(html):
    # shortcut: Google's regional table payload is undocumented; fail closed if it changes.
    marker = re.search(r"AF_initDataCallback\(\{key: 'ds:0'.*?\bdata:", html, re.S)
    if not marker:
        raise ValueError("Public Spot page regional pricing payload is missing")
    data, _ = json.JSONDecoder().raw_decode(html[marker.end():].lstrip())
    expected = {size.removesuffix("-lssd"): spec[:2] for size, spec in SPECS.items()}
    rates = {}

    def visit(node):
        if not isinstance(node, list):
            return
        if len(node) >= 3 and isinstance(node[1], list) and isinstance(node[2], str):
            region = re.search(r"\(([a-z]+-[a-z]+[0-9]+)\)$", node[2])
            headers = list(text_leaves(node[0]))
            if region and headers == ["Machine type", "Virtual CPUs", "Memory", "Current Spot pricing (USD)"]:
                for row in node[1]:
                    cells = list(text_leaves(row))
                    if len(cells) != 4 or cells[0] not in expected:
                        continue
                    size, cpu, memory, price = cells
                    memory_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?) GiB", memory)
                    price_match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?) / 1 hour", price)
                    if not memory_match or not price_match:
                        continue  # Ignore monthly views and unavailable prices, never guess units.
                    if (number(cpu), number(memory_match[1])) != expected[size]:
                        raise ValueError(f"Public Spot machine specifications changed: {size}")
                    key = (size, region[1])
                    value = number(price_match[1])
                    if key in rates and rates[key] != value:
                        raise ValueError(f"Conflicting public Spot prices: {size}/{region[1]}")
                    rates[key] = value
        for child in node:
            visit(child)

    visit(data)
    if not rates:
        raise ValueError("No supported regional USD hourly CPU/RAM Spot prices found")
    return rates


def fetch_rates():
    rates = parse_rates(get(URL, limit=32 * 1024 * 1024).decode("utf-8"))
    return {"source": URL, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "pricing_basis": "Current public webpage CPU/RAM prices; not historical or a 24-hour average",
            "rates": [{"machine_type": size, "region": region, "usd_per_hour": str(rate)}
                      for (size, region), rate in sorted(rates.items())]}


def hourly_price(size, region, rates):
    return rates.get(((size or "").removesuffix("-lssd"), region)) if size in SPECS else None


def price_rows(rows, snapshot):
    rates = {(r["machine_type"], r["region"]): number(r["usd_per_hour"]) for r in snapshot["rates"]}
    result = []
    for original in rows:
        row = dict(original)
        row.update(dict.fromkeys(FIELDS, ""))
        row["gcp_price_note"] = "Not priced: not identified as GCP"
        if row["cloud_provider"] == "gcp":
            size = row.get("vm_size") or ""
            base = size.removesuffix("-lssd")
            rate = hourly_price(size, row.get("region"), rates)
            if row.get("duration_seconds") in (None, "") or not row.get("resolved"):
                row["gcp_price_note"] = "Not priced: final execution duration unavailable"
            elif rate is None:
                row["gcp_price_note"] = "Not priced: no supported current public CPU/RAM rate for this exact region/machine"
            else:
                cost = number(row["duration_seconds"]) / 3600 * rate
                row.update(gcp_spot_usd_per_hour=format(rate, ".8f"),
                           gcp_runtime_estimate_usd=format(cost, ".8f"),
                           gcp_rate_source=snapshot["source"],
                           gcp_rate_components=json.dumps([{"component": "cpu_ram", "machine_type": base,
                               "region": row["region"], "usd_per_hour": str(rate),
                               "retrieved_at": snapshot["retrieved_at"]}], separators=(",", ":")),
                           gcp_price_note=f"Spot assumed; CURRENT public webpage CPU/RAM prices retrieved {snapshot['retrieved_at']}; NOT historical or a 24-hour average; storage is not yet included; no negotiated discounts/credits")
        row.update(cost_columns(row))
        result.append(row)
    return result
