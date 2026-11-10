"""Browser workflow check using a fake CLI and a temporary home, never real tokens."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
from test_app import app, FAKE_CLI, ROOT


def run():
    with tempfile.TemporaryDirectory(prefix="claude-web-browser-") as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        environment = patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")})
        environment.start()
        cli = root / "claude"
        cli.write_text(FAKE_CLI)
        cli.chmod(0o700)
        manager = app.Manager(root / "data", home, str(cli), auto_import=False)
        ident = manager.create("工作号")
        (manager.profile(manager.account(ident)) / ".signed-in").write_text("ok")
        (manager.profile(manager.account(ident)) / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"accountUuid": "browser-owner"},
            "cachedUsageUtilization": {"accountUuid": "browser-owner", "fetchedAtMs": datetime.now(timezone.utc).timestamp()*1000,
                "utilization": {"five_hour": {"utilization": 50}}}}))
        sample = {"type": "assistant", "requestId": "browser-request", "timestamp": datetime.now(timezone.utc).isoformat(),
                  "message": {"id": "browser-message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 10000}}}
        (manager.shared / "browser-session.jsonl").write_text(json.dumps(sample) + "\n" + json.dumps(sample) + "\n")
        codex = home / ".codex/sessions"
        codex.mkdir(parents=True)
        from test_codex import record, sample as codex_sample
        (codex / "rollout.jsonl").write_bytes(record("session_meta", {"id": "browser-codex"}) +
            record("turn_context", {"model": "gpt-5.6-sol"}) + codex_sample(2000, 100, 1000,
                limits={"primary": {"used_percent": 35, "window_minutes": 10080}}))
        manager.refresh(ident)
        with patch.object(app, "claude_processes", return_value=[]):
            manager.activate(ident)
            server = app.Server(("127.0.0.1", 0), manager, password="browser-test-only")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CLAUDE_WEB_TEST_BROWSER"))
                    page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    page.get_by_label("密码", exact=True).fill("wrong")
                    page.get_by_role("button", name="登录", exact=True).click()
                    page.get_by_role("alert").filter(has_text="密码不正确").wait_for()
                    page.get_by_label("密码", exact=True).fill("browser-test-only")
                    page.get_by_role("button", name="登录", exact=True).click()
                    page.get_by_role("tablist", name="功能菜单").wait_for()
                    menu = page.get_by_role("tablist", name="功能菜单")
                    assert page.locator("#accounts-page").is_visible()
                    assert not page.locator("#usage").is_visible()
                    menu.get_by_role("tab", name="用量统计", exact=True).click()
                    page.locator("#model-rows").filter(has_text="claude-opus-5-5").wait_for(timeout=15000)
                    page.get_by_role("group", name="用量来源").get_by_role("button", name="Codex", exact=True).click()
                    page.locator("#model-rows").filter(has_text="Codex · gpt-5.6-sol").wait_for()
                    expect(page.locator("#model-rows")).not_to_contain_text("claude-opus")
                    menu.get_by_role("tab", name="账号额度", exact=True).click()
                    page.get_by_text("35% 已用", exact=True).wait_for()
                    expect(page.get_by_role("group", name="额度来源").get_by_role("button", name="Codex", exact=True)).to_have_attribute("aria-pressed", "true")
                    assert "7 天" in page.locator("#quota-rows").inner_text()
                    page.get_by_role("group", name="额度来源").get_by_role("button", name="Claude", exact=True).click()
                    menu.get_by_role("tab", name="用量统计", exact=True).click()
                    page.locator("#model-rows").filter(has_text="claude-opus-5-5").wait_for()
                    page.get_by_role("button", name="今天", exact=True).click()
                    page.get_by_role("img", name="所选区间按小时Token趋势").wait_for()
                    page.get_by_role("button", name="近 30 天", exact=True).click()
                    page.get_by_label("趋势指标").select_option("usd")
                    page.get_by_role("img", name="所选区间按日API 等价费用趋势").wait_for()
                    today = datetime.now().astimezone().date().isoformat()
                    page.get_by_role('button', name='自选', exact=True).click()
                    page.get_by_label('开始日期', exact=True).fill(today)
                    page.get_by_label('结束日期', exact=True).fill(today)
                    page.get_by_role('button', name='应用', exact=True).click()
                    page.get_by_role('img', name='所选区间按小时API 等价费用趋势').wait_for()
                    expect(page.locator('#range-caption')).to_contain_text(today+' 至 '+today)
                    expect(page.locator('#usage-cost')).to_contain_text('缓存读取')
                    page.get_by_role('button', name='本期', exact=True).click()
                    page.get_by_label('每月结算日', exact=True).fill('31')
                    page.get_by_role('button', name='应用', exact=True).click()
                    page.get_by_role('button', name='上期', exact=True).click()
                    page.get_by_role('button', name='本月', exact=True).click()
                    page.get_by_role('button', name='近 30 天', exact=True).click()
                    page.locator('#model-rows').filter(has_text='claude-opus-5-5').wait_for()
                    page.get_by_role("tab", name="按账号", exact=True).click()
                    assert page.locator("#account-rows tr").last.locator("td").first.inner_text() == "1"
                    page.get_by_role("tab", name="按模型", exact=True).click()
                    menu.get_by_role("tab", name="账号额度", exact=True).click()
                    page.get_by_text("50% 已用", exact=True).wait_for()
                    quota_requests = []
                    def manual_quota_reply(route):
                        quota_requests.append(route.request.post_data_json)
                        route.fulfill(json={"ok": True, "name": "工作号", "cwd": str(home), "accountId": ident})
                    page.route('**/api/quota/refresh', manual_quota_reply)
                    page.get_by_role('button', name='重新读取本机额度缓存').click()
                    assert quota_requests == []
                    page.locator('[data-quota-account="'+ident+'"]').click()
                    page.locator('#quota-query-status').filter(has_text='已更新').wait_for()
                    assert quota_requests == [{"id": ident}]
                    assert not page.locator("#usage").is_visible()
                    menu.get_by_role("tab", name="Anywhere 接入", exact=True).click()
                    page.locator("#launcher-path").filter(has_text="claude-current").wait_for()
                    page.reload()
                    assert page.locator("#connection").is_visible()
                    menu.get_by_role("tab", name="账号管理", exact=True).click()
                    page.get_by_role("button", name="添加账号").click()
                    page.get_by_label("给账号起个名字").fill("个人号")
                    page.get_by_role("button", name="创建并登录").click()
                    page.get_by_role("link", name="打开官方授权页面").wait_for()
                    menu.get_by_role("tab", name="用量统计", exact=True).click()
                    page.locator("#model-rows").filter(has_text="claude-opus-5-5").wait_for()
                    assert not page.locator("#login-panel").is_visible()
                    menu.get_by_role("tab", name="账号管理", exact=True).click()
                    page.get_by_label("如果官方页面给了授权码").fill("valid-test-code")
                    page.get_by_role("button", name="提交", exact=True).click()
                    page.locator("#login-badge").filter(has_text="登录成功").wait_for(timeout=10000)
                    personal = page.locator("article.account").filter(has_text="个人号")
                    personal.get_by_role("button", name="切换", exact=True).click()
                    page.locator("#current-account").filter(has_text="个人号").wait_for()
                    # User text is escaped and cannot execute as markup.
                    page.get_by_role("button", name="添加账号").click()
                    page.get_by_label("给账号起个名字").fill('<img src=x onerror=alert(1)>')
                    page.get_by_role("button", name="取消", exact=True).click()
                    artifacts = ROOT / "artifacts"
                    artifacts.mkdir(exist_ok=True)
                    page.screenshot(path=str(artifacts / "desktop.png"), full_page=True)
                    menu.get_by_role("tab", name="用量统计", exact=True).click()
                    page.locator("#usage").screenshot(path=str(artifacts / "usage-desktop.png"))
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "desktop overflow"
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), 'usage mobile overflow'
                    page.screenshot(path=str(artifacts / "mobile.png"), full_page=True)
                    page.locator("#usage").screenshot(path=str(artifacts / "usage-mobile.png"))
                    menu.get_by_role("tab", name="账号额度", exact=True).click()
                    page.screenshot(path=str(artifacts / "quota-mobile.png"), full_page=True)
                    menu.get_by_role("tab", name="账号管理", exact=True).click()
                    page.screenshot(path=str(artifacts / "accounts-mobile.png"), full_page=True)
                    # Top-level tabs support keyboard activation and browser history.
                    menu.get_by_role("tab", name="账号管理", exact=True).focus()
                    page.keyboard.press("End")
                    assert page.locator("#connection").is_visible()
                    page.keyboard.press("Home")
                    assert page.locator("#accounts-page").is_visible()
                    page.go_back()
                    assert page.locator("#connection").is_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "mobile overflow"
                    menu.get_by_role("tab", name="账号管理", exact=True).click()
                    page.get_by_role("button", name="GPT · Codex", exact=True).click()
                    page.get_by_text("添加第一个账号，用官方登录开始。", exact=True).wait_for()
                    assert page.locator("#codex-entry").is_visible()
                    assert page.locator("#process-status").inner_text() == "下次启动生效"
                    page.screenshot(path=str(artifacts / "codex-accounts-mobile.png"), full_page=True)
                    page.get_by_role("button", name="Claude", exact=True).click()
                    personal.wait_for()
                    assert not errors, errors
                    page.get_by_role("button", name="退出登录", exact=True).click()
                    page.get_by_role("heading", name="登录面板", exact=True).wait_for()
                    assert page.locator("#panel-password").input_value() == ""
                    assert not page.locator("#accounts").count()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(artifacts / "login-mobile.png"), full_page=True)
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    page.screenshot(path=str(artifacts / "login-desktop.png"), full_page=True)
                    print(json.dumps({"browser": "Chromium", "workflow": "create → official link → code → saved → activate", "navigation": "four tabs → reload → login state retained → keyboard → browser history", "statistics": "deduped history → 30 days → USD chart → unattributed account → local quota", "desktop": "1440px", "mobile": "390px", "pageErrors": errors, "horizontalOverflow": False}))
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                manager.close()
                environment.stop()


if __name__ == "__main__":
    run()
