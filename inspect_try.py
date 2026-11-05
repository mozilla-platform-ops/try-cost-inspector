#!/usr/bin/env python3
"""Export task runs and observed infrastructure for a Firefox try push."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen


TC = "https://firefox-ci-tc.services.mozilla.com/api"
TH = "https://treeherder.mozilla.org/api/project/try"
FIELDS = ["task_id", "run_id", "name", "category", "failure_category", "task_kind", "test_suite", "test_type",
          "state", "worker_pool", "worker_group",
          "worker_id", "infrastructure", "cloud_provider", "region", "zone",
          "vm_size", "started", "resolved", "duration_seconds", "metadata_source",
          "notes"]


def get(url, limit=None):
    request = Request(url, headers={"User-Agent": "try-cost-inspector/0.1 (+https://github.com/mozilla-platform-ops/try-cost-inspector)"})
    with urlopen(request, timeout=60) as response:
        return response.read() if limit is None else response.read(limit)


def get_json(url):
    return json.loads(get(url))


def revision_from(value):
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.hostname != "treeherder.mozilla.org":
            raise ValueError("Expected a Treeherder URL or a full revision")
        params = parse_qs(parsed.query or urlsplit(parsed.fragment).query)
        if params.get("repo") != ["try"]:
            raise ValueError("Only repo=try is supported")
        value = params.get("revision", [""])[0]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("Expected a full 40-character revision")
    return value.lower()


def paginated(url, key):
    while True:
        page = get_json(url)
        yield from page[key]
        token = page.get("continuationToken")
        if not token:
            break
        url = url.split("?", 1)[0] + "?" + urlencode({"continuationToken": token})


def push_tasks(revision):
    pushes = get_json(f"{TH}/push/?revision={revision}")["results"]
    if len(pushes) != 1:
        raise ValueError(f"Expected one try push; found {len(pushes)}")
    push_id = pushes[0]["id"]
    index = get_json(f"{TC}/index/v1/task/gecko.v2.try.revision.{revision}.taskgraph.decision")
    group_id = index["taskId"]
    tasks = {t["status"]["taskId"]: t for t in paginated(
        f"{TC}/queue/v1/task-group/{group_id}/list", "tasks")}
    # Include action/retrigger tasks shown on the push even if in another group.
    offset = 0
    while True:
        jobs = get_json(f"{TH}/jobs/?push_id={push_id}&count=1000&offset={offset}")["results"]
        for job in jobs:
            task_id = job.get("task_id")
            if task_id and task_id not in tasks:
                task_url = f"{TC}/queue/v1/task/{quote(task_id, safe='')}"
                tasks[task_id] = {"task": get_json(task_url),
                                  "status": get_json(task_url + "/status")["status"]}
        if len(jobs) < 1000:
            break
        offset += len(jobs)
    return list(tasks.values())


def worker_settings(log):
    """Parse only the worker's JSON preamble, before any task output."""
    lines = log.decode("utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\[taskcluster [^]]+\] Worker Type \([^)]+\) settings:$", line):
            preamble = []
            for following in lines[i + 1:]:
                match = re.match(r"^\[taskcluster [^]]+\] (.*)$", following)
                if not match:
                    break
                preamble.append(match[1])
                try:
                    settings = json.loads("\n".join(preamble))
                except json.JSONDecodeError:
                    continue
                return settings if isinstance(settings, dict) else {}
            break
    return {}


def task_category(kind, suite, test_type):
    if kind == "decision-task":
        return "Orchestration"
    if "signing" in kind.split("-"):
        return "Signing"
    if kind == "generate-profile":
        return "Profile generation"
    if kind in ("build", "instrumented-build"):
        return "Build"
    if kind.startswith("repackage"):
        return "Packaging"
    if kind == "browsertime" or test_type in ("raptor", "talos") or suite in ("raptor", "talos"):
        return "Performance test"
    if kind == "source-test" or suite == "xpcshell":
        return "Unit/source test"
    if suite == "marionette-integration":
        return "Integration test"
    if suite in ("mochitest-webgl1-core", "mochitest-webgl2-core", "web-platform-tests-reftest"):
        return "Graphics/rendering test"
    if suite in ("mochitest-media", "mochitest-browser-media"):
        return "Media test"
    if suite in ("mochitest-plain", "mochitest-browser-chrome", "mochitest-chrome"):
        return "Functional/browser test"
    # shortcut: map known kinds/suites only, extend as other suites are inspected.
    return "Other/unknown"


