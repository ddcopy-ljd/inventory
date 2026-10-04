"""懿臻珠宝云 - 插件数据库模块。

负责：租户库定位、连接、建表结构（幂等）、演示种子数据。
仅在插件进程上下文（环境变量）下使用，不依赖平台数据库。
"""

import os
import re
import sqlite3
from pathlib import Path

PLUGIN_ID = os.environ.get("PLUGIN_ID", "jewelry")
VERSION = os.environ.get("PLUGIN_VERSION", "1.0.0")
DB_DIR = Path(os.environ.get("TENANT_DB_DIR", ""))

TENANT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# 数据库结构（dataVersion 1.0.0）
SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  name_i18n TEXT DEFAULT '{}',
  code TEXT UNIQUE,
  owner TEXT
);

CREATE TABLE IF NOT EXISTS languages (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  is_default INTEGER DEFAULT 0,
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS biz_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  epc_prefix TEXT DEFAULT 'E280',
  seq_bits INTEGER DEFAULT 8
);

CREATE TABLE IF NOT EXISTS categories (
  code TEXT PRIMARY KEY,
  names TEXT NOT NULL DEFAULT '{}',
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  name_i18n TEXT DEFAULT '{}',
  category TEXT DEFAULT '黄金',
  category_code TEXT DEFAULT '',
  material TEXT DEFAULT '',
  weight REAL DEFAULT 0,
  size TEXT DEFAULT '',
  cert TEXT DEFAULT '',
  cost REAL DEFAULT 0,
  price REAL DEFAULT 0,
  status TEXT DEFAULT '在库',
  store_id INTEGER,
  rfid_epc TEXT DEFAULT '',
  showcase_public INTEGER DEFAULT 0,
  showcase_order INTEGER DEFAULT 0,
  showcase_desc TEXT DEFAULT '',
  showcase_desc_i18n TEXT DEFAULT '{}',
  origin TEXT DEFAULT '',
  created TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  phone TEXT DEFAULT '',
  level TEXT DEFAULT '普通',
  total_amount REAL DEFAULT 0,
  due_amount REAL DEFAULT 0,
  birthday TEXT DEFAULT '',
  preference TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_no TEXT UNIQUE,
  customer TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  product TEXT DEFAULT '',
  product_id INTEGER,
  amount REAL DEFAULT 0,
  paid REAL DEFAULT 0,
  method TEXT DEFAULT '现金',
  biz_date TEXT DEFAULT (date('now','localtime')),
  type TEXT DEFAULT '普通',
  status TEXT DEFAULT '已完成',
  created TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tenant_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT DEFAULT '',
  name TEXT DEFAULT '懿臻珠宝',
  short_name TEXT DEFAULT '懿臻',
  slogan TEXT DEFAULT '懿德 · 臻品 · 云智',
  intro TEXT DEFAULT '',
  contact TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  address TEXT DEFAULT '',
  hours TEXT DEFAULT '10:00-21:00',
  categories TEXT DEFAULT '黄金,钻石,翡翠,铂金,彩宝',
  logo TEXT DEFAULT '',
  storefront TEXT DEFAULT '',
  gallery TEXT DEFAULT '[]',
  published INTEGER DEFAULT 1,
  showcase_title TEXT DEFAULT '新品橱窗',
  showcase_subtitle TEXT DEFAULT '本周臻品 · 限量发售'
);

CREATE TABLE IF NOT EXISTS deposits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  product_id INTEGER,
  product TEXT DEFAULT '',
  total REAL DEFAULT 0,
  deposit REAL DEFAULT 0,
  balance REAL DEFAULT 0,
  promised_date TEXT DEFAULT '',
  reminder_days INTEGER DEFAULT 7,
  status TEXT DEFAULT '已定',
  created TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS loans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  direction TEXT DEFAULT 'in',
  product TEXT DEFAULT '',
  code TEXT DEFAULT '',
  party TEXT DEFAULT '',
  qty INTEGER DEFAULT 1,
  loan_date TEXT DEFAULT (date('now','localtime')),
  due_date TEXT DEFAULT '',
  status TEXT DEFAULT '借出中'
);

