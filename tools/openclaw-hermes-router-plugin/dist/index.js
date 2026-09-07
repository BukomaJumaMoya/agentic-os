import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { Type } from "typebox";
import http from "node:http";

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
  ],
});
