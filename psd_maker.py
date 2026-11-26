import argparse
import importlib.util
import os
import struct
import subprocess
import sys
import zlib


def _bootstrap():
    """Install the two core packages automatically if they are missing."""
    need = [pip for mod, pip in (("numpy", "numpy"), ("PIL", "pillow")) if importlib.util.find_spec(mod) is None]
    if need:
        print("Installing missing packages: " + ", ".join(need))
        cmd = [sys.executable, "-m", "pip", "install", *need]
        if subprocess.call(cmd) != 0 and sys.platform != "win32":
            subprocess.check_call(cmd + ["--break-system-packages"])   # PEP 668 workaround


_bootstrap()

import numpy as np  # noqa: E402
from PIL import Image, ImageFilter, ImageOps  # noqa: E402

try:
    import cv2
except Exception:  # optional
    cv2 = None

Image.MAX_IMAGE_PIXELS = None

NORM, MUL, SCRN, LLIT = b"norm", b"mul ", b"scrn", b"lLit"


#  Model: layers and groups (lists always run from BOTTOM to TOP)
class Layer:
    def __init__(self, name, rgb, alpha=None, blend=NORM, opacity=100, visible=True):
        self.name = name
        self.rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        h, w = self.rgb.shape[:2]
        if alpha is None:
            self.alpha = np.full((h, w), 255, np.uint8)
        else:
            a = np.asarray(alpha)
            if a.dtype != np.uint8:
                a = np.clip(a * 255.0 + 0.5, 0, 255).astype(np.uint8)
            self.alpha = np.ascontiguousarray(a)
        self.blend, self.opacity, self.visible = blend, opacity, visible


class Group:
    def __init__(self, name, children, visible=True, opened=True):
        self.name, self.children, self.visible, self.opened = name, children, visible, opened


#  PSD writer (Adobe format v1, RGB 8-bit, per-layer alpha channel)
def _pascal(name):
    b = name.encode("latin-1", "replace")[:255]
    s = bytes([len(b)]) + b
    return s + b"\0" * (-len(s) % 4)


def _block(key, data):
    data += b"\0" * (-len(data) % 4)
    return b"8BIM" + key + struct.pack(">I", len(data)) + data


def _record(name, bounds, lens, blend, opacity, visible, blocks):
    r = struct.pack(">iiii", *bounds) + struct.pack(">H", 4)
    for cid, n in zip((0, 1, 2, -1), lens):
        r += struct.pack(">hI", cid, n)
    flags = 0x08 | (0 if visible else 0x02)
    r += b"8BIM" + blend + struct.pack(">BBBB", int(round(opacity * 2.55)), 0, flags, 0)
    uni = struct.pack(">I", len(name)) + name.encode("utf-16-be")
    extra = struct.pack(">II", 0, 0) + _pascal(name) + _block(b"luni", uni) + b"".join(blocks)
    return r + struct.pack(">I", len(extra)) + extra


def _chan(arr, compress):
    raw = np.ascontiguousarray(arr).tobytes()
    return struct.pack(">H", 2) + zlib.compress(raw, 6) if compress else struct.pack(">H", 0) + raw


def _group_rec(name, kind, visible):
    # kind: 1 = open folder, 2 = closed folder, 3 = end-of-folder divider
    data = struct.pack(">I", kind) + (b"8BIM" + b"pass" if kind != 3 else b"")
    blend = NORM if kind == 3 else b"pass"
    rec = _record(name, (0, 0, 0, 0), [2] * 4, blend, 100, visible, [_block(b"lsct", data)])
    return rec, struct.pack(">H", 0) * 4


def _layer_rec(L, compress):
    ys = np.flatnonzero(L.alpha.any(1))
    xs = np.flatnonzero(L.alpha.any(0))
    if len(ys) == 0:
        return None
    t, b, l, r = int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1
    rgb, a = L.rgb[t:b, l:r], L.alpha[t:b, l:r]
    ch = [_chan(rgb[..., i], compress) for i in range(3)] + [_chan(a, compress)]
    rec = _record(L.name, (t, l, b, r), [len(c) for c in ch], L.blend, L.opacity, L.visible, [])
    return rec, b"".join(ch)


