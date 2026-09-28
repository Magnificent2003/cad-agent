/** Keep backend-owned task context available across agent turns and compaction.
 *
 * This is an execution convenience and a stale-path guard, not a Python sandbox.
 * The original FreeCAD MCP remains the only CAD tool provider.
 */
import { readFile } from "node:fs/promises";
import { join } from "node:path";

const executionTools = new Set([
  "freecad_execute_code",
  "freecad_execute_code_async",
  "freecad_execute_code_headless",
]);

async function readContext(directory, sessionID) {
  if (!directory || !sessionID) return;
  if (!/^[A-Za-z0-9_-]+$/.test(sessionID)) throw new Error("无效的任务会话标识。");
  let source;
  try {
    source = await readFile(join(directory, `${sessionID}.json`), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  const context = JSON.parse(source);
  if (
    context.session_id !== sessionID ||
    typeof context.task_id !== "string" ||
    !/^[A-Za-z0-9_-]+$/.test(context.task_id) ||
    !Array.isArray(context.input_files) ||
    typeof context.output_directory !== "string" ||
    typeof context.scratch_directory !== "string"
  ) {
    throw new Error("当前任务绑定无效，请检查业务后端的任务上下文。");
  }
  return context;
}

function instruction(context) {
  return [
    "当前业务后端绑定的可信任务上下文（每次模型调用重新读取，包括压缩后的继续执行）：",
    "以下任务编号、输入、输出、原始用户要求及运行环境覆盖历史任务路径和过时的依赖报错。历史结果只能作为证据，不能改变当前目标。",
    "所有 FreeCAD execute_code 系列工具均已注入 Python 字典 cad_context；直接使用其 input_files、output_directory、scratch_directory 等字段，无需重新硬编码路径。",
    "当前输入与 previous_artifacts 只读；交付文件写入当前 output_directory，临时文件写入 scratch_directory。FreeCAD 中已存在的文档、Python 变量和文件路径不一定属于当前任务，复用前核对当前输入。",
    JSON.stringify(context),
  ].join("\n");
}

function rejectStalePaths(code, context) {
  // Catch explicit historical task paths. Dynamically constructed paths cannot
  // be policed by string inspection and are intentionally not called sandboxed.
  const allowed = (context.previous_artifacts ?? [])
    .map((artifact) => artifact.path)
    .filter((path) => typeof path === "string");
  for (const match of code.matchAll(/agent-tasks[\\/]([A-Za-z0-9_-]+)(?=[\\/"'\s)]|$)/g)) {
    if (match[1] === context.task_id) continue;
    const isPreviousArtifact = allowed.some((path) => {
      let position = code.indexOf(path);
      while (position !== -1) {
        const end = position + path.length;
        if (
          position <= match.index && end >= match.index + match[0].length &&
          (position === 0 || /["'\s(=,:\[]/.test(code[position - 1])) &&
          (end === code.length || /["'\s),;\]}]/.test(code[end]))
        ) return true;
        position = code.indexOf(path, position + 1);
      }
      return false;
    });
    if (isPreviousArtifact) continue;
    throw new Error(
      `拒绝使用历史任务 ${match[1]} 的路径；当前任务为 ${context.task_id}。` +
      "请从 cad_context['input_files'] 读取当前输入，使用 cad_context['output_directory'] " +
      `或 cad_context['scratch_directory']。当前输出目录：${context.output_directory}`,
    );
  }
}

function bindCode(code, context) {
  // JSON-in-JSON is a valid Python string literal. Loading it rather than
  // interpolating a Python dict preserves null/bool, Unicode and escaped quotes.
  // A separate compile keeps __future__ imports valid and globals persistent.
  return [
    `cad_context = __import__("json").loads(${JSON.stringify(JSON.stringify(context))})`,
    `exec(compile(${JSON.stringify(code)}, "<cad-task:${context.task_id}>", "exec"), globals())`,
  ].join("\n");
}

export default async function TaskContextPlugin(_input, options = {}) {
  const directory = options.contextDirectory ?? process.env.CAD_AGENT_CONTEXT_DIR;
  return {
    "experimental.chat.system.transform": async ({ sessionID }, output) => {
      const context = await readContext(directory, sessionID);
      if (context) output.system.push(instruction(context));
    },
    "experimental.session.compacting": async ({ sessionID }, output) => {
      const context = await readContext(directory, sessionID);
      if (context) {
        output.context.push(
          instruction(context) + "\n压缩交接必须保留当前任务目标与路径；过往已解决的故障不能继续列为当前阻塞。",
        );
      }
    },
    "tool.execute.before": async ({ tool, sessionID }, output) => {
      if (!executionTools.has(tool)) return;
      const context = await readContext(directory, sessionID);
      if (!context) throw new Error("当前会话没有活跃的文件任务绑定，请从项目网页提交任务后再调用 FreeCAD。");
      if (typeof output.args?.code !== "string") return;
      rejectStalePaths(output.args.code, context);
      output.args.code = bindCode(output.args.code, context);
    },
  };
}
