import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import TaskContextPlugin from "../agent/plugins/task-context.mjs";

async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), "cad-context-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const context = {
    session_id: "ses_test",
    task_id: "current-task",
    request: "简单介绍一下这张施工图",
    input_files: [{ path: "/data/agent-tasks/current-task/input/drawing.dxf", format: "dxf" }],
    output_directory: "/data/agent-tasks/current-task/output",
    scratch_directory: "/data/agent-tasks/current-task/scratch",
    previous_artifacts: [],
    runtime: { python: "3.11", packages: { ezdxf: "1.4.4" } },
    ...overrides,
  };
  const save = () => writeFile(join(directory, "ses_test.json"), JSON.stringify(context));
  await save();
  return { hooks: await TaskContextPlugin({}, { contextDirectory: directory }), context, save, directory };
}

test("system and compaction receive the latest backend context", async (t) => {
  const { hooks, context, save } = await fixture(t);
  const system = { system: ["base system prompt"] };
  await hooks["experimental.chat.system.transform"]({ sessionID: "ses_test" }, system);
  assert.equal(system.system[0], "base system prompt");
  assert.ok(system.system[1].includes(JSON.stringify(context)));
  const compact = { context: ["existing compaction context"] };
  await hooks["experimental.session.compacting"]({ sessionID: "ses_test" }, compact);
  assert.ok(compact.context[1].includes(JSON.stringify(context)));

  context.task_id = "next-task";
  context.request = "提取线条，另存为新图";
  context.output_directory = "/data/agent-tasks/next-task/output";
  await save();
  const next = { system: [] };
  await hooks["experimental.chat.system.transform"]({ sessionID: "ses_test" }, next);
  assert.ok(next.system[0].includes(JSON.stringify(context)));
  assert.ok(!next.system[0].includes('"task_id":"current-task"'));
});

test("all execute variants get a dispatch-time snapshot; polling is untouched", async (t) => {
  const { hooks, context, save } = await fixture(t);
  for (const tool of ["freecad_execute_code", "freecad_execute_code_async", "freecad_execute_code_headless"]) {
    const output = { args: { code: "print(cad_context['task_id'])", timeout: 60 } };
    await hooks["tool.execute.before"]({ tool, sessionID: "ses_test" }, output);
    assert.equal(output.args.timeout, 60);
    context.task_id = "newer-task";
    await save();
    const result = execFileSync("python3", ["-c", output.args.code], { encoding: "utf8" }).trim();
    assert.equal(result, tool === "freecad_execute_code" ? "current-task" : "newer-task");
  }
  const poll = { args: { job_id: "job-original", code: "not executable" } };
  const original = structuredClone(poll);
  await hooks["tool.execute.before"]({ tool: "freecad_get_async_status", sessionID: "ses_test" }, poll);
  assert.deepEqual(poll, original);
});

test("Python wrapper preserves Unicode, quoting, future imports and persistent globals", async (t) => {
  const { hooks, context } = await fixture(t, {
    request: '查看“白桥”图纸；引号\'、"、反斜杠\\、换行\n和 Emoji 🏗',
    optional: null,
    enabled: true,
  });
  const first = { args: { code: "from __future__ import annotations\nvalue = cad_context['request']\ndef verify(x: MissingType): return value" } };
  const second = { args: { code: "import json\nprint(json.dumps({'result': verify(None), 'context': cad_context}, ensure_ascii=False))" } };
  for (const output of [first, second]) {
    await hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_test" }, output);
  }
  const driver = "import json, sys\nnamespace = {}\nfor code in json.loads(sys.argv[1]): exec(code, namespace)";
  const result = JSON.parse(execFileSync("python3", ["-c", driver, JSON.stringify([first.args.code, second.args.code])], { encoding: "utf8" }));
  assert.equal(result.result, context.request);
  assert.deepEqual(result.context, context);
});

test("explicit stale input/output paths fail with the current target; current paths work", async (t) => {
  const { hooks } = await fixture(t);
  for (const path of ["/data/agent-tasks/old-task/input/drawing.dxf", "/data/agent-tasks/old-task/output/result.png", "agent-tasks/old-task/scratch"]) {
    await assert.rejects(
      hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_test" }, { args: { code: `p = ${JSON.stringify(path)}` } }),
      /拒绝使用历史任务.*当前任务为 current-task/,
    );
  }
  const current = { args: { code: "p = '/data/agent-tasks/current-task/input/drawing.dxf'" } };
  await hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_test" }, current);
  assert.match(current.args.code, /cad_context/);
});

test("explicit previous artifacts remain readable without allowing the whole old task", async (t) => {
  const path = "/data/agent-tasks/old-task/output/已发布 图纸.dxf";
  const { hooks } = await fixture(t, { previous_artifacts: [{ name: "已发布 图纸.dxf", path }] });
  await hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_test" }, { args: { code: `p = ${JSON.stringify(path)}` } });
  for (const denied of ["/data/agent-tasks/old-task/input/drawing.dxf", `${path}/another-file`, `/other-root${path}`]) {
    await assert.rejects(
      hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_test" }, { args: { code: `allowed = ${JSON.stringify(path)}; denied = ${JSON.stringify(denied)}` } }),
      /拒绝使用历史任务/,
    );
  }
});

test("missing bindings do not affect unrelated hooks but cannot execute unbound Python", async (t) => {
  const { hooks } = await fixture(t);
  const system = { system: ["existing"] };
  await hooks["experimental.chat.system.transform"]({ sessionID: "ses_missing" }, system);
  assert.deepEqual(system.system, ["existing"]);
  await hooks["experimental.chat.system.transform"]({}, system);
  const compact = { context: [] };
  await hooks["experimental.session.compacting"]({ sessionID: "ses_missing" }, compact);
  assert.deepEqual(compact.context, []);
  await assert.rejects(
    hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_missing" }, { args: { code: "print('unbound')" } }),
    /没有活跃的文件任务绑定/,
  );
});

test("mismatched or malformed bindings fail instead of executing against stale files", async (t) => {
  const { hooks, context, save, directory } = await fixture(t);
  context.session_id = "another-session";
  await save();
  await assert.rejects(hooks["experimental.chat.system.transform"]({ sessionID: "ses_test" }, { system: [] }), /任务绑定无效/);
  await writeFile(join(directory, "ses_test.json"), "broken json");
  await assert.rejects(hooks["tool.execute.before"]({ tool: "freecad_execute_code", sessionID: "ses_test" }, { args: { code: "pass" } }), SyntaxError);
  await assert.rejects(hooks["experimental.chat.system.transform"]({ sessionID: "../ses_test" }, { system: [] }), /无效的任务会话标识/);
});
