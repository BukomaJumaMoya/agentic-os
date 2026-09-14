import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { Type } from "typebox";
import http from "node:http";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// dist/index.js -> plugin root -> tools/ -> repo root
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const TELEGRAM_COMMANDS = path.join(REPO_ROOT, "orchestrator", "telegram_commands.py");
const PYTHON = process.env.AGENTIC_PYTHON || "python";

/**
 * Run the approval-command parser with a JSON payload on stdin.
 * The parser owns authorisation: it compares the sender id against the
 * configured allowlist and fails closed when that is unset.
 */
function runTelegramCommand(userId, text, timeoutMs = 30_000) {
  return new Promise((resolve) => {
    let proc;
    try {
      proc = spawn(PYTHON, [TELEGRAM_COMMANDS], {
        stdio: ["pipe", "pipe", "pipe"],
        cwd: REPO_ROOT,
      });
    } catch (error) {
      resolve({ ok: false, error: `spawn failed: ${error?.message ?? error}` });
      return;
    }

    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    const timer = setTimeout(() => {
      proc.kill("SIGTERM");
      finish({ ok: false, error: `telegram_commands timed out after ${timeoutMs}ms` });
    }, timeoutMs);

    proc.stdout.on("data", (c) => { stdout += c.toString(); });
    proc.stderr.on("data", (c) => { stderr += c.toString(); });
    proc.on("error", (error) => finish({ ok: false, error: `spawn failed: ${error?.message ?? error}` }));
    proc.on("close", () => {
      const text = stdout.trim();
      if (!text) {
        finish({ ok: false, error: stderr.trim() || "no output from telegram_commands" });
        return;
      }
      try {
        finish(JSON.parse(text));
      } catch {
        finish({ ok: false, error: `non-JSON output: ${text.slice(0, 500)}` });
      }
    });

    proc.stdin.write(JSON.stringify({ user_id: userId, text }));
    proc.stdin.end();
  });
}

/** Wrap a plain payload in the AgentToolResult {content, details} envelope. */
function toolResult(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: payload,
  };
}

function normalizeSenderId(value) {
  if (value === undefined || value === null) return null;
  const s = String(value).trim();
  return s || null;
}

// Slash commands for the approval pipeline.
//
// Names are prefixed because the bare verbs are unavailable:
//   - "approve" is a built-in (OpenClaw's own exec/plugin approval, matched by
//     /^\/?approve(?:\s|$)/i) but is NOT in the plugin reserved-name set, so
//     registering it would succeed and then be silently shadowed at dispatch.
//   - "status" is both a built-in and reserved; registering it fails outright.
// Underscores, not hyphens: normalizeTelegramCommandName() rewrites "-" to "_"
// when building the Telegram command menu, so a hyphenated name would register
// under one key and appear in Telegram under another.
//
// `verb` is what orchestrator/telegram_commands.py parses; `name` is what the
// user types.
const APPROVAL_COMMANDS = [
  { name: "apr_approve", verb: "APPROVE", acceptsArgs: true,  description: "Approve a pending approval request by id." },
  { name: "apr_reject",  verb: "REJECT",  acceptsArgs: true,  description: "Reject a pending approval request by id." },
  { name: "apr_status",  verb: "STATUS",  acceptsArgs: true,  description: "Show the status of an approval request by id." },
  { name: "apr_resume",  verb: "RESUME",  acceptsArgs: true,  description: "Resume an approved external action by request id." },
  { name: "apr_list",    verb: "LIST",    acceptsArgs: false, description: "List pending approval requests." },
  { name: "apr_jobs",    verb: "JOBS",    acceptsArgs: true,  description: "List recent workflow jobs and their status." },
  { name: "apr_cancel",  verb: "CANCEL",  acceptsArgs: true,  description: "Cancel a running job by id (a short prefix is enough)." },
  { name: "apr_enquiry", verb: "ENQUIRY", acceptsArgs: true,  description: "Draft a proposal for a client enquiry." },
];

/**
 * Build one slash-command definition.
 *
 * These bypass the model entirely: the host matches the command before the
 * agent is invoked, and returning without `continueAgent` ends the turn here.
 * Authorisation is checked twice -- ctx.isAuthorizedSender (host, from the
 * channel allowlist) and again inside telegram_commands.py against the
 * configured allowlist.
 */
function buildApprovalCommand(def) {
  return {
    name: def.name,
    description: def.description,
    acceptsArgs: def.acceptsArgs,
    requireAuth: true,
    channels: ["telegram"],
    async handler(ctx) {
      if (!ctx?.isAuthorizedSender) {
        return { text: JSON.stringify({ ok: false, error: "not_allowed" }, null, 2) };
      }
      const senderId = normalizeSenderId(ctx?.senderId);
      if (!senderId) {
        return {
          text: JSON.stringify({
            ok: false,
            error: "no_verified_sender",
            detail:
              "The command context carried no senderId; refusing to act on " +
              "an unattributed approval command.",
          }, null, 2),
        };
      }
      const args = def.acceptsArgs ? String(ctx?.args ?? "").trim() : "";
      const text = args ? `${def.verb} ${args}` : def.verb;
      try {
        const result = await runTelegramCommand(senderId, text);
        // The bridge returns structured JSON. A few verbs are read by a person
        // on a phone rather than by a program, so they get a plain reply.
        const rendered = renderForHumans(def.verb, result);
        return { text: rendered ?? JSON.stringify(result, null, 2) };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return {
          text: JSON.stringify({ ok: false, error: `/${def.name} failed: ${message}` }, null, 2),
        };
      }
    },
  };
}

