#!/usr/bin/env node
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
 */

const https = require('https');
const { argv, env } = process;

const AGENT = 'projects';
const VERSION = '1.0.0';
const TEAM_ID = '1200430000000602';
const TOKEN = env.CLICKUP_TOKEN;
const PORT = env.PORT || '18789';

if (!TOKEN) {
  console.error(JSON.stringify({
    agent: AGENT,
    error: 'Missing CLICKUP_TOKEN environment variable.',
    hint: 'Set CLICKUP_TOKEN before invoking the Projects Agent.'
  }));
  process.exit(1);
}

const BASE = 'https://api.clickup.com/api/v2';
const AUTH = TOKEN;

function request(path, method = 'GET', body = null, retries = 2) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${BASE}${path}`);
    const opts = {
      hostname: url.hostname,
      path: url.pathname + url.search,
      method,
      headers: {
        'Authorization': AUTH,
        'Content-Type': 'application/json',
      },
    };
    if (body) opts.headers['Content-Length'] = Buffer.byteLength(body);

    const req = https.request(opts, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        const truncated = data.length > 500 ? data.slice(0, 500) + '...<truncated>' : data;
        if (res.statusCode >= 500 && retries > 0) {
          setTimeout(() => request(path, method, body, retries - 1).then(resolve).catch(reject), 2000);
          return;
        }
        if (res.statusCode === 429 && retries > 0) {
          setTimeout(() => request(path, method, body, retries - 1).then(resolve).catch(reject), 4000);
          return;
        }
        try {
          const json = JSON.parse(data);
          resolve({ status: res.statusCode, body: json, raw: truncated });
        } catch {
          resolve({ status: res.statusCode, body: null, raw: truncated });
        }
      });
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('Timeout')); });
    if (body) req.write(body);
    req.end();
  });
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

  const action = payload.action;
  if (!action) {
    console.error(JSON.stringify({
      agent: AGENT,
      error: 'Missing required field: action',
      allowed: ['list_spaces', 'list_lists', 'list_tasks', 'get_task', 'create_task', 'update_task', 'search_tasks']
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
        const res = await request(`/team/${TEAM_ID}/space`);
        if (res.status !== 200) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { spaces: res.body.spaces || [] };
        break;
      }
      case 'list_lists': {
        const spaceId = payload.spaceId;
        if (!spaceId) throw new Error('Missing required field: spaceId');
        const res = await request(`/space/${spaceId}/list`);
        if (res.status !== 200) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { lists: res.body.lists || [] };
        break;
      }
      case 'list_tasks': {
        const listId = payload.listId;
        const spaceId = payload.spaceId;
        let path;
        if (listId) path = `/list/${listId}/task`;
        else if (spaceId) path = `/space/${spaceId}/task`;
        else path = `/team/${TEAM_ID}/task`;
        const params = ['limit=50'];
        if (payload.status) params.push(`status=${encodeURIComponent(payload.status)}`);
        path += '?' + params.join('&');
        const res = await request(path);
        if (res.status !== 200) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { tasks: res.body.tasks || [] };
        break;
      }
      case 'get_task': {
        const taskId = payload.taskId;
        if (!taskId) throw new Error('Missing required field: taskId');
        const res = await request(`/task/${taskId}`);
        if (res.status !== 200) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { task: res.body };
        break;
      }
      case 'create_task': {
        const listId = payload.listId;
        const name = payload.name;
        if (!listId || !name) throw new Error('Missing required fields: listId, name');
        const body = { name };
        if (payload.description) body.description = payload.description;
        if (payload.priority) body.priority = payload.priority;
        if (payload.status) body.status = payload.status;
        const res = await request(`/list/${listId}/task`, 'POST', JSON.stringify(body));
        if (res.status !== 200 && res.status !== 201) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { task: res.body };
        break;
      }
      case 'update_task': {
        const taskId = payload.taskId;
        if (!taskId) throw new Error('Missing required field: taskId');
        const body = {};
        if (payload.name) body.name = payload.name;
        if (payload.description) body.description = payload.description;
        if (payload.status) body.status = payload.status;
        if (payload.priority) body.priority = payload.priority;
        const res = await request(`/task/${taskId}`, 'PUT', JSON.stringify(body));
        if (res.status !== 200) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { task: res.body };
        break;
      }
      case 'search_tasks': {
        const query = payload.query;
        if (!query) throw new Error('Missing required field: query');
        const res = await request(`/team/${TEAM_ID}/task?search=${encodeURIComponent(query)}&limit=20`);
        if (res.status !== 200) throw new Error(`ClickUp ${res.status}: ${res.raw}`);
        result = { tasks: res.body.tasks || [] };
        break;
      }
      default:
        console.error(JSON.stringify({
          agent: AGENT,
          error: `Unknown action: ${action}`,
          allowed: ['list_spaces', 'list_lists', 'list_tasks', 'get_task', 'create_task', 'update_task', 'search_tasks']
        }));
        process.exit(1);
    }
  } catch (e) {
    console.error(JSON.stringify({
      agent: AGENT,
      action,
      status: 'error',
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
  console.log(JSON.stringify(output, indent=2));
  process.exit(0);
}

main();
