import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { Type } from "typebox";

async function invokeHermes(message) {
  const cmd = "hermes";
  const args = ["chat", "-q", message];
  const proc = new Process(cmd, {
    args,
    env: { ...process.env, HOME: process.env.HOME || "C:\\Users\\HP" },
  });

  let stdout = "";
  let stderr = "";
  proc.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
  });
  proc.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  await proc.exited;
  const trimmed = stdout.trim();
  if (proc.exitCode === 0 && trimmed) {
    return { ok: true, reply: trimmed, stderr: stderr.trim(), returnCode: proc.exitCode };
  }

  return {
    ok: false,
    reply: "",
    stderr: stderr.trim() || `Empty output from hermes (exit ${proc.exitCode})`,
    returnCode: proc.exitCode,
  };
}

export default defineToolPlugin({
  id: "openclaw-hermes-router",
  name: "OpenClaw Hermes Router",
  description: "Route OpenClaw messages to Hermes via hermes chat -q.",
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
      async execute(_id, params) {
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
          return await invokeHermes(trimmed);
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
