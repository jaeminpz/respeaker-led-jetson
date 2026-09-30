#!/usr/bin/env python3
"""LED 색·속도를 눈으로 보며 고르는 도구 — ReSpeaker 2-Mics Pi HAT v2.0.

브라우저에서 색을 찍으면 LED 가 즉시 바뀐다. 마음에 드는 값이 나오면
"코드로 내보내기" 로 led_controller.py 에 붙여넣을 상수를 받는다.

    sudo ./led_picker.py
    # 그 다음 이 기기의 브라우저에서 http://127.0.0.1:8777

왜 이런 구조인가
----------------
LED 는 한 프로세스만 잡을 수 있다(gpiod EBUSY). 그래서 이 도구가 LedController
를 직접 소유하고, 브라우저는 HTTP 로 값만 던진다.

색과 속도는 led_controller 의 모듈 전역이고 애니메이션 함수가 매 프레임 그걸
읽어가므로, 여기서 전역을 갈아끼우면 다음 프레임부터 바로 반영된다.

주의
----
LED 를 만지려면 root 여야 해서 이 서버도 root 로 돈다. 그래서 127.0.0.1 에만
묶고 외부에서 접속할 수 없게 했다. 다루는 것은 색과 속도 값뿐이고 파일을
서빙하거나 명령을 실행하지 않는다.
"""

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import led_controller as LC
from led_controller import LedController, State

# 상태 -> led_controller 의 어느 색 상수를 쓰는가.
# LISTENING/CAPTURING 이 한 색을 쓰고 MUTE/ERROR 도 한 색을 쓴다 — 일부러 그렇게
# 뒀다(같은 뜻의 화면이므로). 화면에도 이 사실을 표시한다.
STATE_COLOR = {
    "LISTENING": "C_CYAN",
    "CAPTURING": "C_CYAN",
    "THINKING": "C_PURPLE",
    "SPEAKING": "C_WARM",
    "MUTE": "C_RED",
    "ERROR": "C_RED",
    "ACK": "C_GREEN",
    "BOOT": "C_WHITE",
}

SHARED_NOTE = {
    "C_CYAN": "LISTENING 과 CAPTURING 이 같이 쓴다",
    "C_RED": "MUTE 와 ERROR 가 같이 쓴다",
}

#: 조절 가능한 속도/모양 값 (이름, 라벨, 최소, 최대, 단계)
PARAMS = [
    ("THINKING_SPEED", "생각중 흐름 속도 (칸/초)", 0.4, 8.0, 0.1),
    ("THINKING_TAIL", "생각중 꼬리 길이", 0.6, 3.0, 0.05),
    ("SPEAKING_HZ", "말하는중 파동 속도 (회/초)", 0.3, 5.0, 0.1),
    ("SPEAKING_SPREAD", "말하는중 퍼짐 지연", 0.0, 0.5, 0.01),
    ("MUTE_LEVEL", "마이크꺼짐 밝기", 0.02, 0.6, 0.01),
]

TRANSIENTS = ("BOOT", "ACK", "ERROR")


def _to_hex(rgb):
    # 렌더러(_emit)와 같은 양자화를 써야 한다. round() 는 은행가 반올림이라
    # 0.70*255=178.5 가 178 이 되어 견본 색과 한 칸씩 어긋난다.
    return "#%02X%02X%02X" % tuple(
        int(max(0.0, min(1.0, c)) * 255 + 0.5) for c in rgb)


