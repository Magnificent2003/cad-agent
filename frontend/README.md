# 图纸工作台前端

React、TypeScript 和 Vite 实现的中文图纸工作台。所有业务数据来自同源 `/api`，开发服务将请求代理至 `127.0.0.1:8000`。密钥和智能体配置只在后端读取。

在项目根目录完成环境配置、启动后端后：

```bash
export PATH="$PWD/.tools/node/bin:$PATH"
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。生产构建使用 `npm run build`，由业务服务提供 `dist` 产物。

- `src/api`：业务请求、类型、元数据展示与会话事件订阅。
- `src/components/ChatPanel.tsx`：真实消息历史、Markdown、工具状态、输入和停止。
- `src/components/DrawingViewer.tsx`：按版本与布局加载预览，缩放、平移、适应窗口及 SVG/PNG 切换。
- `src/App.tsx`：上传任务、会话、版本与下载的协调。

历史版本只读，回到当前版本才能发送消息。发送前先建立事件订阅，并携带当前版本以检测冲突。重连时读取消息、图纸和会话状态。只有后端发布图纸更新才切换新版本；停止任务时等待相关 CAD 任务退出。

验证：

```bash
npm run build
npm run format:check
export PLAYWRIGHT_BROWSERS_PATH="$PWD/../.cache/playwright"
npx playwright install chromium
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost npm run test:e2e
```

浏览器测试使用独立接口夹具，覆盖历史版本发送保护、下载版本、预览降级、多布局、缩放、先订阅后发送、停止、上传及移动端溢出。夹具只在测试中生效，应用运行时直接访问真实 API。
