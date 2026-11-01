"use strict";

// Sign-in and the authenticated api() helper. The views are added in Step 6.
// Tokens live only in the variables below, never in browser storage, so a page
// refresh means signing in again, and no other script or later user of this
// computer can read them back.

const $ = (id) => document.getElementById(id);

let config = null;       // {region, clientId, authMode} from /config.json
let tokens = null;       // {id, refresh}; refresh is null in local mode
let signedInAs = "";
let challenge = null;    // {username, session, userId} while NEW_PASSWORD_REQUIRED is pending

// ---- Cognito (InitiateAuth etc.), called straight from the browser. This only
// works because the app client has no secret (DV4). The password goes to
// Cognito and nowhere else; it is never sent to this app's API.

async function cognito(target, body) {
  const response = await fetch(`https://cognito-idp.${config.region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
      "Content-Type": "application/x-amz-json-1.1",
    },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || data.__type || `Sign-in failed (${response.status})`);
  return data;
}

function finishCognito(result, username) {
  const auth = result.AuthenticationResult;
  if (!auth || !auth.IdToken) throw new Error("Sign-in did not return a token");
  startSession({ id: auth.IdToken, refresh: auth.RefreshToken || null }, username);
}

async function cognitoSignIn(username, password) {
  const result = await cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: config.clientId,
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  if (result.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    // Kept in memory only; the Session is single-use and expires in minutes.
    challenge = {
      username,
      session: result.Session,
      userId: (result.ChallengeParameters || {}).USER_ID_FOR_SRP || username,
    };
    showForm("newpass");
    return;
  }
  if (result.ChallengeName) throw new Error(`Unsupported sign-in step: ${result.ChallengeName}`);
  finishCognito(result, username);
}

async function cognitoNewPassword(newPassword) {
  const result = await cognito("RespondToAuthChallenge", {
    ChallengeName: "NEW_PASSWORD_REQUIRED",
    ClientId: config.clientId,
    Session: challenge.session,
    ChallengeResponses: { USERNAME: challenge.userId, NEW_PASSWORD: newPassword },
  });
  const username = challenge.username;
  challenge = null;
  finishCognito(result, username);
}

// RF3: the ID token lasts about an hour. REFRESH_TOKEN_AUTH returns a new ID
// token without asking for the password again.
async function refreshIdToken() {
  if (!tokens || !tokens.refresh) return false;
  try {
    const result = await cognito("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: config.clientId,
      AuthParameters: { REFRESH_TOKEN: tokens.refresh },
    });
    tokens = { ...tokens, id: result.AuthenticationResult.IdToken };
    return true;
  } catch {
    return false;
  }
}

// ---- Screens

function showError(message) {
  const box = $("signin-error");
  box.textContent = message || "";
  box.hidden = !message;
}

function showForm(which) {
  $("signin-form").hidden = which !== "signin";
  $("newpass-form").hidden = which !== "newpass";
  showError("");
  const focus = which === "newpass" ? "new-password" : config.authMode === "local" ? "local-name" : "username";
  $(focus).focus();
}

function setBusy(form, busy) {
  for (const button of form.querySelectorAll("button")) button.disabled = busy;
}

function startSession(newTokens, name) {
  tokens = newTokens;
  signedInAs = name;
  $("password").value = "";
  $("local-name").value = "";  // else the next local sign-in appends to the old name
  $("new-password").value = "";
  $("new-password-confirm").value = "";
  $("signin").hidden = true;
  $("app-header").hidden = false;
  $("view").hidden = false;
  $("user").textContent = name;
  showError("");
  startViews();
}

function signOut(message) {
  stopPolling();
  routeStamp++;          // drop any response still in flight for the previous user
  retryAttempt = null;
  tokens = null;
  signedInAs = "";
  challenge = null;
  $("app-header").hidden = true;
  $("view").hidden = true;
  $("view").replaceChildren();
  $("signin").hidden = false;
  $("user").textContent = "";
  showForm("signin");
  showError(message);
}

// ---- Authenticated calls to this app's API.
// Returns {ok, status, data}. HTTP errors are returned, not thrown, so views
// can show a 400's `errors` list or `detail` text. A string body is sent as
// CSV (the suite routes); anything else as JSON.

async function api(method, path, body) {
  if (!tokens) return { ok: false, status: 401, data: null };  // e.g. a poll that fires after sign-out
  const send = () => {
    const headers = { Authorization: `Bearer ${tokens.id}` };
    let payload;
    if (typeof body === "string") {
      headers["Content-Type"] = "text/csv";
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    return fetch(path, { method, headers, body: payload });
  };

  let response = await send();
  if (response.status === 401) {
    // Refresh once and retry; a second 401 means the session is over.
    if (await refreshIdToken()) response = await send();
    if (response.status === 401) {
      signOut("Your session expired. Please sign in again.");
      return { ok: false, status: 401, data: null };
    }
  }
  const data = await response.json().catch(() => null);
  return { ok: response.ok, status: response.status, data };
}

// ---- Views: #configs, #suites, #runs and #run/<id>.
//
// Everything that comes from the server (names, templates, model replies,
// error text) goes into the page through el(), which only ever creates text
// nodes, so a reply containing "<script>" is displayed as text, not run.

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  node.append(...children);  // strings become text nodes
  return node;
}

let routeStamp = 0;      // bumped on every navigation; late responses from an old view are dropped
let pollTimer = null;
let viewsStarted = false;

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = null;
}

function show(stamp, node, focus = false) {
  if (stamp !== routeStamp) return;
  $("view").replaceChildren(node);
  if (focus) $("view").focus();
}

// Re-draw the current view (e.g. after a save) with a message on top.
function rerender(render, arg, notice) {
  stopPolling();
  render(++routeStamp, arg, notice);
}

// A 400 from the API carries `errors` (CSV rows) and/or `detail` (a string, or
// a list of {loc, msg} from request validation). Show every line.
function errorLines(res) {
  const data = res.data;
  const lines = [];
  const add = (item) => {
    if (typeof item === "string") lines.push(item);
    else if (item && item.msg) {
      const where = (item.loc || []).filter((part) => part !== "body").join(".");
      lines.push(where ? `${where}: ${item.msg}` : item.msg);
    } else lines.push(JSON.stringify(item));
  };
  if (data && Array.isArray(data.errors)) data.errors.forEach(add);
  if (data && Array.isArray(data.detail)) data.detail.forEach(add);
  else if (data && data.detail) add(data.detail);
  return lines.length ? lines : [`Request failed (${res.status})`];
}

function errorList(res) {
  return el("ul", { class: "error", role: "alert" }, ...errorLines(res).map((line) => el("li", { text: line })));
}

// notice is a string or a list of strings and nodes (e.g. a "Test this config" link).
function noticeBox(notice) {
  return notice && notice.length ? el("p", { class: "card", role: "status" }, ...[].concat(notice)) : "";
}

// Follow the cursor until the API says there are no more pages.
async function getAll(path) {
  const items = [];
  let cursor = null;
  do {
    const query = `limit=1000${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    const res = await api("GET", `${path}${path.includes("?") ? "&" : "?"}${query}`);
    if (!res.ok) return res;
    items.push(...res.data.items);
    cursor = res.data.cursor;
  } while (cursor);
  return { ok: true, status: 200, data: items };
}

const percent = (fraction) => `${(fraction * 100).toFixed(1)}%`;
const when = (seconds) => new Date(seconds * 1000).toLocaleString();

function table(headers, rows, className = "") {
  return el("div", { class: "table-wrap" }, el("table", { class: className },
    el("thead", {}, el("tr", {}, ...headers.map((name) => el("th", { text: name })))),
    el("tbody", {}, ...rows),
  ));
}

function cell(content, attrs = {}) {
  return el("td", attrs, content);
}

function field(label, control) {
  return el("label", { text: label }, control);
}

// The list endpoints return every version of every config or suite. Latest
// version first, grouped by id.
function groupById(items, idKey) {
  const groups = new Map();
  for (const item of items) {
    if (!groups.has(item[idKey])) groups.set(item[idKey], []);
    groups.get(item[idKey]).push(item);
  }
  for (const versions of groups.values()) versions.sort((a, b) => b.version - a.version);
  return groups;
}

// ---- #configs

async function renderConfigs(stamp, _arg, notice) {
  const [models, configs] = await Promise.all([api("GET", "/api/models"), api("GET", "/api/configs")]);
  if (!models.ok) return show(stamp, errorList(models));
  if (!configs.ok) return show(stamp, errorList(configs));

  const groups = groupById(configs.data, "cfgId");
  const latest = [...groups.values()].map((versions) => versions[0])
    .sort((a, b) => b.created - a.created);

  const target = el("select", { id: "cfg-target" }, el("option", { value: "", text: "A new config" }),
    ...latest.map((cfg) => el("option", { value: cfg.cfgId, text: `New version of ${cfg.name}` })));
  const name = el("input", { id: "cfg-name", type: "text", required: true, value: "My config" });
  const note = el("input", { id: "cfg-note", type: "text", maxlength: 500 });
  const model = el("select", { id: "cfg-model", required: true },
    ...models.data.map((item) => el("option", { value: item.id, text: item.id })));
  const system = el("textarea", { id: "cfg-system" });
  const template = el("textarea", { id: "cfg-template", required: true });
  template.value = "{{input}}";
  // F2 model settings. Limits and defaults match InferenceConfig in api/app.py.
  const maxTokens = el("input", { id: "cfg-max-tokens", type: "number", min: 1, max: 4096, step: 1, required: true, value: 512 });
  const temperature = el("input", { id: "cfg-temperature", type: "number", min: 0, max: 1, step: 0.05, required: true, value: 0 });
  const topP = el("input", { id: "cfg-top-p", type: "number", min: 0, max: 1, step: 0.05, required: true, value: 1 });
  const stops = el("textarea", { id: "cfg-stops", rows: 2 });

  // Picking an existing config starts the form from its latest version, settings
  // included, so a new version doesn't silently fall back to the defaults. Picking
  // "A new config" again resets the form to the same defaults the page starts with.
  const defaults = { name: "My config", modelId: model.value, system: "", template: "{{input}}" };
  target.addEventListener("change", () => {
    const cfg = latest.find((item) => item.cfgId === target.value) || defaults;
    const inference = cfg.inferenceConfig || {};
    name.value = cfg.name;
    note.value = "";  // the note describes the new version, so it never carries over
    model.value = cfg.modelId;
    system.value = cfg.system || "";
    template.value = cfg.template;
    maxTokens.value = inference.maxTokens ?? 512;
    temperature.value = inference.temperature ?? 0;
    topP.value = inference.topP ?? 1;
    stops.value = (inference.stopSequences || []).join("\n");
  });

  const status = el("div");
  const submit = el("button", { type: "submit", text: "Save" });
  const form = el("form", {},
    field("Save as", target), field("Name", name),
    field("What this version changes (optional, shown wherever the version is listed)", note),
    field("Model", model),
    field("System prompt (optional)", system),
    field("Template (must contain {{input}}, which each test case replaces)", template),
    el("div", { class: "settings" },
      field("Max tokens", maxTokens), field("Temperature", temperature), field("Top-p", topP)),
    field("Stop sequences (optional, one per line, up to 4; Nemotron doesn't support them)", stops),
    submit, status,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    const path = target.value ? `/api/configs/${encodeURIComponent(target.value)}/versions` : "/api/configs";
    const res = await api("POST", path, {
      name: name.value.trim(), note: note.value.trim(), modelId: model.value, system: system.value, template: template.value,
      inferenceConfig: {
        maxTokens: Number(maxTokens.value), temperature: Number(temperature.value), topP: Number(topP.value),
        stopSequences: stops.value.split("\n").map((line) => line.trim()).filter(Boolean),
      },
    });
    submit.disabled = false;
    if (res.ok) {
      // The link opens Runs with this version already chosen (item 2).
      const runLink = el("a", { href: `#runs/${encodeURIComponent(`${res.data.configId}|${res.data.version}`)}`, text: "Test this config" });
      rerender(renderConfigs, null, [`Saved ${res.data.configId} as version ${res.data.version}. `, runLink]);
    } else if (res.status !== 401) status.replaceChildren(errorList(res));
  });

  // The name expands to the latest version's prompt and settings, so the table stays
  // narrow enough for 600 px screenshots.
  const details = (cfg) => {
    const inference = cfg.inferenceConfig || {};
    const settings = `max ${inference.maxTokens ?? 512}, temperature ${inference.temperature ?? 0}, top-p ${inference.topP ?? 1}`
      + ((inference.stopSequences || []).length ? `, stop ${inference.stopSequences.join(" / ")}` : "");
    return el("details", {}, el("summary", { text: cfg.name }),
      el("p", { class: "output" }, el("b", { text: "System: " }), cfg.system || "(none)"),
      el("p", { class: "output" }, el("b", { text: "Template: " }), cfg.template),
      el("p", { class: "muted", text: settings }),
      versionHistory(groups.get(cfg.cfgId), (item) => item.modelId));
  };
  const rows = latest.map((cfg) => el("tr", {},
    cell(details(cfg)), cell(cfg.modelId), cell(`v${cfg.version}`),
    cell(`${groups.get(cfg.cfgId).length}`), cell(cfg.cfgId)));
  show(stamp, el("div", {},
    noticeBox(notice),
    el("p", { class: "muted", text: "PromptGate tests an LLM prompt against a suite of cases and flags a change that makes it worse. "
      + "A config is the prompt and model settings; a suite is the test cases (Suites); a run scores one against the other (Runs)." }),
    el("h2", { text: "Configs" }),
    rows.length ? table(["Name", "Model", "Latest", "Versions", "ID"], rows)
      : el("p", { class: "muted", text: "No configs yet. Create one below." }),
    el("h2", { text: "New config or version" }), form,
  ), true);
}

