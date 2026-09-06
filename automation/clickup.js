#!/usr/bin/env node
/**
 * clickup.js — Hermes ClickUp API wrapper
 * Provides a simple CLI for Hermes to interact with ClickUp.
 * Auth: CLICKUP_TOKEN env var (personal token, owner in "Juma Moya's Workspace")
 *
 * Usage:
 *   node clickup.js list-tasks --space <spaceId>
 *   node clickup.js get-task <taskId>
 *   node clickup.js create-task --list <listId> --name <name> [--desc <desc>] [--priority <priority>] [--status <statusId>]
 *   node clickup.js update-task <taskId> [--name <name>] [--desc <desc>] [--status <status>] [--priority <priority>]
 *   node clickup.js searchTasks --query <query>
 *   node clickup.js list-spaces
 *   node clickup.js list-lists --space <spaceId>
 */
const https = require('https');
const { argv, env } = process;

const TEAM_ID = '1200430000000602'; // Juma Moya's Workspace
const TOKEN = env.CLICKUP_TOKEN;
if (!TOKEN) {
  console.error('ERROR: CLICKUP_TOKEN environment variable not set.');
  console.error('Set it in Hermes .env or export CLICKUP_TOKEN=<your-token>');
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
  const args = argv.slice(2);
  const cmd = args[0];

  try {
    switch (cmd) {
      case 'list-spaces': {
        const res = await request(`/team/${TEAM_ID}/space`);
        if (res.status !== 200) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body.spaces || [], null, 2));
        break;
      }

      case 'list-lists': {
        const spaceId = args.find(a => a.startsWith('--space='))?.split('=')[1];
        if (!spaceId) { console.error('Usage: list-lists --space <spaceId>'); process.exit(1); }
        const res = await request(`/space/${spaceId}/list`);
        if (res.status !== 200) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body.lists || [], null, 2));
        break;
      }

      case 'list-tasks': {
        const spaceId = args.find(a => a.startsWith('--space='))?.split('=')[1];
        const listId = args.find(a => a.startsWith('--list='))?.split('=')[1];
        const status = args.find(a => a.startsWith('--status='))?.split('=')[1];
        let path;
        if (spaceId) path = `/space/${spaceId}/task`;
        else if (listId) path = `/list/${listId}/task`;
        else path = `/team/${TEAM_ID}/task`;
        const params = [`limit=50`];
        if (status) params.push(`status=${status}`);
        path += '?' + params.join('&');
        const res = await request(path);
        if (res.status !== 200) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body.tasks || [], null, 2));
        break;
      }

      case 'get-task': {
        const taskId = args[1];
        if (!taskId) { console.error('Usage: get-task <taskId>'); process.exit(1); }
        const res = await request(`/task/${taskId}`);
        if (res.status !== 200) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body, null, 2));
        break;
      }

      case 'create-task': {
        const listId = args.find(a => a.startsWith('--list='))?.split('=')[1];
        const name = args.find(a => a.startsWith('--name='))?.split('=')[1] || args.find(a => !a.startsWith('--'))?.split('=')[1];
        const desc = args.find(a => a.startsWith('--desc='))?.split('=')[1];
        const priority = args.find(a => a.startsWith('--priority='))?.split('=')[1];
        const statusId = args.find(a => a.startsWith('--status='))?.split('=')[1];
        if (!listId || !name) { console.error('Usage: create-task --list <listId> --name <name> [--desc <desc>] [--priority <priority>] [--status <statusId>]'); process.exit(1); }
        const body = { name };
        if (desc) body.description = desc;
        if (priority) body.priority = priority;
        if (statusId) body.status = statusId;
        const res = await request(`/list/${listId}/task`, 'POST', JSON.stringify(body));
        if (res.status !== 200 && res.status !== 201) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body, null, 2));
        break;
      }

      case 'update-task': {
        const taskId = args[1];
        if (!taskId) { console.error('Usage: update-task <taskId> [--name <name>] [--desc <desc>] [--status <status>] [--priority <priority>]'); process.exit(1); }
        const body = {};
        const name = args.find(a => a.startsWith('--name='))?.split('=')[1];
        const desc = args.find(a => a.startsWith('--desc='))?.split('=')[1];
        const status = args.find(a => a.startsWith('--status='))?.split('=')[1];
        const priority = args.find(a => a.startsWith('--priority='))?.split('=')[1];
        if (name) body.name = name;
        if (desc) body.description = desc;
        if (status) body.status = status;
        if (priority) body.priority = priority;
        const res = await request(`/task/${taskId}`, 'PUT', JSON.stringify(body));
        if (res.status !== 200) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body, null, 2));
        break;
      }

      case 'searchTasks': {
        const query = args.find(a => a.startsWith('--query='))?.split('=')[1] || args[1];
        if (!query) { console.error('Usage: searchTasks <query>'); process.exit(1); }
        const res = await request(`/team/${TEAM_ID}/task?search=${encodeURIComponent(query)}&limit=20`);
        if (res.status !== 200) { console.error('Error:', res.status, res.raw); process.exit(1); }
        console.log(JSON.stringify(res.body.tasks || [], null, 2));
        break;
      }

      default:
        console.log('ClickUp CLI - Hermes wrapper');
        console.log('Usage: node clickup.js <command> [options]');
        console.log('Commands:');
        console.log('  list-spaces                      List all spaces');
        console.log('  list-lists --space <spaceId>    List lists in a space');
        console.log('  list-tasks [--space <id>] [--list <id>] [--status <statusId>]  List tasks');
        console.log('  get-task <taskId>               Get a single task');
        console.log('  create-task --list <id> --name <name> [--desc <desc>] [--priority <priority>] [--status <statusId>]');
        console.log('  update-task <taskId> [--name <name>] [--desc <desc>] [--status <status>] [--priority <priority>]');
        console.log('  searchTasks <query>            Search tasks across team');
        console.log('');
        console.log('Auth: CLICKUP_TOKEN env var (personal token)');
        console.log(`Team ID: ${TEAM_ID}`);
        break;
    }
  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
}

main();
