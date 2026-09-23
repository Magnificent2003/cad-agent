import { expect, test, type Page, type Download } from '@playwright/test'

async function checkDownload(download: Download) {
  expect(await download.failure()).toBeNull()
  const chunks = []
  for await (const chunk of await download.createReadStream()) chunks.push(chunk)
  expect(Buffer.concat(chunks).toString()).toBe('test-download')
}

const metadata = {
  units: { code: 4, name: 'Millimeters', confirmed: true },
  layouts: [
    { name: 'Model', entity_count: 32 },
    { name: 'Layout1', entity_count: 4 },
  ],
  layers: [{ name: '0' }, { name: '建筑' }],
  entity_counts: { LINE: 28, TEXT: 4 },
  warnings: [],
  preview: { preferred_format: 'png', svg_bytes: 24 * 1024 * 1024 },
}
const drawing = {
  id: 'drawing-test',
  project_id: 'default',
  name: '界面验收.dxf',
  current_revision: 'r0002',
  metadata,
  status: 'ready',
}
const resultDrawing = {
  ...drawing,
  id: 'result-drawing',
  name: '建筑红线内.dxf',
  current_revision: 'r0001',
}
const otherDrawing = {
  ...drawing,
  id: 'other-drawing',
  project_id: 'other',
  name: '东区工程.dxf',
  current_revision: 'r0001',
}
const revisions = [
  { id: 'r0002', number: 2, summary: '更新施工说明', metadata },
  { id: 'r0001', number: 1, summary: '导入图纸', metadata },
]
const version = (file: typeof drawing, revision: string, number: number) => ({
  key: `${file.id}:${revision}`,
  drawing_id: file.id,
  revision_id: revision,
  current_revision: file.current_revision,
  name: file.name,
  number,
  label: `v${String(number).padStart(4, '0')}_${file.name.replace(/\.dxf$/, '')}`,
  summary: '图纸文件',
  artifact_id: file.id === resultDrawing.id ? 'file-test' : null,
})
const svg =
  '<svg xmlns="http://www.w3.org/2000/svg" width="700" height="460" viewBox="0 0 700 460"><rect x="40" y="40" width="620" height="380" fill="none" stroke="#7aaceb"/><path d="M100 100h180v250H100zM360 100h220v250H360zM40 385h620" fill="none" stroke="#96bafa"/><text x="90" y="75" fill="#bfd7f6" font-size="18">预览交互测试</text></svg>'

