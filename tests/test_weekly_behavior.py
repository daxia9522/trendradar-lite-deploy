"""Deterministic weekly collection, evidence and presentation contracts."""
import json
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from weekly_report import weekly_ai_report_email as weekly
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from trendradar.report.weekly import render_weekly_html
from weekly_report import analysis, collection


class SyntheticHistory:
    def read_all_titles_for_date(self, *, date, db_type):
        title = "日本发生地震" if db_type == "news" else "火箭发射成功"
        return ({f"{db_type}{i}": {title: {"ranks": [i], "count": 2}} for i in (1, 2)},
                {f"{db_type}{i}": f"{db_type} source {i}" for i in (1, 2)}, {})


class WeeklyBehaviorTests(unittest.TestCase):
    def test_collection_merges_days_but_keeps_news_and_rss_pools(self):
        with patch.object(collection, "HistoryReader", return_value=SyntheticHistory()):
            items, platforms, stats = collection.collect_news(datetime(2026, 9, 29), datetime(2026, 9, 30), max_news=10)
        self.assertEqual((stats["raw_news"], stats["raw_rss"]), (4, 4))
        self.assertEqual((stats["exact_total"], stats["cluster_total"], stats["selected"]), (2, 2, 2))
        self.assertEqual((stats["selected_news"], stats["selected_rss"]), (1, 1))
        self.assertEqual(stats["rss_day_coverage"], 1)
        self.assertEqual(sorted(platforms.values()), [2, 2, 2, 2])
        for item in items:
            self.assertEqual(item["count"], 8)
            self.assertEqual(item["dates"], ["2026-09-29", "2026-09-30"])
            self.assertEqual(len(item["platforms"]), 2)
        evidence = analysis.build_evidence_index(items)
        self.assertEqual(set(evidence), {"N1", "R1"})
        self.assertEqual(evidence["N1"]["source_type"], "news")
        messages = analysis.build_prompt("2026-09-29", "2026-09-30", items, platforms, stats)
        text = messages[-1]["content"]
        for expected in ("N1.", "R1.", "日本发生地震", "火箭发射成功", '"raw_news": 4', '"selected_rss": 1'):
            self.assertIn(expected, text)

    def test_themes_require_real_evidence_and_preserve_markdown(self):
        evidence = {"N1": {}, "N2": {}}
        themes = [{"title": "震后救援", "keyword": "日本地震", "evidence_ids": ["n1", "N1", "N2", "N99"]},
                  {"title": "虚构主题", "keyword": "火箭发射", "evidence_ids": ["N99", "N100"]}]
        body = "# 日本地震\n\n救援继续。"
        raw = f"<THEMES_JSON>{json.dumps(themes, ensure_ascii=False)}</THEMES_JSON><REPORT_MARKDOWN>{body}</REPORT_MARKDOWN>"
        report, parsed = analysis.parse_structured_report(raw, evidence)
        self.assertEqual(report, body)
        self.assertEqual(parsed, [{"title": "震后救援", "evidence_ids": ["N1", "N2"]}])
        self.assertNotIn("keyword", parsed[0])

    def test_keyword_model_failure_returns_empty_without_rules(self):
        parent = analysis.AIClient(CONFIG)
        failed = FakeKeywordClient([RuntimeError("unavailable"), RuntimeError("unavailable")])
        with patch.object(analysis, "build_keyword_client", return_value=failed):
            result = analysis.generate_keywords(parent, "2026-09-29", "2026-09-30", BODY)
        self.assertEqual(result["source"], "keyword_empty")
        self.assertEqual(result["final"], [])
        self.assertEqual(len(failed.calls), 2)
        self.assertFalse(hasattr(analysis, "build_rule_entity_headlines"))

    def test_html_preserves_escaping_lists_and_injected_clock(self):
        rendered = render_weekly_html("标题 <script>", "2026-09-29 ~ 2026-09-30", "synthetic/model",
            {"Top5关键词": "日本地震 / 火箭发射"}, "# 新闻周报\n\n统计周期：重复字段\n\n## 正文\n- **重点**\n- <script>unsafe</script>\n\n1. 第一项",
            generated_at="2026-10-01 07:30:00")
        self.assertIn("2026-10-01 07:30:00", rendered)
        self.assertIn("<strong>重点</strong>", rendered)
        self.assertIn("<ol>", rendered)
        self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("重复字段", rendered)


