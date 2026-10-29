import ast
import copy
import hashlib
import json
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .adapters import MockMainAgent, MockMonitor, agent_input, validate_answer
from .store import Store
from .openai_client import OpenAIClient
from .live_workflow import LiveWorkflow
from .isolated import grade_isolated


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


def validate_config(config):
    if not isinstance(config, dict) or not isinstance(config.get("project"), dict) or not isinstance(config.get("mainAgent"), dict):
        raise ValueError("프로젝트와 메인 에이전트 설정이 필요합니다.")
    def text(value, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("프롬프트·이름·모델 입력을 확인하세요.")
        return value  # Preserve the user's exact prompt, including whitespace.
    main = config["mainAgent"]
    monitors = config.get("monitors")
    if not isinstance(monitors, list) or len(monitors) != 3:
        raise ValueError("감시자 3개의 설정이 필요합니다.")
    if any(not isinstance(m, dict) or type(m.get("id")) is not int for m in monitors) or {m["id"] for m in monitors} != {1, 2, 3}:
        raise ValueError("감시자 ID는 1, 2, 3이어야 합니다.")
    mode = config.get('mode', 'mock')
    if mode not in ('mock', 'openai'):
        raise ValueError('실행 모드를 확인하세요.')
    return {"version": 4, "mode": mode, "architecture": "main-agent-with-three-observers",
            "project": {"name": text(config["project"].get("name"), 120)},
            "mainAgent": {"model": text(main.get("model"), 80), "prompt": text(main.get("prompt"), 20000)},
            "monitors": [{"id": m["id"], "model": text(m.get("model"), 80),
                          "concept": text(m.get("concept"), 80), "instructions": text(m.get("instructions"), 12000)}
                         for m in sorted(monitors, key=lambda m: m["id"])],
            "workflow": {"waitForAllMonitors": False, "pauseAffectedTasksOnly": True,
                         "benchmarkTrigger": "manual_after_project_completion"}}


class Engine(LiveWorkflow):
    ACTIVE = {"running", "waiting", "applying"}

    def __init__(self, root, data_dir=None, benchmark=None, main_agent=None, monitors=None):
        self.root = Path(root).resolve()
        self.data = Path(data_dir or self.root / "var").resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.benchmark = Path(benchmark or self.root / "reservation_benchmark").resolve()
        self.store = Store(self.data / "runs.sqlite3")
        self.cv = threading.Condition(threading.RLock())
        self.runs = {state["id"]: state for state in self.store.all()}
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="monitor")
        self.threads = []
        self.closed = False
        self.api_client = None
        self.connecting = False
        self.main_agent = main_agent or MockMainAgent()
        self.monitors = monitors or {i: MockMonitor(i) for i in (1, 2, 3)}
        for state in self.runs.values():
            if state.get('mode') == 'openai' and state['status'] == 'failed' and state.get('events') and '코드 수정 6회에 도달했습니다.' in state['events'][-1]['message']:
                state.update(status='paused', phase='이전 수정 한도 중단 · 계속하기 대기', pause_reason='legacy_revision_limit')
                state['revision'] += 1
                self._event(state, '이전 수정 횟수 제한으로 중단된 실행을 복구했습니다. 기존 답변을 유지한 채 계속할 수 있습니다.')
            if state["status"] in self.ACTIVE:
                state["status"] = "interrupted"
                state['revision'] += 1
                self._event(state, "서버 재시작으로 실행이 중단됐습니다.")
            if state["test"]["status"] == "running":
                state["test"] = {"status": "interrupted", "report": None}
            self.store.save(state)

    def _event(self, state, message, source="system"):
        state["events"].append({"at": now(), "source": source, "message": message})

    def connection(self):
        with self.cv:
            return {'connected': self.api_client is not None, 'connecting': self.connecting,
                    'models': list(self.api_client.models) if self.api_client else []}

    def _connection_idle(self):
        if self.closed or self.connecting or any(s['status'] in self.ACTIVE for s in self.runs.values()):
            raise Conflict('실행 또는 연결 확인을 먼저 완료하거나 중단하세요.')

    def connect(self, key):
        with self.cv:
            self._connection_idle()
            self.connecting = True
        client = None
        try:
            client = OpenAIClient(key)
            client.connect()
            with self.cv:
                if self.closed:
                    raise Conflict('서버가 종료됐습니다.')
                if self.api_client:
                    self.api_client.clear()
                self.api_client = client
                client = None
        finally:
            if client:
                client.clear()
            with self.cv:
                self.connecting = False
        return self.connection()

    def disconnect(self):
        with self.cv:
            self._connection_idle()
            if self.api_client:
                self.api_client.clear()
            self.api_client = None
            return self.connection()

    def _save(self, state):
        state["revision"] += 1
        state["updated_at"] = now()
        self.store.save(state)
        self.cv.notify_all()

    def _state(self, run_id):
        if run_id not in self.runs:
            raise KeyError("실행을 찾을 수 없습니다.")
        return self.runs[run_id]

    def get(self, run_id):
        with self.cv:
            return copy.deepcopy(self._state(run_id))

    def latest(self):
        with self.cv:
            return copy.deepcopy(next(reversed(self.runs.values()))) if self.runs else None

    def list_runs(self):
        with self.cv:
            return [{"id": s["id"], "name": s["config"]["project"]["name"], "status": s["status"],
                     "created_at": s["created_at"]} for s in reversed(list(self.runs.values()))]

    def _spawn(self, function, *args):
        thread = threading.Thread(target=function, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    def start(self, config):
        config = validate_config(config)
        with self.cv:
            if self.closed or self.connecting or any(s["status"] in self.ACTIVE or s["test"]["status"] == "running" for s in self.runs.values()):
                raise Conflict("진행 중인 실행을 먼저 완료하거나 중단하세요.")
            if config['mode'] == 'openai':
                if not self.api_client:
                    raise Conflict('API 키를 먼저 연결하세요.')
                for agent in [config['mainAgent']] + config['monitors']:
                    if agent['model'] not in self.api_client.models:
                        raise ValueError('사용 가능한 API 모델 ID를 선택하세요: ' + agent['model'])
            run_id = uuid.uuid4().hex
            state = {"id": run_id, "mode": config['mode'], "config": config, "created_at": now(), "revision": 0,
                     "status": "running", "phase": "계획", "code_version": 0, "artifact": None, 'execution_epoch': 0,
                     "tasks": [{"id": key, "name": name, "status": "pending"} for key, name in
                               [("input", "입력 처리"), ("policy", "취소 정책 구현"), ("docs", "사용 방법 작성")]],
                     "plan": [], "policies": {}, "requests": [], "decisions": [], "calls": [], "events": [],
                     "monitors": [{"id": i, "status": "idle", "decision": None, "version": None} for i in (1, 2, 3)],
                     "verification": None, "test": {"status": "not_run", "report": None}}
            self.runs[run_id] = state
            (self.data / run_id).mkdir()
            self._event(state, "OpenAI 실행 시작." if state['mode'] == 'openai' else "모의 실행 시작. 프롬프트는 전달·기록되지만 자연어 해석은 수행하지 않습니다.")
            self._save(state)
            self._spawn(self._work_live if state['mode'] == 'openai' else self._work, run_id)
            return copy.deepcopy(state)

    def _context(self, state):
        return {"project": state["config"]["project"], "code_version": state["code_version"],
                "plan": state["plan"], "tasks": state["tasks"], "policies": state["policies"],
                "decisions": state["decisions"],
                "code": state["artifact"]["code"] if state["artifact"] else None}

    def _invoke_main(self, state, action):
        envelope = agent_input("main", state["config"]["mainAgent"], self._context(state), action)
        state["calls"].append({"at": now(), "agent": "main", "input": envelope})
        return self.main_agent.invoke(copy.deepcopy(envelope))

    def _write_artifact(self, state):
        result = self._invoke_main(state, "implement")
        code = result["code"]
        ast.parse(code)
        version = state["code_version"] + 1
        filename = "solution_v%d.py" % version
        path = self.data / state["id"] / filename
        path.write_text(code)
        # Read back embedded policy constants without executing candidate code.
        tree = ast.parse(path.read_text())
        policy_node = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == "POLICIES" for t in node.targets))
        embedded = json.loads(ast.literal_eval(policy_node.args[0]))
        if embedded != state["policies"]:
            raise ValueError("인간 결정이 생성 코드에 반영되지 않았습니다.")
        state["code_version"] = version
        state["artifact"] = {"filename": filename, "sha256": hashlib.sha256(code.encode()).hexdigest(), "code": code}
        state["verification"] = {"type": "policy_readback_and_syntax", "passed": True, "code_version": version,
                                 "note": "정책 값 반영·문법 확인. 100문항 기능 채점은 별도입니다."}

    def _observe(self, state):
        for config in state["config"]["monitors"]:
            envelope = agent_input("monitor", config, self._context(state), "observe")
            state["calls"].append({"at": now(), "agent": "monitor-%s" % config["id"], "input": envelope})
            state["monitors"][config["id"] - 1]["status"] = "observing"
            self.pool.submit(self._monitor, state["id"], config["id"], envelope)

    def _monitor(self, run_id, monitor_id, envelope):
        try:
            result = self.monitors[monitor_id].invoke(copy.deepcopy(envelope))
            with self.cv:
                state = self._state(run_id)
                if state["status"] not in self.ACTIVE | {"completed"}:
                    return
                monitor = state["monitors"][monitor_id - 1]
                if monitor["version"] is not None and envelope["project_state"]["code_version"] < monitor["version"]:
                    return
                monitor.update(status="observed", decision=result["decision"], version=envelope["project_state"]["code_version"])
                for item in result.get("requests", []):
                    key = item["key"]
                    existing = next((r for r in state["requests"] if r["key"] == key), None)
                    if existing:
                        if monitor_id not in existing["sources"]:
                            existing["sources"].append(monitor_id)
                        continue
                    if key in state["policies"]:
                        continue
                    if state["status"] == "completed":
                        continue
                    state["requests"].append({**copy.deepcopy(item), "id": uuid.uuid4().hex, "sources": [monitor_id],
                                              "status": "waiting", "answer": None,
                                              "observed_version": envelope["project_state"]["code_version"]})
                    state["tasks"][1]["status"] = "blocked"
                    self._event(state, item["question"], "monitor-%s" % monitor_id)
                self._save(state)  # publish immediately, without waiting for other observers
        except Exception as exc:
            with self.cv:
                state = self._state(run_id)
                if state["status"] in self.ACTIVE:
                    state["status"] = "failed"
                    state["monitors"][monitor_id - 1]["status"] = "error"
                    self._event(state, "감시 오류: " + str(exc))
                    self._save(state)

    def _work(self, run_id):
        try:
            with self.cv:
                state = self._state(run_id)
                if state["status"] not in self.ACTIVE:
                    return
                state["plan"] = self._invoke_main(state, "plan")["plan"]
                self._write_artifact(state)
                state["tasks"][0]["status"] = "completed"
                self._observe(state)
                # Independent work continues while observers examine the project.
                (self.data / run_id / "USAGE.txt").write_text("can_cancel(now, booked_at, starts_at) -> bool\nISO 8601 timestamps with UTC offsets.\n")
                state["tasks"][2]["status"] = "completed"
                self._event(state, "입력 처리와 사용 방법 작성 완료. 취소 정책만 인간 판단을 기다립니다.", "main")
                state["status"] = "waiting"
                state["phase"] = "정책 결정 대기"
                state["tasks"][1]["status"] = "blocked"
                self._save(state)
            while True:
                with self.cv:
                    state = self._state(run_id)
                    if state["status"] not in self.ACTIVE:
                        return
                    answered = [r for r in state["requests"] if r["status"] == "answered"]
                    if answered:
                        state["status"] = "applying"
                        state["phase"] = "결정 반영"
                        for request in answered:
                            request["status"] = "applying"
                            state["policies"][request["key"]] = copy.deepcopy(request["answer"])
                        self._write_artifact(state)
                        for request in answered:
                            request["status"] = "applied"
                            request["applied_version"] = state["code_version"]
                        self._event(state, "인간 결정 코드 반영 및 정책 값·문법 확인 완료 (100문항 채점 전).", "main")
                        self._observe(state)
                        state["status"] = "waiting"
                        self._save(state)
                    if set(state["policies"]) == {"cutoff", "calendar", "exceptions"}:
                        state["tasks"][1]["status"] = "completed"
                        state["status"] = "completed"
                        state["phase"] = "프로젝트 완료"
                        self._event(state, "프로젝트 완료. 테스트하기 버튼으로 채점할 수 있습니다.", "main")
                        self._save(state)
                        return
                    state["phase"] = "정책 결정 대기"
                    self.cv.wait(timeout=1)
        except Exception as exc:
            with self.cv:
                state = self._state(run_id)
                if state["status"] in self.ACTIVE:
                    state["status"] = "failed"
                    self._event(state, "작업 오류: " + str(exc))
                    self._save(state)

    def answer(self, run_id, request_id, answer, note=""):
        with self.cv:
            state = self._state(run_id)
            if state["status"] not in self.ACTIVE and not (state['mode'] == 'openai' and state['status'] in {'paused', 'interrupted'}):
                raise Conflict("진행 중인 실행이 아닙니다.")
            request = next((r for r in state["requests"] if r["id"] == request_id), None)
            if not request:
                raise KeyError("요청을 찾을 수 없습니다.")
            if request["status"] != "waiting":
                raise Conflict("이미 답변한 요청입니다.")
            if state['mode'] == 'openai':
                if not isinstance(answer, dict) or set(answer) != {'text'} or not isinstance(answer['text'], str) or not answer['text'].strip() or len(answer['text']) > 12000:
                    raise ValueError('답변을 12000자 이내로 입력하세요.')
                result = copy.deepcopy(answer)
            else:
                result = validate_answer(request["key"], answer)
            if not isinstance(note, str) or len(note) > 4000:
                raise ValueError("조언은 4000자 이내로 입력하세요.")
            request.update(status="answered", answer=result)
            state["decisions"].append({"request_id": request_id, "key": request["key"], "answer": result,
                                       "note": note, "at": now(), "source": "human"})
            self._event(state, "인간 답변 기록: " + request["key"], "human")
            self._save(state)
            return copy.deepcopy(state)

    def resume(self, run_id):
        with self.cv:
            state = self._state(run_id)
            if state['mode'] != 'openai' or state['status'] not in {'paused', 'interrupted'}:
                raise Conflict('이어갈 수 있는 실행이 아닙니다.')
            if self.closed or self.connecting or any(s['status'] in self.ACTIVE or s['test']['status'] == 'running' for s in self.runs.values()):
                raise Conflict('진행 중인 실행을 먼저 완료하거나 중단하세요.')
            if not self.api_client:
                raise Conflict('API 키를 먼저 연결하세요.')
            for agent in [state['config']['mainAgent']] + state['config']['monitors']:
                if agent['model'] not in self.api_client.models:
                    raise ValueError('기존 실행의 모델에 접근할 수 없습니다: ' + agent['model'])
            state['execution_epoch'] = state.get('execution_epoch', 0) + 1
            for call in state['calls']:
                if call.get('status') == 'running':
                    call['status'] = 'interrupted'
            state.update(status='running', phase='기존 답변으로 계속 진행', autonomous_steps=0)
            state.pop('pause_reason', None)
            self._event(state, '사용자가 계속하기를 실행했습니다. 기존 코드·질문·답변을 유지합니다.')
            self._save(state)
            self._spawn(self._work_live, run_id, True, state['execution_epoch'])
            return copy.deepcopy(state)

    def stop(self, run_id):
        with self.cv:
            state = self._state(run_id)
            if state["status"] not in self.ACTIVE:
                raise Conflict("중단할 실행이 없습니다.")
            state["status"] = "stopped"
            state["phase"] = "중단"
            self._event(state, "사용자가 중단했습니다.")
            self._save(state)
            return copy.deepcopy(state)

    def test(self, run_id):
        with self.cv:
            state = self._state(run_id)
            if state["status"] != "completed":
                raise Conflict("프로젝트 완료 후 테스트할 수 있습니다.")
            if state["test"]["status"] == "running":
                raise Conflict("채점이 진행 중입니다.")
            state["test"] = {"status": "running", "report": None, "code_version": state["code_version"]}
            self._event(state, "사용자가 100문항 채점을 시작했습니다.")
            self._save(state)
            self._spawn(self._grade, run_id, copy.deepcopy(state["artifact"]))
            return copy.deepcopy(state)

    def _grade(self, run_id, artifact):
        try:
            run_dir = self.data / run_id
            path = run_dir / artifact["filename"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ValueError("제출 파일이 완료 시점 이후 변경됐습니다.")
            report_path = run_dir / "score.json"
            if self.get(run_id)['mode'] == 'openai':
                report = grade_isolated(path.read_text(), self.benchmark)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                subprocess.run([sys.executable, "-I", "-B", str(self.benchmark / "score.py"), str(path),
                                "--output", str(report_path)], cwd=self.benchmark, timeout=20,
                               check=True, capture_output=True, text=True)
                report = json.loads(report_path.read_text())
            with self.cv:
                state = self._state(run_id)
                state["test"].update(status="completed", report=report, artifact_sha256=artifact["sha256"])
                self._event(state, "채점 완료: %s/%s" % (report["score"], report["total"]))
                self._save(state)
        except Exception as exc:
            with self.cv:
                state = self._state(run_id)
                state["test"].update(status="failed", error=str(exc))
                self._event(state, "채점 실패: " + str(exc))
                self._save(state)

    def close(self):
        with self.cv:
            self.closed = True
            for state in self.runs.values():
                if state["status"] in self.ACTIVE:
                    state["status"] = "interrupted"
                    self._save(state)
            self.cv.notify_all()
        for thread in self.threads:
            thread.join(timeout=22)
        self.pool.shutdown(wait=True)
        if self.api_client:
            self.api_client.clear()
            self.api_client = None