def _from_hex(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class Picker:
    """LED 를 소유하고, 브라우저가 던지는 값을 반영한다."""

    def __init__(self, brightness=0.45, fps=50):
        self.led = LedController(brightness=brightness, fps=fps)
        self.state = "LISTENING"
        self.level = 0.6
        self.auto_level = True
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pump = None

    def start(self):
        self.led.start()
        self.led.set_phase(State.LISTENING)
        self._pump = threading.Thread(target=self._run, name="picker", daemon=True)
        self._pump.start()
        return self

    def stop(self):
        self._stop.set()
        if self._pump is not None:
            self._pump.join(timeout=1.5)
        self.led.stop()

    def _run(self):
        """일회성 상태를 반복 재생하고, 음성 크기를 계속 밀어 넣는다."""
        t0 = time.perf_counter()
        next_pulse = 0.0
        while not self._stop.is_set():
            now = time.perf_counter()
            with self._lock:
                state, lvl, auto = self.state, self.level, self.auto_level

            if state in TRANSIENTS:
                # 일회성은 한 번 보여주고 사라지므로, 고르는 동안 계속 다시 띄운다
                if now >= next_pulse:
                    self.led.pulse(State[state])
                    next_pulse = now + LC.TRANSIENTS[State[state]] + 0.45
            elif state in ("CAPTURING", "SPEAKING"):
                self.led.set_level(
                    LC._fake_level(now - t0, state == "SPEAKING") if auto else lvl)

            if self.led.failure is not None:
                break
            self._stop.wait(0.02)

    # --- 브라우저가 부르는 것들 ---------------------------------------------
    def snapshot(self):
        with self._lock:
            state, level, auto = self.state, self.level, self.auto_level
        return {
            "state": state,
            "level": level,
            "auto_level": auto,
            "brightness": self.led.brightness,
            "colors": {name: _to_hex(getattr(LC, name))
                       for name in set(STATE_COLOR.values())},
            "params": {name: getattr(LC, name) for name, *_ in PARAMS},
            "state_color": STATE_COLOR,
            "shared": SHARED_NOTE,
            "param_defs": [{"name": n, "label": l, "min": lo, "max": hi, "step": st}
                           for n, l, lo, hi, st in PARAMS],
            "states": list(STATE_COLOR.keys()),
            "transients": list(TRANSIENTS),
            "failure": None if self.led.failure is None else repr(self.led.failure),
        }

    def apply(self, body):
        if "state" in body:
            name = str(body["state"])
            if name not in STATE_COLOR:
                raise ValueError(f"모르는 상태: {name}")
            with self._lock:
                self.state = name
            # MUTE 는 플래그, 일회성은 pump 가 반복 재생, 나머지는 phase
            self.led.set_muted(name == "MUTE")
            if name in State.__members__ and State[name] in LC.PHASES:
                self.led.set_phase(State[name])

        if "brightness" in body:
            self.led.brightness = float(body["brightness"])

        if "level" in body:
            with self._lock:
                self.level = max(0.0, min(1.0, float(body["level"])))

        if "auto_level" in body:
            with self._lock:
                self.auto_level = bool(body["auto_level"])

        for key, value in (body.get("colors") or {}).items():
            if key not in STATE_COLOR.values():
                raise ValueError(f"모르는 색 이름: {key}")
            setattr(LC, key, _from_hex(str(value)))

        for key, value in (body.get("params") or {}).items():
            spec = next((p for p in PARAMS if p[0] == key), None)
            if spec is None:
                raise ValueError(f"모르는 값 이름: {key}")
            _, _, lo, hi, _ = spec
            setattr(LC, key, max(lo, min(hi, float(value))))

        # 정지 화면이어도 즉시 다시 그리게 한다
        self.led.brightness = self.led.brightness
        return self.snapshot()

    def export(self):
        lines = ["# led_controller.py 에 붙여넣기", ""]
        for name in ("C_CYAN", "C_PURPLE", "C_WARM", "C_RED", "C_GREEN", "C_WHITE"):
            r, g, b = getattr(LC, name)
            note = SHARED_NOTE.get(name, "")
            comment = f"# {_to_hex(getattr(LC, name))}"
            if note:
                comment += f" — {note}"
            lines.append(f"{name} = ({r:.2f}, {g:.2f}, {b:.2f})".ljust(34) + comment)
        lines.append("")
        for name, label, *_ in PARAMS:
            lines.append(f"{name} = {getattr(LC, name):g}".ljust(34) + f"# {label}")
        lines.append("")
        lines.append(f"# 밝기 기본값: LedController(brightness={self.led.brightness:.2f})")
        lines.append("#")
        lines.append("# led_test.py 에도 같은 팔레트가 복사되어 있다. 양쪽을 같이 고쳐야")
        lines.append("# 드리프트 경고가 나지 않는다.")
        return "\n".join(lines)


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>LED 색 고르기</title>
<style>
:root{color-scheme:dark;--bg:#14161a;--card:#1d2026;--line:#2c3038;--fg:#e8eaed;
      --dim:#9aa0a8;--accent:#6ea8ff;--pad:16px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif;
     padding:var(--pad);padding-top:calc(var(--pad) + env(safe-area-inset-top,0px));
     padding-bottom:calc(var(--pad) + env(safe-area-inset-bottom,0px))}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:16px;margin-bottom:14px}