BODY = '巴拿马深源强震影响航运。新质生产力持续推进。英伟达发布财报。美联储降息传闻被否认。'
CONFIG = {'MODEL': 'gemini/main-analysis', 'API_KEY': 'synthetic-not-a-secret',
          'FALLBACK_MODELS': ['gemini/gemini-3.5-flash-lite'], 'TIMEOUT': 2}

def payload(*pairs):
    return json.dumps([{'label': label, 'evidence': quote} for label, quote in pairs], ensure_ascii=False)

class FakeKeywordClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.model = CONFIG['FALLBACK_MODELS'][0]
        self.last_model = None
        self.fallback_models = []
        self.candidates = (SimpleNamespace(id="fallback:1"),)

    def chat(self, messages):
        self.calls.append(messages)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        self.last_model = self.model
        return value

class KeywordValidationTests(unittest.TestCase):
    def test_long_labels_preserved(self):
        for word in ['新质生产力', '巴拿马强震', '北海道地震', '以色列地震', '中国航天科技', '利雅得机场遭袭']:
            with self.subTest(word=word):
                value, reason = analysis.validate_keyword({'label': word, 'evidence': word}, word)
                self.assertEqual(reason, 'accepted')
                self.assertEqual(value['label'], word)

    def test_label_may_summarize_noncontiguous_source(self):
        self.assertNotIn('巴拿马强震', BODY)
        value, reason = analysis.validate_keyword({'label': '巴拿马强震', 'evidence': '巴拿马深源强震'}, BODY)
        self.assertEqual(reason, 'accepted')
        self.assertEqual(value['label'], '巴拿马强震')

    def test_normalization_only_removes_outer_format(self):
        for raw, expected in [(' 【巴拿马强震】 ', '巴拿马强震'), ('“新质生产力”', '新质生产力'),
                              ('GPT-6', 'GPT-6'), ('C++', 'C++'), ('.NET', '.NET'),
                              ('Apple Watch', 'Apple Watch')]:
            with self.subTest(raw=raw):
                self.assertEqual(analysis._normalize_headline_token(raw), expected)

    def test_decimal_event_numbers_are_never_treated_as_numbering(self):
        for label in ['7.5级地震', '3.5模型更新', '1.5万裁员']:
            value, reason = analysis.validate_keyword({'label': label, 'evidence': label}, label)
            self.assertEqual(reason, 'accepted')
            self.assertEqual(value['label'], label)

    def test_boundaries_never_truncate(self):
        for count, expected in [(1, 'too_short'), (2, 'accepted'), (12, 'accepted'), (13, 'too_long')]:
            label = '甲' * count
            result, reason = analysis.validate_keyword({'label': label, 'evidence': label}, label)
            self.assertEqual(reason, expected)
            self.assertEqual(result['label'] if result else None, label if expected == 'accepted' else None)

    def test_shape_and_evidence_rejections(self):
        for item, reason in [('文字', 'invalid_item_type'), ({'label': 123, 'evidence': '正文'}, 'invalid_field_type'),
                             ({'label': '标签'}, 'invalid_field_type'), ({'label': '', 'evidence': '正文'}, 'empty_label'),
                             ({'label': '新闻', 'evidence': '正文'}, 'banned_label'),
                             ({'label': '标签', 'evidence': ''}, 'empty_evidence'),
                             ({'label': '标签', 'evidence': '不存在'}, 'evidence_not_in_report'),
                             ({'label': '标签', 'evidence': '甲' * 241}, 'evidence_too_long'),
                             ({'label': '标\n签', 'evidence': '正文'}, 'control_character')]:
            with self.subTest(reason=reason):
                self.assertEqual(analysis.validate_keyword(item, '正文')[1], reason)

    def test_semantic_entailment_is_not_claimed(self):
        # This deliberately documents the limit: a valid quote alone is not entailment.
        _, reason = analysis.validate_keyword({'label': '巴拿马震', 'evidence': '巴拿马震后'}, '巴拿马震后')
        self.assertEqual(reason, 'accepted')

    def test_prompt_loaded_and_filled_without_four_char_contract(self):
        messages = analysis.build_keyword_prompt('2026-10-05', '2026-10-11', BODY)
        text = messages[-1]['content']
        self.assertIn(BODY, text)
        self.assertNotIn('{max_labels}', text)
        self.assertIn('必须是2～6个字符', text)
        self.assertNotIn('最多 12 个字符', text)
        self.assertNotIn('本身超过6字时才例外', text)
        self.assertNotIn('2~4', text)
        self.assertNotIn('2至4', analysis.WEEKLY_AI_PROMPT_FILE.read_text())

