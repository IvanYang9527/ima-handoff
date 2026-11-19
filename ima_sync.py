import argparse
import getpass
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Dict, List, Optional

BASE_URL = os.getenv("IMA_BASE_URL", "https://ima.qq.com").rstrip("/")
DEFAULT_NOTEBOOK = "DSH内容同步空间"
DEFAULT_TITLE = "DSH开发任务交接"
FALLBACK_FILE = Path.home() / ".dsh" / "handoff_fallback.md"
SESSION_CACHE_DIR = Path.home() / ".dsh" / "handoff_sessions"
CONFIG_FILE = Path.home() / ".dsh" / "handoff_config.json"
CRED_DIR = Path.home() / ".config" / "ima"
CID_FILE = CRED_DIR / "client_id"
KEY_FILE = CRED_DIR / "api_key"

SEP = "---"
# get_doc_content(format=1) 会把 --- 转成 ***，并转义 [ ] _ * 等字符
SEP_ALIASES = ("---", "***", "___")
HEADER_RE = re.compile(r"^###\s*\[(?P<ts>[^\]]+)\]\s*(?P<meta>.*)$")
META_SPLIT_RE = re.compile(r"\||｜")
MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


# ------------------------------ 基础工具 ------------------------------

def _inline(text, maxlen: int = 64) -> str:
    """把任意文本压成单行、去掉会破坏表头解析的竖线。"""
    t = re.sub(r"[\r\n\t]+", " ", str(text))
    t = re.sub(r"[|｜]", "／", t).strip()
    return t[:maxlen]


def preview(body: str, n: int = 80) -> str:
    flat = re.sub(r"\s+", " ", body or "").strip()
    return flat[:n] + ("…" if len(flat) > n else "")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:  # 凭证文件只给当前用户读写权限
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def default_title() -> str:
    return load_config().get("note_title") or DEFAULT_TITLE


def default_notebook() -> str:
    return load_config().get("notebook") or DEFAULT_NOTEBOOK


def load_credentials():
    client_id = (
        os.getenv("IMA_OPENAPI_CLIENTID", "").strip()
        or os.getenv("IMA_CLIENT_ID", "").strip()
    )
    api_key = (
        os.getenv("IMA_OPENAPI_APIKEY", "").strip()
        or os.getenv("IMA_API_KEY", "").strip()
    )
    if not client_id and CID_FILE.exists():
        client_id = CID_FILE.read_text(encoding="utf-8").strip()
    if not api_key and KEY_FILE.exists():
        api_key = KEY_FILE.read_text(encoding="utf-8").strip()
    return client_id, api_key


def sanitize_markdown(text: str) -> str:
    # 过滤本地图片引用，符合 ima OpenAPI 规范
    cleaned = re.sub(r"!\[[^\]]*\]\((?:file://|/|[A-Za-z]:\\)[^)]+\)", "", text)
    return cleaned.encode("utf-8", errors="ignore").decode("utf-8")


def _workspace_key() -> str:
    """按当前工作目录生成短哈希，用于隔离不同仓库的自动会话 ID 缓存。"""
    cwd = str(Path.cwd())
    return sha1(cwd.lower().replace("\\", "/").encode("utf-8")).hexdigest()[:12]


def _session_cache_path() -> Path:
    return SESSION_CACHE_DIR / f"session-{_workspace_key()}.txt"


def _save_cache(sid: str) -> None:
    try:
        p = _session_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sid, encoding="utf-8")
    except Exception:
        pass  # 缓存失败不影响主流程


