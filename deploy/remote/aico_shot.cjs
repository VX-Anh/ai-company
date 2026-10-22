// Phần chụp của aico-shot: đọc {chromium, wait, shots[]} từ stdin, in JSON kết quả ra stdout.
// Chromium chạy --no-sandbox: sandbox của Chromium cần namespace mà container worker không cho;
// container (không secret của aico, thư mục job tạm) là lớp cô lập, như Codex ở chế độ ghi.
const fs = require("fs");
const { chromium } = require("playwright-core");

const MAX_ERRORS = 20;
const clip = (text) => String(text).slice(0, 300);

// Đếm khung requestAnimationFrame trong 1 giây.
const measureFps = () => new Promise((done) => {
  let frames = 0;
  const start = performance.now();
  const step = () => {
    frames++;
    const elapsed = performance.now() - start;
    if (elapsed < 1000) requestAnimationFrame(step);
    else done(Math.round((frames * 1000) / elapsed));
  };
  requestAnimationFrame(step);
});

async function shoot(browser, shot, wait) {
  const page = await browser.newPage({ viewport: { width: shot.width, height: shot.height } });
  const errors = [];
  const note = (text) => { if (errors.length < MAX_ERRORS) errors.push(clip(text)); };
  page.on("console", (msg) => {
    // "Failed to load resource" trùng với dòng HTTP có địa chỉ bên dưới (hoặc là favicon).
    if (msg.text().startsWith("Failed to load resource")) return;
    if (msg.type() === "error" || msg.type() === "warning") note(`${msg.type()}: ${msg.text()}`);
  });
  page.on("pageerror", (err) => note(`pageerror: ${err && err.message ? err.message : err}`));
  page.on("requestfailed", (req) => note(`requestfailed: ${req.url()} ${req.failure()?.errorText ?? ""}`));
  // Console chỉ báo "404 (Not Found)" không kèm địa chỉ: ghi riêng để biết file nào thiếu.
  page.on("response", (resp) => { if (resp.status() >= 400) note(`HTTP ${resp.status()}: ${resp.url()}`); });
  let status = null;
  try {
    const resp = await page.goto(shot.url, { waitUntil: "load", timeout: 30000 });
    status = resp ? resp.status() : null;
    await page.waitForTimeout(wait);
    const fps = await page.evaluate(measureFps);
    await page.screenshot({ path: shot.path });
    return { ...shot, status, fps, errors };
  } catch (err) {
    return { ...shot, status, errors, error: clip(err && err.message ? err.message : err) };
  } finally {
    await page.close();
  }
}

async function main() {
  const spec = JSON.parse(fs.readFileSync(0, "utf8"));
  const browser = await chromium.launch({
    executablePath: spec.chromium,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const results = [];
  try {
    for (const shot of spec.shots) results.push(await shoot(browser, shot, spec.wait));
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(results));
}

main().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
