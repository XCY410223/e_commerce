"""演示数据初始化脚本。

用法：
    .venv\\Scripts\\python.exe scripts\\seed.py            # 灌入演示数据
    .venv\\Scripts\\python.exe scripts\\seed.py --reset    # 先清空业务表再灌入

会创建：
    6 个商品种类、24 个 SPU、72 个 SKU、首页轮播 / 促销 / 分类展示位、
    以及一个演示账号 demo / demo1234（含一条默认收货地址）。
每个 SKU 生成 3 张占位图，每个品类 12 个 SKU，分类列表可分 2 页。
商品图由 Pillow 现场生成占位图，无需准备素材。
"""

import io
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "apps"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.files.base import ContentFile  # noqa: E402
from django.db import transaction  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from goods.models import (  # noqa: E402
    GoodsImage,
    GoodsSKU,
    GoodsSPU,
    GoodsType,
    IndexGoodsBanner,
    IndexPromotionBanner,
    IndexTypeGoodsBanner,
)
from user.models import Address  # noqa: E402
from user.models import User  # noqa: E402
from promotion.models import (  # noqa: E402
    OrderPromotion,
    Promotion,
    PromotionScope,
    Referral,
    UserCoupon,
)
from promotion.defaults import default_promotion_rules  # noqa: E402

# ---------------------------------------------------------------------------
# 占位图生成
# ---------------------------------------------------------------------------
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",
]
PALETTE = [
    (232, 245, 233), (252, 240, 232), (232, 240, 252), (248, 232, 245),
    (245, 247, 232), (232, 250, 248), (250, 245, 232), (240, 236, 250),
]
INK = (60, 72, 88)