def _flatten(items, compress, recs):
    for it in items:
        if isinstance(it, Group):
            recs.append(_group_rec("</Layer group>", 3, True))
            _flatten(it.children, compress, recs)
            recs.append(_group_rec(it.name, 1 if it.opened else 2, it.visible))
        else:
            r = _layer_rec(it, compress)
            if r:
                recs.append(r)


def write_psd(path, canvas, items, compress=True):
    h, w = canvas.shape[:2]
    if max(h, w) > 30000:
        raise ValueError("Image too large for the PSD format (max 30000 px per side).")
    recs = []
    _flatten(items, compress, recs)
    li = struct.pack(">h", len(recs)) + b"".join(r for r, _ in recs) + b"".join(d for _, d in recs)
    li += b"\0" * (len(li) % 2)
    lm = struct.pack(">I", len(li)) + li + struct.pack(">I", 0)
    with open(path, "wb") as f:
        f.write(b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, h, w, 8, 3))
        f.write(struct.pack(">I", 0))          # color mode data
        f.write(struct.pack(">I", 0))          # image resources
        f.write(struct.pack(">I", len(lm)) + lm)
        f.write(struct.pack(">H", 0))          # uncompressed composite (max compatibility)
        f.write(np.ascontiguousarray(canvas.transpose(2, 0, 1)).tobytes())


def composite(items, h, w):
    """Flatten the layers the way Photoshop would (normal / multiply / screen / linear light)."""
    out = np.full((h, w, 3), 255, np.float32)

    def run(lst):
        nonlocal out
        for it in lst:
            if isinstance(it, Group):
                if it.visible:
                    run(it.children)
                continue
            if not it.visible:
                continue
            s = it.rgb.astype(np.float32)
            a = (it.alpha.astype(np.float32) / 255 * it.opacity / 100)[..., None]
            if it.blend == MUL:
                bl = out * s / 255
            elif it.blend == SCRN:
                bl = 255 - (255 - out) * (255 - s) / 255
            elif it.blend == LLIT:
                bl = np.clip(out + 2 * s - 255, 0, 255)
            else:
                bl = s
            out = out * (1 - a) + bl * a

    run(items)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


#  Decomposition methods
def _lum(img):
    f = img.astype(np.float32)
    return (f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114) / 255.0


def _smooth(x, a, b):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def _stick_break(ws):
    """Weights (top to bottom, summing to 1) -> alphas to stack with 'normal' so that the
    layers cover exactly those weights. The sum of the layers rebuilds the photo with no holes."""
    covered = np.zeros_like(ws[0])
    out = []
    for w in ws:
        rem = 1 - covered
        out.append(np.where(rem > 1e-5, np.clip(w / np.maximum(rem, 1e-5), 0, 1), 0))
        covered = covered + w
    return out


def m_tones(img, **_):
    L = _lum(img)
    sh = 1 - _smooth(L, 0.15, 0.50)
    hi = _smooth(L, 0.50, 0.85)
    mid = np.clip(1 - sh - hi, 0, 1)
    a_hi, a_mid, a_sh = _stick_break([hi, mid, sh])
    return [Layer("Shadows", img, a_sh), Layer("Midtones", img, a_mid), Layer("Highlights", img, a_hi)]


def _kmeans(x, k):
    try:
        from sklearn.cluster import KMeans
        return KMeans(k, n_init=3, random_state=0).fit(x).cluster_centers_.astype(np.float32)
    except Exception:
        pass
    rng = np.random.default_rng(0)
    c = x[rng.choice(len(x), k, replace=False)].copy()
    for _ in range(25):
        lab = (-2 * x @ c.T + (c ** 2).sum(1)).argmin(1)
        for j in range(k):
            m = lab == j
            if m.any():
                c[j] = x[m].mean(0)
    return c


def _label_stats(img, labels, k):
    px = img.reshape(-1, 3)
    cnt = np.bincount(labels.ravel(), minlength=k)
    mean = np.stack([np.bincount(labels.ravel(), weights=px[:, c], minlength=k) for c in range(3)], 1)
    return mean / np.maximum(cnt, 1)[:, None], cnt