class KeywordGenerationTests(unittest.TestCase):
    def run_fake(self, outputs, config=None):
        client = FakeKeywordClient(outputs)
        with patch.object(analysis, 'build_keyword_client', return_value=client) as factory:
            result = analysis.generate_keywords(analysis.AIClient(CONFIG if config is None else config), '2026-10-05', '2026-10-11', BODY)
        return result, client, factory

    def test_one_valid_label_is_success_and_no_extra_attempt(self):
        result, client, _ = self.run_fake([payload(('巴拿马强震', '巴拿马深源强震'))])
        self.assertEqual(result['final'], ['巴拿马强震'])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result['final_details'][0]['model'], CONFIG['FALLBACK_MODELS'][0])
        self.assertEqual(result['source'], 'keyword_ai')

    def test_missing_fallback_never_constructs_main_client(self):
        for fallback in [[], None, '']:
            result, client, factory = self.run_fake([], {**CONFIG, 'FALLBACK_MODELS': fallback})
            self.assertEqual(result['reason'], 'missing_keyword_model')
            factory.assert_not_called()
            self.assertEqual(client.calls, [])

    def test_real_factory_clears_fallback_chain(self):
        client = analysis.build_keyword_client(CONFIG)
        self.assertEqual(client.model, CONFIG['FALLBACK_MODELS'][0])
        self.assertEqual(client.fallback_models, [])

    def test_bad_json_is_retried_with_feedback(self):
        result, client, _ = self.run_fake(['not json', payload(('新质生产力', '新质生产力'))])
        self.assertEqual(result['source'], 'keyword_ai_retry')
        self.assertEqual(len(client.calls), 2)
        self.assertIn('invalid_json', client.calls[1][-1]['content'])

    def test_invalid_evidence_is_not_kept(self):
        result, client, _ = self.run_fake([payload(('标签', '不存在')), '[]'])
        self.assertEqual(result['final'], [])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result['attempts'][0]['candidates'][0]['reason'], 'evidence_not_in_report')

    def test_empty_results_do_not_backfill_rules(self):
        result, client, _ = self.run_fake(['[]', '[]'])
        self.assertEqual(result['final'], [])
        self.assertEqual(result['source'], 'keyword_empty')
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(hasattr(analysis, 'build_rule_entity_headlines'))

    def test_duplicate_capacity_and_candidate_audit(self):
        items = [('标签一', '新质生产力'), ('标签一', '新质生产力')]
        items += [(f'标签{x}', '新质生产力') for x in ['二', '三', '四', '五', '六']]
        result, _, _ = self.run_fake([payload(*items)])
        self.assertEqual(len(result['final']), 5)
        rows = result['attempts'][0]['candidates']
        self.assertEqual(rows[1]['reason'], 'duplicate')
        self.assertEqual(rows[-1]['reason'], 'capacity_limit')
        self.assertEqual(sum(row['accepted'] for row in rows), 5)

    def test_exceptions_never_echo_sensitive_message(self):
        error = RuntimeError('SYNTHETIC-SECRET https://example.invalid/?key=SYNTHETIC-SECRET')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result, client, _ = self.run_fake([error, error])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn('SYNTHETIC-SECRET', json.dumps(result) + output.getvalue())
        self.assertEqual(result['attempts'][0]['error_type'], 'RuntimeError')

    def test_fenced_json_can_be_parsed(self):
        result, _, _ = self.run_fake(['```json\n' + payload(('新质生产力', '新质生产力')) + '\n```'])
        self.assertEqual(result['final'], ['新质生产力'])

    def test_themes_no_longer_require_keyword(self):
        raw = '<THEMES_JSON>[{"title":"主题名称","evidence_ids":["N1","N2"]}]</THEMES_JSON><REPORT_MARKDOWN>' + BODY + '</REPORT_MARKDOWN>'
        text, themes = analysis.parse_structured_report(raw, {'N1': {}, 'N2': {}})
        self.assertEqual(text, BODY)
        self.assertEqual(len(themes), 1)
        self.assertNotIn('keyword', themes[0])

    def test_rejected_candidate_keeps_normalized_audit(self):
        result, _, _ = self.run_fake([payload((' “甲甲甲甲甲甲甲甲甲甲甲甲甲” ', '新质生产力')), '[]'])
        row = result['attempts'][0]['candidates'][0]
        self.assertEqual(row['reason'], 'too_long')
        self.assertEqual(row['normalized']['label'], '甲' * 13)
        self.assertEqual(row['raw']['label'], ' “甲甲甲甲甲甲甲甲甲甲甲甲甲” ')