def resolve_session(explicit: Optional[str]) -> tuple:
    """返回 (session_id, 来源)。优先级：--session > 环境变量 > 本地缓存 > 新建随机 ID。

    关键点：自动生成的 ID 必须落盘复用，否则同一工作会话的多次 write 会被
    拆成多个不同会话，失去按会话过滤的意义。跨电脑要接续同一会话时，必须
    由 DSH 在两台机器上显式传同一个 --session（语义 ID）。
    """
    if explicit and explicit.strip():
        sid = _inline(explicit, 64)
        _save_cache(sid)
        return sid, "--session"
    for var in ("DSH_SESSION_ID", "TERM_SESSION_ID"):
        val = os.getenv(var, "").strip()
        if val:
            sid = _inline(val, 64)
            _save_cache(sid)
            return sid, var
    cached = _session_cache_path()
    if cached.exists():
        sid = cached.read_text(encoding="utf-8", errors="ignore").strip()
        if sid:
            return _inline(sid, 64), f"cache:{cached.name}"
    sid = "sess-" + uuid.uuid4().hex[:8]
    _save_cache(sid)
    return sid, "auto"


def local_hostname(override: Optional[str] = None) -> str:
    return _inline(override or socket.gethostname(), 48)


# ------------------------------ 云端请求 ------------------------------

def request_ima(path: str, payload: dict) -> dict:
    client_id, api_key = load_credentials()
    if not client_id or not api_key:
        print(
            "错误：未检测到 IMA_OPENAPI_CLIENTID 或 IMA_OPENAPI_APIKEY 环境变量（或 ~/.config/ima/ 凭证文件）。",
            file=sys.stderr,
        )
        sys.exit(1)

    url = f"{BASE_URL}/{path.lstrip('/')}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "DSH-Skill-Sync/3.0",
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            res = json.loads(body) if body else {}
            code = res.get("code", 0)
            if str(code) != "0":
                msg = res.get("msg", "未知业务错误")
                raise RuntimeError(f"业务错误码 {code}: {msg}")
            return res
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {err_msg}")
    except Exception as e:
        raise RuntimeError(str(e))