def m_colors(img, k=6, flat_colors=False, smooth=5, **_):
    h, w = img.shape[:2]
    feat = (cv2.cvtColor(img, cv2.COLOR_RGB2LAB) if cv2 is not None else img).reshape(-1, 3).astype(np.float32)
    rng = np.random.default_rng(0)
    sample = feat[rng.choice(len(feat), min(60000, len(feat)), replace=False)]
    k = max(2, min(int(k), 32, len(np.unique(sample, axis=0))))
    cen = _kmeans(sample, k)
    labels = np.empty(len(feat), np.uint8)
    c2 = (cen ** 2).sum(1)
    for s in range(0, len(feat), 200000):
        labels[s:s + 200000] = (-2 * feat[s:s + 200000] @ cen.T + c2).argmin(1)
    labels = labels.reshape(h, w)
    mean, _c = _label_stats(img, labels, k)
    order = np.argsort(mean @ np.array([0.299, 0.587, 0.114]))       # darkest to lightest
    remap = np.empty(k, np.uint8)
    remap[order] = np.arange(k)
    labels = remap[labels]
    if smooth and cv2 is not None:
        labels = cv2.medianBlur(labels, smooth if smooth % 2 else smooth + 1)   # removes speckles
    mean, cnt = _label_stats(img, labels, k)
    out = []
    for j in range(k):
        if cnt[j] == 0:
            continue
        col = tuple(int(v) for v in np.clip(mean[j] + 0.5, 0, 255))
        rgb = np.full(img.shape, col, np.uint8) if flat_colors else img
        name = "Color %d #%02X%02X%02X (%d%%)" % (j + 1, *col, round(100 * cnt[j] / labels.size))
        out.append(Layer(name, rgb, (labels == j).astype(np.uint8) * 255))
    return out