class KeywordIntegrationTests(unittest.TestCase):
    def run_report(self, outputs, *, dry_run=True):
        legacy = [{'title': f'主题{i}', 'keyword': f'旧词{i}', 'evidence_ids': ['N1', 'N2']}
                  for i in range(5)]
        raw = '<THEMES_JSON>' + json.dumps(legacy, ensure_ascii=False) + '</THEMES_JSON><REPORT_MARKDOWN>' + BODY + '</REPORT_MARKDOWN>'
        parent = analysis.AIClient(CONFIG)
        parent.last_model = CONFIG['MODEL']
        keyword_client = FakeKeywordClient(outputs)
        argv = ['weekly', '--start', '2026-10-05', '--end', '2026-10-11']
        if dry_run:
            argv.append('--dry-run')
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(patch.object(weekly, 'OUTPUT_DIR', Path(temp)))
            stack.enter_context(patch.object(weekly, 'report_now', return_value=datetime(2026, 10, 12, 19, 40)))
            stack.enter_context(patch.object(weekly, 'load_ai_config', return_value=CONFIG))
            stack.enter_context(patch.object(weekly, 'load_runtime_env', return_value={
                'EMAIL_FROM': 'from@example.invalid', 'EMAIL_TO': 'to@example.invalid',
                'EMAIL_PASSWORD': 'synthetic',
            }))
            stack.enter_context(patch.object(weekly, 'collect_news', return_value=(
                [{'title': '示例一'}, {'title': '示例二'}], {}, {},
            )))
            stack.enter_context(patch.object(weekly, 'AIClient', return_value=parent))
            chat = stack.enter_context(patch.object(parent, 'chat', return_value=raw))
            factory = stack.enter_context(patch.object(analysis, 'build_keyword_client', return_value=keyword_client))
            dispatcher = Mock()
            dispatcher.send_report.return_value = SimpleNamespace(
                configured=True, sent=True, partially_delivered=False, unknown=(),
            )
            stack.enter_context(patch.object(weekly, 'NotificationDispatcher', return_value=dispatcher))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(weekly.main(), 0)
            saved = json.loads(next(Path(temp).glob('*.keywords.json')).read_text())
            page = next(Path(temp).glob('*.html')).read_text()
            chat.assert_called_once()
            factory.assert_called_once_with(parent)
            self.assertEqual(saved['keyword_version'], 2)
            self.assertEqual(saved['rule_fallback'], [])
            self.assertTrue(saved['rule_fallback_disabled'])
            self.assertEqual(len(saved['themes']), 5)
            self.assertTrue(all('keyword' not in theme for theme in saved['themes']))
            self.assertIn('旧词', saved['raw_themes'])
            self.assertNotIn('旧词', ' / '.join(saved['final']))
            self.assertIn('巴拿马深源强震', page)
            self.assertIn('Gemini/main-analysis', page)
            self.assertEqual(saved['keyword_model'], CONFIG['FALLBACK_MODELS'][0])
            self.assertEqual(dispatcher.send_report.call_count, 0 if dry_run else 1)
            return page, saved, keyword_client

    def test_main_ignores_legacy_labels_and_generates_one_complete_label(self):
        _, saved, client = self.run_report([payload(('巴拿马强震', '巴拿马深源强震'))])
        self.assertEqual(saved['final'], ['巴拿马强震'])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(saved['final_details'][0]['evidence'], '巴拿马深源强震')

    def test_main_preserves_slash_label_without_splitting(self):
        page, saved, _ = self.run_report([payload(('AI/芯片管制', '新质生产力'))])
        self.assertEqual(saved['final'], ['AI/芯片管制'])
        self.assertEqual(page.count('<span class="tab-pill">'), 1)
        self.assertIn('<span class="tab-pill">AI/芯片管制</span>', page)

    def test_empty_labels_preserve_body_and_one_delivery_decision(self):
        page, saved, client = self.run_report(['[]', '[]'], dry_run=False)
        self.assertEqual(saved['final'], [])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn('class="tab-strip"', page)

    def test_wiring_guard_detects_missing_prompt_helper(self):
        with patch.object(analysis, '_format_cluster_line', None), self.assertRaises(TypeError):
            self.run_report(['[]', '[]'])

    def test_render_zero_one_five_labels_and_escape_list(self):
        for count in (0, 1, 5):
            labels = ['AI/芯片<' + str(i) for i in range(count)]
            page = render_weekly_html('测试', '日期', '模型', {
                '关键词列表': labels, 'Top5关键词': 'ignored',
            }, '正文', generated_at='2026-10-12 19:40:00')
            self.assertEqual(page.count('<span class="tab-pill">'), count)
            self.assertEqual('class="tab-strip"' in page, count > 0)
            if count:
                self.assertIn('AI/芯片&lt;0', page)
            self.assertNotIn('ignored', page)

    def test_native_first_slot_keeps_its_own_auth_and_parent_state(self):
        parent = analysis.AIClient({
            'MODEL': 'openai/main', 'API_KEY': 'synthetic-main', 'API_BASE': 'https://main.example.invalid/v1',
            'FALLBACK_MODELS': ['openai/lite', 'openai/lite'],
            'FALLBACK_API_BASE': 'https://first.example.invalid/v1@https://second.example.invalid/v1',
            'FALLBACK_API_KEY': 'synthetic-first@synthetic-second',
        })
        parent.last_model = 'openai/main'
        parent.last_candidate_id = 'main'
        response = {'choices': [{'message': {'content': payload(('巴拿马强震', '巴拿马深源强震'))}, 'finish_reason': 'stop'}]}
        with patch('trendradar.ai.client.completion', return_value=response) as completion:
            result = analysis.generate_keywords(parent, '2026-10-05', '2026-10-11', BODY)
        completion.assert_called_once()
        self.assertEqual(completion.call_args.kwargs['api_base'], 'https://first.example.invalid/v1')
        self.assertEqual(completion.call_args.kwargs['api_key'], 'synthetic-first')
        self.assertEqual(result['final'], ['巴拿马强震'])
        self.assertEqual((parent.last_model, parent.last_candidate_id, len(parent.candidates)), ('openai/main', 'main', 3))

    def test_invalid_first_slot_is_not_promoted_to_second(self):
        parent = analysis.AIClient({
            'MODEL': 'openai/main', 'API_KEY': 'synthetic-main', 'API_BASE': 'https://main.example.invalid/v1',
            'FALLBACK_MODELS': ['openai/first', 'openai/second'],
            'FALLBACK_API_BASE': 'https://first.example.invalid/v1@https://second.example.invalid/v1',
            'FALLBACK_API_KEY': '@synthetic-second',
        })
        with patch('trendradar.ai.client.completion') as completion:
            result = analysis.generate_keywords(parent, '2026-10-05', '2026-10-11', BODY)
        completion.assert_not_called()
        self.assertEqual(result['model'], 'openai/first')
        self.assertEqual(result['final'], [])
        self.assertEqual([row['error_type'] for row in result['attempts']], ['ValueError', 'ValueError'])
