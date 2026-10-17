/* Headless UI verification for the browser preview (uses system Chrome). */
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const OUT = path.resolve(__dirname, "../test-output/screens");
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: "new",
    defaultViewport: { width: 1440, height: 900, deviceScaleFactor: 1 },
    args: ["--no-sandbox", "--hide-scrollbars", "--force-device-scale-factor=1"],
    protocolTimeout: 180000,
  });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });

  const enginePort = process.env.TOPSHOT_UI_ENGINE_PORT || "8731";
  const engineToken = process.env.TOPSHOT_UI_ENGINE_TOKEN || "ui-test-token";
  await page.goto(
    `http://127.0.0.1:1420/?enginePort=${encodeURIComponent(enginePort)}&engineToken=${encodeURIComponent(engineToken)}`,
    { waitUntil: "networkidle2", timeout: 60000 },
  );
  await page.waitForSelector(".tile", { timeout: 30000 });
  await page.evaluate(() => {
    const item = [...document.querySelectorAll(".project-item")].find((node) =>
      node.textContent.includes("UI Test Shoot"),
    );
    item?.click();
  });
  await page.waitForFunction(
    () => document.querySelector(".topbar-title h1")?.textContent?.includes("UI Test Shoot"),
    { timeout: 15000 },
  );
  await sleep(1500);

  const shot = async (name) => page.screenshot({ path: path.join(OUT, `${name}.png`) });

  await shot("01-grid");

  // Profile switcher is present and lists local profiles.
  await page.click(".profile-button");
  await page.waitForSelector(".profile-menu", { timeout: 5000 });
  const profiles = await page.evaluate(() => ({
    menu: document.querySelector(".profile-menu")?.innerText?.trim(),
    active: document.querySelector(".topbar-title .eyebrow")?.textContent?.trim(),
  }));
  console.log("PROFILES", JSON.stringify(profiles));
  if (!profiles.menu || !profiles.menu.includes("Default")) throw new Error("profile menu missing");
  await shot("00-profiles");
  await page.click(".profile-backdrop");

  // Hover-targeted shortcut: S toggles the hovered tile without clicking its corner.
  const hoverTarget = await page.evaluate(() => {
    const tile = document.querySelector(".tile.selected");
    if (!tile) return null;
    tile.scrollIntoView({ block: "center" });
    const rect = tile.getBoundingClientRect();
    return { id: tile.getAttribute("data-photo-id"), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  if (!hoverTarget) throw new Error("expected a selected tile to hover");
  await page.mouse.move(hoverTarget.x, hoverTarget.y);
  await sleep(200);
  const selectedBefore = await page.evaluate(() => document.querySelector(".selection-pill")?.textContent?.trim());
  await page.keyboard.press("s");
  await sleep(500);
  const selectedAfter = await page.evaluate(() => document.querySelector(".selection-pill")?.textContent?.trim());
  await page.keyboard.press("s");
  await sleep(500);
  const selectedRestored = await page.evaluate(() => document.querySelector(".selection-pill")?.textContent?.trim());
  console.log("HOVER_SELECT", { id: hoverTarget.id, selectedBefore, selectedAfter, selectedRestored });
  if (!selectedAfter || selectedAfter === selectedBefore) throw new Error("hovered S did not toggle");
  if (selectedRestored !== selectedBefore) throw new Error("hovered S did not restore selection");
  await page.mouse.move(5, 5);
  const initial = await page.evaluate(() => ({
    tiles: document.querySelectorAll(".tile").length,
    bursts: document.querySelectorAll(".burst-collapsed").length,
    chips: [...document.querySelectorAll(".chip")].map((chip) => chip.textContent.trim()),
    selection: document.querySelector(".selection-pill")?.textContent?.trim(),
    toolbar: document.querySelector(".review-toolbar")?.innerText?.trim(),
    scanNote: document.querySelector(".scan-note")?.textContent?.trim(),
  }));
  console.log("INITIAL", JSON.stringify(initial, null, 2));
  if (initial.tiles < 5) throw new Error("expected photo tiles");
  if (initial.bursts < 1) throw new Error("expected a collapsed burst");

  // Set a keep target and verify AI selection becomes exactly N photos.
  await page.evaluate(() => {
    const input = document.querySelector(".target-control input");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(input, "3");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.evaluate(() => {
    [...document.querySelectorAll(".target-control button")]
      .find((button) => button.textContent.trim() === "Apply")
      ?.click();
  });
  await page.waitForFunction(
    () => {
      const chip = [...document.querySelectorAll(".chip")].find((item) =>
        item.textContent.includes("Keeps"),
      );
      return chip && /Keeps\s*3/.test(chip.textContent);
    },
    { timeout: 20000 },
  );
  await sleep(800);
  await shot("02-keep-target");
  const targeted = await page.evaluate(() => ({
    chips: [...document.querySelectorAll(".chip")].map((chip) => chip.textContent.trim()),
    selection: document.querySelector(".selection-pill")?.textContent?.trim(),
    target: document.querySelector(".target-control input")?.value,
    peopleBadges: [...document.querySelectorAll(".badge-people")].map((badge) => badge.textContent.trim()),
  }));
  console.log("TARGETED", JSON.stringify(targeted, null, 2));

  await page.click(".burst-expand");
  await page.waitForSelector(".burst-panel");
  await sleep(400);
  await shot("03-burst-expanded");
  const burst = await page.evaluate(() => ({
    panel: document.querySelector(".burst-panel")?.innerText?.trim().slice(0, 120),
    members: document.querySelectorAll(".burst-members .tile").length,
  }));
  console.log("BURST", burst);

  await page.click(".burst-members .tile");
  await sleep(150);
  await page.keyboard.press("Space");
  await page.waitForSelector(".loupe", { timeout: 10000 });
  await sleep(700);
  await shot("04-loupe-fit");
  const loupe = await page.evaluate(() => {
    const canvas = document.querySelector(".loupe-canvas")?.getBoundingClientRect();
    const image = document.querySelector(".loupe-canvas img")?.getBoundingClientRect();
    return {
      title: document.querySelector(".loupe-title")?.innerText?.trim(),
      suggestion: document.querySelector(".loupe-suggestion")?.innerText?.trim(),
      meta: document.querySelector(".loupe-meta")?.innerText?.trim(),
      fit:
        canvas && image
          ? image.top >= canvas.top - 1 &&
            image.bottom <= canvas.bottom + 1 &&
            image.left >= canvas.left - 1 &&
            image.right <= canvas.right + 1
          : false,
    };
  });
  console.log("LOUPE", JSON.stringify(loupe, null, 2));
  if (!loupe.fit) throw new Error("loupe fit crops the image");

  const selectionBefore = await page.evaluate(() => document.querySelector(".selection-pill")?.textContent?.trim());
  await page.keyboard.press("s");
  await sleep(450);
  const selectionAfter = await page.evaluate(() => document.querySelector(".selection-pill")?.textContent?.trim());
  await page.keyboard.press("s");
  await sleep(450);
  const selectionRestored = await page.evaluate(() => document.querySelector(".selection-pill")?.textContent?.trim());
  await page.keyboard.press("1");
  await sleep(450);
  const rated = await page.evaluate(() => document.querySelectorAll(".loupe .star.on").length);
  await page.keyboard.press("u");
  await sleep(450);
  const unrated = await page.evaluate(() => document.querySelectorAll(".loupe .star.on").length);
  const firstTitle = await page.evaluate(() => document.querySelector(".loupe-title strong")?.textContent);
  await page.keyboard.press("ArrowRight");
  await sleep(450);
  const secondTitle = await page.evaluate(() => document.querySelector(".loupe-title strong")?.textContent);
  await page.keyboard.press("ArrowLeft");
  await sleep(450);
  await page.keyboard.press("Space");
  await sleep(350);
  const gentleZoom = await page.evaluate(() => document.querySelector(".loupe-meta")?.innerText?.includes("1.5× fit"));
  await page.keyboard.press("Space");
  await sleep(350);
  const zoomed = await page.evaluate(() => document.querySelector(".loupe-meta")?.innerText?.includes("100% proxy"));
  await page.keyboard.press("Space");
  await sleep(350);
  const zoomed2 = await page.evaluate(() => document.querySelector(".loupe-meta")?.innerText?.includes("200% proxy"));
  const zoomCursor = await page.evaluate(() => document.querySelector(".loupe-canvas")?.classList.contains("zoom-out-cursor"));
  await page.keyboard.press("Space");
  await sleep(350);
  const fitted = await page.evaluate(() => document.querySelector(".loupe-meta")?.innerText?.includes("Fit"));
  const stripBefore = await page.evaluate(() => Boolean(document.querySelector(".loupe-strip")));
  await page.keyboard.press("b");
  await sleep(350);
  const stripHidden = await page.evaluate(() => !document.querySelector(".loupe-strip"));
  await page.keyboard.press("b");
  await sleep(350);
  const stripRestored = await page.evaluate(() => Boolean(document.querySelector(".loupe-strip")));
  console.log("LOUPE_SHORTCUTS", {
    selectionBefore,
    selectionAfter,
    selectionRestored,
    rated,
    unrated,
    navigated: firstTitle !== secondTitle,
    gentleZoom,
    zoomed,
    zoomed2,
    zoomCursor,
    fitted,
    stripBefore,
    stripHidden,
    stripRestored,
  });
  if (selectionAfter === selectionBefore || selectionRestored !== selectionBefore) {
    throw new Error("loupe S shortcut failed");
  }
  if (rated !== 1 || unrated !== 0) throw new Error("loupe rating/unrate failed");
  if (firstTitle === secondTitle) throw new Error("loupe arrow navigation failed");
  if (!gentleZoom || !zoomed || !zoomed2 || !fitted) throw new Error("loupe zoom stages failed");
  if (!zoomCursor) throw new Error("loupe zoom-out cursor missing at 200%");
  if (stripBefore && (!stripHidden || !stripRestored)) throw new Error("loupe B strip toggle failed");
  await shot("04b-loupe-shortcuts");

  await page.evaluate(() => {
    [...document.querySelectorAll(".loupe-actions button")]
      .find((button) => button.textContent.trim() === "100%")
      ?.click();
  });
  await sleep(400);
  await shot("05-loupe-100");
  await page.keyboard.press("Escape");
  await page.waitForSelector(".loupe", { hidden: true });

  // Keyboard reject + undo, then select the review photo (with XMP sidecar).
  await page.keyboard.press("x");
  await sleep(500);
  const rejectToast = await page.$eval(".toast", (node) => node.textContent).catch(() => null);
  await page.keyboard.down("Meta");
  await page.keyboard.press("z");
  await page.keyboard.up("Meta");
  await sleep(500);
  const undoToast = await page.$eval(".toast", (node) => node.textContent).catch(() => null);
  console.log("KEYBOARD", { rejectToast, undoToast });

  const selectedReview = await page.evaluate(() => {
    const button = document.querySelector('[aria-label="Select 20260912_082510.jpg"]');
    if (!button) return "missing";
    if (!button.classList.contains("checked")) button.click();
    return button.classList.contains("checked");
  });
  console.log("SELECTED_REVIEW_PHOTO", selectedReview);
  await sleep(600);
  await shot("06-selected");

  await page.evaluate(() => {
    const button = [...document.querySelectorAll(".toolbar-meta button")].find((item) =>
      item.textContent.includes("Copy Keeps"),
    );
    button?.click();
  });
  await page.waitForSelector(".modal", { timeout: 15000 });
  await sleep(700);
  await shot("07-copy-dialog");
  const dialog = await page.$eval(".modal", (node) => node.innerText);
  console.log("COPY DIALOG\n" + dialog);

  await page.click(".modal .modal-actions .primary");
  await page.waitForFunction(
    () => {
      const modal = document.querySelector(".modal");
      return modal && /Copy complete|Copy finished|Copy canceled/.test(modal.innerText);
    },
    { timeout: 120000 },
  );
  await sleep(900);
  await shot("08-copy-summary");
  const summary = await page.$eval(".modal", (node) => node.innerText).catch(() => null);
  console.log("COPY SUMMARY\n" + summary);

  // Close summary and exercise a filter chip.
  await page.evaluate(() => {
    [...document.querySelectorAll(".modal button")]
      .find((button) => button.textContent.includes("Back to review"))
      ?.click();
  });
  await sleep(400);
  await page.evaluate(() => {
    [...document.querySelectorAll(".chip")].find((chip) => chip.textContent.includes("Rejects"))?.click();
  });
  await sleep(500);
  await shot("09-rejects-filter");
  const rejected = await page.evaluate(() => ({
    tiles: document.querySelectorAll(".tile").length,
    empty: document.querySelector(".grid-empty")?.innerText?.trim(),
  }));
  console.log("REJECTS_FILTER", rejected);

  console.log("PAGE_ERRORS", errors);
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
