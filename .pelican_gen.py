# -*- coding: utf-8 -*-
"""生成 pelican-bike.svg —— 鹈鹕骑自行车 2D 动画（纯 SVG + SMIL，无 JS / 无外部依赖）。

几何全部由脚本计算：
  * 曲柄圆周 -> 踏板位置 -> 两段腿逆运动学（IK），保证脚掌始终精确踩在踏板上；
  * 车轮转速 = 曲柄转速 x 传动比；路面虚线的滚动速度 = 轮周长 / 轮周期（物理自洽）；
  * 视差各层按越近越快的规律给出周期，且位移量恰等于图案周期 -> 循环无跳变。
"""
import math
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- 基本几何
W, H = 900.0, 540.0
GROUND = 430.0                      # 车轮着地线（= 沥青路面上沿）
RW = 62.0                           # 车轮半径
RHUB = (300.0, GROUND - RW)         # 后轮心 (300, 368)
FHUB = (560.0, GROUND - RW)         # 前轮心 (560, 368)
BB = (430.0, 372.0)                 # 五通（中轴）
CRANK = 34.0                        # 曲柄长度 = 踏板圆半径
HIP_N, HIP_F = (396.0, 244.0), (388.0, 236.0)   # 近侧 / 远侧髋关节
L1, L2 = 70.0, 110.0                # 大腿 / 跗跖长度

CRANK_DUR = 2.4                     # 曲柄一圈 2.4 s（约 25 rpm，卡通可读性优先）
RATIO = 2.6                         # 传动比（牙盘 / 飞轮）
WHEEL_DUR = CRANK_DUR / RATIO       # 车轮一圈 0.8 s
N = 16                              # 每圈关键帧数
BOB_DUR = CRANK_DUR / 2.0           # 一蹬一颠（每圈两次）

BODY_C, BODY_RX, BODY_RY, BODY_ROT = (378.0, 196.0), 62.0, 56.0, -10.0

GROUND_SPEED = 2 * math.pi * RW / WHEEL_DUR     # 487 px/s

# 调色板
C_OUT = "#2b3240"       # 主轮廓
C_FRAME, C_FRAME_D = "#e14b4b", "#8f2f2f"
C_TIRE = "#2b3240"
C_BODY, C_BODY_L = "#fdfdf8", "#d3d8dd"
C_BILL, C_BILL_D = "#f59a2e", "#d97b17"
C_POUCH, C_POUCH_D = "#ffcf6e", "#e8a93f"
C_LEG, C_LEG_D = "#f2953f", "#b96a24"
C_LEG_F, C_LEG_FD = "#cf7f33", "#9c5f22"


def f(v, nd=2):
    s = f"{v:.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


# ---------------------------------------------------------------- 运动学
def ik(hip, pedal):
    """两段腿 IK。返回 (大腿绝对角, 小腿相对角, 脚掌配平角, 膝点, 髋到踏板距离)。"""
    dx, dy = pedal[0] - hip[0], pedal[1] - hip[1]
    d0 = math.hypot(dx, dy)
    ux, uy = dx / d0, dy / d0
    d = min(max(d0, abs(L1 - L2) + 1e-6), L1 + L2 - 1e-6)
    a = (L1 * L1 - L2 * L2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, L1 * L1 - a * a))
    bx, by = hip[0] + a * ux, hip[1] + a * uy
    k1 = (bx - h * uy, by + h * ux)
    k2 = (bx + h * uy, by - h * ux)
    knee = k1 if k1[0] <= k2[0] else k2          # 膝朝后（x 更小）
    a1 = math.degrees(math.atan2(knee[1] - hip[1], knee[0] - hip[0]))
    a2 = math.degrees(math.atan2(pedal[1] - knee[1], pedal[0] - knee[0])) - a1
    return a1, a2, -(a1 + a2), knee, d0


def pedal_at(phase_deg):
    r = math.radians(phase_deg)
    return (BB[0] + CRANK * math.cos(r), BB[1] + CRANK * math.sin(r))


def leg_frames(hip, phase_deg=0.0):
    a1s, a2s, a3s, reach = [], [], [], []
    for i in range(N + 1):
        ph = phase_deg + 360.0 * i / N
        a1, a2, a3, _knee, d0 = ik(hip, pedal_at(ph))
        a1s.append(a1); a2s.append(a2); a3s.append(a3); reach.append(d0)
    return a1s, a2s, a3s, reach