CREATE TABLE IF NOT EXISTS repairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  item TEXT DEFAULT '',
  issue TEXT DEFAULT '',
  est_fee REAL DEFAULT 0,
  actual_fee REAL DEFAULT 0,
  receive_date TEXT DEFAULT (date('now','localtime')),
  promised_date TEXT DEFAULT '',
  done_date TEXT DEFAULT '',
  status TEXT DEFAULT '待维修',
  technician TEXT DEFAULT '',
  remark TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS purchases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  supplier TEXT DEFAULT '',
  product TEXT DEFAULT '',
  qty INTEGER DEFAULT 1,
  cost REAL DEFAULT 0,
  order_date TEXT DEFAULT (date('now','localtime')),
  expected_date TEXT DEFAULT '',
  received_date TEXT DEFAULT '',
  status TEXT DEFAULT '待发货',
  paid REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outsourcings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factory TEXT DEFAULT '',
  product TEXT DEFAULT '',
  material TEXT DEFAULT '',
  weight REAL DEFAULT 0,
  gold_price REAL DEFAULT 0,
  labor_fee REAL DEFAULT 0,
  send_date TEXT DEFAULT (date('now','localtime')),
  expected_date TEXT DEFAULT '',
  received_date TEXT DEFAULT '',
  status TEXT DEFAULT '加工中'
);

CREATE TABLE IF NOT EXISTS label_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT DEFAULT '',
  size_width REAL DEFAULT 75,
  size_height REAL DEFAULT 25,
  layout TEXT DEFAULT '{}',
  cols INTEGER DEFAULT 1,
  gap REAL DEFAULT 0,
  copies INTEGER DEFAULT 1,
  default_printer TEXT DEFAULT '',
  is_rfid INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inventory_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER,
  epc TEXT DEFAULT '',
  type TEXT DEFAULT 'scan',
  qty INTEGER DEFAULT 1,
  ts TEXT DEFAULT (datetime('now','localtime')),
  operator TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS operate_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time TEXT DEFAULT (datetime('now','localtime')),
  operator TEXT DEFAULT '',
  store TEXT DEFAULT '',
  action TEXT DEFAULT '',
  target TEXT DEFAULT '',
  result TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT DEFAULT '',
  name TEXT DEFAULT '',
  contact TEXT DEFAULT '',
  contact_type TEXT DEFAULT 'phone',
  category TEXT DEFAULT '',
  product_id INTEGER,
  want_date TEXT DEFAULT '',
  want_slot TEXT DEFAULT '',
  remark TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  created TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  display_name TEXT DEFAULT '',
  role TEXT DEFAULT 'EMPLOYEE'
);

CREATE TABLE IF NOT EXISTS stocktakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no TEXT UNIQUE,
  device TEXT DEFAULT '',
  scanned_count INTEGER DEFAULT 0,
  book_count INTEGER DEFAULT 0,
  matched_count INTEGER DEFAULT 0,
  surplus_count INTEGER DEFAULT 0,
  shortage_count INTEGER DEFAULT 0,
  abnormal_count INTEGER DEFAULT 0,
  dup_count INTEGER DEFAULT 0,
  status TEXT DEFAULT '完成',
  operator TEXT DEFAULT '手持机',
  created TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS stocktake_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stocktake_id INTEGER,
  result TEXT DEFAULT '相符',
  epc TEXT DEFAULT '',
  product_id INTEGER,
  code TEXT DEFAULT '',
  product TEXT DEFAULT '',
  book_status TEXT DEFAULT '',
  dup_count INTEGER DEFAULT 0
);

-- 多终端协同盘点：任务会话
CREATE TABLE IF NOT EXISTS stocktake_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_no TEXT UNIQUE,
  task_key TEXT UNIQUE,
  status TEXT DEFAULT '进行中',
  operator TEXT DEFAULT '',
  snapshot_version TEXT DEFAULT '',
  started TEXT DEFAULT (datetime('now','localtime')),
  ended TEXT DEFAULT '',
  result_id INTEGER DEFAULT 0
);

