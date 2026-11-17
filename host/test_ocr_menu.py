"""Focused menu checks: python3 -B -m unittest discover -s host -p test_ocr_menu.py."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ocr_menu import _extract_prices, _name_key, correct_ocr_text, load_menu_items, select_best_menu_item


def row(item_id=1, name="テストカツ丼", price=650, item_type="don"):
    return {"id": item_id, "name": name, "price": price, "type": item_type}


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "menu.json"

    def write_menu(self, rows):
        self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def test_exact_match_preserves_selection_and_correction_contract(self):
        self.write_menu([row(), row(2, "テストうどん", 450, "noodle")])
        selected = select_best_menu_item("食券\nテストカツ丼\n650円", self.path)
        self.assertEqual(selected.selected_item.item_id, 1)
        self.assertEqual(selected.selected_name, "テストカツ丼")
        self.assertEqual(selected.detected_prices, (650,))
        self.assertEqual((selected.item_count, selected.database_loaded), (2, True))
        self.assertGreater(selected.name_score, 0.9)
        self.assertGreater(selected.full_text_score, 0)
        self.assertGreater(selected.line_score, 0)
        self.assertEqual(selected.matched_line, "テストカツ丼")
        corrected = correct_ocr_text("テストカツ丼 650円", self.path)
        self.assertEqual(corrected.corrected_text, "テストカツ丼 | 650 JPY | don")
        match, = corrected.matches
        self.assertEqual((match.source_line, match.item_name, match.price, match.item_type),
                         ("テストカツ丼 650円", "テストカツ丼", 650, "don"))
        self.assertGreater(match.score, 0.9)
        self.assertEqual((corrected.item_count, corrected.database_loaded), (2, True))

    def test_realistic_ocr_confusions_and_fullwidth_price(self):
        self.write_menu([row(), row(2, "テストうどん", 450, "noodle")])
        text = "テスト力ツ井 ６５O円"
        self.assertEqual(select_best_menu_item(text, self.path).selected_item.item_id, 1)
        self.assertEqual(correct_ocr_text(text, self.path).matches[0].item_name, "テストカツ丼")

    def test_boundary_separated_leading_price_confusions(self):
        for token, expected in (("S50", (550,)), ("O50", ()), ("I50", (150,))):
            for prefix in ("", " ", "価格: "):
                for currency in ("", "円", "JPY", "yen"):
                    with self.subTest(token=token, prefix=prefix, currency=currency):
                        self.assertEqual(_extract_prices(prefix + token + currency), expected)
                        if currency:
                            self.assertEqual(_name_key("テストカレー " + token + currency),
                                             _name_key("テストカレー"))

    def test_leading_confused_price_does_not_select_size_variant(self):
        self.write_menu([row(1, "テストカレー", 550), row(2, "テストカレーS", 450)])
        for gap in (" ", " / "):
            for currency in ("円", "JPY", "yen"):
                text = "テストカレー" + gap + "S50" + currency
                with self.subTest(text=text):
                    selected = select_best_menu_item(text, self.path)
                    self.assertEqual(selected.detected_prices, (550,))
                    self.assertIsNotNone(selected.selected_item)
                    self.assertEqual(selected.selected_item.item_id, 1)
                    corrected = correct_ocr_text(text, self.path)
                    match, = corrected.matches
                    self.assertEqual((match.item_name, match.price), ("テストカレー", 550))
                    self.assertEqual(corrected.corrected_text, "テストカレー | 550 JPY | don")

    def test_price_disambiguates_same_name_on_separate_line(self):
        self.write_menu([row(1, price=450), row(2, price=650)])
        self.assertEqual(select_best_menu_item("テストカツ丼\n650円", self.path).selected_item.item_id, 2)
        self.assertEqual(correct_ocr_text("テストカツ丼 650円", self.path).matches[0].price, 650)

    def test_matching_price_does_not_rescue_an_unrelated_name(self):
        self.write_menu([row(1, "Sample bowl red")])
        self.assertIsNone(select_best_menu_item("label board 650", self.path).selected_item)
        self.assertEqual(correct_ocr_text("label board 650", self.path).matches, ())

    def test_unrelated_and_price_only_inputs_do_not_select(self):
        self.write_menu([row(), row(2, "Sample noodles", 450, "noodle")])
        for text in ("qzxwv unrelated", "食券 650円", "650", "", "   "):
            with self.subTest(text=text):
                result = select_best_menu_item(text, self.path)
                self.assertIsNone(result.selected_item)
                self.assertEqual(result.selected_name, "")
                correction = correct_ocr_text(text, self.path)
                self.assertEqual(correction.matches, ())
                self.assertEqual(correction.corrected_text, text.strip())

    def test_tied_candidates_abstain_in_either_database_order(self):
        rows = [row(1, "テスト定食A"), row(2, "テスト定食B")]
        for ordered in (rows, rows[::-1]):
            self.write_menu(ordered)
            self.assertIsNone(select_best_menu_item("テスト定食", self.path).selected_item)
            self.assertEqual(correct_ocr_text("テスト定食", self.path).matches, ())

    def test_identical_names_without_price_are_ambiguous(self):
        self.write_menu([row(1, price=450), row(2, price=650)])
        self.assertIsNone(select_best_menu_item("テストカツ丼", self.path).selected_item)
        self.assertEqual(correct_ocr_text("テストカツ丼", self.path).matches, ())

    def test_exact_name_beats_a_longer_name_containing_it(self):
        self.write_menu([row(2, "テストカツ丼セット"), row()])
        self.assertEqual(select_best_menu_item("テストカツ丼", self.path).selected_item.item_id, 1)

    def test_longer_exact_name_beats_its_contained_menu_name(self):
        self.write_menu([row(), row(2, "テストカツ丼セット")])
        for suffix in ("", " 650円", " ６５O円"):
            text = "テストカツ丼セット" + suffix
            with self.subTest(suffix=suffix):
                self.assertEqual(select_best_menu_item(text, self.path).selected_item.item_id, 2)
                self.assertEqual(correct_ocr_text(text, self.path).matches[0].item_name, "テストカツ丼セット")

    def test_size_suffix_survives_adjacent_currency_prices(self):
        rows = [row(index, "テストカレー" + size) for index, size in enumerate("SLB", 1)]
        self.write_menu(rows)
        for item in rows:
            self.assertEqual(_name_key(item["name"] + "650"), _name_key(item["name"]))
            for price in ("650", "6S0", "６５O"):
                for currency in ("円", "JPY"):
                    for gap in ("", " "):
                        text = item["name"] + gap + price + currency
                        with self.subTest(name=item["name"], price=price, currency=currency, gap=gap):
                            self.assertEqual(_name_key(text), _name_key(item["name"]))
                            selected = select_best_menu_item(text, self.path)
                            self.assertEqual(selected.detected_prices, (650,))
                            self.assertIsNotNone(selected.selected_item)
                            self.assertEqual(selected.selected_item.item_id, item["id"])
                            correction, = correct_ocr_text(text, self.path).matches
                            self.assertEqual(correction.item_name, item["name"])

    def test_explicit_size_name_beats_conflicting_price(self):
        pairs = (
            ("テストカレー", "大テストカレー"),
            ("テストカレー", "テストカレーL"),
            ("Sテストカレー", "Lテストカレー"),
            ("テストカレーS", "テストカレーL"),
        )
        for names in pairs:
            rows = [row(1, names[0], 450), row(2, names[1], 650)]
            for ordered in (rows, rows[::-1]):
                self.write_menu(ordered)
                for explicit, other in (rows, rows[::-1]):
                    for header in ("", "食券 ", "食券\n"):
                        for gap in ("", " ", "\n"):
                            text = f"{header}{explicit['name']}{gap}{other['price']}円"
                            with self.subTest(names=names, text=text, reversed=ordered != rows):
                                selected = select_best_menu_item(text, self.path)
                                self.assertIsNotNone(selected.selected_item)
                                self.assertEqual(selected.selected_item.item_id, explicit["id"])
                                correction, = correct_ocr_text(text, self.path).matches
                                self.assertEqual(correction.item_name, explicit["name"])

    def test_multiple_explicit_names_abstain_despite_matching_price(self):
        self.write_menu([row(), row(2, "テストうどん", 450, "noodle")])
        text = "食券\nテストカツ丼\nテストうどん\n650円"
        self.assertIsNone(select_best_menu_item(text, self.path).selected_item)

    def test_nearly_tied_candidates_abstain(self):
        self.write_menu([row(1, "Sample bowl red"), row(2, "Sample bowl bread")])
        self.assertIsNone(select_best_menu_item("Sample bowl bled", self.path).selected_item)
        self.assertEqual(correct_ocr_text("Sample bowl bled", self.path).matches, ())

    def test_missing_and_empty_database_have_distinct_status(self):
        missing = select_best_menu_item("anything", self.path)
        self.assertEqual((missing.selected_item, missing.item_count, missing.database_loaded), (None, 0, False))
        self.write_menu([])
        empty = select_best_menu_item("anything", self.path)
        self.assertEqual((empty.selected_item, empty.item_count, empty.database_loaded), (None, 0, True))

    def test_supported_wrappers_and_optional_fields(self):
        for data in ({"items": [row()]}, {"first": row()}, [{"name": "Sample noodles"}]):
            self.write_menu(data)
            self.assertEqual(len(load_menu_items(self.path)), 1)

    def test_malformed_database_has_useful_failure(self):
        for data in (42, {"items": "invalid"}, ["invalid"], [row(name=None)],
                     [row(price="invalid")], [row(item_type=42)], [row(item_id=True)]):
            with self.subTest(case=type(data).__name__):
                self.write_menu(data)
                with self.assertRaisesRegex(ValueError, "menu database"):
                    load_menu_items(self.path)
        self.path.write_text('[{"name":', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "menu database"):
            load_menu_items(self.path)


if __name__ == "__main__":
    unittest.main()