def extract_notes_list(data: dict) -> list:
    """兼容旧调用方的兜底；官方实际字段是 search_note_infos。"""
    if not isinstance(data, dict):
        return []
    for key in ("search_note_infos", "notes", "list", "items", "docs", "note_list"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def find_notebook_id(name: str) -> str:
    """按名称精确查找笔记本（folder）ID，逐页扫描。找不到返回空串。"""
    target = (name or "").strip()
    if not target:
        return ""
    cursor = "0"
    for _ in range(20):  # 防御性上限
        res = request_ima(
            "/openapi/note/v1/list_notebook", {"cursor": cursor, "limit": 20}
        )
        data = res.get("data", {}) or {}
        for f in data.get("note_folder_infos") or []:
            if (f.get("name") or "").strip() == target:
                return f.get("folder_id") or ""
        if data.get("is_end"):
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break
    return ""


def create_notebook(name: str) -> str:
    """创建笔记本，返回 folder_id。名称重复时（210030）改为查找已有。"""
    try:
        res = request_ima(
            "/openapi/note/v1/add_notebook", {"folder_name": name.strip()}
        )
        data = res.get("data", {}) or {}
        fid = data.get("folder_id") or ""
        if fid:
            return fid
    except RuntimeError as e:
        # 210030 = 笔记本名称重复，退回查找
        if "210030" not in str(e):
            raise
    return find_notebook_id(name)


def ensure_notebook(name: str) -> str:
    """确保笔记本存在，返回 folder_id（不存在则创建）。"""
    fid = find_notebook_id(name)
    if fid:
        return fid
    return create_notebook(name)


def find_note_in_notebook(title: str, folder_id: str) -> str:
    """在指定笔记本内按标题精确查找 note_id。folder_id 为空则退回全局搜索。"""
    if not folder_id:
        return find_note_id(title)
    target = title.strip()
    cursor = ""
    for _ in range(20):
        res = request_ima(
            "/openapi/note/v1/list_note",
            {"folder_id": folder_id, "sort_type": 0, "cursor": cursor, "limit": 20},
        )
        data = res.get("data", {}) or {}
        for nb in data.get("note_book_list") or []:
            if (nb.get("title") or "").strip() == target:
                return nb.get("note_id") or ""
        if data.get("is_end"):
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break
    return ""


def find_note_id(title: str) -> str:
    """按标题精确查找 note_id（全局搜索）。

    官方 search_note 参数为 query_info.title（不是 keyword），结果是模糊匹配，
    因此必须自己按标题精确比对，否则会把内容追加到无关笔记上。
    逐页扫描直到 is_end 或达到上限；找不到精确匹配就返回空串，
    让调用方走 import_doc 新建，绝不退化为"取第一条结果"。
    """
    target = title.strip()
    start = 0
    page = 20
    max_scan = 200  # 防御性上限，避免异常 is_end 导致死循环
    while start < max_scan:
        res = request_ima(
            "/openapi/note/v1/search_note",
            {
                "query_info": {"title": target},
                "search_type": 0,
                "start": start,
                "end": start + page,
            },
        )
        data = res.get("data", {}) or {}
        infos = data.get("search_note_infos") or []
        if not infos:
            break
        for item in infos:
            book = item.get("note_book_info") or item
            item_title = (book.get("title") or "").strip()
            if item_title == target:
                return book.get("note_id") or ""
        if data.get("is_end"):
            break
        start += page
    return ""


# ------------------------------ 解析 ------------------------------

def parse_blocks(text: str) -> List[Dict]:
    """把笔记正文解析成结构化块列表（按文档追加顺序，末尾即最新）。

    兼容旧版只有 "项目：" 的标题；未知字段忽略，不会因格式变化崩溃。
    get_doc_content 的 Markdown 输出会转义 \\[ \\_ 等字符，先把转义还原，
    否则标题里的 [时间] 永远匹配不上。
    """
    if not isinstance(text, str):
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = MD_ESCAPE_RE.sub(r"\1", text)
    blocks: List[Dict] = []
    cur: Optional[Dict] = None

    def finish():
        if cur is None:
            return
        body_lines = cur.pop("_body")
        # 循环剥离：尾部可能是 "***" 也可能是空行，且二者交替出现
        changed = True
        while changed and body_lines:
            changed = False
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()
                changed = True
            if body_lines and body_lines[-1].strip() in SEP_ALIASES:
                body_lines.pop()
                changed = True
        cur["body"] = "\n".join(body_lines).strip()
        blocks.append(cur)

    for line in text.split("\n"):
        m = HEADER_RE.match(line)
        if m:
            finish()
            meta = {}
            for part in META_SPLIT_RE.split(m.group("meta")):
                if ":" in part:
                    k, v = part.split(":", 1)
                elif "：" in part:
                    k, v = part.split("：", 1)
                else:
                    continue
                meta[k.strip()] = v.strip()
            cur = {
                "ts": m.group("ts").strip(),
                "project": meta.get("项目") or meta.get("project") or "",
                "session": (
                    meta.get("会话ID") or meta.get("会话") or meta.get("会话 ID")
                    or meta.get("session") or meta.get("session_id") or ""
                ),
                "host": meta.get("设备") or meta.get("主机") or meta.get("host") or "",
                "header": line,
                "_body": [],
            }
        elif cur is not None:
            cur["_body"].append(line)
    finish()
    return blocks


def _extract_content(data) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for k in ("content", "doc_content", "text", "markdown", "data"):
            v = data.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                nested = _extract_content(v)
                if nested:
                    return nested
        return json.dumps(data, ensure_ascii=False)
    if isinstance(data, list):
        return "\n".join(_extract_content(x) for x in data)
    return str(data)


def render_block(b: Dict) -> str:
    meta = (
        f"时间：{b['ts']} | 项目：{b['project'] or '—'} "
        f"| 会话ID：{b['session'] or '(未标注)'} | 设备：{b['host'] or '—'}"
    )
    return f"{meta}\n{SEP}\n{b['body']}\n"


def index_text(blocks: List[Dict]) -> str:
    if not blocks:
        return "笔记中暂无可解析的交接记录。"
    seen, picked = set(), []
    for b in reversed(blocks):  # 末尾追加顺序 => 倒序即由新到旧
        key = b["session"] or "(未标注会话)"
        if key in seen:
            continue
        seen.add(key)
        picked.append(b)
        if len(picked) >= 3:
            break
    lines = [f"共 {len(blocks)} 条记录，最近 {len(picked)} 个会话："]
    for i, b in enumerate(picked, 1):
        lines.append(
            f"{i}. [{b['ts']}] 会话ID={b['session'] or '(未标注)'} "
            f"| 项目={b['project'] or '—'} | 设备={b['host'] or '—'}"
        )
        lines.append(f"   预览：{preview(b['body'])}")
    lines.append(
        "提示：read --session <ID> 读取该会话最新完整记录；"
        "read --all 查看全部；read --json 输出结构化数据。"
    )
    return "\n".join(lines)


def print_fallback() -> None:
    if FALLBACK_FILE.exists():
        print(
            f"检测到本地降级备份文件：{FALLBACK_FILE}\n{FALLBACK_FILE.read_text(encoding='utf-8')}"
        )


# ------------------------------ 命令处理 ------------------------------

def resolve_note(title: str, notebook: Optional[str] = None,
                 create_notebook_if_missing: bool = False) -> tuple:
    """解析出 (note_id, folder_id)。

    两层结构：笔记本(notebook) 下放一篇交接笔记(title)。
    - 给了 notebook：先定位/创建笔记本，再在其中按标题找笔记
    - 没给 notebook：退回全局按标题搜索（兼容旧用法）
    """
    folder_id = ""
    nb = (notebook or "").strip()
    if nb:
        folder_id = (ensure_notebook(nb) if create_notebook_if_missing
                     else find_notebook_id(nb))
        if folder_id:
            note_id = find_note_in_notebook(title, folder_id)
            if note_id:
                return note_id, folder_id
    return find_note_id(title), folder_id


def handle_read(title: str, session: Optional[str], show_all: bool, as_json: bool,
                notebook: Optional[str] = None):
    try:
        note_id, folder_id = resolve_note(title, notebook)
        if not note_id:
            where = f"笔记本《{notebook}》中" if notebook else "云端"
            print(f"{where}暂未找到标题为《{title}》的交接笔记。")
            print_fallback()
            return
        res = request_ima(
            "/openapi/note/v1/get_doc_content",
            {"note_id": note_id, "target_content_format": 1},
        )
        data = res.get("data", res)
        blocks = parse_blocks(_extract_content(data))

        if as_json:
            print(json.dumps(
                {"title": title, "note_id": note_id, "count": len(blocks), "blocks": blocks},
                ensure_ascii=False, indent=2,
            ))
            return
        if session:
            hits = [b for b in blocks if b["session"] == session]
            if not hits:
                print(f"未找到会话ID 为「{session}」的记录。\n")
                print(index_text(blocks))
                return
            latest = hits[-1]  # 文档追加顺序 => 最后一条即最新
            print(f"=== 会话「{session}」最新交接记录（该会话共 {len(hits)} 条）===")
            print(render_block(latest))
            return
        if show_all:
            print(f"=== 《{title}》全部 {len(blocks)} 条记录 ===")
            for i, b in enumerate(blocks, 1):
                print(f"\n[{i}] {render_block(b)}")
            return
        print(index_text(blocks))
    except Exception as e:
        print(f"读取云端交接笔记失败: {e}", file=sys.stderr)
        print_fallback()
        sys.exit(1)


def save_fallback(project: str, session_id: str, host: str, content: str, reason: str):
    FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = (
        f"\n{SEP}\n### [{ts}] | 项目：{_inline(project)} "
        f"| 会话ID：{_inline(session_id)} | 设备：{_inline(host)} "
        f"| 降级原因：{_inline(reason, 80)}\n{content}\n"
    )
    with open(FALLBACK_FILE, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"已触发本地降级备份，交接内容已暂存至: {FALLBACK_FILE}", file=sys.stderr)


def handle_write(title: str, project: str, content: str,
                 session: Optional[str], host: Optional[str],
                 notebook: Optional[str] = None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid, source = resolve_session(session)
    hostname = local_hostname(host)
    clean_content = sanitize_markdown(content)
    header = (
        f"### [{ts}] | 项目：{_inline(project)} "
        f"| 会话ID：{_inline(sid)} | 设备：{_inline(hostname)}"
    )
    formatted_block = f"\n{SEP}\n{header}\n\n{clean_content}\n"

    print(f"本次会话ID：{sid}（来源：{source}）；设备：{hostname}")
    try:
        note_id, folder_id = resolve_note(
            title, notebook, create_notebook_if_missing=True
        )
        if note_id:
            # 官方标准：append_doc 传入 note_id, content, content_format=1 (Markdown)
            res = request_ima(
                "/openapi/note/v1/append_doc",
                {"note_id": note_id, "content": formatted_block, "content_format": 1},
            )
            loc = f"笔记本《{notebook}》" if notebook else "云端"
            print(
                f"已追加同步至{loc}笔记《{title}》(note_id={note_id})："
                f"{json.dumps(res.get('data', {}), ensure_ascii=False)}"
            )
        else:
            # 官方标准：import_doc 无 title 字段，标题取正文首行 "# 标题"
            full_md = f"# {title}\n{formatted_block}"
            payload = {"content": full_md, "content_format": 1}
            if folder_id:
                payload["folder_id"] = folder_id  # 直接建到指定笔记本
            res = request_ima("/openapi/note/v1/import_doc", payload)
            note_id = (res.get("data") or {}).get("note_id") or ""
            loc = f"笔记本《{notebook}》" if notebook else "云端"
            print(
                f"已新建并同步至{loc}笔记《{title}》(note_id={note_id})："
                f"{json.dumps(res.get('data', {}), ensure_ascii=False)}"
            )
        print(f"写入完成。本次会话后续继续带 --session {sid} 即可归入同一会话。")
    except Exception as e:
        err_str = str(e)
        print(f"云端同步失败: {err_str}", file=sys.stderr)
        save_fallback(project, sid, hostname, clean_content, err_str)
        sys.exit(1)


# ------------------------------ 初始化向导 ------------------------------

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """交互式读取一行输入；回车则用默认值。secret=True 时不回显。

    只有在真正的终端下才用 getpass；stdin 被重定向时（脚本/CI/DSH 非交互调用）
    改为读一行，避免 getpass 在 Windows 上阻塞挂死。
    """
    hint = f"（回车保留：{default}）" if default and not secret else (
        "（直接回车保留现有值）" if default else ""
    )
    sys.stdout.write(f"{label}{hint}: ")
    sys.stdout.flush()
    try:
        use_getpass = secret and sys.stdin.isatty() and sys.stdout.isatty()
        if use_getpass:
            val = getpass.getpass("")
        else:
            val = sys.stdin.readline()
            if not val:
                return default
            val = val.rstrip("\n")
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val.strip() or default


def _save_credentials(client_id: str, api_key: str) -> bool:
    """把凭证写入 ~/.config/ima/；成功返回 True。"""
    try:
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        CID_FILE.write_text(client_id, encoding="utf-8")
        KEY_FILE.write_text(api_key, encoding="utf-8")
        for p in (CID_FILE, KEY_FILE):
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
        print(f"✓ 凭证已保存到 {CID_FILE} 与 {KEY_FILE}")
        return True
    except Exception as e:
        print(f"✗ 凭证保存失败：{e}", file=sys.stderr)
        return False


def _verify_readonly(title: str, notebook: Optional[str] = None) -> bool:
    """只读验证：不写入任何内容。返回是否成功。"""
    cid, key = load_credentials()
    if not cid or not key:
        print("✗ 凭证缺失，无法验证。")
        return False
    try:
        folder_id = find_notebook_id(notebook) if notebook else ""
        note_id = find_note_in_notebook(title, folder_id) if folder_id else find_note_id(title)
        nb_desc = f"笔记本《{notebook}》" if notebook else "云端"
        if folder_id:
            print(f"✓ 笔记本《{notebook}》已存在 (folder_id={folder_id})")
        elif notebook:
            print(f"· 笔记本《{notebook}》尚未创建，首次 write 时会自动新建")
        if note_id:
            res = request_ima(
                "/openapi/note/v1/get_doc_content",
                {"note_id": note_id, "target_content_format": 1},
            )
            blocks = parse_blocks(_extract_content(res.get("data", res)))
            print(f"✓ 云端连通正常：{nb_desc}笔记《{title}》已存在"
                  f"(note_id={note_id})，现有 {len(blocks)} 条交接记录。")
        else:
            print("✓ 云端连通正常：凭证有效、检索可用。")
            print(f"  {nb_desc}笔记《{title}》尚未创建，首次执行 write 时会自动新建。")
        return True
    except Exception as e:
        print(f"✗ 云端连通失败：{e}", file=sys.stderr)
        print("  请检查凭证是否正确、网络是否可达。")
        return False


def handle_init_apply(notebook=None, note_title=None, client_id=None, api_key=None,
                      keep_credentials=False, verify=True, create=True) -> int:
    """非交互式初始化：由 DSH 在对话中收集答案后直接调用。

    两层结构：notebook（笔记本）+ note_title（交接笔记）。
    只做：保存名称 → 按需建笔记本 → 按需保存凭证 → 只读验证。
    """
    print("=" * 56)
    print(" ima-handoff 初始化")
    print("=" * 56)

    cfg = load_config()
    final_nb = (notebook or "").strip() or cfg.get("notebook") or DEFAULT_NOTEBOOK
    final_title = ((note_title or "").strip()
                   or cfg.get("note_title") or DEFAULT_TITLE)
    cfg["notebook"] = final_nb
    cfg["note_title"] = final_title
    save_config(cfg)
    print(f"\n✓ 交接笔记本：《{final_nb}》")
    print(f"✓ 交接笔记  ：《{final_title}》")
    print(f"  已保存到 {CONFIG_FILE}")

    if client_id or api_key:
        cid, key = load_credentials()
        cid = (client_id or "").strip() or cid
        key = (api_key or "").strip() or key
        if not cid or not key:
            print("✗ Client ID 与 API Key 需要同时提供。", file=sys.stderr)
            return 1
        if not _save_credentials(cid, key):
            return 1
    elif keep_credentials:
        cid, key = load_credentials()
        if cid and key:
            print("✓ 沿用现有凭证（未做修改）")
        else:
            print("⚠ 未检测到现有凭证，请补充 Client ID / API Key。", file=sys.stderr)
            return 1

    # 按需创建笔记本（创建后不可删，因此默认只在明确要求时建）
    if create and (client_id or api_key or keep_credentials):
        try:
            fid = find_notebook_id(final_nb)
            if fid:
                print(f"✓ 笔记本已存在 (folder_id={fid})")
            else:
                fid = create_notebook(final_nb)
                print(f"✓ 已创建笔记本《{final_nb}》(folder_id={fid})")
        except Exception as e:
            print(f"✗ 笔记本创建/查询失败：{e}", file=sys.stderr)

    ok = True
    if verify:
        print("\n【连通性验证】")
        ok = _verify_readonly(final_title, final_nb)
    else:
        print("\n已跳过连通性验证。可稍后执行：ima_sync.py init --check")

    print("\n" + "=" * 56)
    print("初始化完成！之后直接说“保存进度 / 读取交接”即可。")
    print(f"当前交接位置：《{final_nb}》 / 《{final_title}》")
    print("=" * 56)
    return 0 if ok else 1


def handle_init(check: bool):
    print("=" * 56)
    print(" ima-handoff 初始化向导")
    print("=" * 56)

    cfg = load_config()
    cur_title = cfg.get("note_title") or DEFAULT_TITLE

    # ---- 步骤 1：确认/命名交接笔记本 ----
    print("\n【步骤 1 / 3】交接笔记本（同步空间）")
    print("本技能会把所有会话的进度追加到同一篇 ima 笔记里。")
    print("请先在 腾讯 ima (ima.qq.com) → 笔记 中新建一篇空笔记作为同步空间，")
    print("或沿用已有笔记。脚本会在检测到标题匹配时自动追加，找不到时自动创建。")
    print(f"\n默认笔记本名称：《{DEFAULT_TITLE}》")
    title = _prompt(f"输入交接笔记本名称（同步空间）", cur_title)
    cfg["note_title"] = title
    save_config(cfg)
    print(f"✓ 已保存交接笔记本名称：《{title}》 → {CONFIG_FILE}")

    # ---- 步骤 2：绑定凭证 ----
    print("\n【步骤 2 / 3】绑定腾讯 ima OpenAPI 凭证")
    print("获取方式：登录 ima.qq.com → 设置 / 开放平台 → 创建应用，拿到 Client ID 与 API Key。")
    env_cid, env_key = load_credentials()
    print("\n当前凭证状态："
          + ("Client ID 已配置" if env_cid else "Client ID 未配置")
          + " / " + ("API Key 已配置" if env_key else "API Key 未配置"))
    print("凭证可二选一：① 写入本脚本默认读取的文件（推荐，免环境变量）；② 自行 export 环境变量。")

    write_files = _prompt("是否把凭证保存到 ~/.config/ima/（y/n，默认 y）", "y").lower()
    if write_files.startswith("y"):
        cid = _prompt("Client ID", env_cid)
        key = _prompt("API Key（输入不回显）", env_key, secret=True)
        try:
            CRED_DIR.mkdir(parents=True, exist_ok=True)
            CID_FILE.write_text(cid, encoding="utf-8")
            KEY_FILE.write_text(key, encoding="utf-8")
            try:
                os.chmod(CID_FILE, 0o600)
                os.chmod(KEY_FILE, 0o600)
            except Exception:
                pass
            print(f"✓ 凭证已保存到 {CID_FILE} 与 {KEY_FILE}")
        except Exception as e:
            print(f"✗ 凭证保存失败：{e}", file=sys.stderr)
    else:
        print("请在每台电脑的 shell 配置文件中加入以下两行（Linux/macOS 用 ~/.zshrc / ~/.bashrc）：")
        print('  export IMA_OPENAPI_CLIENTID="你的Client_ID"')
        print('  export IMA_OPENAPI_APIKEY="你的API_Key"')
        print("Windows 可用：setx IMA_OPENAPI_CLIENTID \"你的Client_ID\" 与 setx IMA_OPENAPI_APIKEY \"你的API_Key\"")
        print("保存后执行 source ~/.zshrc（或重开终端）使其生效。")

    # ---- 步骤 3：连通性验证（只读，不污染笔记）----
    print("\n【步骤 3 / 3】连通性验证")
    do_check = check or _prompt(
        "是否立即验证云端连通（只读探测，不会写入任何内容）（y/n，默认 y）", "y"
    ).lower().startswith("y")
    if do_check:
        cid, key = load_credentials()
        if not cid or not key:
            print("✗ 凭证仍缺失，跳过验证。请完成步骤 2 后重试。")
        else:
            try:
                note_id = find_note_id(title)
                if note_id:
                    # 只读：能取到正文即证明「鉴权 + 检索 + 读取」全链路可用
                    res = request_ima(
                        "/openapi/note/v1/get_doc_content",
                        {"note_id": note_id, "target_content_format": 1},
                    )
                    blocks = parse_blocks(_extract_content(res.get("data", res)))
                    print(f"✓ 云端连通正常：笔记《{title}》已存在"
                          f"(note_id={note_id})，现有 {len(blocks)} 条交接记录。")
                else:
                    # 笔记还不存在：只验证「鉴权 + 检索」可用，不提前建空笔记
                    print(f"✓ 云端连通正常：凭证有效、检索可用。")
                    print(f"  笔记《{title}》尚未创建，首次执行 write 时会自动新建。")
            except (Exception, SystemExit) as e:
                print(f"✗ 云端连通失败：{e}", file=sys.stderr)
                print("  请检查凭证是否正确、网络是否可达，修复后重新 init。")
    else:
        print("已跳过连通性验证。可稍后执行：python3 ima_sync.py init --check")

    print("\n" + "=" * 56)
    print("初始化完成！之后 DSH 只需说“保存进度 / 读取交接”即可。")
    print(f"当前交接笔记本：《{default_title()}》")
    print("=" * 56)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DSH 腾讯 ima 跨设备记忆同步工具 (支持会话ID/设备标签与按会话过滤读取)"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    init_parser = subparsers.add_parser(
        "init", help="初始化：命名笔记本 + 交接笔记 + 绑定凭证 + 连通性验证"
    )
    init_parser.add_argument("--check", action="store_true", help="只做连通性验证")
    init_parser.add_argument("--notebook", default=None,
                             help="笔记本（同步空间）名称，由 DSH 传入")
    init_parser.add_argument("--note-title", default=None,
                             help="交接笔记名称，默认《DSH开发任务交接》")
    init_parser.add_argument("--client-id", default=None, help="写入 Client ID")
    init_parser.add_argument("--api-key", default=None, help="写入 API Key")
    init_parser.add_argument("--keep-credentials", action="store_true",
                             help="沿用现有凭证，不重新输入")
    init_parser.add_argument("--no-verify", action="store_true",
                             help="跳过连通性验证")
    init_parser.add_argument("--no-create", action="store_true",
                             help="不创建笔记本（笔记本创建后不可删）")

    read_parser = subparsers.add_parser("read", help="读取交接笔记")
    read_parser.add_argument("--notebook", default=None, help="笔记本名称；省略时用 init 配置")
    read_parser.add_argument("--title", default=None, help="笔记标题；省略时用 init 配置")
    read_parser.add_argument("--session", default=None,
                             help="按会话ID过滤，只返回该会话最新一条记录")
    read_parser.add_argument("--all", action="store_true", help="输出全部记录")
    read_parser.add_argument("--json", action="store_true", dest="as_json",
                             help="以结构化 JSON 输出")

    write_parser = subparsers.add_parser("write", help="写入交接进度")
    write_parser.add_argument("--notebook", default=None, help="笔记本名称；省略时用 init 配置")
    write_parser.add_argument("--title", default=None, help="笔记标题；省略时用 init 配置")
    write_parser.add_argument("--project", required=True)
    write_parser.add_argument("--content", required=True)
    write_parser.add_argument("--session", default=None,
                              help="会话ID；省略时按 环境变量>缓存>随机 兜底")
    write_parser.add_argument("--host", default=None,
                              help="设备名；省略时取本机 hostname")

    args = parser.parse_args()
    if args.action == "init":
        # 有任一显式参数 → 走非交互模式（DSH 在对话中收集答案后直接执行）
        non_interactive = any([
            args.notebook, args.note_title, args.client_id, args.api_key,
            args.keep_credentials, args.no_verify, args.no_create,
        ])
        if non_interactive:
            sys.exit(handle_init_apply(
                notebook=args.notebook,
                note_title=args.note_title,
                client_id=args.client_id,
                api_key=args.api_key,
                keep_credentials=args.keep_credentials,
                verify=not args.no_verify,
                create=not args.no_create,
            ))
        handle_init(args.check)
    elif args.action == "read":
        handle_read(args.title or default_title(), args.session, args.all,
                    args.as_json, args.notebook or default_notebook())
    elif args.action == "write":
        handle_write(args.title or default_title(), args.project, args.content,
                     args.session, args.host, args.notebook or default_notebook())
