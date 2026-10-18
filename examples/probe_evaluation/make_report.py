"""Step 2: analyse <work>/probe_eval.pkl (from acquire.py) and write the
multi-scale / probe compensation report (HTML + Markdown + PNG figures).

    python examples/probe_evaluation/make_report.py [--work DIR] [--dest DIR]

--dest copies the finished report into a fixed directory, with report.html
renamed to index.html (e.g. for GitHub Pages).
"""

import argparse
import io
import pickle
import shutil
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rigol_ds1000z.lab import Lab, eng

ap = argparse.ArgumentParser()
ap.add_argument("--work", default="rigol_data/probe_eval")
ap.add_argument("--dest")
args = ap.parse_args()
work = Path(args.work)

d = pickle.load(open(work / "probe_eval.pkl", "rb"))
R = {r["label"]: r for r in d["results"]}
C1 = "#c99a00"
TOP, BOTTOM, COMP = "top plateau 0.1V/div AVG16", "bottom plateau 0.1V/div AVG16", "comp view 100us/div AVG16"


def wave(label):
    x = R[label]["capture"].waveforms[1]
    return x.times, x.volts.astype(float), x


def png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def tscale(t):
    span = np.max(np.abs(t))
    for s, u in ((1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
        if span >= s:
            return t / s, u
    return t / 1e-9, "ns"


def edge_time(t, v, base, top, frac, rising=True):
    """Interpolated time where v crosses base + frac*(top-base), nearest to t=0."""
    lvl = base + frac * (top - base)
    idx = np.flatnonzero(np.diff(np.sign(v - lvl)) != 0)
    idx = [i for i in idx if (v[i + 1] > v[i]) == rising]
    if not idx:
        return None
    i = min(idx, key=lambda i: abs(t[i]))
    return t[i] + (lvl - v[i]) * (t[i + 1] - t[i]) / (v[i + 1] - v[i])


# ------------------------------------------------------------------ analysis
t, v, _ = wave(COMP)
top_lv = float(np.median(v[(t > 100e-6) & (t < 450e-6)]))
base_lv = float(np.median(v[(t > -400e-6) & (t < -50e-6)]))
amp = top_lv - base_lv

edges = {}
for label, rising in (("rise 2 us/div", True), ("fall 2 us/div", False)):
    t, v, _ = wave(label)
    edges[label] = abs(edge_time(t, v, base_lv, top_lv, 0.9, rising) - edge_time(t, v, base_lv, top_lv, 0.1, rising))
rise, fall = edges["rise 2 us/div"], edges["fall 2 us/div"]

plateau = {}
for label, sign in ((TOP, 1), (BOTTOM, -1)):
    t, v, x = wave(label)

    def mean(a, b):
        return float(v[(t >= a) & (t < b)].mean())

    early, mid, late = mean(20e-6, 60e-6), mean(200e-6, 260e-6), mean(420e-6, 480e-6)
    after = (t > 2e-6) & (t < 480e-6)
    peak = float(v[after].max() if sign > 0 else v[after].min())
    within = np.abs(sign * (v - late) / amp * 100) <= 0.5
    idx = np.flatnonzero(after)
    settle = next((float(t[i]) for i in idx if within[i:idx[-1] + 1].all()), None)
    plateau[label] = dict(early=early, mid=mid, late=late, tilt=sign * (early - late) / amp * 100,
                          peak=sign * (peak - late) / amp * 100, settle=settle, lsb=x.preamble.yincrement * 1000)
top_m, bot_m = plateau[TOP], plateau[BOTTOM]

idn = d.get("idn") or {}
maker, model, fw = idn.get("manufacturer", "RIGOL"), idn.get("model", "DS1000Z"), idn.get("firmware", "?")
first = d["results"][0]["capture"]

# -------------------------------------------------------------------- report
lab = Lab("unused", work)
for r in d["results"]:
    lab.captures[r["capture"].id] = r["capture"]
rid = lab.report_create("CH1 テスト信号の多スケール計測とプローブ補正評価").id

lab.report_add_text(rid, f"""## 結論
- 信号は **{eng(R['0.5 V/div']['measure']['FREQ'], 'Hz')}・振幅 約 {amp:.2f} V（{base_lv:.2f} → {top_lv:.2f} V）・デューティ {R['0.5 V/div']['measure']['PDUT'] * 100:.1f} %** の方形波です。
- 立ち上がりは **{rise * 1e6:.2f} µs**、立ち下がりは **{fall * 1e6:.2f} µs**（10–90 %）です。RC 的になだらかな形で、リンギングはありません。
- **プローブ補正は良好**です。平坦部の傾きは上側 **{top_m['tilt']:+.2f} %**、下側 **{bot_m['tilt']:+.2f} %** で、どちらも振幅比 ±1 % 以内に収まっています。
- 上側はエッジ直後がわずかに高く、下側はわずかに低く、どちらも時間とともに戻ります。これは**ごく軽い過補償寄り**の形です。完全にフラットを狙う場合だけ、トリマを角の出っ張りが減る方向へごくわずかに回してください。
- オシロの自動測定のうち立ち上がり時間は、**エッジ全体が画面に入る時間軸（2 µs/div 前後）のときだけ有効**です。200 ns/div 以下では誤った値が出ていました。

## 測定条件
- 機器: {maker} {model}（FW {fw}）、LAN（生 SCPI ソケット）で制御しました。
- CH1: プローブ 10×、DC 結合、帯域制限 OFF。
- トリガ: CH1 エッジ、1.5 V（平坦部を拡大したときは各平坦部の近くに設定）。
- 取り込み: 画面データ 1200 点/回。プローブ補正の評価のみ 16 回平均です。
- 取得日時: {datetime.fromtimestamp(first.timestamp):%Y-%m-%d %H:%M}。測定後、本体の設定は測定前の状態に戻しました。
""")

# 1. vertical scale
labels = ["5 V/div", "2 V/div", "1 V/div", "0.5 V/div"]
fig, axes = plt.subplots(2, 2, figsize=(10, 5.6), sharex=True)
for ax, label in zip(axes.flat, labels):
    t, v, x = wave(label)
    tt, u = tscale(t)
    ax.plot(tt, v, color=C1, lw=0.8)
    ax.set_title(f"{label}  (LSB {x.preamble.yincrement * 1000:.0f} mV)", fontsize=10, loc="left")
    ax.set_ylim(-0.4, 3.4)
    ax.grid(alpha=0.3)
for ax in axes[1]:
    ax.set_xlabel(f"time [{u}]")
for ax in axes[:, 0]:
    ax.set_ylabel("V")
fig.tight_layout()
lab.report_add_text(rid, "## 1. 垂直スケールによる見え方の違い（200 µs/div）")
lab.report_add_image(rid, png(fig), "同じ信号を 4 通りの V/div で取得したもの。V/div が大きいほど量子化の段が粗くなります。")
rows = []
for label in labels:
    m = R[label]["measure"]
    _, _, x = wave(label)
    rows.append([label, f"{x.preamble.yincrement * 1000:.0f} mV", eng(m["VTOP"], "V"), eng(m["VBAS"], "V"),
                 eng(m["VAMP"], "V"), f"{(m['VAMP'] / amp - 1) * 100:+.1f} %", eng(x.analyze()["vpp"], "V"),
                 eng(m["FREQ"], "Hz"), f"{m['PDUT'] * 100:.1f} %"])
lab.report_add_table(rid, ["V/div", "1 LSB", "Vtop", "Vbase", "Vamp", "Vamp 誤差*", "Vpp（ホスト）", "周波数", "デューティ"],
                     rows, f"Vtop〜デューティはオシロの自動測定、Vpp はホスト側の計算。*16 回平均の振幅 {amp:.2f} V を基準にした差")
lab.report_add_text(rid, """- **5 V/div** では 1 LSB が 200 mV と粗く、振幅が約 4 % 小さく出ています。ホスト側の Vpp もノイズと量子化の影響で大きく膨らみました。
- **2 V/div 以下**では、振幅の誤差が ±1 % 以内に収まります。信号が縦 6 div を占める 0.5 V/div で、最も平均値に近い値になりました。
- 周波数とデューティは、どのスケールでも変わりません。ホスト側の周波数推定は、2.4 周期分の画面データだけを使うため ±0.2 % ほどばらつきます。正確な値が必要な場合は、オシロの周波数測定を使ってください。
- **精度よく振幅を測るには、信号が画面の縦 6〜8 div を占める V/div にする**のが基本です。""")

# 2. timebase sweep
tl = ["rise 1000 us/div", "rise 200 us/div", "rise 20 us/div", "rise 2 us/div", "rise 0.2 us/div", "rise 0.05 us/div"]
fig, axes = plt.subplots(3, 2, figsize=(10, 7.5))
for ax, label in zip(axes.flat, tl):
    t, v, x = wave(label)
    tt, u = tscale(t)
    ax.plot(tt, v, color=C1, lw=0.8)
    ax.set_title(label.replace("rise ", "") + f"  ({len(v)} pts, dt {eng(x.preamble.xincrement, 's')})",
                 fontsize=10, loc="left")
    ax.set_xlabel(f"time [{u}]")
    ax.set_ylim(-0.3, 3.3)
    ax.grid(alpha=0.3)
fig.tight_layout()
lab.report_add_text(rid, "## 2. 時間軸スケールと立ち上がりエッジ（0.5 V/div）")
lab.report_add_image(rid, png(fig), "立ち上がりエッジ（画面中央 = トリガ点）を 1 ms/div から 50 ns/div まで拡大したもの")
rows = []
for label in tl + ["fall 2 us/div", "fall 0.2 us/div", "fall 0.05 us/div"]:
    st, m = R[label]["status"], R[label]["measure"]
    key = "RTIM" if label.startswith("rise") else "FTIM"
    window = st["timebase_scale"] * 12
    if m.get(key) is None:
        verdict = "—"
    elif 4 * rise < window < 2e-3:
        verdict = "有効"
    else:
        verdict = "無効（エッジが画面外にはみ出す）"
    rows.append([label.split()[0], eng(st["timebase_scale"], "s/div"), eng(window, "s"), eng(st["sample_rate"], "Sa/s"),
                 eng(m.get(key), "s"), verdict,
                 "—" if m.get("OVER") is None else f"{m['OVER'] * 100:.2f} %",
                 "—" if m.get("PRES") is None else f"{m['PRES'] * 100:.2f} %"])
lab.report_add_table(rid, ["エッジ", "時間軸", "画面幅", "サンプルレート", "立上/立下時間（自動測定）", "判定", "Overshoot", "Preshoot"],
                     rows, "オシロの自動測定（RTIM / FTIM / OVER / PRES）")
lab.report_add_text(rid, """- 1 ms/div・200 µs/div では、エッジが数点しかなく立ち上がり時間は測れません（自動測定の結果も無効値）。
- **2 µs/div** でエッジ全体が画面に入り、自動測定とホスト側の計算（下記）が一致しました。
- 200 ns/div・50 ns/div ではエッジの途中しか画面に入りません。自動測定は**もっともらしいが誤った値**を返し、Overshoot も 30 % のような無意味な値になります。自動測定は、測りたい現象全体が画面に入っていることを確認してから使ってください。
- 平均化していない取り込みでは、Overshoot / Preshoot が 1〜3 % と大きめに出ています。0.5 V/div での 1 LSB（20 mV）は振幅の約 0.7 % にあたり、1 回分のノイズのピークを拾っているためです。16 回平均すると 1 % 未満に下がります（次節）。小さな行き過ぎを評価するときは、平均化と垂直方向の拡大を組み合わせてください。""")

fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
for ax, label, tr, rising in ((axes[0], "rise 2 us/div", rise, True), (axes[1], "fall 2 us/div", fall, False)):
    t, v, _ = wave(label)
    ax.plot(t * 1e6, v, color=C1, lw=0.8)
    for f in (0.1, 0.5, 0.9):
        ax.axhline(base_lv + f * amp, color="gray", lw=0.6, ls="--")
    ax.set_title(f"{'rise' if rising else 'fall'} 10-90 % = {tr * 1e6:.2f} us", fontsize=10, loc="left")
    ax.set_xlabel("time [us]")
    ax.grid(alpha=0.3)
axes[0].set_ylabel("V")
fig.tight_layout()
lab.report_add_image(rid, png(fig), "2 µs/div での立ち上がりと立ち下がり（破線は 10 / 50 / 90 % のレベル）")
lab.report_add_text(rid, f"""- ホスト側の計算では、10–90 % の立ち上がりが {rise * 1e6:.2f} µs、立ち下がりが {fall * 1e6:.2f} µs でした（サンプル間隔 20 ns）。上下でほぼ対称です。
- エッジの形は 1 次の RC 応答（時定数 τ ≈ {rise / 2.197 * 1e6:.2f} µs 相当）に近く、オーバーシュートやリンギングは見られません。
- オシロとプローブの帯域（100 MHz 級で立ち上がり数 ns）よりはるかに遅いので、この立ち上がり時間は**信号源そのものの値**です。プローブの帯域は結果に影響していません。""")

# 3. compensation
t, v, _ = wave(COMP)
fig, ax = plt.subplots(figsize=(10, 3.4))
ax.plot(t * 1e6, v, color=C1, lw=0.9)
ax.set_xlabel("time [us]")
ax.set_ylabel("V")
ax.grid(alpha=0.3)
ax.set_title("probe compensation check view (0.5 V/div, 100 us/div, 16 averages)", fontsize=10, loc="left")
fig.tight_layout()
lab.report_add_text(rid, "## 3. プローブ補正の評価")
lab.report_add_image(rid, png(fig), "一般的な補正チェック用の表示。目視では、角が丸くも尖ってもいないフラットな方形波です。")

fig, axes = plt.subplots(2, 1, figsize=(10, 6.4))
for ax, label, sign in ((axes[0], TOP, 1), (axes[1], BOTTOM, -1)):
    t, v, _ = wave(label)
    mm = plateau[label]
    sel = (t > 1e-6) & (t < 490e-6)
    ax.plot(t[sel] * 1e6, sign * (v[sel] - mm["late"]) / amp * 100, color=C1, lw=0.9)
    ax.axhspan(-0.5, 0.5, color="green", alpha=0.08, label="±0.5 %")
    ax.axhline(0, color="gray", lw=0.6)
    for a, b, name in ((20, 60, "early"), (200, 260, "mid"), (420, 480, "late")):
        ax.axvspan(a, b, color="gray", alpha=0.12)
        ax.text((a + b) / 2, 1.15, name, ha="center", fontsize=8, color="gray")
    ax.set_ylim(-1.5, 1.5)
    ax.set_xlim(0, 490)
    ax.set_ylabel("deviation [% of amplitude]")
    ax.set_title(f"{'top' if sign > 0 else 'bottom'} plateau: tilt {mm['tilt']:+.2f} %, peak {mm['peak']:+.2f} % "
                 f"(0.1 V/div, 16 avg, LSB {mm['lsb']:.0f} mV)", fontsize=10, loc="left")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower right", fontsize=8)
axes[1].set_xlabel("time after edge [us]")
fig.tight_layout()
lab.report_add_image(rid, png(fig), "平坦部を 0.1 V/div まで拡大し、終盤の値からの偏差を振幅比 [%] で示したもの。下側は符号を反転し、上側と同じ向きで比べられるようにしています。")
lab.report_add_table(rid, ["平坦部", "序盤 20–60 µs", "中盤 200–260 µs", "終盤 420–480 µs", "傾き（序盤−終盤）", "直後のピーク", "±0.5 % に収まるまで"], [
    [name, f"{m['early']:.4f} V", f"{m['mid']:.4f} V", f"{m['late']:.4f} V", f"{m['tilt']:+.2f} %", f"{m['peak']:+.2f} %",
     eng(m["settle"], "s")] for name, m in (("上側", top_m), ("下側", bot_m))
], f"傾きとピークは振幅 {amp:.2f} V に対する比。符号は「エッジ直後に行き過ぎる向き」を正としています。")
lab.report_add_text(rid, f"""### 判定: 良好（ごく軽い過補償寄り）
- **評価方法**: 正しく補正されたプローブなら、平坦部はエッジ直後から終盤まで水平になります。
    - 過補償: エッジ直後が行き過ぎ、そこから指数関数的に戻る
    - 補償不足: エッジ直後が足りず、そこから徐々に追いつく
    - 平坦部を 0.1 V/div に拡大し 16 回平均したので、1 LSB は {top_m['lsb']:.0f} mV（振幅の {top_m['lsb'] / 10 / amp:.2f} %）まで細かくなっています。
- **結果**:
    - 上側は序盤 {top_m['early']:.3f} V → 終盤 {top_m['late']:.3f} V と {abs(top_m['early'] - top_m['late']) * 1000:.0f} mV 下がっています（{top_m['tilt']:+.2f} %）。
    - 下側は {bot_m['early'] * 1000:+.0f} mV → {bot_m['late'] * 1000:+.0f} mV と戻っています（{bot_m['tilt']:+.2f} %）。
    - 上下が鏡像で、変化は序盤ほど大きい（指数関数的な減衰）ので、過補償の典型的な形です。
- **大きさ**: 傾きは 0.4〜0.5 % 程度です。0.5 V/div の通常表示では 1 LSB 未満で、目視ではまず判別できません。実用上の目安（±1〜2 %）に対して十分に小さい値です。
- **調整する場合**: 0.1 V/div・50 µs/div で上の平坦部を表示し、トリマを**エッジ直後の出っ張りが減る方向へごくわずかに**回してください。行き過ぎると、角が丸くなる方向（補償不足）に変わります。
- **注意**: 信号源の出力にカップリングコンデンサなどによるサグ（平坦部の垂れ）があると、同じ形になります。傾きのうちどこまでがプローブ由来かを切り分けるには、BNC 直結や 1× 設定でも同じ測定をして比べてください。""")

lab.report_add_text(rid, "## 参考: 本体の画面")
lab.report_add_image(rid, d["screenshot"], "測定時の本体の画面（0.5 V/div、200 µs/div）")
lab.report_add_text(rid, """## 再現方法
- 取得: `python examples/probe_evaluation/acquire.py --host <IP>`
- レポート生成: `python examples/probe_evaluation/make_report.py`
- 2 つのスクリプトは [rigol-ds1000z](https://github.com/necobit/RIGOL-1054Z-HACK) に含まれています。""")

out = lab.report_finish(rid, work / "reports")
print(out)
if args.dest:
    dest = Path(args.dest)
    shutil.copytree(out["dir"], dest, dirs_exist_ok=True)
    shutil.move(dest / "report.html", dest / "index.html")
    print("copied to", dest)