def _font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_image(text, size=(600, 600), seed=0):
    """生成一张带文字的占位图，返回 JPEG 字节。"""
    img = Image.new("RGB", size, PALETTE[seed % len(PALETTE)])
    draw = ImageDraw.Draw(img)
    w, h = size

    # 边框
    draw.rectangle([6, 6, w - 7, h - 7], outline=(210, 218, 228), width=3)

    font = _font(max(20, h // 12))
    lines = []
    for line in str(text).split("\n"):
        lines.append(line)
    total = len(lines) * (font.size + 10)
    y = (h - total) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (w - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=font, fill=INK)
        y += font.size + 10

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


_attach_counter = {"n": 0}

# 置 1 时只建数据不生成图片（用于无写权限的环境 / CI）
SKIP_IMAGES = os.getenv("SEED_SKIP_IMAGES", "0") == "1"


def attach(instance, field_name, text, size=(600, 600)):
    """把生成的占位图挂到 ImageField 上。

    只传文件名，不传目录——目录由字段自己的 upload_to 决定，
    否则会出现 /media/goods/goods/xxx.jpg 这种目录重复。
    """
    _attach_counter["n"] += 1
    if SKIP_IMAGES:
        return
    data = make_image(text, size=size, seed=_attach_counter["n"])
    fname = f"placeholder_{abs(hash(text)) % 10**8}.jpg"
    getattr(instance, field_name).save(fname, ContentFile(data), save=False)


# ---------------------------------------------------------------------------
# 业务数据
# ---------------------------------------------------------------------------
CATALOG = [
    {
        "name": "新鲜水果",
        "logo": "fruit",
        "spus": [
            {
                "name": "烟台红富士苹果",
                "detail": "<p>烟台产区，昼夜温差大，果肉脆甜多汁。</p>"
                          "<ul><li>果径 80-85mm</li><li>冷藏保存可达 30 天</li><li>坏果包赔</li></ul>",
                "skus": [
                    ("烟台红富士苹果 500g", "500g", "脆甜多汁，产地直发", "25.80", "500g", 986, 120),
                    ("烟台红富士苹果 2kg 礼盒", "2kg", "家庭装，送礼佳品", "89.00", "2kg", 320, 68),
                    ("烟台红富士苹果 4kg 整箱", "4kg", "整箱囤货，单价更划算", "168.00", "4kg", 180, 35),
                ],
            },
            {
                "name": "阿克苏冰糖心苹果",
                "detail": "<p>新疆阿克苏产区，果心呈半透明糖心。</p>"
                          "<ul><li>甜度 15 度以上</li><li>霜降后采摘</li><li>产地直发</li></ul>",
                "skus": [
                    ("阿克苏冰糖心苹果 500g", "500g", "糖心明显，甜度极高", "26.80", "500g", 620, 157),
                    ("阿克苏冰糖心苹果 2.5kg", "2.5kg", "家庭分享装，产地直发", "118.00", "2.5kg", 280, 73),
                    ("阿克苏冰糖心苹果 5kg", "5kg", "整箱囤货，送礼皆宜", "218.00", "5kg", 120, 29),
                ],
            },
            {
                "name": "智利车厘子",
                "detail": "<p>智利空运直达，果径均匀，甜脆多汁。</p>"
                          "<ul><li>JJ 级果径 28mm 以上</li><li>全程冷链</li><li>坏果包赔</li></ul>",
                "skus": [
                    ("智利车厘子 JJ级 500g", "500g", "空运直达，果径 28mm", "69.90", "500g", 154, 210),
                    ("智利车厘子 JJ级 1kg", "1kg", "双份装，聚会分享", "129.00", "1kg", 98, 86),
                    ("智利车厘子 JJJ级 500g", "500g", "大果径，颗颗饱满", "89.90", "500g", 76, 132),
                ],
            },
            {
                "name": "海南金钻凤梨",
                "detail": "<p>海南热带产区，果肉金黄，酸甜平衡。</p>"
                          "<ul><li>树上自然成熟</li><li>免泡盐水</li><li>产地直发</li></ul>",
                "skus": [
                    ("海南金钻凤梨 1个", "1个", "单果约 1.5kg，香甜多汁", "19.90", "1个", 360, 243),
                    ("海南金钻凤梨 2个", "2个", "家庭装，现切现吃", "36.90", "2个", 220, 98),
                    ("海南金钻凤梨 4个", "4个", "整箱装，分享更划算", "69.90", "4个", 110, 42),
                ],
            },
        ],
    },
    {
        "name": "海鲜水产",
        "logo": "seafood",
        "spus": [
            {
                "name": "厄瓜多尔白虾",
                "detail": "<p>厄瓜多尔进口，船上急冻锁鲜。</p>"
                          "<ul><li>30/40 规格</li><li>-40℃ 船冻</li><li>化冻不满意可退</li></ul>",
                "skus": [
                    ("厄瓜多尔白虾 500g", "500g", "小份尝鲜，肉质紧实", "69.00", "500g", 560, 145),
                    ("厄瓜多尔白虾 1kg", "1kg", "30/40 规格，个大饱满", "128.00", "1kg", 420, 88),
                    ("厄瓜多尔白虾 2kg", "2kg", "家庭装，囤货更划算", "238.00", "2kg", 260, 41),
                ],
            },
            {
                "name": "挪威三文鱼刺身",
                "detail": "<p>挪威冰鲜三文鱼，全程冷链直达。</p>"
                          "<ul><li>可生食级</li><li>中段厚切</li><li>附赠芥末酱油</li></ul>",
                "skus": [
                    ("挪威三文鱼刺身 300g", "300g", "冰鲜，可直接生食", "98.00", "300g", 210, 65),
                    ("挪威三文鱼刺身 500g", "500g", "加量装，聚餐首选", "158.00", "500g", 160, 32),
                    ("挪威三文鱼刺身 1kg", "1kg", "整段厚切，宴客更尽兴", "298.00", "1kg", 85, 18),
                ],
            },
            {
                "name": "阳澄湖大闸蟹",
                "detail": "<p>阳澄湖水域养殖，公母搭配。</p>"
                          "<ul><li>公 4.5 两 / 母 3.5 两</li><li>活蟹发货</li><li>死蟹包赔</li></ul>",
                "skus": [
                    ("阳澄湖大闸蟹 4对", "4对（8只）", "公 4.5 两 / 母 3.5 两", "368.00", "8只", 96, 42),
                    ("阳澄湖大闸蟹 5对", "5对（10只）", "公母搭配，家宴首选", "448.00", "10只", 66, 25),
                    ("阳澄湖大闸蟹 6对", "6对（12只）", "大份礼盒，鲜活直达", "528.00", "12只", 42, 16),
                ],
            },
            {
                "name": "舟山带鱼段",
                "detail": "<p>东海舟山渔场捕捞，去头去尾中段切块。</p>"
                          "<ul><li>银脂完整</li><li>急冻锁鲜</li><li>适合香煎红烧</li></ul>",
                "skus": [
                    ("舟山带鱼段 500g", "500g", "中段切块，肉质细嫩", "39.90", "500g", 520, 186),
                    ("舟山带鱼段 1kg", "1kg", "家庭装，香煎红烧皆宜", "75.00", "1kg", 330, 94),
                    ("舟山带鱼段 2kg", "2kg", "囤货装，独立分装", "145.00", "2kg", 180, 37),
                ],
            },
        ],
    },
    {
        "name": "精选肉类",
        "logo": "meat",
        "spus": [
            {
                "name": "澳洲牛腱子",
                "detail": "<p>澳洲谷饲牛腱，筋肉比例均匀。</p>"
                          "<ul><li>全程 0-4℃</li><li>可溯源</li><li>适合卤味</li></ul>",
                "skus": [
                    ("澳洲牛腱子 500g", "500g", "小份装，一餐刚好", "62.00", "500g", 260, 74),
                    ("澳洲牛腱子 1kg", "1kg", "卤味首选，筋道不柴", "118.00", "1kg", 142, 58),
                    ("澳洲牛腱子 2kg", "2kg", "家庭装，卤一锅更省心", "228.00", "2kg", 90, 26),
                ],
            },
            {
                "name": "黑猪五花肉",
                "detail": "<p>黑猪冷鲜五花，肥瘦层次分明。</p>"
                          "<ul><li>屠宰后 4 小时内分割</li><li>0-4℃ 冷链</li><li>不含瘦肉精</li></ul>",
                "skus": [
                    ("黑猪五花肉 500g", "500g", "肥瘦相间，适合红烧", "39.90", "500g", 356, 112),
                    ("黑猪五花肉 1kg", "1kg", "家庭装，炖煮更方便", "75.00", "1kg", 240, 47),
                    ("黑猪五花肉 2kg", "2kg", "大份装，分袋冷冻", "146.00", "2kg", 130, 19),
                ],
            },
            {
                "name": "秘制黑椒牛排",
                "detail": "<p>整切牛排腌制入味，煎制方便。</p>"
                          "<ul><li>0-4℃ 冷藏</li><li>煎 3 分钟即食</li><li>附赠黑椒酱</li></ul>",
                "skus": [
                    ("秘制黑椒牛排 5片", "5片", "新手尝鲜，附赠黑椒酱", "52.00", "5片", 300, 143),
                    ("秘制黑椒牛排 10片", "10片", "腌制入味，煎 3 分钟即食", "99.00", "10片", 175, 66),
                    ("秘制黑椒牛排 20片", "20片", "囤货装，家庭西餐常备", "188.00", "20片", 90, 28),
                ],
            },
            {
                "name": "草原羔羊排",
                "detail": "<p>内蒙古草原羔羊，原切肋排，膻味轻。</p>"
                          "<ul><li>6-8 月龄羔羊</li><li>原切不拼接</li><li>适合煎烤</li></ul>",
                "skus": [
                    ("草原羔羊排 500g", "500g", "原切肋排，煎烤皆宜", "68.00", "500g", 210, 64),
                    ("草原羔羊排 1kg", "1kg", "家庭装，周末烤肉", "128.00", "1kg", 135, 31),
                    ("草原羔羊排 2kg", "2kg", "聚餐装，独立分切", "246.00", "2kg", 72, 12),
                ],
            },
        ],
    },
    {
        "name": "蛋奶素食",
        "logo": "daily",
        "spus": [
            {
                "name": "散养土鸡蛋",
                "detail": "<p>散养土鸡，蛋香浓郁。</p>"
                          "<ul><li>当日捡蛋</li><li>冷链直送</li><li>破损包赔</li></ul>",
                "skus": [
                    ("散养土鸡蛋 20枚", "20枚", "散养土鸡，蛋香浓郁", "29.90", "20枚", 468, 189),
                    ("散养土鸡蛋 40枚", "40枚", "家庭装，早餐更省心", "55.00", "40枚", 300, 76),
                    ("散养土鸡蛋 60枚", "60枚", "大份装，烘焙囤货", "79.90", "60枚", 160, 39),
                ],
            },
            {
                "name": "巴氏鲜牛奶",
                "detail": "<p>巴氏杀菌，72 小时鲜。</p>"
                          "<ul><li>冷藏保存</li><li>每日新鲜到仓</li><li>冷链直送</li></ul>",
                "skus": [
                    ("巴氏鲜牛奶 950ml", "950ml", "冷藏保存，72 小时鲜", "19.90", "950ml", 512, 232),
                    ("巴氏鲜牛奶 950ml×2", "950ml×2", "两瓶装，早晚各一瓶", "36.00", "950ml×2", 260, 88),
                    ("巴氏鲜牛奶 950ml×4", "950ml×4", "家庭周装，每日新鲜", "69.00", "950ml×4", 140, 33),
                ],
            },
            {
                "name": "云南高原菌菇拼盘",
                "detail": "<p>云南高原菌菇组合，当日分装。</p>"
                          "<ul><li>5 种菌菇</li><li>清水冲洗即食</li><li>冷链直送</li></ul>",
                "skus": [
                    ("云南高原菌菇拼盘 500g", "500g", "5 种菌菇组合", "42.00", "500g", 118, 44),
                    ("云南高原菌菇拼盘 1kg", "1kg", "家庭装，火锅炖汤皆宜", "78.00", "1kg", 86, 21),
                    ("云南高原菌菇拼盘 2kg", "2kg", "聚餐装，新鲜分装", "148.00", "2kg", 45, 9),
                ],
            },
            {
                "name": "农家石磨豆腐",
                "detail": "<p>非转基因黄豆石磨制作，豆香浓郁。</p>"
                          "<ul><li>当日现做</li><li>0-4℃ 冷藏</li><li>无防腐剂</li></ul>",
                "skus": [
                    ("农家石磨豆腐 400g", "400g", "当日现做，豆香浓郁", "9.90", "400g", 260, 98),
                    ("农家石磨豆腐 800g", "800g", "家庭装，煎炖皆宜", "18.90", "800g", 180, 57),
                    ("农家石磨豆腐 1.2kg", "1.2kg", "大份装，聚餐常备", "26.90", "1.2kg", 120, 26),
                ],
            },
        ],
    },
    {
        "name": "时令蔬菜",
        "logo": "vegetable",
        "spus": [
            {
                "name": "有机小油菜",
                "detail": "<p>有机基地种植，叶片鲜嫩，当日采摘。</p>"
                          "<ul><li>有机认证</li><li>当日采摘</li><li>冷链配送</li></ul>",
                "skus": [
                    ("有机小油菜 300g", "300g", "鲜嫩小棵，清炒即食", "6.90", "300g", 600, 320),
                    ("有机小油菜 600g", "600g", "家庭装，两餐刚好", "12.90", "600g", 420, 180),
                    ("有机小油菜 1.2kg", "1.2kg", "大份装，聚餐常备", "23.90", "1.2kg", 260, 92),
                ],
            },
            {
                "name": "山东水果黄瓜",
                "detail": "<p>山东大棚种植，瓜条顺直，清脆多汁。</p>"
                          "<ul><li>可生食</li><li>当日采摘</li><li>无农残检测</li></ul>",
                "skus": [
                    ("山东水果黄瓜 500g", "500g", "清脆多汁，可直接生食", "9.90", "500g", 500, 267),
                    ("山东水果黄瓜 1kg", "1kg", "家庭装，沙拉凉拌", "18.90", "1kg", 350, 123),
                    ("山东水果黄瓜 2kg", "2kg", "大份装，囤货更划算", "35.90", "2kg", 210, 46),
                ],
            },
            {
                "name": "云南甜脆豌豆尖",
                "detail": "<p>云南高原种植，嫩尖现摘，清甜爽口。</p>"
                          "<ul><li>只取嫩尖</li><li>当日空运</li><li>适合涮煮</li></ul>",
                "skus": [
                    ("云南甜脆豌豆尖 250g", "250g", "嫩尖现摘，清甜爽口", "12.90", "250g", 300, 144),
                    ("云南甜脆豌豆尖 500g", "500g", "家庭装，火锅涮煮", "24.90", "500g", 220, 76),
                    ("云南甜脆豌豆尖 1kg", "1kg", "聚餐装，鲜嫩直达", "46.90", "1kg", 120, 31),
                ],
            },
            {
                "name": "有机贝贝南瓜",
                "detail": "<p>有机种植贝贝南瓜，粉糯香甜，适合蒸煮。</p>"
                          "<ul><li>有机认证</li><li>粉糯栗香</li><li>常温耐放</li></ul>",
                "skus": [
                    ("有机贝贝南瓜 1个", "1个", "单果约 400g，粉糯香甜", "15.90", "1个", 280, 132),
                    ("有机贝贝南瓜 2个", "2个", "家庭装，蒸煮方便", "29.90", "2个", 190, 64),
                    ("有机贝贝南瓜 4个", "4个", "整箱装，早餐常备", "56.90", "4个", 100, 23),
                ],
            },
        ],
    },
    {
        "name": "粮油调味",
        "logo": "pantry",
        "spus": [
            {
                "name": "五常稻花香大米",
                "detail": "<p>黑龙江五常核心产区，稻花香 2 号当季新米。</p>"
                          "<ul><li>当季新米</li><li>真空包装</li><li>米香清甜</li></ul>",
                "skus": [
                    ("五常稻花香大米 2.5kg", "2.5kg", "小包装，米香清甜", "39.90", "2.5kg", 450, 210),
                    ("五常稻花香大米 5kg", "5kg", "家庭装，真空锁鲜", "75.00", "5kg", 280, 95),
                    ("五常稻花香大米 10kg", "10kg", "囤货装，当季新米", "139.00", "10kg", 150, 38),
                ],
            },
            {
                "name": "内蒙古雪花粉",
                "detail": "<p>内蒙古优质小麦研磨，粉质细腻，麦香自然。</p>"
                          "<ul><li>中筋面粉</li><li>适合包子饺子</li><li>无增白剂</li></ul>",
                "skus": [
                    ("内蒙古雪花粉 2.5kg", "2.5kg", "中筋面粉，包子饺子皆宜", "29.90", "2.5kg", 380, 126),
                    ("内蒙古雪花粉 5kg", "5kg", "家庭装，烘焙常备", "56.00", "5kg", 240, 51),
                    ("内蒙古雪花粉 10kg", "10kg", "大份装，面食囤货", "105.00", "10kg", 120, 17),
                ],
            },
            {
                "name": "古法压榨菜籽油",
                "detail": "<p>非转基因油菜籽，小榨工艺，浓香不腻。</p>"
                          "<ul><li>非转基因</li><li>物理压榨</li><li>浓香型</li></ul>",
                "skus": [
                    ("古法压榨菜籽油 1L", "1L", "小瓶装，浓香不腻", "39.90", "1L", 320, 143),
                    ("古法压榨菜籽油 2L", "2L", "家庭装，日常烹饪", "75.90", "2L", 210, 62),
                    ("古法压榨菜籽油 5L", "5L", "大桶装，厨房常备", "179.00", "5L", 95, 22),
                ],
            },
            {
                "name": "零添加特级生抽",
                "detail": "<p>非转基因黄豆酿造，零添加防腐剂，鲜味自然。</p>"
                          "<ul><li>特级酿造</li><li>零添加防腐剂</li><li>180 天发酵</li></ul>",
                "skus": [
                    ("零添加特级生抽 500ml", "500ml", "小瓶装，凉拌炒菜", "16.90", "500ml", 420, 231),
                    ("零添加特级生抽 1L", "1L", "家庭装，厨房常备", "29.90", "1L", 300, 88),
                    ("零添加特级生抽 1.5L", "1.5L", "大瓶装，餐饮更划算", "42.90", "1.5L", 180, 34),
                ],
            },
        ],
    },
]

PROMOTIONS = [
    {
        "slug": "new-customer",
        "name": "新客立减 20 元",
        "summary": "新用户注册后即可参与新人优惠活动，轻松开启第一单。",
        "rules": (
            "1. 活动仅限首次注册的新用户参与。\n"
            "2. 每个账号限参与一次，不可与其他新人活动叠加。\n"
            "3. 具体优惠以下单结算页展示为准。"
        ),
        "category": None,
        "cta_text": "立即注册",
        "cta_url": "/user/register/",
    },
    {
        "slug": "free-shipping",
        "name": "生鲜满 99 免运费",
        "summary": "精选生鲜单笔满 99 元，享受基础配送费减免。",
        "rules": (
            "1. 单笔订单实付满 99 元可参与免运费活动。\n"
            "2. 偏远地区、大件商品及特殊配送区域不参与。\n"
            "3. 具体运费以结算页展示为准。"
        ),
        "category": "新鲜水果",
        "cta_text": "去逛新鲜水果",
        "cta_url": "",
    },
    {
        "slug": "weekend-sale",
        "name": "周末限时 5 折",
        "summary": "周末限定好物，参与商品低至 5 折。",
        "rules": (
            "1. 活动仅限周末期间参与，过期自动结束。\n"
            "2. 折扣商品数量有限，售完即止。\n"
            "3. 具体价格以商品详情页和结算页展示为准。"
        ),
        "category": "海鲜水产",
        "cta_text": "查看活动商品",
        "cta_url": "",
    },
]

DEMO_USER = {"username": "demo", "password": "demo1234", "email": "demo@ecommerce.local"}


def reset_business_data():
    """清空业务表（不动 django_* 系统表与迁移历史）。"""
    OrderGoods = __import__("order.models", fromlist=["OrderGoods"]).OrderGoods
    OrderInfo = __import__("order.models", fromlist=["OrderInfo"]).OrderInfo
    OrderPromotion.all_objects.all().hard_delete()
    Referral.all_objects.all().hard_delete()
    UserCoupon.all_objects.all().hard_delete()
    OrderGoods.all_objects.all().hard_delete()
    OrderInfo.all_objects.all().hard_delete()
    IndexTypeGoodsBanner.all_objects.all().hard_delete()
    IndexPromotionBanner.all_objects.all().hard_delete()
    IndexGoodsBanner.all_objects.all().hard_delete()
    GoodsImage.all_objects.all().hard_delete()
    GoodsSKU.all_objects.all().hard_delete()
    GoodsSPU.all_objects.all().hard_delete()
    GoodsType.all_objects.all().hard_delete()
    Address.all_objects.all().hard_delete()
    User.all_objects.filter(is_superuser=False).hard_delete()
    PromotionScope.all_objects.all().hard_delete()
    Promotion.all_objects.all().hard_delete()
    print("已清空业务数据")


@transaction.atomic
def seed():
    for t_index, cat in enumerate(CATALOG):
        goods_type = GoodsType(name=cat["name"], logo=cat["logo"])
        attach(goods_type, "image", cat["name"], size=(300, 300))
        goods_type.save()

        sku_index = 0
        for spu_data in cat["spus"]:
            spu = GoodsSPU.objects.create(
                name=spu_data["name"], detail=spu_data["detail"]
            )

            for name, spec, desc, price, unite, stock, sales in spu_data["skus"]:
                sku = GoodsSKU(
                    type=goods_type,
                    spu=spu,
                    name=name,
                    spec=spec,
                    desc=desc,
                    price=price,
                    unite=unite,
                    stock=stock,
                    sales=sales,
                    status=GoodsSKU.STATUS_ONLINE,
                )
                sku.save()

                # 第一张作为主图，另外两张作为详情图
                for i in range(3):
                    gi = GoodsImage(sku=sku, is_main=i == 0)
                    label = name if i == 0 else f"{name}\n细节图 {i}"
                    attach(gi, "image", label, size=(600, 600) if i == 0 else (400, 400))
                    gi.save()

                if t_index == 0 and sku_index < 4:
                    banner = IndexGoodsBanner(sku=sku, index=sku_index)
                    attach(banner, "image", f"轮播\n{name}", size=(900, 320))
                    banner.save()

                if sku_index < 4:
                    IndexTypeGoodsBanner.objects.create(
                        type=goods_type,
                        sku=sku,
                        display_type=IndexTypeGoodsBanner.DISPLAY_TYPE_SKU,
                        index=sku_index,
                    )

                sku_index += 1

    types_by_name = {goods_type.name: goods_type for goods_type in GoodsType.objects.all()}
    promotions_by_slug = {}
    for data in default_promotion_rules():
        promotion = Promotion.objects.create(**data)
        promotions_by_slug[promotion.slug] = promotion
    weekend_promotion = promotions_by_slug["weekend-sale"]
    seafood = types_by_name.get("海鲜水产")
    if seafood is not None:
        PromotionScope.objects.create(promotion=weekend_promotion, category=seafood)

    for i, data in enumerate(PROMOTIONS):
        promo = IndexPromotionBanner(
            promotion=promotions_by_slug.get(data["slug"]),
            slug=data["slug"],
            name=data["name"],
            summary=data["summary"],
            rules=data["rules"],
            category=types_by_name.get(data["category"]),
            cta_text=data["cta_text"],
            cta_url=data["cta_url"],
            index=i,
        )
        attach(promo, "image", data["name"], size=(400, 160))
        promo.save()

    user, created = User.objects.get_or_create(
        username=DEMO_USER["username"],
        defaults={"email": DEMO_USER["email"], "is_active": True},
    )
    if created:
        user.set_password(DEMO_USER["password"])
        user.save(update_fields=["password"])

    if not Address.objects.filter(user=user).exists():
        Address.objects.create(
            user=user,
            receiver="演示用户",
            addr="示例省示例市示例区演示路 1 号",
            phone="13800000000",
            zip_code="000000",
            is_default=True,
        )

    return {
        "types": GoodsType.objects.count(),
        "spus": GoodsSPU.objects.count(),
        "skus": GoodsSKU.objects.count(),
        "images": GoodsImage.objects.count(),
        "banners": IndexGoodsBanner.objects.count(),
        "promos": IndexPromotionBanner.objects.count(),
        "promotion_rules": Promotion.objects.count(),
        "type_banners": IndexTypeGoodsBanner.objects.count(),
        "user": DEMO_USER,
    }


if __name__ == "__main__":
    if "--no-images" in sys.argv:
        SKIP_IMAGES = True
    if "--reset" in sys.argv:
        reset_business_data()
    stat = seed()
    print("演示数据已就绪：")
    print(f"  商品种类 {stat['types']} | SPU {stat['spus']} | SKU {stat['skus']} | 商品图 {stat['images']}")
    print(f"  首页轮播 {stat['banners']} | 促销活动 {stat['promos']} | 分类展示位 {stat['type_banners']}")
    print(f"  演示账号：{stat['user']['username']} / {stat['user']['password']}")
    print(f"  图片输出目录：{BASE_DIR / 'media'}")
