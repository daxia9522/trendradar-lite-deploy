# coding=utf-8
"""行情数据流识别与事件聚簇的回归测试。

覆盖已实测的真实缺陷：
1. 模板化行情播报占用叙事分析配额、并在周报里被合并成 span7d 假事件簇；
2. 数字加权在跨源比较时把同一事件的不同转述压到阈值之下；
3. 单一来源霸榜与关键词组整组落榜；
4. 强/弱特征分档：业绩播报类弱特征仅对快讯来源生效，
   避免误伤精选榜上的财报解读选题；
5. 24 天生产语料标定出的六类漏判（地方债发行、成交额、期货开盘、
   前值非 % 单位、截至X盘收盘、指数涨跌至N点）。
"""

import unittest

from trendradar.ai.dataflow import (
    is_market_dataflow_title,
    is_wire_source,
    split_market_dataflow,
)
from trendradar.ai.selector import (
    SOURCE_CAP_RATIO,
    SelectedNewsCluster,
    _select_with_quota,
    select_ai_news,
)

WIRE = "华尔街见闻 快讯"
CURATED = "华尔街见闻 最热"


class MarketDataflowTests(unittest.TestCase):
    def test_identifies_market_dataflow_templates(self):
        cases = (
            "上证指数早盘收报3876.38点，跌0.14%。 深证成指早盘收报13687.06点",
            "国债期货早盘收盘，30年期主力合约跌0.10%，10年期主力合约跌0.01%",
            "日本20年期国债收益率下行6个基点至3.715%",
            "德国8月IFO商业现况指数 88.5，预期 87，前值 86.5。",
            "美国财政部拍卖三个月期国债，得标利率3.715%，投标倍数3.08。",
            "隔夜SHIBOR报1.3840%，上涨0.30个基点。",
            "两市融资余额减少16.88亿元",
            "富时中国A50指数期货盘初跌0.21%，上一个交易日夜盘收跌0.20%。",
            "上海钢联发布数据显示，今日MMLC电池级碳酸锂中间价报150950元/吨",
            "WTI 10月原油期货收跌2.05美元，跌幅超过2.35%，报85.01美元/桶。",
            "纳斯达克金龙中国指数收涨1.11%，报6268.26点。",
            "美国7月新屋销售 60.7万户，预期 62万户，前值 62.8万户。",
        )
        for title in cases:
            with self.subTest(title=title):
                self.assertTrue(is_market_dataflow_title(title))

    def test_strong_features_recovered_from_production_corpus(self):
        """24 天生产语料标定的六类漏判，修复后应全部命中且不依赖来源。

        修复前这六类合计漏判 197 条：地方债发行 48、成交额 33、
        期货开盘 17、前值非 % 单位 60、截至X盘收盘 14、指数涨跌至N点 25。
        """
        cases = (
            # 地方债/政金债发行（旧周报 RULE_DATA_TEMPLATE_TITLE_RE 覆盖，抽模块时丢失）
            "河北发行5年期一般债地方债，规模33.4600亿元，发行利率1.4800%，"
            "边际倍数1.85倍，倍数预期1.48",
            # 成交额播报
            "沪深两市今日成交额合计25231.03亿元",
            "沪深京三市成交额超2万亿元，较上日此时缩量2074亿元。",
            # 期货开盘播报
            "夜盘期货开盘，原油、燃油跌逾3%，低硫燃油跌逾2%",
            "商品期货开盘，纯碱、乙二醇主力合约涨超3%，燃料油涨超2%",
            # 前值 + 非百分号单位（含外币，故用通用单位槽而非枚举）
            "美国7月ADP就业人数变动 4.4万人，预期 6.5万人，前值 9.8万人。",
            "越南7月贸易余额 -35.9亿美元，前值 -26.44亿美元。",
            "意大利6月对欧盟贸易帐 15.77亿欧元，前值 8.45亿欧元。",
            "英国7月公共部门净借款 18亿英镑，预期 0亿英镑，前值 160亿英镑。",
            "日本8月7日当周外资净买进日本股票 -3685亿日元，前值 -3925亿日元。",
            "新西兰7月出口 73.9亿纽元，前值 80.9亿纽元。",
            # 截至X盘收盘
            "截至早盘收盘，日经225指数跌1.7%，东证指数跌2.2%。",
            # 指数涨跌 + 至N点
            "台湾证交所加权股价指数上涨1%，至43,552.65点。",
            "波罗的海干散货运价指数涨4.06%，至2843点。",
        )
        for title in cases:
            with self.subTest(title=title):
                self.assertTrue(is_market_dataflow_title(title))

    def test_shibor_matches_without_ascii_word_boundary(self):
        """旧版写作 \\bSHIBOR\\b，中文语境下词边界不成立，实测全语料命中 0 次。"""
        title = "隔夜SHIBOR报1.4130%，上涨0.30个基点。"
        self.assertTrue(is_market_dataflow_title(title))

    def test_earnings_templates_are_wire_gated(self):
        """业绩播报是弱特征：快讯流里是模板，精选榜上是选题。"""
        cases = (
            "中兴通讯：2026年上半年净利润27.53亿元，同比下降45.57%",
            "沃尔核材：上半年净利润5.67亿元，同比增长1.54%",
            "华大基因：2026年半年度净利润4740.46万元，同比增长718.24%",
            "合盛硅业：2026年上半年净利润3.24亿元，同比扭亏为盈",
            "温氏股份：2026年上半年净利润亏损43.66亿元，同比由盈转亏",
            "珀莱雅：上半年净利润11.68亿元，同比增长46.26%；拟10派11.80元",
            # 旧版要求「净利润」紧邻数字，漏掉「为/约」中缀
            "药明康德：上半年净利润为110.8亿元，同比增长33.7%",
            "纳芯微：预计2026年上半年净利润约6700万元，同比扭亏为盈",
            # 涨跌幅扩大：纯报价时是模板，但「油价跌幅扩大至4%！贝森特：…」
            # 这类带叙事从句的标题同样命中，故归为弱特征由来源门控。
            "韩国首尔综指涨幅扩大至2%。",
        )
        for title in cases:
            with self.subTest(title=title):
                self.assertTrue(is_market_dataflow_title(title, WIRE))
                self.assertFalse(is_market_dataflow_title(title, CURATED))
                # 省略来源时按保守口径处理，不误伤叙事新闻
                self.assertFalse(is_market_dataflow_title(title))

    def test_curated_analysis_is_not_flagged_as_dataflow(self):
        """精选榜上的财报/宏观解读是叙事选题，不得被打成模板流。

        全部为修复前实测出的假阳性样本。
        """
        cases = (
            ("SpaceX首份财报出炉：二季度营收同比增长92% 资本开支飙升至184亿美元",
             "财联社热门"),
            ("腾讯Q2营收同比增长11%超预期，算力采购大幅推高资本开支至527.8亿元，"
             "自由现金流转负 | 财报见闻", CURATED),
            ("贵州茅台上半年归母净利润同比下降 1.95%，这意味着什么？", "知乎"),
            ("逼近历史警戒线！美债长端融资成本飙升，30年期得标利率创2001年最高",
             CURATED),
            ("油价跌幅扩大至4%！贝森特：明日美伊或达成协议以开放霍尔木兹海峡",
             CURATED),
            ("中国7月CPI同比涨幅收窄至0.5%，PPI同比上涨3.5%，"
             "AI驱动平板电脑价格环比上涨11.3%", CURATED),
            ("江波龙上半年净利润同比增长71529%，拟最高8亿元回购股份 | 财报见闻",
             CURATED),
        )
        for title, source in cases:
            with self.subTest(title=title):
                self.assertFalse(is_market_dataflow_title(title, source))

    def test_wire_source_detection(self):
        """来源判定必须对着 config 里真实存在的 id 与展示名断言。

        曾用自造名「中新网即时滚动」写测试，测试绿但与真实配置无关：
        实际展示名是「中新网即时」，并不含「滚动」。
        """
        for source in (WIRE, "wallstreetcn-quick", "7x24快讯"):
            with self.subTest(source=source):
                self.assertTrue(is_wire_source(source))
        # 以下均为 config/config.yaml 里实际配置的值
        for source in (
            CURATED, "wallstreetcn-hot", "财联社热门", "cls-hot",
            "中新网即时", "chinanews-scroll",
            "百度热搜", "baidu", "知乎", "zhihu", "拖音", "douyin",
            "微博", "weibo", "澎湃新闻", "thepaper", "凤凰网", "ifeng",
            "参考消息", "cankaoxiaoxi", "联合早报", "zaobao",
            "BBC World", "bbc-world", "卫报 World", "guardian-world",
            "", None,
        ):
            with self.subTest(source=source):
                self.assertFalse(is_wire_source(source))

    def test_only_quick_feed_is_gated_in_current_config(self):
        """锁住弱特征的生效范围：当前 14 个配置来源中只有快讯流开启。

        若日后新增快讯/滚动类来源，此用例会失败，提醒同步评估。
        实测：将 chinanews-scroll 纳入门控仅影响 4/7186 条（0.056%），
        且那 4 条均带编辑叙事（如「物业收益带动盈利攀升」「韩股又熔断了」），
        留在叙事池更正确，故不纳入。
        """
        configured = {
            "百度热搜", "澎湃新闻", "凤凰网", "参考消息", "联合早报",
            "华尔街见闻 最热", "华尔街见闻 快讯", "财联社热门",
            "微博", "拖音", "知乎",
            "中新网即时", "BBC World", "卫报 World",
        }
        gated = {name for name in configured if is_wire_source(name)}
        self.assertEqual(gated, {"华尔街见闻 快讯"})

    def test_source_accepts_platform_collections(self):
        """周报侧传入的是平台集合，任一平台为快讯即启用弱特征。"""
        title = "沃尔核材：上半年净利润5.67亿元，同比增长1.54%"
        self.assertTrue(is_market_dataflow_title(title, {CURATED, WIRE}))
        self.assertFalse(is_market_dataflow_title(title, {CURATED, "知乎"}))

    def test_does_not_flag_narrative_news(self):
        """叙事新闻即便含数字或“预期”也不得被判为行情流。"""
        cases = (
            "美国波士顿联储主席Susan Collins：当前密切留意通胀预期。",
            "高盛将日本央行下次加息时间点预期提前至9月",
            "高盛大幅上调全球半导体设备支出预期 行业景气有望持续至2028年",
            "A股电力股异动拉升，郴电国际直线涨停，新中港此前封板",
            "赛力斯中报巨亏，问界 M6 走量预期落空，如何评价华为智选车模式？",
            "中情局局长赴俄会谈，讨论乌克兰停火框架",
            "大摩：AI相关表外承诺突破3万亿美元",
            "歼-10CE首次公开亮相，24架编队通场",
            "在韩国的中国女留学生遇害案引发关注",
            # 叙事型财经报道：含数字但不是模板播报
            "英伟达财报超预期，数据中心业务成为核心引擎",
            "拼多多Q2营收超预期，海外业务增速亮眼",
            "比亚迪拟投资50亿元建设新电池工厂",
            "小米汽车交付量创新高，单月突破4万辆",
            "某公司因财务造假被罚，涉及虚增利润数亿元",
        )
        for title in cases:
            with self.subTest(title=title):
                self.assertFalse(is_market_dataflow_title(title))
                # 即便来自快讯流，叙事新闻也不应命中任何一档特征
                self.assertFalse(is_market_dataflow_title(title, WIRE))

    def test_requires_digits(self):
        self.assertFalse(is_market_dataflow_title("国债期货收盘"))
        self.assertFalse(is_market_dataflow_title(""))
        self.assertFalse(is_market_dataflow_title(None))

    def test_split_partitions_all_items(self):
        items = [
            {"title": "隔夜SHIBOR报1.3840%，上涨0.30个基点。"},
            {"title": "中情局局长赴俄会谈"},
        ]
        narrative, dataflow = split_market_dataflow(items)
        self.assertEqual(len(narrative), 1)
        self.assertEqual(len(dataflow), 1)
        self.assertEqual(narrative[0]["title"], "中情局局长赴俄会谈")

    def test_split_honours_source_key(self):
        items = [
            {"title": "沃尔核材：上半年净利润5.67亿元，同比增长1.54%", "src": WIRE},
            {"title": "沃尔核材：上半年净利润5.67亿元，同比增长1.54%", "src": CURATED},
        ]
        narrative, dataflow = split_market_dataflow(items, source_key="src")
        self.assertEqual(len(dataflow), 1)
        self.assertEqual(dataflow[0]["src"], WIRE)
        self.assertEqual(len(narrative), 1)
        self.assertEqual(narrative[0]["src"], CURATED)


