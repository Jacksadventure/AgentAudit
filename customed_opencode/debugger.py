"""Standalone OpenCode trace auditor and evaluation runner."""
import argparse
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODES = ("raw", "instrumented", "graph")
COMPONENTS = {"Skill", "Harness", "Model", "Tool"}
RELATIONS = {"LOADS", "INJECTS", "TRANSFORMS", "RETRIEVES", "INVOKES", "RETURNS", "WRITES", "DERIVES_FROM"}
EVENT_KEYS = set("schema_version run_id session_id event_id timestamp component_type component_id component_version operation input_refs output_refs input output metadata observation_source".split())
TEXT_FIELDS = {"run_id", "event_id", "timestamp", "component_type", "component_id",
               "component_version", "operation", "observation_source"}


def read_json(path):
    return json.loads(Path(path).read_text())


def trajectory(path):
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    ids = set()
    for event in events:
        if (not isinstance(event, dict) or set(event) != EVENT_KEYS
                or type(event["schema_version"]) is not int or event["schema_version"] != 1
                or any(not isinstance(event[key], str) or not event[key] for key in TEXT_FIELDS)
                or event["component_type"] not in COMPONENTS
                or not isinstance(event["session_id"], (str, type(None)))
                or not isinstance(event["metadata"], dict)):
            raise ValueError("Invalid event")
        if datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Timestamp must include a time zone")
        for field in ("input_refs", "output_refs"):
            if not isinstance(event[field], list):
                raise ValueError("Invalid event references")
            for ref in event[field]:
                if (not isinstance(ref, dict) or set(ref) != {"event_id", "relation", "observed"}
                        or not isinstance(ref["event_id"], str) or not ref["event_id"]
                        or not isinstance(ref["relation"], str) or ref["relation"] not in RELATIONS
                        or type(ref["observed"]) is not bool):
                    raise ValueError("Invalid event reference")
        if event["event_id"] in ids:
            raise ValueError("Duplicate event ID")
        ids.add(event["event_id"])
    return events


def graph(events):
    ids = {event["event_id"]: event["run_id"] for event in events}
    edges, unresolved = [], []
    seen = set()
    for event in events:
        current = event["event_id"]
        for field in ("input_refs", "output_refs"):
            for ref in event[field]:
                other = ref["event_id"]
                if ref["relation"] not in RELATIONS or type(ref["observed"]) is not bool:
                    raise ValueError("Invalid reference")
                source, target = (other, current) if field == "input_refs" else (current, other)
                edge = {"source": source, "target": target, "relation": ref["relation"],
                        "observed": ref["observed"]}
                destination = edges if other in ids and ids[other] == ids[current] else unresolved
                key = (source, target, ref["relation"], ref["observed"])
                if key not in seen:
                    destination.append(edge)
                    seen.add(key)
    return {"nodes": list(ids), "edges": edges, "unresolved": unresolved}


def upstream(dependencies, seeds):
    incoming = {}
    for edge in dependencies["edges"]:
        incoming.setdefault(edge["target"], []).append(edge["source"])
    seen = set(seeds)
    pending = list(seeds)
    while pending:
        for parent in incoming.get(pending.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return [node for node in dependencies["nodes"] if node in seen]


def payload(events, task, mode, components=None, environment=None, seeds=()):
    if mode not in MODES:
        raise ValueError("Unknown analysis mode")
    data = {"task": task, "components": components, "environment": environment}
    if mode == "raw":
        visible = {"user.message", "tool.invoke", "tool.return", "skill.load", "message.part.updated"}
        data["trajectory"] = [
            {"event_id": event["event_id"], "operation": event["operation"],
             "input": None if event["operation"] == "skill.load" else event["input"],
             "output": event["output"]}
            for event in events if event["operation"] in visible
        ]
    else:
        data["trajectory"] = events
    if mode == "graph":
        dependencies = graph(events)
        if not set(seeds) <= set(dependencies["nodes"]):
            raise ValueError("Unknown focus event")
        data["graph"] = dependencies
        data["focus"] = {"selected": list(seeds), "ancestors_including_focus": upstream(dependencies, seeds)}
    return data


def validate(report, supplied):
    if not isinstance(report, dict) or set(report) != {"status", "findings", "summary"}:
        raise ValueError("Invalid audit report keys")
    if report["status"] not in ("failure", "clean", "insufficient_evidence"):
        raise ValueError("Invalid audit status")
    if not isinstance(report["summary"], str) or not isinstance(report["findings"], list):
        raise ValueError("Invalid audit summary or findings")
    if bool(report["findings"]) != (report["status"] == "failure"):
        raise ValueError("Only failures have findings")
    for finding in report["findings"]:
        if not isinstance(finding, dict) or set(finding) != {
            "component_type", "component_id", "fault_event_id", "evidence_event_ids", "explanation"
        }:
            raise ValueError("Invalid finding keys")
        if not isinstance(finding["component_type"], str) or finding["component_type"] not in COMPONENTS or any(
            not isinstance(finding[key], str) or not finding[key]
            for key in ("component_id", "fault_event_id", "explanation")
        ):
            raise ValueError("Invalid finding")
        evidence = finding["evidence_event_ids"]
        if not isinstance(evidence, list) or not evidence or any(
            not isinstance(item, str) or item not in supplied for item in evidence
        ) or finding["fault_event_id"] not in evidence:
            raise ValueError("Unknown or missing evidence ID")
    return report


def analyze(args, events, task, mode, components=None, environment=None, seeds=()):
    data = payload(events, task, mode, components, environment, seeds)
    request = urllib.request.Request(
        args.endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": args.model, "temperature": 0, "max_tokens": args.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": Path(args.prompt).read_text()},
                         {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
        }).encode(),
        headers={"Content-Type": "application/json", **(
            {"Authorization": "Bearer " + os.environ["DEBUGGER_API_KEY"]}
            if os.environ.get("DEBUGGER_API_KEY") else {}
        )},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    report = validate(json.loads(result["choices"][0]["message"]["content"]),
                      {event["event_id"] for event in data["trajectory"]})
    return {"mode": mode, "model": args.model, "usage": result.get("usage"), "report": report}


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def ratio(a, b):
    return a / b if b else None


