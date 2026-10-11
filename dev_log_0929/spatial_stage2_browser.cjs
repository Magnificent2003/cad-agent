#!/usr/bin/env node
/* Real browser evidence only: no server startup, request replay, or mocked API.
 * Usage:
 *   node dev_log_0929/spatial_stage2_browser.cjs \
 *     --result /path/to/result.json --output /path/to/browser.json \
 *     --screenshot /path/to/browser.png [--download /path/to/download.dxf]
 * Missing published versions are failures, never replaced with the input drawing.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const ROOT = path.resolve(__dirname, '..');
const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');

function argumentsFrom(argv) {
  const args = {};
  const allowed = new Set(['result', 'output', 'screenshot', 'download', 'chromium', 'timeout-ms']);
  for (let i = 0; i < argv.length; i++) {
    const name = argv[i].replace(/^--/, '');
    if (!allowed.has(name) || !argv[i + 1] || argv[i + 1].startsWith('--')) {
      throw new Error(`Invalid argument: ${argv[i]}`);
    }
    args[name] = argv[++i];
  }
  if (!args.result || !args.output) throw new Error('--result and --output are required');
  args.result = path.resolve(args.result);
  args.output = path.resolve(args.output);
  args.screenshot = path.resolve(args.screenshot || args.output.replace(/\.json$/i, '') + '.png');
  args.timeout = Number(args['timeout-ms'] || 120000);
  if (!Number.isFinite(args.timeout) || args.timeout < 1000 || args.timeout > 300000) {
    throw new Error('--timeout-ms must be 1000..300000');
  }
  if (args.download) args.download = path.resolve(args.download);
  if ([args.output, args.screenshot, args.download].filter(Boolean).includes(args.result)) {
    throw new Error('Evidence output must not overwrite the stage2 result');
  }
  return args;
}

function browserExecutable(explicit) {
  if (explicit) {
    const resolved = path.resolve(explicit);
    if (!fs.existsSync(resolved)) throw new Error(`Chromium not found: ${resolved}`);
    return resolved;
  }
  const cache = path.join(ROOT, '.cache/playwright');
  if (fs.existsSync(cache)) {
    const versions = fs.readdirSync(cache).filter((name) => /^chromium-\d+$/.test(name))
      .sort((a, b) => Number(b.split('-').at(-1)) - Number(a.split('-').at(-1)));
    for (const version of versions) {
      for (const suffix of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
        const executable = path.join(cache, version, suffix);
        if (fs.existsSync(executable)) return executable;
      }
    }
  }
  throw new Error('No cached Chromium found; pass --chromium. This script never downloads a browser.');
}

async function main() {
  const args = argumentsFrom(process.argv.slice(2));
  const inputBytes = fs.readFileSync(args.result);
  const input = JSON.parse(inputBytes.toString('utf8'));
  const report = {
    schema: 'spatial-stage2-browser/1', started_at: new Date().toISOString(), status: 'running',
    result_path: args.result, result_sha256: sha256(inputBytes), stage2_status: input.status,
    browser_url: input.browser_url, project_name: input.project_name, project_id: input.project_id,
    task_id: input.task_id, selected_drawing_id: input.selected_drawing_id || null,
    mocked_requests: false, services_started: false, frontend_modified: false,
    checks: {}, console_errors: [], page_errors: [], failed_api_responses: [], preview_responses: [],
  };
  let browser;
  let page;
  const responseReads = [];
  try {
    if (!input.browser_url || !input.project_name) throw new Error('Result lacks browser_url or project_name');
    const url = new URL(input.browser_url);
    if (!['http:', 'https:'].includes(url.protocol)
        || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
      throw new Error('browser_url must address the local running application');
    }
    let playwright;
    try { playwright = require(path.join(ROOT, 'frontend/node_modules/playwright')); }
    catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
      playwright = require(path.join(ROOT, 'frontend/node_modules/@playwright/test'));
    }
    report.chromium_executable = browserExecutable(args.chromium);
    browser = await playwright.chromium.launch({
      executablePath: report.chromium_executable, headless: true, args: ['--no-sandbox'],
    });
    const context = await browser.newContext({ viewport: { width: 1680, height: 1050 }, acceptDownloads: true });
    page = await context.newPage();
    page.setDefaultTimeout(args.timeout);
    page.setDefaultNavigationTimeout(args.timeout);
    page.on('console', (message) => {
      if (message.type() === 'error' && report.console_errors.length < 30) report.console_errors.push(message.text());
    });
    page.on('pageerror', (error) => {
      if (report.page_errors.length < 30) report.page_errors.push(error.message);
    });
    page.on('response', (response) => {
      const address = new URL(response.url());
      if (address.pathname.startsWith('/api/') && response.status() >= 400 && report.failed_api_responses.length < 30) {
        report.failed_api_responses.push({ url: response.url(), status: response.status() });
      }
      if (!input.selected_drawing_id
          || !address.pathname.startsWith(`/api/drawings/${input.selected_drawing_id}/revisions/`)
          || !address.pathname.endsWith('/preview') || response.status() !== 200) return;
      responseReads.push((async () => {
        const entry = { url: response.url(), status: response.status(), content_type: response.headers()['content-type'] };
        try {
          const bytes = await response.body();
          Object.assign(entry, { bytes: bytes.length, sha256: sha256(bytes) });
        } catch (error) { entry.body_error = error.message; }
        report.preview_responses.push(entry);
      })());
    });
    await page.goto(input.browser_url, { waitUntil: 'domcontentloaded' });
    const expand = page.getByRole('button', { name: '展开项目栏', exact: true });
    if (await expand.isVisible()) await expand.click();
    const project = page.getByRole('navigation', { name: '选择工程项目' })
      .getByRole('button', { name: input.project_name, exact: true });
    await project.waitFor({ state: 'visible' });
    await project.click();
    await page.waitForFunction((name) => {
      const selected = document.querySelector('.project-item[aria-current="page"]');
      return selected && selected.title === name;
    }, input.project_name);
    report.checks.correct_project_selected = true;
    const picker = page.getByRole('combobox', { name: '项目图纸与历史', exact: true });
    await picker.waitFor({ state: 'visible' });
    await page.waitForFunction(() => {
      const select = document.querySelector('select[aria-label="项目图纸与历史"]');
      return select && !select.disabled && select.options.length > 0;
    });
    if (input.original_drawing_id) {
      await page.waitForFunction((drawingId) => {
        const select = document.querySelector('select[aria-label="项目图纸与历史"]');
        return select && [...select.options].some((option) => option.value.startsWith(drawingId + ':'));
      }, input.original_drawing_id);
    }
    report.versions = await picker.locator('option').evaluateAll((options) =>
      options.map((option) => ({ key: option.value, label: option.textContent.trim() })));
    if (input.status !== 'published') throw new Error(`Stage2 did not publish an openable modified DXF: ${input.status}`);
    if (!input.selected_drawing_id || input.selected_drawing_id === input.original_drawing_id) {
      throw new Error('No distinct published drawing is selected; refusing to validate the original');
    }
    const artifact = (input.artifacts || []).find((item) => item.id === input.selected_artifact_id)
      || (input.artifacts || []).find((item) => item.drawing_id === input.selected_drawing_id);
    if (!artifact || artifact.preview_status !== 'succeeded' || !artifact.name.toLowerCase().endsWith('.dxf')) {
      throw new Error('Selected drawing has no successfully previewed published DXF artifact');
    }
    const knownVersion = (input.versions || []).find((item) => item.drawing_id === input.selected_drawing_id
      && (!artifact.revision_id || item.revision_id === artifact.revision_id));
    const expectedKey = artifact.revision_id ? `${input.selected_drawing_id}:${artifact.revision_id}` : knownVersion?.key;
    if (!expectedKey) throw new Error('Result does not identify the published revision');
    const shownVersion = report.versions.find((item) => item.key === expectedKey);
    if (!shownVersion) throw new Error(`Published revision is missing from the actual dropdown: ${expectedKey}`);
    report.checks.generated_version_in_dropdown = true;
    report.checks.original_and_generated_versions_present = report.versions.some((item) =>
      item.key.startsWith(`${input.original_drawing_id}:`));
    if (!report.checks.original_and_generated_versions_present) throw new Error('Original version missing from project history');
    report.selected_version = shownVersion;
    report.latest_dropdown_version = report.versions[0];
    report.checks.selected_is_latest_dropdown_version = report.versions[0].key === expectedKey;
    if (!report.checks.selected_is_latest_dropdown_version) throw new Error('Selected output is not the latest dropdown version');
    await picker.selectOption(expectedKey);
    const selectedLabel = shownVersion.label.replace(/\s*·\s*当前\s*$/, '');
    await page.waitForFunction(({ key, label }) => {
      const selected = document.querySelector('select[aria-label="项目图纸与历史"]');
      const image = document.querySelector('.drawing-image');
      return selected && selected.value === key && image && image.complete
        && image.naturalWidth > 0 && image.naturalHeight > 0 && image.alt.includes(label);
    }, { key: expectedKey, label: selectedLabel });
    report.preview = await page.locator('.drawing-image').evaluate(async (image) => {
      const response = await fetch(image.currentSrc || image.src);
      if (!response.ok) throw new Error('Displayed blob image cannot be read');
      const blob = await response.blob();
      const buffer = await blob.arrayBuffer();
      const hash = await crypto.subtle.digest('SHA-256', buffer);
      return { alt: image.alt, source_type: (image.currentSrc || image.src).split(':')[0],
        content_type: blob.type, bytes: buffer.byteLength,
        sha256: [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, '0')).join(''),
        natural_width: image.naturalWidth, natural_height: image.naturalHeight,
        rendered_width: image.getBoundingClientRect().width, rendered_height: image.getBoundingClientRect().height };
    });
    report.preview.format_control = await page.getByRole('combobox', { name: '预览格式', exact: true }).inputValue();
    await Promise.all(responseReads);
    // Browser response-body capture can be empty after cancellation/cache reuse.
    // Verify the image actually displayed against the independently downloaded
    // published preview, while retaining the original network capture unchanged.
    const format = report.preview.format_control;
    const publishedPreview = artifact.previews?.[format];
    const expectedPreviewPath = `/api/drawings/${input.selected_drawing_id}/revisions/${expectedKey.split(':').at(-1)}/preview`;
    if (!publishedPreview?.sha256 || new URL(publishedPreview.url).pathname !== expectedPreviewPath) {
      throw new Error('Result has no independently hashed preview for the displayed published revision/format');
    }
    report.preview.published_reference = publishedPreview;
    report.checks.actual_image_matches_published_preview = report.preview.sha256 === publishedPreview.sha256
      && report.preview.bytes === publishedPreview.bytes && report.preview.bytes > 0;
    if (!report.checks.actual_image_matches_published_preview) {
      throw new Error('Displayed image bytes differ from the independently downloaded published preview');
    }
    const matchingResponses = report.preview_responses.filter((entry) => {
      const url = new URL(entry.url);
      return entry.status === 200 && url.pathname === expectedPreviewPath && url.searchParams.get('format') === format;
    });
    report.checks.selected_preview_http_success = matchingResponses.length > 0;
    if (!report.checks.selected_preview_http_success) {
      throw new Error('No successful preview HTTP response was observed for the selected revision/format');
    }
    report.raw_response_capture = {
      matching_response_count: matchingResponses.length,
      nonempty_body_count: matchingResponses.filter((entry) => entry.bytes > 0).length,
      any_body_matches_displayed_blob: matchingResponses.some((entry) => entry.sha256 === report.preview.sha256),
      note: 'Raw response bodies are diagnostic only; displayed blob bytes were independently checked against the published preview hash. Empty capture is preserved, not replaced.',
    };
    report.checks.preview_visible = report.preview.rendered_width > 0 && report.preview.rendered_height > 0;
    if (!report.checks.preview_visible) throw new Error('Preview image exists but is not visibly rendered');
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: args.timeout }),
      page.getByRole('button', { name: '下载 DXF', exact: true }).click(),
    ]);
    const failed = await download.failure();
    if (failed) throw new Error(`DXF download failed: ${failed}`);
    const downloadPath = await download.path();
    if (!downloadPath) throw new Error('Browser download did not provide a local payload');
    const downloaded = fs.readFileSync(downloadPath);
    report.download = { suggested_filename: download.suggestedFilename(), bytes: downloaded.length,
      sha256: sha256(downloaded), expected_published_sha256: artifact.download_sha256 || null };
    report.checks.dxf_download_nonempty = downloaded.length > 0;
    report.checks.dxf_download_matches_published = Boolean(artifact.download_sha256)
      && report.download.sha256 === artifact.download_sha256;
    if (!report.checks.dxf_download_nonempty || !report.checks.dxf_download_matches_published) {
      throw new Error('DXF downloaded from the UI differs from the stage2 published artifact or lacks a reference hash');
    }
    if (args.download) {
      fs.mkdirSync(path.dirname(args.download), { recursive: true });
      await download.saveAs(args.download);
      report.download.saved_path = args.download;
    }
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.error = `${error.name}: ${error.message}`;
  } finally {
    if (page) {
      await Promise.allSettled(responseReads);
      try {
        fs.mkdirSync(path.dirname(args.screenshot), { recursive: true });
        await page.screenshot({ path: args.screenshot, fullPage: true, animations: 'disabled', timeout: 30000 });
        report.screenshot_path = args.screenshot;
        report.screenshot_sha256 = sha256(fs.readFileSync(args.screenshot));
      } catch (error) {
        report.screenshot_error = error.message;
        report.status = 'failed';
        report.error ||= 'Required browser screenshot could not be saved';
      }
      report.page_url = page.url();
      report.visible_alerts = await page.getByRole('alert').allTextContents().catch(() => []);
    }
    if (browser) await browser.close().catch((error) => { report.browser_close_error = error.message; });
    report.finished_at = new Date().toISOString();
    fs.mkdirSync(path.dirname(args.output), { recursive: true });
    fs.writeFileSync(args.output, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: report.status, output: args.output,
      screenshot: report.screenshot_path || null, error: report.error || null, checks: report.checks }));
    if (report.status !== 'passed' || report.screenshot_error) process.exitCode = 1;
  }
}

main().catch((error) => { console.error(`${error.name}: ${error.message}`); process.exitCode = 1; });
