import { appendFileSync, readFileSync, lstatSync } from "node:fs"
import { createHash, randomUUID } from "node:crypto"
import { isAbsolute, join } from "node:path"

const secretKey = /authorization|cookie|password|passwd|secret|token$|(?:api|access|private)[_-]?key|credential|^env$/i
const secretText = /Bearer\s+[^\s"']+|(?:sk-|gh[pousr]_|AKIA)[A-Za-z0-9_-]{12,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*["']?[^\s"',;]+|-----BEGIN [^-]+PRIVATE KEY-----[\s\S]*?-----END [^-]+PRIVATE KEY-----/gi

function redact(value) {
  let count = 0
  const configured = Object.entries(process.env)
    .filter(([key, value]) => secretKey.test(key) && value && value.length >= 8)
    .map(([, value]) => value).sort((a, b) => b.length - a.length)
  function cleanString(value) {
    if (/^data:[^,]{0,256},/.test(value)) {
      count++
      return "[REDACTED DATA URI]"
    }
    for (const secret of configured) {
      value = value.replaceAll(secret, () => { count++; return "[REDACTED]" })
    }
    return value.replace(secretText, () => { count++; return "[REDACTED]" })
  }
  function visit(value) {
    if (typeof value === "string") return cleanString(value)
    if (Array.isArray(value)) return value.map(visit)
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => {
        if (secretKey.test(key)) { count++; return [cleanString(key), "[REDACTED]"] }
        return [cleanString(key), visit(item)]
      }))
    }
    return value ?? null
  }
  return { value: visit(value), count }
}

function collector(file, runId, version) {
  return ({ sessionID, type, component, operation, input, output, source, refs = [], metadata = {},
    componentVersion = version }) => {
    const eventId = randomUUID()
    try {
      const clean = redact({
        schema_version: 1, run_id: runId, session_id: sessionID ?? null,
        event_id: eventId, timestamp: new Date().toISOString(),
        component_type: type, component_id: component, component_version: componentVersion,
        operation, input_refs: refs, output_refs: [], input: input ?? null,
        output: output ?? null, metadata, observation_source: source,
      })
      clean.value.metadata.redactions = clean.count
      appendFileSync(file, JSON.stringify(clean.value) + "\n", { mode: 0o600 })
      return eventId
    } catch {
      // A collector failure must not fail an OpenCode hook.
      return undefined
    }
  }
}

function skillSource(directory) {
  if (typeof directory !== "string" || !isAbsolute(directory)) return null
  const path = join(directory, "SKILL.md")
  try {
    const stat = lstatSync(path)
    if (stat.isFile() && stat.size <= 256 * 1024) return { path, text: readFileSync(path, "utf8") }
  } catch {
    return null
  }
  return null
}

