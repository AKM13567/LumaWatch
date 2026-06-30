"""
LumaWatch — Ambient / time-of-day awareness

Phones don't just react to on-screen content — they also bias the
result by time of day and ambient context (e.g. capping max brightness
late at night even in a bright room, and shifting color temperature
warmer as it gets dark, like Night Shift / Night Light).

This module is pure math: given the current local time, it returns
(a) a brightness ceiling multiplier and (b) a warmth amount (0-1) to
apply as a color overlay. It has no Windows/UI dependencies so it's
easy to unit test.
"""

import datetime
import math


def _smoothstep(edge0, edge1, x):
    if edge0 == edge1:
        return 0.0 if x < edge0 else 1.0
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def brightness_ceiling(now: datetime.datetime, day_start_hour=7.0, night_start_hour=21.5,
                        night_floor=0.55) -> float:
    """
    Returns a multiplier in [night_floor, 1.0] applied on top of the
    content-derived target brightness. Full strength during the day,
    tapering down through the evening, flat at night_floor overnight,
    ramping back up at dawn. Mirrors how phones dim their max allowed
    brightness late at night regardless of what's on screen.
    """
    h = now.hour + now.minute / 60.0

    # Transition windows: 2 hours to wind down into night, 1.5 hours to wake up.
    evening_fade_start = night_start_hour - 2.0
    morning_fade_end = day_start_hour

    if evening_fade_start <= h < night_start_hour:
        t = _smoothstep(evening_fade_start, night_start_hour, h)
        return 1.0 - t * (1.0 - night_floor)

    if h >= night_start_hour or h < morning_fade_end - 1.5:
        return night_floor

    if morning_fade_end - 1.5 <= h < morning_fade_end:
        t = _smoothstep(morning_fade_end - 1.5, morning_fade_end, h)
        return night_floor + t * (1.0 - night_floor)

    return 1.0


def night_light_warmth(now: datetime.datetime, sunset_hour=19.0, sunrise_hour=7.0,
                        strength=45) -> float:
    """
    Returns warmth in [0, strength/100], ramping in after sunset_hour
    and ramping out before sunrise_hour, like Night Light/Night Shift.
    `strength` is a 0-100 user setting controlling max warmth.
    """
    h = now.hour + now.minute / 60.0
    max_warmth = max(0.0, min(100.0, strength)) / 100.0

    ramp_hours = 1.0

    if sunset_hour <= h < sunset_hour + ramp_hours:
        return _smoothstep(sunset_hour, sunset_hour + ramp_hours, h) * max_warmth

    is_overnight = h >= sunset_hour + ramp_hours or h < sunrise_hour - ramp_hours
    if is_overnight:
        return max_warmth

    if sunrise_hour - ramp_hours <= h < sunrise_hour:
        t = _smoothstep(sunrise_hour - ramp_hours, sunrise_hour, h)
        return max_warmth * (1.0 - t)

    return 0.0


def warmth_to_rgb_gain(warmth: float):
    """
    Convert a 0-1 warmth amount into per-channel multiplicative gains
    used to tint the gamma ramp (R stays ~1.0, G/B pull down slightly).
    Tuned to feel like a mild Night Light, not a sepia filter.
    """
    warmth = max(0.0, min(1.0, warmth))
    r = 1.0
    g = 1.0 - 0.12 * warmth
    b = 1.0 - 0.35 * warmth
    return r, g, b
