"""
gen_tags.py
===========
Generate AprilTag PNG images for HackTheSkies, in the same family the game
detects (tag_detector.TAG_FAMILY, currently tag25h9 -> IDs 0..34).

Renders from the pupil_apriltags internal family struct, then runs the
detector on each image to verify it decodes.

Usage:
    python gen_tags.py                   # players 1-10 -> ./tags/
    python gen_tags.py --ids 1 2         # specific player IDs only
    python gen_tags.py --size 2400       # print resolution (default 400px)
    python gen_tags.py --pdf             # print-ready PDF, front + back per player

Printing bibs: use the largest size people can wear (A3 / tabloid), keep the
white border, mount on card so it stays flat, and use matte paper.

Testing without a printer:
    Open the PNGs on a phone/monitor so the WHOLE image is visible, including
    the white border on all four sides, and aim your camera at them.
"""
from __future__ import annotations

import argparse
import ctypes
import pathlib
import sys

import cv2
import numpy as np
import pupil_apriltags as apriltag

from tag_detector import TAG_FAMILY


# ---------------------------------------------------------------------------
# Tag rendering (classic AprilTag families: tag16h5 / tag25h9 / tag36h11)
#
# The code word is packed into a uint64 (nbits bits, e.g. 25 for tag25h9).
# Bit i (MSB first) goes at (bit_x[i], bit_y[i]) from the C struct.  Those
# coordinates are 1-indexed within the data area, i.e. relative to the black
# border ring:
#
#   [quiet][quiet]...[quiet]               <- 1 row of white (quiet zone)
#   [quiet][border]...[border][quiet]      <- black border ring
#   [quiet][border][data NxN][border][quiet]
#   [quiet][border]...[border][quiet]
#   [quiet][quiet]...[quiet]
#
# total_width = quiet + border + data + border + quiet (9 for tag25h9).
# A '1' bit = WHITE cell, '0' bit = BLACK cell  (per the apriltag C source).
# ---------------------------------------------------------------------------

def make_tag_image(tag_id: int, cell_px: int = 50, quiet_extra: int = 2,
                   family: str = TAG_FAMILY) -> np.ndarray:
    """
    Render an AprilTag as a grayscale numpy image.

    Parameters
    ----------
    tag_id     : int  Tag ID, 0..(ncodes-1) for the family
    cell_px    : int  Pixels per cell
    quiet_extra: int  Extra quiet-zone cells beyond the built-in 1 (default 2)
    family     : str  AprilTag family (default: the one the game detects)

    Returns
    -------
    np.ndarray  grayscale uint8, shape (H, W)
    """
    det = apriltag.Detector(families=family)
    tf  = det.tag_families[family]
    c   = tf.contents

    if tag_id >= c.ncodes:
        raise ValueError(f'{family} has {c.ncodes} codes; max tag_id={c.ncodes-1}')

    # Read code word
    CodesType = ctypes.c_uint64 * c.ncodes
    codes_arr = CodesType.from_address(ctypes.addressof(c.codes.contents))
    code  = int(codes_arr[tag_id])
    nbits = int(c.nbits)

    total_width     = int(c.total_width)
    width_at_border = int(c.width_at_border)
    built_in_quiet  = (total_width - width_at_border) // 2   # 1
    border_w        = 1                         # 1 for the classic families

    # Offset from total_width grid edge to first data column:
    # quiet + border = built_in_quiet + border_w
    data_offset = built_in_quiet + border_w    # = 2

    # Full grid size including extra quiet zone
    ew = total_width + 2 * quiet_extra          # extended width in cells
    eq = built_in_quiet + quiet_extra           # extended quiet zone

    W = cell_px * ew
    img = np.ones((W, W), dtype=np.uint8) * 255   # white

    # Draw black border ring
    for row in range(ew):
        for col in range(ew):
            in_active = (eq <= row < ew - eq and eq <= col < ew - eq)
            on_border = in_active and (
                row == eq or row == ew - eq - 1 or
                col == eq or col == ew - eq - 1
            )
            if on_border:
                img[row*cell_px:(row+1)*cell_px, col*cell_px:(col+1)*cell_px] = 0

    # Draw data bits using bit_x / bit_y from the struct.
    # bit_x[i]/bit_y[i] are 1-indexed within the data area.
    # Extended-grid column = quiet_extra + data_offset + (bit_x[i] - 1)
    bx = c.bit_x
    by = c.bit_y
    eoff = quiet_extra + data_offset           # extended grid offset to data area

    for i in range(nbits):
        col = eoff + (int(bx[i]) - 1)
        row = eoff + (int(by[i]) - 1)
        # MSB-first; '1' = WHITE, '0' = BLACK  (apriltag convention)
        bit_val = (code >> (nbits - 1 - i)) & 1
        colour  = 255 if bit_val else 0
        img[row*cell_px:(row+1)*cell_px, col*cell_px:(col+1)*cell_px] = colour

    return img


def family_width(family: str = TAG_FAMILY) -> int:
    """The family's total_width in cells (9 for tag25h9).  The detector must stay
    alive while its family struct is read: it frees its families when it goes."""
    det = apriltag.Detector(families=family)
    return int(det.tag_families[family].contents.total_width)


def verify_tag(img_gray: np.ndarray, expected_id: int, family: str = TAG_FAMILY) -> bool:
    """Run the detector on the generated image and confirm it decodes correctly."""
    det = apriltag.Detector(families=family, quad_decimate=1.0)
    results = det.detect(img_gray)
    ids = [r.tag_id for r in results]
    return expected_id in ids