export default async function instrumentation() {
  const file = process.env.OPENCODE_TRACE_FILE
  if (!file) return {}
  const emit = collector(file, process.env.OPENCODE_TRACE_RUN_ID || randomUUID(),
    process.env.OPENCODE_TRACE_VERSION || "unknown")
  const calls = new Map()
  const results = new Map()
  const messages = new Map()
  const requests = new Map()
  const sessions = new Map()
  const ref = (id, relation) => id ? [{ event_id: id, relation, observed: true }] : []
  const hook = (source, operation, component = "opencode") => (input, output) => {
    emit({ sessionID: input.sessionID, type: "Harness", component, operation, input, output, source })
  }
  return {
    "chat.message": async (input, output) => {
      const id = emit({ sessionID: input.sessionID, type: "Harness", component: "user-input",
        operation: "user.message", input, output, source: "chat.message" })
      if (output.message.id && id) messages.set(output.message.id, id)
    },
    "experimental.chat.system.transform": hook("experimental.chat.system.transform", "context.system"),
    "experimental.chat.messages.transform": async (_, output) => {
      const sessions = new Set(output.messages.map(({ info }) => info.sessionID))
      emit({ sessionID: sessions.size === 1 ? [...sessions][0] : null, type: "Harness",
        component: "context", operation: "context.messages", output,
        refs: output.messages.flatMap(({ info, parts }) => [
          ...ref(messages.get(info.id), "INJECTS"),
          ...parts.filter(part => part.type === "tool" && part.state.status === "completed")
            .flatMap(part => ref(results.get(JSON.stringify([info.sessionID, part.callID])), "INJECTS")),
        ]),
        source: "experimental.chat.messages.transform",
        metadata: { limitation: "Hook snapshot precedes provider conversion and later hooks" } })
    },
    "chat.params": async (input, output) => {
      emit({ sessionID: input.sessionID, type: "Harness", component: "model-request",
        operation: "model.params", input: { agent: input.agent, model: input.model.id, messageID: input.message.id },
        output: { temperature: output.temperature, maxOutputTokens: output.maxOutputTokens },
        refs: ref(messages.get(input.message.id), "DERIVES_FROM"), source: "chat.params" })
    },
    "experimental.session.compacting": hook("experimental.session.compacting", "context.compacting"),
    "tool.definition": async (input, output) => {
      emit({ type: "Tool", component: input.toolID, operation: "tool.definition",
        input, output, source: "tool.definition" })
    },
    "shell.env": async (input, output) => {
      emit({ sessionID: input.sessionID, type: "Harness", component: "shell",
        operation: "shell.env", input,
        output: { override_count: Object.keys(output.env).length },
        refs: ref(calls.get(JSON.stringify([input.sessionID, input.callID])), "DERIVES_FROM"),
        source: "shell.env", metadata: { limitation: "Environment values omitted" } })
    },
    "tool.execute.before": async (input, output) => {
      const id = emit({ sessionID: input.sessionID, type: "Tool", component: input.tool,
        operation: "tool.invoke", input: { ...input, args: output.args }, source: "tool.execute.before" })
      if (id && input.callID) calls.set(JSON.stringify([input.sessionID, input.callID]), id)
    },
    "tool.execute.after": async (input, output) => {
      const key = JSON.stringify([input.sessionID, input.callID])
      const isSkill = input.tool === "skill"
      const original = isSkill ? skillSource(output.metadata?.dir) : null
      const id = emit({ sessionID: input.sessionID, type: isSkill ? "Skill" : "Tool",
        component: isSkill ? String(input.args?.name ?? "skill") : input.tool,
        componentVersion: isSkill ? (original
          ? "sha256:" + createHash("sha256").update(original.text).digest("hex")
          : "unknown") : undefined,
        operation: isSkill ? "skill.load" : "tool.return",
        input: isSkill ? { ...input, original } : input, output,
        metadata: isSkill ? { original_source: original ? "disk_after_load" : "unavailable" } : {},
        refs: ref(calls.get(key), isSkill ? "LOADS" : "RETURNS"), source: "tool.execute.after" })
      if (id && input.callID) results.set(key, id)
      calls.delete(key)
    },
    event: async ({ event }) => {
      const { type, properties: p } = event
      if (!/^(session\.|permission\.|message\.updated$|message\.part\.updated$|file\.)/.test(type)) return
      const payload = p.part ?? p.info ?? p
      const sessionID = p.sessionID ?? payload.sessionID ?? (type.startsWith("session.") ? p.info?.id : undefined)
      const lookup = JSON.stringify([sessionID, p.requestID ?? p.id])
      const assistant = type === "message.updated" && payload.role === "assistant"
      const model = assistant || (type === "message.part.updated" && ["text", "reasoning", "step-finish"].includes(payload.type))
      const id = emit({ sessionID, type: model ? "Model" : "Harness",
        component: model ? (payload.modelID ?? "assistant-output") : "opencode",
        operation: type, output: p, source: "event",
        refs: [
          ...ref(messages.get(assistant ? payload.parentID : payload.messageID), "DERIVES_FROM"),
          ...ref(payload.type === "tool" ? results.get(JSON.stringify([sessionID, payload.callID])) : undefined, "RETURNS"),
          ...ref(type === "permission.asked" && p.tool?.callID
            ? calls.get(JSON.stringify([sessionID, p.tool.callID])) : undefined, "DERIVES_FROM"),
          ...ref(type === "permission.replied" ? requests.get(lookup) : undefined, "DERIVES_FROM"),
          ...ref(type === "session.created" ? sessions.get(payload.parentID) : undefined, "DERIVES_FROM"),
        ] })
      if (id && assistant) messages.set(payload.id, id)
      if (id && type === "session.created" && sessionID) sessions.set(sessionID, id)
      if (id && type === "permission.asked") requests.set(lookup, id)
      if (type === "permission.replied") requests.delete(lookup)
    },
  }
}
