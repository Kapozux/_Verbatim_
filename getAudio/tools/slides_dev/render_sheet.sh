#!/bin/bash
# 把一个 .pptx 渲染成两列拼图，用来肉眼检查排版：render_sheet.sh <pptx> <输出.jpg>
# 要 LibreOffice（soffice）和 pdftoppm（brew install poppler）。soffice 必须带私有 profile，不然会静默失败。
set -u
P=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); OUTJPG=$2
W=$(mktemp -d); cp "$P" "$W/d.pptx"; cd "$W"
soffice -env:UserInstallation=file:///tmp/slides_dev_lo_profile --headless --convert-to pdf d.pptx >/dev/null 2>&1
pdftoppm -jpeg -r 50 d.pdf s
PY=/Users/kapozux/Documents/CODEelse/venv/bin/python
$PY - "$OUTJPG" <<'PYEOF'
import sys, glob
from PIL import Image
ims = [Image.open(f) for f in sorted(glob.glob('s-*.jpg'))]
w, h = ims[0].size; rows = (len(ims) + 1) // 2
sheet = Image.new('RGB', (2 * w + 10, rows * (h + 6)), 'white')
for i, im in enumerate(ims): sheet.paste(im, ((i % 2) * (w + 10), (i // 2) * (h + 6)))
sheet.save(sys.argv[1], quality=85); print(sys.argv[1], len(ims), 'slides')
PYEOF
rm -rf "$W"