/**
 * Plain-text replies for the verbs a person reads on a phone.
 *
 * Returns null for anything else, so the JSON envelope stays the default and a
 * new verb is never silently rendered as an empty string.
 */
function renderForHumans(verb, result) {
  if (!result || result.ok === false) {
    if (verb === "CANCEL" && result && result.outcome) {
      const explain = {
        not_found: "No job with that id.",
        already_finished: "That job already finished (" + result.status + ").",
        too_late_already_executed:
          "Too late - the approval was already used by a completed action. " +
          "That cannot be undone.",
        ambiguous_job_id:
          "More than one job starts with that: " +
          (result.matches || []).join(", ") + ". Send more characters.",
      }[result.outcome];
      if (explain) return explain;
    }
    if (verb === "ENQUIRY" && result && result.error === "enquiry_too_long") {
      return (
        "That enquiry is " + result.length + " characters, above the " +
        result.limit + " limit." + NEWLINE + NEWLINE +
        "Refusing rather than truncating, so none of it is silently lost. " +
        "Send a shorter version."
      );
    }
    return null;
  }

  if (verb === "JOBS") {
    const jobs = result.jobs || [];
    if (!jobs.length) return "No jobs yet.";
    const lines = jobs.map((j) => {
      const bits = [j.job + "  " + j.workflow + "  " + j.status + "  " + j.age + " ago"];
      if (j.at) bits.push("      at " + j.at + (j.cancelling ? " (cancelling)" : ""));
      if (j.request) bits.push("      request " + j.request);
      return bits.join(NEWLINE);
    });
    const more = result.shown >= result.limit
      ? NEWLINE + NEWLINE + "Showing the " + result.limit + " most recent."
      : "";
    return "Recent jobs:" + NEWLINE + NEWLINE + lines.join(NEWLINE) + more;
  }

  if (verb === "CANCEL" && result.outcome === "cancelling") {
    return (
      "Cancelling job " + String(result.job_id).slice(0, 8) + "." +
      NEWLINE + NEWLINE +
      "It stops at its next checkpoint; an agent already running is not " +
      "interrupted, so this can take a minute."
    );
  }

  if (verb === "ENQUIRY" && result.status === "queued") {
    return (
      "Working on it.  Job " + result.short_id + NEWLINE + NEWLINE +
      "Enquiry: " + result.preview + NEWLINE +
      "Length: " + result.chars + " characters" + NEWLINE + NEWLINE +
      "Drafting a proposal now - this takes around half a minute. An approval " +
      "prompt will follow, or a message saying why none came." +
      NEWLINE + NEWLINE +
      "Stop it with:  /apr_cancel " + result.short_id
    );
  }
  return null;
}

const TELEGRAM_APPROVAL_DESCRIPTION =
  "REQUIRED handler for Telegram approval replies. Call this tool " +
  "whenever an incoming message matches 'APPROVE <id>', 'REJECT <id>', " +
  "'STATUS <id>', 'RESUME <id>' or 'LIST' (case-insensitive), where " +
  "<id> is a UUID taken from an approval prompt. Pass the raw message " +
  "text through unchanged. This is the only correct handler for those " +
  "messages: do not route them to skill, workshop or proposal tools, " +
  "even though the UUID resembles a proposal id. Authorisation is " +
  "enforced by the orchestrator against the configured allowlist.";

const TELEGRAM_APPROVAL_PARAMETERS = Type.Object({
  text: Type.String({
    description:
      "The raw command text exactly as the user sent it, e.g. " +
      "'APPROVE 123e4567-e89b-12d3-a456-426614174000'.",
    maxLength: 500,
  }),
});

const NEWLINE = "\n";

const DEFAULT_ROUTER_HOST = "127.0.0.1";
const DEFAULT_ROUTER_PORT = 18790;
const DEFAULT_TIMEOUT_MS = 90_000;

function request(host, port, path, body, timeoutMs = DEFAULT_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = http.request(
      {
        hostname: host,
        port,
        path,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf-8");
          resolve({ status: res.statusCode, headers: res.headers, body: text });
        });
        res.on("error", reject);
      }
    );

    req.on("error", reject);

    const timer = setTimeout(() => {
      reject(new Error(`Router request timed out after ${timeoutMs}ms`));
      req.destroy();
    }, timeoutMs);

    req.on("response", () => clearTimeout(timer));

    req.write(payload);
    req.end();
  });
}

