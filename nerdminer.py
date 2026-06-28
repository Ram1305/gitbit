#!/usr/bin/env python3
"""
NerdMiner — PNG sprite-sheet cat mining animation for SSD1305 128x32 OLED

Full animation loop:
  Phase 0  MINING    — cat swings pickaxe at diamond LEFT  |  chest RIGHT
  Phase 1  RUNNING   — cat dashes RIGHT carrying diamond   |  chest RIGHT
  Phase 2  DEPOSIT   — wallet interaction frames 1-5
  Phase 3  HAPPY     — wallet idle chest + burst sparkles
  Phase 4  STATS     — BTC price / wallet balance
  Phase 5  BLOCK!    — blocks found counter (0)
  Phase 6  BTC PRICE — live BTC/USD price
  Phase 7  HASHRATE  — hash rate + progress bar
"""
import sys, os, time, json, urllib.request, threading
import http.server, socket, urllib.parse, subprocess, shutil
from PIL import Image, ImageDraw, ImageFont

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
IMG_DIR     = os.path.join(BASE_DIR, 'images')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

# Load persisted settings; fall back to built-in defaults
_DEFAULT_ADDR = "bc1qc7mjrxwqr4a6rdnmsa3gwp7n9vweg0n5yc24z8"
try:
    with open(CONFIG_FILE) as _f:
        _saved = json.load(_f)
except Exception:
    _saved = {}
BTC_ADDRESS = _saved.get('btc_address', _DEFAULT_ADDR)

# ── Image filenames (spaces in names) ─────────────────────────────────────────
F_WALK   = 'walking frames.png'             # 7 frames
F_MINE   = 'mining frames.png'              # 4 frames
F_CARRY  = 'carying diamond frames.png'     # 6 frames
F_WALLET = 'wallet ineteraction frames.png' # 6 frames  32x32 each (wllet.png spec)
F_ICONS  = 'icon and assets.png'            # 5 icons: diamond wallet pickaxe sparkle cat

# ── Display ────────────────────────────────────────────────────────────────────
SIMULATION_MODE = False
try:
    sys.path.append(os.path.join(BASE_DIR, 'drive'))
    from drive import SSD1305
    disp = SSD1305.SSD1305()
    disp.Init()
    disp.clear()
    WIDTH, HEIGHT = disp.width, disp.height
except Exception as e:
    SIMULATION_MODE = True
    WIDTH, HEIGHT = 128, 32
    print(f"Simulation mode: {e}")

try:
    font    = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 8)
    font_b  = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 8)
    font_lg = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 16)
except Exception:
    font = font_b = font_lg = ImageFont.load_default()

# ── Simulator imports (window created after PHASES so NFRAMES = len(PHASES)) ──
if SIMULATION_MODE:
    import tkinter as tk
    from PIL import ImageTk
    SCALE = 5
else:
    def show_frame(img, slot):
        disp.getbuffer(img)
        disp.ShowImage()

    def update_info(_): pass
    def pump(): pass


# ── Sprite loaders ─────────────────────────────────────────────────────────────

def _crop_scale(raw_L, target_h):
    """Auto-crop black bg → scale to target_h with NEAREST → threshold to 1-bit."""
    mask = raw_L.point(lambda p: 255 if p > 40 else 0)
    bbox = mask.getbbox()
    if bbox:
        raw_L = raw_L.crop(bbox)
    cw, ch = raw_L.size
    nw     = max(1, round(cw * target_h / ch))
    scaled = raw_L.resize((nw, target_h), Image.NEAREST)
    return scaled.point(lambda p: 255 if p > 127 else 0, '1')


def load_sheet(filename, n_frames, target_h):
    """
    Slice a horizontal sprite sheet into n_frames equal columns.
    Each frame: auto-crop → scale to target_h → 1-bit.
    Returns list of PIL '1'-mode images.
    """
    path  = os.path.join(IMG_DIR, filename)
    sheet = Image.open(path).convert('L')
    sw, sh = sheet.size
    fw = sw // n_frames
    return [_crop_scale(sheet.crop((i * fw, 0, (i + 1) * fw, sh)), target_h)
            for i in range(n_frames)]


