# -*- coding: utf-8 -*-
"""Flask REST API + 静态页面服务。
所有接口绑定 127.0.0.1，仅供本机（应用窗口 / Hermes agent）调用。"""
import os
import re
import sys
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import db
from services import weather, favicon as favicon_service
from services import updater
from app import APP_VERSION

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.isdir(STATIC_DIR) and hasattr(sys, "_MEIPASS"):
    STATIC_DIR = os.path.join(sys._MEIPASS, "static")

app = Flask(__name__, static_folder=None)

VALID_STATUS = {"todo", "doing", "done"}
VALID_PRIORITY = {"low", "medium", "high"}


def _parse_tags(tags):
    if tags is None:
        return ""
    if isinstance(tags, list):
        return ",".join(str(t).strip() for t in tags if str(t).strip())
    parts = [p.strip() for p in str(tags).split(",") if p.strip()]
    return ",".join(parts)


def _task_out(row):
    row["tags"] = [t for t in row.get("tags", "").split(",") if t]
    return row


# ---------------- 健康 / 统计 ----------------

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "workbench", "version": APP_VERSION})


# ---------------- 更新 ----------------

@app.get("/api/update/check")
def update_check():
    """检查 GitHub 最新版本。返回 {tag, has_update, ...}"""
    return jsonify(updater.check(APP_VERSION))


@app.post("/api/update/download")
def update_download():
    """下载新版本 zip 到 data/updates/。body: {url, filename}"""
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    filename = str(data.get("filename", "workbench.zip")).strip()
    if not url.startswith("https://github.com/"):
        return jsonify({"error": "下载地址无效"}), 400
    try:
        path = updater.download(url, filename)
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"error": f"下载失败：{e}"}), 502


@app.get("/api/update/progress")
def update_progress():
    """查询下载进度（流式下载时前端轮询）。"""
    p = updater.progress()
    if p is None:
        return jsonify({"downloaded": 0, "total": 0, "percent": 0, "done": False, "active": False})
    p["active"] = True
    return jsonify(p)


@app.post("/api/update/apply")
def update_apply():
    """应用更新：启动 update.bat 替换文件并重启（本进程 1s 后退出）。"""
    try:
        updater.apply()
        return jsonify({"ok": True, "message": "更新已开始，应用将自动重启"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/stats")
def stats():
    conn = db.get_conn()
    try:
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        }
        today = datetime.now().strftime("%Y-%m-%d")
        done_today = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE completed_at LIKE ?",
            (today + "%",),
        ).fetchone()["n"]
    finally:
        conn.close()
    return jsonify({
        "total": sum(counts.values()),
        "todo": counts.get("todo", 0),
        "doing": counts.get("doing", 0),
        "done": counts.get("done", 0),
        "done_today": done_today,
    })


# ---------------- 任务 ----------------

@app.get("/api/tasks")
def tasks_list():
    status = request.args.get("status")
    if status and status not in VALID_STATUS:
        return jsonify({"error": f"status 必须是 {sorted(VALID_STATUS)}"}), 400
    rows = db.list_tasks(status=status)
    return jsonify([_task_out(r) for r in rows])


@app.post("/api/tasks")
def tasks_create():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "title 不能为空"}), 400
    status = data.get("status", "todo")
    if status not in VALID_STATUS:
        return jsonify({"error": f"status 必须是 {sorted(VALID_STATUS)}"}), 400
    priority = data.get("priority", "medium")
    if priority not in VALID_PRIORITY:
        return jsonify({"error": f"priority 必须是 {sorted(VALID_PRIORITY)}"}), 400
    source = data.get("source", "manual")
    if source not in {"manual", "agent"}:
        return jsonify({"error": "source 必须是 manual 或 agent"}), 400

    row = db.create_task(
        title=title,
        description=str(data.get("description", "")).strip(),
        status=status,
        priority=priority,
        due_date=str(data.get("due_date", "")).strip(),
        tags=_parse_tags(data.get("tags")),
        source=source,
    )
    return jsonify(_task_out(row)), 201


