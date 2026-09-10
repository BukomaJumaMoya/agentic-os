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

/**
 * Resolve the real sender from the gateway context. Never from model-supplied
 * parameters: a spoofable sender id would hand approval authority to anything
 * that can call the tool.
 */
function resolveSenderId(context) {
  const candidates = [
    context?.senderId,
    context?.userId,
    context?.user?.id,
    context?.message?.from?.id,
    context?.source?.userId,
    context?.chat?.id,
  ];
  for (const candidate of candidates) {
    if (candidate === undefined || candidate === null) continue;
    const value = String(candidate).trim();
    if (value) return value;
  }
  return null;
}

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

export default defineToolPlugin({
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
      description:
        "REQUIRED handler for Telegram approval replies. Call this tool " +
        "whenever an incoming message matches 'APPROVE <id>', 'REJECT <id>', " +
        "'STATUS <id>', 'RESUME <id>' or 'LIST' (case-insensitive), where " +
        "<id> is a UUID taken from an approval prompt. Pass the raw message " +
        "text through unchanged. This is the only correct handler for those " +
        "messages: do not route them to skill, workshop or proposal tools, " +
        "even though the UUID resembles a proposal id. Authorisation is " +
        "enforced by the orchestrator against the configured allowlist.",
      parameters: Type.Object({
        text: Type.String({
          description:
            "The raw command text exactly as the user sent it, e.g. " +
            "'APPROVE 123e4567-e89b-12d3-a456-426614174000'.",
          maxLength: 500,
        }),
      }),
      outputSchema: Type.Object({}, { additionalProperties: true }),
      async execute(params, _config, context) {
        const text = typeof params?.text === "string" ? params.text.trim() : "";
        if (!text) {
          return { ok: false, error: "empty_command" };
        }
        // Sender comes from the gateway context only. If the context does not
        // carry one we refuse rather than trusting a model-supplied id.
        const senderId = resolveSenderId(context);
        if (!senderId) {
          return {
            ok: false,
            error: "no_verified_sender",
            detail:
              "The gateway context carried no sender id; refusing to act on an " +
              "unauthenticated approval command.",
          };
        }
        try {
          return await runTelegramCommand(senderId, text);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          return { ok: false, error: `telegram_approval_command failed: ${message}` };
        }
      },
    }),
  ],
});