.card h2{font-size:14px;margin:0 0 12px;color:var(--dim);font-weight:600;
         letter-spacing:.03em;text-transform:uppercase}
.states{display:flex;flex-wrap:wrap;gap:8px}
.states button{background:#252932;color:var(--fg);border:1px solid var(--line);
       border-radius:8px;padding:8px 13px;font:inherit;font-size:13px;cursor:pointer}
.states button:hover{border-color:#3d434e}
.states button[aria-pressed=true]{background:var(--accent);color:#0b1220;
       border-color:var(--accent);font-weight:600}
.swatches{display:grid;grid-template-columns:repeat(auto-fill,minmax(64px,1fr));gap:9px}
.swatch{aspect-ratio:1.5;border-radius:9px;border:2px solid transparent;cursor:pointer;
        padding:0;position:relative}
.swatch:hover{border-color:#5a6172}
.swatch[aria-pressed=true]{border-color:#fff;box-shadow:0 0 0 2px #0008 inset}
.row{display:flex;align-items:center;gap:12px;margin:11px 0}
.row label{width:190px;flex:none;color:var(--dim);font-size:13px}
.row input[type=range]{flex:1;min-width:90px;accent-color:var(--accent)}
.row .val{width:56px;flex:none;text-align:right;font-variant-numeric:tabular-nums;
          font-size:13px}
.pick{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.pick input[type=color]{width:64px;height:44px;padding:0;border:1px solid var(--line);
          border-radius:8px;background:none;cursor:pointer}
.hex{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:15px;
     letter-spacing:.05em}
.note{color:var(--dim);font-size:12px;margin-top:9px}
.warn{color:#ffb86b}
pre{background:#0e1015;border:1px solid var(--line);border-radius:9px;padding:14px;
    overflow-x:auto;font-size:12.5px;margin:0;white-space:pre}
.btn{background:#252932;color:var(--fg);border:1px solid var(--line);border-radius:8px;
     padding:9px 15px;font:inherit;font-size:13px;cursor:pointer}
.btn:hover{border-color:#3d434e}
.err{background:#3a1c1c;border-color:#7a3030;color:#ffb0b0}
@media(max-width:560px){.row label{width:100%}.row{flex-wrap:wrap}}
</style>
<div class="wrap">
<h1>LED 색 고르기</h1>
<p class="sub">색을 찍으면 보드의 LED 가 바로 바뀐다. 마음에 들면 아래에서 코드로 내보내라.</p>

<div id="err" class="card err" hidden></div>

<div class="card">
  <h2>어떤 상태를 볼까</h2>
  <div class="states" id="states"></div>
  <div class="note" id="stateNote"></div>
</div>

<div class="card">
  <h2>색</h2>
  <div class="swatches" id="swatches"></div>
  <div class="pick" style="margin-top:14px">
    <input type="color" id="color">
    <span class="hex" id="hex"></span>
    <span class="note" id="shared"></span>
  </div>
  <div class="row"><label>빨강 R</label><input type="range" id="r" min="0" max="255"><span class="val" id="rv"></span></div>
  <div class="row"><label>초록 G</label><input type="range" id="g" min="0" max="255"><span class="val" id="gv"></span></div>
  <div class="row"><label>파랑 B</label><input type="range" id="b" min="0" max="255"><span class="val" id="bv"></span></div>
</div>

<div class="card">
  <h2>밝기와 움직임</h2>
  <div class="row"><label>전체 밝기</label><input type="range" id="brightness" min="0.02" max="1" step="0.01"><span class="val" id="brightnessv"></span></div>
  <div id="params"></div>
</div>

<div class="card" id="levelCard">
  <h2>음성 크기</h2>
  <div class="row">
    <label><input type="checkbox" id="auto"> 자동으로 흔들기</label>
    <input type="range" id="level" min="0" max="1" step="0.01"><span class="val" id="levelv"></span>
  </div>
  <div class="note">CAPTURING 과 SPEAKING 만 이 값을 탄다.</div>
</div>

<div class="card">
  <h2>코드로 내보내기</h2>
  <button class="btn" id="exportBtn">지금 값을 코드로</button>
  <button class="btn" id="copyBtn" hidden>복사</button>
  <pre id="export" hidden></pre>
</div>
</div>

<script>
let S = null, timer = null;

const $ = id => document.getElementById(id);

function post(body){
  clearTimeout(timer);
  timer = setTimeout(async () => {
    try{
      const res = await fetch('/api/apply', {method:'POST', body: JSON.stringify(body)});
      if(!res.ok) throw new Error(await res.text());
      S = await res.json();
      if(S.failure) showErr('그리기 스레드가 죽었다: ' + S.failure);
    }catch(e){ showErr(String(e)); }
  }, 30);
}
function showErr(msg){ const e=$('err'); e.textContent = msg; e.hidden = false; }

const SWATCHES = {
  C_CYAN:   ['#00B3FF','#0078FF','#00FFC8','#2850FF','#50DCFF','#00E0E0'],
  C_PURPLE: ['#9900FF','#7A00FF','#C000FF','#5500FF','#FF00E0','#8A6AFF'],
  C_WARM:   ['#FFA84F','#FFFFFF','#FFD9A0','#FF8C00','#FFE4B5','#FFC864'],
  C_RED:    ['#FF0000','#FF3232','#FF5000','#E00000','#FF0040','#FF6060'],
  C_GREEN:  ['#00FF3D','#00FF00','#32FF80','#7FFF00','#00E060','#40FFB0'],
  C_WHITE:  ['#FFFFFF','#E0E8FF','#FFF0D0','#D0E0FF','#FFE8C0','#C8D8F0'],
};

function colorKey(){ return S.state_color[S.state]; }

function render(){
  // 상태 버튼
  const sc = $('states');
  if(!sc.children.length){
    S.states.forEach(name => {
      const b = document.createElement('button');
      b.textContent = name;
      b.onclick = () => { S.state = name; post({state:name}); render(); };
      sc.appendChild(b);
    });
  }
  [...sc.children].forEach(b =>
    b.setAttribute('aria-pressed', b.textContent === S.state));

  $('stateNote').textContent = S.transients.includes(S.state)
    ? '일회성 표시라 고르는 동안 반복 재생된다.'
    : '';

  const key = colorKey(), hex = S.colors[key];

  // 견본
  const sw = $('swatches');
  sw.innerHTML = '';
  (SWATCHES[key] || []).forEach(c => {
    const b = document.createElement('button');
    b.className = 'swatch';
    b.style.background = c;
    b.title = c;
    b.setAttribute('aria-pressed', c.toUpperCase() === hex.toUpperCase());
    b.onclick = () => { S.colors[key] = c; post({colors:{[key]:c}}); render(); };
    sw.appendChild(b);
  });

  $('color').value = hex;
  $('hex').textContent = hex;
  $('shared').textContent = S.shared[key] ? '※ ' + S.shared[key] : '';
  const n = i => parseInt(hex.slice(1+i*2, 3+i*2), 16);
  ['r','g','b'].forEach((id,i) => { $(id).value = n(i); $(id+'v').textContent = n(i); });

  $('brightness').value = S.brightness;
  $('brightnessv').textContent = Number(S.brightness).toFixed(2);

  // 속도 값들
  const pc = $('params');
  if(!pc.children.length){
    S.param_defs.forEach(p => {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `<label>${p.label}</label>
        <input type="range" id="p_${p.name}" min="${p.min}" max="${p.max}" step="${p.step}">
        <span class="val" id="p_${p.name}v"></span>`;
      pc.appendChild(row);
      row.querySelector('input').oninput = e => {
        const v = parseFloat(e.target.value);
        S.params[p.name] = v;
        $(`p_${p.name}v`).textContent = v.toFixed(2);
        post({params:{[p.name]: v}});
      };
    });
  }
  S.param_defs.forEach(p => {
    $(`p_${p.name}`).value = S.params[p.name];
    $(`p_${p.name}v`).textContent = Number(S.params[p.name]).toFixed(2);
  });

  $('auto').checked = S.auto_level;
  $('level').value = S.level;
  $('level').disabled = S.auto_level;
  $('levelv').textContent = Number(S.level).toFixed(2);
  $('levelCard').style.opacity =
    (S.state === 'CAPTURING' || S.state === 'SPEAKING') ? '1' : '.45';
}

function setHex(hex){
  const key = colorKey();
  S.colors[key] = hex.toUpperCase();
  post({colors:{[key]: hex}});
  render();
}

$('color').oninput = e => setHex(e.target.value);
['r','g','b'].forEach(id => $(id).oninput = () => {
  const h = '#' + ['r','g','b']
    .map(x => parseInt($(x).value).toString(16).padStart(2,'0')).join('');
  setHex(h);
});
$('brightness').oninput = e => {
  S.brightness = parseFloat(e.target.value);
  $('brightnessv').textContent = S.brightness.toFixed(2);
  post({brightness: S.brightness});
};
$('auto').onchange = e => {
  S.auto_level = e.target.checked; post({auto_level: S.auto_level}); render();
};
$('level').oninput = e => {
  S.level = parseFloat(e.target.value);
  $('levelv').textContent = S.level.toFixed(2);
  post({level: S.level});
};
$('exportBtn').onclick = async () => {
  const t = await (await fetch('/api/export')).text();
  $('export').textContent = t; $('export').hidden = false; $('copyBtn').hidden = false;
};
$('copyBtn').onclick = () => {
  navigator.clipboard.writeText($('export').textContent)
    .then(() => { $('copyBtn').textContent = '복사됨';
                  setTimeout(() => $('copyBtn').textContent = '복사', 1200); })
    .catch(() => showErr('복사 실패 — 직접 선택해서 복사해라'));
};

fetch('/api/state').then(r => r.json()).then(s => { S = s; render(); })
  .catch(e => showErr('서버에 연결하지 못했다: ' + e));
</script>
"""


class Handler(BaseHTTPRequestHandler):
    picker = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                   # 요청마다 찍으면 시끄럽다

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, json.dumps(self.picker.snapshot()))
        elif self.path == "/api/export":
            self._send(200, self.picker.export(), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/apply":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 64 * 1024:
                raise ValueError("요청이 너무 크다")
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, json.dumps(self.picker.apply(body)))
        except Exception as e:
            self._send(400, f"{type(e).__name__}: {e}", "text/plain; charset=utf-8")


def main():
    ap = argparse.ArgumentParser(
        description="LED 색·속도를 눈으로 보며 고르는 웹 도구 (root 필요)")
    ap.add_argument("-p", "--port", type=int, default=8777)
    ap.add_argument("-b", "--brightness", type=float, default=0.45)
    ap.add_argument("--fps", type=int, default=50)
    args = ap.parse_args()

    try:
        picker = Picker(brightness=args.brightness, fps=args.fps).start()
    except PermissionError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"오류: LED 를 잡지 못했다 — {e}", file=sys.stderr)
        print("다른 LED 프로그램이 떠 있는지 확인해라.", file=sys.stderr)
        return 1

    picker.led.install_signal_handlers()
    Handler.picker = picker

    # 127.0.0.1 에만 묶는다 — 이 서버는 root 로 돈다
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"오류: 포트 {args.port} 를 열지 못했다 — {e}", file=sys.stderr)
        picker.stop()
        return 1
    httpd.daemon_threads = True

    url = f"http://127.0.0.1:{args.port}"
    print(f"이 기기의 브라우저에서 열어라:  {url}")
    print("(외부에서는 접속되지 않는다. 끝내려면 Ctrl-C)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        picker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
