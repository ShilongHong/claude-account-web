# Claude Accounts Web

独立运行的本机 Claude 官方账号管理面板。使用真实 Claude Code CLI 登录；不修改 Agents Anywhere 源码，不实现第三方 OAuth 登录接口。

当前原型面向 **Linux + Python 3.10+ + Claude Code CLI**。没有 Python 或前端依赖，不需要构建。

## 启动

```bash
cd /home/shilong/code/claude-account-web
python3 app.py
```

打开 <http://127.0.0.1:18769>。直接启动默认只监听本机回环地址。可用 `--port`、`--data-dir`、`--claude` 覆盖默认设置。

界面按「账号管理」「用量统计」「账号额度」「Anywhere 接入」四个标签组织。默认打开账号页，URL 中的 `#usage`、`#quota`、`#connection` 可以直接打开对应功能，刷新和浏览器前进后退会保留页面。CLI 输出、统计口径与路径说明按需展开；原先的宣传标题和重复操作说明已移除。登录进行中可以切换标签，返回后继续操作。用量数据在打开用量或额度页时读取。

### 局域网访问

当前 systemd 服务已配置局域网访问：<http://10.81.161.212:18769>。打开后会显示网页内的登录表单，输入面板访问密码即可，无需用户名或浏览器认证弹框。访问密码保存在 `~/.local/share/claude-account-web/access-password`（权限 600）。这是本面板的密码，与 Claude 账号密码无关。本机地址也需要同一密码。登录状态通过 HttpOnly / SameSite Cookie 保存，最长 7 天；服务重启后需重新登录。右上角可退出当前浏览器登录，退出或过期后数据接口拒绝访问。

手动启用时：

```bash
python3 app.py --host 0.0.0.0 --allow-host 10.81.161.212
```

局域网模式强制要求口令，首次启动会自动生成，并在后续启动时保留。允许地址使用显式列表，IP 变化后需要调整 `--allow-host` 并重启服务。远端打开官方登录链接后，如拿到授权码，请回到面板提交给本机 CLI。网页登录适用于可信局域网；不要转发到公网。

首次启动会读取现有 `~/.claude/.credentials.json` 是否存在，并把本机账号作为只读关联项加入列表。固定入口默认指向它；这不会改变原 CLI 或 Anywhere 的启动配置。已有配置和历史不搬家、不复制凭据。本机账号的重新登录由原 Claude 终端完成。

## 添加与切换

1. 点击「添加账号」，输入备注名。服务创建独立配置目录并运行 `claude auth login --claudeai`。
2. 打开面板给出的 Claude 官方授权链接。若官方页面给出授权码，在面板提交；输入直接转交 CLI 的伪终端，不写入日志或账号记录。
3. 官方 CLI 登录结束后，面板用 `claude auth status --json` 验证 Claude 订阅身份，只保存邮箱、登录方式、套餐和检查时间等白名单字段。
4. 登录成功不会自动切换。先停止 Anywhere Claude Runtime 和其他 Claude 进程，再点击「切换」。它会重新检查登录状态，然后原子替换 `current` 目录链接。
5. 重新启动 Claude Runtime，再尝试打开原会话。

登录最多等待 10 分钟；可随时取消。服务只终止自己启动的登录进程，不会终止你的其他 Claude 会话。CLI 官方行为及页面随版本可能变化，需要时可查看界面中的实时输出。

## 接入 Agents Anywhere

Claude Runtime 的「Claude 可执行文件路径」建议设为本项目的启动器：

```text
/home/shilong/code/claude-account-web/claude-current
```

它在每次启动时把 `current` 解析成真实账号目录，再 `exec` 官方 Claude CLI。进程启动后就固定在该账号目录，避免入口后来改变时，旧进程把刷新凭据写入另一个账号。这层不会代替停止旧进程的切换检查。启动器默认从 PATH 查找 `claude`，特殊安装可用 `CLAUDE_ACCOUNT_WEB_REAL_CLAUDE` 指定真实二进制。

给 Claude Runtime 的环境变量添加：

```text
CLAUDE_CONFIG_DIR=/home/shilong/.local/share/claude-account-web/current
```

Connector 本身也应在相同环境变量下启动：Claude SDK 的历史发现可能在 Connector 进程内读取环境，而执行聊天使用子进程环境。首次接入后重启 Connector。之后切换账号必须结束旧 Claude 进程，再启动 Runtime；只改符号链接不会让存活的 Claude 进程换号。