class ConservativeEventSelectionTests(unittest.TestCase):
    """关键词候选先精确去重，再做保守事件聚类和当前式评分。"""

    @staticmethod
    def _stats(groups):
        return [
            {"word": f"测试组{index}", "titles": titles}
            for index, titles in enumerate(groups)
        ]

    @staticmethod
    def _item(title, source, rank=1, count=1, is_new=False):
        return {
            "title": title,
            "source_name": source,
            "ranks": [rank],
            "count": count,
            "is_new": is_new,
        }

    def test_same_source_same_title_is_deduped(self):
        item = self._item("同一媒体同一标题", "凤凰网", rank=1)
        selection = select_ai_news(self._stats([[item, dict(item)]]), None, 10)
        self.assertEqual(selection.selected_count, 1)

    def test_same_title_from_different_sources_is_one_event(self):
        stats = self._stats([
            [self._item("同一标题", "凤凰网", rank=1)],
            [self._item("同一标题", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 1)
        self.assertEqual(selection.clusters[0].sources, {"凤凰网", "参考消息"})
        self.assertEqual(len(selection.clusters[0].member_items), 2)

    def test_high_similarity_titles_are_clustered(self):
        stats = self._stats([
            [self._item("西藏吉隆泥石流灾害原因现已查明", "凤凰网", rank=1)],
            [self._item("西藏吉隆泥石流灾害原因查明", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 1)

    def test_conflicting_directions_are_not_clustered(self):
        stats = self._stats([
            [self._item("某公司季度利润同比上涨5%", "凤凰网", rank=1)],
            [self._item("某公司季度利润同比下跌5%", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 2)

    def test_disjoint_fact_numbers_are_not_clustered(self):
        stats = self._stats([
            [self._item("西藏泥石流已致7人遇难554人失联", "凤凰网", rank=1)],
            [self._item("西藏泥石流已致16人遇难546人失联", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 2)

    def test_competition_stages_are_not_clustered(self):
        stats = self._stats([
            [self._item("中国女排晋级亚锦赛半决赛", "凤凰网", rank=1)],
            [self._item("中国女排晋级亚锦赛决赛", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 2)

    def test_warning_levels_are_not_clustered(self):
        stats = self._stats([
            [self._item("水利部联合发布红色山洪灾害气象预警", "凤凰网", rank=1)],
            [self._item("水利部联合发布橙色山洪灾害气象预警", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 2)

    def test_different_explicit_dates_are_not_clustered(self):
        stats = self._stats([
            [self._item("8月29日发布山洪灾害预警", "凤凰网", rank=1)],
            [self._item("8月30日发布山洪灾害预警", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 2)

    def test_keyword_matches_are_merged_after_dedupe(self):
        item = self._item("多关键词命中", "凤凰网", rank=1)
        stats = [
            {"word": "关键词A", "titles": [item]},
            {"word": "关键词B", "titles": [item]},
        ]
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 1)
        self.assertEqual(
            selection.clusters[0].representative_item["_ai_keywords"],
            ["关键词A", "关键词B"],
        )

    def test_higher_score_news_is_selected_first(self):
        stats = self._stats([[
            self._item("低热度新闻", "凤凰网", rank=50),
            self._item("高热度新闻", "参考消息", rank=1),
        ]])
        selection = select_ai_news(stats, None, 10)
        titles = [
            cluster.representative_item["title"] for cluster in selection.clusters
        ]
        self.assertEqual(titles[0], "高热度新闻")

    def test_source_cap_enforced_when_alternatives_exist(self):
        stats = self._stats([
            [self._item(f"A源新闻{i}", "A", rank=i + 1) for i in range(20)],
            [self._item(f"B源新闻{i}", "B", rank=i + 1) for i in range(20)],
            [self._item(f"C源新闻{i}", "C", rank=i + 1) for i in range(20)],
            [self._item(f"D源新闻{i}", "D", rank=i + 1) for i in range(20)],
        ])
        selection = select_ai_news(stats, None, 10, source_cap_ratio=0.30)
        counts = {}
        for cluster in selection.clusters:
            source = next(iter(cluster.sources))
            counts[source] = counts.get(source, 0) + 1
        self.assertEqual(len(selection.clusters), 10)
        self.assertLessEqual(counts.get("A", 0), 3)

    def test_source_cap_is_soft_when_sources_are_insufficient(self):
        stats = self._stats([
            [self._item(f"A源新闻{i}", "A", rank=i + 1) for i in range(10)],
        ])
        selection = select_ai_news(stats, None, 10, source_cap_ratio=0.30)
        self.assertEqual(selection.selected_count, 10)

    def test_selection_respects_total_limit(self):
        stats = self._stats([
            [self._item(f"新闻{i}", "媒体A", rank=i + 1) for i in range(400)],
        ])
        selection = select_ai_news(stats, None, 120, source_cap_ratio=0.30)
        self.assertLessEqual(selection.selected_count, 120)

    def test_unrelated_titles_are_not_forced_into_one_event(self):
        stats = self._stats([
            [self._item("宇树科技发布新一代机器人", "凤凰网", rank=1)],
            [self._item("美联储官员释放鹰派信号", "参考消息", rank=2)],
        ])
        selection = select_ai_news(stats, None, 10)
        self.assertEqual(selection.selected_count, 2)


class QuotaConstantTests(unittest.TestCase):
    def test_quota_constants_are_sane(self):
        self.assertGreater(SOURCE_CAP_RATIO, 0.0)
        self.assertLess(SOURCE_CAP_RATIO, 1.0)

    def test_source_cap_balances_selection_when_alternatives_exist(self):
        clusters = []
        for source_index, source in enumerate(("A", "B", "C", "D")):
            for item_index in range(10):
                clusters.append(SelectedNewsCluster(
                    cluster_index=source_index * 10 + item_index,
                    representative_item={
                    "title": f"{source}来源独立事件{item_index}",
                    "source_name": source,
                    "ranks": [item_index + 1 + source_index * 10],
                    "count": 1,
                    "is_new": False,
                    },
                    sources={source},
                    score=100 - source_index * 10 - item_index,
                ))

        selection = _select_with_quota(clusters, 10, source_cap_ratio=0.30)
        source_counts = {}
        for cluster in selection:
            source = cluster.representative_item["source_name"]
            source_counts[source] = source_counts.get(source, 0) + 1

        self.assertEqual(len(selection), 10)
        self.assertLessEqual(source_counts.get("A", 0), 3)


class DataflowExclusionTests(unittest.TestCase):
    """行情流不得进入 AI 分析输入。

    旧版给行情流留 DATAFLOW_CAP_RATIO=5% 名额，实测发现：
    - 叙事候选簇每日 433~481 个，远超名额上限，该阶段从未执行到，
      参数实际为死参数（limit 降到 30 仍为 0）；
    - 但四阶段补齐仍会引入行情流。
    现改为彻底排除，本类锁住这一契约。
    """

    @staticmethod
    def _stats(titles, source_name="华尔街见闻 快讯"):
        return [{
            "word": "测试组",
            "titles": [
                {
                    "title": title,
                    "source_name": source_name,
                    "ranks": [1],
                    "count": 1,
                    "is_new": False,
                }
                for title in titles
            ],
        }]

    def test_dataflow_never_selected_even_when_slots_remain(self):
        """叙事不足时宁可留空，也不用模板数字填充。"""
        stats = self._stats([
            "中情局局长赴俄会谈，讨论乌克兰停火框架",
            "隔夜SHIBOR报1.3840%，上涨0.30个基点。",
            "两市融资余额减少16.88亿元",
            "美国7月新屋销售 60.7万户，预期 62万户，前值 62.8万户。",
        ])
        selection = select_ai_news(stats, None, 50)
        titles = [
            c.representative_item.get("title", "") for c in selection.clusters
        ]
        self.assertEqual(titles, ["中情局局长赴俄会谈，讨论乌克兰停火框架"])
        for cluster in selection.clusters:
            self.assertFalse(cluster.is_dataflow)

    def test_all_dataflow_input_yields_empty_selection(self):
        """全部是行情流时，输入为空而非退化为全选。"""
        stats = self._stats([
            "隔夜SHIBOR报1.3840%，上涨0.30个基点。",
            "两市融资余额减少16.88亿元",
        ])
        selection = select_ai_news(stats, None, 50)
        self.assertEqual(selection.selected_count, 0)

    def test_curated_earnings_still_reach_ai(self):
        """精选榜财报解读不是行情流，不得被排除。"""
        title = "腾讯Q2营收同比增长11%超预期，算力采购大幅推高资本开支 | 财报见闻"
        selection = select_ai_news(
            self._stats([title], source_name="华尔街见闻 最热"), None, 50
        )
        self.assertEqual(selection.selected_count, 1)
        self.assertFalse(selection.clusters[0].is_dataflow)


if __name__ == "__main__":
    unittest.main()