async function invokeHermesViaRouter(message) {
  const host = process.env.OPENCLAW_HERMES_ROUTER_HOST || DEFAULT_ROUTER_HOST;
  const port = parseInt(process.env.OPENCLAW_HERMES_ROUTER_PORT || String(DEFAULT_ROUTER_PORT), 10);
  const path = "/invoke";

  let lastError = null;

  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const { status, body } = await request(host, port, path, { message });

      if (status !== 200) {
        lastError = `Router returned HTTP ${status}: ${body.slice(0, 500)}`;
        continue;
      }

      let data;
      try {
        data = JSON.parse(body);
      } catch {
        lastError = `Router returned non-JSON response: ${body.slice(0, 500)}`;
        continue;
      }

      if (typeof data.ok !== "boolean") {
        lastError = `Router returned non-structured response: ${JSON.stringify(data).slice(0, 500)}`;
        continue;
      }

      return data;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      lastError = `Router request failed: ${reason}`;
    }
  }

  return {
    ok: false,
    reply: "",
    stderr: lastError || "Hermes router unavailable",
    returnCode: -1,
  };
}

const pluginEntry = defineToolPlugin({
  id: "openclaw-hermes-router",
  name: "OpenClaw Hermes Router",
  description: "Route OpenClaw messages to Hermes via the local Hermes HTTP router.",
  configSchema: Type.Object(
    {
      host: Type.Optional(Type.String({ description: "Router bind host" })),
      port: Type.Optional(Type.Number({ description: "Router bind port" })),
    },
    { additionalProperties: false }
  ),
  tools: (tool) => [
    tool({
      name: "invoke_hermes",
      description: "Invoke Hermes with a message and return its response.",
      parameters: Type.Object({
        message: Type.String({ description: "Message to send to Hermes.", maxLength: 4000 }),
      }),
      outputSchema: Type.Object(
        {
          ok: Type.Boolean(),
          reply: Type.String(),
          stderr: Type.String(),
          returnCode: Type.Number(),
        },
        { additionalProperties: false }
      ),
      async execute(params, _config, _context) {
        if (!params?.message || typeof params.message !== "string" || !params.message.trim()) {
          return {
            ok: false,
            reply: "",
            stderr: "Missing or empty message",
            returnCode: 2,
          };
        }

        const trimmed = params.message.trim();
        if (trimmed.length > 4000) {
          return {
            ok: false,
            reply: "",
            stderr: "Message exceeds 4000 characters",
            returnCode: 2,
          };
        }

        try {
          return await invokeHermesViaRouter(trimmed);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          return {
            ok: false,
            reply: "",
            stderr: `Hermes invocation failed: ${message}`,
            returnCode: -1,
          };
        }
      },
    }),
    tool({
      name: "telegram_approval_command",
      description: TELEGRAM_APPROVAL_DESCRIPTION,
      parameters: TELEGRAM_APPROVAL_PARAMETERS,
      outputSchema: Type.Object({}, { additionalProperties: true }),
      // Factory shape, not execute: ToolPluginExecutionContext carries only
      // {api, signal, toolCallId, onUpdate} and never any sender identity.
      // OpenClawPluginToolContext does -- requesterSenderId and senderIsOwner
      // are documented "runtime-provided, not tool args", so they cannot be
      // spoofed by the model. The host invokes this per materialisation with
      // the current turn's context.
      factory: ({ toolContext }) => ({
        name: "telegram_approval_command",
        label: "Telegram Approval Command",
        description: TELEGRAM_APPROVAL_DESCRIPTION,
        parameters: TELEGRAM_APPROVAL_PARAMETERS,
        outputSchema: Type.Object({}, { additionalProperties: true }),
        async execute(_toolCallId, params, _signal) {
          const text = typeof params?.text === "string" ? params.text.trim() : "";
          if (!text) return toolResult({ ok: false, error: "empty_command" });

          // Trusted, runtime-supplied sender. Absent means we cannot attribute
          // the command, so refuse -- same behaviour as before.
          const senderId = normalizeSenderId(toolContext?.requesterSenderId);
          if (!senderId) {
            return toolResult({
              ok: false,
              error: "no_verified_sender",
              detail:
                "The gateway context carried no requesterSenderId; refusing to " +
                "act on an unauthenticated approval command.",
            });
          }
          try {
            const result = await runTelegramCommand(senderId, text);
            return toolResult({
              ...result,
              senderIsOwner: toolContext?.senderIsOwner === true,
            });
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            return toolResult({
              ok: false,
              error: `telegram_approval_command failed: ${message}`,
            });
          }
        },
      }),
    }),
  ],
});

// defineToolPlugin generates register() itself and only wires tools -- there is
// no user hook in its options -- so wrap it to add the slash commands. The
// generated register is writable/configurable, and base runs first so tool
// registration is unchanged.
const baseRegister = pluginEntry.register;
pluginEntry.register = function register(api) {
  const result = baseRegister.call(this, api);
  if (typeof api?.registerCommand === "function") {
    for (const def of APPROVAL_COMMANDS) api.registerCommand(buildApprovalCommand(def));
  }
  return result;
};

export default pluginEntry;