def _subject_alpha(img, rect=None):
    h, w = img.shape[:2]
    try:
        from rembg import remove
        return np.asarray(remove(Image.fromarray(img)).convert("RGBA"))[..., 3].astype(np.float32) / 255
    except Exception:
        pass
    if cv2 is None:
        return None
    sc = min(1.0, 800 / max(h, w))
    small = cv2.resize(img, (max(1, int(w * sc)), max(1, int(h * sc))), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    rx, ry, rw, rh = rect or (0.06, 0.06, 0.88, 0.88)
    r = (int(rx * sw), int(ry * sh), max(1, int(rw * sw)), max(1, int(rh * sh)))
    mask = np.zeros((sh, sw), np.uint8)
    cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), mask, r,
                np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64), 6, cv2.GC_INIT_WITH_RECT)
    m = ((mask == 1) | (mask == 3)).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    if n > 1:
        m = (lab == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
    m = cv2.resize(m.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(cv2.GaussianBlur(m, (0, 0), 1.2), 0, 1)


def m_subject(img, rect=None, **_):
    a = _subject_alpha(img, rect)
    if a is None:
        return None
    return [Layer("Background", img), Layer("Subject", img, a)]


def m_edges(img, **_):
    g = np.asarray(Image.fromarray(img).convert("L"))
    if cv2 is not None:
        e = cv2.Canny(cv2.GaussianBlur(g, (0, 0), 1.0), 50, 150)
        e = cv2.GaussianBlur(e, (0, 0), 0.7)
    else:
        e = np.asarray(ImageOps.autocontrast(Image.fromarray(g).filter(ImageFilter.FIND_EDGES)))
    return [Layer("Photo", img), Layer("Lines", np.zeros_like(img), e, blend=MUL)]


def m_channels(img, **_):
    out = [Layer("Black", np.zeros_like(img))]
    for i, n in enumerate(("Red", "Green", "Blue")):
        c = np.zeros_like(img)
        c[..., i] = img[..., i]
        out.append(Layer(n, c, blend=SCRN))
    return out


def m_frequency(img, radius=None, **_):
    h, w = img.shape[:2]
    radius = radius or max(2.0, max(h, w) / 250)
    low = np.asarray(Image.fromarray(img).filter(ImageFilter.GaussianBlur(radius)))
    high = np.clip((img.astype(np.float32) - low) / 2 + 128, 0, 255).astype(np.uint8)
    return [Layer("Color (low frequencies)", low), Layer("Texture (high frequencies)", high, blend=LLIT)]


METHODS = {
    "subject": ("Subject & background", m_subject),
    "tones": ("Tones", m_tones),
    "colors": ("Colors", m_colors),
    "edges": ("Lines", m_edges),
    "channels": ("RGB channels", m_channels),
    "frequency": ("Frequencies", m_frequency),
}
STANDALONE = {"channels", "frequency"}


def parse_methods(s):
    ms = []
    for m in (s or "flat").lower().replace(" ", "").split(","):
        if m == "all":
            ms += ["subject", "tones", "colors", "edges"]
        elif m == "flat" or m in METHODS:
            ms.append(m)
        else:
            raise ValueError("Unknown method: %s" % m)
    ms = [m for i, m in enumerate(ms) if m not in ms[:i]]
    if "flat" in ms and len(ms) > 1:
        ms.remove("flat")
    if len(ms) > 1 and STANDALONE & set(ms):
        raise ValueError("'channels' and 'frequency' must be used on their own.")
    return ms


def build_items(img, methods, with_original=True, **opts):
    """Returns (layers bottom to top, notes)."""
    notes = []
    if methods == ["flat"]:
        return [Layer("Photo", img)], notes
    groups = []
    for m in methods:
        title, fn = METHODS[m]
        layers = fn(img, **opts)
        if layers is None:
            notes.append("'%s' skipped: needs opencv-python (pip install opencv-python) or rembg." % m)
            continue
        groups.append((title, layers))
    items = []
    if with_original:
        items.append(Layer("Original (hidden)", img, visible=False))
    if len(groups) == 1:
        items += groups[0][1]
    else:
        gs = [Group(t, ls, visible=(i == 0), opened=(i == 0)) for i, (t, ls) in enumerate(groups)]
        items += gs[::-1]          # the first group ends up on top of the Layers panel
    return items, notes


#  Logo / image replacement
def _load_rgba(path):
    return np.asarray(ImageOps.exif_transpose(Image.open(path)).convert("RGBA"))


def _need_cv2():
    if cv2 is None:
        raise RuntimeError("Replacing an image needs opencv-python:  pip install opencv-python")


def _nms(cands, max_matches):
    cands.sort(key=lambda c: -c[0])
    keep = []
    for sc, x, y, w, h in cands:
        ok = True
        for _, X, Y, W, H in keep:
            iw = min(x + w, X + W) - max(x, X)
            ih = min(y + h, Y + H) - max(y, Y)
            if iw > 0 and ih > 0 and iw * ih / min(w * h, W * H) > 0.4:
                ok = False
                break
        if ok:
            keep.append((sc, x, y, w, h))
            if len(keep) >= max_matches:
                break
    return keep


def _match_template(g, tg, threshold, max_matches):
    """Multi-scale template matching. g, tg = grayscale. Returns ([(score,x,y,w,h)], best score)."""
    th, tw = tg.shape
    gh, gw = g.shape
    smax = min(gw / tw, gh / th, 8.0)
    s = max(14.0 / min(tw, th), 0.04)
    cands, best = [], -1.0
    while s <= smax:
        size = (max(1, round(tw * s)), max(1, round(th * s)))
        if 12 <= size[0] <= gw and 12 <= size[1] <= gh:
            t = cv2.resize(tg, size, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
            if t.std() > 2:
                res = np.nan_to_num(cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED), nan=0.0, posinf=0.0, neginf=0.0)
                best = max(best, float(res.max()))
                ys, xs = np.where(res >= threshold)
                if len(ys) > 300:
                    top = np.argsort(res[ys, xs])[-300:]
                    ys, xs = ys[top], xs[top]
                cands += [(float(res[y, x]), int(x), int(y), size[0], size[1]) for y, x in zip(ys, xs)]
        s *= 1.1
    return _nms(cands, max_matches), best


def _match_sift(g, tg, max_inst=8):
    """Plan B for rotated / perspective copies: SIFT + homography. Returns [(score, H template->g)]."""
    try:
        sift = cv2.SIFT_create()
    except Exception:
        return []
    kt, dt = sift.detectAndCompute(tg, None)
    ki, di = sift.detectAndCompute(g, None)
    if dt is None or di is None or len(kt) < 8 or len(ki) < 8:
        return []
    th, tw = tg.shape
    corners = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]]).reshape(-1, 1, 2)
    bf, alive, out = cv2.BFMatcher(), np.ones(len(ki), bool), []
    for _ in range(max_inst):
        idx = np.flatnonzero(alive)
        if len(idx) < 8:
            break
        pairs = [p for p in bf.knnMatch(dt, di[idx], k=2) if len(p) == 2]
        good = [m for m, n in pairs if m.distance < 0.75 * n.distance]
        if len(good) < 8:
            break
        src = np.float32([kt[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([ki[idx[m.trainIdx]].pt for m in good]).reshape(-1, 1, 2)
        Hm, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
        if Hm is None or int(inl.sum()) < 8:
            break
        quad = cv2.perspectiveTransform(corners, Hm).reshape(4, 2).astype(np.float32)
        if cv2.isContourConvex(quad) and 100 < cv2.contourArea(quad) < 1.5 * g.shape[0] * g.shape[1]:
            out.append((min(1.0, float(inl.sum()) / 25), Hm))
        alive[[idx[good[i].trainIdx] for i in range(len(good)) if inl[i]]] = False
    return out


def find_matches(img, tpl, threshold=0.62, max_matches=30):
    """Look for tpl (RGBA) inside img (RGB). Returns ([{'H','score','how'}], best similarity).
    H maps template pixel coordinates to image coordinates."""
    _need_cv2()
    H, W = img.shape[:2]
    ds = min(1.0, 1600 / max(H, W))
    small = cv2.resize(img, (max(1, round(W * ds)), max(1, round(H * ds))), interpolation=cv2.INTER_AREA) if ds < 1 else img
    a = tpl[..., 3:4].astype(np.float32) / 255
    t = (tpl[..., :3] * a + 128 * (1 - a)).astype(np.uint8)       # transparent -> neutral gray
    tg = cv2.cvtColor(t, cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (0, 0), 0.7)
    th, tw = tg.shape
    boxes, best = _match_template(g, tg, threshold, max_matches)
    res = []
    for sc, x, y, w, h in boxes:
        Hm = np.array([[w / tw / ds, 0, x / ds], [0, h / th / ds, y / ds], [0, 0, 1]], np.float64)
        res.append({"H": Hm, "score": sc, "how": "template"})
    if not res:
        for sc, Hs in _match_sift(g, tg):
            res.append({"H": np.diag([1 / ds, 1 / ds, 1.0]) @ Hs, "score": sc, "how": "sift"})
            best = max(best, sc)
    return res, best


def apply_replacements(img, pairs, threshold=0.62, stretch=False, clean=True, log=print):
    """pairs = [(image to find, new image)]. Returns the list of layers (Cleanup / New image)."""
    _need_cv2()
    Hh, Ww = img.shape[:2]
    cleaned = img.copy() if clean else None
    layers, n = [], 0
    for old_p, new_p in pairs:
        tpl, new = _load_rgba(old_p), _load_rgba(new_p)
        ms, best = find_matches(img, tpl, threshold)
        if not ms:
            log("  ! '%s' not found (best similarity %.2f, threshold %.2f): try lowering the threshold."
                % (os.path.basename(old_p), max(best, 0), threshold))
            continue
        th, tw = tpl.shape[:2]
        nh, nw = new.shape[:2]
        fx, fy = (tw / nw, th / nh) if stretch else (min(tw / nw, th / nh),) * 2
        T = np.array([[fx, 0, (tw - nw * fx) / 2], [0, fy, (th - nh * fy) / 2], [0, 0, 1]], np.float64)
        corners = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]]).reshape(-1, 1, 2)
        for m in ms:
            n += 1
            Hm = m["H"]
            quad = cv2.perspectiveTransform(corners, Hm).reshape(4, 2)
            log("  found #%d  similarity %.2f (%s)" % (n, m["score"], m["how"]))
            # 1) area of the old logo (+ margin) -> Cleanup layer
            if tpl[..., 3].min() < 250:
                mask = (cv2.warpPerspective(tpl[..., 3], Hm, (Ww, Hh)) > 20).astype(np.uint8)
            else:
                mask = np.zeros((Hh, Ww), np.uint8)
                cv2.fillConvexPoly(mask, np.round(quad).astype(np.int32), 1)
            k = max(2, int(0.03 * min(np.ptp(quad[:, 0]), np.ptp(quad[:, 1]))))
            mask = cv2.dilate(mask, np.ones((2 * k + 1, 2 * k + 1), np.uint8))
            rows, cols = np.flatnonzero(mask.any(1)), np.flatnonzero(mask.any(0))
            if clean and len(rows):
                y0, y1 = max(0, rows[0] - 8), min(Hh, rows[-1] + 9)
                x0, x1 = max(0, cols[0] - 8), min(Ww, cols[-1] + 9)
                roi = cv2.cvtColor(cleaned[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
                fixed = cv2.inpaint(roi, mask[y0:y1, x0:x1] * 255, 3, cv2.INPAINT_TELEA)
                cleaned[y0:y1, x0:x1] = cv2.cvtColor(fixed, cv2.COLOR_BGR2RGB)
                al = np.zeros((Hh, Ww), np.uint8)
                soft = cv2.GaussianBlur(mask[y0:y1, x0:x1].astype(np.float32) * 255, (0, 0), 1.0)
                al[y0:y1, x0:x1] = np.clip(soft, 0, 255).astype(np.uint8)
                layers.append(Layer("Cleanup %d" % n, cleaned, al))
            # 2) the new image, warped onto the same area -> New image layer
            Hn = Hm @ T
            r = float(np.sqrt(abs(np.linalg.det(Hn[:2, :2]))))
            src = new.astype(np.float32)
            src[..., :3] *= src[..., 3:4] / 255                          # premultiplied alpha
            if r < 0.8:
                rw, rh = max(1, round(nw * r)), max(1, round(nh * r))
                src = cv2.resize(src, (rw, rh), interpolation=cv2.INTER_AREA)
                Hn = Hn @ np.diag([nw / rw, nh / rh, 1.0])
            sh_, sw_ = src.shape[:2]
            qn = cv2.perspectiveTransform(np.float32([[0, 0], [sw_, 0], [sw_, sh_], [0, sh_]]).reshape(-1, 1, 2), Hn).reshape(4, 2)
            x0, x1 = max(0, int(np.floor(qn[:, 0].min())) - 2), min(Ww, int(np.ceil(qn[:, 0].max())) + 2)
            y0, y1 = max(0, int(np.floor(qn[:, 1].min())) - 2), min(Hh, int(np.ceil(qn[:, 1].max())) + 2)
            if x1 <= x0 or y1 <= y0:
                continue
            Tr = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], np.float64)
            wp = cv2.warpPerspective(src, Tr @ Hn, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
            a = np.clip(wp[..., 3], 0, 255)
            rgb = np.zeros((Hh, Ww, 3), np.uint8)
            alpha = np.zeros((Hh, Ww), np.uint8)
            rgb[y0:y1, x0:x1] = np.clip(wp[..., :3] / np.maximum(a[..., None] / 255, 1e-4), 0, 255).astype(np.uint8)
            alpha[y0:y1, x0:x1] = (a + 0.5).astype(np.uint8)
            layers.append(Layer("New image %d" % n, rgb, alpha))
    return layers


#  Converting a file
def load_image(path):
    im = ImageOps.exif_transpose(Image.open(path))
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        im = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im)
    return np.asarray(im.convert("RGB"))