def vals(seq, nd=2):
    return ";".join(f(v, nd) for v in seq)


def times(n):
    return ";".join(f(i / n, 4) for i in range(n + 1))


A1N, A2N, A3N, REACH_N = leg_frames(HIP_N, 0.0)
A1F, A2F, A3F, REACH_F = leg_frames(HIP_F, 180.0)
KT = times(N)


def self_check():
    for name, reach, hip, ph in (("near", REACH_N, HIP_N, 0.0),
                                 ("far", REACH_F, HIP_F, 180.0)):
        span = L1 + L2
        for i, d in enumerate(reach):
            assert d <= span, f"{name} 第{i}帧腿长不足: 需 {d:.2f} > {span}"
            assert d >= abs(L1 - L2), f"{name} 第{i}帧腿过长: {d:.2f}"
        worst = 0.0
        for i in range(N + 1):
            a1 = math.radians(A1N[i] if name == "near" else A1F[i])
            a2 = math.radians(A2N[i] if name == "near" else A2F[i])
            kx, ky = hip[0] + L1 * math.cos(a1), hip[1] + L1 * math.sin(a1)
            fx = kx + L2 * math.cos(a1 + a2)
            fy = ky + L2 * math.sin(a1 + a2)
            p = pedal_at(ph + 360.0 * i / N)
            worst = max(worst, math.hypot(fx - p[0], fy - p[1]))
        assert worst < 0.05, f"{name} 脚掌未踩在踏板上，最大偏差 {worst:.3f}px"
        print(f"  [ok] {name}腿 伸展 {min(reach):.1f}~{max(reach):.1f}px / 全长 {span:.0f}"
              f"，踏板拟合偏差 {worst:.4f}px")
    for name, hip in (("near", HIP_N), ("far", HIP_F)):
        for dy in (0.0, -3.5, 3.5):
            px, py = hip[0] - BODY_C[0], hip[1] + dy - BODY_C[1]
            t = math.radians(-BODY_ROT)
            rx = px * math.cos(t) - py * math.sin(t)
            ry = px * math.sin(t) + py * math.cos(t)
            e = (rx / BODY_RX) ** 2 + (ry / BODY_RY) ** 2
            assert e < 1.0, f"{name} 髋关节跑到身体外 (e={e:.3f})"
        print(f"  [ok] {name}髋关节在身体轮廓内（含颠簸幅度）")
    print(f"  [ok] 曲柄 {CRANK_DUR}s / 车轮 {WHEEL_DUR:.3f}s（比 {RATIO}）"
          f"→ 地面 {GROUND_SPEED:.0f} px/s")


# ---------------------------------------------------------------- 绘制部件
def rot_anim(v, dur, keytimes=None, begin=None):
    kt = f' keyTimes="{keytimes}"' if keytimes else ""
    bg = f' begin="{begin}"' if begin else ""
    return (f'<animateTransform attributeName="transform" type="rotate" values="{v}" '
            f'dur="{f(dur,3)}s"{kt}{bg} repeatCount="indefinite"/>')


def spin(cx, cy, dur, a0=0.0):
    return (f'<animateTransform attributeName="transform" type="rotate" '
            f'from="{f(a0)} {f(cx)} {f(cy)}" to="{f(a0 + 360)} {f(cx)} {f(cy)}" '
            f'dur="{f(dur,3)}s" repeatCount="indefinite"/>')


def wheel(cx, cy):
    inner = RW - 13
    spokes = "".join(
        f'<line x1="{f(cx)}" y1="{f(cy - inner)}" x2="{f(cx)}" y2="{f(cy + inner)}" '
        f'transform="rotate({f(180.0 * k / 8)} {f(cx)} {f(cy)})"/>' for k in range(8))
    return f'''  <g>
    <circle cx="{f(cx)}" cy="{f(cy)}" r="{f(RW)}" fill="none" stroke="{C_TIRE}" stroke-width="9"/>
    <g>
      {spin(cx, cy, WHEEL_DUR)}
      <circle cx="{f(cx)}" cy="{f(cy)}" r="{f(RW - 1.5)}" fill="none" stroke="#4c5566" stroke-width="8" stroke-dasharray="11 13"/>
      <g stroke="#c9d1da" stroke-width="2.2" stroke-linecap="round">{spokes}</g>
    </g>
    <circle cx="{f(cx)}" cy="{f(cy)}" r="{f(RW - 9)}" fill="none" stroke="#cfd7df" stroke-width="5"/>
    <circle cx="{f(cx)}" cy="{f(cy)}" r="6.5" fill="#8b94a3" stroke="{C_OUT}" stroke-width="2"/>
  </g>'''


