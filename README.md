# ima-handoff（DSH 跨设备记忆与任务交接 Skill）

在多台电脑的 DeepSeek Harness (DSH) 之间通过腾讯 ima 云端笔记无缝衔接开发进度。
所有同步内容汇总在同一篇《DSH开发任务交接》总文档，通过每条记录上的**结构化元数据标签**
（时间 / 项目 / 会话ID / 设备）区分不同对话，实现“一篇文档、多会话隔离”。

## 1. 核心能力

- **官方端点**：`https://ima.qq.com/openapi/note/v1/` 下的 `search_note`、`get_doc_content`、`append_doc`、`import_doc`。
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
- **并发安全**：多窗口各带自己的会话ID 往同一篇总笔记追加，时间线独立、互不覆盖。
- **降级保护**：网络异常 / 配额耗尽时自动暂存至 `~/.dsh/handoff_fallback.md`。

## 2. 安装部署

将 `ima-handoff` 文件夹完整放置于全局 DSH 技能目录：`~/.dsh/skills/ima-handoff/`。

## 3. 初始化（推荐，新电脑首次使用）

放置好目录后，在**本地终端**运行一次交互向导：

```bash
python3 ~/.dsh/skills/ima-handoff/ima_sync.py init
```

向导依次完成：
1. **命名交接笔记本（同步空间）**：输入自定义名称，回车沿用默认《DSH开发任务交接》；
   名称保存到 `~/.dsh/handoff_config.json`，之后 read/write 默认用它。
2. **绑定凭证**：粘贴 `Client_ID` / `API_Key`，选择写入 `~/.config/ima/`（推荐）
   或按提示自行 `export`。
3. **连通性验证**：向该笔记本追加一条测试记录，确认云端可用。

非交互环境（无 TTY）下向导只打印指引、不阻塞输入；此时请在本地终端手动运行一次。
跳过交互直接验证可用：`python3 ima_sync.py init --check`。

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
