#!/usr/bin/env node
'use strict';
/**
 * Projects Agent — standalone bounded process.
 *
 * Authority levels:
 *   READ      - list/get/search
 *   INTERNAL WRITE - create/update within ClickUp
 *   EXTERNAL ACTION - not allowed
 *
 * Purpose: bounded ClickUp/project-management operations
 * Forbidden: global orchestration, external messaging, destructive ops without approval
 *
 * 'use strict' is deliberate: the previous version's `JSON.stringify(output,
 * indent=2)` was Python syntax that silently created an implicit global and
 * passed 2 as the `replacer` argument, where it was ignored. Under strict mode
 * that line throws instead of quietly doing the wrong thing.
 */

const https = require('https');
const http = require('http');
const { argv, env } = process;

const AGENT = 'projects';
const VERSION = '1.1.0';
// Owner-scoped id (audit S-5). Overridable so the agent is not welded to one
// workspace; the previous hardcoded value remains the default.
const TEAM_ID = env.CLICKUP_TEAM_ID || '1200430000000602';
const TOKEN = env.CLICKUP_TOKEN;

if (!TOKEN) {
  console.error(JSON.stringify({
    agent: AGENT,
    error: 'Missing CLICKUP_TOKEN environment variable.',
    hint: 'Set CLICKUP_TOKEN before invoking the Projects Agent.'
  }));
  process.exit(1);
}

// Overridable only so tests can point at a loopback server. The POST-retry
// behaviour below cannot be verified against the real API without creating
// duplicate tasks in a client's workspace, which is precisely the bug.
const BASE = env.CLICKUP_API_BASE || 'https://api.clickup.com/api/v2';
const AUTH = TOKEN;

/** A caller's fault, not ClickUp's. Reported without a third-party body. */
class AgentError extends Error {}

// ClickUp ids are opaque alphanumeric strings; custom ids add - and _.
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * Validate and encode one path segment.
 *
 * Interpolating a caller-supplied id straight into the path let it escape the
 * API base entirely: taskId "../../team/<id>/space" made
 * `${BASE}/task/${taskId}` resolve to /api/team/<id>/space, since new URL()
 * normalises dot segments. Verified against the live API, which answered from a
 * different route. A whitelist rejects that before encodeURIComponent ever runs.
 */
function pathSegment(value, field) {
  const raw = String(value === undefined || value === null ? '' : value);
  if (!ID_RE.test(raw)) {
    throw new AgentError(
      `Invalid ${field}: expected an alphanumeric ClickUp id (letters, digits, - and _, max 64)`);
  }
  return encodeURIComponent(raw);
}

/**
 * Reduce a ClickUp error response to a stable, non-leaky reason.
 *
 * The previous version put up to 500 characters of raw third-party response
 * body into the thrown message, which flowed into workflow output and evidence
 * files on disk (audit S-9). ECODE is ClickUp's stable machine code and is safe
 * to keep; the free-text body is not.
 */
function reasonFor(status, data) {
  let ecode = null;
  try {
    const parsed = JSON.parse(data);
    if (parsed && typeof parsed.ECODE === 'string') ecode = parsed.ECODE;
  } catch {
    /* non-JSON body: report the status alone */
  }
  return ecode ? `ClickUp ${status} (${ecode})` : `ClickUp ${status}`;
}

// Safe to repeat. POST is absent on purpose -- see request().
const IDEMPOTENT = new Set(['GET', 'PUT', 'HEAD']);