def run_row(task, run):
    status = task["status"]
    definition = task["task"]
    tags = definition.get("tags", {})
    kind = tags.get("kind", "")
    suite = tags.get("test-suite") or definition.get("extra", {}).get("suite", "")
    test_type = tags.get("test-type", "")
    row = dict.fromkeys(FIELDS)
    row.update(task_id=status["taskId"], run_id=run["runId"],
               name=definition["metadata"]["name"], state=run["state"],
               category=task_category(kind, suite, test_type), task_kind=kind,
               test_suite=suite, test_type=test_type,
               worker_pool=status.get("taskQueueId") or
               f'{status["provisionerId"]}/{status["workerType"]}',
               worker_group=run.get("workerGroup"), worker_id=run.get("workerId"),
               started=run.get("started"), resolved=run.get("resolved"),
               infrastructure="unknown")
    is_test = (definition.get("extra", {}).get("treeherder", {}).get("jobKind") == "test"
               or row["category"].endswith("test"))
    row["failure_category"] = "Failed test" if run["state"] == "failed" and is_test else ""
    notes = []
    if not row["started"]:
        row["notes"] = "Never started; no execution duration"
        return row
    if row["resolved"]:
        seconds = (datetime.fromisoformat(row["resolved"]) -
                   datetime.fromisoformat(row["started"])).total_seconds()
        if seconds < 0:
            raise ValueError(f'Negative duration for {row["task_id"]}/{row["run_id"]}')
        row["duration_seconds"] = round(seconds, 3)
    else:
        notes.append("Still running; final duration unavailable")
    url = (f'{TC}/queue/v1/task/{row["task_id"]}/runs/{row["run_id"]}'
           '/artifacts/public/logs/live_backing.log')
    try:
        # shortcut: generic-worker preamble only, add other log formats when needed.
        settings = worker_settings(get(url, limit=65536))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        settings = {}
        notes.append(f"Worker log unavailable: {exc}")
    provider = None
    if settings.get("project-id"):
        provider = "gcp"
    elif settings.get("vm-id"):
        provider = "azure"
    elif str(settings.get("instance-id", "")).startswith("i-"):
        provider = "aws"
    row.update(cloud_provider=provider,
               region=str(settings["region"]).lower() if settings.get("region") else None,
               zone=settings.get("zone") or settings.get("availability-zone"),
               vm_size=settings.get("instance-type", "").rsplit("/", 1)[-1] or None)
    if provider:
        row["infrastructure"] = "cloud-vm"
    elif status["provisionerId"] == "releng-hardware":
        row["infrastructure"] = "physical-hardware"
        notes.append("Physical hardware; cloud region and VM size not applicable")
    if settings:
        row["metadata_source"] = url
    if row["infrastructure"] != "physical-hardware" and any(
            not row[field] for field in ("cloud_provider", "region", "vm_size")):
        notes.append("Incomplete cloud metadata; no VM size inferred from current pool config")
    row["notes"] = "; ".join(notes)
    return row


def inspect(revision):
    tasks = push_tasks(revision)
    attempts = [(task, run) for task in tasks for run in task["status"]["runs"]]
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda pair: run_row(*pair), attempts))
    return sorted(rows, key=lambda row: (row["name"], row["task_id"], row["run_id"]))


def write_csv(rows, stream):
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("push", help="Treeherder try URL or full revision")
    args = parser.parse_args()
    try:
        rows = inspect(revision_from(args.push))
    except (ValueError, KeyError, URLError, OSError) as exc:
        parser.exit(1, f"Inspection failed: {exc}\n")
    write_csv(rows, sys.stdout)
    print(f'{len(rows)} task runs; {sum(bool(r["started"]) for r in rows)} started; '
          f'{sum(r["infrastructure"] == "unknown" for r in rows)} unknown infrastructure',
          file=sys.stderr)


if __name__ == "__main__":
    main()
