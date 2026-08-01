"""Regenerate results/chart_targets.svg from results.json.

Single source of truth: edit the JSON, re-run this, the README updates.

    python make_chart.py
"""
import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'results')
DATA = json.load(open(os.path.join(ROOT, 'results.json')))

INK = '#1c1f26'
MUTED = '#6b7280'
GRID = '#dfe3e8'
BG = '#fbfbfc'
OURS = '#2f6f4f'          # paid
BAD = '#a33a3a'           # did not pay


def chart_targets():
    t = DATA['targets']
    sql = t['sql_dumps']['files']
    rows = [
        ('OCI layer, zstd re-encode',
         100 * t['oci_layer']['saving_vs_shipped']['zstd -19 --long=27'], OURS),
        ('SQL dump, narrow columns', 100 * sql['chinook.sql']['gain'], OURS),
        ('SQL dump, 6 columns + timestamps', 100 * sql['wiki_meta.sql']['gain'], OURS),
        ('SQLite, page grouping',
         100 * max(v['gain'] for v in t['sqlite_pages']['results'].values()), BAD),
        ('SQL dump, parser reaches 2%', 100 * sql['wiki.sql']['gain'], BAD),
    ]
    fig, ax = plt.subplots(figsize=(8.6, 3.4))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for side in ('top', 'right', 'left'):
        ax.spines[side].set_visible(False)
    ax.spines['bottom'].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0, labelsize=9)

    y = range(len(rows))
    ax.barh(list(y), [r[1] for r in rows], color=[r[2] for r in rows], height=0.6, zorder=3)
    ax.set_yticks(list(y))
    ax.set_yticklabels([r[0] for r in rows], color=INK, fontsize=9)
    ax.invert_yaxis()
    ax.xaxis.grid(True, color=GRID, zorder=0, linewidth=0.8)
    ax.set_xlabel('% smaller than the best existing tool', color=MUTED, fontsize=9)
    ax.set_title('Four targets, three that paid', color=INK, fontsize=11.5,
                 loc='left', pad=14, fontweight='bold')
    for i, r in enumerate(rows):
        ax.text(r[1] + 0.6, i, f'{r[1]:.1f}%', va='center', color=INK,
                fontsize=9, fontweight='bold')
    ax.set_xlim(0, max(r[1] for r in rows) * 1.15)
    ax.text(0, 1.0, 'The biggest win needed no code at all - two codec flags. The bottom row is a '
                    'parser limit, not a result.',
            transform=ax.transAxes, color=MUTED, fontsize=8, va='bottom')
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'chart_targets.svg')
    fig.tight_layout()
    fig.savefig(path, format='svg', facecolor=BG, bbox_inches='tight')
    plt.close(fig)
    print('wrote', os.path.relpath(path, ROOT))


if __name__ == '__main__':
    chart_targets()
