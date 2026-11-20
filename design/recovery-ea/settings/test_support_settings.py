from decimal import localcontext
from unittest import TestCase

from support_settings import SupportSettings, SupportTicks, validate_support_edit


class SupportSettingsTest(TestCase):
    def test_default_on_different_tick_grids(self):
        for tick, count in [('0.01', 40), ('0.05', 8), ('0.10', 4), ('0.001', 400)]:
            with self.subTest(tick=tick):
                self.assertEqual(SupportSettings().to_ticks(tick), SupportTicks(0, count, 0))

    def test_exact_edit_preserves_armed_snapshot(self):
        first = SupportSettings()
        original = first.to_ticks("0.01")
        edited = validate_support_edit({"support_threshold": "0.70"}, generation_active=False,
                                      symbol_tick_size="0.01")
        self.assertEqual(edited.to_ticks("0.01"), SupportTicks(0, 70, 0))
        self.assertEqual(original, SupportTicks(0, 40, 0))
        self.assertEqual(first.support_threshold, "0.40")

    def test_active_or_unverified_cannot_edit(self):
        for status in (True, None, 0, "idle"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                validate_support_edit({"support_threshold": "0.45"}, generation_active=status,
                                      symbol_tick_size="0.01")

    def test_rejects_invalid_or_off_grid_without_rounding(self):
        for price in ("0", "0.0000", "-0.25", "NaN", "4e-1", "0.40 ", "0.401", 0.40, True):
            with self.subTest(price=price), self.assertRaises(ValueError):
                SupportSettings(price).to_ticks("0.01")
        with self.assertRaises(ValueError):
            SupportSettings().to_ticks("0")

    def test_exact_arithmetic_and_bounded_tick_count(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(SupportSettings("0.407").to_ticks("0.001"), SupportTicks(0, 407, 0))
        with self.assertRaisesRegex(ValueError, "outside supported"):
            SupportSettings().to_ticks("0.00000000000001")

    def test_legacy_and_extra_edit_fields_never_accepted(self):
        for payload in ({}, None, {"support_second": "0.42"},
                        {"support_threshold": "0.42", "support_first_enabled": True},
                        {"support_threshold": "0.42", "support_offset": "0.25"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_support_edit(payload, generation_active=False, symbol_tick_size="0.01")
