import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import debugger
from fixtures import generate


class PipelineTest(unittest.TestCase):
    def test_fixtures_and_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fixtures"
            generate(root)
            files = list((root / "inputs").glob("*.jsonl"))
            self.assertEqual(len(files), 9)
            events = debugger.trajectory(root / "inputs/case-01.jsonl")
            dependencies = debugger.graph(events)
            self.assertEqual(debugger.upstream(dependencies, ["e4"]), ["e0", "e1", "e2", "e3", "e4"])
            raw = debugger.payload(events, "task", "raw")
            instrumented = debugger.payload(events, "task", "instrumented")
            guided = debugger.payload(events, "task", "graph")
            self.assertEqual(instrumented["trajectory"], guided["trajectory"])
            self.assertNotIn("e2", {event["event_id"] for event in raw["trajectory"]})
            self.assertEqual(dependencies["unresolved"], [])

    def test_http_round_trip_and_citation_validation(self):
        report = {"status": "failure", "summary": "Incorrect output", "findings": [{
            "component_type": "Model", "component_id": "assistant-output", "fault_event_id": "e4",
            "evidence_event_ids": ["e4"], "explanation": "The observed answer was incorrect.",
        }]}
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": json.dumps(report)}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                }).encode())

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                root = Path(tmp) / "fixtures"
                generate(root)
                events = debugger.trajectory(root / "inputs/case-02.jsonl")
                args = SimpleNamespace(endpoint=f"http://127.0.0.1:{server.server_port}/v1",
                                       model="mock", max_tokens=100, prompt=debugger.ROOT / "system_prompt.md")
                for mode in debugger.MODES:
                    result = debugger.analyze(args, events, "task", mode)
                    self.assertEqual(result["report"], report)
                self.assertEqual({(r["model"], r["max_tokens"]) for r in requests}, {("mock", 100)})
                self.assertTrue(all("truth" not in r["messages"][1]["content"] for r in requests))
                truth = debugger.read_json(root / "truth/case-02.json")
                metrics = debugger.score([(result, truth)])
                self.assertEqual(metrics["component_exact_match"], 1)
                self.assertEqual(metrics["fault_event_exact_match"], 1)
                report["findings"][0]["evidence_event_ids"].append("absent")
                with self.assertRaises(ValueError):
                    debugger.analyze(args, events, "task", "graph")
                report.update(status="clean", findings=[], summary="Mock response")
                requests.clear()
                output = Path(tmp) / "evaluation"
                with patch("sys.argv", ["debugger", "evaluate", str(root), "--out", str(output),
                                       "--model", args.model, "--endpoint", args.endpoint]):
                    debugger.main()
                self.assertEqual(len(requests), 27)
                self.assertEqual(len(list(output.glob("*.json"))), 28)
                self.assertTrue(all(set(json.loads(r["messages"][1]["content"])) <= {
                    "task", "components", "environment", "trajectory", "graph", "focus"
                } for r in requests))
                metrics = debugger.read_json(output / "metrics.json")
                self.assertEqual(metrics["modes"]["graph"]["tokens"]["prompt_tokens"]["total"], 450)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
