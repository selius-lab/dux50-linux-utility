#!/usr/bin/env python3
"""dux50 web UI - a small, dependency-free browser front end for dux50.py.

Serves one page on http://127.0.0.1:8765/ that lets you read and change the
ELECOM M-DUX50 / M-DUX30 configuration from a browser: device info, button
assignments, DPI stages, report rate, backup / restore and raw read/write.

The mouse is reached through usbfs, so run this with root:

    sudo python3 webui.py                 # open http://127.0.0.1:8765/
    sudo python3 webui.py --port 9000 --no-browser

Pure Python 3 standard library - no dependencies (uses dux50.py for the
device protocol).
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import dux50

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backups")
DEV_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------- device layer

def _open_device():
    info = dux50.find_device()
    if not info:
        raise ApiError("マウスが見つかりません (VID 056e / PID 00e7-00ea)", 404)
    try:
        dev = dux50.Dux50(info)
    except OSError as e:
        raise ApiError(f"デバイスを開けません: {e} (root 権限が必要です)", 403)
    return dev, info


def with_device(fn):
    """Run fn(dev, info) with exclusive access to the mouse."""
    with DEV_LOCK:
        dev, info = _open_device()
        try:
            return fn(dev, info)
        finally:
            dev.close()


def save_backup(data, prefix="dux50-backup"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, time.strftime(f"{prefix}-%Y%m%d-%H%M%S.bin"))
    with open(path, "wb") as f:
        f.write(data)
    return path


def device_json(info):
    return {"pid": info["pid"], "name": dux50.PIDS[info["pid"]],
            "busnum": info["busnum"], "devnum": info["devnum"],
            "iface": info["iface"]}


def req_int(body, key, default=None):
    if key not in body or body[key] is None:
        if default is not None:
            return default
        raise ApiError(f"{key} が必要です")
    try:
        return int(body[key])
    except (TypeError, ValueError):
        raise ApiError(f"{key} が数値ではありません")


def key_to_hid(ch):
    ch = ch.lower()
    if len(ch) != 1:
        raise ApiError("キーは 1 文字で指定してください")
    if "a" <= ch <= "z":
        return 0x04 + ord(ch) - ord("a")
    if "1" <= ch <= "9":
        return 0x1E + ord(ch) - ord("1")
    if ch == "0":
        return 0x27
    raise ApiError(f"未対応のキーです: {ch!r}")


# ------------------------------------------------------------------ endpoints

def api_snapshot(profile):
    def run(dev, info):
        cfg = dev.read_config()
        names = [s for _, s in dux50.extract_utf16_strings(cfg) if len(s) >= 3]
        buttons = []
        for i, name, t, tname, extra in dux50.decode_buttons(cfg, profile):
            buttons.append({
                "index": i, "name": name, "type": t, "type_name": tname,
                "extra": extra,
                "b": cfg[dux50.btn_off(profile, i, "b")],
                "c": cfg[dux50.btn_off(profile, i, "c")],
                "d": cfg[dux50.btn_off(profile, i, "d")],
            })
        stages = [{"stage": s, "x": x, "y": y, "xdpi": dx, "ydpi": dy}
                  for s, x, y, dx, dy in dux50.decode_dpi(cfg)]
        val, hz = dux50.decode_report_rate(cfg)
        return {
            "device": device_json(info),
            "info": {"used": dux50.used_size(cfg), "size": dux50.CONFIG_SIZE,
                     "names": names},
            "buttons": {"count": dux50.BTN_COUNT, "names": dux50.BTN_NAMES,
                        "types": {str(k): v for k, v in dux50.TYPE_NAMES.items()},
                        "rows": buttons},
            "dpi": {"unit": dux50.DPI_UNIT, "count": dux50.DPI_STAGES,
                    "stages": stages},
            "rate": {"value": val, "hz": hz,
                     "options": [v for _, v in dux50.RATE_VALUES]},
        }
    return with_device(run)


def api_set_button(body):
    profile = req_int(body, "profile", default=0)
    index = req_int(body, "index")
    if not 0 <= index < dux50.BTN_COUNT:
        raise ApiError("ボタン番号が範囲外です (0..%d)" % (dux50.BTN_COUNT - 1))
    t = body.get("type")
    b = body.get("b")
    c = body.get("c")
    d = body.get("d")
    if body.get("key"):
        if t is None:
            t = 5
        c = key_to_hid(str(body["key"]))
    targets = [(a, v) for a, v in (("a", t), ("b", b), ("c", c), ("d", d))
               if v is not None]
    if not targets:
        raise ApiError("変更内容がありません")
    try:
        targets = [(a, int(v)) for a, v in targets]
    except (TypeError, ValueError):
        raise ApiError("値は数値で指定してください")
    for _, v in targets:
        if not 0 <= v <= 0xFF:
            raise ApiError("値は 0..255 で指定してください")

    def run(dev, info):
        save_backup(dev.read_config(), "dux50-pre-button")
        for arr, val in targets:
            dev.write(dux50.btn_off(profile, index, arr), bytes([val]))
            time.sleep(0.1)
        time.sleep(0.3)
        cfg = dev.read_config()
        if not all(cfg[dux50.btn_off(profile, index, a)] == v
                   for a, v in targets):
            raise ApiError("書き込みが反映されませんでした。デバイスが書き込みを"
                           "受け付けない状態かもしれません (docs/PROTOCOL.md 参照)。", 409)
        row = None
        for i, name, tt, tname, extra in dux50.decode_buttons(cfg, profile):
            if i == index:
                row = {"index": i, "name": name, "type": tt,
                       "type_name": tname, "extra": extra}
        return {"ok": True, "button": row}
    return with_device(run)


def api_set_dpi(body):
    stage = req_int(body, "stage")
    if not 0 <= stage < dux50.DPI_STAGES:
        raise ApiError("DPI 段が範囲外です (0..%d)" % (dux50.DPI_STAGES - 1))
    x = req_int(body, "x")
    y = req_int(body, "y", default=x)
    live = bool(body.get("live", True))
    xb, yb = dux50.dpi_byte(x), dux50.dpi_byte(y)

    def run(dev, info):
        save_backup(dev.read_config(), "dux50-pre-dpi")
        dev.write(dux50.dpi_off(stage, 0), bytes([xb, yb]), apply=False)
        if live:
            dev.set_dpi_live(xb, yb)
        time.sleep(0.15)
        dev.apply()
        time.sleep(0.2)
        cfg = dev.read_config()
        got = (cfg[dux50.dpi_off(stage, 0)], cfg[dux50.dpi_off(stage, 1)])
        if got != (xb, yb):
            raise ApiError("書き込みが反映されませんでした", 409)
        return {"ok": True, "stage": stage,
                "xdpi": got[0] * dux50.DPI_UNIT, "ydpi": got[1] * dux50.DPI_UNIT}
    return with_device(run)


def api_use_dpi(body):
    stage = req_int(body, "stage")
    if not 0 <= stage < dux50.DPI_STAGES:
        raise ApiError("DPI 段が範囲外です (0..%d)" % (dux50.DPI_STAGES - 1))

    def run(dev, info):
        cfg = dev.read_config()
        xb = cfg[dux50.dpi_off(stage, 0)]
        yb = cfg[dux50.dpi_off(stage, 1)]
        dev.select_dpi_stage(stage)
        time.sleep(0.2)
        return {"ok": True, "stage": stage,
                "xdpi": xb * dux50.DPI_UNIT, "ydpi": yb * dux50.DPI_UNIT}
    return with_device(run)


def api_set_rate(body):
    hz = req_int(body, "hz")
    try:
        val = dux50.rate_value(hz)
    except ValueError as e:
        raise ApiError(str(e))

    def run(dev, info):
        before = dev.read_config()
        save_backup(before, "dux50-pre-rate")
        new = (before[dux50.RATE_BASE] & ~dux50.RATE_MASK) | (val << dux50.RATE_SHIFT)
        cfg = before
        for _ in range(3):
            dev.write(dux50.RATE_BASE, bytes([new]))
            time.sleep(0.4)
            cfg = dev.read_config()
            if cfg[dux50.RATE_BASE] == new:
                break
        if dux50.decode_report_rate(cfg)[0] != val:
            raise ApiError("書き込みが反映されませんでした", 409)
        return {"ok": True, "hz": hz}
    return with_device(run)


def api_backup_download():
    return with_device(lambda dev, info: dev.read_config())


def api_restore(image):
    if len(image) != dux50.CONFIG_SIZE:
        raise ApiError("設定イメージは %d バイトである必要があります (受信 %d)"
                       % (dux50.CONFIG_SIZE, len(image)))

    def run(dev, info):
        before = dev.read_config()
        if image == before:
            return {"ok": True, "note": "すでに同じ内容のため書き込みませんでした"}
        save_backup(before, "dux50-pre-restore")
        dev.write_config(image)
        time.sleep(0.4)
        got = dev.read_config()
        if got == image:
            return {"ok": True}
        if got == before:
            raise ApiError("書き込みが無視されました。デバイスが書き込みを"
                           "受け付けない状態かもしれません (docs/PROTOCOL.md 参照)。", 409)
        raise ApiError("検証エラー: 書き込み後の内容が一致しません", 409)
    return with_device(run)


def api_raw_read(offset, length):
    if not 0 <= offset <= 0x10000 or not 1 <= length <= 0x10000:
        raise ApiError("offset / length が範囲外です")

    def run(dev, info):
        data = dev.read(offset, length)
        return {"offset": offset, "length": len(data),
                "hexdump": dux50.hexdump(data, offset),
                "hex": data.hex(" ")}
    return with_device(run)


def api_raw_write(body):
    addr = req_int(body, "addr")
    try:
        data = bytes.fromhex(body.get("hex") or "")
    except (ValueError, TypeError) as e:
        raise ApiError(f"hex の形式が不正です: {e}")
    if not data:
        raise ApiError("書き込むバイトがありません")

    def run(dev, info):
        before = dev.read(addr, len(data))
        save_backup(dev.read_config(), "dux50-pre-write")
        dev.write(addr, data)
        time.sleep(0.2)
        got = dev.read(addr, len(data))
        if got == data:
            return {"ok": True, "bytes": len(data)}
        if got == before:
            raise ApiError("書き込みが無視されました", 409)
        raise ApiError(f"検証エラー: {got.hex(' ')}", 409)
    return with_device(run)


# ------------------------------------------------------------- HTTP interface

def _int_arg(query, name, default, base=0):
    try:
        return int(query.get(name, [str(default)])[0], base)
    except ValueError:
        raise ApiError(f"{name} が数値ではありません")


class Handler(BaseHTTPRequestHandler):
    server_version = "dux50web"

    def _host_ok(self):
        host = self.headers.get("Host", "")
        return (host.startswith("127.0.0.1") or host.startswith("localhost")
                or host.startswith("[::1]"))

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, exc):
        self._send(exc.status, {"error": exc.message})

    def do_GET(self):
        try:
            if not self._host_ok():
                raise ApiError("許可されていない Host です", 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/snapshot":
                return self._send(200, api_snapshot(_int_arg(q, "profile", 0)))
            if u.path == "/api/backup":
                data = api_backup_download()
                name = time.strftime("dux50-backup-%Y%m%d-%H%M%S.bin")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if u.path == "/api/raw":
                return self._send(200, api_raw_read(_int_arg(q, "offset", 0),
                                                    _int_arg(q, "length", 256)))
            raise ApiError("見つかりません", 404)
        except ApiError as e:
            self._error(e)
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if not self._host_ok():
                raise ApiError("許可されていない Host です", 403)
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if u.path == "/api/restore":
                return self._send(200, api_restore(raw))
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                raise ApiError("JSON の形式が不正です")
            if u.path == "/api/button":
                return self._send(200, api_set_button(body))
            if u.path == "/api/dpi":
                return self._send(200, api_set_dpi(body))
            if u.path == "/api/use-dpi":
                return self._send(200, api_use_dpi(body))
            if u.path == "/api/report-rate":
                return self._send(200, api_set_rate(body))
            if u.path == "/api/write":
                return self._send(200, api_raw_write(body))
            raise ApiError("見つかりません", 404)
        except ApiError as e:
            self._error(e)
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------------ page

PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dux50 Web UI</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--fg:#e6e9ef;--muted:#8b93a7;--acc:#4f8cff;
--ok:#2fbf71;--err:#e5534b;--line:#252a35}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans JP",sans-serif;
background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0}
h1 span{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
main{padding:20px;display:grid;gap:16px;max-width:1020px;margin:0 auto}
section,details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
h2{font-size:12px;margin:0 0 12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:600}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:6px 12px;
cursor:pointer;font-size:13px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button:hover{filter:brightness(1.12)}
input,select{background:#0d0f14;color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:5px 8px;font-size:13px}
input[type=number]{width:92px}
label{font-size:13px}
.chip{font-size:12px;padding:4px 10px;border-radius:999px;background:#222733;color:var(--muted);
white-space:nowrap}
.chip.ok{background:rgba(47,191,113,.15);color:var(--ok)}
.chip.err{background:rgba(229,83,75,.15);color:var(--err)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.muted{color:var(--muted)}
pre{background:#0d0f14;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;
font-size:12px;max-height:320px}
.radio{display:inline-flex;align-items:center;gap:4px;margin-right:14px}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(10px);
background:#222733;padding:10px 16px;border-radius:8px;opacity:0;transition:.2s;
pointer-events:none;max-width:80vw}
#toast.show{opacity:1;transform:translateX(-50%)}
#toast.err{background:var(--err)}
.sep{height:1px;background:var(--line);margin:12px 0}
</style>
</head>
<body>
<header>
  <h1>dux50 <span>ELECOM M-DUX50 / M-DUX30 設定</span></h1>
  <div id="device" class="chip">接続確認中…</div>
  <button class="ghost" onclick="refresh()">再読み込み</button>
</header>
<main>
  <section>
    <h2>デバイス情報</h2>
    <div id="info" class="row muted">-</div>
  </section>

  <section>
    <h2>ボタン割当</h2>
    <div class="row" style="margin-bottom:10px">
      <label>プロファイル
        <select id="profile" onchange="refresh()"></select>
      </label>
    </div>
    <table>
      <thead><tr><th>#</th><th>ボタン</th><th>機能</th><th>詳細</th></tr></thead>
      <tbody id="btn-rows"></tbody>
    </table>
    <div class="sep"></div>
    <div class="row">
      <label>変更するボタン <select id="btn-index"></select></label>
      <label>機能 <select id="btn-type" onchange="syncBtnFields()"></select></label>
      <label id="f-key">キー <input id="btn-key" maxlength="1" size="3" placeholder="a"></label>
      <label id="f-macro">マクロ番号 <input id="btn-macro" type="number" min="0" max="255"></label>
      <span id="f-raw" class="row">
        <label>B <input id="btn-b" size="4" placeholder="00"></label>
        <label>C <input id="btn-c" size="4" placeholder="00"></label>
        <label>D <input id="btn-d" size="4" placeholder="00"></label>
      </span>
      <button onclick="applyButton()">適用</button>
    </div>
    <div class="muted" style="margin-top:6px">
      Keyboard は「キー」、Macro は「マクロ番号」、それ以外は B/C/D (16進) を指定します。
    </div>
  </section>

  <section>
    <h2>DPI 設定</h2>
    <table>
      <thead><tr><th>段</th><th>X (dpi)</th><th>Y (dpi)</th><th>生値</th><th>操作</th></tr></thead>
      <tbody id="dpi-rows"></tbody>
    </table>
    <div class="muted" style="margin-top:6px">
      dpi = 生値 × <span id="dpi-unit">50</span>。「適用」で保存＋即時反映、「使用」でその段に切替。
    </div>
  </section>

  <section>
    <h2>レポートレート</h2>
    <div class="row">
      <span>現在: <b id="rate-current">-</b></span>
      <span id="rate-opts" class="row"></span>
      <button onclick="setRate()">適用</button>
    </div>
  </section>

  <section>
    <h2>バックアップ / 復元</h2>
    <div class="row">
      <button class="ghost" onclick="location.href='/api/backup'">現在の設定をダウンロード</button>
      <span class="muted">4096 バイトの設定イメージ</span>
    </div>
    <div class="sep"></div>
    <div class="row">
      <input type="file" id="restore-file" accept=".bin,application/octet-stream">
      <button onclick="doRestore()">復元</button>
      <span class="muted">選択したイメージを本体へ書き戻します（実行前に自動バックアップ）</span>
    </div>
    <div class="muted" id="backup-note" style="margin-top:6px"></div>
  </section>

  <details>
    <summary>上級者向け — 生アドレスの読み書き</summary>
    <div class="sep"></div>
    <div class="row">
      <label>offset <input id="raw-offset" size="8" value="0x40"></label>
      <label>length <input id="raw-length" size="6" value="0x20"></label>
      <button onclick="rawRead()">読み出し</button>
    </div>
    <pre id="raw-out" style="margin-top:10px">-</pre>
    <div class="row">
      <label>addr <input id="raw-addr" size="8" value="0x110"></label>
      <label>hex <input id="raw-hex" size="24" placeholder="deadbeef"></label>
      <button onclick="rawWrite()">書き込み</button>
    </div>
  </details>
</main>
<div id="toast"></div>

<script>
let state = null;

function toast(msg, ok) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (ok === false ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = ''; }, 3200);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
  return data;
}

const hex2 = n => n.toString(16).padStart(2, '0');
const opt = (v, label, sel) => `<option value="${v}"${sel ? ' selected' : ''}>${label}</option>`;

function renderProfile(s) {
  const sel = document.getElementById('profile');
  if (sel.options.length === 0) {
    let h = '';
    for (let p = 0; p < 5; p++) h += opt(p, 'profile ' + p, p === 0);
    sel.innerHTML = h;
  }
}

function renderInfo(s) {
  document.getElementById('info').innerHTML =
    `<span>PID 0x${s.device.pid} — ${s.device.name}</span>` +
    `<span>bus ${s.device.busnum} / dev ${s.device.devnum} / iface ${s.device.iface}</span>` +
    `<span>設定 ${s.info.used} / ${s.info.size} バイト使用</span>` +
    (s.info.names.length ? `<span>名前: ${s.info.names.slice(0, 12).join(', ')}</span>` : '');
}

function renderButtons(s) {
  const rows = document.getElementById('btn-rows');
  let h = '';
  s.buttons.rows.forEach(b => {
    h += `<tr><td>${b.index}</td><td>${b.name}</td><td>${b.type_name}` +
         ` <span class="muted">(0x${hex2(b.type)})</span></td>` +
         `<td class="muted">${b.extra || ''}</td></tr>`;
  });
  rows.innerHTML = h;

  const idx = document.getElementById('btn-index');
  if (idx.options.length === 0) {
    let o = '';
    s.buttons.rows.forEach(b => { o += opt(b.index, b.index + ': ' + b.name); });
    idx.innerHTML = o;
    const tp = document.getElementById('btn-type');
    let to = '';
    Object.keys(s.buttons.types).sort((a, c) => a - c)
      .forEach(k => { to += opt(k, k + ' = ' + s.buttons.types[k]); });
    tp.innerHTML = to;
    syncBtnFields();
  }
}

function syncBtnFields() {
  const t = +document.getElementById('btn-type').value;
  document.getElementById('f-key').style.display = (t === 5) ? '' : 'none';
  document.getElementById('f-macro').style.display = ([8, 9, 10].includes(t)) ? '' : 'none';
  document.getElementById('f-raw').style.display = (t === 5 || [8, 9, 10].includes(t)) ? 'none' : '';
}

async function applyButton() {
  const body = {
    profile: +document.getElementById('profile').value,
    index: +document.getElementById('btn-index').value,
    type: +document.getElementById('btn-type').value,
  };
  if (body.type === 5) {
    const k = document.getElementById('btn-key').value;
    if (!k) return toast('キーを入力してください', false);
    body.key = k;
  } else if ([8, 9, 10].includes(body.type)) {
    const m = document.getElementById('btn-macro').value;
    if (m === '') return toast('マクロ番号を入力してください', false);
    body.d = +m;
  } else {
    for (const k of ['b', 'c', 'd']) {
      const v = document.getElementById('btn-' + k).value.trim();
      if (v !== '') body[k] = parseInt(v, 16);
    }
  }
  try {
    await api('/api/button', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    toast('適用しました');
    refresh();
  } catch (e) { toast(e.message, false); }
}

function renderDpi(s) {
  document.getElementById('dpi-unit').textContent = s.dpi.unit;
  let h = '';
  s.dpi.stages.forEach(st => {
    h += `<tr><td>stage ${st.stage}</td>` +
      `<td><input id="dx${st.stage}" type="number" value="${st.xdpi}"></td>` +
      `<td><input id="dy${st.stage}" type="number" value="${st.ydpi}"></td>` +
      `<td class="muted">0x${hex2(st.x)} / 0x${hex2(st.y)}</td>` +
      `<td><button onclick="setDpi(${st.stage})">適用</button> ` +
      `<button class="ghost" onclick="useDpi(${st.stage})">使用</button></td></tr>`;
  });
  document.getElementById('dpi-rows').innerHTML = h;
}

async function setDpi(stage) {
  const x = +document.getElementById('dx' + stage).value;
  const y = +document.getElementById('dy' + stage).value;
  try {
    const r = await api('/api/dpi', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stage, x, y, live: true})});
    toast(`stage ${stage} を ${r.xdpi} / ${r.ydpi} dpi に設定しました`);
    refresh();
  } catch (e) { toast(e.message, false); }
}

async function useDpi(stage) {
  try {
    const r = await api('/api/use-dpi', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stage})});
    toast(`stage ${r.stage} (${r.xdpi} / ${r.ydpi} dpi) に切り替えました`);
  } catch (e) { toast(e.message, false); }
}

function renderRate(s) {
  document.getElementById('rate-current').textContent =
    s.rate.hz ? (s.rate.hz + ' Hz') : ('不明 (値 ' + s.rate.value + ')');
  let h = '';
  s.rate.options.forEach(hz => {
    const c = (hz === s.rate.hz) ? ' checked' : '';
    h += `<label class="radio"><input type="radio" name="rate" value="${hz}"${c}> ${hz} Hz</label>`;
  });
  document.getElementById('rate-opts').innerHTML = h;
}

async function setRate() {
  const el = document.querySelector('input[name=rate]:checked');
  if (!el) return;
  try {
    await api('/api/report-rate', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({hz: +el.value})});
    toast('レポートレートを ' + el.value + ' Hz に変更しました');
    refresh();
  } catch (e) { toast(e.message, false); }
}

async function doRestore() {
  const f = document.getElementById('restore-file').files[0];
  if (!f) return toast('ファイルを選択してください', false);
  if (!confirm('現在の設定をこのファイルで上書きします。よろしいですか？')) return;
  try {
    const buf = await f.arrayBuffer();
    await api('/api/restore', {method: 'POST',
      headers: {'Content-Type': 'application/octet-stream'}, body: buf});
    toast('復元しました');
    refresh();
  } catch (e) { toast(e.message, false); }
}

async function rawRead() {
  const o = document.getElementById('raw-offset').value;
  const l = document.getElementById('raw-length').value;
  try {
    const r = await api('/api/raw?offset=' + encodeURIComponent(o) +
                        '&length=' + encodeURIComponent(l));
    document.getElementById('raw-out').textContent = r.hexdump;
  } catch (e) { toast(e.message, false); }
}

async function rawWrite() {
  const body = {addr: parseInt(document.getElementById('raw-addr').value, 16),
                hex: document.getElementById('raw-hex').value};
  if (!body.hex.trim()) return toast('hex を入力してください', false);
  if (!confirm('0x' + body.addr.toString(16) + ' に書き込みます。よろしいですか？')) return;
  try {
    const r = await api('/api/write', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    toast(r.bytes + ' バイト書き込みました');
    rawRead();
  } catch (e) { toast(e.message, false); }
}

async function refresh() {
  const chip = document.getElementById('device');
  chip.textContent = '読み込み中…';
  chip.className = 'chip';
  try {
    const profile = document.getElementById('profile').value || 0;
    const s = await api('/api/snapshot?profile=' + profile);
    state = s;
    chip.textContent = '接続OK';
    chip.className = 'chip ok';
    renderProfile(s);
    renderInfo(s);
    renderButtons(s);
    renderDpi(s);
    renderRate(s);
  } catch (e) {
    chip.textContent = e.message;
    chip.className = 'chip err';
  }
}

window.addEventListener('DOMContentLoaded', () => { refresh(); });
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="dux50 web UI")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("warning: not running as root - the mouse may not be accessible",
              file=sys.stderr)

    httpd = Server((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"dux50 web UI listening on {url}")
    print("press Ctrl-C to stop")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
