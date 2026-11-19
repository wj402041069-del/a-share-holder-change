#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股重要股东增减持家数 · 每日构建

口径（不要随便改，README 有完整说明）：
  · 数据源：东方财富数据中心 RPT_SHARE_HOLDER_INCREASE（重要股东增减持公告）
  · 日期轴：NOTICE_DATE（公告日），不是 END_DATE（变动截止日）
  · 计数  ：按 SECURITY_CODE 去重的「公司家数」，不是记录条数、不是股东人次
  · 主指标：净家数 = 增持家数 − 减持家数；主图看 20 交易日累计净家数
  · 方向  ：只用 DIRECTION 字段（CHANGE_NUM 有「减持但数值为正」的脏数据）

产物：
  history.json      逐日家数（随仓库提交，是唯一数据资产）
  docs/index.html   单文件零外链静态看板（GitHub Pages 发布目录）
"""
import bisect
import datetime
import html
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(BASE, 'history.json')
OUT_DIR = os.path.join(BASE, 'docs')
OUT = os.path.join(OUT_DIR, 'index.html')
HS_HISTORY = os.path.join(BASE, 'hs300.json')
LIFT_HISTORY = os.path.join(BASE, 'lift.json')
LIFT_DAYS = 30        # 解禁看未来 30 天
LIFT_API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'

API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
PAGE_SIZE = 500
LOOKBACK_DAYS = 12          # 每天回看最近 12 个自然日，捞迟到的公告
MAX_HISTORY_DAYS = 5 * 366  # 保留 5 年
WIN = 20                    # 主口径：20 交易日累计

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; holder-change/1.0)',
    'Referer': 'https://data.eastmoney.com/',
}


# ---------------------------------------------------------------- 取数
def http_json(params, tries=3):
    url = API + '?' + urllib.parse.urlencode(params)
    last = ''
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                j = json.loads(resp.read().decode('utf-8'))
            if not j.get('success'):
                raise RuntimeError('success=false: ' + str(j)[:160])
            if not j.get('result'):
                raise RuntimeError('空 result: ' + str(j)[:160])
            return j
        except Exception as e:
            last = str(e)[:200]
            if k < tries - 1:
                time.sleep(2 * (k + 1))
    raise RuntimeError('东财接口失败：' + last)


def fetch_lift(today):
    """东财限售解禁：抓未来 LIFT_DAYS 天，按日聚合市值(亿元)与家数。"""
    end = today + datetime.timedelta(days=LIFT_DAYS)
    params = {
        'reportName': 'RPT_LIFT_STAGE', 'columns': 'ALL',
        'source': 'WEB', 'client': 'WEB', 'pageSize': 500, 'pageNumber': 1,
        'sortColumns': 'FREE_DATE', 'sortTypes': '1',
        'filter': "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (today.isoformat(), end.isoformat()),
    }
    rows = []
    page = 1
    while page <= 10:
        params['pageNumber'] = page
        url = LIFT_API + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=45) as resp:
            j = json.loads(resp.read().decode('utf-8'))
        r = j.get('result')
        if not r:
            raise RuntimeError('解禁接口空 result')
        rows.extend(r.get('data') or [])
        if page >= int(r.get('pages') or 1):
            break
        page += 1
        time.sleep(0.1)
    by = defaultdict(lambda: {'cap': 0.0, 'cnt': 0, 'top': None, 'top_cap': 0.0})
    for row in rows:
        d = (row.get('FREE_DATE') or '')[:10]
        if not d:
            continue
        try:
            cap = float(row.get('LIFT_MARKET_CAP') or 0) / 10000.0  # 万元 -> 亿元
        except Exception:
            cap = 0.0
        by[d]['cap'] += cap
        by[d]['cnt'] += 1
        if cap > by[d]['top_cap']:
            by[d]['top_cap'] = cap
            by[d]['top'] = row.get('SECURITY_NAME_ABBR') or row.get('SECURITY_CODE')
    return {d: {'cap': round(v['cap'], 2), 'cnt': v['cnt'],
                'top': v['top'], 'top_cap': round(v['top_cap'], 2)}
            for d, v in by.items()}, len(rows)


def load_lift():
    if not os.path.exists(LIFT_HISTORY):
        return {}
    try:
        with open(LIFT_HISTORY, encoding='utf-8') as f:
            return json.load(f).get('days') or {}
    except Exception:
        return {}


def fetch_hs300(start, end):
    """腾讯日K取沪深300（独立宿主，不经过 Worker、不需要密钥）。"""
    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    params = {'param': 'sh000300,day,%s,%s,1500,qfq' % (start, end)}
    req = urllib.request.Request(
        url + '?' + urllib.parse.urlencode(params),
        headers={'User-Agent': 'Mozilla/5.0 (compatible; holder-change/1.0)'})
    with urllib.request.urlopen(req, timeout=45) as resp:
        j = json.loads(resp.read().decode('utf-8'))
    obj = (j.get('data') or {}).get('sh000300') or {}
    arr = obj.get('qfqday') or obj.get('day') or []
    if len(arr) < 200:
        raise RuntimeError('沪深300 日K只有 %d 根，按失败处理' % len(arr))
    return {x[0]: round(float(x[2]), 2) for x in arr}


def load_hs300():
    if not os.path.exists(HS_HISTORY):
        return {}
    try:
        with open(HS_HISTORY, encoding='utf-8') as f:
            return json.load(f).get('data') or {}
    except Exception:
        return {}


def rolling_rank(values, window=250):
    """250 交易日滚动 percentile rank（0-100），只用当日及之前的数据。

    不对净家数做 min-max「归一到 100」：它有负值、均值为负，映射到 100 会失真。
    两条序列都换算成分位后，差值才是同量纲的背离读数。
    """
    out = []
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        seg = [x for x in values[max(0, i - window + 1):i + 1] if x is not None]
        out.append(round(bisect.bisect_right(sorted(seg), v) / len(seg) * 100, 1)
                   if seg else None)
    return out


def cninfo_probe(date_str):
    """巨潮资讯公告检索 —— 只做健康诊断，绝不参与出数。

    两套口径不同：东财是结构化的「重要股东增减持记录」，巨潮是「标题含增减持的公告」，
    同一天家数会不一样（实测 2026-11-19：东财 7 增/4 减，巨潮约 14 增/3 减）。
    所以只用它判断「主源返回空到底是真没公告，还是主源挂了」。
    失败一律返回 None，绝不让备源反过来把主流程搞挂。
    """
    url = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    }
    total = 0
    for kw in ('增持', '减持'):
        payload = {
            'pageNum': 1, 'pageSize': 30, 'column': 'szse', 'tabName': 'fulltext',
            'plate': '', 'stock': '', 'searchkey': kw, 'secid': '', 'category': '',
            'trade': '', 'seDate': '%s~%s' % (date_str, date_str),
            'sortName': '', 'sortType': '', 'isHLtitle': 'true',
        }
        try:
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode(payload).encode('utf-8'),
                headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=20) as resp:
                j = json.loads(resp.read().decode('utf-8'))
            total += int(j.get('totalAnnouncement') or 0)
        except Exception:
            return None          # 备源不可用时保持沉默，不影响主流程
    return total


def fetch_range(start, end):
    params = {
        'reportName': 'RPT_SHARE_HOLDER_INCREASE',
        'columns': 'ALL', 'source': 'WEB', 'client': 'WEB',
        'pageSize': PAGE_SIZE, 'pageNumber': 1,
        'sortColumns': 'NOTICE_DATE', 'sortTypes': '-1',
        'filter': "(NOTICE_DATE>='%s')(NOTICE_DATE<='%s')" % (start, end),
    }
    rows = []
    page = 1
    while page <= 300:
        params['pageNumber'] = page
        j = http_json(params)
        r = j['result']
        data = r.get('data') or []
        rows.extend(data)
        pages = int(r.get('pages') or 1)
        if page >= pages:
            break
        page += 1
        time.sleep(0.12)
    return rows


def aggregate(rows):
    """按公告日聚合：去重公司数 + 估算金额（亿元）。

    金额 = CHANGE_NUM(万股) × CLOSE_PRICE(元) ÷ 10000 = 亿元。
    CLOSE_PRICE 缺失(约3%)时该条金额按 0 计，不影响家数。
    """
    by = defaultdict(lambda: {'增持': set(), '减持': set(),
                              'inc_amt': 0.0, 'dec_amt': 0.0})
    for row in rows:
        d = (row.get('NOTICE_DATE') or '')[:10]
        direction = row.get('DIRECTION')
        code = row.get('SECURITY_CODE')
        if not d or not code or direction not in ('增持', '减持'):
            continue
        by[d][direction].add(code)
        try:
            amt = float(row.get('CHANGE_NUM') or 0) * float(row.get('CLOSE_PRICE') or 0) / 10000.0
        except Exception:
            amt = 0.0
        if amt > 0:
            by[d]['inc_amt' if direction == '增持' else 'dec_amt'] += amt
    return {d: {'inc': len(v['增持']), 'dec': len(v['减持']),
                'inc_amt': round(v['inc_amt'], 4), 'dec_amt': round(v['dec_amt'], 4)}
            for d, v in by.items()}


# ---------------------------------------------------------------- 历史
def load_history():
    if not os.path.exists(HISTORY):
        return {'schema': 1, 'asof': None, 'generated_at': None, 'days': {}}
    with open(HISTORY, encoding='utf-8') as f:
        h = json.load(f)
    h.setdefault('days', {})
    return h


def update_history(h, today):
    start = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = today.isoformat()
    rows = fetch_range(start, end)
    if not rows:
        probe = cninfo_probe(today.isoformat())
        hint = ''
        if probe is None:
            hint = '（巨潮备源也未响应，无法判断）'
        elif probe > 0:
            hint = '（巨潮同期检索到 %d 条增减持公告 → 疑似主源缺数，不是真没公告）' % probe
        else:
            hint = '（巨潮同期也无增减持公告，可能是真的无公告日）'
        raise RuntimeError('回看窗口内 0 条记录，按失败处理，绝不覆盖历史' + hint)
    fresh = aggregate(rows)
    if not fresh:
        raise RuntimeError('聚合后 0 天有效数据，按失败处理')

    changed = 0
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for d, v in fresh.items():
        old = h['days'].get(d)
        if old != v:
            changed += 1
        h['days'][d] = {'inc': v['inc'], 'dec': v['dec'],
                        'inc_amt': v.get('inc_amt', 0), 'dec_amt': v.get('dec_amt', 0),
                        'updated': stamp}

    # 保留最近 5 年，防止文件无限增长
    cutoff = (today - datetime.timedelta(days=MAX_HISTORY_DAYS)).isoformat()
    h['days'] = {k: v for k, v in h['days'].items() if k >= cutoff}
    h['asof'] = max(h['days'])
    h['generated_at'] = stamp
    h['last_raw'] = len(rows)
    return h, changed, len(rows)


def backfill_history(today):
    """一次性回填 5 年（首次建仓用，之后日常只跑 update_history）。"""
    start = (today - datetime.timedelta(days=MAX_HISTORY_DAYS)).isoformat()
    end = today.isoformat()
    print('backfill %s ~ %s ...' % (start, end), flush=True)
    rows = fetch_range(start, end)
    if len(rows) < 1000:
        raise RuntimeError('回填只拿到 %d 条，怀疑上游异常，拒绝写入' % len(rows))
    fresh = aggregate(rows)
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    h = {
        'schema': 1, 'asof': max(fresh), 'generated_at': stamp, 'last_raw': len(rows),
        'days': {d: {'inc': v['inc'], 'dec': v['dec'],
                     'inc_amt': v.get('inc_amt', 0), 'dec_amt': v.get('dec_amt', 0),
                     'updated': stamp} for d, v in fresh.items()},
    }
    return h, 0, len(rows)


# ---------------------------------------------------------------- 指标
def derive(days, hs=None):
    hs = hs or {}
    dates = sorted(days)
    out = []
    incs = []
    for d in dates:
        i = int(days[d].get('inc') or 0)
        j = int(days[d].get('dec') or 0)
        ia = float(days[d].get('inc_amt') or 0)
        ja = float(days[d].get('dec_amt') or 0)
        incs.append(i)
        w = incs[-WIN:]
        si, sj = i, j
        sia, sja = ia, ja
        for k in range(1, min(WIN, len(incs))):
            pd = dates[len(incs) - 1 - k]
            si += int(days[pd].get('inc') or 0)
            sj += int(days[pd].get('dec') or 0)
            sia += float(days[pd].get('inc_amt') or 0)
            sja += float(days[pd].get('dec_amt') or 0)
        net = i - j
        s20 = si - sj
        r20 = round(si / (si + sj) * 100, 2) if (si + sj) else None
        out.append({'d': d, 'i': i, 'j': j, 'n': net,
                    's20': s20, 'si20': si, 'sj20': sj, 'r20': r20,
                    'ia': round(ia, 3), 'ja': round(ja, 3),
                    's20a': round(sia - sja, 2),
                    'px': hs.get(d)})
    # 两条序列各自算 250 日滚动分位，差值才是同量纲背离
    npct = rolling_rank([x['s20'] for x in out])
    hs_dates = sorted(hs)
    hp_map = dict(zip(hs_dates, rolling_rank([hs[d] for d in hs_dates])))
    for idx, x in enumerate(out):
        x['npct'] = npct[idx]
        d = x['d']
        if d in hs:
            x['px'], x['hp'], x['px_cf'] = hs[d], hp_map[d], False
        else:
            # 公告日含周六与节假日，沪深300 没有这些日子的行情。
            # 用「最近一个更早交易日」的收盘与分位做 carry-forward：
            # 非交易日指数点位本来就不变，这是标准做法，不是编造数据；
            # 前端 tooltip 会标注「非交易日沿用前收盘」。
            j = bisect.bisect_right(hs_dates, d) - 1
            if j >= 0:
                dd = hs_dates[j]
                x['px'], x['hp'], x['px_cf'] = hs[dd], hp_map[dd], True
            else:
                x['px'], x['hp'], x['px_cf'] = None, None, True
    vals = sorted(x['r20'] for x in out if x['r20'] is not None)
    for x in out:
        x['pct'] = (round(bisect.bisect_right(vals, x['r20']) / len(vals) * 100, 1)
                    if x['r20'] is not None and vals else None)
    return out


# ---------------------------------------------------------------- 页面
def render(h, rows, changed, raw_count, lift=None, lift_raw=0):
    last = rows[-1]
    asof = h['asof']
    gen = h.get('generated_at') or '-'
    provisional = rows[-2:]
    prov_dates = [x['d'] for x in provisional]
    lift = lift or {}
    lift_ok = bool(lift)
    hs_ok = any(x.get('px') is not None for x in rows)
    hs_badge = ('沪深300 对照已接入' if hs_ok
                else '沪深300 对照暂缺（不影响主数据）')
    data_json = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
    lift_json = json.dumps(lift, ensure_ascii=False, separators=(',', ':'))
    lift_total = round(sum(v['cap'] for v in lift.values()), 1)
    lift_cnt = sum(v['cnt'] for v in lift.values())
    lift_peak = max(lift.items(), key=lambda kv: kv[1]['cap']) if lift else (None, {'cap': 0})
    lift_peak_day, lift_peak_val = lift_peak[0], round(lift_peak[1]['cap'], 1)
    prov_json = json.dumps(prov_dates, ensure_ascii=False)

    def jt(v):
        return '<span class="%s">%s</span>' % (
            'up' if v > 0 else ('down' if v < 0 else 'flat'),
            ('+' if v > 0 else '') + str(v))

    # 这套披露口径天然「减持多于增持」（减持多为法定强制披露，增持多为自愿），
    # 因此 50% 绝不是中性线；基准必须取本序列自己的历史中位。
    r20_all = sorted(x['r20'] for x in rows if x['r20'] is not None)
    med = statistics.median(r20_all) if r20_all else 50.0

    kpi = (
        '<div class="kpis">'
        '<div class="kpi"><div class="kl">当日净家数</div><div class="kv">%s</div><div class="ks">增 %d · 减 %d</div></div>'
        '<div class="kpi"><div class="kl">%d 日累计净家数</div><div class="kv">%s</div><div class="ks">增 %d · 减 %d</div></div>'
        '<div class="kpi"><div class="kl">%d 日增持占比</div><div class="kv">%s</div><div class="ks">历史中位 %.1f%% 为基准</div></div>'
        '<div class="kpi"><div class="kl">该占比历史分位</div><div class="kv">%s</div><div class="ks">近 %d 年</div></div>'
        '</div>'
    ) % (jt(last['n']), last['i'], last['j'],
         WIN, jt(last['s20']), last['si20'], last['sj20'],
         WIN, ('%.1f%%' % last['r20']) if last['r20'] is not None else '—', med,
         ('%.1f%%' % last['pct']) if last['pct'] is not None else '—',
         round(len(rows) / 245.0, 1))

    tpl_path = os.path.join(BASE, 'template.html')
    with open(tpl_path, encoding='utf-8') as f:
        tpl = f.read()
    return tpl.replace('__KPI__', kpi) \
                    .replace('__DATA__', data_json) \
                    .replace('__PROV__', prov_json) \
                    .replace('__HSBADGE__', hs_badge) \
                    .replace('__LIFT__', lift_json) \
                    .replace('__LIFTTOTAL__', str(lift_total)) \
                    .replace('__LIFTCOUNT__', str(lift_cnt)) \
                    .replace('__LIFTDAY__', str(lift_peak_day)) \
                    .replace('__LIFTVAL__', str(lift_peak_val)) \
                    .replace('__LIFTRAW__', str(lift_raw)) \
                    .replace('__ASOF__', html.escape(asof)) \
                    .replace('__GEN__', html.escape(gen)) \
                    .replace('__WIN__', str(WIN)) \
                    .replace('__ROWS__', str(len(rows))) \
                    .replace('__CHANGED__', str(changed)) \
                    .replace('__RAW__', str(raw_count))


# ---------------------------------------------------------------- main
def main():
    today = datetime.date.today()
    if '--backfill' in sys.argv:
        h, changed, raw_count = backfill_history(today)
    else:
        h = load_history()
        if len(h['days']) < 60:
            raise RuntimeError('本地 history.json 不足 60 天，请先跑 --backfill')
        h, changed, raw_count = update_history(h, today)
    # 沪深300 对照：抓新失败就沿用本地缓存；都没有就留空、页面标「对照暂缺」，
    # 绝不能因为对照源挂了而阻断主数据发布。
    hs = load_hs300()
    hs_fresh = False
    hs_start = (today - datetime.timedelta(days=MAX_HISTORY_DAYS)).isoformat()
    try:
        fresh = fetch_hs300(hs_start, today.isoformat())
        if len(fresh) >= 200:
            hs = fresh
            hs_fresh = True
    except Exception as e:
        print('沪深300 抓取失败，沿用本地缓存：' + str(e)[:140])

    # 解禁：抓新失败沿用本地 lift.json 缓存
    lift = load_lift()
    lift_fresh = False
    lift_raw = 0
    try:
        fresh_lift, lift_raw = fetch_lift(today)
        if fresh_lift:
            lift = fresh_lift
            lift_fresh = True
    except Exception as e:
        print('解禁抓取失败，沿用本地缓存：' + str(e)[:140])

    rows = derive(h['days'], hs)
    if len(rows) < 60:
        raise RuntimeError('有效交易日不足 60，拒绝覆盖页面')
    os.makedirs(OUT_DIR, exist_ok=True)
    page = render(h, rows, changed, int(h.get('last_raw') or raw_count), lift, lift_raw)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        f.write(page)
    with open(HISTORY, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(h, f, ensure_ascii=False, indent=1, sort_keys=True)
    if lift_fresh or not os.path.exists(LIFT_HISTORY):
        with open(LIFT_HISTORY, 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'asof': today.isoformat(),
                       'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'days': lift}, f, ensure_ascii=False, indent=1, sort_keys=True)
    if hs_fresh or not os.path.exists(HS_HISTORY):
        with open(HS_HISTORY, 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'asof': max(hs) if hs else None,
                       'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'data': hs}, f, ensure_ascii=False, indent=1, sort_keys=True)
    print('asof %s | 交易日 %d | 原始公告 %d 条 | 变化 %d 天 | 当日 增%d/减%d | %d日净 %+d | 占比 %s'
          % (h['asof'], len(rows), raw_count, changed,
             rows[-1]['i'], rows[-1]['j'], WIN, rows[-1]['s20'],
             ('%.2f%%' % rows[-1]['r20']) if rows[-1]['r20'] is not None else '—'))
    print('沪深300 对照 %d 天（%s）'
          % (sum(1 for x in rows if x.get('px') is not None),
             '本次新抓' if hs_fresh else '沿用缓存' if hs else '缺失'))
    _lc = sum(v['cnt'] for v in lift.values())
    _lt = round(sum(v['cap'] for v in lift.values()), 1)
    print('解禁未来%d天 %d 条 / %.1f 亿元（%s）'
          % (LIFT_DAYS, _lc, _lt, '本次新抓' if lift_fresh else '沿用缓存' if lift else '缺失'))
    print('written', OUT, os.path.getsize(OUT), 'bytes')


if __name__ == '__main__':
    main()
