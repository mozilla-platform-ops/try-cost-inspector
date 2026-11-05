#!/usr/bin/env python3
"""Run a POC analysis or prediction and write CSVs plus a caveated Actions summary."""

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import URLError

import inspect_try as inspection
import predict_try as predictor
import price_azure as azure
import price_gcp as gcp


CAVEATS = [
    "POC estimate, not an invoice or complete push cost. Not determined means unknown, never zero.",
    "Storage is not yet included. Temporary storage bundled into Azure VM rates remains in those rates.",
    "Spot instances are assumed; this does not verify the original VM's purchasing model.",
    "Only task execution runtime is charged. Queue wait, boot/idle outside the task, networking and shared overhead are excluded.",
    "Azure rates are daily, usage-weighted subscription billing averages with reporting lag; they can blend OS/licensing classes and price changes. They are not exact per-VM charges or rolling 24-hour Spot prices.",
    "GCP uses historical public CPU/RAM Spot list prices, not negotiated project/account rates, credits or discounts. More accurate billing access is deferred.",
    "Hardware, shared signing/artifact workers, unsupported machines/regions and missing metadata/rates remain Not determined.",
    "Task classification covers known metadata only; Other/unknown categories may need refinement. A Failed test identifies a task, not an individual test case.",
]
PREDICTION_CAVEATS = [
    "Prediction uses the optimized scheduled graph plus decision, excluding reused dependencies. Later action tasks, retriggers and retries are not forecast.",
    "Runtime is a completed-run median by task name and pool, falling back to task kind and pool with low confidence. Sparse, cached or unrepresentative runs can severely underestimate long builds/tests.",
    "Placement uses the historical VM/region mix; the actual scheduler may select different hardware. Missing history or any missing scenario price leaves that task cost unknown.",
    "GCP prediction integrates Spot CPU/RAM prices over the preceding 24 hours. Azure prediction explicitly uses the latest available daily billing-rate approximation, at most seven days old.",
    "Seed history contains only two example pushes. This is not a trained or calibrated budget model. Future and target-push observations are excluded.",
]
CLOUD_ERRORS = (OSError, ValueError, KeyError, URLError, subprocess.SubprocessError)


def write_csv(path, rows, fields=None):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def catalog_for(start, end, warnings):
    try:
        return gcp.fetch_catalog([{"started": start.isoformat(), "resolved": end.isoformat()}])
    except CLOUD_ERRORS as exc:
        warnings.append(f"Live GCP catalog unavailable ({type(exc).__name__}); billing API/catalog access may be missing. No APIs or IAM grants were changed.")
    for path in sorted(Path("examples").glob("*-catalog*.json")):
        catalog = json.loads(path.read_text())
        if gcp.timestamp(catalog["requested_start"]) <= start and end <= gcp.timestamp(catalog["requested_end"]):
            warnings.append(f"Used saved GCP catalog {path.name}, covering this exact requested window; no stale rates were extended.")
            return catalog
    warnings.append("No saved GCP catalog covers the entire requested window; GCP prices remain Not determined.")
    return {"requested_start": start.isoformat(), "requested_end": end.isoformat(), "skus": []}


def azure_rates_for(rows, subscription, warnings):
    if not rows or not subscription:
        if rows:
            warnings.append("Azure subscription is not configured; Azure prices remain Not determined.")
        return {}
    try:
        return azure.billing_rates(azure.fetch_billing(rows, subscription))
    except CLOUD_ERRORS as exc:
        warnings.append(f"Azure billing query unavailable ({type(exc).__name__}); Azure prices remain Not determined.")
        return {}


def prediction_azure_rates(history, as_of, subscription, warnings):
    start = as_of - timedelta(hours=24)
    samples = [r for r in history if r.get("cloud_provider") == "azure"]
    # Query around forecast time, not the historical task execution dates.
    current = [dict(r, started=as_of.isoformat()) for r in samples]
    rates = azure_rates_for(current, subscription, warnings)
    result = []
    for size, region in sorted({(r["vm_size"], r["region"]) for r in samples if r.get("vm_size") and r.get("region")}):
        meter = size.removeprefix("Standard_").replace("_", " ") + " Spot"
        matches = [(day, rate) for (name, location, day), rate in rates.items()
                   if name == meter and location == region and 0 <= (as_of.date() - day).days <= 7]
        if matches:
            day, rate = max(matches)
            result.append({"vm_size": size, "region": region, "window_start": start.isoformat(),
                           "window_end": as_of.isoformat(), "usd_per_hour": str(rate),
                           "price_note": f"Approximation: daily blended subscription Spot rate ({day}); exact rolling 24-hour average unavailable"})
    return result