**本原型不会自动写入 Anywhere 配置、修改其服务或调用重启接口。** 页面提供接入路径和操作步骤；部署方式不同，Connector 启动环境的设置位置也不同。

新账号的 `projects` 链接到现有 `~/.claude/projects`，如果它不存在则使用本服务的 `shared-projects`。所有账号可读取同一份原始 session JSONL 文件。恢复时还需要相同工作目录和 session ID。

**共享文件已验证，跨账号的真实模型续聊尚未验证。** 不承诺正在执行的一轮中途热切换；Anywhere 的会话映射、SDK 恢复与账号权限都可能影响结果。全局插件、MCP、skills 和其他设置没有自动共享。

## 普通终端统一使用所选账号

当前机器已配置普通 Bash 终端。新终端直接输入 `claude`，会通过本项目启动器读取面板的当前账号。已有终端先执行一次：

```bash
source ~/.bashrc
```

之后在面板换号，再次启动 `claude` 即使用新账号，不需要每次重新加载变量。已有 Claude 进程继续固定在原账号，切换保护仍然有效。

安装命令为 `python3 install_terminal.py`。它设置 `~/.bashrc`、`~/.profile` 和 `~/.config/environment.d/60-claude-account-web.conf` 中的用户环境变量 `CLAUDE_CONFIG_DIR`，并给 Bash 添加一个调用本项目启动器的 `claude` 函数。官方 `~/.local/bin/claude` 保持原样，官方更新不会覆盖该函数。systemd 用户环境也会设置同一变量，供后续启动的用户服务读取；已经运行的应用不会被改写环境。

修改前的配置保存在数据目录的 `terminal-backups/<时间戳>/`，对应清单记录文件原先是否存在。要取消接入，移除 Bash / profile 中的 `Claude Accounts terminal` 标记块及上述 environment.d 文件，并运行 `systemctl --user unset-environment CLAUDE_CONFIG_DIR`；然后在终端执行 `unset -f claude; unset CLAUDE_CONFIG_DIR` 或重开终端。

## 用量与额度统计

- 今天、近 7 天、近 30 天、本月、自选日期及每月结算周期的 token 用量和 API 等价费用，按本机日历日期统计。自选日期包含首尾两日；本期截至今天，上期显示完整周期。结算日支持 1—31 日，短月份自动取月底，并在当前浏览器保存结算日。一次最多查询 3660 天。费用显示输入、输出、缓存写入和缓存读取拆项及计价覆盖数。
- 输入、输出、缓存写入、缓存读取、缓存命中率，以及按日趋势（今天按小时）、模型汇总、账号归属和最近 40 条模型回复。
- 5 小时 / 7 天额度来自本机 `.claude.json` 的 `cachedUsageUtilization` 或桌面端 `plan-usage-history.json`。缓存须匹配账号；桌面端样本须匹配组织，多个账号共用组织时不把组织数据分配到个人。超过 30 分钟或已过重置时刻，标明过期。缺少数据不显示为 0%。
- 会话 JSONL 以消息 ID 与请求 ID 去重，选完整快照并保留原始时间，防止续聊重放重复计算。子代理记录包含在内。
- 历史账号归属移植 QB Gate 的三级索引：桌面端 `claude-code-sessions/<accountUuid>/<orgUuid>/local_*.json` 的 `cliSessionId`、工作区路径里的账号标识、旧 `bridge-session.ownerAccountUuid`。子代理与 Workflow 继承父会话；互相矛盾的证据不分配。历史按整个会话归属，不能还原当时跨账号续聊的每笔消耗，表格标注为「历史会话」。
- 启用记录后的新增回复，优先使用启动器固定的实际账号，以及官方 `SessionStart` / `Stop` / `StopFailure` / `SubagentStop` / `SessionEnd` Hook 读取的新增转写。只保存消息去重键、账号 UUID、运行标识、时间和文件偏移；不保存 Hook 输入、对话正文或凭据。旧消息重放、未完成的一行不会被重新认领。同一会话并发运行时保守地撤销相关运行的归属；没有启动基线、绕过启动器、禁用 Hook 或记录缺失时单列未归属，不再套用历史创建账号。
- 启动器只在已关联账号目录中添加统计 Hook，保留原来的其他设置和 Hook，并在 `attribution-backups/` 保存修改前配置。新启动的终端 / Anywhere Claude 会话生效；已经运行的 Claude 不会被悄悄修改或补记。`attribution.json` 保存启用时间，`account-attribution.sqlite3` 保存新增回复证据。普通统计索引可重建，归属证据库应当保留。
- 统计不使用登录 token，不发额度接口请求，不驱动自动换号。SQLite 索引只保存模型、时间、用量、归属标识等统计字段，不保存对话内容。

