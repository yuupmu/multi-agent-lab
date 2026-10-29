"use strict";

// API keys are submitted separately and never included in saved configurations.
const $ = id => document.getElementById(id);
$('toggle-rules').addEventListener('click', () => {
  const panel = $('grading-rules');
  panel.hidden = !panel.hidden;
  $('toggle-rules').setAttribute('aria-expanded', String(!panel.hidden));
});
const configForm = $("configuration");
const storageKey = "research-project-monitor-configuration-v2";
const monitorIds = [1, 2, 3];
const commonMonitorPrompt = "전달받은 프로젝트 상태를 담당 관점으로 검토하고, ‘계속 진행 / 추가 검증 / 인간 판단 필요’ 중 하나를 제안한다. 인간 판단이 필요하면 근거·질문·영향받는 작업을 제시한다.";
const fields = ["run-mode", "project-name", "main-model", "main-prompt",
  ...monitorIds.flatMap(id => [`concept-${id}`, `model-${id}`, `instructions-${id}`])];
const limits = { "project-name": 120, "main-prompt": 20000 };
for (const id of monitorIds) { limits[`concept-${id}`] = 80; limits[`instructions-${id}`] = 12000; }

function readConfiguration() {
  return {
    version: 4,
    mode: $('run-mode').value,
    architecture: "main-agent-with-three-observers",
    project: { name: $("project-name").value.trim() },
    mainAgent: { model: $("main-model").value, prompt: $("main-prompt").value },
    monitors: monitorIds.map(id => ({ id, model: $("model-" + id).value,
      concept: $("concept-" + id).value.trim(), instructions: $("instructions-" + id).value })),
    workflow: { observeDuringExecution: true, waitForAllMonitors: false,
      deduplicateRequests: true, pauseAffectedTasksOnly: true, verifyAfterHumanDecision: true,
      benchmarkTrigger: "manual_after_project_completion" }
  };
}
function updateConceptLabels() {
  for (const id of monitorIds) $("monitor-name-" + id).textContent = $("concept-" + id).value.trim() || "미입력";
}
function validInputs() {
  for (const id of fields) {
    const input = $(id);
    input.setCustomValidity(input.value.trim() ? "" : "공백이 아닌 내용을 입력하세요.");
  }
  return configForm.reportValidity();
}
function restoreConfiguration(config, migrateSaved = false) {
  if (!config || ![2, 3, 4].includes(config.version) || config.architecture !== "main-agent-with-three-observers" ||
      !Array.isArray(config.monitors) || config.monitors.length !== 3) throw new Error("Unsupported configuration");
  const aliases = {'GPT-6.1 Sol':'gpt-6.1-sol', 'GPT-6 Astra':'gpt-6-astra', 'GPT-6 Luna':'gpt-6-luna'};
  const values = { 'run-mode': config.mode || 'openai', "project-name": config.project?.name, "main-model": aliases[config.mainAgent?.model] || config.mainAgent?.model, "main-prompt": config.mainAgent?.prompt };
  for (const id of monitorIds) {
    const matches = config.monitors.filter(m => m && m.id === id);
    if (matches.length !== 1) throw new Error("Invalid monitor configuration");
    const monitor = matches[0];
    values[`concept-${id}`] = monitor.concept;
    values[`model-${id}`] = aliases[monitor.model] || monitor.model;
    values[`instructions-${id}`] = monitor.instructions;
    if (migrateSaved && config.version === 2 && typeof monitor.instructions === 'string' &&
        !monitor.instructions.includes(commonMonitorPrompt)) {
      values[`instructions-${id}`] += '\n' + commonMonitorPrompt;
    }
  }
  // Validate every field before applying any saved values.
  for (const [id, value] of Object.entries(values)) {
    if (typeof value !== "string" || !value.trim() || (limits[id] && value.length > limits[id])) throw new Error("Invalid saved value");
    const input = $(id);
    if (input.tagName === "SELECT" && !Array.from(input.options).some(option => option.value === value)) throw new Error("Unknown model");
  }
  for (const [id, value] of Object.entries(values)) $(id).value = value;
  updateConceptLabels();
}