-- 多终端协同盘点：任务内设备与临时编号
CREATE TABLE IF NOT EXISTS stocktake_devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  device_key TEXT DEFAULT '',
  device_no INTEGER DEFAULT 0,
  name TEXT DEFAULT '',
  last_seen TEXT DEFAULT (datetime('now','localtime')),
  finished INTEGER DEFAULT 0,
  UNIQUE(session_id, device_key)
);

-- 多终端协同盘点：实时扫描记录（全局按任务+EPC去重）
CREATE TABLE IF NOT EXISTS stocktake_scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  epc TEXT DEFAULT '',
  device_key TEXT DEFAULT '',
  device_no INTEGER DEFAULT 0,
  rssi INTEGER DEFAULT 0,
  scanned_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(session_id, epc)
);
"""


def db_file(tenant_id: str) -> Path:
    """返回某租户的库文件路径（文件名由平台命名约定决定）。"""
    return DB_DIR / f"db_{PLUGIN_ID}_{tenant_id}_v{VERSION}.sqlite"


def connect(tenant_id: str, read_only: bool = False) -> sqlite3.Connection:
    """打开（必要时创建）某租户的 SQLite 库。read_only 仅用于查询上下文。"""
    if not DB_DIR:
        raise RuntimeError("TENANT_DB_DIR 未设置")
    if not TENANT_RE.match(tenant_id or ""):
        raise ValueError("非法租户标识")
    path = db_file(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"租户库不存在：{path.name}")
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        migrate_schema(conn)
    return conn


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {r[1] for r in rows}
    return col in names


def migrate_schema(conn: sqlite3.Connection) -> None:
    """幂等建表，并为旧库补齐列。"""
    conn.executescript(SCHEMA)
    alters = [
        ("sales", "product_id", "INTEGER"),
        ("products", "rfid_epc", "TEXT DEFAULT ''"),
        ("products", "showcase_public", "INTEGER DEFAULT 0"),
        ("products", "showcase_order", "INTEGER DEFAULT 0"),
        ("products", "showcase_desc", "TEXT DEFAULT ''"),
        ("products", "origin", "TEXT DEFAULT ''"),
        ("products", "high_value", "INTEGER DEFAULT 0"),
        ("products", "name_i18n", "TEXT DEFAULT '{}'"),
        ("products", "category_code", "TEXT DEFAULT ''"),
        ("products", "showcase_desc_i18n", "TEXT DEFAULT '{}'"),
        ("stores", "name_i18n", "TEXT DEFAULT '{}'"),
        ("tenant_profiles", "showcase_title", "TEXT DEFAULT '新品橱窗'"),
        ("tenant_profiles", "showcase_subtitle", "TEXT DEFAULT '本周臻品 · 限量发售'"),
    ]
    for table, col, decl in alters:
        if not _has_column(conn, table, col):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    migrate_schema(conn)


def ensure_new_seeds(conn: sqlite3.Connection) -> None:
    """为已有数据库补充新模块种子数据（每表独立检测）。"""
    if conn.execute("SELECT COUNT(*) FROM deposits").fetchone()[0] == 0:
        deposits = [
            ("王晓丽", "13800001111", 2, "钻石耳钉", 4280, 2000, 2280, "2026-02-15", 7, "已定"),
            ("赵明辉", "13600004444", 1, "足金手镯定制", 32000, 16000, 16000, "2026-02-28", 7, "已定"),
        ]
        conn.executemany("INSERT INTO deposits(customer,phone,product_id,product,total,deposit,balance,promised_date,reminder_days,status) VALUES(?,?,?,?,?,?,?,?,?,?)", deposits)
    if conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0:
        loans = [
            ("out", "足金手镯", "J001", "同行老刘", 1, "2026-01-20", "2026-02-20", "借出中"),
            ("in", "钻石戒指", "DR-8899", "供应商A", 1, "2026-01-25", "2026-02-15", "借入中"),
        ]
        conn.executemany("INSERT INTO loans(direction,product,code,party,qty,loan_date,due_date,status) VALUES(?,?,?,?,?,?,?,?)", loans)
    if conn.execute("SELECT COUNT(*) FROM repairs").fetchone()[0] == 0:
        repairs = [
            ("李强", "13900002222", "18K金链", "链扣断裂", 200, 180, "2026-01-18", "2026-01-25", "2026-01-24", "已完成", "王师傅", "配原装链扣"),
            ("张美凤", "13700003333", "翡翠手镯", "轻微裂纹修复", 500, 0, "2026-01-28", "2026-02-10", "", "维修中", "李师傅", ""),
        ]
        conn.executemany("INSERT INTO repairs(customer,phone,item,issue,est_fee,actual_fee,receive_date,promised_date,done_date,status,technician,remark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", repairs)
    if conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 0:
        purchases = [
            ("深圳金料供应", "足金金料", 100, 45000, "2026-01-05", "2026-01-15", "2026-01-14", "已入库", 45000),
        ]
        conn.executemany("INSERT INTO purchases(supplier,product,qty,cost,order_date,expected_date,received_date,status,paid) VALUES(?,?,?,?,?,?,?,?,?)", purchases)
    if conn.execute("SELECT COUNT(*) FROM outsourcings").fetchone()[0] == 0:
        outsourcings = [
            ("金艺加工厂", "镶钻吊坠", "18K金", 5.6, 380, 1200, "2026-01-12", "2026-01-22", "", "加工中"),
        ]
        conn.executemany("INSERT INTO outsourcings(factory,product,material,weight,gold_price,labor_fee,send_date,expected_date,received_date,status) VALUES(?,?,?,?,?,?,?,?,?,?)", outsourcings)
    if conn.execute("SELECT COUNT(*) FROM operate_logs").fetchone()[0] == 0:
        conn.execute("INSERT INTO operate_logs(operator,store,action,target,result) VALUES('admin','总店','初始化演示数据','jewelry','成功')")
    if conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
        customers = [("王晓丽", "13800001111", "金卡", 43600, 0, "1990-05-12", "偏好足金手镯"),
                     ("李强", "13900002222", "银卡", 12800, 21800, "1988-11-03", "钻石类"),
                     ("张美凤", "13700003333", "普通", 5600, 0, "1995-02-20", "彩宝")]
        conn.executemany("INSERT INTO customers(name,phone,level,total_amount,due_amount,birthday,preference) VALUES(?,?,?,?,?,?,?)", customers)
    if conn.execute("SELECT COUNT(*) FROM tenant_profiles").fetchone()[0] == 0:
        conn.execute(
            """INSERT INTO tenant_profiles(tenant_id,name,short_name,slogan,intro,contact,phone,address,hours,categories,published)
               VALUES('tenant_trial','懿臻珠宝总店','懿臻','懿德 · 臻品 · 云智',
                      '面向中高端珠宝门店的数智化经营。','张店长','13800000000','上海市黄浦区南京东路 88 号','10:00-21:00','黄金,钻石,翡翠,铂金,彩宝',1)"""
        )
    if conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone() is None:
        conn.execute("INSERT INTO users(username,password,display_name,role) VALUES('admin','123456','店长','TENANT_ADMIN')")
    if conn.execute("SELECT 1 FROM users WHERE username='staff'").fetchone() is None:
        conn.execute("INSERT INTO users(username,password,display_name,role) VALUES('staff','123456','店员','EMPLOYEE')")
    conn.commit()


def seed_demo(conn: sqlite3.Connection) -> None:
    """写入演示数据（幂等：仅在接近空库时执行）。"""
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0:
        return
    stores = [("总店", "HQ", "管理员"), ("分店A", "A001", "店长小张")]
    conn.executemany("INSERT INTO stores(name,code,owner) VALUES(?,?,?)", stores)

    # 语言
    conn.execute("INSERT OR IGNORE INTO languages(code,name,is_default,sort_order) VALUES('zh','中文',1,1)")
    conn.execute("INSERT OR IGNORE INTO languages(code,name,is_default,sort_order) VALUES('it','Italiano',0,2)")
    conn.execute("INSERT OR IGNORE INTO languages(code,name,is_default,sort_order) VALUES('en','English',0,3)")
    # 业务配置
    conn.execute("INSERT OR IGNORE INTO biz_config(id,epc_prefix,seq_bits) VALUES(1,'E280',8)")
    # 分类（多语言）
    categories = [
        ("01", '{"zh":"黄金","it":"Oro","en":"Gold"}', 1),
        ("02", '{"zh":"钻石","it":"Diamante","en":"Diamond"}', 2),
        ("03", '{"zh":"翡翠","it":"Giada","en":"Jadeite"}', 3),
        ("04", '{"zh":"铂金","it":"Platino","en":"Platinum"}', 4),
        ("05", '{"zh":"彩宝","it":"Pietre colorate","en":"Colored gems"}', 5),
        ("06", '{"zh":"银饰","it":"Argento","en":"Silver"}', 6),
        ("07", '{"zh":"珍珠","it":"Perla","en":"Pearl"}', 7),
        ("99", '{"zh":"其他","it":"Altro","en":"Other"}', 99),
    ]
    conn.executemany("INSERT OR IGNORE INTO categories(code,names,sort_order) VALUES(?,?,?)", categories)

    products = [
        # J001 - 足金手镯 (1)
        ("J001", "足金手镯", '{"zh":"足金手镯"}', "黄金", "01", "Au999", 28.6, "56号", "GDH-88231", 18500, 21800, "在库", 1, "E28011606000020999A1C14501", 1, 1,
         "产地：深圳水贝｜材质：足金999｜金重：28.60g｜尺寸：56号｜经典光面圆条，福韵满堂，妈妈婚嫁首选｜参考价：¥21,800",
         "深圳·水贝"),
        # J002 - 钻石耳钉 (2)
        ("J002", "钻石耳钉", '{"zh":"钻石耳钉"}', "钻石", "02", "18K Gold+Diamond", 2.4, "单只", "DZ-12034", 3200, 4280, "在库", 1, "", 1, 2,
         "产地：比利时安特卫普｜材质：18K金镶嵌30分天然真钻｜金重：2.40g｜H色VVS净度｜通勤百搭，闪耀出众｜参考价：¥4,280",
         "比利时·安特卫普"),
        # J003 - 翡翠吊坠 (3)
        ("J003", "翡翠观音吊坠", '{"zh":"翡翠观音吊坠"}', "翡翠", "03", "Jadeite", 12.8, "", "FC-55410", 6800, 8600, "在库", 1, "", 1, 3,
         "产地：缅甸帕敢｜材质：天然A货冰种翡翠｜总重：12.80g｜飘绿花雕，观音慈面，护佑平安｜附国检证书｜参考价：¥8,600",
         "缅甸·帕敢"),
        # J004 - 铂金项链 (4)
        ("J004", "铂金肖邦项链", '{"zh":"铂金肖邦项链"}', "铂金", "04", "PT950", 9.2, "45cm", "BJ-20988", 7200, 8900, "在库", 1, "", 1, 4,
         "产地：上海老庙｜材质：PT950 铂金｜金重：9.20g｜链长：45cm｜肖邦链柔韧有光，日常轻奢｜参考价：¥8,900",
         "上海·老庙"),
        # J005 - 彩宝戒指 (5)
        ("J005", "红碧玺彩宝戒指", '{"zh":"红碧玺彩宝戒指"}', "彩宝", "05", "18K Gold+Rubellite", 3.1, "13号", "CB-77421", 4100, 5600, "在库", 2, "", 1, 5,
         "产地：巴西米纳斯｜材质：18K金+3.2ct天然红碧玺｜金重：3.10g｜13号戒圈｜旺运招财，女王气场｜参考价：¥5,600",
         "巴西·米纳斯"),
        # J006 - 黄金吊坠（已定，不进橱窗）
        ("J006", "黄金福字吊坠", '{"zh":"黄金福字吊坠"}', "黄金", "01", "Au999", 6.8, "", "GDH-90344", 4600, 5600, "已定", 1, "E28011606000020999A1C14588", 0, 0, "",
         "深圳·水贝"),
        # J007 - 银质对戒 (6)
        ("J007", "银质一生一世对戒", '{"zh":"银质一生一世对戒"}', "银饰", "06", "Sterling Silver", 8.5, "17号", "AG-12098", 900, 1280, "在库", 2, "", 1, 6,
         "产地：广州番禺｜材质：925纯银镀铂金｜总重：8.50g｜17号戒圈｜刻字「一生一世」，情侣首选｜参考价：¥1,280",
         "广州·番禺"),
        # J008 - 古法黄金 (7)
        ("J008", "古法黄金传承手串", '{"zh":"古法黄金传承手串"}', "黄金", "01", "Au999", 42.3, "18cm", "GDH-98771", 26800, 32600, "在库", 1, "", 1, 7,
         "产地：深圳百泰｜材质：足金999 古法工艺｜金重：42.30g｜18cm手围｜哑光磨砂，传家臻品｜参考价：¥32,600",
         "深圳·百泰"),
        # J009 - 祖母绿吊坠 (8)
        ("J009", "祖母绿锁骨链", '{"zh":"祖母绿锁骨链"}', "彩宝", "05", "18K Gold+Emerald", 2.8, "42cm", "CB-98211", 9800, 12800, "在库", 1, "", 1, 8,
         "产地：哥伦比亚｜材质：18K金镶嵌1.8ct天然祖母绿｜金重：2.80g｜42cm锁骨链｜高贵典雅，收藏级｜参考价：¥12,800",
         "哥伦比亚·木佐"),
        # J0095 - 蓝宝戒指 (9)
        ("J0095", "蓝宝石戒指", '{"zh":"蓝宝石戒指"}', "彩宝", "05", "18K Gold+Sapphire", 3.5, "15号", "CB-77520", 8200, 10800, "在库", 1, "", 1, 9,
         "产地：斯里兰卡｜材质：18K金+2.5ct皇家蓝蓝宝石｜金重：3.50g｜15号戒圈｜丝绒皇家蓝，尊贵非凡｜参考价：¥10,800",
         "斯里兰卡·拉特纳普勒"),
        # J010 - 和田玉 (10)
        ("J010", "和田玉平安扣", '{"zh":"和田玉平安扣"}', "翡翠", "03", "Nephrite", 15.6, "", "FC-88211", 5800, 7800, "在库", 1, "", 1, 10,
         "产地：新疆和田｜材质：和田玉羊脂白玉｜总重：15.60g｜平安扣圆圆满满，馈赠长辈佳品｜附鉴定证书｜参考价：¥7,800",
         "新疆·和田"),
        # J011 - 珍珠项链 (不进橱窗)
        ("J011", "珍珠项链", '{"zh":"珍珠项链"}', "珍珠", "07", "South Sea Pearl+925 Silver", 0, "45cm", "", 2600, 3600, "在库", 2, "", 0, 0, "",
         "菲律宾·巴拉望"),
    ]
    conn.executemany(
        """INSERT INTO products
            (code,name,name_i18n,category,category_code,material,weight,size,cert,cost,price,status,store_id,rfid_epc,
             showcase_public,showcase_order,showcase_desc,origin)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        products,
    )

    customers = [("王晓丽", "13800001111", "金卡", 43600, 0, "1990-05-12", "偏好足金手镯"),
                 ("李强", "13900002222", "银卡", 12800, 21800, "1988-11-03", "钻石类"),
                 ("张美凤", "13700003333", "普通", 5600, 0, "1995-02-20", "彩宝")]
    conn.executemany("INSERT INTO customers(name,phone,level,total_amount,due_amount,birthday,preference) VALUES(?,?,?,?,?,?,?)", customers)

    now = "date('now','localtime')"
    sales = [
        ("XS20260101001", "王晓丽", "13800001111", "足金手镯", None, 21800, 21800, "现金", "2026-01-01", "普通", "已完成"),
        ("XS20260102001", "李强", "13900002222", "钻石耳钉", None, 4280, 4280, "微信", "2026-01-02", "普通", "已完成"),
        ("XS20260103001", "张美凤", "13700003333", "翡翠吊坠", None, 8600, 8600, "刷卡", "2026-01-03", "普通", "已完成"),
    ]
    conn.executemany(
        "INSERT INTO sales(bill_no,customer,phone,product,product_id,amount,paid,method,biz_date,type,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        sales,
    )

    conn.execute("INSERT INTO users(username,password,display_name,role) VALUES('admin','123456','店长','TENANT_ADMIN')")
    conn.execute("INSERT INTO users(username,password,display_name,role) VALUES('staff','123456','店员','EMPLOYEE')")
    conn.execute(
        """INSERT INTO tenant_profiles(tenant_id,name,short_name,slogan,intro,contact,phone,address,hours,categories,published,
                                       showcase_title,showcase_subtitle)
           VALUES('tenant_trial','懿臻珠宝总店','懿臻','懿德 · 臻品 · 云智',
                  '面向中高端珠宝门店的数智化经营，一物一码、RFID 隔空盘点。',
                  '张店长','13800000000','上海市黄浦区南京东路 88 号','10:00-21:00',
                  '黄金,钻石,翡翠,铂金,彩宝',1,'新品橱窗·十月臻选','限量上新 · 到店鉴赏享 9 折礼遇')"""
    )

    deposits = [
        ("王晓丽", "13800001111", 2, "钻石耳钉", 4280, 2000, 2280, "2026-02-15", 7, "已定"),
        ("赵明辉", "13600004444", 1, "足金手镯定制", 32000, 16000, 16000, "2026-02-28", 7, "已定"),
    ]
    conn.executemany("INSERT INTO deposits(customer,phone,product_id,product,total,deposit,balance,promised_date,reminder_days,status) VALUES(?,?,?,?,?,?,?,?,?,?)", deposits)

    loans = [
        ("out", "足金手镯", "J001", "同行老刘", 1, "2026-01-20", "2026-02-20", "借出中"),
        ("in", "钻石戒指", "DR-8899", "供应商A", 1, "2026-01-25", "2026-02-15", "借入中"),
    ]
    conn.executemany("INSERT INTO loans(direction,product,code,party,qty,loan_date,due_date,status) VALUES(?,?,?,?,?,?,?,?)", loans)

    repairs = [
        ("李强", "13900002222", "18K金链", "链扣断裂", 200, 180, "2026-01-18", "2026-01-25", "2026-01-24", "已完成", "王师傅", "配原装链扣"),
        ("张美凤", "13700003333", "翡翠手镯", "轻微裂纹修复", 500, 0, "2026-01-28", "2026-02-10", "", "维修中", "李师傅", ""),
    ]
    conn.executemany("INSERT INTO repairs(customer,phone,item,issue,est_fee,actual_fee,receive_date,promised_date,done_date,status,technician,remark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", repairs)

    purchases = [
        ("深圳金料供应", "足金金料", 100, 45000, "2026-01-05", "2026-01-15", "2026-01-14", "已入库", 45000),
    ]
    conn.executemany("INSERT INTO purchases(supplier,product,qty,cost,order_date,expected_date,received_date,status,paid) VALUES(?,?,?,?,?,?,?,?,?)", purchases)

    outsourcings = [
        ("金艺加工厂", "镶钻吊坠", "18K金", 5.6, 380, 1200, "2026-01-12", "2026-01-22", "", "加工中"),
    ]
    conn.executemany("INSERT INTO outsourcings(factory,product,material,weight,gold_price,labor_fee,send_date,expected_date,received_date,status) VALUES(?,?,?,?,?,?,?,?,?,?)", outsourcings)

    # 为已上架橱窗的 10 件商品生成初始库存流水
    showcase_ids = [
        r[0] for r in conn.execute("SELECT id FROM products WHERE showcase_public=1").fetchall()
    ]
    for pid in showcase_ids:
        conn.execute(
            "INSERT INTO inventory_logs(product_id, type, qty, operator) VALUES(?,'in',1,'init')",
            (pid,),
        )

    conn.execute(
        "INSERT INTO operate_logs(operator,store,action,target,result) VALUES('admin','总店','初始化演示数据（含10件新品橱窗）','jewelry','成功')"
    )
    conn.commit()