// One line per version, oldest first, with its change note, so a reader can see
// what v2 and v3 are without opening each one.
function versionHistory(versions, extra) {
  const lines = [...versions].sort((a, b) => a.version - b.version).map((item) =>
    el("li", {}, el("b", { text: `v${item.version}` }), ` (${extra(item)}): ${item.note || "no note"}`));
  return el("div", {}, el("b", { text: "Versions" }), el("ul", { class: "muted" }, ...lines));
}

// "name vN: note", the label used wherever one version is picked or shown.
const versionLabel = (item) => `${item.name} v${item.version}${item.note ? `: ${item.note}` : ""}`;

// ---- #suites

async function renderSuites(stamp, _arg, notice) {
  const suites = await api("GET", "/api/suites");
  if (!suites.ok) return show(stamp, errorList(suites));

  const groups = groupById(suites.data, "suiteId");
  const latest = [...groups.values()].map((versions) => versions[0])
    .sort((a, b) => a.name.localeCompare(b.name));
  const own = latest.filter((suite) => !suite.suiteId.startsWith("pub_"));

  const target = el("select", { id: "suite-target" }, el("option", { value: "", text: "A new suite" }),
    ...own.map((suite) => el("option", { value: suite.suiteId, text: `New version of ${suite.name}` })));
  const name = el("input", { id: "suite-name", type: "text" });
  const note = el("input", { id: "suite-note", type: "text", maxlength: 500 });
  const file = el("input", { id: "suite-file", type: "file", accept: ".csv,text/csv", required: true });
  const status = el("div");
  const submit = el("button", { type: "submit", text: "Import CSV" });
  const form = el("form", {},
    field("Import as", target),
    field("Name (optional; a new version keeps the suite's name)", name),
    field("What this version changes (optional)", note),
    field("CSV file (columns: input, expected, scorer, pattern; optional description = the check in plain words)", file),
    submit, status,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!file.files.length) return;
    submit.disabled = true;
    const text = await file.files[0].text();
    const params = new URLSearchParams();
    if (name.value.trim()) params.set("name", name.value.trim());
    if (note.value.trim()) params.set("note", note.value.trim());
    const query = params.size ? `?${params}` : "";
    const path = target.value ? `/api/suites/${encodeURIComponent(target.value)}/versions` : "/api/suites";
    const res = await api("POST", path + query, text);
    submit.disabled = false;
    if (res.ok) rerender(renderSuites, null, `Imported ${res.data.suiteId} as version ${res.data.version}.`);
    else if (res.status !== 401) status.replaceChildren(errorList(res));
  });

  const rows = latest.map((suite) => el("tr", {},
    cell(el("details", {}, el("summary", { text: suite.name }),
      versionHistory(groups.get(suite.suiteId), (item) => `${item.caseCount} cases`))),
    cell(suite.suiteId.startsWith("pub_") ? "sample" : "yours"),
    cell(`v${suite.version}`), cell(`${suite.caseCount}`), cell(suite.suiteId)));
  show(stamp, el("div", {},
    noticeBox(notice),
    el("h2", { text: "Suites" }),
    table(["Name", "Kind", "Latest", "Cases", "ID"], rows),
    el("h2", { text: "Import a suite" }), form,
  ), true);
}

