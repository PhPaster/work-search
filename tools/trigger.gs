/**
 * jobradar — hourly trigger (Google Apps Script, free) + watchdog.
 * Adapted from PhPaster/shafa-watcher tools/trigger.gs.
 *
 * Layers (SETUP.md → «Автозапуск у хмарі»):
 *   1. This script: every hour on Google's servers, no computer needed.
 *   2. GitHub schedule in radar.yml: every 3 h, runs only if this script was silent for 70+ min.
 * Lock: before dispatching we look at the latest run; if it is still running or started
 * < GATE_MIN minutes ago, we skip. So retries/duplicate triggers never multiply runs.
 *
 * Watchdog: no successful run for STALE_MIN minutes → ⚠️ in Telegram (at most every ALERT_EVERY_H),
 * ✅ when runs recover. 14 days before the GitHub token expires → a daily 🔑 reminder.
 *
 * One-time setup:
 *   1. https://script.google.com → New project → paste this file instead of Code.gs → Save.
 *   2. Project Settings (⚙) → Script Properties:
 *        GH_TOKEN = fine-grained GitHub token (only PhPaster/work-search, Actions: Read and write)
 *        TG_TOKEN = Telegram bot token (same as in .env)   — for the watchdog
 *        TG_CHAT  = Telegram chat id (same as in .env)
 *   3. Select function `setup` → Run (approve the permissions). Then `check` — should show 200.
 *
 * If the function fails, Google e-mails you about the trigger error.
 */

const REPO = 'PhPaster/work-search';
const WORKFLOW = 'radar.yml';
const BRANCH = 'main';
const EVERY_HOURS = 1;      // how often to start a check
const GATE_MIN = 45;        // don't dispatch if the last run started less than GATE_MIN min ago
const STALE_MIN = 180;      // watchdog: alert if there was no successful run for this long
const ALERT_EVERY_H = 6;    // repeat the alert at most this often
const EXPIRY_WARN_D = 14;   // remind about the token this many days before it expires

const props_ = () => PropertiesService.getScriptProperties();

