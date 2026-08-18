"""
rtl_diagnostic.py — run once on your real Windows machine, look at the PNG.
Tests: raw / reshape-only / bidi-only / both (current) x font choice.
Usage:
    python rtl_diagnostic.py
    python rtl_diagnostic.py --font "Segoe UI"
"""
import argparse
import matplotlib
matplotlib.use("Agg")  # file output, no display needed
import matplotlib.pyplot as plt
import arabic_reshaper
from bidi.algorithm import get_display

TEST_STRING = "تعديل مكرر على القانون رقم ١٢٣ لسنة ٢٠٢٤"

def variant_raw(s):
    return s

def variant_reshape_only(s):
    return arabic_reshaper.reshape(s)

def variant_bidi_only(s):
    return get_display(s)

def variant_both(s):
    return get_display(arabic_reshaper.reshape(s))

VARIANTS = [
    ("1: raw (baseline)", variant_raw),
    ("2: reshape only", variant_reshape_only),
    ("3: bidi only", variant_bidi_only),
    ("4: reshape+bidi (current/broken)", variant_both),
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", default=None, help="Force a specific font, e.g. 'Segoe UI'")
    args = parser.parse_args()

    font_list = [args.font] if args.font else ["Arial", "Tahoma", "DejaVu Sans"]
    plt.rcParams["font.family"] = font_list

    fig, axes = plt.subplots(len(VARIANTS), 1, figsize=(10, 6))
    fig.suptitle(f"RTL diagnostic — font(s): {font_list}", fontsize=10)

    for ax, (label, fn) in zip(axes, VARIANTS):
        processed = fn(TEST_STRING)
        ax.text(0.5, 0.5, processed, fontsize=16, ha="center", va="center")
        ax.set_title(label, fontsize=9, loc="left")
        ax.axis("off")

    plt.tight_layout()
    out = f"rtl_diagnostic_{(args.font or 'default').replace(' ', '_')}.png"
    plt.savefig(out, dpi=150)
    print(f"Saved: {out}")
    print("Open it and note which row (1-4) reads correctly, right-to-left, "
          "letters properly joined.")

if __name__ == "__main__":
    main()