# ── Sprite dimensions ──────────────────────────────────────────────────────────
GROUND_Y = HEIGHT - 4   # y=28 — dotted ground line
CAT_H    = GROUND_Y     # cat fills y=0 → y=28 (auto-cropped sprite scales to this)
GEM_H    = 12           # diamond icon
CHEST_H  = 18           # chest icon display height

# ── Load all sprites ───────────────────────────────────────────────────────────
WALK_FRAMES   = load_sheet(F_WALK,   7, CAT_H)
# Flip horizontally so cat faces LEFT toward the diamond (confirmed by functionframe.png)
MINE_FRAMES   = [f.transpose(Image.FLIP_LEFT_RIGHT) for f in load_sheet(F_MINE, 4, CAT_H)]
CARRY_FRAMES  = load_sheet(F_CARRY,  6, CAT_H)
WALLET_FRAMES = load_sheet(F_WALLET, 6, CAT_H)  # frames 0-5: deposit anim + idle chest

_icons   = load_sheet(F_ICONS, 5, GEM_H)
GEM_ICON  = _icons[0]                            # slot 0 = diamond
PICK_ICON = _icons[2]                            # slot 2 = pickaxe

# CHEST_ICON: last wallet frame (frame 6 = IDLE CHEST) scaled to CHEST_H.
# This is the best-looking chest — used as static icon in phases 0-2 and 4.
CHEST_ICON = load_sheet(F_WALLET, 6, CHEST_H)[5]


# ── Layout ─────────────────────────────────────────────────────────────────────
GEM_X    =  2
GEM_Y    = GROUND_Y - GEM_H
WALL_X   = WIDTH  - CHEST_ICON.size[0] - 2
WALL_Y   = GROUND_Y - CHEST_ICON.size[1]
CAT_Y    = 0
CAT_HOME = 34   # cat x during mining/idle


# ── Render helpers ─────────────────────────────────────────────────────────────

def blit(canvas, sprite, x, y):
    """Copy white pixels from 1-bit sprite onto canvas."""
    sw, sh = sprite.size
    px     = sprite.convert('L').load()
    draw   = ImageDraw.Draw(canvas)
    for row in range(sh):
        for col in range(sw):
            if px[col, row] > 127:
                cx, cy = x + col, y + row
                if 0 <= cx < WIDTH and 0 <= cy < HEIGHT:
                    draw.point((cx, cy), fill=255)


def sparkle(draw, cx, cy, t, size=2):
    """Animated ✦: cross on even ticks, diagonals on odd. Animates with t."""
    if t % 4 in (0, 2):
        draw.line([(cx - size, cy), (cx + size, cy)], fill=255)
        draw.line([(cx, cy - size), (cx, cy + size)], fill=255)
    else:
        s = max(1, size - 1)
        for dx, dy in ((-s, -s), (s, -s), (-s, s), (s, s)):
            draw.point((cx + dx, cy + dy), fill=255)


def draw_ground(draw):
    """Dotted ground line — matches oled_example.png."""
    for x in range(0, WIDTH, 3):
        draw.point((x, GROUND_Y), fill=255)


def chest_idle(img, draw, t):
    """
    Static chest icon + two animated sparkles above it.
    Used in phases 0, 1, 2 — matches wllet.png HOW IT LOOKS panel.
    """
    blit(img, CHEST_ICON, WALL_X, WALL_Y)
    hw = CHEST_ICON.size[0]
    sparkle(draw, WALL_X - 2,      WALL_Y - 4, t,     size=2)
    sparkle(draw, WALL_X + hw + 1, WALL_Y - 5, t + 2, size=2)


def _new():
    img = Image.new('1', (WIDTH, HEIGHT), 0)
    return img, ImageDraw.Draw(img)


# ── Network data (background thread) ──────────────────────────────────────────
_lock       = threading.Lock()
_btc_price  = "..."
_wallet_bal = None
_hash_rate  = None           # kH/s — updated by _fetch_loop


