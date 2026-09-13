#!/usr/bin/env node
'use strict';
/**
 * Projects agent — offline tests.
 *
 * No network and no ClickUp token required. Every HTTP test runs against a
 * loopback server via CLICKUP_API_BASE, which is the only reason that override
 * exists: the duplicate-task bug cannot be demonstrated against the real API
 * without creating duplicate tasks in a client's workspace.
 */

const assert = require('assert');
const http = require('http');

process.env.CLICKUP_TOKEN = process.env.CLICKUP_TOKEN || 'test-token-not-real';

let passed = 0;
function ok(name) { passed += 1; console.log(`PASS: ${name}`); }

/** Start a loopback server that records every request it receives. */
function server(handler) {
  return new Promise((resolve) => {
    const seen = [];
    const srv = http.createServer((req, res) => {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        seen.push({ method: req.method, url: req.url, body });
        handler(req, res, seen.length);
      });
    });
    srv.listen(0, '127.0.0.1', () => {
      resolve({ srv, seen, base: `http://127.0.0.1:${srv.address().port}/api/v2` });
    });
  });
}

async function withAgent(base, fn) {
  process.env.CLICKUP_API_BASE = base;
  // Fresh module each time so BASE is re-read from the environment.
  delete require.cache[require.resolve('../agents/projects/main.js')];
  const agent = require('../agents/projects/main.js');
  try {
    return await fn(agent);
  } finally {
    delete process.env.CLICKUP_API_BASE;
  }
}

async function test_post_is_not_retried_on_5xx() {
  const { srv, seen, base } = await server((req, res) => {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end('{"err":"upstream boom","ECODE":"SRV_001"}');
  });
  await withAgent(base, async (agent) => {
    const res = await agent.request('/list/abc123/task', 'POST', JSON.stringify({ name: 'x' }));
    assert.strictEqual(res.status, 500);
    // The whole point: one POST reached the server, not three.
    assert.strictEqual(seen.length, 1,
      `POST retried ${seen.length} times on 5xx -- this creates duplicate tasks`);
    assert.strictEqual(seen[0].method, 'POST');
  });
  srv.close();
  ok('post_is_not_retried_on_5xx');
}

async function test_post_is_not_retried_on_429() {
  const { srv, seen, base } = await server((req, res) => {
    res.writeHead(429, { 'Content-Type': 'application/json' });
    res.end('{"err":"rate limited","ECODE":"RATE_001"}');
  });
  await withAgent(base, async (agent) => {
    await agent.request('/list/abc123/task', 'POST', JSON.stringify({ name: 'x' }));
    assert.strictEqual(seen.length, 1, `POST retried ${seen.length} times on 429`);
  });
  srv.close();
  ok('post_is_not_retried_on_429');
}

async function test_get_is_retried_on_5xx() {
  const { srv, seen, base } = await server((req, res, n) => {
    if (n < 3) {
      res.writeHead(503); res.end('{"err":"try later","ECODE":"SRV_002"}');
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"tasks":[]}');
    }
  });
  await withAgent(base, async (agent) => {
    const res = await agent.request('/team/123/task', 'GET');
    // GET is safe to repeat, so the transient 503s are absorbed.
    assert.strictEqual(res.status, 200);
    assert.strictEqual(seen.length, 3);
  });
  srv.close();
  ok('get_is_retried_on_5xx');
}

async function test_put_is_retried_on_5xx() {
  const { srv, seen, base } = await server((req, res, n) => {
    if (n < 2) { res.writeHead(500); res.end('{}'); }
    else { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"id":"t1"}'); }
  });
  await withAgent(base, async (agent) => {
    const res = await agent.request('/task/t1', 'PUT', JSON.stringify({ name: 'y' }));
    assert.strictEqual(res.status, 200);
    assert.strictEqual(seen.length, 2);
  });
  srv.close();
  ok('put_is_retried_on_5xx');
}

async function test_path_traversal_is_rejected() {
  await withAgent('http://127.0.0.1:1/api/v2', async (agent) => {
    for (const bad of ['../../team/123/space', 'a/b', 'x?y=1', 'x#f', '..', '', 'a'.repeat(65)]) {
      assert.throws(() => agent.pathSegment(bad, 'taskId'), /Invalid taskId/,
        `traversal or injection payload accepted: ${JSON.stringify(bad)}`);
    }
    // Legitimate ClickUp ids still pass.
    for (const good of ['123t3hvmy45', 'abc-DEF_9', '1200430000000602']) {
      assert.strictEqual(agent.pathSegment(good, 'taskId'), good);
    }
  });
  ok('path_traversal_is_rejected');
}

async function test_error_body_is_not_propagated() {
  await withAgent('http://127.0.0.1:1/api/v2', async (agent) => {
    const secretish = JSON.stringify({
      err: 'Team not authorized, token pk_240010007_ABCDEF leaked here',
      ECODE: 'OAUTH_027',
    });
    const reason = agent.reasonFor(401, secretish);
    assert.strictEqual(reason, 'ClickUp 401 (OAUTH_027)');
    // Nothing from the third-party body survives except the stable code.
    assert.ok(!reason.includes('pk_240010007'));
    assert.ok(!reason.includes('Team not authorized'));
    // A non-JSON body degrades to the status alone, not to raw HTML.
    assert.strictEqual(agent.reasonFor(502, '<html>gateway error</html>'), 'ClickUp 502');
  });
  ok('error_body_is_not_propagated');
}

async function test_idempotent_set_excludes_post() {
  await withAgent('http://127.0.0.1:1/api/v2', async (agent) => {
    assert.ok(!agent.IDEMPOTENT.has('POST'));
    assert.ok(agent.IDEMPOTENT.has('GET'));
    assert.ok(agent.IDEMPOTENT.has('PUT'));
  });
  ok('idempotent_set_excludes_post');
}

async function main() {
  try {
    await test_post_is_not_retried_on_5xx();
    await test_post_is_not_retried_on_429();
    await test_get_is_retried_on_5xx();
    await test_put_is_retried_on_5xx();
    await test_path_traversal_is_rejected();
    await test_error_body_is_not_propagated();
    await test_idempotent_set_excludes_post();
    console.log(`\nALL PROJECTS AGENT TESTS PASSED (${passed})`);
    process.exit(0);
  } catch (e) {
    console.error(`FAIL: ${e.message}`);
    console.error(e.stack);
    process.exit(1);
  }
}

main();
