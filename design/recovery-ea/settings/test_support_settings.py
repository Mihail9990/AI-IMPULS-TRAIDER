from decimal import localcontext
from unittest import TestCase

from support_settings import SupportSettings, SupportTicks, validate_support_edit


class SupportSettingsTest(TestCase):
    def test_defaults_convert_to_exact_broker_ticks(self):
        self.assertEqual(SupportSettings().to_ticks("0.01"), SupportTicks(25, 40, 25))
        self.assertEqual(SupportSettings().to_ticks("0.05"), SupportTicks(5, 8, 5))
        self.assertEqual(SupportSettings().to_ticks("0.001"), SupportTicks(250, 400, 250))

    def test_edit_independent_offset_and_immutable_generation_snapshot(self):
        original = SupportSettings()
        armed = original.to_ticks("0.01")
        edited = validate_support_edit(dict(support_first="0.30", support_second="0.45"),
                                       generation_active=False, symbol_tick_size="0.01")
        self.assertEqual(edited.to_ticks("0.01"), SupportTicks(30, 45, 25))
        self.assertEqual(armed, SupportTicks(25, 40, 25))
        self.assertEqual(original.support_second, "0.40")
        # Crossing stage 1 earlier does not also change where its SL goes.
        self.assertEqual(SupportSettings("0.15", "0.20").to_ticks("0.01"), SupportTicks(15, 20, 25))

    def test_active_or_unverified_generation_cannot_be_edited(self):
        for phase in [True, None, 0, "idle"]:
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                validate_support_edit(dict(support_first="0.30", support_second="0.45"),
                                      generation_active=phase, symbol_tick_size="0.01")

    def test_rejects_zero_equal_reversed_and_invalid_decimals(self):
        for first, second in [("0", "0.40"), ("0.40", "0.4"), ("0.41", "0.40"),
                              ("-0.25", "0.40"), ("NaN", "0.4"), ("0.25", "4e-1"),
                              (0.25, "0.40"), (True, "0.40"), ("0.25 ", "0.40")]:
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                SupportSettings(first, second).to_ticks("0.01")

    def test_off_grid_is_not_rounded(self):
        for settings, tick in [(SupportSettings("0.251", "0.40"), "0.01"),
                               (SupportSettings("0.25", "0.401"), "0.01"),
                               (SupportSettings("0.20", "0.40"), "0.10")]:
            with self.subTest(settings=settings, tick=tick), self.assertRaisesRegex(ValueError, "tick grid"):
                settings.to_ticks(tick)
        with self.assertRaises(ValueError):
            SupportSettings().to_ticks("0")

    def test_exactness_does_not_depend_on_decimal_context(self):
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(SupportSettings("0.253", "0.405").to_ticks("0.001"),
                             SupportTicks(253, 405, 250))
        with self.assertRaisesRegex(ValueError, "outside supported"):
            SupportSettings().to_ticks("0.00000000000001")

    def test_rejects_separate_offset_edit_or_partial_change(self):
        for body in [dict(support_first="0.30"),
                     dict(support_first="0.30", support_second="0.45", support_offset="0.30")]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_support_edit(body, generation_active=False, symbol_tick_size="0.01")