def _fetch_loop():
    global _btc_price, _wallet_bal, _hash_rate
    import math, random
    while True:
        try:
            r = urllib.request.urlopen(
                "https://mempool.space/api/v1/prices", timeout=5)
            d = json.loads(r.read().decode())
            with _lock:
                _btc_price = f"${d['USD']:,.0f}"
        except Exception:
            pass
        try:
            r = urllib.request.urlopen(
                f"https://mempool.space/api/address/{BTC_ADDRESS}", timeout=6)
            d = json.loads(r.read().decode())
            s = d['chain_stats']
            with _lock:
                _wallet_bal = (s['funded_txo_sum'] - s['spent_txo_sum']) / 1e8
        except Exception:
            pass
        # Simulate hash rate (replace with real miner API if available)
        base = 54.0
        noise = math.sin(time.time() / 30) * 8 + random.uniform(-3, 3)
        with _lock:
            _hash_rate = max(1.0, base + noise)
        time.sleep(60)


threading.Thread(target=_fetch_loop, daemon=True).start()


# ── Web config portal ──────────────────────────────────────────────────────────
CONFIG_PORT = 8080

def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

_HTML_PAGE = """\
<!DOCTYPE html>
<html>
<head>
<title>NerdMiner Setup</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:monospace;background:#0a0a0a;color:#e0e0e0;padding:24px;max-width:480px;margin:auto}}
h1{{color:#f7931a;font-size:22px;margin-bottom:18px}}
h2{{color:#aaa;font-size:13px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}}
.card{{border:1px solid #2a2a2a;border-radius:8px;padding:18px;margin-bottom:16px;background:#111}}
label{{color:#777;font-size:11px;display:block;margin-bottom:4px;margin-top:10px}}
input{{background:#1a1a1a;color:#fff;border:1px solid #333;padding:10px;width:100%;border-radius:5px;font-size:13px}}
input:focus{{outline:none;border-color:#f7931a}}
.btn{{background:#f7931a;color:#000;border:none;padding:13px;width:100%;font-size:15px;font-weight:bold;border-radius:6px;cursor:pointer;margin-top:8px}}
.btn:hover{{background:#e8860f}}
.ok{{background:#0f2a0f;border:1px solid #2a6a2a;color:#4caf50;padding:12px;border-radius:6px;margin-bottom:16px}}
.ip{{color:#444;font-size:11px;text-align:center;margin-top:20px}}
</style>
</head>
<body>
<h1>⛏ NerdMiner Setup</h1>
{msg}
<form method="POST" action="/save">
  <div class="card">
    <h2>₿ Bitcoin</h2>
    <label>Wallet Address</label>
    <input type="text" name="btc_address" value="{btc_address}" placeholder="bc1q..." required>
  </div>
  <div class="card">
    <h2>📶 Wi-Fi</h2>
    <label>Network Name (SSID)</label>
    <input type="text" name="wifi_ssid" value="{wifi_ssid}" placeholder="MyHomeWiFi">
    <label>Password</label>
    <input type="password" name="wifi_password" placeholder="Leave blank to keep current">
  </div>
  <button class="btn" type="submit">💾  Save &amp; Apply</button>
</form>
<p class="ip">http://{ip}:{port}</p>
</body>
</html>"""

def _save_cfg(btc_addr, wifi_ssid):
    try:
        existing = {}
        try:
            with open(CONFIG_FILE) as f:
                existing = json.load(f)
        except Exception:
            pass
        existing['btc_address'] = btc_addr
        if wifi_ssid:
            existing['wifi_ssid'] = wifi_ssid
        with open(CONFIG_FILE, 'w') as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass

def _apply_wifi(ssid, password):
    if not ssid:
        return ''
    if shutil.which('nmcli'):
        try:
            cmd = ['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid]
            if password:
                cmd += ['password', password]
            subprocess.run(cmd, timeout=15, check=True,
                           capture_output=True)
            return f'Connected to {ssid}'
        except Exception as e:
            return f'nmcli error: {e}'
    # Fallback: append to wpa_supplicant.conf
    try:
        entry = (f'\nnetwork={{\n'
                 f'    ssid="{ssid}"\n'
                 f'    psk="{password}"\n'
                 f'}}\n')
        with open('/etc/wpa_supplicant/wpa_supplicant.conf', 'a') as f:
            f.write(entry)
        subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'],
                       timeout=10, check=False, capture_output=True)
        return f'Added {ssid} — reboot to apply'
    except Exception as e:
        return f'wifi error: {e}'