function request(path, method = 'GET', body = null, retries = 2) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${BASE}${path}`);
    const client = url.protocol === 'http:' ? http : https;
    const opts = {
      hostname: url.hostname,
      // Never set before: with the real API on default 443 it did not matter,
      // but an explicit port in BASE was silently ignored.
      port: url.port || undefined,
      path: url.pathname + url.search,
      method,
      headers: {
        'Authorization': AUTH,
        'Content-Type': 'application/json',
        'User-Agent': `juma-freelance-ai-${AGENT}/${VERSION}`,
      },
    };
    if (body) opts.headers['Content-Length'] = Buffer.byteLength(body);

    // A retried POST creates a second task. The previous version retried every
    // method on 5xx and on 429, so one flaky response duplicated work in the
    // client's workspace -- and 5xx in particular gives no evidence about
    // whether the first attempt was applied. Only idempotent methods repeat;
    // a failed create is reported to the caller, who can decide.
    const repeatable = IDEMPOTENT.has(method);

    const req = client.request(opts, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        if (retries > 0 && repeatable && (res.statusCode >= 500 || res.statusCode === 429)) {
          const delay = res.statusCode === 429 ? 4000 : 2000;
          setTimeout(
            () => request(path, method, body, retries - 1).then(resolve).catch(reject),
            delay);
          return;
        }
        let parsed = null;
        try { parsed = JSON.parse(data); } catch { /* leave null */ }
        resolve({
          status: res.statusCode,
          body: parsed,
          reason: reasonFor(res.statusCode, data),
          retried: !repeatable && (res.statusCode >= 500 || res.statusCode === 429),
        });
      });
    });
    req.on('error', () => reject(new Error('ClickUp request failed (transport)')));
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('ClickUp request timed out')); });
    if (body) req.write(body);
    req.end();
  });
}

/** Throw on any non-success status, carrying only the sanitised reason. */
function expectOk(res, allowed = [200]) {
  if (!allowed.includes(res.status)) throw new Error(res.reason);
  return res;
}

async function main() {
  const inputFromArg = argv[2];
  let input = '';
  if (inputFromArg) {
    input = inputFromArg;
  } else if (!process.stdin.isTTY) {
    input = await new Promise((resolve) => {
      let data = '';
      process.stdin.on('data', chunk => data += chunk);
      process.stdin.on('end', () => resolve(data));
    });
  }
  // Windows PowerShell prefixes piped text with a UTF-8 BOM, which JSON.parse
  // rejects. automation/generate-proposal.ps1 pipes into these agents.
  input = input.replace(/^\uFEFF/, '').trim();

  let payload = {};
  try {
    payload = input ? JSON.parse(input) : {};
  } catch (e) {
    console.error(JSON.stringify({
      agent: AGENT,
      error: `Invalid JSON input: ${e.message}`
    }));
    process.exit(1);
  }

  const ALLOWED = ['list_spaces', 'list_lists', 'list_tasks', 'get_task',
                   'create_task', 'update_task', 'search_tasks'];

  const action = payload.action;
  if (!action) {
    console.error(JSON.stringify({
      agent: AGENT,
      error: 'Missing required field: action',
      allowed: ALLOWED
    }));
    process.exit(1);
  }

  const forbidden = ['delete_task', 'delete_list', 'delete_space'];
  if (forbidden.includes(action)) {
    console.error(JSON.stringify({
      agent: AGENT,
      error: `Action '${action}' is forbidden for this agent.`,
      authority: 'READ | INTERNAL_WRITE',
      forbidden: 'destructive external actions'
    }));
    process.exit(1);
  }

  let result;
  try {
    switch (action) {
      case 'list_spaces': {
        const res = expectOk(await request(`/team/${pathSegment(TEAM_ID, 'teamId')}/space`));
        result = { spaces: (res.body && res.body.spaces) || [] };
        break;
      }
      case 'list_lists': {
        if (!payload.spaceId) throw new AgentError('Missing required field: spaceId');
        const res = expectOk(await request(`/space/${pathSegment(payload.spaceId, 'spaceId')}/list`));
        result = { lists: (res.body && res.body.lists) || [] };
        break;
      }
      case 'list_tasks': {
        let path;
        if (payload.listId) path = `/list/${pathSegment(payload.listId, 'listId')}/task`;
        else if (payload.spaceId) path = `/space/${pathSegment(payload.spaceId, 'spaceId')}/task`;
        else path = `/team/${pathSegment(TEAM_ID, 'teamId')}/task`;
        const params = [];
        if (payload.status) params.push(`statuses[]=${encodeURIComponent(payload.status)}`);
        if (params.length) path += '?' + params.join('&');
        const res = expectOk(await request(path));
        result = { tasks: (res.body && res.body.tasks) || [] };
        break;
      }
      case 'get_task': {
        if (!payload.taskId) throw new AgentError('Missing required field: taskId');
        const res = expectOk(await request(`/task/${pathSegment(payload.taskId, 'taskId')}`));
        result = { task: res.body };
        break;
      }
      case 'create_task': {
        if (!payload.listId || !payload.name) {
          throw new AgentError('Missing required fields: listId, name');
        }
        const body = { name: String(payload.name) };
        if (payload.description) body.description = String(payload.description);
        if (payload.priority) body.priority = payload.priority;
        if (payload.status) body.status = String(payload.status);
        const res = await request(
          `/list/${pathSegment(payload.listId, 'listId')}/task`, 'POST', JSON.stringify(body));
        expectOk(res, [200, 201]);
        result = { task: res.body };
        break;
      }
      case 'update_task': {
        if (!payload.taskId) throw new AgentError('Missing required field: taskId');
        const body = {};
        if (payload.name) body.name = String(payload.name);
        if (payload.description) body.description = String(payload.description);
        if (payload.status) body.status = String(payload.status);
        if (payload.priority) body.priority = payload.priority;
        const res = expectOk(await request(
          `/task/${pathSegment(payload.taskId, 'taskId')}`, 'PUT', JSON.stringify(body)));
        result = { task: res.body };
        break;
      }
      case 'search_tasks': {
        if (!payload.query) throw new AgentError('Missing required field: query');
        // ClickUp v2 has no free-text task search. The team task endpoint
        // filters on structured fields only (confirmed against
        // developer.clickup.com/reference/getfilteredteamtasks), and silently
        // ignores an unrecognised `search=` parameter -- so the previous
        // implementation returned the entire team backlog and labelled it a
        // search result. Verified live: the query "zzzznosuchthingzzzz"
        // returned all 18 tasks. Filter here instead, and say that is what
        // happened rather than implying the server did it.
        const res = expectOk(await request(`/team/${pathSegment(TEAM_ID, 'teamId')}/task`));
        const all = (res.body && res.body.tasks) || [];
        const needle = String(payload.query).toLowerCase();
        const matched = all.filter((t) => {
          const hay = `${(t && t.name) || ''} ${(t && t.text_content) || ''}`.toLowerCase();
          return hay.includes(needle);
        });
        result = {
          tasks: matched.slice(0, 20),
          filter: 'client-side substring match on name and text_content',
          scanned: all.length,
          matched: matched.length,
          // ClickUp pages this endpoint. Anything beyond the first page was
          // never examined, and a caller must not read "0 matches" as "absent".
          complete: !!(res.body && res.body.last_page === true),
        };
        break;
      }
      default:
        console.error(JSON.stringify({
          agent: AGENT,
          error: `Unknown action: ${action}`,
          allowed: ALLOWED
        }));
        process.exit(1);
    }
  } catch (e) {
    console.error(JSON.stringify({
      agent: AGENT,
      action,
      status: 'error',
      // AgentError messages are ours. Everything else is already reduced to a
      // status code plus ClickUp's ECODE by reasonFor().
      error: e.message
    }));
    process.exit(1);
  }

  const output = {
    agent: AGENT,
    version: VERSION,
    action,
    authority: 'READ | INTERNAL_WRITE',
    result,
    status: 'success'
  };
  console.log(JSON.stringify(output, null, 2));
  process.exit(0);
}

// Exported for tests; only runs the CLI when executed directly.
module.exports = { pathSegment, reasonFor, request, IDEMPOTENT, ID_RE };

if (require.main === module) {
  main();
}
