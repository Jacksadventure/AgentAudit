# OpenCode component debugger

A passive OpenCode plugin captures a JSONL trajectory. An independent Python CLI builds the dependency graph and audits a copy of the evidence with a separate model endpoint. The prototype supports Skill, Harness, Model and Tool findings.

## Reproduce locally

Run from the repository root with Python 3.10+ and Bun. The debugger uses the Python standard library.

```sh
python -m unittest discover -s customed_opencode -p 'test_*.py' -v
bun test customed_opencode/trace.test.mjs
python customed_opencode/fixtures.py customed_opencode/out/fixtures
python customed_opencode/debugger.py graph customed_opencode/out/fixtures/inputs/case-01.jsonl \
  --focus e4 --out customed_opencode/out/delivery-graph.json
```

The generator creates nine deterministic traces and task context files under `inputs/`. Scoring labels live under `truth/`. The evaluator reads labels locally after the model returns. Individual reports, graphs and evaluation results use new files so an output path cannot overwrite the original trajectory.

Run the same scenarios as isolated OpenCode integration tests using a local scripted provider. The test checks skill source, tool results, model output, and what the provider actually received. Fault injection is confined to the test workload.

```sh
OPENCODE_TEST_BINARY=bun \
OPENCODE_TEST_ENTRY=/path/to/opencode/packages/opencode/src/index.ts \
OPENCODE_TEST_FAULTS=1 python -m unittest discover -s customed_opencode -v
```

## Capture an OpenCode run

The inspected source is the vendored OpenCode commit `23fb5e0516c99ac04a1aa46c193efda2e1b9bb24`. Its package declares 1.14.18 and its source CLI reports `local`. The plugin uses the V1 hook API. Register `trace.mjs` as a local plugin with `"plugin": ["file:///absolute/path/to/customed_opencode/trace.mjs"]` in the pinned version. Set `OPENCODE_TRACE_FILE` to an absolute path in a private directory outside the task workspace and `OPENCODE_TRACE_VERSION` to the release or checkout identifier. The plugin is inactive without `OPENCODE_TRACE_FILE`. Regular OpenCode execution, tool and approval behavior are unchanged by hook return values.

With an installed OpenCode binary and a configured provider, the paired runner creates fresh directories and alternating run order for an isolated skill-and-ledger task. Configure provider credentials via the environment and pass the model as `provider/model`.

```sh
python customed_opencode/live.py --model "$OPENCODE_MODEL" --pairs 3 \
  --out customed_opencode/out/live
```

The runner saves the original CLI JSON stream, the plugin trajectory for instrumented runs, and paired wall-time deltas and trace sizes in `overhead.json`. The raw CLI stream may contain sensitive values. The debugger's raw mode instead uses a transcript projection from the redacted JSONL, with event IDs retained for scoring. For existing Harness2 streams and verified bridge references, export the legacy links as inferred evidence without adding edges to the runtime graph.

```sh
python customed_opencode/debugger.py trace-links raw.jsonl \
  --harness path/to/harness --refs refs.jsonl --out legacy-links.json
```

## Audit and evaluate

Use a separately configured chat-completions compatible endpoint supporting JSON object output. Set `DEBUGGER_ENDPOINT` to its API base, and `DEBUGGER_API_KEY` if required. Provide the supplied Component-Aware Agent Debugger prompt with `--prompt`; the bundled prompt is a local substitute. Review trajectory, task, component and environment evidence before sending them to an endpoint.

```sh
python customed_opencode/debugger.py audit run.jsonl --task task.txt \
  --components components.json --environment environment.json \
  --mode graph --model "$DEBUGGER_MODEL" --out audit.json

python customed_opencode/debugger.py evaluate customed_opencode/out/fixtures \
  --model "$DEBUGGER_MODEL" --out customed_opencode/out/evaluation
```

All modes use the same system prompt, model, temperature and maximum output budget. Raw exposes transcript events. Instrumented adds hook snapshots and provenance. Graph adds explicit adjacency and optional backward-focus results to the same instrumented events. Compare raw with instrumented for the information effect and instrumented with graph for the graph presentation and traversal effect. Actual input length varies by mode and is included in token metrics when reported by the provider. Fixture focus is the final output event, independent of fault labels.

Metrics include micro failure detection precision, recall and F1, exact component and fault-event set match on failure cases, micro evidence precision against fixture annotations, clean false positives, abstention on incomplete traces and token consumption. An undefined denominator is reported as JSON null. The controlled evaluation is synthetic; the live runner measures skill/task capture and paired overhead.

## Evidence contracts and limitations

`event-v1.schema.json` describes individual JSONL records. An input reference points from an upstream event to the current event. An output reference points from the current event to a downstream event. Each edge carries a relationship and an explicit observed flag. Missing or cross-run targets are listed as unresolved and excluded from traversal. The graph retains event IDs and the source JSONL remains available. `audit.schema.json` describes the nested report in the audit artifact. The CLI additionally checks citations against the events actually shown in each mode.

| Surface | Evidence available |
| --- | --- |
| Instructions | `chat.message` gives the incoming message and parts at hook time. System and message transform hooks snapshot assembled context at their position in the hook chain. Individual system instruction file provenance and original user request bytes may be unavailable. |
| Skills | System and tool-description hook snapshots can contain the advertised skill list. A matching tool call shows selection. The after-hook gives the wrapped result and, for a bounded regular SKILL.md resolved from built-in tool metadata, the disk contents sampled after execution. Loader cache contents and discovery order are unavailable. Context tool parts show later delivery at the message transform hook. |
| Context and model | System, message and parameter hooks observe assembly. Compaction hooks and session events show some compaction activity. Assistant message and part events show stored outputs and available usage. Exact provider requests, internal reasoning and provider-side truncation are unavailable. Stored reasoning parts are only provider-exposed data. |
| Tools | Before/after hooks and persisted tool parts carry call IDs. An after-hook can be missing for a tool failure. A result at the hook may be altered later. |
| Permissions | Asked and replied bus events capture explicit prompts and replies. Automatic rule resolutions may leave no event. |
| Sessions and subagents | Session events expose IDs and available parent IDs. Task tool output can expose child session IDs. Asynchronous event delivery may reorder observation and omit links when the upstream event has not been observed yet. |
| Files and environment | File events and session diffs expose some changes. The shell hook records only the count of environment overrides at sampling time. Shell side effects, process environment mutations and external system changes require independent evidence. |

The collector redacts common secret-bearing keys, token patterns, private keys and known secret environment values. It records replacement counts. Arbitrary sensitive prose, unusual credentials and binary attachments can escape heuristic redaction. Model snapshots can contain source code and user data. Traces are created with mode 0600, and existing files retain their permissions. Hooks tolerate write failures, which can make traces incomplete. Use one process per capture file and a writable private destination. Prefer isolated workspaces and reviewed evidence when running the debugger.

The hook and event boundaries were checked in the pinned `packages/plugin/src/index.ts` and `packages/opencode/src/{session,skill,tool,permission,plugin}` trees. The instance session routes expose persisted messages and diffs for retrospective inspection. This collector uses the event bus and hooks so it can observe intermediate snapshots while the run is in progress. The current plugin documentation describes a newer plugin configuration format. [OpenCode plugin documentation](https://opencode.ai/docs/plugins/)
