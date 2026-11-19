# -*- coding: utf-8 -*-
"""发布前校验：任何一项不过就非 0 退出，旧产物不被覆盖。"""
import datetime
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
errors = []


def ck(cond, msg):
    print(('OK   ' if cond else 'FAIL ') + msg)
    if not cond:
        errors.append(msg)


h = json.load(io.open(os.path.join(BASE, 'history.json'), encoding='utf-8'))
days = h.get('days') or {}
asof = h.get('asof') or ''
page = io.open(os.path.join(BASE, 'docs', 'index.html'), encoding='utf-8').read()

ck(len(days) >= 800, 'history.json 至少 800 个交易日（实际 %d）' % len(days))
ck(bool(asof), '存在 asof')

if asof:
    lag = (datetime.date.today() - datetime.date.fromisoformat(asof)).days
    ck(lag <= 7, 'asof 距今 %d 天（>7 天视为过期，不发布）' % lag)

last = days.get(asof) or {}
ck((last.get('inc', 0) + last.get('dec', 0)) > 0, '末日增持+减持家数 > 0')

ck('var D=[' in page, '页面内嵌数据块存在')
ck(not re.search(r'__[A-Z]+__', page), '页面无未替换占位符')
ck('var D=' in page and 'render();' in page, '页面渲染入口存在')
ck('历史中位' in page and '50% 不是中性线' in page, '基准线口径说明存在')
ck('未定稿' in page, '滞后提示存在')
ck('staleBadge' in page and '数据已过期' in page, '过期警示存在')

hp = os.path.join(BASE, 'hs300.json')
if os.path.exists(hp):
    hsd = json.load(io.open(hp, encoding='utf-8')).get('data') or {}
    ck(len(hsd) >= 200, '沪深300 日K至少 200 根（实际 %d）' % len(hsd))
else:
    print('WARN  hs300.json 不存在（页面会标「对照暂缺」，允许发布）')
ck('沪深300 对照' in page and '__HSBADGE__' not in page, '沪深300 对照说明存在且占位符已替换')
ck('分位差' in page and 'npct' in page, '分位差字段已接入')
if os.path.exists(hp):
    m = re.search(r'var D=(\[.*?\]), PROV=', page, re.S)
    if m:
        ds = json.loads(m.group(1))
        miss = sum(1 for x in ds if x.get('px') is None)
        ck(miss == 0, '沪深300点位已 carry-forward 填满（空 %d 天）' % miss)
        ck(any(x.get('px_cf') for x in ds), '非交易日 carry-forward 标记存在')
ck('每日增持 / 减持家数' not in page, '日度柱已按要求移除')
ck('净金额趋势（估算）' in page and 's20a' in page and 'renderAmt' in page, '金额常驻窗口已接入')
ck('切换金额口径' not in page and 'amountMode' not in page, '旧的金额切换按钮已移除')
ck('未来 30 天解禁压力' in page and 'renderLift' in page and '__LIFT' not in page, '解禁卡片已接入')
ck(re.search(r'(\d+) 笔解禁 · (\d+) 个交易日', page) and re.search(r'(\d+) 笔解禁 · (\d+) 个交易日', page).group(1) != re.search(r'(\d+) 笔解禁 · (\d+) 个交易日', page).group(2), '解禁笔数与交易日数不混淆')

liftf = os.path.join(BASE, 'lift.json')
if os.path.exists(liftf):
    lf = json.load(io.open(liftf, encoding='utf-8')).get('days') or {}
    ck(len(lf) >= 10, '解禁数据至少 10 个交易日（实际 %d）' % len(lf))
else:
    print('WARN  lift.json 不存在（页面会标缺失，允许发布）')

# 凭据红线：仓库内不得出现任何密钥
all_text = ''
for root, _, files in os.walk(BASE):
    if '.git' in root:
        continue
    for fn in files:
        if fn.endswith(('.py', '.md', '.yml', '.html', '.json', '.gitignore')):
            try:
                all_text += io.open(os.path.join(root, fn), encoding='utf-8', errors='ignore').read()
            except Exception:
                pass
ck(not re.search(r'ghp_[A-Za-z0-9]{20,}|cfat_[A-Za-z0-9]{20,}', all_text), '无 GitHub / Cloudflare 令牌明文')

if errors:
    print('\n%d 项失败，停止发布' % len(errors))
    sys.exit(1)
print('\n全部校验通过')
