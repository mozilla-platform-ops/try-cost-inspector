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
import price_gcp_public as public_spot


CAVEATS = [
    "POC estimate, not an invoice or complete push cost. Not determined means unknown, never zero.",
    "Storage is not yet included. Temporary storage bundled into Azure VM rates remains in those rates.",
    "Spot instances are assumed; this does not verify the original VM's purchasing model.",
    "Only task execution runtime is charged. Queue wait, boot/idle outside the task, networking and shared overhead are excluded.",
    "Azure prefers subscription billing rates cached for 24 hours, then uses current public Windows DevTest Spot retail fallback prices when available. Billing rates are daily usage-weighted averages with reporting lag and may blend OS/licensing classes. Public fallback is explicitly labeled; neither source is an exact per-VM historical charge or rolling 24-hour average.",
    "GCP uses public CPU/RAM Spot list prices, not negotiated project/account rates, credits or discounts. More accurate billing access is deferred.",
    "Hardware, shared signing/artifact workers, unsupported machines/regions and missing metadata/rates remain Not determined.",
    "Task classification covers known metadata only; Other/unknown categories may need refinement. A Failed test identifies a task, not an individual test case.",
]
PREDICTION_CAVEATS = [
    "Prediction uses the optimized scheduled graph plus decision, excluding reused dependencies. Later action tasks, retriggers and retries are not forecast.",
    "Runtime is a completed-run median by task name and pool, falling back to task kind and pool with low confidence. Sparse, cached or unrepresentative runs can severely underestimate long builds/tests.",
    "Placement uses the historical VM/region mix; the actual scheduler may select different hardware. Missing history or any missing scenario price leaves that task cost unknown.",
    "GCP prediction uses CURRENT regional public webpage Spot CPU/RAM prices, not historical or a preceding-24-hour average. No GCP authentication or Billing API access is required. Azure uses a daily billing-rate approximation at most seven days old, or explicitly labeled current public Spot fallback. A 24-hour cache lifetime is not a 24-hour average price.",
    "Seed history contains only two example pushes. This is not a trained or calibrated budget model. Future and target-push observations are excluded.",
]
CLOUD_ERRORS = (OSError, ValueError, KeyError, URLError, subprocess.SubprocessError)


def write_csv(path, rows, fields=None):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def prediction_azure_rates(history, as_of, subscription, warnings):
    start = as_of - timedelta(hours=24)
    # One-hour probes obtain a rate; these are not observed task executions.
    probes = [dict(r, started=as_of.isoformat(), resolved=(as_of + timedelta(hours=1)).isoformat(),
                   duration_seconds="3600") for r in history if r.get("cloud_provider") == "azure"]
    priced = azure.estimate_rows(probes, subscription, warnings)
    result = {}
    for row in priced:
        if row.get("azure_spot_usd_per_hour"):
            key = (row["vm_size"], row["region"])
            result[key] = {"vm_size": key[0], "region": key[1], "window_start": start.isoformat(),
                           "window_end": as_of.isoformat(), "usd_per_hour": row["azure_spot_usd_per_hour"],
                           "price_note": row["azure_price_note"] + "; Azure price is an approximation, not an exact rolling 24-hour average"}
    return list(result.values())


def cell(value):
    return str(value if value not in (None, "") else "Not determined").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)] + [
        "| " + " | ".join(cell(value) for value in row) + " |" for row in rows]


