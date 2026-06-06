#!/usr/bin/env python3
"""
深圳房地产数据抓取与分析系统 v3.0
- 预售许可证列表 + 项目详情 + 楼栋信息 + 房源价格
数据来源: 深圳市房地产信息平台 (fdc.zjj.sz.gov.cn)
使用方法: python3 server.py
访问: http://localhost:8080
"""

import http.server
import json
import urllib.request
import urllib.error
import urllib.parse
import os
import sys
import re
import threading
import time
import sqlite3
import ssl
from datetime import datetime

API_SZFDC = 'https://fdc.zjj.sz.gov.cn/szfdcscjy'
PORT = 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SCRIPT_DIR, 'szfdc_data.db')
DATA_DIR = os.path.join(SCRIPT_DIR, 'data')
HTML_FILE = os.path.join(SCRIPT_DIR, 'index.html')
FLOORPLANS_DIR = os.path.join(SCRIPT_DIR, 'floorplans')
os.makedirs(FLOORPLANS_DIR, exist_ok=True)

# Auto-assemble DB from split parts if needed
if not os.path.exists(DB_FILE) and os.path.exists(DATA_DIR):
    print('数据库未找到，正在从分片组装...')
    parts = sorted([f for f in os.listdir(DATA_DIR) if f.startswith('szfdc_data_part_')])
    if parts:
        with open(DB_FILE, 'wb') as out:
            for p in parts:
                with open(os.path.join(DATA_DIR, p), 'rb') as f:
                    out.write(f.read())
        print(f'✅ 数据库组装完成 ({os.path.getsize(DB_FILE)/1024/1024:.0f}MB)')
OPENDATA_KEY = 'c8d3e7c16e9a432a8b14af89113062e2'
OPENDATA_API = 'https://opendata.sz.gov.cn/api/29200_01903510/1/service.xhtml'
OPENDATA_KEY2 = 'e5d7c1a6564e428c89434bd860c5a5c6'
OPENDATA_API2 = 'https://opendata.sz.gov.cn/api/29200_01903513/1/service.xhtml'

szfdc_cookies = ''
szfdc_lock = threading.Lock()
db_lock = threading.Lock()

_ssl_ctx = None

def get_ssl_context():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = ssl.CERT_NONE
        _ssl_ctx.options |= 0x4  # SSL_OP_LEGACY_SERVER_CONNECT for Python 3.13+
    return _ssl_ctx