def score(results):
    tp = fp = fn = clean = clean_fp = unknown = abstained = 0
    cases = component_hits = event_hits = cited = relevant = 0
    usage = {"prompt_tokens": [], "completion_tokens": []}
    for audit, truth in results:
        report = audit["report"]
        actual, expected = report["status"], truth["status"]
        tp += actual == expected == "failure"
        fp += actual == "failure" and expected != "failure"
        fn += actual != "failure" and expected == "failure"
        clean += expected == "clean"
        clean_fp += expected == "clean" and actual == "failure"
        unknown += expected == "insufficient_evidence"
        abstained += expected == actual == "insufficient_evidence"
        findings = report["findings"]
        if expected == "failure":
            cases += 1
            component_hits += {(f["component_type"], f["component_id"]) for f in findings} == {
                tuple(component) for component in truth["components"]}
            event_hits += {f["fault_event_id"] for f in findings} == set(truth["fault_events"])
        ids = {item for finding in findings for item in finding["evidence_event_ids"]}
        cited += len(ids)
        relevant += len(ids & set(truth.get("evidence", [])))
        for key, values in usage.items():
            value = (audit.get("usage") or {}).get(key)
            if isinstance(value, int):
                values.append(value)
    return {
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "component_exact_match": ratio(component_hits, cases),
        "fault_event_exact_match": ratio(event_hits, cases),
        "evidence_precision": ratio(relevant, cited), "clean_false_positive_rate": ratio(clean_fp, clean),
        "appropriate_abstention": ratio(abstained, unknown),
        "tokens": {key: {"total": sum(values) if values else None, "reported_cases": len(values)}
                   for key, values in usage.items()},
    }


def metric_deltas(before, after):
    return {key: after[key] - value for key, value in before.items()
            if isinstance(value, (int, float)) and isinstance(after[key], (int, float))}


def add_model_options(parser):
    parser.add_argument("--endpoint", default=os.getenv("DEBUGGER_ENDPOINT", "http://localhost:8000/v1"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=2500)
    parser.add_argument("--prompt", default=ROOT / "system_prompt.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    audit = subs.add_parser("audit")
    audit.add_argument("trajectory")
    audit.add_argument("--task", required=True, help="Task description file")
    audit.add_argument("--components")
    audit.add_argument("--environment")
    audit.add_argument("--mode", choices=MODES, default="graph")
    audit.add_argument("--focus", action="append", default=[])
    audit.add_argument("--out", required=True)
    add_model_options(audit)
    build = subs.add_parser("graph")
    build.add_argument("trajectory")
    build.add_argument("--focus", action="append", default=[])
    build.add_argument("--out", required=True)
    links = subs.add_parser("trace-links", help="Reuse Harness2 Trace Links on an OpenCode JSON stream")
    links.add_argument("stream", type=Path)
    links.add_argument("--harness", type=Path, required=True)
    links.add_argument("--refs", type=Path, action="append", default=[])
    links.add_argument("--out", required=True)
    evaluation = subs.add_parser("evaluate")
    evaluation.add_argument("fixtures", type=Path)
    evaluation.add_argument("--out", type=Path, required=True)
    add_model_options(evaluation)
    args = parser.parse_args()
    if args.command == "trace-links":
        from harness2.core.trace_links import RefsIndex, link_rollout
        rollout = link_rollout(args.stream.stem, args.stream, args.harness, RefsIndex(args.refs))
        write_new(args.out, [
            {"call_id": link.action.call_id, "kind": link.kind, "file": link.file,
             "first_line": link.first, "last_line": link.last, "quote": link.quote,
             "observed": False}
            for link in rollout.links
        ])
        return
    if args.command == "evaluate":
        args.out.mkdir(parents=True, exist_ok=False)
        summary = {}
        for mode in MODES:
            results = []
            for path in sorted((args.fixtures / "inputs").glob("*.jsonl")):
                context = read_json(path.with_suffix(".json"))
                report = analyze(args, trajectory(path), context["task"], mode,
                                 context.get("components"), context.get("environment"), context.get("focus", []))
                write_new(args.out / f"{mode}-{path.stem}.json", report)
                results.append((report, read_json(args.fixtures / "truth" / path.with_suffix(".json").name)))
            if not results:
                raise ValueError("No fixture trajectories")
            summary[mode] = score(results)
        write_new(args.out / "metrics.json", {
            "modes": summary,
            "deltas": {
                "instrumented_minus_raw": metric_deltas(summary["raw"], summary["instrumented"]),
                "graph_minus_instrumented": metric_deltas(summary["instrumented"], summary["graph"]),
            },
        })
        return
    events = trajectory(args.trajectory)
    if args.command == "graph":
        result = graph(events)
        if not set(args.focus) <= set(result["nodes"]):
            parser.error("Unknown focus event")
        result["upstream"] = upstream(result, args.focus)
        result["trajectory_path"] = str(Path(args.trajectory).resolve())
    else:
        result = analyze(args, events, Path(args.task).read_text(), args.mode,
                         read_json(args.components) if args.components else None,
                         read_json(args.environment) if args.environment else None, args.focus)
    write_new(args.out, result)


if __name__ == "__main__":
    main()
