import copy
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import inspect_try as tool


REVISION = "0bb06e8c5ee891b8ebb69479dc7822c153c4970b"
TASK = {"task": {"metadata": {"name": "test"}}, "status": {
    "taskId": "example", "provisionerId": "gecko-t", "workerType": "win",
    "taskQueueId": "gecko-t/win", "runs": []}}
RUN = {"runId": 0, "state": "failed", "workerGroup": "eastus",
       "workerId": "vm-example", "started": "2026-11-04T17:48:54.457Z",
       "resolved": "2026-11-04T17:50:53.650Z"}


def log(settings):
    return ("[taskcluster now] Worker Type (gecko-t/win) settings:\n" +
            "\n".join("[taskcluster now] " + line for line in settings.splitlines()) +
            '\n[taskcluster now] === Task Starting ===\n').encode()


class InspectionTests(unittest.TestCase):
    def test_public_requests_identify_the_tool_and_bound_log_reads(self):
        with patch.object(tool, "urlopen") as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = b"log"
            self.assertEqual(tool.get("https://treeherder.mozilla.org/api/", limit=65536), b"log")
            request = fetch.call_args.args[0]
            self.assertIn("try-cost-inspector", request.get_header("User-agent"))
            fetch.return_value.__enter__.return_value.read.assert_called_once_with(65536)

    def test_failed_test_is_an_additional_label(self):
        task = copy.deepcopy(TASK)
        task["task"]["tags"] = {"kind": "test", "test-suite": "xpcshell"}
        with patch.object(tool, "get", return_value=b""):
            row = tool.run_row(task, RUN)
            self.assertEqual(row["category"], "Unit/source test")
            self.assertEqual(row["failure_category"], "Failed test")
            for state in ("completed", "exception", "running"):
                self.assertEqual(tool.run_row(task, dict(RUN, state=state))["failure_category"], "")
            task["task"]["tags"] = {"kind": "build"}
            self.assertEqual(tool.run_row(task, RUN)["failure_category"], "")
            task["task"].update(tags={}, extra={"treeherder": {"jobKind": "test"}})
            self.assertEqual(tool.run_row(task, RUN)["failure_category"], "Failed test")

    def test_categories_follow_task_purpose(self):
        cases = [
            ("build", "", "", "Build"),
            ("instrumented-build", "", "", "Build"),
            ("generate-profile", "", "", "Profile generation"),
            ("source-test", "", "", "Unit/source test"),
            ("test", "xpcshell", "", "Unit/source test"),
            ("mochitest", "mochitest-plain", "mochitest", "Functional/browser test"),
            ("test", "marionette-integration", "", "Integration test"),
            ("mochitest", "mochitest-webgl2-core", "mochitest", "Graphics/rendering test"),
            ("web-platform-tests", "web-platform-tests-reftest", "", "Graphics/rendering test"),
            ("mochitest", "mochitest-browser-media", "mochitest", "Media test"),
            ("browsertime", "raptor", "raptor", "Performance test"),
            ("test", "talos", "talos", "Performance test"),
            ("repackage-msix", "", "", "Packaging"),
            ("repackage-signing-msix", "", "", "Signing"),
            ("decision-task", "", "", "Orchestration"),
            ("mochitest", "new-unrecognized-suite", "", "Other/unknown"),
            ("", "", "", "Other/unknown"),
        ]
        for kind, suite, test_type, expected in cases:
            with self.subTest(kind=kind, suite=suite):
                self.assertEqual(tool.task_category(kind, suite, test_type), expected)

    def test_category_metadata_survives_without_a_log(self):
        task = copy.deepcopy(TASK)
        task["task"].update(tags={"kind": "test", "test-type": "talos"},
                            extra={"suite": "talos"})
        # A graphics benchmark remains a performance test, regardless of its name/pool.
        task["task"]["metadata"]["name"] = "test-talos-webgl"
        row = tool.run_row(task, {"runId": 0, "state": "pending"})
        self.assertEqual(row["category"], "Performance test")
        self.assertEqual((row["task_kind"], row["test_suite"], row["test_type"]),
                         ("test", "talos", "talos"))

    def test_url_validation(self):
        self.assertEqual(tool.revision_from(
            f"https://treeherder.mozilla.org/jobs?repo=try&revision={REVISION}"), REVISION)
        for value in ["short", f"https://example.com/?repo=try&revision={REVISION}",
                      f"https://treeherder.mozilla.org/jobs?repo=autoland&revision={REVISION}"]:
            with self.assertRaises(ValueError):
                tool.revision_from(value)

    def test_actual_metadata_and_failed_duration(self):
        with patch.object(tool, "get", return_value=log(
                '{"vm-id":"id", "instance-type":"Standard_F8alds_v7", "region":"EastUS"}')):
            row = tool.run_row(TASK, RUN)
        self.assertEqual((row["cloud_provider"], row["region"], row["vm_size"],
                          row["duration_seconds"]),
                         ("azure", "eastus", "Standard_F8alds_v7", 119.193))
        self.assertEqual(row["state"], "failed")

    def test_gcp_resource_path(self):
        with patch.object(tool, "get", return_value=log(
                '{"project-id":"project", "instance-type":"projects/1/machineTypes/c4d-standard-8-lssd",'
                '"region":"us-east1", "zone":"us-east1-c"}')):
            row = tool.run_row(TASK, RUN)
        self.assertEqual(row["vm_size"], "c4d-standard-8-lssd")
        self.assertEqual(row["cloud_provider"], "gcp")

    def test_missing_log_keeps_duration(self):
        with patch.object(tool, "get", side_effect=HTTPError("url", 404, "gone", {}, None)):
            row = tool.run_row(TASK, RUN)
        self.assertEqual(row["duration_seconds"], 119.193)
        self.assertIsNone(row["vm_size"])
        self.assertIn("Worker log unavailable", row["notes"])

    def test_aws_and_invalid_duration(self):
        with patch.object(tool, "get", return_value=log(
                '{"instance-id":"i-123", "instance-type":"m5.large", "region":"us-west-2"}')):
            self.assertEqual(tool.run_row(TASK, RUN)["cloud_provider"], "aws")
        with self.assertRaises(ValueError):
            tool.run_row(TASK, dict(RUN, resolved="2026-11-04T17:00:00Z"))

    def test_does_not_parse_task_output_as_worker_metadata(self):
        self.assertEqual(tool.worker_settings(b'{"instance-type":"fake"}'), {})
        self.assertEqual(tool.worker_settings(log('{}') + log('{"region":"fake"}')), {})

    def test_hardware_and_unfinished_runs(self):
        task = copy.deepcopy(TASK)
        task["status"]["provisionerId"] = "releng-hardware"
        with patch.object(tool, "get", return_value=log('{"generic-worker":{}}')):
            row = tool.run_row(task, RUN)
            self.assertEqual(row["infrastructure"], "physical-hardware")
            self.assertIsNone(row["vm_size"])
            row = tool.run_row(TASK, dict(RUN, resolved=None, state="running"))
            self.assertIsNone(row["duration_seconds"])
        with patch.object(tool, "get") as fetch:
            row = tool.run_row(TASK, {"runId": 0, "state": "pending"})
            fetch.assert_not_called()
            self.assertIsNone(row["duration_seconds"])

    def test_pagination(self):
        with patch.object(tool, "get_json", side_effect=[
                {"tasks": [1], "continuationToken": "a+b"}, {"tasks": [2]}]) as fetch:
            self.assertEqual(list(tool.paginated("https://example.com/list", "tasks")), [1, 2])
        self.assertIn("continuationToken=a%2Bb", fetch.call_args.args[0])

    def test_push_includes_action_tasks_and_all_attempts(self):
        task = copy.deepcopy(TASK)
        task["status"]["runs"] = [RUN, dict(RUN, runId=1, state="completed")]
        with patch.object(tool, "get_json", side_effect=[
                {"results": [{"id": 123}]}, {"taskId": "decision"},
                {"tasks": [task]}, {"results": [{"task_id": "action"}]},
                {"metadata": {"name": "action"}}, {"status": dict(task["status"], taskId="action")}]):
            tasks = tool.push_tasks(REVISION)
        self.assertEqual(len(tasks), 2)
        with patch.object(tool, "push_tasks", return_value=tasks), patch.object(tool, "get", return_value=b""):
            rows = tool.inspect(REVISION)
        self.assertEqual(len(rows), 4)
        output = io.StringIO()
        tool.write_csv(rows, output)
        self.assertIn("duration_seconds", output.getvalue())


if __name__ == "__main__":
    unittest.main()
