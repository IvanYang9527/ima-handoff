# ima-handoff（DSH 跨设备记忆与任务交接 Skill）

在多台电脑的 DeepSeek Harness (DSH) 之间通过腾讯 ima 云端笔记无缝衔接开发进度。
所有同步内容汇总在同一篇交接笔记里，通过每条记录上的**结构化元数据标签**
（时间 / 项目 / 会话ID / 设备）区分不同对话，实现“一篇文档、多会话隔离”。

## 0. 安装 / 更新

仓库：https://github.com/IvanYang9527/ima-handoff

```bash
# 首次安装：克隆到 DSH 技能目录
git clone https://github.com/IvanYang9527/ima-handoff.git ~/.dsh/skills/ima-handoff

# 后续更新
cd ~/.dsh/skills/ima-handoff && git pull
```

> 本仓库是**唯一源**。日常改动请在这里提交并推送，再 `git pull` 到各机器的
> `~/.dsh/skills/ima-handoff/`，避免多台机器各自修改导致分叉。

## 1. 核心能力

- **两层结构**：`《笔记本》/《笔记》`。笔记本是分类容器，笔记存交接记录；两者名称都可自定义。
- **官方端点**：`https://ima.qq.com/openapi/note/v1/` 下的 `search_note`、`list_notebook`、
  `list_note`、`add_notebook`、`get_doc_content`、`append_doc`、`import_doc`。
- **官方鉴权头**：`ima-openapi-clientid` 与 `ima-openapi-apikey`。
- **结构化元数据标签**：每条写入生成一个独立分隔块，形如：
  ```markdown
  ---
  ### [2026-11-19 01:15:00] | 项目：my-app | 会话ID：sess-a1b2c3 | 设备：MacBook-Pro
  **当前进度与修改**：...
  **下一步待办**：...
  ```
- **会话ID 生成/传递**：`--session` 显式指定优先；否则按 `DSH_SESSION_ID` / `TERM_SESSION_ID`
  环境变量 → 本机缓存 → 随机短码 `sess-xxxxxxxx` 兜底。自动生成的 ID 会落盘复用，
  保证同一工作区多次 write 归入同一会话，不会碎片化。
- **按会话精准读取**：`read --session <ID>` 只返回该会话最新一条完整记录；不带参数则返回
  最近 3 个不同会话的摘要索引（`--all` 全部，`--json` 结构化）。
- **并发安全**：多窗口各带自己的会话ID 往同一篇笔记追加，时间线独立、互不覆盖。
- **降级保护**：网络异常 / 配额耗尽时自动暂存至 `~/.dsh/handoff_fallback.md`。

## 2. 安装部署

将 `ima-handoff` 文件夹完整放置于全局 DSH 技能目录：`~/.dsh/skills/ima-handoff/`。

## 3. 初始化

**推荐在 DSH 对话里完成**：对 DSH 说“初始化 ima 交接”，它会用选项询问笔记本名 / 凭证 / 是否验证，
然后自动执行。等价命令：

```bash
python3 ~/.dsh/skills/ima-handoff/ima_sync.py init \
  --notebook "DSH内容同步空间" --note-title "DSH开发任务交接" --keep-credentials
```

参数说明：
- `--notebook` 笔记本名（同步空间，默认《DSH内容同步空间》）
- `--note-title` 交接笔记名（默认《DSH开发任务交接》）
- `--keep-credentials` 沿用现有凭证；或用 `--client-id` / `--api-key` 写入新的
- `--no-verify` 跳过验证；`--no-create` 不创建笔记本（**笔记本建了删不掉**）

只做只读验证：`python3 ima_sync.py init --check`

## 4. 手动配置凭证（可选）

若不用 init，也可在每台电脑的终端配置文件中配置：

```bash
export IMA_OPENAPI_CLIENTID="你的Client_ID"
export IMA_OPENAPI_APIKEY="你的API_Key"
```
（脚本同时兼容读取 `~/.config/ima/client_id` 与 `~/.config/ima/api_key` 文件。）

## 5. 使用示例

```bash
# 写入（新任务建议显式给一个语义会话名，便于跨电脑接续）
python3 ~/.dsh/skills/ima-handoff/ima_sync.py write \
  --project "my-app" \
  --content "**当前进度与修改**：... **下一步待办**：..." \
  --session "feat-login-01"

# 不传 --session：脚本自动兜底并打印本次 ID
python3 ~/.dsh/skills/ima-handoff/ima_sync.py write --project "my-app" --content "连通性验证"

# 读取：先看索引，再按会话拉取最新完整记录
python3 ~/.dsh/skills/ima-handoff/ima_sync.py read
python3 ~/.dsh/skills/ima-handoff/ima_sync.py read --session "feat-login-01"
python3 ~/.dsh/skills/ima-handoff/ima_sync.py read --all
python3 ~/.dsh/skills/ima-handoff/ima_sync.py read --json
```

DSH 内触发：
- 切换电脑前：**“保存进度”** 或 **“准备换电脑”**
- 在新电脑打开 DSH：**“读取交接”** 或 **“继续上次任务”**

> 跨电脑接续同一会话：随机 ID 只在单机缓存，两台电脑要接续必须手工约定同一个
> 语义会话名（如 `feat-login-01`），两端 write 都带 `--session <名字>`；另一端
> `read --session <名字>` 即可精准拉取。