// ---- #runs

// Reused while the last attempt got a 503, so a retry cannot start a second run (M3).
let retryAttempt = null;

// Latest version of each config or suite first; older versions stay selectable
// (N6: a run can pin any version) but sit in their own group.
function versionSelect(id, items, idKey, label, order) {
  const latest = [];
  const older = [];
  for (const versions of groupById(items, idKey).values()) {
    latest.push(versions[0]);
    older.push(...versions.slice(1));
  }
  const option = (item) => el("option", { value: `${item[idKey]}|${item.version}`, text: label(item) });
  return el("select", { id, required: true },
    ...latest.sort(order).map(option),
    older.length ? el("optgroup", { label: "Older versions" }, ...older.sort(order).map(option)) : "");
}

async function renderRuns(stamp, chosen, notice) {
  const [configs, suites, runs] = await Promise.all([
    api("GET", "/api/configs"), api("GET", "/api/suites"), api("GET", "/api/runs"),
  ]);
  for (const res of [configs, suites, runs]) if (!res.ok) return show(stamp, errorList(res));

  const cfgVersions = configs.data;
  const suiteVersions = suites.data;
  const config = versionSelect("run-config", cfgVersions, "cfgId",
    (cfg) => `${versionLabel(cfg)} (${cfg.modelId})`,
    (a, b) => b.created - a.created || b.version - a.version);
  const suite = versionSelect("run-suite", suiteVersions, "suiteId",
    (item) => `${versionLabel(item)} (${item.caseCount} cases)`,
    (a, b) => a.name.localeCompare(b.name) || b.version - a.version);
  // #runs/<cfgId>|<v> from "Test this config", or #runs/<cfgId>|<v>|<suiteId>|<v>
  // from "Run again" on a run page. Only versions that exist in the list are chosen.
  const [cfgId, cfgV, suiteId, suiteV] = (chosen || "").split("|");
  const pick = (select, value) => {
    if ([...select.options].some((option) => option.value === value)) select.value = value;
  };
  if (cfgV) pick(config, `${cfgId}|${cfgV}`);
  if (suiteV) pick(suite, `${suiteId}|${suiteV}`);
  const suiteNames = new Map(suiteVersions.map((item) => [item.suiteId, item.name]));
  const mode = el("select", { id: "run-mode" },
    el("option", { value: "replay", text: "replay: reuse saved replies (calls the model only for new ones)" }),
    el("option", { value: "fresh", text: "fresh: call the model for every case (needed for a baseline)" }),
    el("option", { value: "auto", text: "auto: replay if a fresh run happened recently, else fresh" }));
  const status = el("div");
  const submit = el("button", { type: "submit", text: "Start run", disabled: !cfgVersions.length || !suiteVersions.length });
  const form = el("form", {}, field("Config", config), field("Suite", suite), field("Mode", mode), submit, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const [configId, configVersion] = config.value.split("|");
    const [suiteId, suiteVersion] = suite.value.split("|");
    const request = { configId, configVersion: Number(configVersion), suiteId, suiteVersion: Number(suiteVersion), mode: mode.value };
    // A new key per click, except to retry the identical request after a 503.
    const signature = JSON.stringify(request);
    const key = retryAttempt && retryAttempt.signature === signature ? retryAttempt.key : crypto.randomUUID();
    submit.disabled = true;
    const res = await api("POST", "/api/runs", { ...request, idempotencyKey: key });
    submit.disabled = false;
    if (res.status === 202) {
      retryAttempt = null;
      location.hash = `#run/${res.data.runId}`;
    } else if (res.status === 503) {
      retryAttempt = { signature, key };
      status.replaceChildren(el("p", { class: "error", role: "alert",
        text: "The run was saved, but the queue did not accept it. Click Start run again to retry it." }));
    } else if (res.status !== 401) {
      retryAttempt = null;
      status.replaceChildren(errorList(res));
    }
  });

  const runsTable = (items) => {
    const rows = [...items].sort((a, b) => b.created - a.created).map((run) => el("tr", {},
      cell(when(run.created)), cell(`${run.config.name} v${run.configVersion}`),
      cell(`${suiteNames.get(run.suiteId) || run.suiteId} v${run.suiteVersion}`), cell(run.effectiveMode),
      cell(run.status, { class: statusClass(run.status) }),
      cell(`${run.passed} / ${run.total}`), cell(percent(run.passRate)),
      cell(el("a", { href: `#run/${encodeURIComponent(run.runId)}`, text: "Open" }))));
    return rows.length ? table(["Started", "Config", "Suite", "Mode", "Status", "Passed", "Pass rate", ""], rows)
      : el("p", { class: "muted", text: "No runs yet." });
  };
  const runsSection = el("div", {}, runsTable(runs.data));
  show(stamp, el("div", {},
    noticeBox(notice),
    el("h2", { text: "Start a run" }),
    cfgVersions.length && suiteVersions.length ? form
      : el("p", { class: "muted", text: "Create a config and a suite first." }),
    el("h2", { text: "Runs" }),
    runsSection,
  ), true);

  // While any run is QUEUED or RUNNING, refresh only the table every 3 s, so the
  // form above keeps its selections. Stops when every run is final or the view changes.
  const pending = (items) => items.some((run) => run.status !== "COMPLETE" && run.status !== "INCOMPLETE");
  const refresh = async () => {
    const res = await api("GET", "/api/runs");
    if (stamp !== routeStamp) return;
    if (res.ok) runsSection.replaceChildren(runsTable(res.data));
    // A failed refresh (e.g. a 5xx while ECS replaces the API task) just tries again.
    if (!res.ok || pending(res.data)) pollTimer = setTimeout(refresh, 3000);
  };
  if (stamp === routeStamp && pending(runs.data)) pollTimer = setTimeout(refresh, 3000);
}

