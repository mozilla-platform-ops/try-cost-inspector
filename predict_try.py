#!/usr/bin/env python3
"""Predict scheduled try-task compute costs from earlier runs and 24-hour Spot rates."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from statistics import median
import sys
from urllib.error import URLError

import inspect_try as inspection
import price_gcp as gcp
from price_azure import number


FIELDS = ["task_id", "name", "category", "worker_pool", "history_basis", "history_samples",
          "predicted_duration_seconds", "cloud_provider", "region", "vm_size",
          "average_spot_usd_per_hour", "predicted_compute_usd", "price_window_start",
          "price_window_end", "price_note", "notes"]


def resolve_revision(value):
    if value.startswith("https://") or len(value) == 40:
        return inspection.revision_from(value)
    if not re.fullmatch(r"[0-9a-fA-F]{12,39}", value):
        raise ValueError("Expected a try URL or 12–40 hexadecimal revision characters")
    pushes = inspection.get_json(f"{inspection.TH}/push/?revision={value.lower()}")["results"]
    matches = {push["revision"] for push in pushes if push["revision"].startswith(value.lower())}
    if len(matches) != 1:
        raise ValueError("Short revision must resolve to exactly one try push")
    return inspection.revision_from(matches.pop())


def predict_rows(graph, decision, decision_id, history, catalog, as_of, azure_rates=()):
    if decision.get("created") and gcp.timestamp(decision["created"]) > as_of:
        raise ValueError("Prediction timestamp precedes the decision task")
    start = as_of - timedelta(hours=24)
    seen = set()
    previous = []
    for sample in history:
        key = (sample["task_id"], sample["run_id"])
        if key in seen or sample["task_id"] in graph or sample["task_id"] == decision_id:
            continue
        seen.add(key)
        # Completed runs model a normal execution; retries/failures are not forecast.
        if (sample.get("state") == "completed" and sample.get("resolved")
                and gcp.timestamp(sample["resolved"]) <= as_of
                and sample.get("duration_seconds") not in (None, "")):
            number(sample["duration_seconds"])
            previous.append(sample)
    tasks = [(task_id, node["task"]) for task_id, node in graph.items()]
    if decision_id not in graph:
        tasks.append((decision_id, decision))
    result = []
    for task_id, task in tasks:
        tags = task.get("tags", {})
        kind = tags.get("kind", "decision-task" if task_id == decision_id else "")
        pool = task.get("taskQueueId") or f"{task['provisionerId']}/{task['workerType']}"
        name = task["metadata"]["name"]
        row = dict.fromkeys(FIELDS, "")
        row.update(task_id=task_id, name=name, worker_pool=pool,
                   category=inspection.task_category(kind, tags.get("test-suite", ""), tags.get("test-type", "")),
                   price_window_start=start.isoformat(), price_window_end=as_of.isoformat(),
                   notes="Prediction: one run per scheduled task; storage is not yet included")
        samples = [s for s in previous if s["worker_pool"] == pool and s["name"] == name]
        row["history_basis"] = "Same task name and pool"
        if not samples:
            samples = [s for s in previous if s["worker_pool"] == pool and s.get("task_kind") == kind]
            row["history_basis"] = "Same task kind and pool; low confidence"
        row["history_samples"] = len(samples)
        if not samples:
            row["history_basis"] = "Not determined"
            row["notes"] += "; no matching duration or machine history"
            result.append(row)
            continue
        duration = median(number(s["duration_seconds"]) for s in samples)
        row["predicted_duration_seconds"] = format(duration, ".3f")
        machines = Counter((s.get("cloud_provider", ""), s.get("region", ""), s.get("vm_size", "")) for s in samples)
        for field, position in (("cloud_provider", 0), ("region", 1), ("vm_size", 2)):
            row[field] = ";".join(sorted({key[position] for key in machines if key[position]}))
        weighted = Decimal(0)
        notes = set()
        try:
            for (provider, region, size), count in machines.items():
                if provider == "gcp":
                    rate, _, _ = gcp.estimate({"vm_size": size, "region": region,
                        "started": start.isoformat(), "resolved": as_of.isoformat(),
                        "duration_seconds": "86400"}, catalog)
                    notes.add("GCP public Spot CPU/RAM; time-weighted 24-hour average")
                elif provider == "azure":
                    matches = [r for r in azure_rates if r["vm_size"] == size and r["region"] == region
                               and gcp.timestamp(r["window_start"]) == start
                               and gcp.timestamp(r["window_end"]) == as_of]
                    if len(matches) != 1:
                        raise ValueError("No Azure rate for this 24-hour window")
                    rate = number(matches[0]["usd_per_hour"])
                    notes.add(matches[0]["price_note"])
                else:
                    raise ValueError("Cloud machine or pricing not determined")
                weighted += rate * count / len(samples)
        except (ValueError, KeyError) as exc:
            row["price_note"] = "Not determined: " + str(exc)
        else:
            row["average_spot_usd_per_hour"] = format(weighted, ".8f")
            row["predicted_compute_usd"] = format(duration / 3600 * weighted, ".8f")
            row["price_note"] = "; ".join(sorted(notes))
            if len(machines) > 1:
                row["price_note"] += "; machine/region mix weighted by historical sample count"
        result.append(row)
    return sorted(result, key=lambda row: (row["name"], row["task_id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    parser.add_argument("--history", action="append", required=True, help="Earlier inspector CSV; repeat for more pushes")
    parser.add_argument("--graph", help="Saved optimized task-graph.json; use with --decision and --decision-id")
    parser.add_argument("--decision", help="Saved decision task definition JSON")
    parser.add_argument("--decision-id")
    parser.add_argument("--gcp-catalog", help="Historical catalog covering the full preceding 24 hours; otherwise fetch with gcloud")
    parser.add_argument("--azure-rates", help="CSV: vm_size,region,window_start,window_end,usd_per_hour,price_note")
    parser.add_argument("--as-of", help="UTC prediction timestamp; default now")
    args = parser.parse_args()
    try:
        as_of = gcp.timestamp(args.as_of).astimezone(timezone.utc) if args.as_of else datetime.now(timezone.utc)
        if args.graph:
            if not args.decision or not args.decision_id:
                raise ValueError("Saved graph requires --decision and --decision-id")
            with open(args.graph) as f:
                graph = json.load(f)
            with open(args.decision) as f:
                decision = json.load(f)
            decision_id = args.decision_id
            revision = inspection.revision_from(args.revision)
            if decision.get("payload", {}).get("env", {}).get("GECKO_HEAD_REV") != revision:
                raise ValueError("Saved decision does not match the requested revision")
        else:
            revision = resolve_revision(args.revision)
            decision_id = inspection.get_json(f"{inspection.TC}/index/v1/task/gecko.v2.try.revision.{revision}.taskgraph.decision")["taskId"]
            decision = inspection.get_json(f"{inspection.TC}/queue/v1/task/{decision_id}")
            graph = inspection.get_json(f"{inspection.TC}/queue/v1/task/{decision_id}/artifacts/public/task-graph.json")
        history = []
        for path in args.history:
            with open(path, newline="") as f:
                history.extend(csv.DictReader(f))
        if args.gcp_catalog:
            with open(args.gcp_catalog) as f:
                catalog = json.load(f)
        else:
            catalog = gcp.fetch_catalog([{"started": (as_of - timedelta(hours=24)).isoformat(), "resolved": as_of.isoformat()}])
        azure_rates = []
        if args.azure_rates:
            with open(args.azure_rates, newline="") as f:
                azure_rates = list(csv.DictReader(f))
        rows = predict_rows(graph, decision, decision_id, history, catalog, as_of, azure_rates)
    except (OSError, ValueError, KeyError, URLError, gcp.subprocess.SubprocessError) as exc:
        parser.exit(1, f"Prediction failed: {exc}\n")
    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    costs = [number(r["predicted_compute_usd"]) for r in rows if r["predicted_compute_usd"]]
    print(f"Predicted known compute subtotal: ${sum(costs, Decimal(0)):.6f} USD; "
          f"priced {len(costs)}/{len(rows)} scheduled tasks including decision; "
          "storage, retries and later action tasks excluded", file=sys.stderr)


if __name__ == "__main__":
    main()