async function fixture(
  page: Page,
  options: { empty?: boolean; svgFails?: boolean; session?: boolean; legacy?: boolean } = {},
) {
  await page.addInitScript(() => {
    class TestEventSource {
      onopen: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      closed = false
      constructor(url: string) {
        void fetch(url).then(() => {
          if (!this.closed) this.onopen?.()
        })
      }
      close() {
        this.closed = true
      }
    }
    Object.defineProperty(window, 'EventSource', { value: TestEventSource })
  })
  const requests: string[] = []
  let messages: object[] = []
  let eventsRequested = false
  let running = false
  const submitted: Record<string, unknown>[] = []
  const opened: string[] = []
  let imported = !options.empty
  let importReady = !options.empty
  let importPolls = 0
  let uploadProject = 'default'
  let generated = false
  let previewStatus = 'queued'
  let reportOnly = false
  const projects = [
    { id: 'default', name: '白桥一号园（二期）' },
    { id: 'other', name: '东区配套工程' },
  ]
  let conversation =
    options.session || options.legacy
      ? {
          id: 'session-test',
          drawing_id: drawing.id,
          title: '总图设计对话',
          status: 'idle',
          engine: options.legacy ? 'legacy' : 'freecad',
        }
      : null
  const files = () =>
    generated
      ? [
          ...(!reportOnly
            ? [
                {
                  id: 'file-test',
                  name: resultDrawing.name,
                  bytes: 1234,
                  task_id: 'task-test',
                  drawing_id: resultDrawing.id,
                  preview_status: previewStatus,
                  preview_error: previewStatus === 'failed' ? '图纸预览失败' : null,
                  download_url: '/api/artifacts/file-test/download',
                },
              ]
            : []),
          {
            id: 'report-test',
            name: '工程统计.csv',
            bytes: 50,
            task_id: 'task-test',
            drawing_id: null,
            preview_status: 'unavailable',
            preview_error: null,
            download_url: '/api/artifacts/report-test/download',
          },
        ]
      : []
  const versions = () =>
    imported && importReady
      ? [
          version(drawing, 'r0001', 1),
          version(drawing, 'r0002', 2),
          ...(generated && !reportOnly && previewStatus === 'succeeded'
            ? [version(resultDrawing, 'r0001', 3)]
            : []),
        ]
      : []
  await page.context().route('**/api/**', async (route) => {
    const request = route.request(),
      url = new URL(request.url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    requests.push(request.method() + ' ' + url.pathname + url.search)
    const respond = (data: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
    if (url.pathname === '/api/health') return respond({ status: 'ok', agent: 'connected' })
    if (url.pathname === '/api/projects') {
      if (request.method() === 'POST') {
        const project = { id: 'new-project', name: request.postDataJSON().name }
        projects.push(project)
        return respond(project, 201)
      }
      return respond(projects)
    }
    if (request.method() === 'PATCH' && url.pathname.startsWith('/api/projects/')) {
      const project = projects.find((p) => url.pathname.endsWith('/' + p.id))!
      project.name = request.postDataJSON().name
      return respond(project)
    }
    if (url.pathname.endsWith('/workbench')) {
      const id = url.pathname.split('/')[3]
      const own = id === uploadProject
      return respond({
        project: projects.find((p) => p.id === id),
        versions: own ? versions() : id === 'other' ? [version(otherDrawing, 'r0001', 1)] : [],
        sessions:
          own && conversation ? [{ ...conversation, status: running ? 'busy' : 'idle' }] : [],
        artifacts: own ? files() : [],
      })
    }
    if (url.pathname.endsWith('/drawings') && request.method() === 'POST') {
      uploadProject = url.pathname.split('/')[3]
      imported = true
      importReady = false
      return respond({ drawing_id: drawing.id, job_id: 'import-job' }, 202)
    }
    if (url.pathname === '/api/jobs/import-job') {
      importReady = ++importPolls > 1
      return respond({ id: 'import-job', status: importReady ? 'succeeded' : 'running' })
    }
    if (url.pathname.endsWith('/preview')) {
      if (options.svgFails && url.searchParams.get('format') === 'svg')
        return respond({ detail: 'SVG unavailable' }, 503)
      return route.fulfill({ contentType: 'image/svg+xml', body: svg })
    }
    if (url.pathname.endsWith('/download'))
      return route.fulfill({ contentType: 'application/octet-stream', body: 'test-download' })
    if (url.pathname.endsWith('/revisions'))
      return respond(
        url.pathname.includes(resultDrawing.id) || url.pathname.includes(otherDrawing.id)
          ? [revisions[1]]
          : importReady
            ? revisions
            : [],
      )
    if (url.pathname.endsWith('/sessions') && request.method() === 'POST') {
      conversation = {
        id: 'session-test',
        drawing_id: url.pathname.split('/')[3],
        title: '总图设计对话',
        status: 'idle',
        engine: 'freecad',
      }
      return respond(conversation, 201)
    }
    if (url.pathname.endsWith('/artifacts'))
      return respond({
        artifacts: files(),
        task: generated ? { id: 'task-test', status: 'completed', error: null } : null,
      })
    if (url.pathname.endsWith('/open')) {
      opened.push(url.pathname)
      if (previewStatus !== 'succeeded') return respond({ detail: '预览尚未就绪' }, 409)
      conversation!.drawing_id = resultDrawing.id
      return respond({ drawing_id: resultDrawing.id, session_id: 'session-test' })
    }
    if (url.pathname.endsWith('/status')) return respond({ status: running ? 'busy' : 'idle' })
    if (url.pathname.endsWith('/events')) {
      eventsRequested = true
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'data: {"type":"connected"}\n\n',
      })
    }
    if (url.pathname.endsWith('/messages')) {
      if (request.method() === 'POST') {
        expect(eventsRequested).toBe(true)
        const payload = request.postDataJSON()
        submitted.push(payload)
        running = true
        messages = [
          ...messages,
          {
            id: `message-${submitted.length}`,
            role: 'user',
            parts: [{ type: 'text', text: payload.text }],
          },
        ]
        return respond({ accepted: true }, 202)
      }
      return respond(messages)
    }
    if (url.pathname.endsWith('/abort')) {
      running = false
      return respond({ status: 'stopped', model_stopped: true, jobs: [] })
    }
    for (const file of [drawing, resultDrawing, otherDrawing])
      if (url.pathname === `/api/drawings/${file.id}`) return respond(file)
    return respond({ detail: `Missing test fixture: ${url.pathname}` }, 404)
  })
  return {
    requests,
    submitted,
    opened,
    finish: (status = 'succeeded', onlyReport = false) => {
      generated = true
      previewStatus = status
      running = false
      reportOnly = onlyReport
    },
    ready: () => {
      previewStatus = 'succeeded'
    },
  }
}