const statusClass = (status) => (status === "COMPLETE" ? "pass" : status === "INCOMPLETE" ? "fail" : "pending");
const verdictClass = (verdict) => (verdict === "PASS" ? "pass" : "fail");

// ---- #run/<id>: polls every 2 s until COMPLETE or INCOMPLETE.

function renderRun(stamp, runId) {
  let run = null;
  let results = null;
  let cases = new Map();    // caseId -> the case (input, scorer, description), for the results table
  let suite = null;         // the run's suite version, for its name and change note
  let baseline = { current: null, verdicts: null };  // current baseline run ID; caseId -> verdict in run.baselineRunId
  let notice = "";
  let firstDraw = true;

  const draw = () => {
    show(stamp, runPage(run, results, cases, suite, baseline, notice, markBaseline), firstDraw);
    firstDraw = false;
  };

  const markBaseline = async () => {
    const res = await api("PUT", `/api/runs/${encodeURIComponent(run.runId)}/baseline`);
    if (res.ok) baseline = { ...baseline, current: run.runId };
    else if (res.status !== 401) notice = errorLines(res).join(" ");
    draw();
  };

  const tick = async () => {
    try {
      const res = await api("GET", `/api/runs/${encodeURIComponent(runId)}`);
      if (stamp !== routeStamp) return;
      // A 5xx (e.g. the load balancer while ECS replaces or adds tasks) is
      // temporary, so keep polling; a 4xx such as 404 will not change.
      if (!res.ok && res.status < 500) return show(stamp, errorList(res), true);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      run = res.data;
      notice = "";
      if (run.status === "COMPLETE" || run.status === "INCOMPLETE") {
        // Result items don't store the input, so read it from the run's pinned suite version.
        // The baseline this run was compared with (run.baselineRunId, fixed when it started)
        // gives each case's earlier verdict; the current baseline may be newer.
        const baselinePath = `/api/baselines/${encodeURIComponent(run.cfgId)}/${encodeURIComponent(run.suiteId)}/${run.suiteVersion}`;
        const [all, items, suites, current, earlier] = await Promise.all([
          getAll(`/api/runs/${encodeURIComponent(runId)}/results`),
          getAll(`/api/suites/${encodeURIComponent(run.suiteId)}/versions/${run.suiteVersion}/cases`),
          api("GET", "/api/suites"),
          api("GET", baselinePath),
          run.baselineRunId ? getAll(`/api/runs/${encodeURIComponent(run.baselineRunId)}/results`) : null,
        ]);
        if (stamp !== routeStamp) return;
        results = all.ok ? all.data : [];
        if (!all.ok) notice = errorLines(all).join(" ");
        // Without the cases the table still works; the Check and Input columns are just empty.
        if (items.ok) cases = new Map(items.data.map((item) => [item.caseId, item]));
        if (suites.ok) suite = suites.data.find((item) => item.suiteId === run.suiteId && item.version === run.suiteVersion) || null;
        baseline = {
          current: current.ok ? current.data.runId : null,
          verdicts: earlier && earlier.ok ? new Map(earlier.data.map((item) => [item.caseId, item.verdict])) : null,
        };
        return draw();
      }
      draw();
    } catch {
      notice = "Connection problem; retrying.";
      if (stamp === routeStamp) {
        if (run) draw();
        else show(stamp, noticeBox(notice));
      }
    }
    if (stamp === routeStamp) pollTimer = setTimeout(tick, 2000);
  };
  return tick();
}