function gh_(path, method, payload) {
  const token = props_().getProperty('GH_TOKEN')?.trim();
  if (!token) throw new Error('GH_TOKEN is missing in Script Properties');
  const opts = {
    method: method || 'get',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'jobradar-trigger',
    },
    muteHttpExceptions: true,
  };
  if (payload) { opts.contentType = 'application/json'; opts.payload = JSON.stringify(payload); }
  return UrlFetchApp.fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}${path}`, opts);
}

function runs_() {
  const res = gh_('/runs?per_page=20');
  if (res.getResponseCode() !== 200) {
    throw new Error(`GitHub ${res.getResponseCode()}: ${res.getContentText().slice(0, 300)}`);
  }
  // GitHub's own schedule runs are mostly just the `gate` check, so they don't count.
  const runs = JSON.parse(res.getContentText()).workflow_runs.filter((r) => r.event !== 'schedule');
  return { runs, headers: res.getHeaders() };
}

function minutesSince_(iso) { return (Date.now() - new Date(iso).getTime()) / 60000; }

function tg_(text) {
  const token = props_().getProperty('TG_TOKEN')?.trim();
  const chat = props_().getProperty('TG_CHAT')?.trim();
  if (!token || !chat) { console.warn('TG_TOKEN/TG_CHAT missing — watchdog messages are off'); return; }
  const res = UrlFetchApp.fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    payload: JSON.stringify({ chat_id: chat, text, parse_mode: 'HTML' }),
  });
  if (res.getResponseCode() !== 200) console.warn(`Telegram ${res.getResponseCode()}: ${res.getContentText().slice(0, 200)}`);
}

/** Trigger entry point: watchdog + lock + dispatch. */
function dispatch() {
  const { runs, headers } = runs_();
  watchdog_(runs);
  tokenExpiry_(headers);

  const latest = runs[0];
  if (latest) {
    const age = minutesSince_(latest.created_at);
    if (latest.status !== 'completed' && age < 40) {
      console.log(`skip: run ${latest.id} is still ${latest.status} (${age.toFixed(1)} min)`);
      return;
    }
    if (age < GATE_MIN) {
      console.log(`skip: last run started ${age.toFixed(1)} min ago (${latest.event})`);
      return;
    }
  }
  const res = gh_('/dispatches', 'post', { ref: BRANCH });
  const code = res.getResponseCode();
  if (code !== 204 && code !== 200) {
    throw new Error(`GitHub ${code}: ${res.getContentText().slice(0, 300)}`);
  }
  console.log(`dispatched (${code})`);
}

function watchdog_(runs) {
  const ok = runs.find((r) => r.conclusion === 'success');
  const age = ok ? minutesSince_(ok.updated_at) : Infinity;
  const p = props_();
  const alertedAt = Number(p.getProperty('alerted_at') || 0);
  if (age >= STALE_MIN && runs.length) {
    if (Date.now() - alertedAt >= ALERT_EVERY_H * 3600e3) {
      const last = `останній запуск: ${runs[0].event}, ${runs[0].status}/${runs[0].conclusion || '—'}`;
      tg_(`⚠️ <b>jobradar</b>: успішного запуску не було ${Math.round(age / 60)} год.\n${last}\n` +
          `Перевір: github.com/${REPO}/actions`);
      p.setProperty('alerted_at', String(Date.now()));
    }
  } else if (alertedAt && age < STALE_MIN) {
    tg_('✅ <b>jobradar</b>: запуски відновились.');
    p.deleteProperty('alerted_at');
  }
}

function header_(headers, name) {
  const key = Object.keys(headers).find((k) => k.toLowerCase() === name);
  return key ? headers[key] : '';
}

/** "2027-11-09 12:00:00 UTC" or "2027-11-09 14:00:00 +0200" → milliseconds. */
function parseExpiry_(raw) {
  const m = /(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*(UTC|[+-]\d{2}:?\d{2})?/.exec(raw);
  if (!m) return NaN;
  let tz = m[3] && m[3] !== 'UTC' ? m[3] : 'Z';
  if (/^[+-]\d{4}$/.test(tz)) tz = tz.slice(0, 3) + ':' + tz.slice(3);
  return new Date(`${m[1]}T${m[2]}${tz}`).getTime();
}

function tokenExpiry_(headers) {
  const raw = header_(headers, 'github-authentication-token-expiration');
  if (!raw) return;  // token without expiry
  const days = (parseExpiry_(raw) - Date.now()) / 86400e3;
  if (!(days < EXPIRY_WARN_D)) return;
  const p = props_();
  if (Date.now() - Number(p.getProperty('expiry_warned_at') || 0) < 86400e3) return;
  tg_(`🔑 <b>jobradar</b>: токен GitHub у тригері Apps Script спливає через ${Math.max(0, Math.floor(days))} дн. (${raw}).\n` +
      'Створи новий (лише work-search, Actions: Read and write) і заміни GH_TOKEN у Script Properties.');
  p.setProperty('expiry_warned_at', String(Date.now()));
}

/** Creates the hourly trigger (removes old ones) and dispatches the first run right away. */
function setup() {
  stop();
  ScriptApp.newTrigger('dispatch').timeBased().everyHours(EVERY_HOURS).create();
  console.log(`trigger created: dispatch every ${EVERY_HOURS} h`);
  dispatch();
}

/** Diagnostics: token access and expiry, latest runs, Telegram test. */
function check() {
  const res = gh_('/runs?per_page=5');
  const h = res.getHeaders();
  console.log(`repository: ${res.getResponseCode()} (404 = repo not selected in the token; 401 = invalid token)`);
  if (res.getResponseCode() !== 200) {
    // on 401/404 GitHub sends no expiry header, so "never" would be misleading
    console.warn('token NOT accepted – check GH_TOKEN in Script Properties (saved? full value?)');
  } else {
    console.log(`token expires: ${header_(h, 'github-authentication-token-expiration') || 'never'}`);
  }
  if (res.getResponseCode() === 200) {
    JSON.parse(res.getContentText()).workflow_runs.forEach((r) =>
      console.log(`${r.created_at} ${r.event} ${r.status}/${r.conclusion || '—'}`));
  }
  console.log(`triggers: ${ScriptApp.getProjectTriggers().map((t) => t.getHandlerFunction()).join(', ') || 'none'}`);
  tg_('🧪 jobradar: тест сторожа Apps Script — Telegram працює.');
}

/** Removes the trigger. */
function stop() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === 'dispatch')
    .forEach((t) => ScriptApp.deleteTrigger(t));
  console.log('dispatch triggers removed');
}