def pedal(px, py, color, outline):
    return (f'<line x1="{f(BB[0])}" y1="{f(BB[1])}" x2="{f(px)}" y2="{f(py)}" '
            f'stroke="{outline}" stroke-width="14" stroke-linecap="round"/>'
            f'<line x1="{f(BB[0])}" y1="{f(BB[1])}" x2="{f(px)}" y2="{f(py)}" '
            f'stroke="{color}" stroke-width="10" stroke-linecap="round"/>'
            f'<rect x="{f(px - 16)}" y="{f(py - 4.5)}" width="32" height="9" rx="3" '
            f'fill="{C_OUT}"/>')


def crank_far():
    px, py = BB[0] + CRANK, BB[1]
    return f'''  <g>
    {spin(BB[0], BB[1], CRANK_DUR, 180.0)}
    {pedal(px, py, "#7c828e", "#4b5160")}
  </g>'''


def crank_near():
    px, py = BB[0] + CRANK, BB[1]
    return f'''  <g>
    {spin(BB[0], BB[1], CRANK_DUR)}
    <circle cx="{f(BB[0])}" cy="{f(BB[1])}" r="30" fill="none" stroke="{C_OUT}" stroke-width="5"/>
    <circle cx="{f(BB[0])}" cy="{f(BB[1])}" r="27.5" fill="none" stroke="#9aa3b1" stroke-width="5" stroke-dasharray="6 5"/>
    <circle cx="{f(BB[0])}" cy="{f(BB[1])}" r="12" fill="#8b94a3" stroke="{C_OUT}" stroke-width="2.5"/>
    {pedal(px, py, "#c3c9d3", C_OUT)}
  </g>'''


def leg(hip, a1s, a2s, a3s, color, outline):
    return f'''  <g transform="translate({f(hip[0])},{f(hip[1])})">
    <g>
      {rot_anim(vals(a1s), CRANK_DUR, KT)}
      <path d="M0,0 L{f(L1)},0" stroke="{outline}" stroke-width="17" stroke-linecap="round" fill="none"/>
      <path d="M0,0 L{f(L1)},0" stroke="{color}" stroke-width="12" stroke-linecap="round" fill="none"/>
      <g transform="translate({f(L1)},0)">
        <g>
          {rot_anim(vals(a2s), CRANK_DUR, KT)}
          <path d="M0,0 L{f(L2)},0" stroke="{outline}" stroke-width="13" stroke-linecap="round" fill="none"/>
          <path d="M0,0 L{f(L2)},0" stroke="{color}" stroke-width="8.5" stroke-linecap="round" fill="none"/>
          <g transform="translate({f(L2)},0)">
            <g>
              {rot_anim(vals(a3s), CRANK_DUR, KT)}
              <path d="M-9,-2 C 1,-8 14,-8 23,0 C 16,7 3,10 -9,7 Z"
                    fill="{color}" stroke="{outline}" stroke-width="2.4" stroke-linejoin="round"/>
              <path d="M2,-6 L5,6 M12,-7 L13,5" stroke="{outline}" stroke-width="1.6" fill="none" opacity="0.75"/>
            </g>
          </g>
        </g>
      </g>
    </g>
  </g>'''


def hill(base, amp, period, phase=0.0, color="#a9d3a0", close=GROUND, step=20.0):
    pts, x = [], -period
    while x <= W + period:
        y = base - amp * (0.5 + 0.5 * math.sin(2 * math.pi * x / period + phase))
        pts.append((x, y))
        x += step
    d = "M " + " L ".join(f"{f(px,1)},{f(py,1)}" for px, py in pts)
    d += f" L {f(W + period,1)},{f(close,1)} L {f(-period,1)},{f(close,1)} Z"
    return d, pts


def ridge_y(pts, x):
    best = min(pts, key=lambda p: abs(p[0] - x))
    return best[1]


