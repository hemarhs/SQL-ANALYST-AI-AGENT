"""Drives the real UI in Chromium: signup, multi-upload, select, ask, delete.
Captures screenshots and fails on any console error."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
SHOTS.mkdir(exist_ok=True)
DATA = HERE / "data"
LONG = DATA / "quarterly_regional_sales_performance_and_forecast_rollup_with_channel_breakdown_final_v7_2025.csv"

errors, results = [], []


def ok(name, cond, extra=""):
    results.append((name, cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   <- {extra}" if extra and not cond else ""))


def run(pw, theme="light"):
    b = pw.chromium.launch()
    ctxb = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2,
                         color_scheme="dark" if theme == "dark" else "light")
    page = ctxb.new_page()
    page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#auth:not([hidden])")
    page.click("#tabSignup")
    page.fill("#f-name", "Hema")
    page.fill("#f-email", f"hema+{theme}@example.com")
    page.fill("#f-pw", "supersecret1")
    page.screenshot(path=SHOTS / f"01-auth-{theme}.png")
    page.click("#authBtn")
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    page.wait_for_timeout(500)
    ok(f"[{theme}] signed in and app shown", page.is_visible("#app"))
    page.screenshot(path=SHOTS / f"02-sample-empty-{theme}.png")

    # --- sample data question ---
    page.click(".chip >> nth=0")
    page.wait_for_selector(".card .answer", timeout=20000)
    page.wait_for_timeout(700)
    ok(f"[{theme}] sample question answered", page.locator(".card .answer").count() >= 1)
    page.screenshot(path=SHOTS / f"03-sample-answer-{theme}.png")

    # --- switch to my files and upload three CSVs at once ---
    page.click('#srcSeg button[data-s="mine"]')
    page.wait_for_selector("#uploadBox:not([hidden])")
    page.screenshot(path=SHOTS / f"04-mydata-empty-{theme}.png")
    page.set_input_files("#fileInput", [str(LONG), str(DATA / "support_tickets.csv"),
                                        str(DATA / "employee_headcount.csv")])
    page.wait_for_function("document.querySelectorAll('#dsList .ds').length === 3", timeout=30000)
    page.wait_for_timeout(600)
    ok(f"[{theme}] three datasets listed", page.locator("#dsList .ds").count() == 3)

    # the long-filename bug: the name must stay inside the sidebar
    side = page.locator("aside").bounding_box()
    names = page.locator("#dsList .ds-name")
    overflow = []
    for i in range(names.count()):
        bb = names.nth(i).bounding_box()
        if bb["x"] + bb["width"] > side["x"] + side["width"] + 1:
            overflow.append(names.nth(i).inner_text()[:40])
    ok(f"[{theme}] long filenames stay inside the sidebar", not overflow, overflow)
    ok(f"[{theme}] no horizontal page scroll",
       page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
    page.screenshot(path=SHOTS / f"05-datasets-{theme}.png")

    # --- select the long-named dataset explicitly ---
    page.click('#dsList .ds[data-t^="quarterly"] [data-a="pick"]')
    page.wait_for_timeout(600)
    ok(f"[{theme}] selected dataset marked active",
       page.locator('#dsList .ds[data-t^="quarterly"].on').count() == 1)
    ctx = page.locator("#ctxName").inner_text()
    ok(f"[{theme}] header shows the active file", ctx.startswith("quarterly"), ctx)

    # column list
    page.click('#dsList .ds[data-t^="quarterly"] [data-a="cols"]')
    page.wait_for_timeout(300)
    ok(f"[{theme}] columns expand", page.locator('#dsList .ds[data-t^="quarterly"] .colrow').count() == 8)
    page.screenshot(path=SHOTS / f"06-columns-{theme}.png")

    # preview modal
    page.click('#dsList .ds[data-t^="quarterly"] [data-a="peek"]')
    page.wait_for_selector(".modal table tbody tr", timeout=10000)
    ok(f"[{theme}] preview shows rows", page.locator(".modal table tbody tr").count() == 15)
    page.screenshot(path=SHOTS / f"07-preview-{theme}.png")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # --- ask about the uploaded file ---
    page.fill("#q", "Total revenue by region")
    page.click("#send")
    page.wait_for_selector(".msg:last-child .card .answer", timeout=25000)
    page.wait_for_timeout(700)
    answer = page.locator(".card .answer").last.inner_text()
    ok(f"[{theme}] answer mentions the uploaded data", "North America" in answer, answer[:80])
    ok(f"[{theme}] result table rendered", page.locator(".msg:last-child table tbody tr").count() == 4)
    page.screenshot(path=SHOTS / f"08-answer-table-{theme}.png")

    # chart tab
    page.click('.msg:last-child .tab[data-t="chart"]')
    page.wait_for_timeout(900)
    ok(f"[{theme}] chart renders", page.locator(".msg:last-child canvas").count() == 1)
    page.screenshot(path=SHOTS / f"09-answer-chart-{theme}.png")

    # SQL panel
    page.click(".msg:last-child details.more summary")
    page.wait_for_timeout(400)
    ok(f"[{theme}] SQL shown", "SELECT" in page.locator(".msg:last-child pre").inner_text())
    page.screenshot(path=SHOTS / f"10-sql-trace-{theme}.png")

    # time-series question -> line chart
    page.fill("#q", "Show revenue by month")
    page.click("#send")
    page.wait_for_selector(".msg:last-child .card .answer", timeout=25000)
    page.wait_for_timeout(500)
    page.click('.msg:last-child .tab[data-t="chart"]')
    page.wait_for_timeout(900)
    ok(f"[{theme}] line chart for a time series", page.locator(".msg:last-child canvas").count() == 1)
    page.screenshot(path=SHOTS / f"11-line-chart-{theme}.png")

    # single-value answers render as a figure, not a one-cell table
    page.fill("#q", "How many rows are there?")
    page.click("#send")
    page.wait_for_selector(".msg:last-child .card .answer", timeout=25000)
    page.wait_for_timeout(600)
    ok(f"[{theme}] scalar answer shown as a stat, not a table",
       page.locator(".msg:last-child .stat-value").count() == 1
       and page.locator(".msg:last-child table").count() == 0)
    page.screenshot(path=SHOTS / f"17-stat-{theme}.png")

    # --- switching datasets keeps things consistent ---
    page.click('#dsList .ds[data-t="support_tickets"] [data-a="pick"]')
    page.wait_for_timeout(700)
    ok(f"[{theme}] switched active dataset", page.locator("#ctxName").inner_text() == "support_tickets.csv")

    # --- delete a dataset ---
    page.click('#dsList .ds[data-t="employee_headcount"] [data-a="del"]')
    page.wait_for_selector(".modal")
    page.screenshot(path=SHOTS / f"12-delete-confirm-{theme}.png")
    page.click(".modal [data-ok]")
    page.wait_for_function("document.querySelectorAll('#dsList .ds').length === 2", timeout=10000)
    ok(f"[{theme}] dataset deleted from the panel", page.locator("#dsList .ds").count() == 2)

    # --- reload keeps history, source and selection ---
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    page.wait_for_timeout(1200)
    ok(f"[{theme}] history restored after reload", page.locator(".msg .card").count() >= 3)
    ok(f"[{theme}] active dataset restored", page.locator("#ctxName").inner_text() == "support_tickets.csv")
    page.screenshot(path=SHOTS / f"13-restored-{theme}.png", full_page=False)

    if theme == "light":
        # theme toggle
        page.click("#themeBtn")
        page.wait_for_timeout(600)
        ok("theme toggle switches to dark", page.evaluate("document.documentElement.dataset.theme") == "dark")
        page.screenshot(path=SHOTS / "14-dark-toggle.png")
        page.click("#themeBtn")
        page.wait_for_timeout(400)

        # mobile
        m = ctxb.new_page()
        m.set_viewport_size({"width": 390, "height": 844})
        m.on("pageerror", lambda e: errors.append(f"[mobile pageerror] {e}"))
        m.goto(BASE, wait_until="networkidle")
        m.wait_for_selector("#app:not([hidden])", timeout=15000)
        m.wait_for_timeout(1200)
        ok("mobile: no horizontal scroll",
           m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
           m.evaluate("document.documentElement.scrollWidth + ' vs ' + window.innerWidth"))
        m.screenshot(path=SHOTS / "15-mobile-chat.png")
        m.click("#openSide")
        m.wait_for_timeout(500)
        ok("mobile: drawer opens", m.locator("#app.open").count() == 1)
        bb_side = m.locator("aside").bounding_box()
        nm = m.locator("#dsList .ds-name")
        bad = [nm.nth(i).inner_text()[:30] for i in range(nm.count())
               if nm.nth(i).bounding_box()["x"] + nm.nth(i).bounding_box()["width"] > bb_side["x"] + bb_side["width"] + 1]
        ok("mobile: filenames stay inside the drawer", not bad, bad)
        m.screenshot(path=SHOTS / "16-mobile-drawer.png")
        m.close()

    b.close()


import os, shutil, signal, subprocess, time, urllib.request

HERE_ROOT = HERE.parent
DEMO_DIR = "/tmp/sqa-demo-ui"
shutil.rmtree(DEMO_DIR, ignore_errors=True)
env = {**os.environ, "DEMO_DIR": DEMO_DIR}
srv = subprocess.Popen([sys.executable, str(HERE / "demo_server.py")], cwd=str(HERE_ROOT), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=1).read()
        break
    except Exception:
        time.sleep(0.5)

try:
    with sync_playwright() as pw:
        run(pw, "light")
        run(pw, "dark")
finally:
    os.killpg(os.getpgid(srv.pid), signal.SIGTERM)

print("\n" + "=" * 46)
bad = [n for n, c in results if not c]
print(f"{len(results) - len(bad)} passed, {len(bad)} failed")
if errors:
    print("\nBROWSER ERRORS:")
    for e in dict.fromkeys(errors):
        print("  -", e)
print("screenshots:", SHOTS)
sys.exit(1 if bad or errors else 0)
