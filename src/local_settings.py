"""Decimal input used by the local desktop and its control mailbox."""
from decimal import Decimal
import re

DEFAULT_LOTS = "0.01,0.01,0.01,0.01,0.01,0.02,0.02,0.02,0.03,0.03,0.04,0.04,0.05,0.05,0.06,0.06,0.07,0.07,0.08,0.08,0.09,0.09,0.10,0.10"
DEFAULT_SETTINGS = dict(initial_sl_distance="0.80", support_threshold="0.40", lot_schedule=DEFAULT_LOTS.split(","))


def positive_decimal(text):
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) or len(text) > 32:
        raise ValueError("Введите положительное десятичное число с точкой.")
    if Decimal(text) <= 0:
        raise ValueError("Все значения должны быть больше нуля.")
    return text


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SETTINGS):
        raise ValueError("Неполный набор настроек.")
    lots = value["lot_schedule"]
    if not isinstance(lots, list) or len(lots) != 24:
        raise ValueError("Укажите 24 лота, по одному для каждой ступени.")
    return dict(initial_sl_distance=positive_decimal(value["initial_sl_distance"]),
                support_threshold=positive_decimal(value["support_threshold"]),
                lot_schedule=[positive_decimal(v) for v in lots])