def tree(x, y, s=1.0):
    return (f'<rect x="{f(x - 4 * s)}" y="{f(y - 26 * s)}" width="{f(8 * s)}" height="{f(30 * s)}" '
            f'rx="3" fill="#7d5a3c"/>'
            f'<circle cx="{f(x)}" cy="{f(y - 40 * s)}" r="{f(22 * s)}" fill="#5da45f"/>'
            f'<circle cx="{f(x - 15 * s)}" cy="{f(y - 26 * s)}" r="{f(15 * s)}" fill="#6cb56b"/>'
            f'<circle cx="{f(x + 15 * s)}" cy="{f(y - 27 * s)}" r="{f(14 * s)}" fill="#6cb56b"/>'
            f'<circle cx="{f(x)}" cy="{f(y - 54 * s)}" r="{f(13 * s)}" fill="#7cc47a"/>')


def cloud(x, y, s, dur, begin):
    return f'''  <g opacity="0.95">
    <animateTransform attributeName="transform" type="translate" from="0 0" to="-1500 0" dur="{f(dur)}s" begin="{begin}" repeatCount="indefinite"/>
    <g transform="translate({f(x)},{f(y)}) scale({f(s)})" fill="#ffffff">
      <ellipse cx="0" cy="0" rx="46" ry="20"/>
      <ellipse cx="-30" cy="6" rx="30" ry="14"/>
      <ellipse cx="30" cy="5" rx="34" ry="15"/>
      <ellipse cx="-6" cy="-14" rx="28" ry="17"/>
    </g>
  </g>'''


def flowers(period, dur, y, seed=0):
    """路边小花带，按 period 重复，位移 = period -> 无缝。"""
    out = []
    for k in range(-1, 5):
        for j, (dx, col) in enumerate(((0, "#ff8fb1"), (period / 2, "#ffd166"),
                                       (period * 0.72, "#c9a7ff"))):
            x = k * period + dx
            out.append(f'<g transform="translate({f(x)},{f(y)})">'
                       f'<rect x="-1.5" y="0" width="3" height="12" fill="#5aa348"/>'
                       f'<circle cx="0" cy="-6" r="4.2" fill="{col}"/>'
                       f'<circle cx="-5" cy="-2" r="3.4" fill="{col}" opacity="0.85"/>'
                       f'<circle cx="5" cy="-2" r="3.4" fill="{col}" opacity="0.85"/></g>')
    return (f'  <g>\n    <animateTransform attributeName="transform" type="translate" '
            f'from="0 0" to="-{f(period)} 0" dur="{f(dur,3)}s" repeatCount="indefinite"/>\n    '
            + "".join(out) + "\n  </g>")


def streak(y, x0, x1, dur, begin, op, w=5, col="#ffffff"):
    return (f'  <line x1="{f(x0)}" y1="{f(y)}" x2="{f(x1)}" y2="{f(y)}" stroke="{col}" '
            f'stroke-width="{f(w)}" stroke-linecap="round" opacity="{f(op)}">'
            f'<animate attributeName="x1" values="{f(x0)};{f(x0 - 260)}" dur="{f(dur)}s" begin="{begin}" repeatCount="indefinite"/>'
            f'<animate attributeName="x2" values="{f(x1)};{f(x1 - 260)}" dur="{f(dur)}s" begin="{begin}" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="{f(op)};0" dur="{f(dur)}s" begin="{begin}" repeatCount="indefinite"/>'
            f'</line>')