class _CfgHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _html(self, msg=''):
        cfg = {}
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            pass
        return _HTML_PAGE.format(
            msg=msg,
            btc_address=cfg.get('btc_address', BTC_ADDRESS),
            wifi_ssid=cfg.get('wifi_ssid', ''),
            ip=_get_local_ip(), port=CONFIG_PORT)

    def _send(self, body, code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(self._html())

    def do_POST(self):
        global BTC_ADDRESS
        if self.path != '/save':
            self._send('not found', 404); return
        raw  = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        data = dict(urllib.parse.parse_qsl(raw.decode()))
        if data.get('btc_address'):
            BTC_ADDRESS = data['btc_address'].strip()
        wifi_msg = _apply_wifi(data.get('wifi_ssid','').strip(),
                               data.get('wifi_password','').strip())
        _save_cfg(BTC_ADDRESS, data.get('wifi_ssid','').strip())
        extra = f' &nbsp;·&nbsp; {wifi_msg}' if wifi_msg else ''
        self._send(self._html(
            f'<div class="ok">✔ Saved!{extra}</div>'))


def _start_portal():
    try:
        srv = http.server.HTTPServer(('0.0.0.0', CONFIG_PORT), _CfgHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Config portal → http://{_get_local_ip()}:{CONFIG_PORT}")
    except Exception as e:
        print(f"Config portal unavailable: {e}")

_start_portal()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE RENDER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Phase 0 — MINING ──────────────────────────────────────────────────────────
# functionframe.png steps 1-3: cat swings pickaxe at diamond on left
# mining frames: [0] back-swing  [1] forward+sparks  [2] raise  [3] collect+glow

def render_intro(t):
    img, draw = _new()

    # ═══ TOP HALF: white block, black content (inverted badge) ════════════════
    draw.rectangle([(0, 0), (WIDTH - 1, 15)], fill=255)

    # Measure "BITCOIN" to centre coin + text as a group
    try:
        bw = int(draw.textlength("BITCOIN", font=font_lg))
    except Exception:
        bw = 70
    cr       = 7                            # coin radius
    coin_d   = cr * 2                       # = 14
    gap      = 4
    total_w  = coin_d + gap + bw
    start_x  = max(2, (WIDTH - total_w) // 2)
    ccx      = start_x + cr                 # coin centre x

    # Bitcoin coin — black outline on white background
    draw.ellipse([(ccx - cr, 1), (ccx + cr, 15)], outline=0)
    draw.text((ccx - 3, 4), "B", font=font_b, fill=0)
    # Two tick marks → ₿ look
    for tx in (ccx - 1, ccx + 1):
        draw.line([(tx, 1),  (tx, 3) ], fill=0)
        draw.line([(tx, 12), (tx, 14)], fill=0)

    # "BITCOIN" in black to the right of the coin
    draw.text((start_x + coin_d + gap, 0), "BITCOIN", font=font_lg, fill=0)

    # Animated star top-right corner (black on white)
    if (t // 3) % 2 == 0:
        draw.line([(WIDTH - 7, 7), (WIDTH - 3, 7)], fill=0)
        draw.line([(WIDTH - 5, 5), (WIDTH - 5, 9)], fill=0)
    else:
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            draw.point((WIDTH - 5 + dx, 7 + dy), fill=0)

    # ═══ BOTTOM HALF: black bg, white content ═════════════════════════════════
    # Hard divider already visible as edge of white block; add a second line
    draw.line([(0, 16), (WIDTH - 1, 16)], fill=255)

    # "Merch" centred in white
    try:
        mw = int(draw.textlength("Merch", font=font_lg))
    except Exception:
        mw = 46
    mx = max(2, (WIDTH - mw) // 2)
    draw.text((mx, 17), "Merch", font=font_lg, fill=255)

    # Flanking sparkles either side of "Merch"
    sparkle(draw, max(3,         mx - 6),      25, t,     size=2)
    sparkle(draw, min(WIDTH - 4, mx + mw + 5), 25, t + 2, size=2)

    return img


def render_mining(t):
    img, draw = _new()
    draw_ground(draw)
    fi = (t // 5) % len(MINE_FRAMES)
    blit(img, MINE_FRAMES[fi], CAT_HOME, CAT_Y)
    if fi in (1, 3):                               # impact / collect frames
        # Cat faces LEFT — sparks appear between cat's left edge and diamond
        sx = CAT_HOME - 4
        for dx, dy in ((0, 0), (-2, -1), (1, -2), (-1, 1), (-3, -2)):
            if 0 <= sx + dx < WIDTH:
                draw.point((sx + dx, GEM_Y + dy), fill=255)
    blit(img, GEM_ICON, GEM_X, GEM_Y)
    chest_idle(img, draw, t)
    return img


# ── Phase 1 — PICKUP ──────────────────────────────────────────────────────────
# functionframe.png step 4: diamond slides left→right toward cat

def render_pickup(t, dur):
    img, draw = _new()
    draw_ground(draw)
    blit(img, WALK_FRAMES[0], CAT_HOME, CAT_Y)    # static cat, no walk cycle
    prog = t / max(dur - 1, 1)
    gx   = int(GEM_X + (CAT_HOME - 2 - GEM_X) * prog)
    blit(img, GEM_ICON, gx, GEM_Y)
    chest_idle(img, draw, t)
    return img


# ── Phase 2 — RUNNING ─────────────────────────────────────────────────────────
# functionframe.png step 5: cat dashes right carrying diamond; speed lines trail

def render_run(t, dur):
    img, draw = _new()
    draw_ground(draw)
    fi    = (t // 3) % len(CARRY_FRAMES)
    prog  = t / max(dur - 1, 1)
    dest  = WALL_X - CARRY_FRAMES[fi].size[0] - 2
    cat_x = int(CAT_HOME + (dest - CAT_HOME) * prog)
    blit(img, CARRY_FRAMES[fi], cat_x, CAT_Y)
    for i in range(3):
        lx = cat_x - 5 - i * 5
        if lx > GEM_X + 12:
            ly = CAT_Y + 10 + i * 4
            draw.line([(lx, ly), (lx - 4, ly)], fill=255)
    chest_idle(img, draw, t)
    return img


# ── Phase 3 — DEPOSIT ─────────────────────────────────────────────────────────
# wallet interaction frames per wllet.png spec:
#   WALLET_FRAMES[0] OPENING  → [1] PICK UP → [2] DEPOSIT 1
#   WALLET_FRAMES[3] DEPOSIT 2 → [4] CLOSE   (frames 0-4 = cat+chest combined)
#   WALLET_FRAMES[5] IDLE CHEST (used in phase 4)
# Gem flies in from left during first half; sparkles build up after.

def render_deposit(t, dur):
    img, draw = _new()
    draw_ground(draw)
    fi   = min(t * 5 // max(dur, 1), 4)           # step through frames 0→4
    sw   = WALLET_FRAMES[fi].size[0]
    blit(img, WALLET_FRAMES[fi], WALL_X - sw, CAT_Y)
    if t > dur // 3:
        sparkle(draw, WALL_X + 5,  WALL_Y + 3,  t,     size=2)
    if t > dur // 2:
        sparkle(draw, WALL_X + 11, WALL_Y + 9,  t + 2, size=2)
        sparkle(draw, WALL_X - 2,  WALL_Y + 5,  t + 1, size=2)
    return img


# ── Phase 4 — HAPPY ───────────────────────────────────────────────────────────
# wllet.png spec & phase 4 panel: WALLET_FRAMES[5] = IDLE CHEST (frame 6)
# Chest surrounded by burst sparkles at varied sizes and offsets.

def render_happy(t):
    img, draw = _new()
    draw_ground(draw)
    # Same CHEST_ICON (IDLE CHEST frame) used in phases 0-2 — burst sparkles around it
    blit(img, CHEST_ICON, WALL_X, WALL_Y)
    hw = CHEST_ICON.size[0]
    sparkle(draw, WALL_X - 6,        WALL_Y - 4, t,     size=3)
    sparkle(draw, WALL_X + hw + 5,   WALL_Y - 6, t + 1, size=3)
    sparkle(draw, WALL_X + hw // 2,  WALL_Y - 8, t + 2, size=2)
    sparkle(draw, WALL_X - 4,        WALL_Y + 10, t + 3, size=2)
    sparkle(draw, WALL_X + hw + 3,   WALL_Y + 9,  t,    size=2)
    return img


# ── Phase 5 — STATS ───────────────────────────────────────────────────────────
# dashboard_example.png: gem icon | vertical divider | BTC/USD price + balance | dots

def render_stats(t):
    img, draw = _new()
    with _lock:
        price, bal = _btc_price, _wallet_bal
    blit(img, GEM_ICON, 1, (HEIGHT - GEM_H) // 2)
    sep_x = GEM_ICON.size[0] + 3
    draw.line([(sep_x, 0), (sep_x, HEIGHT - 1)], fill=255)
    ox = sep_x + 3
    draw.text((ox,  0), "BTC/USD", font=font_b, fill=255)
    draw.text((ox, 10), price,     font=font_b, fill=255)
    bal_s = f"{bal:.6f} BTC" if bal is not None else "syncing..."
    draw.text((ox, 22), bal_s, font=font, fill=255)
    for i in range(3):                             # animated alive-dots top-right
        if (t // 6) % 4 == i:
            draw.point((WIDTH - 6 + i * 2, 2), fill=255)
    return img


def render_block_found(t):
    img, draw = _new()
    # Flashing border: full box ↔ top+bottom lines
    if (t // 3) % 2 == 0:
        draw.rectangle([(0, 0), (WIDTH - 1, HEIGHT - 1)], outline=255)
    else:
        draw.line([(0, 0),         (WIDTH - 1, 0)        ], fill=255)
        draw.line([(0, HEIGHT - 1),(WIDTH - 1, HEIGHT - 1)], fill=255)
    # size=1 sparkles stay within 3px of each corner — no overlap with text
    sparkle(draw,  3,         3,          t,     size=1)
    sparkle(draw,  WIDTH - 4, 3,          t + 2, size=1)
    sparkle(draw,  3,         HEIGHT - 4, t + 1, size=1)
    sparkle(draw,  WIDTH - 4, HEIGHT - 4, t + 3, size=1)
    # Thin separator divides heading from stats
    draw.line([(0, 10), (WIDTH - 1, 10)], fill=255)
    # All text at x=8 — clear of the 3px corner sparkles on both sides
    draw.text(( 8,  1), "BLOCK FOUND!", font=font_b, fill=255)
    draw.text(( 8, 12), "Blocks:  0",   font=font_b, fill=255)

    return img


def render_btc_price(t):
    img, draw = _new()
    with _lock:
        price = _btc_price

    # ── Bitcoin coin logo (left) ───────────────────────────────────────────────
    cx, cy, r = 14, 16, 12
    # Outer ring — always visible
    draw.ellipse([(cx - r,     cy - r),     (cx + r,     cy + r)    ], outline=255)
    # Inner ring pulses — gives a "glowing coin" effect
    if (t // 4) % 2 == 0:
        draw.ellipse([(cx - r + 2, cy - r + 2), (cx + r - 2, cy + r - 2)], outline=255)
    # "B" centred in the circle
    bx, by = cx - 3, cy - 4
    draw.text((bx, by), "B", font=font_b, fill=255)
    # Two tick marks above and below the B → makes it look like ₿
    for tx in (bx + 1, bx + 3):
        draw.line([(tx, by - 2), (tx, by)     ], fill=255)
        draw.line([(tx, by + 8), (tx, by + 10)], fill=255)

    # ── Vertical separator ─────────────────────────────────────────────────────
    sep_x = cx + r + 2
    draw.line([(sep_x, 0), (sep_x, HEIGHT - 1)], fill=255)
    ox = sep_x + 3

    # ── Text (right side) ──────────────────────────────────────────────────────
    draw.text((ox,  0), "BTC / USD", font=font_b,  fill=255)
    draw.text((ox, 11), price,       font=font_lg, fill=255)   # large 16px price

    # ── Sparkle top-right corner ───────────────────────────────────────────────
    sparkle(draw, WIDTH - 5, 4, t, size=2)

    # ── Alive dots bottom ──────────────────────────────────────────────────────
    for i in range(3):
        if (t // 6) % 4 == i:
            draw.point((ox + i * 4, HEIGHT - 2), fill=255)

    return img


def render_hashrate(t):
    img, draw = _new()
    with _lock:
        hr = _hash_rate
    # Pickaxe icon on left
    blit(img, PICK_ICON, 1, (HEIGHT - PICK_ICON.size[1]) // 2)
    sep_x = PICK_ICON.size[0] + 3
    draw.line([(sep_x, 0), (sep_x, HEIGHT - 1)], fill=255)
    ox = sep_x + 3
    # Header
    draw.text((ox,  0), "HASH RATE", font=font_b, fill=255)
    # Rate value
    if hr is not None:
        if hr >= 1000:
            rate_s = f"{hr / 1000:.2f} MH/s"
        else:
            rate_s = f"{hr:.1f} kH/s"
    else:
        rate_s = "-- kH/s"
    draw.text((ox, 11), rate_s, font=font_b, fill=255)
    # Animated progress bar showing relative rate (max ~150 kH/s)
    bar_y  = 23
    bar_x0 = ox
    bar_x1 = WIDTH - 4
    bar_w  = bar_x1 - bar_x0
    draw.rectangle([(bar_x0, bar_y), (bar_x1, bar_y + 5)], outline=255)
    if hr is not None:
        fill_w = max(1, int(bar_w * min(hr / 150.0, 1.0)))
        # Animated tick fills bar 1px at a time (bounces up/down)
        disp_w = min(fill_w, bar_x0 + fill_w)
        draw.rectangle([(bar_x0 + 1, bar_y + 1),
                         (bar_x0 + disp_w - 1, bar_y + 4)], fill=255)
    # Alive dots top-right
    for i in range(3):
        if (t // 6) % 4 == i:
            draw.point((WIDTH - 6 + i * 2, 2), fill=255)
    return img


# ── Phase table ────────────────────────────────────────────────────────────────
PHASES = [
    ("intro  ", render_intro,       40),   # splash — shown on first boot
    ("mining ", render_mining,      30),
    # ("pickup ", render_pickup,    20),
    ("running", render_run,         25),
    ("deposit", render_deposit,     25),
    ("happy  ", render_happy,       30),
    ("stats  ", render_stats,       30),
    ("block! ", render_block_found, 30),
    ("btcprice", render_btc_price,  30),
    ("hashrate", render_hashrate,   30),
]

# ── Simulator window — single 128x32 screen, mirrors the real OLED ─────────────
if SIMULATION_MODE:
    _tk_active = False
    _current_photo = [None]
    try:
        root = tk.Tk()
        root.title("NerdMiner OLED  [128x32 @ 5x]")
        root.configure(bg="#111")
        tk.Label(root, text="◉  NerdMiner OLED Simulator",
                 fg="#aaa", bg="#111", font=("Courier", 10, "bold")).pack(
                 anchor="w", padx=14, pady=(10, 2))
        canvas = tk.Canvas(root, width=WIDTH * SCALE, height=HEIGHT * SCALE,
                           bg="black", highlightthickness=2,
                           highlightbackground="#444")
        canvas.pack(padx=14, pady=(0, 4))
        lbl_info = tk.Label(root, text="", fg="#555", bg="#111",
                            font=("Courier", 8))
        lbl_info.pack(anchor="w", padx=14, pady=(0, 10))
        _tk_active = True
    except Exception as _te:
        print(f"[headless] no display ({_te}) — running without GUI")

    if _tk_active:
        def show_frame(img, slot):
            ph = ImageTk.PhotoImage(
                img.resize((WIDTH * SCALE, HEIGHT * SCALE), Image.NEAREST))
            _current_photo[0] = ph
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=ph)

        def update_info(text):
            lbl_info.config(text=text)

        def pump():
            try:
                root.update()
            except tk.TclError:
                sys.exit(0)
    else:
        def show_frame(img, slot): pass
        def update_info(text):
            if text:
                print(text, flush=True)
        def pump(): pass

# ── Main loop ──────────────────────────────────────────────────────────────────
print("NerdMiner OLED starting...")
phase, tick = 0, 0

while True:
    label, fn, dur = PHASES[phase]
    img = fn(tick, dur) if fn in (render_run, render_deposit) else fn(tick)

    # Push current frame — hardware: OLED; simulation: single window
    show_frame(img, phase)

    if SIMULATION_MODE:
        with _lock:
            p, b = _btc_price, _wallet_bal
        bal_s = f"{b:.6f} BTC" if b is not None else "fetching..."
        update_info(
            f"phase {phase} ({label.strip()})  t={tick:02d}/{dur-1:02d}"
            f"   BTC {p}   wallet {bal_s}"
        )
        pump()

    tick += 1
    if tick >= dur:
        tick  = 0
        phase = (phase + 1) % len(PHASES)

    time.sleep(0.1)