首次打开面板会在后台建立 `usage.sqlite3`，显示扫描进度；之后每分钟按文件变化增量读取，手动「重新扫描」也只读本机。截断、替换或删除的记录会更新索引；正在写入但未完成的一行留待下次读取。没有有效时间的记录不计入日期区间。

价格快照核对日期为 **2026-11-08**，来源为 [Anthropic 官方 API 定价](https://platform.claude.com/docs/en/about-claude/pricing)。费用仅表示对应 token 按 API 价格折算的参考金额，不是 Pro / Max 订阅账单，不含服务端工具等额外费用。未知模型或未支持的计价条件会单列未计价，不使用猜测价格。支持两档缓存写入、已知 fast 档和 US 推理倍率；旧模型的未覆盖长上下文档位也不猜价。价目表在 `usage_stats.py` 中显式维护，不会联网自动抓价。

### 参考项目的切换原理

本次查看 [QB Gate](https://github.com/smithtaylor7748-ops/qb-gate) 的版本为 `8669c1451d41015b8e6da190a2ad49f3d5d3bc0d`。其 Claude 账号模块采用独立目录和 Windows junction；切换前关闭 Claude，切换多个目录指向时有事务恢复；启动 Claude Code 时把 `CLAUDE_CONFIG_DIR` 指向具体账号目录，防止运行中的进程跟随可变链接。额度显示读本机缓存，token 统计读会话 JSONL 并去重。相关源码见 [账号目录](https://github.com/smithtaylor7748-ops/qb-gate/blob/8669c1451d41015b8e6da190a2ad49f3d5d3bc0d/crates/qb-accounts/src/accounts/mod.rs)、[启动环境](https://github.com/smithtaylor7748-ops/qb-gate/blob/8669c1451d41015b8e6da190a2ad49f3d5d3bc0d/crates/qb-launch/src/launch.rs)、[额度来源](https://github.com/smithtaylor7748-ops/qb-gate/blob/8669c1451d41015b8e6da190a2ad49f3d5d3bc0d/crates/qb-accounts/src/accounts/usage.rs)、[转写统计](https://github.com/smithtaylor7748-ops/qb-gate/blob/8669c1451d41015b8e6da190a2ad49f3d5d3bc0d/crates/qb-accounts/src/accounts/tokens.rs)。

Codex 统计按上述版本的 `codex/usage.rs` 与 `codex/ratelimit.rs` 移植为 Python，加入本服务的增量 SQLite 索引。读取 `CODEX_HOME`（未设置时为 `~/.codex`）下的 `sessions` 和 `archived_sessions`，按会话累计差分、跨归档去重；缓存输入从普通输入中扣除，推理 token 不重复加入输出。不会累加 `last_token_usage`，新版 `token_usage_record` 不再重复统计。累计计数回退或缺少会话标识时记为覆盖不完整。界面可选「全部 / Claude / Codex」，日期、模型、趋势和额度同步筛选；Codex 历史单列本机记录，账号未确认。

Codex 额度沿用日志内最新非空 `rate_limits`，按实际 `window_minutes` 命名，标明过期与重置状态。缺失数据不算作 0%。Codex 价格已于 **2026-11-08** 按 [OpenAI 官方定价](https://developers.openai.com/api/docs/pricing) 核对，新增 GPT-6.1 Terra、GPT-5.6 Terra / Luna，收录 `gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-luna`、`gpt-6-sol`、`gpt-5.6-sol`。按 Standard API 等价价格折算，输入超过 272,000 token 时对整次请求使用长上下文价格；缓存读取、缓存写入从普通输入中扣除并各自计价。金额不是订阅账单，不推算实际服务档位或地域附加费。未知模型（如未公开定价的 `codex-auto-review`）、缓存写入字段缺失、累计差分跨越多个请求且无法区分档位时不估价。历史用量统一使用本次核对的价格快照折算，并非还原历史账单。统计只读本机日志，历史用量暂不按 Codex 账号拆分。

## 来源与许可

基于 QB Gate，版权所有 (C) 2026 smithtaylor7748-ops。
源码：https://github.com/smithtaylor7748-ops/qb-gate

Claude Accounts Web 是独立修改版，非 QB Gate 原作者发布、认可或支持。合并应用按 AGPL-3.0-only 提供，原项目材料保留其附加条款；见 [LICENSE](LICENSE)、[LICENSE-ADDITIONAL-TERMS.md](LICENSE-ADDITIONAL-TERMS.md) 与 [NOTICE.md](NOTICE.md)。界面底部「关于与源码」保留署名并提供本版本完整源码下载，不含账号数据、凭据、会话或统计数据库。

## 数据与更新

默认状态保存在 `~/.local/share/claude-account-web/`：

```text
accounts.json              账号备注和白名单状态，不含 OAuth token
profiles/<随机 ID>/         新增账号的独立 Claude 配置，凭据由 CLI 保存
current -> 账号配置目录      Anywhere 使用的固定入口
shared-projects/            没有现有历史目录时使用
manager.lock               防止同一数据目录被重复启动
usage.sqlite3              不含对话正文的增量统计索引（可停止服务后删除重建）
attribution.json           后续逐条归属的启用时间
account-attribution.sqlite3 运行账号与新增消息的证据（应保留，不能从历史重建）
attribution-backups/       添加 Hook 前的账号 settings.json 备份
```

账号目录权限为 700，记录文件为 600。OAuth 链接和实时登录输出只保存在服务内存中。局域网模式使用网页密码登录，账号信息、会话状态、统计、源码下载和面板脚本均需有效登录会话；仅登录页及其样式、图标和登录脚本公开。登录接口和其他 API 使用 CSRF token、Origin 与 Host 检查。错误密码有短时频率限制；不加载外部脚本或字体，密码不写入浏览器存储或日志。

更新时停止服务、替换应用代码再启动，保留数据目录。主项目无补丁，因此更新 Anywhere 无需合并本服务代码；但仍需要验证新版 CLI / SDK / Connector 的兼容性。

## 后台运行

仓库带有适用于当前安装路径的 systemd 用户服务示例：

```bash
mkdir -p ~/.config/systemd/user
cp claude-account-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start claude-account-web
systemctl --user status claude-account-web
```

按需运行 `systemctl --user enable claude-account-web` 设置登录后启动；默认不启用。停止用 `systemctl --user stop claude-account-web`。如更换源码位置或 CLI 路径，请调整服务文件。

## 检查

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
# 可选：已有 Playwright 和 Chromium 时运行
python3 tests/browser_smoke.py
```

自动测试使用临时目录和模拟 CLI，覆盖真实 PTY 输入输出、共享历史、切换保护、现有文件保留、访问检查和凭据不泄漏。浏览器测试同样使用模拟 CLI，不会登录或切换你的真实账号。


### GPT · Codex 账号

账号管理中的「GPT · Codex」沿用 QB Gate 的独立目录方案：每个账号有自己的 CODEX_HOME、auth.json 和配置。添加后由官方 codex login --device-auth 完成授权，面板不自行刷新令牌。首次自动关联本机 ~/.codex，保持原凭据原位。

切换改变面板的 codex/current 入口，codex-current 启动时解析成固定目录；已经运行的 Codex 不会换号。终端可用本目录的 codex-current 启动。新增账号仅共享 sessions/archived_sessions 转写目录，其他设置和状态数据库独立；跨账号恢复和桌面 App 展示需另行验证。此功能不控制 ChatGPT 网页登录，也不自动接管 Codex 桌面 App。


### 后台启动与手动额度查询

执行 systemctl --user enable --now claude-account-web.service 后，面板随用户服务启动；本机已启用 linger，可在未登录桌面时运行。

账号额度页每个 Claude 账号旁的「刷新」仅在手动点击时调用该账号的官方 CLI /usage，不切换当前账号、不查询其他账号；顶部「读取缓存」不调用 CLI。工作目录固定为用户主目录（~），账号配置目录固定为所查询账号，不恢复已有会话。查询启用 safe-mode 并禁用工具，最多等待 45 秒，仅在官方写入新的匹配账号缓存后显示成功。按用户授权，首次遇到官方目录信任提示时，仅在提示路径完整匹配主目录（~）且本次使用 safe-mode、禁用工具的情况下自动确认一次；首次启动的已知外观选择和安全说明也会自动完成；不自动信任其他项目，登录仍须本人完成。并发查询被拒绝，每次查询至少间隔 10 秒。打开页面、定时读取统计和面板自启动均不会触发 /usage。Codex 仍只重读本机快照。

2026-10-26 在本机 CLI 2.1.308 复现并修复：`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 会拦截 CLI 自己的额度请求，导致 `/status` 正常但 `/usage` 显示 `Failed to load usage data`。额度查询子进程会移除该变量（包括从服务继承的值），改为单独关闭遥测、错误上报和自动更新。仅调整本次查询的子进程环境。