def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS presale_list (
            id TEXT PRIMARY KEY, sypId TEXT, sypeId TEXT, zone TEXT,
            strpreprojectid TEXT, project TEXT, name TEXT,
            siteaddress TEXT, passdate TEXT, imagePath TEXT,
            raw_json TEXT, fetched_at TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_meta (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS houses (
            house_id INTEGER PRIMARY KEY, sypeId TEXT, fybId TEXT,
            buildingName TEXT, buildingbranch TEXT, floor TEXT,
            housenb TEXT, useage TEXT, ysbuildingarea REAL,
            ysinsidearea REAL, ysexpandarea REAL,
            askpriceeachB REAL, askpricetotalB REAL,
            recordedPricePerUnitInside REAL,
            lastStatusName TEXT, color TEXT,
            raw_json TEXT, first_seen TEXT, last_updated TEXT
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_zone ON presale_list(zone)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_passdate ON presale_list(passdate)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_project ON presale_list(project)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_houses_sype ON houses(sypeId)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_houses_fyb ON houses(fybId)')
        c.execute('''CREATE TABLE IF NOT EXISTS floorplan_layouts (
            sypeId TEXT PRIMARY KEY,
            image_path TEXT,
            layout_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )''')
        conn.commit()
        conn.close()


def refresh_session():
    global szfdc_cookies
    try:
        req = urllib.request.Request(
            f'{API_SZFDC}/',
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml',
            }
        )
        with urllib.request.urlopen(req, timeout=10, context=get_ssl_context()) as resp:
            cookies = resp.getheader('Set-Cookie', '')
            all_cookies = []
            for part in re.split(r',(?=\s*\w+=)', cookies):
                cp = part.split(';')[0].strip()
                if cp: all_cookies.append(cp)
            with szfdc_lock:
                szfdc_cookies = '; '.join(all_cookies)
            return True
    except Exception as e:
        print(f'[session] 刷新失败: {e}')
        return False


def http_fetch(url, body=None, content_type='application/json', max_retries=2):
    """Generic HTTP POST to the government API"""
    global szfdc_cookies
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Content-Type': content_type,
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'{API_SZFDC}/#/presaleHouse/presaleHouseDetail',
        'Origin': 'https://fdc.zjj.sz.gov.cn',
    }

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            with szfdc_lock:
                if szfdc_cookies:
                    req.add_header('Cookie', szfdc_cookies)
            with urllib.request.urlopen(req, timeout=20, context=get_ssl_context()) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < max_retries - 1:
                refresh_session()
                continue
            err_body = e.read().decode(errors='ignore')[:300]
            return None, f'HTTP {e.code}: {err_body}'
        except Exception as e:
            if attempt < max_retries - 1:
                refresh_session()
                continue
            return None, str(e)
    return None, 'Max retries exceeded'


# ---- Cache operations ----

def fetch_all_pages(page_size=100, max_pages=50):
    zone, keyword, total = '', '', 0
    all_items = []
    for page in range(1, max_pages + 1):
        body = json.dumps({
            'pageIndex': page, 'pageSize': page_size,
            'total': total, 'zone': zone, 'keyword': keyword
        }).encode()
        raw, err = http_fetch(f'{API_SZFDC}/ysf/publicity/getYsfYsPublicity', body)
        if err: break
        data = json.loads(raw)
        if data.get('status') != 200: break
        d = data.get('data', {})
        total = d.get('total', 0)
        items = d.get('list', [])
        all_items.extend(items)
        print(f'[fetch] Page {page}: {len(items)} items (total {total})')
        if len(items) < page_size: break
        time.sleep(0.3)
    return all_items


def save_to_cache(items):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now = datetime.now().isoformat()
        for item in items:
            c.execute('''INSERT OR REPLACE INTO presale_list
                (id,sypId,sypeId,zone,strpreprojectid,project,name,siteaddress,passdate,imagePath,raw_json,fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (
                item.get('id', ''), item.get('sypId', ''), item.get('sypeId', ''),
                item.get('zone', ''), item.get('strpreprojectid', ''), item.get('project', ''),
                item.get('name', ''), item.get('siteaddress', ''), item.get('passdate', ''),
                item.get('imagePath', ''), json.dumps(item, ensure_ascii=False), now))
        c.execute('INSERT OR REPLACE INTO cache_meta VALUES (?,?,?)',
                  ('last_cache_update', now, now))
        conn.commit()
        conn.close()
    print(f'[cache] Saved {len(items)} items')


def get_cached_stats(dateFrom='', dateTo=''):
    """Get stats, optionally filtered by date range"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        params = []
        date_filter = ''
        if dateFrom:
            date_filter += ' AND passdate >= ?'
            params.append(dateFrom)
        if dateTo:
            date_filter += ' AND passdate <= ?'
            params.append(dateTo)

        c.execute(f'SELECT COUNT(*) FROM presale_list WHERE 1=1 {date_filter}', params)
        total = c.fetchone()[0]
        c.execute(f'SELECT zone, COUNT(*) as cnt FROM presale_list WHERE 1=1 {date_filter} GROUP BY zone ORDER BY cnt DESC', params)
        zones = [{'zone': r[0], 'count': r[1]} for r in c.fetchall()]
        c.execute('SELECT MAX(fetched_at) FROM presale_list')
        last_update = c.fetchone()[0] or ''
        c.execute(f'''SELECT substr(passdate,1,7) as month, COUNT(*) as cnt
                     FROM presale_list WHERE passdate!='' {date_filter}
                     GROUP BY month ORDER BY month DESC LIMIT 12''', params)
        monthly = [{'month': r[0], 'count': r[1]} for r in c.fetchall()]
        conn.close()
    return {'total': total, 'zones': zones, 'last_update': last_update, 'monthly': monthly}


def get_cached_list(page=1, page_size=12, zone='', keyword=''):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        conditions, params = [], []
        if zone:
            conditions.append('zone=?'); params.append(zone)
        if keyword:
            conditions.append('(project LIKE ? OR name LIKE ? OR strpreprojectid LIKE ? OR siteaddress LIKE ?)')
            kw = f'%{keyword}%'; params.extend([kw, kw, kw, kw])
        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        c.execute(f'SELECT COUNT(*) FROM presale_list {where}', params)
        total = c.fetchone()[0]
        offset = (page - 1) * page_size
        c.execute(f'SELECT raw_json FROM presale_list {where} ORDER BY passdate DESC, id DESC LIMIT ? OFFSET ?',
                  params + [page_size, offset])
        items = [json.loads(r[0]) for r in c.fetchall()]
        conn.close()
    return {'total': total, 'list': items, 'pageIndex': page, 'pageSize': page_size}


def save_houses_to_db(sypeId, houses):
    """Save houses to DB, preserving original prices if API returns null"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now = datetime.now().isoformat()
        saved = 0
        for h in houses:
            house_id = h.get('id')
            if not house_id: continue
            # Check existing record to preserve prices
            c.execute('SELECT askpriceeachB, askpricetotalB, first_seen FROM houses WHERE house_id=?', (house_id,))
            existing = c.fetchone()
            up = h.get('askpriceeachB')
            tp = h.get('askpricetotalB')
            rp = h.get('recordedPricePerUnitInside')
            # If API returns null/zero but DB has a value, keep DB value
            if existing and (not up or up == 0) and existing[0]:
                up = existing[0]
            if existing and (not tp or tp == 0) and existing[1]:
                tp = existing[1]
            c.execute('''INSERT OR REPLACE INTO houses
                (house_id,sypeId,fybId,buildingName,buildingbranch,floor,housenb,useage,
                 ysbuildingarea,ysinsidearea,ysexpandarea,
                 askpriceeachB,askpricetotalB,recordedPricePerUnitInside,
                 lastStatusName,color,raw_json,first_seen,last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                house_id, sypeId, h.get('fybId', ''), h.get('buildingName', ''),
                h.get('buildingbranch', ''), h.get('floor', ''),
                h.get('housenb', ''), h.get('useage', ''),
                h.get('ysbuildingarea', 0), h.get('ysinsidearea', 0),
                h.get('ysexpandarea', 0), up, tp, rp,
                h.get('lastStatusName', ''), h.get('color', ''),
                json.dumps(h, ensure_ascii=False),
                existing[2] if existing else now if existing else now, now))
            saved += 1
        conn.commit()
        conn.close()
    return saved


def get_houses_from_db(sypeId, fybId=None):
    """Get houses from DB with preserved prices"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        params = [sypeId]
        if fybId:
            c.execute('SELECT raw_json FROM houses WHERE sypeId=? AND fybId=? ORDER BY floor DESC, housenb ASC', params + [fybId])
        else:
            c.execute('SELECT raw_json FROM houses WHERE sypeId=? ORDER BY floor DESC, housenb ASC', params)
        houses = [json.loads(r[0]) for r in c.fetchall()]
        # Override prices with DB values (in case raw_json has null)
        # Actually raw_json IS from API, need to re-query specific columns
        conn.close()
    return houses


def get_houses_with_prices(sypeId, fybId=None):
    """Get houses with price-protected data"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        params = [sypeId]
        if fybId:
            c.execute('''SELECT
                house_id, sypeId, fybId, buildingName, buildingbranch, floor,
                housenb, useage, ysbuildingarea, ysinsidearea, ysexpandarea,
                askpriceeachB, askpricetotalB, recordedPricePerUnitInside,
                lastStatusName, color
                FROM houses WHERE sypeId=? AND fybId=? ORDER BY floor DESC, housenb ASC''', params + [fybId])
        else:
            c.execute('''SELECT
                house_id, sypeId, fybId, buildingName, buildingbranch, floor,
                housenb, useage, ysbuildingarea, ysinsidearea, ysexpandarea,
                askpriceeachB, askpricetotalB, recordedPricePerUnitInside,
                lastStatusName, color
                FROM houses WHERE sypeId=? ORDER BY floor DESC, housenb ASC''', params)
        rows = c.fetchall()
        cols = ['id','sypeId','fybId','buildingName','buildingbranch','floor',
                'housenb','useage','ysbuildingarea','ysinsidearea','ysexpandarea',
                'askpriceeachB','askpricetotalB','recordedPricePerUnitInside',
                'lastStatusName','color']
        houses = [dict(zip(cols, r)) for r in rows]
        conn.close()
    return houses


def fetch_and_cache_houses(sypeId, fybId, ysProjectId):
    """Fetch houses from API, merge with DB prices, save to DB, return merged data"""
    body = json.dumps({
        'fybId': str(fybId), 'preSellId': str(sypeId),
        'ysProjectId': str(ysProjectId), 'status': -1, 'floor': '', 'buildingbranch': ''
    }).encode()
    raw, err = http_fetch(f'{API_SZFDC}/projectPublish/getHouseInfoListToPublicity', body)
    if err: return None, err

    data = json.loads(raw)
    if data.get('status') != 200: return None, data.get('msg', 'Unknown error')

    # Flatten houses and tag with fybId
    all_houses = []
    for f in data.get('data', []):
        for h in f.get('list', []):
            h['fybId'] = str(fybId)
            all_houses.append(h)

    # Save to DB (price protection happens in save)
    save_houses_to_db(sypeId, all_houses)

    # Return price-protected data
    return get_houses_with_prices(sypeId, fybId), None


def get_enriched_projects(page=1, page_size=12, zone='', keyword=''):
    base = get_cached_list(page, page_size, zone, keyword)
    enriched = []
    for item in base['list']:
        sid = item.get('sypeId', '')
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''SELECT useage, COUNT(*) as cnt, 
                          ROUND(MIN(ysbuildingarea),1) as min_area,
                          ROUND(MAX(ysbuildingarea),1) as max_area,
                          ROUND(MIN(askpricetotalB)/10000,0) as min_total,
                          ROUND(MAX(askpricetotalB)/10000,0) as max_total
                          FROM houses WHERE sypeId=? AND askpricetotalB > 0
                          GROUP BY useage ORDER BY cnt DESC''', (sid,))
            usage_stats = [{'useage': r[0], 'count': r[1], 'minArea': r[2], 'maxArea': r[3], 'minTotal': r[4], 'maxTotal': r[5]} for r in c.fetchall()]
            c.execute('SELECT COUNT(*), SUM(CASE WHEN lastStatusName IN ("已备案","已签认购书","已录入合同","已签合同") THEN 1 ELSE 0 END) FROM houses WHERE sypeId=?', (sid,))
            row = c.fetchone()
            total_units = row[0] if row else 0
            sold_units = row[1] if row and row[1] else 0
            conn.close()
        item['usageStats'] = usage_stats
        item['totalUnits'] = total_units
        item['soldUnits'] = sold_units
        item['absorptionRate'] = round(sold_units / total_units * 100, 1) if total_units else 0
        enriched.append(item)
    return {'total': base['total'], 'list': enriched, 'pageIndex': page, 'pageSize': page_size}


def get_all_zones():
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT DISTINCT zone FROM presale_list ORDER BY zone')
        zones = [r[0] for r in c.fetchall()]
        conn.close()
    return zones


def get_zone_overview():
    """Get per-zone market overview"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Per zone: total projects, total units, avg price, sold units, recent supply
        c.execute('''SELECT zone, COUNT(DISTINCT p.sypeId) as projects,
                     COUNT(h.house_id) as total_units,
                     ROUND(AVG(CASE WHEN h.askpriceeachB>0 THEN h.askpriceeachB END),0) as avg_price,
                     SUM(CASE WHEN h.lastStatusName IN ("已备案","已签认购书","已录入合同","已签合同") THEN 1 ELSE 0 END) as sold,
                     COUNT(h.house_id) as with_price
                     FROM presale_list p LEFT JOIN houses h ON p.sypeId=h.sypeId
                     WHERE p.passdate >= date("now","-2 years")
                     GROUP BY zone ORDER BY total_units DESC''')
        zones = [{'zone':r[0],'projects':r[1],'totalUnits':r[2],'avgPrice':r[3],'sold':r[4],'withPrice':r[5]} for r in c.fetchall()]
        conn.close()
    return zones


def get_floor_price_data(sypeId, fybId=None, useage=None):
    """Get floor-level price correlation for a project"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        q = '''SELECT floor, housenb, ROUND(AVG(askpriceeachB),0) as avg_unit_price,
               ROUND(AVG(askpricetotalB)/10000,0) as avg_total_price,
               ROUND(AVG(ysbuildingarea),1) as avg_area
               FROM houses WHERE sypeId=? AND askpriceeachB>0'''
        params = [sypeId]
        if fybId:
            q += ' AND fybId=?'
            params.append(fybId)
        if useage and useage != 'all':
            q += ' AND useage=?'
            params.append(useage)
        q += ' GROUP BY floor, housenb ORDER BY CAST(floor AS INTEGER) DESC, housenb ASC'
        c.execute(q, params)
        data = [{'floor':r[0],'room':r[1],'unitPrice':r[2],'totalPrice':r[3],'area':r[4]} for r in c.fetchall()]
        conn.close()
    return data


def get_comparison_data(sypeIds):
    """Get comparison data for multiple projects"""
    result = []
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for sid in sypeIds:
            # Project basic info
            c.execute('SELECT project, zone, passdate, strpreprojectid FROM presale_list WHERE sypeId=?', (sid,))
            proj = c.fetchone()
            if not proj: continue
            # Usage stats
            c.execute('''SELECT useage, COUNT(*), ROUND(MIN(ysbuildingarea),1), ROUND(MAX(ysbuildingarea),1),
                         ROUND(MIN(askpricetotalB)/10000,0), ROUND(MAX(askpricetotalB)/10000,0),
                         ROUND(AVG(askpriceeachB),0)
                         FROM houses WHERE sypeId=? AND askpricetotalB>0
                         GROUP BY useage ORDER BY COUNT(*) DESC''', (sid,))
            usages = [{'useage':r[0],'count':r[1],'minArea':r[2],'maxArea':r[3],'minTotal':r[4],'maxTotal':r[5],'avgPrice':r[6]} for r in c.fetchall()]
            # Status distribution
            c.execute('''SELECT lastStatusName, COUNT(*) FROM houses WHERE sypeId=?
                         GROUP BY lastStatusName ORDER BY COUNT(*) DESC''', (sid,))
            statuses = [{'status':r[0],'count':r[1]} for r in c.fetchall()]
            # Total
            c.execute('SELECT COUNT(*), SUM(CASE WHEN lastStatusName IN ("已备案","已签认购书","已录入合同","已签合同") THEN 1 ELSE 0 END) FROM houses WHERE sypeId=?', (sid,))
            tot,sold = c.fetchone()
            result.append({
                'sypeId':sid,'project':proj[0],'zone':proj[1],'date':proj[2],'permit':proj[3],
                'usages':usages,'statuses':statuses,'totalUnits':tot or 0,'soldUnits':sold or 0,
                'absorptionRate':round(sold/tot*100,1) if tot else 0
            })
        conn.close()
    return result


# ---- HTTP Handler ----

cache_progress = {'running': False, 'total': 0, 'current': 0, 'message': '', 'houses_cached': 0}
cache_lock = threading.Lock()


def batch_cache_all_houses():
    global cache_progress
    with cache_lock:
        if cache_progress['running']: return
        cache_progress = {'running': True, 'total': 0, 'current': 0, 'message': '准备中...', 'houses_cached': 0}
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            eight_years_ago = (datetime.now().replace(year=datetime.now().year - 3)).strftime('%Y-%m-%d')
            c.execute("SELECT DISTINCT sypeId, project, zone FROM presale_list WHERE passdate >= ? ORDER BY passdate DESC", (eight_years_ago,))
            projects = [{'sypeId':r[0],'project':r[1],'zone':r[2]} for r in c.fetchall()]
            conn.close()
        total = len(projects)
        with cache_lock: cache_progress.update({'total': total, 'message': f'共{total}个项目'})
        for i, proj in enumerate(projects):
            if get_houses_with_prices(proj['sypeId']):
                with cache_lock: cache_progress.update({'current':i+1,'message':f'[{i+1}/{total}] {proj["project"]} 已有缓存，跳过'})
                continue
            try:
                body = urllib.parse.urlencode({'preSellId': proj['sypeId']}).encode()
                raw, err = http_fetch(f'{API_SZFDC}/projectPublish/getProjectByPreSellId', body, 'application/x-www-form-urlencoded')
                if err: raise Exception(err)
                data = json.loads(raw).get('data', {})
                buildings = data.get('iszYsProjectBuildingVoList', [])
                watchers = data.get('moneyWatcherVoList', [])
                ysProjectId = str(watchers[0].get('fypId', '')) if watchers else ''
                th = 0
                for bld in buildings:
                    houses, e2 = fetch_and_cache_houses(proj['sypeId'], str(bld['id']), ysProjectId)
                    if houses: th += len(houses)
                    time.sleep(0.3)
                with cache_lock: cache_progress.update({'current':i+1,'houses_cached':cache_progress['houses_cached']+th,'message':f'[{i+1}/{total}] {proj["project"]} 缓存{th}套'})
            except Exception as e:
                with cache_lock: cache_progress.update({'current':i+1,'message':f'[{i+1}/{total}] {proj["project"]} 错误:{str(e)[:40]}'})
    except Exception as e:
        print(f'[batch] Error: {e}')
    finally:
        with cache_lock: cache_progress.update({'running':False,'message':f'完成！共缓存{cache_progress["houses_cached"]}套房源'})


# ---- HTTP Handler ----

# Transaction data cache (refreshed hourly)
trans_cache = {'new': None, 'old': None, 'time': 0}
trans_lock = threading.Lock()


def get_transaction_data(source='new'):
    """Fetch all available transaction data, with 1-hour cache"""
    global trans_cache
    now = time.time()
    with trans_lock:
        if trans_cache[source] and (now - trans_cache['time']) < 3600:
            return trans_cache[source]

    api_url = OPENDATA_API2 if source == 'old' else OPENDATA_API
    appkey = OPENDATA_KEY2 if source == 'old' else OPENDATA_KEY
    total_records = 92300 if source == 'old' else 119500
    total_pages = int(total_records / 100) + 5
    all_rows = []; seen = set()
    for page in range(total_pages, total_pages - 200, -1):
        if page < 1: break
        url = f'{api_url}?page={page}&rows=100&appKey={appkey}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
                data = json.loads(resp.read())
                if data.get('errorCode'): break
                rows = data.get('data', [])
                if not rows: continue
                for r in rows:
                    cat = r.get('REPORTCATALOG', r.get('HOUSE_USAGE2', ''))
                    key = r.get('TJ_DATE','')+r.get('ZONE','')+cat
                    if key not in seen: seen.add(key); all_rows.append(r)
                time.sleep(0.1)
        except Exception as e: break
    # Save to cache
    with trans_lock:
        trans_cache[source] = all_rows
        trans_cache['time'] = now
    return all_rows


def aggregate_transactions(rows, mode='daily', zone='全市', catalog='', source='new'):
    filtered = rows
    if zone: filtered = [r for r in filtered if r.get('ZONE')==zone]
    if catalog:
        cat_field = 'HOUSE_USAGE2' if source == 'old' else 'REPORTCATALOG'
        filtered = [r for r in filtered if r.get(cat_field)==catalog]
    groups = {}
    for r in filtered:
        dt = r.get('TJ_DATE','')
        if mode == 'weekly':
            try:
                d = datetime.strptime(dt,'%Y-%m-%d'); wk = d.isocalendar()
                key = f'{wk[0]}-W{wk[1]:02d}'
            except: key = dt
        elif mode == 'monthly': key = dt[:7]
        else: key = dt
        if key not in groups: groups[key] = {'cjNum':0,'cjArea':0,'ksNum':0,'ksArea':0}
        groups[key]['cjNum'] += int(r.get('CJ_NUM',0) or 0)
        groups[key]['cjArea'] += float(r.get('CJ_AREA',0) or 0)
        groups[key]['ksNum'] += int(r.get('KS_NUM',0) or 0)
        groups[key]['ksArea'] += float(r.get('KS_AREA',0) or 0)
    return [{'period':k,**groups[k]} for k in sorted(groups.keys())]


class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        routes = {
            '/': self.serve_html, '/index.html': self.serve_html,
            '/api/presale/list': lambda: self.handle_presale_list(q),
            '/api/presale/enriched': lambda: self.handle_enriched(q),
            '/api/presale/zones': self.handle_zones,
            '/api/presale/zone-overview': self.handle_zone_overview,
            '/api/presale/transactions': lambda: self.handle_transactions(q),
            '/api/presale/stats': lambda: self.handle_stats(q),
            '/api/presale/cache': self.handle_cache_refresh,
            '/api/presale/cache-progress': self.handle_cache_progress,
            '/api/presale/export': lambda: self.handle_export(q),
            '/api/image': lambda: self.handle_image(q),
            '/api/floorplan/image': lambda: self.handle_floorplan_image(q),
            '/api/floorplan/load': lambda: self.handle_floorplan_load(q),
        }
        handler = routes.get(path)
        if handler:
            handler()
        elif path.startswith('/api/'):
            self.send_json(404, {'error': f'Unknown API: {path}'})
        else:
            self.serve_static(path)

    def do_POST(self):
        path = self.path
        cl = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(cl) if cl else b''
        ct = self.headers.get('Content-Type', '')

        # Parse body
        if 'application/json' in ct:
            try: params = json.loads(body)
            except: params = {}
        elif 'application/x-www-form-urlencoded' in ct:
            params = {}
            for k, v in urllib.parse.parse_qs(body.decode()).items():
                params[k] = v[0] if len(v) == 1 else v
        else:
            try: params = json.loads(body)
            except:
                try:
                    params = {}
                    for k, v in urllib.parse.parse_qs(body.decode()).items():
                        params[k] = v[0] if len(v) == 1 else v
                except: params = {}

        if path == '/api/presale/list':
            self.proxy_presale(**params)
        elif path == '/api/project/detail':
            self.proxy_form_api('projectPublish/getProjectByPreSellId', params)
        elif path == '/api/project/houses':
            self.proxy_json_api('projectPublish/getHouseInfoListToPublicity', params)
        elif path == '/api/project/houses-db':
            self.handle_houses_db(params)
        elif path == '/api/project/floor-price':
            self.handle_floor_price(params)
        elif path == '/api/project/compare':
            self.handle_compare(params)
        elif path == '/api/project/building-dict':
            self.proxy_form_api('projectPublish/getBuildingDictToPublicity', params)
        elif path == '/api/project/building-names':
            self.proxy_form_api('projectPublish/getBuildingNameListToPublicity', params)
        elif path == '/api/project/house-statuses':
            self.proxy_form_api('projectPublish/getAllHouseStatusToPublicity', params)
        elif path == '/api/presale/batch-cache':
            self.handle_batch_cache()
        elif path == '/api/project/seller':
            self.proxy_form_api('projectPublish/projectCreateDetailInfoToPublicity', params)
        elif path == '/api/floorplan/tags':
            self.handle_floorplan_tags(params)
        elif path == '/api/floorplan/prices':
            self.handle_floorplan_prices(params)
        elif path == '/api/floorplan/upload':
            self.handle_floorplan_upload(params)
        elif path == '/api/floorplan/save':
            self.handle_floorplan_save(params)
        elif path == '/api/floorplan/prices-all':
            self.handle_floorplan_prices_all(params)
        else:
            self.send_json(404, {'error': f'Unknown POST: {path}'})

    # ---- HTML & static ----
    def serve_html(self):
        try:
            with open(HTML_FILE, 'rb') as f: content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(500, 'HTML file not found')

    def serve_static(self, path):
        fp = os.path.join(SCRIPT_DIR, path.lstrip('/'))
        if os.path.exists(fp) and os.path.isfile(fp):
            with open(fp, 'rb') as f: content = f.read()
            ct = 'text/html'
            if path.endswith('.css'): ct = 'text/css'
            elif path.endswith('.js'): ct = 'application/javascript'
            elif path.endswith('.json'): ct = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    # ---- Presale list ----
    def handle_presale_list(self, q):
        page = int(q.get('pageIndex', ['1'])[0])
        page_size = int(q.get('pageSize', ['12'])[0])
        zone = q.get('zone', [''])[0]
        keyword = q.get('keyword', [''])[0]
        use_cache = q.get('cache', ['1'])[0] == '1'

        if use_cache:
            data = get_cached_list(page, page_size, zone, keyword)
            if data['total'] > 0:
                self.send_json(200, {'status': 200, 'msg': '成功', 'data': data})
                return
        self.proxy_presale(page, page_size, zone, keyword, 0)

    def proxy_presale(self, page_idx=1, page_size=12, zone='', keyword='', total=0):
        body = json.dumps({
            'pageIndex': page_idx, 'pageSize': page_size,
            'total': total, 'zone': zone or '', 'keyword': keyword or ''
        }).encode()
        raw, err = http_fetch(f'{API_SZFDC}/ysf/publicity/getYsfYsPublicity', body)
        if err:
            self.send_json(502, {'error': err})
            return
        self.send_raw(200, raw, 'application/json; charset=utf-8')

    # ---- Project detail APIs (POST proxy) ----
    def handle_houses_db(self, params):
        """Get houses with price protection from DB, fetching from API if needed"""
        sypeId = params.get('sypeId', params.get('preSellId', ''))
        fybId = params.get('fybId', '')
        ysProjectId = params.get('ysProjectId', '')

        if not sypeId or not fybId:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId or fybId'})
            return

        # Check if we have cached data
        cached = get_houses_with_prices(sypeId, fybId)
        if cached:
            print(f'[houses-db] Returned {len(cached)} cached houses for sypeId={sypeId} fybId={fybId}')
            self.send_json(200, {'status': 200, 'msg': '成功', 'data': cached})
            return

        # Fetch from API and cache
        houses, err = fetch_and_cache_houses(sypeId, fybId, ysProjectId)
        if err:
            self.send_json(502, {'status': 502, 'msg': str(err)})
            return

        print(f'[houses-db] Fetched & cached {len(houses) if houses else 0} houses')
        self.send_json(200, {'status': 200, 'msg': '成功', 'data': houses})

    def proxy_form_api(self, api_path, params):
        """Proxy an API that expects form-urlencoded data"""
        body = urllib.parse.urlencode(params).encode()
        raw, err = http_fetch(f'{API_SZFDC}/{api_path}', body, 'application/x-www-form-urlencoded')
        if err:
            self.send_json(502, {'error': err, 'status': 502})
            return
        self.send_raw(200, raw, 'application/json; charset=utf-8')

    def proxy_json_api(self, api_path, params):
        """Proxy an API that expects JSON data"""
        body = json.dumps(params).encode()
        raw, err = http_fetch(f'{API_SZFDC}/{api_path}', body, 'application/json')
        if err:
            self.send_json(502, {'error': err, 'status': 502})
            return
        self.send_raw(200, raw, 'application/json; charset=utf-8')

    # ---- Stats ----
    def handle_stats(self, q):
        f = q.get('dateFrom', [''])[0]
        t = q.get('dateTo', [''])[0]
        self.send_json(200, {'status': 200, 'data': get_cached_stats(f, t)})

    # ---- Cache ----
    def handle_cache_refresh(self):
        self.send_json(200, {'status': 200, 'msg': '正在后台更新缓存...'})
        t = threading.Thread(target=self._do_cache_refresh, daemon=True)
        t.start()

    def _do_cache_refresh(self):
        print('[cache] Starting background refresh...')
        items = fetch_all_pages()
        if items:
            save_to_cache(items)
            print(f'[cache] Done: {len(items)} items')

    def handle_cache_progress(self):
        with cache_lock:
            self.send_json(200, {'status': 200, 'data': dict(cache_progress)})

    def handle_batch_cache(self):
        if cache_progress['running']:
            self.send_json(200, {'status': 200, 'msg': '已有批量缓存任务在进行中', 'data': dict(cache_progress)})
        else:
            self.send_json(200, {'status': 200, 'msg': '批量缓存已启动'})
            threading.Thread(target=batch_cache_all_houses, daemon=True).start()

    def handle_enriched(self, q):
        page = int(q.get('pageIndex', ['1'])[0])
        page_size = int(q.get('pageSize', ['12'])[0])
        zone = q.get('zone', [''])[0]
        keyword = q.get('keyword', [''])[0]
        data = get_enriched_projects(page, page_size, zone, keyword)
        self.send_json(200, {'status': 200, 'msg': '成功', 'data': data})

    def handle_zones(self):
        self.send_json(200, {'status': 200, 'data': get_all_zones()})

    def handle_zone_overview(self):
        self.send_json(200, {'status': 200, 'data': get_zone_overview()})

    def handle_transactions(self, q):
        mode = q.get('mode', ['daily'])[0]
        zone = q.get('zone', ['全市'])[0]
        catalog = q.get('catalog', [''])[0]
        source = q.get('source', ['new'])[0]
        dateFrom = q.get('dateFrom', [''])[0]
        dateTo = q.get('dateTo', [''])[0]
        # Fetch enough data to cover the range (max 365 days)
        rows = get_transaction_data(source)
        # Filter by date range
        if dateFrom:
            rows = [r for r in rows if r.get('TJ_DATE','') >= dateFrom]
        if dateTo:
            rows = [r for r in rows if r.get('TJ_DATE','') <= dateTo]
        agg = aggregate_transactions(rows, mode, zone, catalog, source)
        self.send_json(200, {'status': 200, 'data': {'rows': agg, 'total': len(rows), 'source': source}})

    def handle_floor_price(self, params):
        sid = params.get('sypeId', params.get('preSellId', ''))
        fid = params.get('fybId', '')
        useage = params.get('useage', None)
        if not sid:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId'})
            return
        self.send_json(200, {'status': 200, 'data': get_floor_price_data(sid, fid if fid else None, useage)})

    def handle_compare(self, params):
        ids = params.get('sypeIds', params.get('ids', []))
        if not ids:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeIds array'})
            return
        self.send_json(200, {'status': 200, 'data': get_comparison_data(ids)})

    # ---- Export ----
    def handle_export(self, q):
        zone = q.get('zone', [''])[0]
        keyword = q.get('keyword', [''])[0]
        fmt = q.get('format', ['csv'])[0]
        data = get_cached_list(1, 5000, zone, keyword)
        items = data['list']

        if fmt == 'json':
            output = json.dumps(items, ensure_ascii=False, indent=2).encode('utf-8')
            ct, fn = 'application/json; charset=utf-8', 'shenzhen_presale.json'
        else:
            hdrs = ['预售证号', '项目名称', '开发商', '区域', '地址', '发证日期']
            keys = ['strpreprojectid', 'project', 'name', 'zone', 'siteaddress', 'passdate']
            rows = [','.join(f'"{item.get(k,"")}"' for k in keys) for item in items]
            output = ('\ufeff' + ','.join(hdrs) + '\n' + '\n'.join(rows)).encode('utf-8')
            ct, fn = 'text/csv; charset=utf-8', 'shenzhen_presale.csv'

        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Disposition', f'attachment; filename="{fn}"')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(output)

    # ---- Image proxy ----
    def handle_image(self, q):
        img_path = q.get('path', [''])[0]
        if not img_path:
            self.send_error(400)
            return
        try:
            req = urllib.request.Request(
                f'https://fdc.zjj.sz.gov.cn/szfdcscjy/{img_path}',
                headers={'User-Agent': 'Mozilla/5.0',
                         'Referer': 'https://fdc.zjj.sz.gov.cn/szfdcscjy/'}
            )
            with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
                raw = resp.read()
            self.send_response(200)
            self.send_header('Content-Type', resp.getheader('Content-Type', 'image/jpeg'))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(raw)
        except Exception:
            self.send_error(404)

    # ---- Utilities ----
    def send_raw(self, code, data, ct='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ct)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8') if isinstance(data, dict) else (
            data if isinstance(data, bytes) else str(data).encode())
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    # ---- Floor plan APIs ----
    def handle_floorplan_tags(self, params):
        sypeId = params.get('sypeId', '')
        if not sypeId:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId'})
            return
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''SELECT DISTINCT buildingName, buildingbranch,
                         SUBSTR(housenb, -2) as pos
                         FROM houses WHERE sypeId=? AND askpriceeachB>0 AND useage='住宅'
                         ORDER BY buildingName, buildingbranch, pos''', (sypeId,))
            rows = c.fetchall()
            conn.close()
        tag_map = {}
        for bld, branch, pos in rows:
            key = (bld or '') + '|' + (branch or '')
            if key not in tag_map:
                tag_map[key] = {'buildingName': bld or '', 'buildingbranch': branch or '', 'positions': []}
            tag_map[key]['positions'].append(pos)
        self.send_json(200, {'status': 200, 'data': list(tag_map.values())})

    def handle_floorplan_prices(self, params):
        sypeId = params.get('sypeId', '')
        tags = params.get('tags', [])
        if not sypeId or not tags:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId or tags'})
            return
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            result = []
            for tag in tags:
                bld = tag.get('buildingName', '')
                branch = tag.get('buildingbranch', '')
                pos = tag.get('position', '')
                if not branch:
                    c.execute('''SELECT COUNT(*),
                                 ROUND(AVG(askpriceeachB),0),
                                 ROUND(MIN(askpriceeachB),0),
                                 ROUND(MAX(askpriceeachB),0),
                                 ROUND(AVG(askpricetotalB)/10000,0),
                                 ROUND(MIN(askpricetotalB)/10000,0),
                                 ROUND(MAX(askpricetotalB)/10000,0),
                                 ROUND(AVG(ysbuildingarea),1)
                                 FROM houses
                                 WHERE sypeId=? AND buildingName=? AND buildingbranch IS NULL
                                 AND SUBSTR(housenb, -2)=? AND askpriceeachB>0''',
                              (sypeId, bld, pos))
                else:
                    c.execute('''SELECT COUNT(*),
                                 ROUND(AVG(askpriceeachB),0),
                                 ROUND(MIN(askpriceeachB),0),
                                 ROUND(MAX(askpriceeachB),0),
                                 ROUND(AVG(askpricetotalB)/10000,0),
                                 ROUND(MIN(askpricetotalB)/10000,0),
                                 ROUND(MAX(askpricetotalB)/10000,0),
                                 ROUND(AVG(ysbuildingarea),1)
                                 FROM houses
                                 WHERE sypeId=? AND buildingName=? AND buildingbranch=?
                                 AND SUBSTR(housenb, -2)=? AND askpriceeachB>0''',
                              (sypeId, bld, branch, pos))
                row = c.fetchone()
                if row and row[0] > 0:
                    result.append({
                        'buildingName': bld, 'buildingbranch': branch, 'position': pos,
                        'count': row[0],
                        'avgUnitPrice': row[1], 'minUnitPrice': row[2], 'maxUnitPrice': row[3],
                        'avgTotalPrice': row[4], 'minTotalPrice': row[5], 'maxTotalPrice': row[6],
                        'avgArea': row[7]
                    })
            conn.close()
        self.send_json(200, {'status': 200, 'data': result})

    def handle_floorplan_upload(self, params):
        sypeId = params.get('sypeId', '')
        image_base64 = params.get('imageBase64', '')
        if not sypeId or not image_base64:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId or imageBase64'})
            return
        try:
            import base64
            img_data = image_base64
            if ',' in img_data:
                img_data = img_data.split(',', 1)[1]
            img_bytes = base64.b64decode(img_data)
            ext = 'jpg'
            if image_base64.startswith('data:image/png'):
                ext = 'png'
            elif image_base64.startswith('data:image/gif'):
                ext = 'gif'
            elif image_base64.startswith('data:image/webp'):
                ext = 'webp'
            filename = f'{sypeId}.{ext}'
            filepath = os.path.join(FLOORPLANS_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(img_bytes)
            self.send_json(200, {'status': 200, 'data': {'imagePath': filepath, 'filename': filename}})
        except Exception as e:
            self.send_json(500, {'status': 500, 'msg': str(e)})

    def handle_floorplan_image(self, q):
        sypeId = q.get('sypeId', [''])[0]
        if not sypeId:
            self.send_error(400)
            return
        for ext in ['jpg', 'png', 'gif', 'webp']:
            filepath = os.path.join(FLOORPLANS_DIR, f'{sypeId}.{ext}')
            if os.path.exists(filepath):
                ct_map = {'jpg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}
                with open(filepath, 'rb') as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header('Content-Type', ct_map.get(ext, 'image/jpeg'))
                self.send_header('Cache-Control', 'max-age=3600')
                self.end_headers()
                self.wfile.write(raw)
                return
        self.send_error(404)

    def handle_floorplan_save(self, params):
        sypeId = params.get('sypeId', '')
        layout = params.get('layout', {})
        image_path = params.get('imagePath', '')
        if not sypeId:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId'})
            return
        now = datetime.now().isoformat()
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT sypeId FROM floorplan_layouts WHERE sypeId=?', (sypeId,))
            existing = c.fetchone()
            if existing:
                c.execute('''UPDATE floorplan_layouts SET image_path=?, layout_json=?, updated_at=?
                             WHERE sypeId=?''', (image_path, json.dumps(layout, ensure_ascii=False), now, sypeId))
            else:
                c.execute('''INSERT INTO floorplan_layouts (sypeId, image_path, layout_json, created_at, updated_at)
                             VALUES (?,?,?,?,?)''', (sypeId, image_path, json.dumps(layout, ensure_ascii=False), now, now))
            conn.commit()
            conn.close()
        self.send_json(200, {'status': 200, 'msg': '保存成功'})

    def handle_floorplan_prices_all(self, params):
        sypeId = params.get('sypeId', '')
        if not sypeId:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId'})
            return
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''SELECT buildingName, buildingbranch,
                         SUBSTR(housenb, -2) as pos,
                         COUNT(*) as cnt,
                         ROUND(AVG(askpriceeachB),0) as avg_unit,
                         ROUND(MIN(askpriceeachB),0) as min_unit,
                         ROUND(MAX(askpriceeachB),0) as max_unit,
                         ROUND(AVG(askpricetotalB)/10000,0) as avg_total,
                         ROUND(MIN(askpricetotalB)/10000,0) as min_total,
                         ROUND(MAX(askpricetotalB)/10000,0) as max_total,
                         ROUND(AVG(ysbuildingarea),1) as avg_area
                         FROM houses
                         WHERE sypeId=? AND askpriceeachB>0 AND useage='住宅'
                         GROUP BY buildingName, buildingbranch, SUBSTR(housenb, -2)
                         ORDER BY buildingName, buildingbranch, pos''', (sypeId,))
            rows = c.fetchall()
            conn.close()
        result = []
        for r in rows:
            result.append({
                'buildingName': r[0] or '', 'buildingbranch': r[1] or '', 'position': r[2],
                'count': r[3],
                'avgUnitPrice': r[4], 'minUnitPrice': r[5], 'maxUnitPrice': r[6],
                'avgTotalPrice': r[7], 'minTotalPrice': r[8], 'maxTotalPrice': r[9],
                'avgArea': r[10]
            })
        self.send_json(200, {'status': 200, 'data': result})

    def handle_floorplan_load(self, q):
        sypeId = q.get('sypeId', [''])[0]
        if not sypeId:
            self.send_json(400, {'status': 400, 'msg': 'Missing sypeId'})
            return
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT image_path, layout_json, created_at, updated_at FROM floorplan_layouts WHERE sypeId=?', (sypeId,))
            row = c.fetchone()
            conn.close()
        if row:
            self.send_json(200, {
                'status': 200,
                'data': {
                    'imagePath': row[0],
                    'layout': json.loads(row[1]) if row[1] else {},
                    'createdAt': row[2],
                    'updatedAt': row[3]
                }
            })
        else:
            self.send_json(200, {'status': 200, 'data': None})


if __name__ == '__main__':
    init_db()
    print('深圳房地产数据分析系统 v3.0')
    print('=' * 50)
    refresh_session()
    def initial_load():
        items = fetch_all_pages()
        if items: save_to_cache(items)
    threading.Thread(target=initial_load, daemon=True).start()
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'服务启动: http://localhost:{PORT}')
    print(f'按 Ctrl+C 停止')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        server.server_close()