// The baseline rate, plus the change in percentage points once the run is final.
// Below baseline is the same check as the CLI gate with tolerance 0 (core/gate.py).
function baselineStat(run, final) {
  const label = el("span", { class: "muted", text: "Baseline when started" });
  if (run.baselineRate == null) return el("div", {}, label, el("b", { text: "none" }));
  if (!final) return el("div", {}, label, el("b", { text: percent(run.baselineRate) }));
  const points = (run.passRate - run.baselineRate) * 100;
  const below = points < -1e-9;
  return el("div", {}, label,
    el("b", { text: percent(run.baselineRate) }),
    el("span", { class: below ? "fail" : "pass",
      text: `${points >= 0 ? "+" : ""}${points.toFixed(1)} pts${below ? ", below baseline" : ""}` }));
}

// What a case checks: its description when the CSV gave one, else the raw scorer.
function checkText(item) {
  if (!item) return "";
  if (item.description) return item.description;
  return `${item.scorer} ${item.scorer.endsWith("regex") ? `/${item.pattern}/` : JSON.stringify(item.expected)}`;
}

// A failed regex's reason repeats the whole pattern and the reply's first 80
// characters, both already on the row; the full reason stays on hover.
function detailCell(item) {
  const plain = item.reason.startsWith("no match for /")
    ? "The reply did not match this check's pattern (hover to see it)." : item.reason;
  return cell(plain, { class: "muted detail", title: plain === item.reason ? null : item.reason });
}

