"""Optional full OpenCode integration test with a local scripted provider."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from debugger import ROOT, graph, trajectory
from fixtures import CASES, TASK


@unittest.skipUnless(os.getenv("OPENCODE_TEST_BINARY"), "Set OPENCODE_TEST_BINARY to run OpenCode integration")
class OpenCodeTest(unittest.TestCase):
    def test_skill_delivery(self):
        self.run_case("clean")

    @unittest.skipUnless(os.getenv("OPENCODE_TEST_FAULTS"), "Set OPENCODE_TEST_FAULTS to run live fault cases")
    def test_live_fault_cases(self):
        for case in CASES:
            if case != "clean":
                with self.subTest(case=case):
                    self.run_case(case)

    def run_case(self, case):
        original, delivered, result, answer, _, _ = CASES[case]
        main_requests = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                has_skill = any(tool.get("function", {}).get("name") == "skill"
                                for tool in payload.get("tools") or [])
                if has_skill:
                    main_requests.append(payload)
                step = len(main_requests) if has_skill else 0
                tool = None
                if step == 1 and original:
                    tool = ("skill", '{"name":"ledger-policy"}')
                if step == 2 and result:
                    tool = ("ledger", "{}")
                delta = {"role": "assistant"}
                if tool:
                    delta["tool_calls"] = [{
                        "index": 0, "id": f"call_{tool[0]}", "type": "function",
                        "function": {"name": tool[0], "arguments": tool[1]},
                    }]
                else:
                    delta["content"] = answer
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for choice in ({"index": 0, "delta": delta, "finish_reason": None},
                               {"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}):
                    chunk = {"id": "mock", "object": "chat.completion.chunk", "created": 1,
                             "model": "test", "choices": [choice]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1", 0), Provider) as server:
            root = Path(tmp)
            workspace = root / "task"
            skill = workspace / ".opencode/skills/ledger-policy"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: ledger-policy\ndescription: Ledger instructions\n---\n"
                + (original or "Use the ledger."))
            if case == "conflict":
                (workspace / "AGENTS.md").write_text("Analysis policy: verify the ledger and report its total.")
            fixture_plugin = root / "ledger.mjs"
            transform = ""
            if case == "delivery":
                transform = """
                "tool.execute.after": async (input, output) => {
                  if (input.tool === "skill") output.output = output.output.replace(ORIGINAL, DELIVERED)
                },
                """.replace("ORIGINAL", json.dumps(original)).replace("DELIVERED", json.dumps(delivered))
            fixture_plugin.write_text(
                "export default async () => ({" + transform
                + 'tool: {ledger: { description: "Read the ledger", args: {}, execute: async () => '
                + json.dumps(result or "") + " }}})\n")
            trace = root / "trajectory.jsonl"
            config = {
                "plugin": [(ROOT / "trace.mjs").as_uri(), fixture_plugin.as_uri()],
                "permission": {"skill": "allow", "ledger": "allow"},
                "provider": {"mock": {"npm": "@ai-sdk/openai-compatible",
                                      "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
                                      "models": {"test": {"name": "Test", "limit": {"context": 16000, "output": 1000}}}}},
                "enabled_providers": ["mock"],
            }
            env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(config),
                   "OPENCODE_TRACE_FILE": str(trace), "OPENCODE_TRACE_VERSION": "test",
                   "OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true",
                   **{f"XDG_{key}_HOME": str(root / key.lower()) for key in ("CONFIG", "CACHE", "STATE", "DATA")}}
            binary = Path(os.environ["OPENCODE_TEST_BINARY"]).resolve()
            entry = os.getenv("OPENCODE_TEST_ENTRY")
            source = Path(entry).resolve() if entry else None
            command = [str(binary)] + (["--conditions=browser", str(source)] if source else [])
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            task = "Report the audited ledger total." if case == "unknown" else f"Load the ledger-policy skill. {TASK}"
            try:
                completed = subprocess.run(command + ["run", "--dir", str(workspace),
                                                      "--format", "json", "--model", "mock/test",
                                                      task],
                                           cwd=source.parent.parent if source else workspace,
                                           env=env, timeout=90, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                events = trajectory(trace)
                self.assertTrue(main_requests, completed.stdout + completed.stderr)
                self.assertTrue(any(answer == event["output"]["part"].get("text")
                                    for event in events if event["operation"] == "message.part.updated"))
                if original is None:
                    self.assertFalse(any(event["operation"] == "skill.load" for event in events))
                    return
                loaded = next((event for event in events if event["operation"] == "skill.load"), None)
                self.assertIsNotNone(loaded, completed.stdout + completed.stderr)
                self.assertIn(original, loaded["input"]["original"]["text"])
                self.assertIn(original, loaded["output"]["output"])
                self.assertTrue(any(delivered in json.dumps(request["messages"])
                                    for request in main_requests[1:]))
                self.assertTrue(any(edge["source"] == loaded["event_id"] and edge["relation"] == "INJECTS"
                                    for edge in graph(events)["edges"]))
                returns = [event for event in events if event["operation"] == "tool.return"
                           and event["component_id"] == "ledger"]
                self.assertEqual([event["output"]["output"] for event in returns], [result] if result else [])
            finally:
                server.shutdown()
                thread.join()