# ---------------------------------------------------------------- 组装
def build():
    far_hill_d, _ = hill(base=352, amp=56, period=380, phase=0.4, color="#b7dcae", close=424)
    mid_hill_d, mid_pts = hill(base=406, amp=26, period=260, phase=1.1, color="#8ecb84", close=GROUND)

    trees = "".join(tree(k * 260 + 70, ridge_y(mid_pts, k * 260 + 70), 1.0) for k in range(-1, 5))
    bushes = "".join(
        f'<ellipse cx="{f(k * 260 + 190)}" cy="{f(ridge_y(mid_pts, k * 260 + 190) - 6)}" rx="26" ry="14" fill="#6cb56b"/>'
        for k in range(-1, 5))

    dash_period = 130.0
    dash_dur = dash_period / GROUND_SPEED
    flower_period, flower_dur = 240.0, 240.0 / (GROUND_SPEED * 1.02)
    peb_period, peb_dur = 150.0, 150.0 / (GROUND_SPEED * 1.75)

    pebbles = "".join(
        f'<ellipse cx="{f(k * peb_period + j * 43)}" cy="{f(498 + (j % 3) * 14)}" rx="{f(7 + j % 3 * 2)}" ry="{f(3 + (j % 2))}" fill="#5c6572" opacity="0.55"/>'
        for k in range(-1, 8) for j in range(3))

    sg = (f'<g>\n    <animateTransform attributeName="transform" type="translate" from="0 0" '
          f'to="-{f(peb_period)} 0" dur="{f(peb_dur,3)}s" repeatCount="indefinite"/>\n    {pebbles}\n  </g>')

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" width="{f(W)}" height="{f(H)}"
     role="img" aria-labelledby="pelicanTitle pelicanDesc">
  <title id="pelicanTitle">骑自行车的鹈鹕</title>
  <desc id="pelicanDesc">一只戴头盔的鹈鹕骑着红色自行车向前疾驰：车轮与曲柄转动，双腿精确踩踏板，
  路面与远山按视差滚动，喉囊随颠簸轻颤。纯 SVG + SMIL 动画。</desc>

  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#6fc0ea"/>
      <stop offset="0.55" stop-color="#b9e2f6"/>
      <stop offset="1" stop-color="#e8f6fd"/>
    </linearGradient>
    <radialGradient id="sunGlow">
      <stop offset="0" stop-color="#fff6c4" stop-opacity="0.95"/>
      <stop offset="0.45" stop-color="#ffe680" stop-opacity="0.35"/>
      <stop offset="1" stop-color="#ffe680" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="road" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#77808e"/>
      <stop offset="1" stop-color="#5f6875"/>
    </linearGradient>
    <clipPath id="scene"><rect x="0" y="0" width="{f(W)}" height="{f(H)}"/></clipPath>
    <clipPath id="bodyClip">
      <ellipse cx="{f(BODY_C[0])}" cy="{f(BODY_C[1])}" rx="{f(BODY_RX)}" ry="{f(BODY_RY)}"
               transform="rotate({f(BODY_ROT)} {f(BODY_C[0])} {f(BODY_C[1])})"/>
    </clipPath>
  </defs>

  <g clip-path="url(#scene)">
    <rect width="{f(W)}" height="{f(H)}" fill="url(#sky)"/>

    <!-- 太阳 -->
    <circle cx="782" cy="80" r="96" fill="url(#sunGlow)"/>
    <circle cx="782" cy="80" r="44" fill="#ffe066">
      <animate attributeName="r" values="44;46.5;44" dur="4.5s" repeatCount="indefinite"/>
    </circle>

    <!-- 云（向左漂移，起止点都在画面外 -> 循环无缝） -->
{cloud(1180, 74, 1.0, 26, "0s")}
{cloud(1240, 152, 0.8, 32, "-8s")}
{cloud(1100, 168, 0.62, 29, "-17s")}
{cloud(1320, 96, 1.2, 37, "-24s")}

    <!-- 远处飞鸟 -->
    <g stroke="#5c7a91" stroke-width="2.6" fill="none" opacity="0.5" stroke-linecap="round">
      <g><animateTransform attributeName="transform" type="translate" from="0 0" to="-1200 0" dur="40s" begin="-4s" repeatCount="indefinite"/>
        <path d="M300,120 q7,-6 14,0 q7,-6 14,0"/>
        <path d="M352,102 q6,-5 12,0 q6,-5 12,0"/>
        <path d="M330,142 q5,-4 10,0 q5,-4 10,0"/></g>
    </g>

    <!-- 远山 -->
    <g>
      <animateTransform attributeName="transform" type="translate" from="0 0" to="-380 0" dur="17s" repeatCount="indefinite"/>
      <path d="{far_hill_d}" fill="#b7dcae"/>
    </g>

    <!-- 中景丘陵 + 树 -->
    <g>
      <animateTransform attributeName="transform" type="translate" from="0 0" to="-260 0" dur="4.33s" repeatCount="indefinite"/>
      <path d="{mid_hill_d}" fill="#8ecb84"/>
      {bushes}
      {trees}
    </g>

    <!-- 草地边带 -->
    <rect x="0" y="420" width="{f(W)}" height="12" fill="#9ad46f"/>
    <rect x="0" y="420" width="{f(W)}" height="4" fill="#b6e58c"/>
{flowers(flower_period, flower_dur, 420)}

    <!-- 路面 -->
    <rect x="0" y="{f(GROUND)}" width="{f(W)}" height="{f(H - GROUND)}" fill="url(#road)"/>
    <rect x="0" y="{f(GROUND)}" width="{f(W)}" height="5" fill="#57606d"/>
    <line x1="0" y1="492" x2="{f(W)}" y2="492" stroke="#eef3f7" stroke-width="7"
          stroke-dasharray="{f(dash_period * 0.5)} {f(dash_period * 0.5)}" opacity="0.9">
      <animate attributeName="stroke-dashoffset" from="0" to="-{f(dash_period)}" dur="{f(dash_dur,3)}s" repeatCount="indefinite"/>
    </line>
{sg}

    <!-- 车影 -->
    <ellipse cx="430" cy="434" rx="206" ry="11" fill="#2b3240" opacity="0.16">
      <animate attributeName="rx" values="206;198;206" dur="{f(BOB_DUR,3)}s" repeatCount="indefinite"/>
    </ellipse>

    <!-- ===== 远侧：曲柄 -> 腿 ===== -->
{crank_far()}
{leg(HIP_F, A1F, A2F, A3F, C_LEG_F, C_LEG_FD)}

    <!-- ===== 车架 ===== -->
{wheel(RHUB[0], RHUB[1])}
    <g fill="none" stroke-linecap="round">
      <g stroke="{C_FRAME_D}" stroke-width="15">
        <path d="M{f(BB[0])},{f(BB[1])} L398,272"/>
        <path d="M{f(BB[0])},{f(BB[1])} L562,318"/>
        <path d="M398,272 L552,266"/>
        <path d="M{f(BB[0])},{f(BB[1])} L{f(RHUB[0])},{f(RHUB[1])}"/>
        <path d="M398,272 L{f(RHUB[0])},{f(RHUB[1])}"/>
      </g>
      <g stroke="{C_FRAME}" stroke-width="11">
        <path d="M{f(BB[0])},{f(BB[1])} L398,272"/>
        <path d="M{f(BB[0])},{f(BB[1])} L562,318"/>
        <path d="M398,272 L552,266"/>
        <path d="M{f(BB[0])},{f(BB[1])} L{f(RHUB[0])},{f(RHUB[1])}"/>
        <path d="M398,272 L{f(RHUB[0])},{f(RHUB[1])}"/>
      </g>
      <!-- 座管 / 车座 -->
      <path d="M398,272 L390,250" stroke="{C_OUT}" stroke-width="11"/>
      <path d="M398,272 L390,250" stroke="#4a5260" stroke-width="8"/>
      <!-- 头管 + 前叉 -->
      <path d="M552,266 L562,318" stroke="#2b3240" stroke-width="14"/>
      <path d="M560,316 L{f(FHUB[0])},{f(FHUB[1])}" stroke="#2b3240" stroke-width="10"/>
      <path d="M560,316 L{f(FHUB[0])},{f(FHUB[1])}" stroke="#3a4150" stroke-width="7"/>
    </g>
    <path d="M366,250 C 372,242 392,238 408,244 C 413,247 414,251 409,254 C 394,259 375,258 366,250 Z"
          fill="#4a3f35" stroke="#2f2a24" stroke-width="2"/>

    <!-- 传动链（真外切线） -->
{chain()}

    <!-- ===== 前轮 + 车把 ===== -->
{wheel(FHUB[0], FHUB[1])}
    <path d="M552,266 L557,248 Q 559,240 549,238 L516,236" stroke="{C_OUT}" stroke-width="13"
          fill="none" stroke-linecap="round"/>
    <path d="M552,266 L557,248 Q 559,240 549,238 L516,236" stroke="#3a4150" stroke-width="9"
          fill="none" stroke-linecap="round"/>
    <line x1="524" y1="237" x2="512" y2="236" stroke="#20262f" stroke-width="13" stroke-linecap="round"/>

    <!-- ===== 近侧：曲柄 -> 腿 ===== -->
{crank_near()}
{leg(HIP_N, A1N, A2N, A3N, C_LEG, C_LEG_D)}

    <!-- ===== 鹈鹕 ===== -->
    <g>
      <animateTransform attributeName="transform" type="translate" values="0 0;0 -3.5;0 0"
                        dur="{f(BOB_DUR,3)}s" repeatCount="indefinite"/>

      <!-- 尾羽（摆动） -->
      <g transform="translate(330,206)">
        <g>
          <animateTransform attributeName="transform" type="rotate" values="-4;5;-4" dur="{f(BOB_DUR,3)}s" repeatCount="indefinite"/>
          <path d="M-6,4 C -40,-6 -68,-26 -78,-40 C -58,-38 -24,-26 -2,-10 Z"
                fill="#eff2f5" stroke="#d3d8dd" stroke-width="2.2" stroke-linejoin="round"/>
          <path d="M-14,-2 L-58,-28 M-10,2 L-50,-20" stroke="#d3d8dd" stroke-width="2" fill="none"/>
        </g>
      </g>

      <!-- 远侧翅膀（被身体压住，仅露边缘） -->
      <path d="M368,150 C 336,130 304,140 294,166 C 316,180 350,184 376,176 Z"
            fill="#dfe5ec" stroke="#c3cbd4" stroke-width="2.2" stroke-linejoin="round"/>
      <path d="M330,150 C 312,148 300,156 296,166 M348,160 C 330,160 314,166 306,172"
            stroke="#c3cbd4" stroke-width="2" fill="none"/>

      <!-- 身体 -->
      <ellipse cx="{f(BODY_C[0])}" cy="{f(BODY_C[1])}" rx="{f(BODY_RX)}" ry="{f(BODY_RY)}"
               transform="rotate({f(BODY_ROT)} {f(BODY_C[0])} {f(BODY_C[1])})"
               fill="{C_BODY}" stroke="{C_BODY_L}" stroke-width="2.5"/>
      <g clip-path="url(#bodyClip)">
        <ellipse cx="372" cy="222" rx="52" ry="40" fill="#e9eef2" opacity="0.8"/>
        <path d="M320,150 C 352,158 400,166 440,206 C 430,220 400,200 360,186 C 336,176 320,164 320,150 Z"
              fill="#e2e8ee" opacity="0.55"/>
      </g>

      <!-- 脖子 -->
      <path d="M388,158 C 384,124 406,112 448,118" stroke="{C_BODY_L}" stroke-width="35" fill="none" stroke-linecap="round"/>
      <path d="M388,158 C 384,124 406,112 448,118" stroke="{C_BODY}" stroke-width="30" fill="none" stroke-linecap="round"/>
      <path d="M392,152 C 390,128 406,118 436,120" stroke="#e9eef2" stroke-width="7" fill="none" stroke-linecap="round" opacity="0.9"/>

      <!-- 头 + 头盔 -->
      <circle cx="462" cy="104" r="24" fill="{C_BODY}" stroke="{C_BODY_L}" stroke-width="2.5"/>
      <path d="M437,102 C 433,78 452,72 470,74 C 486,76 492,88 490,102 Z"
            fill="#e14b4b" stroke="#a83232" stroke-width="2.4" stroke-linejoin="round"/>
      <path d="M441,84 C 452,76 470,76 482,84" stroke="#f4887f" stroke-width="3" fill="none" stroke-linecap="round"/>
      <path d="M444,112 C 448,126 462,132 476,126" stroke="#a83232" stroke-width="3" fill="none" stroke-linecap="round"/>

      <!-- 上喙 -->
      <path d="M474,90 C 520,88 572,100 608,116 C 612,118 612,124 606,126 C 568,122 518,116 472,112 Z"
            fill="{C_BILL}" stroke="{C_BILL_D}" stroke-width="2.4" stroke-linejoin="round"/>
      <path d="M486,96 C 528,96 570,106 600,119" stroke="{C_BILL_D}" stroke-width="1.8" fill="none" opacity="0.6"/>

      <!-- 喉囊（随颠簸轻颤） -->
      <g transform="translate(478,106)">
        <g>
          <animateTransform attributeName="transform" type="rotate" values="0;3.6;0;-2.4;0" dur="{f(BOB_DUR,3)}s" repeatCount="indefinite"/>
          <path d="M0,0 C 22,42 60,60 98,46 C 118,38 128,26 128,18 C 88,26 42,14 0,0 Z"
                fill="{C_POUCH}" stroke="{C_POUCH_D}" stroke-width="2.4" stroke-linejoin="round"/>
          <path d="M10,8 C 34,40 66,52 96,42" stroke="{C_POUCH_D}" stroke-width="1.8" fill="none" opacity="0.7"/>
        </g>
      </g>

      <!-- 眼睛 -->
      <circle cx="470" cy="101" r="5.8" fill="#ffffff" stroke="{C_OUT}" stroke-width="1.6"/>
      <circle cx="471.6" cy="101.5" r="2.9" fill="#2b2b2b"/>
      <circle cx="470.4" cy="100.2" r="1.1" fill="#ffffff"/>

      <!-- 近侧翅膀：搭在车把上 -->
      <g transform="translate(402,168)">
        <g>
          <animateTransform attributeName="transform" type="rotate" values="-1.8;1.8;-1.8" dur="{f(BOB_DUR,3)}s" repeatCount="indefinite"/>
          <path d="M0,-2 C 46,2 84,30 110,64 L 115,82 C 78,76 40,52 -4,24 Z"
                fill="#f6f8fa" stroke="{C_BODY_L}" stroke-width="2.4" stroke-linejoin="round"/>
          <path d="M22,12 C 54,24 82,46 102,70 M14,6 C 48,16 76,38 96,62"
                stroke="#dde3e9" stroke-width="2.4" fill="none"/>
          <path d="M96,62 L 115,82 C 100,80 84,74 74,68 Z" fill="#c9d2db" stroke="#b3bec9" stroke-width="1.8" stroke-linejoin="round"/>
        </g>
      </g>
    </g>

    <!-- 速度线（前景） -->
{streak(196, 250, 330, 0.55, "0s", 0.42, 4)}
{streak(268, 120, 210, 0.7, "-0.2s", 0.34, 5)}
{streak(352, 640, 740, 0.5, "-0.35s", 0.4, 4)}
{streak(150, 700, 780, 0.62, "-0.1s", 0.3, 3.5)}
{streak(300, 40, 120, 0.48, "-0.28s", 0.3, 4)}
  </g>