def convert(path, out=None, method="flat", k=6, flat_colors=False, radius=None, rect=None,
            with_original=True, compress=True, max_side=None, log=print,
            replace=None, threshold=0.62, stretch=False, clean=True):
    methods = parse_methods(method)
    img = load_image(path)
    if max_side and max(img.shape[:2]) > max_side:
        s = max_side / max(img.shape[:2])
        img = np.asarray(Image.fromarray(img).resize((round(img.shape[1] * s), round(img.shape[0] * s)), Image.LANCZOS))
    out = out or os.path.splitext(path)[0] + ".psd"
    if os.path.abspath(out) == os.path.abspath(path):          # never overwrite the source file
        out = os.path.splitext(path)[0] + "_edited.psd"
    if replace:
        if methods != ["flat"]:
            log("  ! the split method is ignored when replacing an image")
        layers = apply_replacements(img, replace, threshold, stretch, clean, log)
        if not layers:
            raise RuntimeError("Nothing was replaced.")
        items = [Layer("Photo", img), Group("Replacements", layers)]
        notes = []
        canvas = composite(items, *img.shape[:2])
    else:
        items, notes = build_items(img, methods, with_original, k=k, flat_colors=flat_colors, radius=radius, rect=rect)
        canvas = img if methods == ["flat"] else composite(items, *img.shape[:2])
    write_psd(out, canvas, items, compress)
    for n in notes:
        log("  ! " + n)
    return out, items