def cell(value):
    return str(value if value not in (None, "") else "Not determined").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)] + [
        "| " + " | ".join(cell(value) for value in row) + " |" for row in rows]


def render_report(mode, revision, rows, warnings, output):
    prediction = mode == "predict"
    field = "predicted_compute_usd" if prediction else "total_known_cost_usd"
    costs = [azure.number(r[field]) for r in rows if r.get(field)]
    subtotal = f"${sum(costs, Decimal(0)):.8f} USD" if costs else "Not determined"
    lines = [f"# POC Cost {'Predictor' if prediction else 'Analysis'}", "",
             f"Try revision: `{revision}`", "", f"**Known compute subtotal: {subtotal}. Priced {len(costs)}/{len(rows)} {'scheduled tasks (including decision)' if prediction else 'task runs'}.**", "",
             "**Storage is not yet included. Totals are partial.**", ""]
    if prediction and rows:
        lines += [f"Requested pricing window: {rows[0]['price_window_start']} through {rows[0]['price_window_end']}.", ""]
        exact = sum(r["history_basis"] == "Same task name and pool" for r in rows)
        fallback = sum("low confidence" in r["history_basis"] for r in rows)
        lines += [f"Duration history: {exact} exact name/pool matches, {fallback} low-confidence kind/pool fallbacks, {len(rows)-exact-fallback} without history.", ""]
    lines += ["## Caveats", ""] + [f"- {note}" for note in CAVEATS + (PREDICTION_CAVEATS if prediction else [
        "Analysis includes observed failed runs and retries; reused dependencies are not counted as new executions. A running push is a snapshot and must be rerun after completion.",
        "Runtime is resolved minus started, including setup/cleanup inside that interval. Logs can expire or omit cloud metadata; no historical machine size is inferred from current pool configuration.",
        "Failed-test and infrastructure-exception subtotals below are already included in the known total; do not add them again.",
    ])] + ["", "## Data availability", ""]
    lines += [f"- {warning}" for warning in warnings] or ["No provider-query failures reported. Unsupported rows may still be unknown."]
    lines += ["", "## Tasks", ""]
    if prediction:
        lines += table(["Task", "Type", "Pool", "Provider / region / VM", "Predicted minutes", "History samples / basis", "USD", "Price basis"], [
            [r["name"], r["category"], r["worker_pool"], " / ".join(r[k] for k in ("cloud_provider", "region", "vm_size")),
             f"{azure.number(r['predicted_duration_seconds'])/60:.2f}" if r["predicted_duration_seconds"] else "",
             f"{r['history_samples']} / {r['history_basis']}", r[field], r["price_note"]] for r in rows])
    else:
        lines += table(["Task", "Run", "Type", "Pool", "Provider / region / VM", "Minutes", "Result", "USD"], [
            [r["name"], r["run_id"], r["category"], r["worker_pool"], " / ".join(r[k] or "" for k in ("cloud_provider", "region", "vm_size")),
             f"{azure.number(r['duration_seconds'])/60:.2f}" if r.get("duration_seconds") not in (None, "") else "", r["state"], r[field]] for r in rows])
    summaries = []
    for category in sorted({r["category"] for r in rows}):
        selected = [r for r in rows if r["category"] == category]
        amounts = [azure.number(r[field]) for r in selected if r.get(field)]
        summaries.append({"category": category, "task_runs": len(selected), "priced_task_runs": len(amounts),
                          "estimated_usd": str(sum(amounts, Decimal(0))) if amounts else ""})
    write_csv(output / "category-costs.csv", summaries, ["category", "task_runs", "priced_task_runs", "estimated_usd"])
    lines += ["", "## Cost by task type", ""] + table(["Type", "Runs/tasks", "Priced", "Known USD"], [list(r.values()) for r in summaries])
    if not prediction:
        for title, selected in [("Failed tests", [r for r in rows if r.get("failure_category") == "Failed test"]),
                                ("Infrastructure exceptions", [r for r in rows if r["state"] == "exception"])]:
            amounts = [azure.number(r[field]) for r in selected if r.get(field)]
            lines += ["", f"{title}: {len(selected)} runs; {len(amounts)} priced; known subtotal {'$'+str(sum(amounts, Decimal(0))) if amounts else 'Not determined'} (already included)."]
        hardware = []
        for pool in sorted({r["worker_pool"] for r in rows if r["infrastructure"] == "physical-hardware"}):
            selected = [r for r in rows if r["worker_pool"] == pool]
            hardware.append({"worker_pool": pool, "task_runs": len(selected), "observed_workers": len({(r["worker_group"], r["worker_id"]) for r in selected}),
                             "runtime_minutes": str(sum((azure.number(r["duration_seconds"]) for r in selected if r.get("duration_seconds") not in (None, "")), Decimal(0))/60), "cost": "Not determined"})
        write_csv(output / "hardware-totals.csv", hardware, ["worker_pool", "task_runs", "observed_workers", "runtime_minutes", "cost"])
        lines += ["", "## Hardware totals", ""] + table(["Pool", "Runs", "Distinct workers", "Minutes", "Cost"], [list(r.values()) for r in hardware])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("analysis", "predict"))
    parser.add_argument("revision")
    args = parser.parse_args()
    output = Path("reports")
    output.mkdir(exist_ok=True)
    warnings = []
    try:
        revision = predictor.resolve_revision(args.revision)
        subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        if args.mode == "analysis":
            rows = inspection.inspect(revision)
            write_csv(output / "tasks.csv", rows, inspection.FIELDS)
            azure_rows = [r for r in rows if r["cloud_provider"] == "azure"]
            rows = azure.price_rows(rows, azure_rates_for(azure_rows, subscription, warnings))
            eligible = [r for r in rows if r["cloud_provider"] == "gcp" and r.get("started") and r.get("resolved") and r.get("vm_size") in gcp.SPECS]
            catalog = catalog_for(min(gcp.timestamp(r["started"]) for r in eligible), max(gcp.timestamp(r["resolved"]) for r in eligible), warnings) if eligible else {}
            rows = gcp.price_rows(rows, catalog)
        else:
            as_of = datetime.now(timezone.utc).replace(microsecond=0)
            decision_id = inspection.get_json(f"{inspection.TC}/index/v1/task/gecko.v2.try.revision.{revision}.taskgraph.decision")["taskId"]
            decision = inspection.get_json(f"{inspection.TC}/queue/v1/task/{decision_id}")
            graph = inspection.get_json(f"{inspection.TC}/queue/v1/task/{decision_id}/artifacts/public/task-graph.json")
            history = []
            for name in ("0bb06e8-tasks.csv", "38085f6-tasks.csv"):
                with (Path("examples") / name).open(newline="") as f:
                    history.extend(csv.DictReader(f))
            catalog = catalog_for(as_of - timedelta(hours=24), as_of, warnings)
            rates = prediction_azure_rates(history, as_of, subscription, warnings)
            write_csv(output / "azure-prediction-rates.csv", rates, ["vm_size", "region", "window_start", "window_end", "usd_per_hour", "price_note"])
            rows = predictor.predict_rows(graph, decision, decision_id, history, catalog, as_of, rates)
        (output / "gcp-catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
        write_csv(output / "costs.csv", rows, predictor.FIELDS if args.mode == "predict" else inspection.FIELDS + azure.PRICE_FIELDS + azure.COST_FIELDS + gcp.FIELDS)
        report = render_report(args.mode, revision, rows, warnings, output)
    except CLOUD_ERRORS as exc:
        report = "# POC cost report failed\n\nNo complete report was produced. Failure type: " + type(exc).__name__ + ".\n\n" + "\n".join(f"- {note}" for note in CAVEATS)
        (output / "report.md").write_text(report)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(report)
        parser.exit(1, f"Report failed ({type(exc).__name__}); check revision, decision artifacts and provider configuration.\n")
    (output / "report.md").write_text(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report)
    print(report)


if __name__ == "__main__":
    main()