// How a case moved since the baseline this run was compared with.
function changeSince(was, now) {
  if (was == null) return { text: "", order: 2 };
  if (was === "PASS" && now !== "PASS") return { text: "regressed (was PASS)", cls: "fail", order: 0 };
  if (was !== "PASS" && now === "PASS") return { text: `fixed (was ${was})`, cls: "pass", order: 3 };
  return { text: `same (${was})`, cls: "muted", order: now === "PASS" ? 4 : 1 };
}

// Which run is the baseline, and whether this run was compared with it.
function baselineInfo(run, baseline) {
  const link = (id) => el("a", { href: `#run/${encodeURIComponent(id)}`, text: id });
  if (baseline.current === run.runId) {
    return el("p", { class: "card pass" }, "This run is the current baseline: later runs of this config on this suite version are compared with it.");
  }
  if (!baseline.current) {
    return el("p", { class: "muted" }, "No baseline yet. Mark a complete fresh run with no errors as the baseline, and later runs of this config on this suite version are compared with it.");
  }
  const lines = ["Current baseline: ", link(baseline.current), "."];
  if (!run.baselineRunId) lines.push(" This run started before it was marked, so it isn't compared; use Run again to compare.");
  else if (run.baselineRunId !== baseline.current) lines.push(" This run was compared with an earlier baseline, ", link(run.baselineRunId), ".");
  return el("p", { class: "muted" }, ...lines);
}

