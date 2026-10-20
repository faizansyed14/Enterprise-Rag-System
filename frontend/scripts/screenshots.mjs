// Visual check of every page: signs in, asks a demo question, opens a source, and saves screenshots of each page in
// both themes (and the chat at phone width) to ./screenshots. Needs the stack running with the demo library:
//   scripts/start.sh && scripts/demo.sh        (or: rag demo seed --wait)
//   BASE_URL=http://localhost:3000 npm run screenshots
// Environment: BASE_URL, DEMO_EMAIL, DEMO_PASSWORD, CHROMIUM_PATH (optional browser binary), THEMES=dark,light

import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { chromium } from "playwright";

const base = process.env.BASE_URL || "http://localhost:3000";
const email = process.env.DEMO_EMAIL || "admin@demo.local";
const password = process.env.DEMO_PASSWORD || "ChangeMe-Demo-2025";
const themes = (process.env.THEMES || "dark,light").split(",");
const out = join(process.cwd(), "screenshots");
mkdirSync(out, { recursive: true });

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const failures = [];

async function shot(page, name) {
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(out, `${name}.png`), fullPage: false });
  console.log(`saved ${name}.png`);
}

for (const theme of themes) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: theme === "dark" ? "dark" : "light" });
  await context.addInitScript((t) => localStorage.setItem("rag-theme", t), theme);
  const page = await context.newPage();
  page.on("pageerror", (error) => failures.push(`${theme}: ${error.message}`));
  // The sign-in page checks for an existing session first; its 401 is expected.
  page.on("console", (message) => message.type() === "error" && !/status of 401/.test(message.text()) && failures.push(`${theme} console: ${message.text()}`));

  await page.goto(`${base}/login`);
  await page.getByLabel("E-mail").waitFor();
  await shot(page, `${theme}-01-login`);
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${base}/`);
  await page.getByRole("heading", { name: "Ask your project documents" }).waitFor();
  await shot(page, `${theme}-02-ask`);
  if (theme === themes[0]) {
    await page.getByRole("button", { name: /^Filters/ }).click();
    await page.getByText("Search only in").waitFor();
    await shot(page, `${theme}-02b-filters`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: /Automatic/ }).click();
    await page.getByText("Answer with").waitFor();
    await shot(page, `${theme}-02c-models`);
    await page.keyboard.press("Escape");
  }

  await page.getByLabel("Question").fill("What concrete grade is specified for the columns?");
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: /^Open source 1/ }).first().waitFor({ timeout: 60_000 });
  await page.waitForTimeout(800);
  await shot(page, `${theme}-03-answer`);
  await page.getByRole("button", { name: /^Open source 1/ }).first().click();
  await page.getByRole("dialog").getByText("Source 1").waitFor();
  await page.waitForTimeout(1500);
  await shot(page, `${theme}-04-citation`);
  await page.keyboard.press("Escape");

  // A question the library cannot answer: nothing is guessed.
  await page.getByLabel("Question").fill("What is the warranty period for the passenger lifts?");
  await page.keyboard.press("Enter");
  await page.getByText("Not answered by the documents you can open").waitFor({ timeout: 60_000 });
  await shot(page, `${theme}-04b-not-found`);

  await page.goto(`${base}/documents`);
  await page.getByRole("heading", { name: "Documents" }).waitFor();
  await page.locator("tbody tr").first().waitFor();
  await shot(page, `${theme}-05-documents`);
  if (theme === themes[0]) {
    await page.getByRole("button", { name: "Upload" }).first().click();
    await page.getByRole("dialog", { name: "Upload documents" }).waitFor();
    await shot(page, `${theme}-05b-upload`);
    await page.keyboard.press("Escape");
  }
  await page.locator("tbody tr").first().click();
  await page.getByText("Title block").waitFor();
  await page.waitForTimeout(1500);
  await shot(page, `${theme}-06-document`);

  for (const [path, heading, name] of [
    ["/models", "Models", "07-models"],
    ["/sources", "Sources", "08-sources"],
    ["/monitor", "Monitor", "09-monitor"],
    ["/evaluation", "Evaluation", "11-evaluation"],
    ["/users", "Users", "12-users"],
  ]) {
    await page.goto(`${base}${path}`);
    await page.getByRole("heading", { name: heading, exact: true }).waitFor();
    await page.waitForTimeout(1500);
    await shot(page, `${theme}-${name}`);
  }

  if (theme === themes[0]) {
    await page.goto(`${base}/sources`);
    await page.getByRole("button", { name: "Add source" }).first().click();
    await page.getByRole("dialog").getByText("SharePoint site").waitFor();
    await shot(page, `${theme}-08b-add-source`);
    await page.getByRole("dialog").getByText("SharePoint site").click();
    await shot(page, `${theme}-08c-sharepoint`);
    await page.keyboard.press("Escape");

    await page.goto(`${base}/models`);
    await page.getByRole("button", { name: "Switch", exact: true }).first().click();
    await page.getByRole("dialog").waitFor();
    await shot(page, `${theme}-07b-switch`);
    await page.keyboard.press("Escape");

    await page.goto(`${base}/monitor`);
    await page.getByText("Recent questions").waitFor();
    await page.locator("tr", { hasText: "admin@demo.local" }).first().click();
    await page.getByRole("dialog").getByText("Model call").waitFor({ timeout: 30_000 });
    await shot(page, `${theme}-09b-trace`);
    await page.keyboard.press("Escape");

    await page.goto(`${base}/evaluation`);
    await page.getByRole("button", { name: "Run", exact: true }).click();
    await page.getByText("Done", { exact: true }).first().waitFor({ timeout: 180_000 });
    await page.waitForTimeout(1000);
    await shot(page, `${theme}-11b-evaluation-run`);
    await page.locator("tbody tr").first().click();
    await page.getByRole("dialog").waitFor();
    await page.waitForTimeout(800);
    await shot(page, `${theme}-11c-evaluation-items`);
    await page.keyboard.press("Escape");
  }

  await page.goto(`${base}/lab`);
  await page.getByLabel("Question").fill("RFI 23 response");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("heading", { name: "How the question was read" }).waitFor({ timeout: 60_000 });
  await shot(page, `${theme}-10-lab`);
  await context.close();
}

// Phone width: the chat and the menu.
const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
await phone.addInitScript(() => localStorage.setItem("rag-theme", "dark"));
const mobile = await phone.newPage();
await mobile.goto(`${base}/login`);
await mobile.getByLabel("E-mail").fill(email);
await mobile.getByLabel("Password").fill(password);
await mobile.getByRole("button", { name: "Sign in" }).click();
await mobile.waitForURL(`${base}/`);
await mobile.getByLabel("Question").fill("What was the engineer's response to RFI-023?");
await mobile.keyboard.press("Enter");
await mobile.getByRole("button", { name: /^Open source 1/ }).first().waitFor({ timeout: 60_000 });
await shot(mobile, "phone-01-answer");
await mobile.getByRole("button", { name: "Open menu" }).click();
await shot(mobile, "phone-02-menu");
await phone.close();

await browser.close();
if (failures.length) {
  console.error(`\n${failures.length} browser errors:\n${failures.join("\n")}`);
  process.exitCode = 1;
}
