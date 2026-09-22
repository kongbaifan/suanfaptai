# -*- coding: utf-8 -*-
"""对 headless 渲染的帧做结构化校验，并输出文本色块图（便于无图形环境下审阅构图）。"""
import glob
import importlib.util
import math
import numpy as np
from PIL import Image

# 与生成脚本共用同一套几何常量，避免验收口径与出图口径漂移
_spec = importlib.util.spec_from_file_location("pelican_gen", ".pelican_gen.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)
BB, CRANK, RW, CRANK_DUR = gen.BB, gen.CRANK, gen.RW, gen.CRANK_DUR
RHUB, FHUB = gen.RHUB, gen.FHUB

PALETTE = [
    ("W", (253, 253, 248)), ("w", (233, 238, 242)),      # 身体高光 / 腹部阴影
    ("O", (245, 154, 46)), ("P", (255, 207, 110)),       # 上喙 / 喉囊
    ("L", (242, 149, 63)), ("l", (207, 127, 51)),        # 近腿 / 远腿
    ("F", (225, 75, 75)), ("f", (143, 47, 47)),          # 车架 / 车架暗边（头盔同色）
    ("T", (43, 50, 64)),                                 # 轮胎/轮廓/眼睛（深色）
    ("S", (74, 63, 53)),                                 # 车座
    ("Y", (111, 192, 234)), (".", (185, 226, 246)),      # 天空上下
    ("C", (255, 255, 255)),                              # 云
    ("H", (183, 220, 174)), ("h", (142, 203, 132)),      # 远山 / 中景丘陵
    ("t", (109, 164, 95)), ("b", (125, 90, 60)),         # 树冠 / 树干
    ("G", (154, 212, 111)), ("r", (109, 118, 132)),      # 草地 / 路面
    ("s", (255, 224, 102)), ("p", (250, 240, 200)),      # 太阳
]
CLS = np.array([c for _, c in PALETTE], dtype=np.int16)
CHARS = [n for n, _ in PALETTE]


def classify(arr):
    d = np.abs(arr[:, :, None, :].astype(np.int16) - CLS[None, None, :, :]).sum(axis=3)
    return d.argmin(axis=2)


def mask(arr, rgb, tol):
    return (np.abs(arr.astype(np.int16) - np.array(rgb, dtype=np.int16)).max(axis=2) <= tol)


def text_map(arr, cell=10):
    idx = classify(arr)
    h, w = idx.shape
    rows = []
    for y in range(0, h, cell):
        line = []
        for x in range(0, w, cell):
            block = idx[y:y + cell, x:x + cell].ravel()
            counts = np.bincount(block, minlength=len(CHARS))
            line.append(CHARS[int(counts.argmax())])
        rows.append("".join(line))
    return "\n".join(rows)


def main():
    frames = sorted(glob.glob(".frame_*.png"), key=lambda p: int(p.split("_")[1].split(".")[0]))
    assert frames, "没有渲染帧"
    prev = None
    print(f"共 {len(frames)} 帧: {[f.split('_')[1].split('.')[0] + 'ms' for f in frames]}")
    for path in frames:
        img = Image.open(path).convert("RGB")
        arr = np.array(img)
        tag = path.split("_")[1].split(".")[0]
        assert arr.shape[:2] == (540, 900), f"尺寸异常 {arr.shape}"

        sky = arr[40, 50].astype(int)
        road = arr[500, 60].astype(int)
        tire = arr[307, 300].astype(int)          # 后轮外胎顶点 (hub y - R)
        assert sky[2] > sky[0] + 20, f"{tag}: 天空区域颜色异常 {sky}"
        assert int(road.max()) - int(road.min()) < 40 and road.mean() < 180, \
            f"{tag}: 路面异常 {road}"
        assert tire.max() < 130 and tire.mean() < 115, f"{tag}: 后轮外胎缺失 {tire}"

        # --- 腿：y>335 的橙色像素必须落在曲柄圆附近（脚掌踩踏板的核心不变量）---
        low = np.zeros(arr.shape[:2], dtype=bool)
        low[335:, :] = True
        near = mask(arr, (242, 149, 63), 22) & low       # tol 收紧：避免路边小花(255,209,102)混入
        # 近腿轮廓的抗锯齿像素会向远腿色偏移，做 3px 膨胀后从远腿掩膜里剔除
        near_halo = np.zeros_like(near)
        for dxy in range(-3, 4):
            for dyx in range(-3, 4):
                near_halo |= np.roll(np.roll(near, dxy, 0), dyx, 1)
        far = mask(arr, (207, 127, 51), 22) & low & ~near_halo

        feet = {}
        for name, m, off in (("近腿", near, 0.0), ("远腿", far, 180.0)):
            n = int(m.sum())
            assert n > 60, f"{tag}: {name}下半段像素过少 ({n})"
            ys, xs = np.nonzero(m)
            d = np.hypot(xs - BB[0], ys - BB[1])
            assert d.min() > 4 and d.max() < 85, \
                f"{tag}: {name}下半段脱离车身范围 d=[{d.min():.1f},{d.max():.1f}]"
            print(f"  [{tag}] {name}: {n}px 距中轴 {d.min():.1f}~{d.max():.1f}px "
                  f"最低点 y={ys.max()} x范围 {xs.min()}~{xs.max()}")
            feet[name] = (float(xs[ys == ys.max()].mean()), int(ys.max()), off, xs, ys)

        # (1) 两个脚掌都必须落在解析出的踏板位置附近（最小间距判据，抗遮挡）
        for name, (obs_x, obs_y, off, xs, ys) in feet.items():
            phase = ((int(tag) / 1000.0 / CRANK_DUR * 360.0) + off) % 360.0
            px = BB[0] + CRANK * math.cos(math.radians(phase))
            py = BB[1] + CRANK * math.sin(math.radians(phase))
            gmin = float(np.min(np.hypot(xs - px, ys - py)))
            print(f"  [{tag}] {name}相位 {phase:6.1f}° 踏板解析 ({px:.0f},{py:.0f})"
                  f" 掩膜最近像素间距 {gmin:.1f}px")
            assert gmin < 20, f"{tag}: {name}没有踩在踏板上（最近 {gmin:.1f}px）"

        # (2) 近侧脚掌下缘高度必须与曲柄相位吻合（无遮挡，可精确比对）
        obs_x, obs_y, _off, _xs, _ys = feet["近腿"]
        phase = (int(tag) / 1000.0 / CRANK_DUR * 360.0) % 360.0
        px = BB[0] + CRANK * math.cos(math.radians(phase))
        pred_y = BB[1] + CRANK * math.sin(math.radians(phase)) + 8.0
        print(f"  [{tag}] 近脚掌下缘 预期 y≈{pred_y:.0f}；实测 ({obs_x:.0f},{obs_y})"
              f" Δy={obs_y - pred_y:+.0f} Δx={obs_x - px:+.0f}")
        assert abs(obs_y - pred_y) < 20, f"{tag}: 近脚掌高度与曲柄相位不符"
        assert abs(obs_x - px) < 26, f"{tag}: 近脚掌横向与曲柄相位不符"

        # --- 关键色块存在性 ---
        checks = [
            ("身体白", mask(arr, (253, 253, 248), 10), (120, 260, 300, 470), 2500),
            ("上喙橙", mask(arr, (245, 154, 46), 34), (85, 135, 470, 615), 200),
            ("喉囊黄", mask(arr, (255, 207, 110), 26), (100, 175, 470, 615), 250),
            ("车架红", mask(arr, (225, 75, 75), 40), (245, 385, 290, 575), 260),
        ]
        for name, m, (y0, y1, x0, x1), need in checks:
            sub = m[y0:y1, x0:x1].sum()
            assert sub > need, f"{tag}: {name} 在 ({x0}-{x1},{y0}-{y1}) 仅 {sub}px < {need}"
            print(f"  [{tag}] {name}: {sub}px (>= {need})")

        if prev is not None:
            wheelbox = (slice(300, 436), slice(232, 368))
            diff = np.abs(arr[wheelbox].astype(int) - prev[wheelbox].astype(int)).mean()
            print(f"  [{tag}] 与前一帧后轮区域平均差 {diff:.2f}")
        prev = arr

    img = Image.open(frames[len(frames) // 2]).convert("RGB")
    arr = np.array(img)
    print(f"\n===== 文本色块图（帧 {frames[len(frames)//2]}，每格 10x10 px）=====")
    print("图例: .=天空 Y=高空 W=身体 O=上喙 P=喉囊 L=近腿 l=远腿 F=红 f=暗红 "
          "T=深色/轮胎 S=车座 H=远山 h=丘陵 t=树 G=草 r=路面 C=云 s=太阳")
    print(text_map(arr))
    Image.open(frames[len(frames) // 2]).resize((450, 270)).save(".preview.png")


if __name__ == "__main__":
    main()