def render_report(mode, revision, rows, warnings, output):
    prediction = mode == "predict"
    field = "predicted_compute_usd" if prediction else "total_known_cost_usd"
    duration_field = "predicted_duration_seconds" if prediction else "duration_seconds"

    def runtime(selected):
        durations = [azure.number(r[duration_field]) for r in selected if r.get(duration_field) not in (None, "")]
        return format(sum(durations, Decimal(0)) / 60, ".4f") if durations else ""

    def is_hardware(row):
        return row.get("infrastructure") == "physical-hardware" or (
            prediction and row["worker_pool"].startswith("releng-hardware/"))

    def provider(row):
        if is_hardware(row):
            return "Physical hardware"
        return row["cloud_provider"].upper() if row.get("cloud_provider") else "Uncategorized"

    def summary(label, name, selected):
        amounts = [azure.number(r[field]) for r in selected if r.get(field)]
        return {label: name, "task_runs": len(selected), "priced_task_runs": len(amounts),
                "unpriced_task_runs": len(selected) - len(amounts),
                "runtime_minutes": runtime(selected),
                "estimated_usd": str(sum(amounts, Decimal(0))) if amounts or not selected else ""}

    costs = [azure.number(r[field]) for r in rows if r.get(field)]
    subtotal = f"${sum(costs, Decimal(0)):.8f} USD" if costs else "Not determined"
    lines = [f"# POC Cost {'Predictor' if prediction else 'Analysis'}", "", "## Total cost", "",
             f"**Known compute subtotal: {subtotal}. Priced {len(costs)}/{len(rows)} {'scheduled tasks (including decision)' if prediction else 'task runs'}.**", "",
             "**Storage is not yet included. Totals are partial.**", "", f"Try revision: `{revision}`", ""]
    duration_count = sum(r.get(duration_field) not in (None, "") for r in rows)
    total_time = f"{Decimal(runtime(rows)):.2f} minutes" if runtime(rows) else "Not determined"
    lines += [f"**{'Predicted total task runtime' if prediction else 'Total task runtime'}: {total_time}** ({duration_count}/{len(rows)} with duration). Summed task time, not elapsed push time.", ""]
    if prediction and rows:
        azure_rows = [r for r in rows if r["cloud_provider"] == "azure"]
        if azure_rows:
            lines += [f"Azure rate reference window: {azure_rows[0]['price_window_start']} through {azure_rows[0]['price_window_end']}; rates are approximations.", ""]
        exact = sum(r["history_basis"] == "Same task name and pool" for r in rows)
        fallback = sum("low confidence" in r["history_basis"] for r in rows)
        lines += [f"Duration history: {exact} exact name/pool matches, {fallback} low-confidence kind/pool fallbacks, {len(rows)-exact-fallback} without history.", ""]

    for title, label, groups in [
            ("Cost by provider", "provider", [(name, [r for r in rows if provider(r) == name]) for name in sorted({provider(r) for r in rows})]),
            ("Cost by task type", "category", [(name, [r for r in rows if r["category"] == name]) for name in sorted({r["category"] for r in rows})])]:
        summaries = [summary(label, name, selected) for name, selected in groups]
        write_csv(output / ("provider-costs.csv" if label == "provider" else "category-costs.csv"), summaries,
                  [label, "task_runs", "priced_task_runs", "unpriced_task_runs", "runtime_minutes", "estimated_usd"])
        lines += ["", "## " + title, ""] + table(
            ["Provider" if label == "provider" else "Type", "Runs/tasks", "Priced", "Not determined", "Runtime minutes", "Known USD"],
            [list(r.values()) for r in summaries])

    missing = [r for r in rows if not is_hardware(r) and not r.get(field)]
    lines += ["", "## Uncategorized non-hardware costs", ""]
    uncategorized = [{"worker_pool": pool, "task_runs": sum(r["worker_pool"] == pool for r in missing),
                      "runtime_minutes": runtime([r for r in missing if r["worker_pool"] == pool])}
                     for pool in sorted({r["worker_pool"] for r in missing})]
    write_csv(output / "uncategorized-totals.csv", uncategorized, ["worker_pool", "task_runs", "runtime_minutes"])
    if missing:
        lines += [f"**Uncategorized: {len(missing)} non-hardware {'tasks' if prediction else 'task runs'}; total runtime {runtime(missing) or 'Not determined'} minutes.** Their costs are Not determined and missing from the known subtotal.", ""]
        lines += table(["Worker pool", "Uncategorized runs/tasks", "Runtime minutes"], [list(r.values()) for r in uncategorized])
    else:
        lines += ["No uncategorized non-hardware task costs."]
    lines += ["", "Storage and other excluded cost components are still outside the estimate, including for priced tasks."]

    hardware = []
    for pool in sorted({r["worker_pool"] for r in rows if is_hardware(r)}):
        selected = [r for r in rows if r["worker_pool"] == pool]
        hardware.append({"worker_pool": pool, "task_runs": len(selected),
                         "observed_workers": "" if prediction else len({(r["worker_group"], r["worker_id"]) for r in selected}),
                         "runtime_minutes": runtime(selected),
                         "cost": "Not determined"})
    write_csv(output / "hardware-totals.csv", hardware, ["worker_pool", "task_runs", "observed_workers", "runtime_minutes", "cost"])
    lines += ["", "## Hardware breakdown", ""]
    lines += table(["Pool", "Runs/tasks", "Distinct observed workers", "Predicted minutes" if prediction else "Runtime minutes", "Cost"],
                   [list(r.values()) for r in hardware]) if hardware else ["No physical hardware tasks identified."]

    lines += ["", "## Cost of failed tasks", ""]
    if prediction:
        lines += ["Not predicted. This forecast assumes one run per scheduled task; failures and retries are excluded."]
    else:
        failures = [summary("outcome", title, selected) for title, selected in [
            ("Failed tasks", [r for r in rows if r["state"] == "failed"]),
            ("Failed tests (subset of failed tasks)", [r for r in rows if r.get("failure_category") == "Failed test"]),
            ("Infrastructure exceptions", [r for r in rows if r["state"] == "exception"])]]
        write_csv(output / "failed-task-costs.csv", failures,
                  ["outcome", "task_runs", "priced_task_runs", "unpriced_task_runs", "runtime_minutes", "estimated_usd"])
        lines += table(["Outcome", "Runs", "Priced", "Not determined", "Runtime minutes", "Known USD"], [list(r.values()) for r in failures])
        lines += ["", "These costs are already included in the known total. Failed tests are a subset of failed tasks; do not add these rows together."]

    lines += ["", "## Caveats", ""] + [f"- {note}" for note in CAVEATS + (PREDICTION_CAVEATS if prediction else [
        "GCP analysis uses CURRENT regional public-webpage prices retrieved when the workflow runs, not prices effective when the task ran or a preceding-24-hour average. No GCP billing credentials or Billing API calls are needed for analysis.",
        "Analysis includes observed failed runs and retries; reused dependencies are not counted as new executions. A running push is a snapshot and must be rerun after completion.",
        "Runtime is resolved minus started, including setup/cleanup inside that interval. Logs can expire or omit cloud metadata; no historical machine size is inferred from current pool configuration.",
    ])] + ["", "## Data availability", ""]
    lines += [f"- {warning}" for warning in warnings] or ["No provider-query failures reported. Unsupported rows may still be unknown."]
    lines += ["", "## Individual task breakdown", ""]
    if prediction:
        lines += table(["Task", "Type", "Pool", "Provider / region / VM", "Predicted minutes", "History samples / basis", "USD", "Price basis"], [
            [r["name"], r["category"], r["worker_pool"], " / ".join(r[k] for k in ("cloud_provider", "region", "vm_size")),
             f"{azure.number(r['predicted_duration_seconds'])/60:.2f}" if r["predicted_duration_seconds"] else "",
             f"{r['history_samples']} / {r['history_basis']}", r[field], r["price_note"]] for r in rows])
    else:
        lines += table(["Task", "Run", "Type", "Pool", "Provider / region / VM", "Minutes", "Result", "USD"], [
            [r["name"], r["run_id"], r["category"], r["worker_pool"], " / ".join(r[k] or "" for k in ("cloud_provider", "region", "vm_size")),
             f"{azure.number(r['duration_seconds'])/60:.2f}" if r.get("duration_seconds") not in (None, "") else "", r["state"], r[field]] for r in rows])
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
        snapshot = {"source": public_spot.URL, "retrieved_at": datetime.now(timezone.utc).isoformat(), "rates": []}
        try:
            snapshot = public_spot.fetch_rates()
            warnings.append(f"GCP priced from CURRENT public regional webpage rates retrieved {snapshot['retrieved_at']}; not historical or a 24-hour average. CPU/RAM only; no Billing API request was made.")
        except CLOUD_ERRORS as exc:
            warnings.append(f"GCP public Spot webpage unavailable or changed ({type(exc).__name__}); missing GCP costs remain Not determined.")
        (output / "gcp-public-spot-rates.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        if args.mode == "analysis":
            rows = inspection.inspect(revision)
            write_csv(output / "tasks.csv", rows, inspection.FIELDS)
            rows = azure.estimate_rows(rows, subscription, warnings)
            rows = public_spot.price_rows(rows, snapshot)
        else:
            as_of = datetime.now(timezone.utc).replace(microsecond=0)
            decision_id = inspection.get_json(f"{inspection.TC}/index/v1/task/gecko.v2.try.revision.{revision}.taskgraph.decision")["taskId"]
            decision = inspection.get_json(f"{inspection.TC}/queue/v1/task/{decision_id}")
            graph = inspection.get_json(f"{inspection.TC}/queue/v1/task/{decision_id}/artifacts/public/task-graph.json")
            history = []
            for name in ("0bb06e8-tasks.csv", "38085f6-tasks.csv"):
                with (Path("examples") / name).open(newline="") as f:
                    history.extend(csv.DictReader(f))
            rates = prediction_azure_rates(history, as_of, subscription, warnings)
            write_csv(output / "azure-prediction-rates.csv", rates, ["vm_size", "region", "window_start", "window_end", "usd_per_hour", "price_note"])
            rows = predictor.predict_rows(graph, decision, decision_id, history, {}, as_of, rates, public_rates=snapshot)
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