def _count(items):
    return sum(_count(i.children) if isinstance(i, Group) else 1 for i in items)


#  Graphical interface (tkinter)
def run_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, ttk

    modes = [("PSD only (1 layer)", "flat"),
             ("Everything: subject + tones + colors + lines", "all"),
             ("Subject / background", "subject"),
             ("Tones: shadows, midtones, highlights", "tones"),
             ("Dominant colors", "colors"),
             ("Red / Green / Blue channels", "channels"),
             ("Frequencies: color + texture", "frequency"),
             ("Lines (outlines)", "edges"),
             ("Replace logo / image", "replace")]
    root = tk.Tk()
    root.title("PSD Maker")
    files, q = [], queue.Queue()
    lbl = tk.StringVar(value="No photo selected")
    mode = tk.StringVar(value=modes[0][0])
    kvar, orig = tk.IntVar(value=6), tk.BooleanVar(value=True)
    flatc, outdir = tk.BooleanVar(value=False), tk.StringVar(value="")
    oldp, newp, thr = tk.StringVar(value=""), tk.StringVar(value=""), tk.DoubleVar(value=0.62)
    full = {}
    imgtypes = [("Images", "*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp *.psd"), ("All files", "*.*")]

    def pick():
        sel = filedialog.askopenfilenames(filetypes=imgtypes)
        if sel:
            files[:] = sel
            lbl.set("%d photo(s): %s" % (len(sel), os.path.basename(sel[0])))

    def pick_to(var, key):
        p = filedialog.askopenfilename(filetypes=imgtypes)
        if p:
            var.set(os.path.basename(p))
            full[key] = p

    def pick_out():
        d = filedialog.askdirectory()
        if d:
            outdir.set(d)

    def work(m):
        rep = None
        if m == "replace":
            if not (full.get("old") and full.get("new")):
                q.put("Choose both the image to find and the new image.")
                q.put(None)
                return
            rep, m = [(full["old"], full["new"])], "flat"
        for p in files:
            try:
                o = os.path.join(outdir.get(), os.path.splitext(os.path.basename(p))[0] + ".psd") if outdir.get() else None
                o, items = convert(p, o, m, k=kvar.get(), flat_colors=flatc.get(), with_original=orig.get(),
                                   log=lambda s: q.put(s), replace=rep, threshold=thr.get())
                q.put("OK  %s (%d layers)" % (o, _count(items)))
            except Exception as e:
                q.put("ERROR %s: %s" % (os.path.basename(p), e))
        q.put(None)

    def go():
        if not files:
            pick()
        if files:
            btn.config(state="disabled")
            log.delete("1.0", "end")
            threading.Thread(target=work, args=(dict(modes)[mode.get()],), daemon=True).start()

    def poll():
        try:
            while True:
                s = q.get_nowait()
                if s is None:
                    btn.config(state="normal")
                    log.insert("end", "Done.\n")
                else:
                    log.insert("end", s + "\n")
                log.see("end")
        except queue.Empty:
            pass
        root.after(150, poll)

    f = ttk.Frame(root, padding=12)
    f.grid()
    ttk.Button(f, text="Choose photo(s)...", command=pick).grid(row=0, column=0, sticky="w")
    ttk.Label(f, textvariable=lbl).grid(row=0, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Label(f, text="What do you want:").grid(row=1, column=0, sticky="w", pady=6)
    ttk.Combobox(f, textvariable=mode, values=[m[0] for m in modes], state="readonly", width=40).grid(row=1, column=1, columnspan=2, sticky="w")
    ttk.Label(f, text="Colors (k):").grid(row=2, column=0, sticky="w")
    ttk.Spinbox(f, from_=2, to=32, textvariable=kvar, width=5).grid(row=2, column=1, sticky="w")
    ttk.Checkbutton(f, text="Flat color layers", variable=flatc).grid(row=2, column=2, sticky="w")
    ttk.Checkbutton(f, text="Include the original photo (hidden)", variable=orig).grid(row=3, column=0, columnspan=3, sticky="w")
    ttk.Button(f, text="Output folder...", command=pick_out).grid(row=4, column=0, sticky="w", pady=6)
    ttk.Label(f, textvariable=outdir).grid(row=4, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Button(f, text="To replace (old logo)...", command=lambda: pick_to(oldp, "old")).grid(row=5, column=0, sticky="w")
    ttk.Label(f, textvariable=oldp).grid(row=5, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Button(f, text="New image...", command=lambda: pick_to(newp, "new")).grid(row=6, column=0, sticky="w", pady=4)
    ttk.Label(f, textvariable=newp).grid(row=6, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Label(f, text="Minimum similarity:").grid(row=7, column=0, sticky="w")
    ttk.Spinbox(f, from_=0.30, to=0.95, increment=0.02, textvariable=thr, width=5, format="%.2f").grid(row=7, column=1, sticky="w")
    ttk.Label(f, text="(only used with 'Replace logo / image')").grid(row=7, column=2, sticky="w")
    btn = ttk.Button(f, text="Create PSD", command=go)
    btn.grid(row=8, column=0, columnspan=3, pady=6)
    log = tk.Text(f, width=70, height=8)
    log.grid(row=9, column=0, columnspan=3)
    poll()
    root.mainloop()


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Photo -> editable PSD", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Methods: flat, subject, tones, colors, edges, channels, frequency, all.\n"
                                        "Run with no arguments to open the window.")
    ap.add_argument("inputs", nargs="*", help="one or more images")
    ap.add_argument("-m", "--method", default="flat", help="flat, subject, tones, colors, edges, channels, frequency, all")
    ap.add_argument("-o", "--output", help="output file (single photo only) or folder")
    ap.add_argument("-k", type=int, default=6, help="number of colors for 'colors'")
    ap.add_argument("--flat-colors", action="store_true", help="solid-color layers instead of the original pixels")
    ap.add_argument("--radius", type=float, help="blur radius for 'frequency'")
    ap.add_argument("--rect", help="subject area x,y,w,h as fractions 0-1 (e.g. 0.2,0.1,0.6,0.8)")
    ap.add_argument("--no-original", action="store_true", help="do not include the hidden original photo")
    ap.add_argument("--raw", action="store_true", help="no compression (bigger files, maximum compatibility)")
    ap.add_argument("--max-side", type=int, help="resize the long side to N pixels")
    ap.add_argument("--replace", nargs=2, action="append", metavar=("TO_FIND", "NEW"),
                    help="find TO_FIND in the photo (all copies) and replace it with NEW; repeatable")
    ap.add_argument("--threshold", type=float, default=0.62, help="minimum similarity 0-1 for --replace (default 0.62)")
    ap.add_argument("--stretch", action="store_true", help="--replace: fill the area ignoring proportions")
    ap.add_argument("--no-clean", action="store_true", help="--replace: do not erase the old logo")
    a = ap.parse_args(argv)
    if not a.inputs:
        try:
            return run_gui()
        except ImportError:
            ap.print_help()
            return 1
    rect = tuple(float(v) for v in a.rect.split(",")) if a.rect else None
    code = 0
    for p in a.inputs:
        out = None
        if a.output:
            out = os.path.join(a.output, os.path.splitext(os.path.basename(p))[0] + ".psd") if (len(a.inputs) > 1 or os.path.isdir(a.output)) else a.output
        try:
            o, items = convert(p, out, a.method, a.k, a.flat_colors, a.radius, rect, not a.no_original, not a.raw, a.max_side,
                               replace=[tuple(r) for r in a.replace] if a.replace else None,
                               threshold=a.threshold, stretch=a.stretch, clean=not a.no_clean)
            print("OK  %s  (%d layers)" % (o, _count(items)))
        except Exception as e:
            print("ERROR %s: %s" % (p, e))
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
