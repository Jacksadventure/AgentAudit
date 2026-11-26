import { test, expect } from "bun:test"
import { mkdtempSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import instrumentation from "./trace.mjs"

test("captures matched tool calls without changing hook payloads", async () => {
  const root = mkdtempSync(join(tmpdir(), "opencode-trace-"))
  const file = join(root, "trace.jsonl")
  process.env.OPENCODE_TRACE_FILE = file
  try {
    const hooks = await instrumentation()
    const call = { sessionID: "s1", callID: "c1", tool: "read" }
    const input = { args: { password: "sensitive", filePath: "ledger.txt" } }
    const output = { output: "total: 42" }
    await hooks["tool.execute.before"](call, input)
    await hooks["tool.execute.after"](call, output)
    await hooks["experimental.chat.messages.transform"]({}, { messages: [{
      info: { id: "m1", sessionID: "s1" },
      parts: [{ type: "tool", callID: "c1", state: { status: "completed", output: "total: 42" } }],
    }] })
    const lines = readFileSync(file, "utf8")
    const [before, after, context] = lines.trim().split("\n").map(JSON.parse)
    expect(lines).not.toContain("sensitive")
    expect(input.args.password).toBe("sensitive")
    expect(before.metadata.redactions).toBe(1)
    expect(after.input_refs).toEqual([{ event_id: before.event_id, relation: "RETURNS", observed: true }])
    expect(context.input_refs).toEqual([{ event_id: after.event_id, relation: "INJECTS", observed: true }])
  } finally {
    delete process.env.OPENCODE_TRACE_FILE
    rmSync(root, { recursive: true })
  }
})