test('project history is read-only, preserves conversation, and downloads selected file', async ({
  page,
}) => {
  const state = await fixture(page, { session: true })
  await page.goto('/')
  await expect(page.getByRole('img', { name: /界面验收/ })).toBeVisible()
  await page.getByLabel('项目图纸与历史').selectOption('drawing-test:r0001')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
  await expect(page.getByText('历史版本仅供查看')).toBeVisible()
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 DXF', exact: true }).click()
  expect((await downloaded).suggestedFilename()).toBe('v0001_界面验收.dxf')
  await checkDownload(await downloaded)
  expect(state.requests.some((url) => url.includes('/r0001/download?'))).toBe(true)
  expect(state.opened).toHaveLength(0)
  await expect(page.getByLabel('历史对话')).toHaveValue('session-test')
  await page.getByRole('button', { name: '回到当前版本' }).first().click()
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  await expect(page.getByLabel('项目图纸与历史')).toHaveValue('drawing-test:r0002')
})

test('PNG fallback and layout changes keep zoom controls working', async ({ page }) => {
  const { requests } = await fixture(page, { svgFails: true })
  await page.goto('/')
  await expect(page.getByText('PNG 预览')).toBeVisible()
  await page.getByLabel('图纸布局').selectOption('Layout1')
  await expect(page.getByRole('img', { name: /Layout1/ })).toBeVisible()
  expect(requests.some((url) => url.includes('layout=Layout1&format=png'))).toBe(true)
  await page.getByRole('button', { name: '放大', exact: true }).click()
  await expect(page.locator('.zoom-toolbar')).not.toContainText('100%')
  await page.getByRole('button', { name: '适应窗口' }).click()
  await expect(page.locator('.zoom-toolbar')).toContainText('100%')
})

test('large drawings default to SVG and allow manual PNG with direct download', async ({
  page,
}) => {
  const { requests } = await fixture(page)
  await page.goto('/')
  await expect(page.getByRole('img', { name: /界面验收/ })).toBeVisible()
  expect(requests.filter((url) => url.includes('/preview?'))[0]).toContain('format=svg')
  await page.getByLabel('预览格式').selectOption('png')
  await expect(page.getByText('PNG 预览')).toBeVisible()
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 PNG', exact: true }).click()
  expect((await downloaded).suggestedFilename()).toBe('v0002_界面验收.png')
  await checkDownload(await downloaded)
  await page.getByLabel('图纸布局').selectOption('Layout1')
  await expect(page.getByLabel('预览格式')).toHaveValue('svg')
})

test('send subscribes before submission and stop retains the active drawing', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('/')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  await page.getByLabel('向 CAD 助手发送消息').fill('提取施工说明')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('.message-user')).toContainText('提取施工说明')
  expect(state.submitted).toEqual([{ text: '提取施工说明', expected_revision: 'r0002' }])
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByText('任务已停止。')).toBeVisible()
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
})

test('upload belongs to the selected project and mobile layout has no horizontal overflow', async ({
  page,
}) => {
  const state = await fixture(page, { empty: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '从一张图纸开始' })).toBeVisible()
  await page.getByLabel('上传 CAD 图纸').setInputFiles({
    name: '界面验收.dxf',
    mimeType: 'application/dxf',
    buffer: Buffer.from('test fixture'),
  })
  await expect(page.getByText('图纸已加入当前项目，可以开始查看和对话。')).toBeVisible()
  await expect(page.getByRole('img', { name: /界面验收/ })).toBeVisible()
  expect(state.requests).toContain('POST /api/projects/default/drawings')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: '../.runtime/workbench-validation/mobile.png', fullPage: true })
  await page.getByRole('button', { name: '展开项目栏' }).click()
  await expect(page.getByRole('navigation', { name: '选择工程项目' })).toBeVisible()
  await page
    .getByRole('button', { name: '关闭项目栏', exact: true })
    .click({ position: { x: 350, y: 100 } })
  await expect(page.getByRole('navigation', { name: '选择工程项目' })).toBeHidden()
})

