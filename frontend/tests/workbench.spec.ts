import { expect, test, type Page } from '@playwright/test'

const metadata = {
  units: { code: 4, name: 'Millimeters', confirmed: true },
  layouts: [
    { name: 'Model', entity_count: 32 },
    { name: 'Layout1', entity_count: 4 },
  ],
  layers: [{ name: '0' }, { name: '建筑' }],
  entity_counts: { LINE: 28, TEXT: 4 },
  warnings: [],
  // Old stored metadata recommended PNG for large files. The workbench must
  // now open SVG regardless, while keeping manual PNG and failure fallback.
  preview: { preferred_format: 'png', svg_bytes: 24 * 1024 * 1024 },
}
const drawing = {
  id: 'drawing-test',
  name: '界面验收.dxf',
  current_revision: 'r0002',
  metadata,
  status: 'ready',
}
const revisions = [
  { id: 'r0002', number: 2, summary: '更新施工说明', metadata },
  { id: 'r0001', number: 1, summary: '导入图纸', metadata },
]
const svg =
  '<svg xmlns="http://www.w3.org/2000/svg" width="700" height="460" viewBox="0 0 700 460"><rect x="40" y="40" width="620" height="380" fill="none" stroke="#84baaa"/><path d="M100 100h180v250H100zM360 100h220v250H360zM40 385h620" fill="none" stroke="#91bcb2"/><text x="90" y="75" fill="#bbd7c9" font-size="18">预览交互测试</text></svg>'

async function fixture(page: Page, options: { empty?: boolean; svgFails?: boolean } = {}) {
  const requests: string[] = []
  let messages: object[] = []
  let eventsRequested = false
  let submitted: Record<string, unknown> | null = null
  let imported = !options.empty
  let importReady = !options.empty
  let importPolls = 0
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    requests.push(url.pathname + url.search)
    const respond = (data: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
    if (url.pathname === '/api/health') return respond({ status: 'ok', opencode: 'connected' })
    if (url.pathname === '/api/drawings' && request.method() === 'POST') {
      imported = true
      importReady = false
      return respond({ drawing_id: drawing.id, job_id: 'import-job' }, 202)
    }
    if (url.pathname === '/api/drawings') return respond(imported ? [drawing] : [])
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
    if (url.pathname.endsWith('/revisions')) return respond(importReady ? revisions : [])
    if (url.pathname.endsWith('/sessions'))
      return respond(
        request.method() === 'POST'
          ? { id: 'session-test', title: '验收对话', status: 'idle' }
          : [],
      )
    if (url.pathname.endsWith('/status')) return respond({ status: 'idle' })
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
        submitted = request.postDataJSON()
        messages = [
          { id: 'user-message', role: 'user', parts: [{ type: 'text', text: submitted!.text }] },
        ]
        return respond({ accepted: true }, 202)
      }
      return respond(messages)
    }
    if (url.pathname.endsWith('/abort'))
      return respond({ status: 'stopped', model_stopped: true, jobs: [] })
    if (url.pathname === `/api/drawings/${drawing.id}`)
      return respond(
        importReady
          ? drawing
          : { ...drawing, metadata: null, current_revision: null, status: 'importing' },
      )
    return respond({ detail: 'Missing test fixture' }, 404)
  })
  return { requests, submitted: () => submitted }
}

test('history is read-only and downloads the selected revision', async ({ page }) => {
  const { requests } = await fixture(page)
  await page.goto('/')
  await expect(page.getByRole('img', { name: /界面验收/ })).toBeVisible()
  await page.getByLabel('图纸版本').selectOption('r0001')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
  await expect(page.getByText('历史版本仅供查看')).toBeVisible()
  await page.getByRole('button', { name: '下载', exact: true }).click()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: /DXF 可编辑图纸/ }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toContain('r0001.dxf')
  expect(requests.some((url) => url.includes('/r0001/download?'))).toBe(true)
  await page.getByRole('button', { name: '回到当前版本' }).first().click()
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  await expect(page.getByLabel('图纸版本')).toHaveValue('r0002')
})

test('PNG fallback and layout changes use separate preview requests', async ({ page }) => {
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

test('large drawings default to SVG and allow manual PNG', async ({ page }) => {
  const { requests } = await fixture(page)
  await page.goto('/')
  await expect(page.getByRole('img', { name: /界面验收/ })).toBeVisible()
  expect(requests.filter((url) => url.includes('/preview?'))[0]).toContain('format=svg')
  const format = page.getByLabel('预览格式')
  await expect(format).toHaveValue('svg')
  await format.selectOption('png')
  await expect(page.getByText('PNG 预览')).toBeVisible()
  await page.getByLabel('图纸布局').selectOption('Layout1')
  await expect(format).toHaveValue('svg')
  await expect(page.getByRole('img', { name: /Layout1/ })).toBeVisible()
})

test('message send subscribes before submission and includes the current revision', async ({
  page,
}) => {
  // Route fulfillment closes a native SSE stream immediately. Keep the test transport
  // connected while still making the real subscription request before the POST.
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
  const state = await fixture(page)
  await page.goto('/')
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
  await page.getByLabel('向 CAD 助手发送消息').fill('提取施工说明')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('.message-user')).toContainText('提取施工说明')
  expect(state.submitted()).toEqual({ text: '提取施工说明', expected_revision: 'r0002' })
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeDisabled()
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByText('任务已停止。')).toBeVisible()
  await expect(page.getByLabel('向 CAD 助手发送消息')).toBeEnabled()
})

test('upload follows import job completion and the workbench fits a mobile screen', async ({
  page,
}) => {
  await fixture(page, { empty: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '从一张图纸开始' })).toBeVisible()
  await page.getByLabel('上传 CAD 图纸').setInputFiles({
    name: '界面验收.dxf',
    mimeType: 'application/dxf',
    buffer: Buffer.from('test fixture'),
  })
  await expect(page.getByText('图纸导入完成，可以开始查看和对话。')).toBeVisible()
  await expect(page.getByRole('img', { name: /界面验收/ })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})