@app.get("/api/tasks/<int:task_id>")
def tasks_get(task_id):
    row = db.get_task(task_id)
    if not row:
        return jsonify({"error": "任务不存在"}), 404
    data = _task_out(row)
    data["events"] = db.list_events(task_id)
    return jsonify(data)


@app.patch("/api/tasks/<int:task_id>")
def tasks_update(task_id):
    row = db.get_task(task_id)
    if not row:
        return jsonify({"error": "任务不存在"}), 404
    data = request.get_json(silent=True) or {}
    fields = {}
    if "title" in data:
        fields["title"] = str(data["title"]).strip() or row["title"]
    if "description" in data:
        fields["description"] = str(data["description"]).strip()
    if "status" in data:
        if data["status"] not in VALID_STATUS:
            return jsonify({"error": f"status 必须是 {sorted(VALID_STATUS)}"}), 400
        fields["status"] = data["status"]
    if "priority" in data:
        if data["priority"] not in VALID_PRIORITY:
            return jsonify({"error": f"priority 必须是 {sorted(VALID_PRIORITY)}"}), 400
        fields["priority"] = data["priority"]
    if "due_date" in data:
        fields["due_date"] = str(data["due_date"]).strip()
    if "tags" in data:
        fields["tags"] = _parse_tags(data["tags"])
    updated = db.update_task(task_id, **fields)
    return jsonify(_task_out(updated))


@app.delete("/api/tasks/<int:task_id>")
def tasks_delete(task_id):
    if not db.get_task(task_id):
        return jsonify({"error": "任务不存在"}), 404
    db.delete_task(task_id)
    return jsonify({"ok": True})


# ---------------- 便签 ----------------

@app.get("/api/notes")
def notes_list():
    return jsonify(db.list_notes())


@app.post("/api/notes")
def notes_create():
    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"error": "content 不能为空"}), 400
    return jsonify(db.create_note(content)), 201


@app.delete("/api/notes/<int:note_id>")
def notes_delete(note_id):
    if not db.get_note(note_id):
        return jsonify({"error": "便签不存在"}), 404
    db.delete_note(note_id)
    return jsonify({"ok": True})


# ---------------- 天气 ----------------

@app.get("/api/weather")
def weather_now():
    code = db.get_setting("city_code", "101020100")
    lat = db.get_setting("lat", "31.2304")
    lon = db.get_setting("lon", "121.4737")
    city = db.get_setting("city", "上海")
    try:
        data = weather.forecast(code, lat=lat, lon=lon, city_name=city)
    except Exception:
        return jsonify({"error": "天气服务暂时不可用（网络问题）"}), 502
    return jsonify(data)


@app.get("/api/weather/search")
def weather_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    return jsonify(weather.geocode(q))


# ---------------- 快捷链接 ----------------

@app.get("/api/links")
def links_list():
    return jsonify(db.list_links())


@app.post("/api/links")
def links_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    url = str(data.get("url", "")).strip()
    if not name or not url:
        return jsonify({"error": "name 和 url 不能为空"}), 400
    # 拒绝带协议前缀但不是 http/https 的（javascript: 等）
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", url) and not url.startswith(
        ("http://", "https://", "ftp://")
    ):
        return jsonify({"error": "url 必须是 http(s) 地址"}), 400
    if not url.startswith(("http://", "https://", "ftp://")):
        url = "https://" + url
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "url 必须是 http(s) 地址"}), 400
    return jsonify(db.create_link(
        name=name,
        url=url,
        icon=str(data.get("icon", "")).strip(),
        sort_order=int(data.get("sort_order") or 0) if str(data.get("sort_order", "")).isdigit() else 0,
    )), 201


@app.patch("/api/links/<int:link_id>")
def links_update(link_id):
    if not db.get_link(link_id):
        return jsonify({"error": "链接不存在"}), 404
    data = request.get_json(silent=True) or {}
    fields = {}
    if "name" in data:
        fields["name"] = str(data["name"]).strip()
    if "url" in data:
        fields["url"] = str(data["url"]).strip()
    if "icon" in data:
        fields["icon"] = str(data["icon"]).strip()
    if not fields:
        return jsonify({"error": "没有可更新的字段"}), 400
    db.update_link(link_id, **fields)
    return jsonify(db.get_link(link_id))