test('new CAD result waits for preview then opens automatically in the same conversation', async ({
  page,
}) => {
  const state = await fixture(page, { session: true })
  await page.goto('/')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  await page.getByLabel('向 CAD 助手发送消息').fill('提取建筑红线内的信息')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('.message-user')).toContainText('提取建筑红线内的信息')
  state.finish('running')
  await expect(page.getByText('正在更新成果预览')).toBeVisible()
  expect(state.opened).toHaveLength(0)
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
  state.ready()
  await expect(page.getByLabel('项目图纸与历史')).toHaveValue('result-drawing:r0001')
  await expect(page.getByRole('img', { name: /建筑红线内/ })).toBeVisible()
  await expect(page.getByLabel('历史对话')).toHaveValue('session-test')
  await expect(page.locator('.message-user')).toContainText('提取建筑红线内的信息')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  await expect(
    page.getByRole('region', { name: '智能助手对话' }).getByText('生成的文件'),
  ).toHaveCount(0)
  await page.getByLabel('项目图纸与历史').selectOption('drawing-test:r0001')
  await page.waitForTimeout(2200)
  await expect(page.getByLabel('项目图纸与历史')).toHaveValue('drawing-test:r0001')
  expect(state.opened).toHaveLength(1)
  await page.screenshot({
    path: '../.runtime/workbench-validation/desktop-history.png',
    fullPage: true,
  })
  await page.getByRole('button', { name: '回到当前版本' }).first().click()
  await page.screenshot({
    path: '../.runtime/workbench-validation/desktop-current.png',
    fullPage: true,
  })
  await page.getByLabel('向 CAD 助手发送消息').fill('继续检查新图')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect.poll(() => state.submitted.length).toBe(2)
  expect(state.submitted[1]).toEqual({ text: '继续检查新图', expected_revision: 'r0001' })
})

test('reports and failed previews stay downloadable in the right file menu', async ({ page }) => {
  const state = await fixture(page, { session: true })
  state.finish('failed')
  await page.goto('/')
  await page.getByLabel('项目生成文件', { exact: true }).click()
  const files = page.getByRole('region', { name: '项目生成文件列表' })
  await expect(files.getByText('工程统计.csv', { exact: true })).toBeVisible()
  await expect(files.getByText('图纸预览失败', { exact: true })).toBeVisible()
  const downloaded = page.waitForEvent('download')
  await files.getByRole('link', { name: '下载 工程统计.csv' }).click()
  expect((await downloaded).suggestedFilename()).toBe('工程统计.csv')
  await checkDownload(await downloaded)
  expect(state.opened).toHaveLength(0)
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
})

test('report-only completion preserves drawing and conversation', async ({ page }) => {
  const state = await fixture(page, { session: true })
  state.finish('unavailable', true)
  await page.goto('/')
  await expect(page.getByLabel('项目图纸与历史')).toHaveValue('drawing-test:r0002')
  await expect(page.getByLabel('历史对话')).toHaveValue('session-test')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  expect(state.opened).toHaveLength(0)
})

test('sidebar collapses, persists, switches projects and supports create and rename', async ({
  page,
}) => {
  await fixture(page)
  await page.goto('/')
  await page.getByRole('button', { name: '收起项目栏', exact: true }).first().click()
  await expect(page.getByRole('navigation', { name: '选择工程项目' })).toBeHidden()
  await page.reload()
  await expect(page.getByRole('navigation', { name: '选择工程项目' })).toBeHidden()
  await page.getByRole('button', { name: '展开项目栏' }).click()
  await page.getByRole('button', { name: '东区配套工程', exact: true }).click()
  await expect(page.getByRole('img', { name: /东区工程/ })).toBeVisible()
  await expect(page.getByLabel('项目图纸与历史')).toHaveValue('other-drawing:r0001')
  await expect(page.getByLabel('项目图纸与历史').locator('option')).toHaveCount(1)
  await page.getByRole('button', { name: '重命名项目 东区配套工程' }).click()
  await page.getByLabel('项目名称', { exact: true }).fill('东区二期')
  await page.getByRole('button', { name: '保存项目' }).click()
  await expect(page.locator('.project-breadcrumb')).toContainText('东区二期')
  await page.getByRole('button', { name: '新建项目', exact: true }).click()
  await page.getByLabel('项目名称', { exact: true }).fill('西区新工程')
  await page.getByRole('button', { name: '保存项目' }).click()
  await expect(page.locator('.project-breadcrumb')).toContainText('西区新工程')
  await expect(page.getByRole('heading', { name: '从一张图纸开始' })).toBeVisible()
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
})

test('legacy conversations remain read-only', async ({ page }) => {
  await fixture(page, { legacy: true })
  await page.goto('/')
  await page.getByLabel('历史对话').selectOption('session-test')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
  await expect(page.getByText('旧会话仅供查看，请新建对话使用自主执行能力。')).toBeVisible()
})