function runPage(run, results, cases, suite, baseline, notice, onMarkBaseline) {
  const final = run.status === "COMPLETE" || run.status === "INCOMPLETE";
  const stat = (label, value) => el("div", {}, el("span", { class: "muted", text: label }), el("b", { text: value }));
  const totals = el("div", { class: "card totals" },
    el("div", {}, el("span", { class: "muted", text: "Status" }), el("b", { class: statusClass(run.status), text: run.status })),
    stat("Finished", `${run.done + run.errored} / ${run.total}`),
    stat("Passed", `${run.passed}`),
    stat("Errored", `${run.errored}`),
    stat("Pass rate", percent(run.passRate)),
    baselineStat(run, final),
    stat("Mode", run.effectiveMode === run.mode ? run.mode : `${run.mode} (ran as ${run.effectiveMode})`),
    stat("Model calls", `${run.freshCalls}`),
    stat("Cache hits", `${run.cacheHits}`),
    el("div", {}, el("span", { class: "muted", text: "Tokens in / out" }), el("b", { text: `${run.tokensIn} / ${run.tokensOut}` }),
      final && run.freshCalls === 0 && run.cacheHits > 0 ? el("span", { class: "muted", text: "all from cache, no model cost" }) : ""),
  );

  const isBaseline = baseline.current === run.runId;
  const canBaseline = run.status === "COMPLETE" && run.effectiveMode === "fresh" && run.errored === 0 && !isBaseline;
  const markButton = el("button", {
    type: "button", text: isBaseline ? "Current baseline" : "Mark baseline", disabled: !canBaseline, onclick: onMarkBaseline,
    title: canBaseline || isBaseline ? "" : "Only a complete fresh run with no errors can be the baseline",
  });
  // Opens the Runs form with this run's config and suite versions chosen; you pick
  // the mode and start it, so a stray click never spends model calls.
  const again = `${run.cfgId}|${run.configVersion}|${run.suiteId}|${run.suiteVersion}`;
  const runAgain = el("a", { class: "button", href: `#runs/${encodeURIComponent(again)}`, text: "Run again" });

  let resultsSection = el("p", { class: "muted", text: "Results appear here when the run finishes." });
  if (results) {
    // Regressions, then other failures and errors, so they are the rows you see (and
    // screenshot) first. Without a baseline, simply failures before passes.
    const compared = baseline.verdicts;
    const rowsOf = results.map((item) => ({
      item, change: changeSince(compared ? compared.get(item.caseId) : null, item.verdict),
    }));
    rowsOf.sort((a, b) => compared ? a.change.order - b.change.order
      : (a.item.verdict === "PASS") - (b.item.verdict === "PASS"));
    const headers = ["Case", "Check", "Verdict", ...(compared ? ["vs baseline"] : []), "Scorer detail",
      "Input", "Source (tokens in / out)", "Latency (ms)", "Output"];
    const counts = rowsOf.reduce((sum, { change }) => {
      if (change.order === 0) sum.regressed++;
      if (change.order === 3) sum.fixed++;
      return sum;
    }, { regressed: 0, fixed: 0 });
    resultsSection = results.length ? el("div", {},
      compared ? el("p", {}, el("b", { class: counts.regressed ? "fail" : "pass", text: `${counts.regressed} regressed` }),
        `, ${counts.fixed} fixed since baseline run ${run.baselineRunId}.`) : "",
      table(headers, rowsOf.map(({ item, change }) => el("tr", {},
        cell(item.caseId), cell(el("b", { text: checkText(cases.get(item.caseId)) })),
        cell(item.verdict, { class: verdictClass(item.verdict) }),
        compared ? cell(change.text, { class: `${change.cls || ""} change` }) : "",
        detailCell(item),
        cell(cases.get(item.caseId)?.input || "", { class: "output" }),
        cell(`${item.source}\n${item.tokensIn ?? 0} / ${item.tokensOut ?? 0}`, { class: "output" }),
        cell(item.latencyMs == null ? "" : `${Math.round(item.latencyMs)}`),
        cell(item.truncated ? `${item.output}\n[output truncated]` : item.output, { class: "output" }))), "results"))
      : el("p", { class: "muted", text: "No results were recorded." });
  }

  return el("div", {},
    el("p", {}, el("a", { href: "#runs", text: "← Runs" })),
    el("h2", { text: `${run.config.name} v${run.configVersion} on ${suite ? suite.name : run.suiteId} v${run.suiteVersion}` }),
    run.config.note ? el("p", {}, el("b", { text: `Config v${run.configVersion} note: ` }), run.config.note) : "",
    suite && suite.note ? el("p", {}, el("b", { text: `Suite v${run.suiteVersion} note: ` }), suite.note) : "",
    el("p", { class: "muted", text: `Run ${run.runId}, started ${when(run.created)}` }),
    run.status === "INCOMPLETE"
      ? el("p", { class: "error", role: "alert", text: "The run reached its deadline before every case finished." }) : "",
    noticeBox(notice),
    totals,
    final ? baselineInfo(run, baseline) : "",
    el("p", { class: "actions" }, markButton, runAgain),
    el("h2", { text: "Results" }),
    final ? resultsSection : el("p", { class: "muted", text: "Updating every 2 seconds…" }),
  );
}

