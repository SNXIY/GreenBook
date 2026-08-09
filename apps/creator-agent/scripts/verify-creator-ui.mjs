import path from "node:path";
import process from "node:process";
import { chromium } from "playwright-core";

const baseUrl = process.env.CREATOR_UI_URL || "http://127.0.0.1:8092";
const zhiguangToken = process.env.CREATOR_UI_ZHIGUANG_TOKEN || "";
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const outputRoot = path.resolve("target");

const browser = await chromium.launch({
  executablePath,
  headless: true,
});

try {
  const desktop = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const desktopErrors = captureErrors(desktop);
  await openStudio(desktop);
  await desktop.locator("#documentEditor").waitFor({ state: "visible" });
  await assertNoHorizontalOverflow(desktop, "desktop");
  await assertDesktopPanes(desktop);

  await desktop.locator('[data-library-view="projects"]').click();
  await desktop.locator("#openCreateProject").click();
  await assertDialogFits(desktop, "#projectDialog", "project dialog");
  await desktop.locator('[data-close-project]').first().click();

  await desktop.locator('[data-library-view="materials"]').click();
  await desktop.locator("#openCreateMaterial").click();
  await assertDialogFits(desktop, "#materialDialog", "material dialog");
  await desktop.locator('[data-close-material]').first().click();
  if (process.env.CREATOR_UI_VERIFY_AI === "true") {
    await verifyLiveSuggestion(desktop);
  }
  await desktop.screenshot({
    path: path.join(outputRoot, "creator-studio-desktop.png"),
    fullPage: true,
  });
  assertNoRuntimeErrors(desktopErrors, "desktop");

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const mobileErrors = captureErrors(mobile);
  await openStudio(mobile);
  await assertOnlyMobilePaneVisible(mobile, "library");
  await assertNoHorizontalOverflow(mobile, "mobile library");

  await mobile.locator('[data-mobile-target="editor"]').click();
  await mobile.locator("#documentEditor").waitFor({ state: "visible" });
  await assertNoHorizontalOverflow(mobile, "mobile editor");
  await assertOnlyMobilePaneVisible(mobile, "editor");

  await mobile.locator('[data-mobile-target="library"]').click();
  await assertOnlyMobilePaneVisible(mobile, "library");
  await assertNoHorizontalOverflow(mobile, "mobile library");

  await mobile.locator('[data-mobile-target="assistant"]').click();
  await assertOnlyMobilePaneVisible(mobile, "assistant");
  await assertNoHorizontalOverflow(mobile, "mobile assistant");
  await mobile.screenshot({
    path: path.join(outputRoot, "creator-studio-mobile.png"),
    fullPage: true,
  });
  assertNoRuntimeErrors(mobileErrors, "mobile");

  process.stdout.write(
    JSON.stringify(
      {
        status: "ok",
        baseUrl,
        screenshots: [
          path.join(outputRoot, "creator-studio-desktop.png"),
          path.join(outputRoot, "creator-studio-mobile.png"),
        ],
      },
      null,
      2,
    ) + "\n",
  );
} finally {
  await browser.close();
}

async function openStudio(page) {
  const entry = zhiguangToken
    ? `${baseUrl}/creator.html#zhiguang_token=${encodeURIComponent(zhiguangToken)}`
    : `${baseUrl}/`;
  await page.goto(entry, { waitUntil: "domcontentloaded" });
  if (!zhiguangToken) {
    await page.waitForURL("**/creator.html", { timeout: 15_000 });
  }
  await page.locator("#apiState").waitFor({ state: "attached" });
  await page.waitForFunction(
    () => {
      const badge = document.querySelector("#apiState");
      return badge && !badge.classList.contains("is-loading");
    },
    null,
    { timeout: 15_000 },
  );
}

function captureErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 500) {
      errors.push(`http ${response.status()}: ${response.url()}`);
    }
  });
  return errors;
}

function assertNoRuntimeErrors(errors, label) {
  if (errors.length) {
    throw new Error(`${label} runtime errors:\n${errors.join("\n")}`);
  }
}

async function assertNoHorizontalOverflow(page, label) {
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  if (
    dimensions.document > dimensions.viewport + 1 ||
    dimensions.body > dimensions.viewport + 1
  ) {
    throw new Error(`${label} has horizontal overflow: ${JSON.stringify(dimensions)}`);
  }
}

async function assertDesktopPanes(page) {
  const boxes = await Promise.all(
    [".library-pane", ".document-pane", ".assistant-pane"].map((selector) =>
      page.locator(selector).boundingBox(),
    ),
  );
  if (boxes.some((box) => !box)) throw new Error("a desktop pane is not visible");
  const [library, editor, assistant] = boxes;
  if (
    library.x + library.width > editor.x + 1 ||
    editor.x + editor.width > assistant.x + 1
  ) {
    throw new Error(`desktop panes overlap: ${JSON.stringify(boxes)}`);
  }
}

async function assertDialogFits(page, selector, label) {
  const box = await page.locator(selector).boundingBox();
  const viewport = page.viewportSize();
  if (
    !box ||
    !viewport ||
    box.x < 0 ||
    box.y < 0 ||
    box.x + box.width > viewport.width + 1 ||
    box.y + box.height > viewport.height + 1
  ) {
    throw new Error(`${label} does not fit viewport: ${JSON.stringify({ box, viewport })}`);
  }
}

async function assertOnlyMobilePaneVisible(page, target) {
  const visible = await page.evaluate(() => {
    const selectors = {
      library: ".library-pane",
      editor: ".document-pane",
      assistant: ".assistant-pane",
    };
    return Object.fromEntries(
      Object.entries(selectors).map(([name, selector]) => {
        const element = document.querySelector(selector);
        return [name, Boolean(element && getComputedStyle(element).display !== "none")];
      }),
    );
  });
  for (const [name, isVisible] of Object.entries(visible)) {
    if (isVisible !== (name === target)) {
      throw new Error(`mobile ${target} pane state is invalid: ${JSON.stringify(visible)}`);
    }
  }
}

async function verifyLiveSuggestion(page) {
  const paragraph = page.locator(".ProseMirror p").filter({ hasText: /.+/ }).first();
  await paragraph.click({ clickCount: 3 });
  await page.locator("#selectionAssistant").waitFor({ state: "visible" });
  await page.locator('[data-editor-assist="rewrite"]').click();
  await page.locator("#aiSuggestionPanel").waitFor({
    state: "visible",
    timeout: 120_000,
  });
  const originalText = await page.locator(".diff-side.is-before del").innerText();
  const replacementText = await page.locator(".diff-side.is-after ins").innerText();
  if (!originalText.trim() || originalText.trim() === replacementText.trim()) {
    throw new Error("live AI suggestion did not produce a reviewable change");
  }
  await page.screenshot({
    path: path.join(outputRoot, "creator-studio-suggestion.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "不采用", exact: true }).click();
  await page.locator("#aiSuggestionPanel").waitFor({ state: "hidden" });
}
