const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');

function setup(saved) {
  const elements = new Map();
  for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const [,tag,attrs,id] = match;
    const contents = html.slice(match.index + match[0].length).split('</' + tag + '>')[0];
    const options = tag === 'select' ? [...contents.matchAll(/<option(?: value="([^"]*)")?>([^<]*)<\/option>/g)].map(m=>({value:m[1] || m[2]})) : [];
    elements.set(id, {tagName:tag.toUpperCase(), options,
      value: tag === 'textarea' ? contents : tag === 'select' ? options[0].value : attrs.match(/\bvalue="([^"]*)"/)?.[1] || '',
      textContent:'', hidden:/\bhidden\b/.test(attrs), listeners:{}, attributes:{},
      addEventListener(event, listener){this.listeners[event]=listener;},
      setAttribute(name,value){this.attributes[name]=value;},
      setCustomValidity(){}, reportValidity(){return true;}, querySelectorAll(){return [];} });
  }
  const context = {document:{getElementById(id){assert.ok(elements.has(id), 'missing ID: ' + id); return elements.get(id);}},
    localStorage:{getItem(){return saved ? JSON.stringify(saved) : null;}}, location:{protocol:'file:'}};
  vm.createContext(context);
  vm.runInContext(script + '\nglobalThis.probe = {readConfiguration,restoreConfiguration,commonMonitorPrompt,setRuntime(state, connected) {currentRun=state; online=true; apiConnected=connected; selectedRequest=state.requests[0]?.id; controls();}};', context);
  return {probe:context.probe, elements};
}

test('default instructions include common judgment prompt for all three monitors', () => {
  const {probe} = setup();
  for (const m of probe.readConfiguration().monitors) assert.ok(m.instructions.includes(probe.commonMonitorPrompt));
});

test('paused and interrupted runs accept answers even before key reconnection', () => {
  const {probe, elements} = setup();
  const input = {};
  elements.get('answer-fields').querySelectorAll = () => [input];
  for (const status of ['paused', 'interrupted']) {
    const state = {mode:'openai', status, test:{status:'not_run'}, requests:[{id:'pending',status:'waiting'}]};
    probe.setRuntime(state, false);
    assert.equal(input.disabled, false);
    assert.equal(elements.get('submit-answer').disabled, false);
    assert.equal(elements.get('resume-run').hidden, false);
    assert.equal(elements.get('resume-run').disabled, true);
    probe.setRuntime(state, true);
    assert.equal(elements.get('resume-run').disabled, false);
    state.requests[0].status = 'answered';
    probe.setRuntime(state, true);
    assert.equal(input.disabled, true);
  }
});

test('rules toggle is initially closed and excluded from agent configuration', () => {
  const {probe, elements} = setup();
  const panel = elements.get('grading-rules'), button = elements.get('toggle-rules');
  assert.equal(panel.hidden, true);
  button.listeners.click();
  assert.equal(panel.hidden, false);
  assert.equal(button.attributes['aria-expanded'], 'true');
  button.listeners.click();
  assert.equal(panel.hidden, true);
  assert.equal(button.attributes['aria-expanded'], 'false');
  const config = JSON.stringify(probe.readConfiguration());
  for (const hidden of ['2026-11-10', '2026-12-24', '24시간', '금·토·일', 'UTC+9']) assert.ok(!config.includes(hidden));
});
test('old saved prompts migrate once and edited version 3 prompts stay verbatim', () => {
  const initial = setup().probe.readConfiguration();
  initial.version = 2;
  initial.mainAgent.prompt = '  메인 원문\n  ';
  initial.monitors.forEach(m => {m.instructions='  직접 입력\n관점 ' + m.id;});
  const {probe} = setup(initial);
  const updated = probe.readConfiguration();
  assert.equal(updated.mainAgent.prompt, initial.mainAgent.prompt);
  for (const m of updated.monitors) assert.equal(m.instructions, initial.monitors[m.id-1].instructions + '\n' + probe.commonMonitorPrompt);
  const twice = setup(updated).probe.readConfiguration();
  assert.equal(JSON.stringify(twice), JSON.stringify(updated));
  updated.monitors[0].instructions = '  새 지침\n원문  ';
  assert.equal(setup(updated).probe.readConfiguration().monitors[0].instructions, '  새 지침\n원문  ');
});