function route() {
  if (!tokens) return;
  stopPolling();
  const stamp = ++routeStamp;
  const [name, arg] = (location.hash.slice(1) || "configs").split("/");
  const views = { configs: renderConfigs, suites: renderSuites, runs: renderRuns, run: renderRun };
  const render = views[name] || renderConfigs;
  const section = name === "run" ? "runs" : views[name] ? name : "configs";
  for (const link of document.querySelectorAll("#nav a")) {
    if (link.getAttribute("href") === `#${section}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("view").replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  render(stamp, arg ? decodeURIComponent(arg) : arg);
}

function startViews() {
  if (!viewsStarted) {
    window.addEventListener("hashchange", route);
    viewsStarted = true;
  }
  route();
}

// ---- Wiring

async function init() {
  config = await (await fetch("/config.json")).json();
  const local = config.authMode === "local";
  $("cognito-fields").hidden = local;
  $("cognito-fields").disabled = local;
  $("local-fields").hidden = !local;
  $("local-fields").disabled = !local;

  $("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(form, true);
    showError("");
    try {
      if (local) {
        const name = $("local-name").value.trim();
        startSession({ id: `local-${name}`, refresh: null }, name);
      } else {
        await cognitoSignIn($("username").value.trim(), $("password").value);
      }
    } catch (error) {
      showError(error.message);
    } finally {
      setBusy(form, false);
    }
  });

  $("newpass-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if ($("new-password").value !== $("new-password-confirm").value) {
      showError("The two passwords do not match.");
      return;
    }
    setBusy(form, true);
    showError("");
    try {
      await cognitoNewPassword($("new-password").value);
    } catch (error) {
      // Typically the pool's password rules; the challenge Session may still be valid.
      showError(error.message);
    } finally {
      setBusy(form, false);
    }
  });

  $("signout").addEventListener("click", () => signOut(""));
  showForm("signin");
}

init().catch(() => {
  $("signin").hidden = false;
  showError("Could not load the dashboard settings. Please reload the page.");
});
