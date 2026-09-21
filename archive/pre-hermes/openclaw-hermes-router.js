#!/usr/bin/env node
/**
 * OpenClaw -> Hermes HTTP router
 *
 * Local-only HTTP server. Not exposed externally.
 * Endpoint: POST /invoke
 * Body: { message: string, conversationId?: string }
 * Response: { ok, reply, stderr, returnCode }
 */
const http = require('node:http');
const { spawn } = require('node:child_process');

const PORT = parseInt(process.env.OPENCLAW_HERMES_ROUTER_PORT || '18790', 10);
const HOST = process.env.OPENCLAW_HERMES_ROUTER_HOST || '127.0.0.1';
const HERMES_TIMEOUT_MS = parseInt(process.env.OPENCLAW_HERMES_TIMEOUT_MS || '120000', 10);

async function invokeHermes(message, retries = 2) {
  const args = ['chat', '-q', message];
  let lastError = null;
  let attempt = 0;

  while (attempt < retries) {
    attempt += 1;
    try {
      const proc = spawn('hermes', args, {
        stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, HOME: process.env.HOME || 'C:\\Users\\HP' },
      });

      let stdout = '';
      let stderr = '';
      let settled = false;

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          proc.kill('SIGTERM');
          setTimeout(() => {
            if (!proc.killed) proc.kill('SIGKILL');
          }, 5000);
        }
      }, HERMES_TIMEOUT_MS);

      proc.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
      proc.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

      const exitCode = await new Promise((resolve) => {
        proc.on('close', (code) => {
          clearTimeout(timer);
          resolve(code);
        });
        proc.on('error', (err) => {
          clearTimeout(timer);
          resolve(-1);
        });
      });

      const trimmed = stdout.trim();
      if (exitCode === 0 && trimmed) {
        return { ok: true, reply: trimmed, stderr: stderr.trim(), returnCode: exitCode };
      }

      lastError = { ok: false, reply: '', stderr: stderr.trim() || `Empty output from hermes (exit ${exitCode})`, returnCode: exitCode };
    } catch (e) {
      lastError = { ok: false, reply: '', stderr: String(e), returnCode: -1 };
    }
  }

  return lastError || { ok: false, reply: '', stderr: 'hermes unavailable', returnCode: -1 };
}

async function handleRequest(req, res) {
  if (req.method !== 'POST' || req.url !== '/invoke') {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
    return;
  }

  let body = '';
  req.on('data', (chunk) => { body += chunk.toString(); });

  req.on('end', async () => {
    let payload = {};
    try {
      payload = body ? JSON.parse(body) : {};
    } catch (e) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: `Invalid JSON: ${e.message}` }));
      return;
    }

    const message = payload.message || payload.query || '';
    if (!message) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Missing message' }));
      return;
    }

    const result = await invokeHermes(message, 2);
    const status = result.ok ? 200 : 502;
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(result));
  });
}

const server = http.createServer(handleRequest);
server.listen(PORT, HOST, () => {
  process.stdout.write(`OPENCLAW_HERMES_ROUTER_LISTEN ${HOST}:${PORT}\n`);
});

process.on('SIGINT', () => {
  process.stdout.write('OPENCLAW_HERMES_ROUTER_SHUTDOWN\n');
  server.close(() => process.exit(0));
});