@app.delete("/api/links/<int:link_id>")
def links_delete(link_id):
    if not db.get_link(link_id):
        return jsonify({"error": "链接不存在"}), 404
    db.delete_link(link_id)
    return jsonify({"ok": True})


# favicon 抓取：GET /api/links/favicon?url=https://github.com → {"icon": "/icons/github.com.png"}
# url 可省略协议（如 bing.com），自动补全 https://
@app.get("/api/links/favicon")
def links_favicon():
    url = str(request.args.get("url", "")).strip()
    if not url:
        return jsonify({"error": "url 不能为空"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        icon = favicon_service.fetch_favicon(url)
    except Exception:
        icon = None
    if not icon:
        return jsonify({"icon": ""}), 404
    return jsonify({"icon": icon})


# 图标静态文件：data/icons/ 目录
@app.get("/icons/<path:filename>")
def icons_static(filename):
    icons_dir = os.path.join(BASE_DIR, "data", "icons")
    if os.path.isfile(os.path.join(icons_dir, filename)):
        return send_from_directory(icons_dir, filename)
    return jsonify({"error": "图标不存在"}), 404


# ---------------- 番茄钟 ----------------

@app.get("/api/pomodoro")
def pomodoro_get():
    return jsonify(db.pomodoro_state())


@app.post("/api/pomodoro/complete")
def pomodoro_complete():
    return jsonify(db.pomodoro_complete())


# ---------------- 设置 ----------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Workbench"


def _autostart_command():
    """生成开机自启命令：打包版直接指向 exe；源码版用 pythonw 跑 app.py。
    若进程是 --port 启动（如测试版 17891），自启命令带上该参数，避免 find_port 复用他人服务。"""
    extra = ""
    if "--port" in sys.argv:
        try:
            extra = f" --port {int(sys.argv[sys.argv.index('--port') + 1])}"
        except (ValueError, IndexError):
            pass
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"{extra}'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    app_py = os.path.join(BASE_DIR, "app.py")
    return f'"{pyw}" "{app_py}"{extra}'


@app.get("/api/settings/autostart")
def autostart_get():
    """查询开机自启状态：读注册表为准（settings 仅作界面记忆）。"""
    enabled = False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            enabled = True
    except FileNotFoundError:
        enabled = False
    except OSError:
        pass
    return jsonify({"enabled": enabled})


@app.post("/api/settings/autostart")
def autostart_set():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        db.set_setting("autostart", "1" if enabled else "0")
        return jsonify({"ok": True, "enabled": enabled})
    except OSError as e:
        return jsonify({"error": f"设置开机自启失败：{e}"}), 500


@app.get("/api/settings")
def settings_get():
    s = db.get_all_settings()
    # 兜底：老库缺少新键时补默认值
    for k, v in db.DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return jsonify(s)


@app.put("/api/settings")
def settings_update():
    data = request.get_json(silent=True) or {}
    # 通用设置项（布尔开关等）：confirm_delete_* / autostart
    SIMPLE_KEYS = {"autostart", "confirm_delete_task", "confirm_delete_link", "confirm_delete_note"}
    for k in SIMPLE_KEYS:
        if k in data:
            db.set_setting(k, "1" if data[k] else "0")
    # 直接给城市代码（前端从搜索结果选定）：city + city_code 一起存
    if "city_code" in data and str(data.get("city_code", "")).strip():
        code = str(data["city_code"]).strip()
        city = str(data.get("city", "")).strip()
        if not city:
            hit = weather.city_by_code(code)
            city = hit["n"] if hit else ""
        db.set_setting("city_code", code)
        db.set_setting("city", city)
        return jsonify(db.get_all_settings())
    # 按城市名查表（向后兼容）
    if "city" in data and str(data["city"]).strip():
        found = weather.geocode(str(data["city"]).strip())
        if found:
            db.set_setting("city", found[0]["n"])
            db.set_setting("city_code", found[0]["c"])
            return jsonify(db.get_all_settings())
        return jsonify({"error": "未找到城市（仅支持国内城市）"}), 404
    return jsonify(db.get_all_settings())


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


def run_server(port):
    db.init_db()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