for (const id of fields) {
  $(id).addEventListener("input", () => {
    $(id).setCustomValidity("");
    updateConceptLabels();
    $("config-status").textContent = "변경됨";
  });
}
configForm.addEventListener("submit", event => {
  event.preventDefault();
  if (!validInputs()) return;
  try {
    localStorage.setItem(storageKey, JSON.stringify(readConfiguration()));
    $("config-status").textContent = "저장됨";
  } catch {
    $("config-status").textContent = "저장 실패 · JSON 내려받기 가능";
  }
});
$("export-config").addEventListener("click", () => {
  if (!validInputs()) return;
  const blob = new Blob([JSON.stringify(readConfiguration(), null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "project-monitor-config.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
try {
  const saved = localStorage.getItem(storageKey);
  if (saved) {
    restoreConfiguration(JSON.parse(saved), true);
    $("config-status").textContent = "불러옴";
  }
} catch {
  $("config-status").textContent = "불러오기 실패";
}

let online = false;
let currentRun = null;
let selectedRequest = null;
let formRequest = null;
let lastRevision = null;
let busy = false;
let operationVersion = 0;
let apiConnected = false;
let modelList = '';
const activeStates = ['running', 'waiting', 'applying'];
const statusLabels = {waiting:'답변 대기', answered:'답변받음', applying:'반영 중', applied:'정책 반영 확인',
  completed:'완료', paused:'계속하기 대기', stopped:'중단', interrupted:'중단됨', failed:'실패', running:'진행 중', not_run:'채점 전'};
const decisionLabels = {human:'인간 판단 필요', continue:'계속 진행', verify:'추가 검증'};
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '요청 실패');
  return data;
}
function controls() {
  const running = currentRun && activeStates.includes(currentRun.status);
  const testing = currentRun && currentRun.test.status === 'running';
  const resumable = currentRun?.mode === 'openai' && ['paused', 'interrupted'].includes(currentRun.status);
  $('resume-run').hidden = !resumable;
  $('resume-run').disabled = !online || busy || !resumable || !apiConnected;
  for (const id of fields) $(id).disabled = !!running;
  $('start-run').disabled = !online || busy || running || testing || ($('run-mode').value === 'openai' && !apiConnected);
  $('api-key').disabled = !online || busy || running;
  $('connect-api').disabled = !online || busy || running;
  $('disconnect-api').disabled = !online || busy || running || !apiConnected;
  $('stop-run').disabled = !online || busy || !running;
  $('test-project').disabled = !online || busy || !currentRun || currentRun.status !== 'completed' || testing;
  $('download-run').disabled = !currentRun;
  $('download-code').disabled = !currentRun || !currentRun.artifact;
  const request = currentRun?.requests.find(r => r.id === selectedRequest);
  const canAnswer = online && !busy && (running || resumable) && request?.status === 'waiting';
  $('submit-answer').disabled = !canAnswer;
  $('human-answer').disabled = !canAnswer;
  for (const input of $('answer-fields').querySelectorAll('input,select,textarea')) input.disabled = !canAnswer;
}
function answerForm() {
  const request = currentRun?.requests.find(r => r.id === selectedRequest);
  if (!request) {
    $('selected-question').textContent = '요청 없음';
    $('answer-fields').replaceChildren();
    formRequest = null;
    return;
  }
  $('selected-question').textContent = request.question;
  if (formRequest !== request.id) {
    $('answer-fields').replaceChildren();
    $('human-answer').value = '';
    for (const field of request.fields) {
      const label = document.createElement('label');
      label.textContent = field.label;
      const input = document.createElement(field.type === 'boolean' ? 'select' : field.type === 'text' ? 'textarea' : 'input');
      input.id = 'policy-' + field.name;
      input.dataset.field = field.name;
      input.required = !['dates', 'weekdays'].includes(field.type);
      if (field.type === 'boolean') {
        for (const [value, text] of [['','선택'],['true','예'],['false','아니요']]) {
          const option = document.createElement('option'); option.value = value; option.textContent = text; input.append(option);
        }
      } else if (field.type === 'text') {
        input.rows = 3; input.maxLength = 12000;
      } else {
        input.type = ['dates', 'weekdays'].includes(field.type) ? 'text' : field.type;
        if (field.type === 'number') { input.min = field.min; input.max = field.max; input.step = '1'; }
        if (field.type === 'time') input.step = '1';
      }
      if (request.answer) {
        const value = request.answer[field.name];
        input.value = Array.isArray(value) ? value.join(', ') : String(value);
      }
      label.append(input); $('answer-fields').append(label);
    }
    formRequest = request.id;
  }
}
function render(state) {
  currentRun = state;
  if (!state) { controls(); return; }
  if (lastRevision === state.id + ':' + state.revision) { controls(); return; }
  lastRevision = state.id + ':' + state.revision;
  $('run-phase').textContent = state.phase + (['failed','interrupted','stopped'].includes(state.status) ? ' · ' + statusLabels[state.status] : '');
  $('active-tasks').textContent = state.tasks.filter(t => ['running','pending'].includes(t.status)).map(t => t.name).join(', ') || '없음';
  $('blocked-tasks').textContent = state.tasks.filter(t => t.status === 'blocked').map(t => t.name).join(', ') || '없음';
  $('code-version').textContent = 'v' + state.code_version;
  $('shared-goal').textContent = state.config.project.name;
  $('shared-plan').textContent = state.plan.join(' → ');
  $('shared-assumptions').textContent = (state.assumptions || state.requests.filter(r => r.status === 'waiting').map(r => r.key)).join(', ') || '없음';
  $('shared-decisions').textContent = state.decisions.map(d => d.key).join(', ') || '없음';
  $('shared-tests').textContent = state.test.report ? state.test.report.score + '/100' : statusLabels[state.test.status] || state.test.status;
  for (const monitor of state.monitors) {
    $('monitor-name-' + monitor.id).textContent = state.config.monitors.find(m => m.id === monitor.id).concept;
    $('monitor-state-' + monitor.id).textContent = monitor.status === 'observing' ? '관찰 중' : decisionLabels[monitor.decision] || (monitor.status === 'idle' ? '관찰 전' : '오류');
    $('monitor-state-' + monitor.id).title = monitor.reason || '';
  }
  $('request-count').textContent = state.requests.length + '건';
  $('request-rows').replaceChildren();
  for (const request of state.requests) {
    const tr = document.createElement('tr');
    const first = document.createElement('td');
    const button = document.createElement('button'); button.textContent = request.id.slice(0,6);
    button.type = 'button';
    button.addEventListener('click', () => { selectedRequest = request.id; answerForm(); controls(); });
    first.append(button); tr.append(first);
    for (const text of [request.sources.join(', '), request.question + ' ' + request.reason,
      request.affected_tasks.join(', '), request.priority === 'high' ? '높음' : '보통',
      request.status === 'applied' && state.mode === 'openai' ? '코드 반영 보고' : statusLabels[request.status] || request.status]) {
      const td = document.createElement('td'); td.textContent = text; tr.append(td);
    }
    $('request-rows').append(tr);
  }
  if (!state.requests.length) {
    const row = document.createElement('tr'), td = document.createElement('td'); td.colSpan=6; td.textContent='없음'; row.append(td); $('request-rows').append(row);
  }
  if (!selectedRequest || !state.requests.some(r => r.id === selectedRequest)) selectedRequest = state.requests.find(r => r.status === 'waiting')?.id || null;
  answerForm();
  $('decision-record').textContent = state.decisions.length + '건';
  $('applied-version').textContent = state.requests.filter(r => r.applied_version).map(r => r.key + ': v' + r.applied_version).join(', ') || '없음';
  $('verification-result').textContent = state.verification ? (state.verification.type === 'syntax_and_public_checks' ?
    '문법 확인 · 자체 검증 ' + state.verification.correct + '/' + state.verification.total : '정책 값·문법 확인') + ' (v' + state.verification.code_version + ')' : '없음';
  $('test-status').textContent = state.test.error || statusLabels[state.test.status] || state.test.status;
  $('score-value').textContent = state.test.report ? state.test.report.score + ' / ' + state.test.report.total : '— / 100';
  $('test-details').hidden = !state.test.report;
  $('category-scores').replaceChildren();
  if (state.test.report) for (const [name, score] of Object.entries(state.test.report.categories)) {
    const p = document.createElement('p'); p.textContent = name + ': ' + score.correct + '/' + score.total; $('category-scores').append(p);
  }
  $('event-log').replaceChildren();
  for (const event of state.events.slice(-100)) {
    const li = document.createElement('li');
    li.textContent = new Date(event.at).toLocaleTimeString('ko-KR', {timeZone:'Asia/Seoul',hour12:false}) + ' [' + event.source + '] ' + event.message;
    $('event-log').append(li);
  }
  controls();
}
async function operation(action) {
  if (busy) return;
  operationVersion += 1;
  busy = true; controls(); $('operation-status').textContent = '';
  try { await action(); } catch (error) { $('operation-status').textContent = error.message; }
  finally { busy = false; controls(); }
}
$('start-run').addEventListener('click', () => {
  if (!validInputs()) return;
  operation(async () => { selectedRequest = null; formRequest = null; render(await api('/api/runs', readConfiguration())); });
});
$('stop-run').addEventListener('click', () => operation(async () => render(await api('/api/runs/' + currentRun.id + '/stop', {}))));
$('resume-run').addEventListener('click', () => operation(async () => render(await api('/api/runs/' + currentRun.id + '/resume', {}))));
$('test-project').addEventListener('click', () => operation(async () => render(await api('/api/runs/' + currentRun.id + '/test', {}))));
$('human-form').addEventListener('submit', event => {
  event.preventDefault();
  const request = currentRun?.requests.find(r => r.id === selectedRequest);
  if (!request || request.status !== 'waiting' || !$('human-form').reportValidity()) return;
  const answer = {};
  for (const field of request.fields) {
    const value = $('policy-' + field.name).value;
    answer[field.name] = field.type === 'boolean' ? value === 'true' : field.type === 'number' ? Number(value) :
      field.type === 'dates' ? value.split(',').map(x => x.trim()).filter(Boolean) :
      field.type === 'weekdays' ? value.split(',').map(x => x.trim()).filter(Boolean).map(Number) : value;
  }
  operation(async () => {
    const state = await api('/api/runs/' + currentRun.id + '/requests/' + request.id + '/answer', {answer, note:$('human-answer').value});
    selectedRequest = state.requests.find(r => r.status === 'waiting')?.id || request.id;
    render(state);
  });
});
function download(data, name, type) {
  const url = URL.createObjectURL(new Blob([data], {type}));
  const link = document.createElement('a'); link.href=url; link.download=name; link.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}
$('download-run').addEventListener('click', () => { if(currentRun) download(JSON.stringify(currentRun,null,2),'run-'+currentRun.id+'.json','application/json'); });
$('download-code').addEventListener('click', () => { if(currentRun?.artifact) download(currentRun.artifact.code,currentRun.artifact.filename,'text/x-python'); });
function showConnection(connection) {
  apiConnected = connection.connected;
  $('api-status').textContent = apiConnected ? '연결됨 · 서버 종료 시 삭제' : '미연결';
  const signature = connection.models.join(',');
  if (signature !== modelList) {
    modelList = signature;
    $('api-models').replaceChildren();
    for (const id of connection.models) {
      const option = document.createElement('option'); option.value = id; $('api-models').append(option);
    }
  }
  controls();
}
$('run-mode').addEventListener('change', controls);
$('api-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('api-key').value.trim()) { $('api-status').textContent = '키를 입력하세요.'; return; }
  operation(async () => {
    let key = $('api-key').value.trim();
    $('api-key').value = '';
    $('api-status').textContent = '연결 확인 중';
    try { showConnection(await api('/api/connection', {key})); }
    catch (error) { $('api-status').textContent = '연결 실패'; throw error; }
    finally { key = ''; }
  });
});
$('disconnect-api').addEventListener('click', () => operation(async () => showConnection(await api('/api/connection/disconnect', {}))));
async function poll() {
  const version = operationVersion;
  try {
    const [state, connection] = await Promise.all([api('/api/runs/latest'), api('/api/connection')]);
    online = true;
    $('connection-status').textContent = state ? (state.mode === 'openai' ? 'OpenAI API' : '모의 실행') : '서버 연결됨';
    if (!busy && version === operationVersion) {
      showConnection(connection);
      if (state && state.id !== currentRun?.id) { restoreConfiguration(state.config); selectedRequest = null; formRequest = null; }
      render(state);
    }
  } catch { online = false; $('connection-status').textContent = '서버 연결 끊김'; controls(); }
  setTimeout(poll, 800);
}
if (location.protocol === 'http:' || location.protocol === 'https:') poll();
else { $('connection-status').textContent = '서버에서 열기: localhost:8000'; controls(); }