</svg>
'''
    return svg


def chain(rA=27.5, rB=11.0, pitch=12.0):
    """牙盘(r=27.5@BB) 与 飞轮(r=11@RHUB) 的外公切线。

    链条线速度 = 牙盘角速度 x 牙盘半径 = (2*pi/CRANK_DUR)*rA，
    虚线周期 pitch 对应的滚动时长 = pitch / v —— 链节运动与曲柄严格同步。
    """
    A, B = BB, RHUB
    dx, dy = B[0] - A[0], B[1] - A[1]
    d = math.hypot(dx, dy)
    ux, uy = dx / d, dy / d
    c = (rA - rB) / d
    s = math.sqrt(max(0.0, 1 - c * c))
    v_chain = 2 * math.pi * rA / CRANK_DUR
    dur = pitch / v_chain
    out = []
    for sign, direction in ((1, -1), (-1, 1)):      # sign=+1 上段；链条上段向前、下段向后
        nx, ny = c * ux - sign * s * uy, c * uy + sign * s * ux
        p1 = (A[0] + rA * nx, A[1] + rA * ny)
        p2 = (B[0] + rB * nx, B[1] + rB * ny)
        out.append(
            f'    <line x1="{f(p1[0])}" y1="{f(p1[1])}" x2="{f(p2[0])}" y2="{f(p2[1])}" '
            f'stroke="#454c5a" stroke-width="4.5" stroke-dasharray="7 5">'
            f'<animate attributeName="stroke-dashoffset" from="0" '
            f'to="{f(direction * pitch)}" dur="{f(dur,3)}s" repeatCount="indefinite"/></line>')
    return "\n".join(out)


if __name__ == "__main__":
    self_check()
    svg = build()
    with open("pelican-bike.svg", "w", encoding="utf-8") as fh:
        fh.write(svg)
    ET.fromstring(svg.encode("utf-8"))          # XML 合法性
    ids = set()
    for el in ET.fromstring(svg.encode("utf-8")).iter():
        if "id" in el.attrib:
            ids.add(el.attrib["id"])
    for ref in ("sky", "sunGlow", "road", "scene", "bodyClip"):
        assert ref in ids, f"缺少 defs id: {ref}"
    print(f"  [ok] XML 合法，{len(svg)} 字节，defs: {sorted(ids)}")