def add_label(img_gray: np.ndarray, label: str, colour_bgr=(50, 50, 50)) -> np.ndarray:
    bgr   = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2BGR)
    h, w  = bgr.shape[:2]
    bar_h = max(44, h // 7)
    bar   = np.full((bar_h, w, 3), colour_bgr, dtype=np.uint8)
    scale = max(0.5, bar_h / 55.0)
    cv2.putText(bar, label, (12, bar_h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, scale, (230, 230, 230),
                max(1, int(scale * 1.8)), cv2.LINE_AA)
    return np.vstack([bgr, bar])


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def tag_label(tag_id: int) -> str:
    if 1 <= tag_id <= 99:
        return f'Player {tag_id}  ({TAG_FAMILY} ID {tag_id})'
    if tag_id == 100:
        return 'Red Base  (tag ID 100)'
    if tag_id == 101:
        return 'Blue Base  (tag ID 101)'
    if tag_id >= 200:
        return f'Flag {tag_id - 200}  (tag ID {tag_id})'
    return f'Tag {tag_id}'


def tag_filename(tag_id: int) -> str:
    if 1 <= tag_id <= 99:
        return f'player_{tag_id:02d}.png'
    if tag_id == 100:
        return 'base_red.png'
    if tag_id == 101:
        return 'base_blue.png'
    if tag_id >= 200:
        return f'flag_{tag_id - 200}.png'
    return f'tag_{tag_id}.png'


def bar_colour(tag_id: int) -> tuple:
    if tag_id == 100:
        return (0, 0, 140)
    if tag_id == 101:
        return (140, 0, 0)
    if tag_id >= 200:
        return (0, 80, 0)
    return (50, 50, 50)


def write_print_pdf(ids: list[int], path: pathlib.Path, dpi: int = 300) -> None:
    """
    US Letter PDF, one tag per page, two pages per player (FRONT + BACK bib).
    The tag fills the page width minus margins; print at 100% / "actual size".
    """
    from PIL import Image, ImageDraw, ImageFont

    page_w, page_h = int(8.5 * dpi), int(11 * dpi)
    ew      = family_width() + 2 * 2
    cell_px = (page_w - dpi // 2) // ew               # ~0.25in side margins
    try:
        font = ImageFont.truetype('arial.ttf', dpi // 3)
    except OSError:
        font = ImageFont.load_default(size=dpi // 3)

    pages = []
    for tag_id in ids:
        gray = make_tag_image(tag_id, cell_px=cell_px, quiet_extra=2)
        if not verify_tag(gray, tag_id):
            print(f'  WARN: tag {tag_id} did not verify')
        tag = Image.fromarray(gray)
        for side in ('FRONT', 'BACK'):
            page = Image.new('L', (page_w, page_h), 255)
            x = (page_w - tag.width) // 2
            page.paste(tag, (x, dpi // 2))
            draw = ImageDraw.Draw(page)
            text = f'Player {tag_id}  -  {side}'
            tw = draw.textlength(text, font=font)
            draw.text(((page_w - tw) // 2, dpi // 2 + tag.height + dpi // 4),
                      text, fill=0, font=font)
            pages.append(page.convert('RGB'))

    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=dpi)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # UTF-8 output on Windows terminals (done here, not at import, so importing
    # this module never swaps out the caller's stdout)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Generate AprilTag PNGs for HackTheSkies')
    parser.add_argument('--ids',  nargs='+', type=int, default=None)
    parser.add_argument('--size', type=int,  default=400,
                        help='Output image size in pixels (default 400)')
    parser.add_argument('--out',  type=str,  default='tags')
    parser.add_argument('--verify', action='store_true', default=True,
                        help='Run detector on each generated image to verify (default on)')
    parser.add_argument('--pdf', action='store_true',
                        help='Write a print-ready PDF instead (2 pages per player: front + back)')
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(exist_ok=True)

    ids = args.ids if args.ids else list(range(1, 11))   # players 1-10

    if args.pdf:
        path = out_dir / 'print_tags.pdf'
        print(f'Writing {len(ids) * 2}-page PDF ({TAG_FAMILY}, players {ids}) ...')
        write_print_pdf(ids, path)
        print(f'Done: {path.resolve()}')
        print('Print at 100% / "Actual size" (not "Fit"), matte paper, mount on card.')
        return 0

    # Pick cell_px so we get roughly args.size pixels total
    # (family total_width + 2 extra quiet cells per side, as in make_tag_image)
    ew      = family_width() + 2 * 2
    cell_px = max(10, args.size // ew)

    print(f'Generating {len(ids)} {TAG_FAMILY} tag(s) -> {out_dir.resolve()}/')
    print(f'Cell size: {cell_px}px  |  Approx image size: {cell_px*ew}x{cell_px*ew}px')
    print()

    ok_count = err_count = 0

    for tag_id in ids:
        label    = tag_label(tag_id)
        filename = tag_filename(tag_id)
        path     = out_dir / filename

        print(f'  [{tag_id:3d}]  {label:38s}  ', end='', flush=True)
        try:
            gray = make_tag_image(tag_id, cell_px=cell_px, quiet_extra=2)
            ok   = verify_tag(gray, tag_id)
            img  = add_label(gray, label, colour_bgr=bar_colour(tag_id))
            cv2.imwrite(str(path), img)
            status = 'OK (verified)' if ok else 'WARN: detector did not verify -- may still work on camera'
            print(status)
            ok_count += 1
        except Exception as exc:
            print(f'FAILED: {exc}')
            err_count += 1

    print()
    print(f'Done: {ok_count} OK, {err_count} failed')
    print()
    print('Testing without a printer:')
    print('  Open the PNGs on a monitor or second phone/tablet so the WHOLE image shows.')
    print('  The white border must be visible on all four sides of the black square.')
    print('  Aim your game phone camera directly at the screen, in good lighting.')
    print()
    return err_count


if __name__ == '__main__':
    sys.exit(main())
