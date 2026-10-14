# -*- coding: utf-8 -*-
"""
ربات سیگنال‌دهی USD/JPY - استراتژی واقعی بانکی
USD/JPY Signal Bot - Real Bank Trading Strategy

اعتراف صادقانه:
- هیچ رباتی 100% دقیق نیست
- Yahoo Finance داده تأخیری داره (15-20 دقیقه)
- هدف: سیگنال‌های با کیفیت، نه سیگنال زیاد
- حداقل 1 سیگنال روزانه (خرید/فروش/صبر با توضیح)

استراتژی واقعی بانکی:
1. ترید در جهت روند (Continuation) - BOS
2. ترید ریورسال (Reversal) - CHoCH
3. ترید در رنج (Range) - خرید از کف، فروش از سقف
4. ترید Liquidity Sweep - شکست فیک

معماری مولتی‌تایم‌فریم:
- Weekly + Daily: روند کلی (نه فیلتر، بلکه context)
- 1H: مناطق ورود (OB, S/D, FVG)
- 15m: تریگر ورود

AI نقش:
- تصمیم‌گیرنده نهایی (نه فقط تحلیلگر)
- بررسی کیفیت سیگنال
- فیلتر سیگنال‌های ضعیف
"""

import os
import sys
import json
import logging
import requests
from datetime import datetime
from typing import Dict, List, Optional

# ============================================================
def install_dependencies():
    deps = ["yfinance", "pandas", "numpy", "requests", "python-dotenv"]
    for dep in deps:
        os.system(f'pip install {dep} --quiet')

install_dependencies()

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
    from dotenv import load_dotenv
except ImportError as e:
    print(f"❌ خطا در ایمپورت: {e}")
    sys.exit(1)

load_dotenv()

# ============================================================
# Configuration
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
AI_MODEL = "glm-4.5"

SYMBOL = "JPY=X"
SYMBOL_DISPLAY = "USD/JPY"

# تایم‌فریم‌ها
TF_HTF = "1d"      # روزانه - context
TF_PATTERN = "1h"  # 1 ساعته - مناطق ورود
TF_TRIGGER = "15m" # 15 دقیقه - تریگر

# پارامترهای اندیکاتور (ثابت - بدون بهینه‌سازی)
EMA_FAST = 21
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
RSI_OVERSOLD = 35    # کمی آسان‌تر
RSI_OVERBOUGHT = 65
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5

# آستانه‌های منطقی (نه خیلی سخت، نه خیلی آسون)
MIN_SIGNAL_STRENGTH = 50  # حداقل 50% قدرت
MIN_CONFLUENCE = 3         # حداقل 3 تأیید

SIGNALS_DB_FILE = "signals_db.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================
# Data Fetcher (جلوگیری از Repainting)
# ============================================================
class DataFetcher:
    def __init__(self):
        self.ticker = yf.Ticker(SYMBOL)
        self._cache = {}
        self._cache_time = {}

    def fetch_closed_candles(self, timeframe, count=300):
        """فقط کندل‌های بسته‌شده - جلوگیری از Repainting"""
        import time as t
        cache_key = f"{timeframe}_{count}"
        if cache_key in self._cache and cache_key in self._cache_time:
            if t.time() - self._cache_time[cache_key] < 300:
                return self._cache[cache_key]

        try:
            period_map = {"15m": "60d", "1h": "730d", "60m": "730d", "4h": "60d", "1d": "2y", "1wk": "5y"}
            period = period_map.get(timeframe, "60d")
            df = self.ticker.history(period=period, interval=timeframe, auto_adjust=False, prepost=False)
            if df.empty:
                df = yf.download(SYMBOL, period=period, interval=timeframe, progress=False, auto_adjust=False)
            if df.empty:
                raise ValueError(f"داده خالی برای {timeframe}")

            df.columns = [str(c).lower().replace(' ', '_') for c in df.columns]
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    df[col] = 0 if col == 'volume' else df.iloc[:, 0]
            df = df.dropna(subset=['open', 'high', 'low', 'close'])
            if len(df) > count + 1:
                df = df.tail(count + 1)

            # حذف کندل آخر (ممکنه در حال تشکیل باشه)
            df = df.iloc[:-1].copy()

            self._cache[cache_key] = df
            self._cache_time[cache_key] = t.time()
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {timeframe}: {e}")
            raise


# ============================================================
# Market Context Analyzer (Daily)
# ============================================================
class MarketContextAnalyzer:
    """
    تحلیل context بازار - نه فیلتر، بلکه اطلاعات

    خروجی:
    - trend: bullish/bearish/range
    - strength: 0-100
    - key_levels: سطوح مهم
    - in_range: آیا بازار رنج هست؟
    """

    def __init__(self):
        self.fetcher = DataFetcher()

    def analyze(self):
        try:
            df = self.fetcher.fetch_closed_candles(TF_HTF, count=200)
            if df.empty or len(df) < 50:
                return {'trend': 'unknown', 'strength': 0, 'description': 'داده کافی نیست'}

            price = df['close'].iloc[-1]
            ema21 = df['close'].ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
            ema50 = df['close'].ewm(span=EMA_MID, adjust=False).mean().iloc[-1]
            ema200 = df['close'].ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]

            # تشخیص روند
            bull = bear = 0
            if price > ema200: bull += 1
            else: bear += 1
            if ema21 > ema50: bull += 1
            else: bear += 1
            if ema50 > ema200: bull += 1
            else: bear += 1

            # تشخیص رنج (EMAs نزدیک هم)
            ema_spread = abs(ema21 - ema50) / price * 100
            is_range = ema_spread < 0.3  # اگه فاصله کمتر از 0.3% باشه، رنج هست

            # Swing points
            swing_highs, swing_lows = self._find_swings(df)

            if is_range and len(swing_highs) >= 2 and len(swing_lows) >= 2:
                # بازار رنج هست
                range_high = max(swing_highs[-3:]) if len(swing_highs) >= 3 else max(swing_highs)
                range_low = min(swing_lows[-3:]) if len(swing_lows) >= 3 else min(swing_lows)
                return {
                    'trend': 'range',
                    'strength': 50,
                    'description': f"بازار رنج ({range_low:.2f} - {range_high:.2f})",
                    'range_high': range_high,
                    'range_low': range_low,
                    'price': price,
                    'key_levels': {'resistances': swing_highs[-3:], 'supports': swing_lows[-3:]},
                }

            if bull > bear:
                trend = 'bullish'
                strength = min(100, 50 + bull * 15)
                desc = f"روند صعودی (قدرت: {strength:.0f}%)"
            elif bear > bull:
                trend = 'bearish'
                strength = min(100, 50 + bear * 15)
                desc = f"روند نزولی (قدرت: {strength:.0f}%)"
            else:
                trend = 'neutral'
                strength = 40
                desc = "روند خنثی"

            return {
                'trend': trend,
                'strength': strength,
                'description': desc,
                'price': price,
                'ema21': ema21, 'ema50': ema50, 'ema200': ema200,
                'key_levels': {'resistances': swing_highs[-3:], 'supports': swing_lows[-3:]},
                'is_range': is_range,
            }
        except Exception as e:
            logger.error(f"خطا در Context: {e}")
            return {'trend': 'unknown', 'strength': 0, 'description': f'خطا: {e}'}

    def _find_swings(self, df, lookback=100):
        highs, lows = [], []
        recent = df.tail(lookback)
        for i in range(2, min(len(recent) - 2, lookback - 2)):
            if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i-2]
                and recent['high'].iloc[i] > recent['high'].iloc[i+1] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                highs.append(float(recent['high'].iloc[i]))
            if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i-2]
                and recent['low'].iloc[i] < recent['low'].iloc[i+1] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                lows.append(float(recent['low'].iloc[i]))
        return highs, lows


# ============================================================
# Pattern Analyzer (1H)
# ============================================================
class PatternAnalyzer:
    """تحلیل مناطق ورود در 1H"""

    def __init__(self):
        self.fetcher = DataFetcher()

    def analyze(self):
        try:
            df = self.fetcher.fetch_closed_candles(TF_PATTERN, count=200)
            if df.empty or len(df) < 50:
                return {'zones': [], 'structure': 'neutral', 'description': 'داده کافی نیست'}

            cp = df['close'].iloc[-1]

            # تحلیل‌ها
            obs = self._find_order_blocks(df)
            sd = self._find_supply_demand(df)
            fvgs = self._find_fvgs(df)
            structure = self._detect_structure(df)
            liquidity = self._find_liquidity_sweep(df)

            # جمع‌آوری مناطق
            zones = []
            for ob in obs['bullish']:
                zones.append({'type': 'demand', 'price': ob['price'], 'source': 'OB', 'age': ob['age']})
            for ob in obs['bearish']:
                zones.append({'type': 'supply', 'price': ob['price'], 'source': 'OB', 'age': ob['age']})
            for z in sd['demand']:
                zones.append({'type': 'demand', 'price': z['mid'], 'source': 'SD', 'age': z['age']})
            for z in sd['supply']:
                zones.append({'type': 'supply', 'price': z['mid'], 'source': 'SD', 'age': z['age']})

            # فیلتر مناطق معتبر (کم سن)
            valid_zones = [z for z in zones if z['age'] <= 25]
            # مرتب‌سازی بر اساس نزدیکی به قیمت
            valid_zones.sort(key=lambda x: abs(x['price'] - cp))

            return {
                'zones': valid_zones[:5],
                'all_zones': valid_zones,
                'structure': structure,
                'liquidity_sweep': liquidity,
                'fvgs': fvgs,
                'current_price': cp,
                'description': f"{len(valid_zones)} ناحیه فعال، ساختار: {structure.get('description', 'نامشخص')}",
            }
        except Exception as e:
            logger.error(f"خطا در Pattern: {e}")
            return {'zones': [], 'structure': 'neutral', 'description': f'خطا: {e}'}

    def _find_order_blocks(self, df, lookback=50):
        recent = df.tail(lookback).copy()
        bullish_obs, bearish_obs = [], []
        try:
            for i in range(2, min(len(recent) - 5, lookback - 5)):
                if recent['close'].iloc[i] < recent['open'].iloc[i]:
                    move_up = recent['close'].iloc[i + 3] - recent['close'].iloc[i]
                    if move_up > recent['close'].iloc[i] * 0.0015:
                        bullish_obs.append({'price': (recent['high'].iloc[i] + recent['low'].iloc[i]) / 2, 'age': len(recent) - i})
                if recent['close'].iloc[i] > recent['open'].iloc[i]:
                    move_down = recent['close'].iloc[i] - recent['close'].iloc[i + 3]
                    if move_down > recent['close'].iloc[i] * 0.0015:
                        bearish_obs.append({'price': (recent['high'].iloc[i] + recent['low'].iloc[i]) / 2, 'age': len(recent) - i})
        except: pass
        return {'bullish': bullish_obs, 'bearish': bearish_obs}

    def _find_supply_demand(self, df, lookback=100):
        try:
            recent = df.tail(lookback).copy()
            demand_zones, supply_zones = [], []
            for i in range(4, min(len(recent) - 4, lookback - 4)):
                base = recent.iloc[i-2:i+1]
                rally = recent.iloc[i+1:i+4]
                if len(base) == 3 and len(rally) == 3:
                    if rally['close'].iloc[-1] > base['close'].iloc[-1] * 1.003:
                        demand_zones.append({'mid': (base['high'].max() + base['low'].min()) / 2, 'age': len(recent) - i})
                    if rally['close'].iloc[-1] < base['close'].iloc[-1] * 0.997:
                        supply_zones.append({'mid': (base['high'].max() + base['low'].min()) / 2, 'age': len(recent) - i})
            return {'demand': demand_zones, 'supply': supply_zones}
        except:
            return {'demand': [], 'supply': []}

    def _find_fvgs(self, df, lookback=50):
        recent = df.tail(lookback).copy()
        bullish_fvgs, bearish_fvgs = [], []
        try:
            for i in range(2, len(recent)):
                if recent['low'].iloc[i] > recent['high'].iloc[i - 2]:
                    gap = recent['low'].iloc[i] - recent['high'].iloc[i - 2]
                    if gap > recent['close'].iloc[i] * 0.0005:
                        bullish_fvgs.append({'mid': (recent['low'].iloc[i] + recent['high'].iloc[i - 2]) / 2, 'age': len(recent) - i})
                if recent['high'].iloc[i] < recent['low'].iloc[i - 2]:
                    gap = recent['low'].iloc[i - 2] - recent['high'].iloc[i]
                    if gap > recent['close'].iloc[i] * 0.0005:
                        bearish_fvgs.append({'mid': (recent['low'].iloc[i - 2] + recent['high'].iloc[i]) / 2, 'age': len(recent) - i})
        except: pass
        return {'bullish': bullish_fvgs, 'bearish': bearish_fvgs}

    def _detect_structure(self, df, lookback=50):
        recent = df.tail(lookback).copy()
        cp = recent['close'].iloc[-1]
        swing_highs, swing_lows = [], []
        for i in range(2, min(len(recent) - 2, lookback - 2)):
            if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i-2]
                and recent['high'].iloc[i] > recent['high'].iloc[i+1] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                swing_highs.append({'price': recent['high'].iloc[i], 'index': i})
            if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i-2]
                and recent['low'].iloc[i] < recent['low'].iloc[i+1] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                swing_lows.append({'price': recent['low'].iloc[i], 'index': i})
        result = {'structure': 'neutral', 'description': 'ساختار نامشخص', 'bos': None, 'choch': None, 'direction': 'neutral'}
        try:
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                last_sh, prev_sh = swing_highs[-1], swing_highs[-2]
                last_sl, prev_sl = swing_lows[-1], swing_lows[-2]
                if cp > last_sh['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(structure='bullish', bos='bullish', direction='bullish', description='BOS صعودی - ادامه روند')
                elif cp < last_sl['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(structure='bearish', bos='bearish', direction='bearish', description='BOS نزولی - ادامه روند')
                elif cp > last_sh['price'] and last_sl['index'] > prev_sl['index']:
                    # CHoCH صعودی - ریورسال
                    result.update(structure='reversal_bullish', choch='bullish', direction='bullish', description='CHoCH صعودی - ریورسال به صعودی')
                elif cp < last_sl['price'] and last_sh['index'] > prev_sh['index']:
                    # CHoCH نزولی - ریورسال
                    result.update(structure='reversal_bearish', choch='bearish', direction='bearish', description='CHoCH نزولی - ریورسال به نزولی')
        except: pass
        return result

    def _find_liquidity_sweep(self, df, lookback=30):
        """تشخیص Liquidity Sweep (شکست فیک)"""
        try:
            recent = df.tail(lookback).copy()
            if len(recent) < 10:
                return {'detected': False}

            # پیدا کردن سقف/کف اخیر
            recent_high = recent['high'].iloc[:-3].max()
            recent_low = recent['low'].iloc[:-3].min()
            cp = recent['close'].iloc[-1]
            prev_cp = recent['close'].iloc[-2]

            # Bullish Sweep: قیمت کف رو شکسته ولی بسته‌شده بالاتر
            if recent['low'].iloc[-1] < recent_low and cp > recent_low:
                return {'detected': True, 'type': 'bullish', 'description': 'Liquidity Sweep صعودی - شکست فیک کف'}

            # Bearish Sweep: قیمت سقف رو شکسته ولی بسته‌شده پایین‌تر
            if recent['high'].iloc[-1] > recent_high and cp < recent_high:
                return {'detected': True, 'type': 'bearish', 'description': 'Liquidity Sweep نزولی - شکست فیک سقف'}

            return {'detected': False}
        except:
            return {'detected': False}


# ============================================================
# Trigger Analyzer (15m)
# ============================================================
class TriggerAnalyzer:
    """تحلیل تریگر ورود در 15m"""

    def __init__(self):
        self.fetcher = DataFetcher()

    def analyze(self):
        try:
            df = self.fetcher.fetch_closed_candles(TF_TRIGGER, count=100)
            if df.empty or len(df) < 20:
                return {'patterns': [], 'rsi': 50, 'ema_trend': 'neutral', 'description': 'داده کافی نیست'}

            cp = df['close'].iloc[-1]
            patterns = self._detect_patterns(df)
            rsi = self._calc_rsi(df)
            ema_trend = self._calc_ema_trend(df)

            # امتیاز تریگر
            bull_score = 0
            bear_score = 0

            for p in patterns:
                if p['type'] == 'bullish':
                    bull_score += p['strength'] / 100
                elif p['type'] == 'bearish':
                    bear_score += p['strength'] / 100

            if rsi < RSI_OVERSOLD:
                bull_score += 0.5
            elif rsi > RSI_OVERBOUGHT:
                bear_score += 0.5

            if ema_trend == 'bullish':
                bull_score += 0.5
            elif ema_trend == 'bearish':
                bear_score += 0.5

            # تعیین جهت تریگر
            if bull_score > bear_score:
                direction = 'bullish'
                strength = min(100, bull_score * 50)
            elif bear_score > bull_score:
                direction = 'bearish'
                strength = min(100, bear_score * 50)
            else:
                direction = 'neutral'
                strength = 30

            return {
                'patterns': patterns,
                'rsi': rsi,
                'ema_trend': ema_trend,
                'direction': direction,
                'strength': strength,
                'current_price': cp,
                'bull_score': bull_score,
                'bear_score': bear_score,
                'description': f"تریگر: {direction} (RSI: {rsi:.0f}, {len(patterns)} الگو)",
            }
        except Exception as e:
            logger.error(f"خطا در Trigger: {e}")
            return {'patterns': [], 'rsi': 50, 'ema_trend': 'neutral', 'description': f'خطا: {e}'}

    def _detect_patterns(self, df):
        patterns = []
        if len(df) < 3:
            return patterns
        c, p = df.iloc[-1], df.iloc[-2]
        p2 = df.iloc[-3] if len(df) >= 3 else p
        body_c = abs(c['close'] - c['open'])
        us_c = c['high'] - max(c['close'], c['open'])
        ls_c = min(c['close'], c['open']) - c['low']
        range_c = c['high'] - c['low']
        if range_c == 0:
            return patterns

        # Pin Bar
        if ls_c > 2 * body_c and ls_c > range_c * 0.6 and body_c > 0:
            patterns.append({'name': 'پین بار صعودی', 'type': 'bullish', 'strength': 75})
        if us_c > 2 * body_c and us_c > range_c * 0.6 and body_c > 0:
            patterns.append({'name': 'پین بار نزولی', 'type': 'bearish', 'strength': 75})

        # Engulfing
        if p['close'] < p['open'] and c['close'] > c['open'] and c['close'] > p['open'] and c['open'] < p['close']:
            patterns.append({'name': 'پوشای صعودی', 'type': 'bullish', 'strength': 80})
        if p['close'] > p['open'] and c['close'] < c['open'] and c['open'] > p['close'] and c['close'] < p['open']:
            patterns.append({'name': 'پوشای نزولی', 'type': 'bearish', 'strength': 80})

        # Hammer / Shooting Star
        if ls_c > body_c * 2 and us_c < body_c * 0.3 and body_c > 0:
            patterns.append({'name': 'چکش', 'type': 'bullish', 'strength': 75})
        if us_c > body_c * 2 and ls_c < body_c * 0.3 and body_c > 0:
            patterns.append({'name': 'ستاره دنباله‌دار', 'type': 'bearish', 'strength': 75})

        # Morning/Evening Star
        if len(df) >= 3:
            if (p2['close'] < p2['open'] and abs(p['close'] - p['open']) < 0.3 * (p['high'] - p['low'])
                and c['close'] > c['open'] and c['close'] > (p2['open'] + p2['close']) / 2):
                patterns.append({'name': 'ستاره صبحگاهی', 'type': 'bullish', 'strength': 90})
            if (p2['close'] > p2['open'] and abs(p['close'] - p['open']) < 0.3 * (p['high'] - p['low'])
                and c['close'] < c['open'] and c['close'] < (p2['open'] + p2['close']) / 2):
                patterns.append({'name': 'ستاره عصرگاهی', 'type': 'bearish', 'strength': 90})

        return patterns

    def _calc_rsi(self, df):
        try:
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
            rs = gain / loss
            rsi = (100 - (100 / (1 + rs))).iloc[-1]
            return float(rsi) if not pd.isna(rsi) else 50
        except:
            return 50

    def _calc_ema_trend(self, df):
        try:
            if len(df) < EMA_SLOW:
                return 'neutral'
            price = df['close'].iloc[-1]
            ema21 = df['close'].ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
            ema50 = df['close'].ewm(span=EMA_MID, adjust=False).mean().iloc[-1]
            ema200 = df['close'].ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
            bull = (price > ema200) + (ema21 > ema50) + (ema50 > ema200)
            bear = (price < ema200) + (ema21 < ema50) + (ema50 < ema200)
            if bull >= 3: return 'bullish'
            if bear >= 3: return 'bearish'
            if bull > bear: return 'bullish'
            if bear > bull: return 'bearish'
            return 'neutral'
        except:
            return 'neutral'


# ============================================================
# Signal Engine - ترکیب همه لایه‌ها
# ============================================================
class SignalEngine:
    """
    موتور سیگنال - استراتژی واقعی

    3 حالت ترید:
    1. Continuation (در جهت روند)
    2. Reversal (برخلاف روند، وقتی CHoCH هست)
    3. Range (در رنج، خرید از کف، فروش از سقف)
    """

    def __init__(self):
        self.fetcher = DataFetcher()
        self.context = MarketContextAnalyzer()
        self.pattern = PatternAnalyzer()
        self.trigger = TriggerAnalyzer()

    def analyze(self):
        try:
            print("\n📊 لایه 1: Context (Daily)...")
            context = self.context.analyze()
            print(f"   {context['description']}")

            print("\n📊 لایه 2: Pattern (1H)...")
            pattern = self.pattern.analyze()
            print(f"   {pattern['description']}")

            print("\n📊 لایه 3: Trigger (15m)...")
            trigger = self.trigger.analyze()
            print(f"   {trigger['description']}")

            # محاسبه امتیاز کلی
            cp = trigger.get('current_price', 0)
            bull_score = 0
            bear_score = 0

            # 1. Context (وزن 30%)
            if context['trend'] == 'bullish':
                bull_score += context['strength'] * 0.30
            elif context['trend'] == 'bearish':
                bear_score += context['strength'] * 0.30
            elif context['trend'] == 'range':
                # در رنج، بررسی موقعیت قیمت
                if 'range_high' in context and 'range_low' in context:
                    range_high = context['range_high']
                    range_low = context['range_low']
                    # اگر نزدیک کف هست - خرید
                    if cp < range_low * 1.001:
                        bull_score += 50 * 0.30
                    # اگر نزدیک سقف هست - فروش
                    elif cp > range_high * 0.999:
                        bear_score += 50 * 0.30

            # 2. Pattern Structure (وزن 35%)
            structure = pattern.get('structure', {})
            struct_dir = structure.get('direction', 'neutral')
            if struct_dir == 'bullish':
                # BOS یا CHoCH صعودی
                bull_score += 80 * 0.35
                if structure.get('choch') == 'bullish':
                    # ریورسال - امتیاز بیشتر
                    bull_score += 20 * 0.35
            elif struct_dir == 'bearish':
                bear_score += 80 * 0.35
                if structure.get('choch') == 'bearish':
                    bear_score += 20 * 0.35

            # Liquidity Sweep
            liq = pattern.get('liquidity_sweep', {})
            if liq.get('detected'):
                if liq.get('type') == 'bullish':
                    bull_score += 70 * 0.15
                elif liq.get('type') == 'bearish':
                    bear_score += 70 * 0.15

            # 3. Trigger (وزن 35%)
            trig_dir = trigger.get('direction', 'neutral')
            trig_strength = trigger.get('strength', 0)
            if trig_dir == 'bullish':
                bull_score += trig_strength * 0.35
            elif trig_dir == 'bearish':
                bear_score += trig_strength * 0.35

            # محاسبه نهایی
            total = bull_score + bear_score
            if total > 0:
                bull_pct = (bull_score / total) * 100
                bear_pct = (bear_score / total) * 100
            else:
                bull_pct = bear_pct = 50

            diff = bull_pct - bear_pct

            # تصمیم‌گیری
            if diff > 15:
                direction = 'BUY'
                strength = min(100, 50 + abs(diff))
            elif diff < -15:
                direction = 'SELL'
                strength = min(100, 50 + abs(diff))
            else:
                direction = 'WAIT'
                strength = 50 - abs(diff)

            # تعیین نوع ورود
            entry_type = None
            entry_price = cp
            nearest_zone = None

            if direction != 'WAIT':
                # پیدا کردن نزدیک‌ترین ناحیه مناسب
                zones = pattern.get('all_zones', [])
                if direction == 'BUY':
                    # برای خرید، ناحیه demand زیر قیمت
                    suitable_zones = [z for z in zones if z['type'] == 'demand' and z['price'] < cp]
                else:
                    # برای فروش، ناحیه supply بالای قیمت
                    suitable_zones = [z for z in zones if z['type'] == 'supply' and z['price'] > cp]

                if suitable_zones:
                    nearest_zone = min(suitable_zones, key=lambda x: abs(x['price'] - cp))
                    distance_pct = abs(nearest_zone['price'] - cp) / cp * 100

                    if distance_pct < 0.15:
                        # قیمت در ناحیه هست - ورود مارکت
                        entry_type = 'market'
                        entry_price = cp
                    else:
                        # منتظر رسیدن به ناحیه - ورود لیمیت
                        entry_type = 'limit'
                        entry_price = nearest_zone['price']

            # محاسبه SL/TP
            atr_val = self._calc_atr()
            stop_loss = tp1 = tp2 = tp3 = None
            if direction != 'WAIT':
                if direction == 'BUY':
                    stop_loss = entry_price - (atr_val * ATR_SL_MULTIPLIER)
                    risk = entry_price - stop_loss
                    tp1 = entry_price + risk * 1.0
                    tp2 = entry_price + risk * 2.0
                    tp3 = entry_price + risk * 3.0
                else:
                    stop_loss = entry_price + (atr_val * ATR_SL_MULTIPLIER)
                    risk = stop_loss - entry_price
                    tp1 = entry_price - risk * 1.0
                    tp2 = entry_price - risk * 2.0
                    tp3 = entry_price - risk * 3.0

            # توضیح دلیل
            reasons = []
            if context['trend'] != 'unknown':
                reasons.append(f"Context: {context['trend']}")
            if struct_dir != 'neutral':
                reasons.append(f"Structure: {structure.get('description', '')}")
            if liq.get('detected'):
                reasons.append(f"Liquidity: {liq.get('description', '')}")
            if trigger.get('patterns'):
                reasons.append(f"Trigger: {len(trigger['patterns'])} الگو")
            if entry_type:
                reasons.append(f"Entry: {entry_type}")
            reason = " | ".join(reasons) if reasons else "تحلیل انجام شد"

            return {
                'direction': direction,
                'strength': strength,
                'confidence': 'high' if strength >= 75 else 'medium' if strength >= 60 else 'low',
                'bull_pct': bull_pct,
                'bear_pct': bear_pct,
                'diff': diff,
                'entry_type': entry_type,
                'entry_price': entry_price,
                'current_price': cp,
                'stop_loss': stop_loss,
                'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
                'risk_reward': 3.0,
                'timestamp': datetime.now().isoformat(),
                'context': context,
                'pattern': pattern,
                'trigger': trigger,
                'nearest_zone': nearest_zone,
                'reason': reason,
            }
        except Exception as e:
            logger.error(f"خطا در تحلیل: {e}", exc_info=True)
            return None

    def _calc_atr(self):
        try:
            df = self.fetcher.fetch_closed_candles(TF_TRIGGER, count=50)
            hl = df['high'] - df['low']
            hc = (df['high'] - df['close'].shift()).abs()
            lc = (df['low'] - df['close'].shift()).abs()
            tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean().iloc[-1]
            if pd.isna(atr) or atr == 0:
                atr = df['close'].iloc[-1] * 0.002
            return float(atr)
        except:
            return 0.3


# ============================================================
# AI Decision Maker (تصمیم‌گیرنده، نه فقط تحلیلگر)
# ============================================================
class AIDecisionMaker:
    """AI برای تأیید نهایی سیگنال"""

    def __init__(self):
        self.enabled = bool(ZAI_API_KEY)
        self.api_key = ZAI_API_KEY

    def review_signal(self, signal):
        """بررسی سیگنال توسط AI و تأیید/رد"""
        if not self.enabled:
            return {'approved': True, 'reason': 'AI غیرفعال', 'analysis': None}

        try:
            context = signal.get('context', {})
            pattern = signal.get('pattern', {})
            trigger = signal.get('trigger', {})

            prompt = f"""
شما یک تریدر حرفه‌ای بانک هستید. این سیگنال USD/JPY را بررسی کنید:

سیگنال: {signal['direction']} (قدرت: {signal['strength']:.0f}%)
قیمت فعلی: {signal.get('current_price', 0):.3f}

Context (Daily):
- روند: {context.get('trend', 'unknown')}
- {context.get('description', '')}

Pattern (1H):
- {pattern.get('description', '')}
- ساختار: {pattern.get('structure', {}).get('description', 'نامشخص')}
- Liquidity Sweep: {pattern.get('liquidity_sweep', {}).get('detected', False)}

Trigger (15m):
- {trigger.get('description', '')}
- الگوها: {len(trigger.get('patterns', []))}

ناحیه ورود: {signal.get('nearest_zone', {}).get('source', 'نامشخص') if signal.get('nearest_zone') else 'نامشخص'}
نوع ورود: {signal.get('entry_type', 'نامشخص')}

سوالات:
1. آیا با این سیگنال موافقی؟ (بله/خیر)
2. اگر خیر، چرا؟
3. نقاط قوت سیگنال چیست؟
4. ریسک‌ها چیست؟
5. توصیه نهایی (یک جمله)

پاسخ را در قالب JSON زیر بده:
{{
  "approved": true/false,
  "reason": "دلیل کوتاه",
  "strengths": "نقاط قوت",
  "risks": "ریسک‌ها",
  "recommendation": "توصیه"
}}
"""
            response = requests.post(
                "https://api.z.ai/api/paas/v4/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 500},
                timeout=30
            )

            if response.status_code == 200:
                content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                # تلاش برای parse JSON
                try:
                    # پیدا کردن JSON در متن
                    start = content.find('{')
                    end = content.rfind('}') + 1
                    if start >= 0 and end > start:
                        result = json.loads(content[start:end])
                        return {
                            'approved': result.get('approved', True),
                            'reason': result.get('reason', ''),
                            'strengths': result.get('strengths', ''),
                            'risks': result.get('risks', ''),
                            'recommendation': result.get('recommendation', ''),
                            'analysis': content,
                        }
                except:
                    pass
                return {'approved': True, 'reason': 'AI بررسی کرد اما فرمت نامشخص', 'analysis': content}
            return {'approved': True, 'reason': 'خطا در API', 'analysis': None}
        except Exception as e:
            logger.error(f"خطا در AI: {e}")
            return {'approved': True, 'reason': f'خطا: {e}', 'analysis': None}


# ============================================================
# Stats Manager
# ============================================================
class StatsManager:
    def __init__(self):
        self.signals_db = SIGNALS_DB_FILE

    def load_signals(self):
        try:
            if os.path.exists(self.signals_db):
                with open(self.signals_db, 'r') as f:
                    return json.load(f)
        except: pass
        return []

    def save_signal(self, signal):
        signals = self.load_signals()
        minimal = {
            'direction': signal['direction'],
            'strength': signal.get('strength', 0),
            'entry_type': signal.get('entry_type'),
            'price': signal.get('current_price'),
            'timestamp': signal['timestamp'],
        }
        signals.append(minimal)
        if len(signals) > 500:
            signals = signals[-500:]
        try:
            with open(self.signals_db, 'w') as f:
                json.dump(signals, f, indent=2)
        except: pass


# ============================================================
# Telegram
# ============================================================
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"خطا: {e}")
        return False


def format_signal_message(signal, ai_review=None):
    direction = signal['direction']
    if direction == 'BUY':
        icon, dir_fa = "🟢", "خرید"
    elif direction == 'SELL':
        icon, dir_fa = "🔴", "فروش"
    else:
        icon, dir_fa = "⏳", "صبر"

    conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(signal.get('confidence', 'low'), "⚪")

    msg = f"{icon} سیگنال {dir_fa} USD/JPY\n"
    msg += f"{'='*30}\n\n"

    msg += f"💰 قیمت فعلی: {signal.get('current_price', 0):.3f}\n"
    if direction in ['BUY', 'SELL']:
        entry_type = signal.get('entry_type', 'market')
        entry_icon = "⚡" if entry_type == 'market' else "📋"
        msg += f"{entry_icon} نوع ورود: {entry_type.upper()}\n"
        msg += f"🔹 نقطه ورود: {signal.get('entry_price', 0):.3f}\n"
    msg += f"💪 قدرت: {signal.get('strength', 0):.0f}%\n"
    msg += f"{conf_icon} اطمینان: {signal.get('confidence', 'low')}\n"
    msg += f"📈 صعودی: {signal.get('bull_pct', 50):.0f}% | 📉 نزولی: {signal.get('bear_pct', 50):.0f}%\n"
    msg += f"🕐 زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    # Context
    context = signal.get('context', {})
    msg += "📊 Context (Daily):\n"
    msg += "-"*30 + "\n"
    msg += f"• {context.get('description', 'نامشخص')}\n"
    if context.get('is_range'):
        msg += f"• بازار در حالت رنج\n"
    msg += "\n"

    # Pattern
    pattern = signal.get('pattern', {})
    msg += "📊 Pattern (1H):\n"
    msg += "-"*30 + "\n"
    msg += f"• {pattern.get('description', 'نامشخص')}\n"
    structure = pattern.get('structure', {})
    msg += f"• ساختار: {structure.get('description', 'نامشخص')}\n"

    liq = pattern.get('liquidity_sweep', {})
    if liq.get('detected'):
        msg += f"• {liq.get('description', '')}\n"

    nz = signal.get('nearest_zone')
    if nz:
        msg += f"• ناحیه فعال: {nz.get('source', '?')} ({nz.get('type', '?')}) - {nz.get('price', 0):.3f}\n"
    msg += "\n"

    # Trigger
    trigger = signal.get('trigger', {})
    msg += "📊 Trigger (15m):\n"
    msg += "-"*30 + "\n"
    msg += f"• {trigger.get('description', 'نامشخص')}\n"

    patterns = trigger.get('patterns', [])
    if patterns:
        msg += "الگوها:\n"
        for p in patterns:
            ic = "🟢" if p['type'] == 'bullish' else "🔴" if p['type'] == 'bearish' else "⚪"
            msg += f"  {ic} {p['name']} ({p['strength']}%)\n"
    msg += "\n"

    if direction in ['BUY', 'SELL']:
        msg += "📌 مشخصات معامله:\n"
        msg += "-"*30 + "\n"
        if signal.get('stop_loss'):
            msg += f"🛑 حد ضرر: {signal['stop_loss']:.3f} ({abs(signal.get('entry_price', 0) - signal['stop_loss'])*100:.1f} پیپ)\n"
        if signal.get('tp1'):
            msg += f"🎯 TP1: {signal['tp1']:.3f}\n"
        if signal.get('tp2'):
            msg += f"🎯 TP2: {signal['tp2']:.3f}\n"
        if signal.get('tp3'):
            msg += f"🎯 TP3: {signal['tp3']:.3f}\n"
        msg += f"⚖️ ریسک/ریوارد: 1:3\n\n"

    # AI Review
    if ai_review and ai_review.get('analysis'):
        msg += "🤖 تحلیل AI:\n"
        msg += "-"*30 + "\n"
        if ai_review.get('approved'):
            msg += f"✅ تأیید AI: {ai_review.get('reason', '')}\n"
        else:
            msg += f"⚠️ هشدار AI: {ai_review.get('reason', '')}\n"
        if ai_review.get('recommendation'):
            msg += f"💡 توصیه: {ai_review.get('recommendation', '')}\n"
        msg += "\n"

    msg += f"💡 دلیل: {signal.get('reason', '')}\n"
    msg += f"\n{'='*30}\n"
    msg += "⚠️ این سیگنال صرفاً تحلیلی است.\n"
    msg += "🎯 حداقل 1 سیگنال روزانه دریافت می‌کنید."
    return msg


# ============================================================
# Main
# ============================================================
def main():
    print("\n" + "=" * 60)
    print("🤖 ربات سیگنال USD/JPY - استراتژی واقعی")
    print("=" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ تنظیمات تلگرام ناقص!")
        sys.exit(1)

    print(f"📊 نماد: {SYMBOL_DISPLAY}")
    print(f"🏗 Context: Daily")
    print(f"📐 Pattern: 1H")
    print(f"🎯 Trigger: 15m")
    print(f"🤖 AI: {'فعال' if ZAI_API_KEY else 'غیرفعال'}")
    print(f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    engine = SignalEngine()
    signal = engine.analyze()

    if not signal:
        send_telegram_message("❌ خطا در تحلیل بازار.")
        return

    print(f"\n📊 نتیجه:")
    print(f"جهت: {signal['direction']}")
    print(f"قدرت: {signal.get('strength', 0):.0f}%")
    print(f"دلیل: {signal.get('reason', '')}")

    # ذخیره
    stats = StatsManager()
    stats.save_signal(signal)

    # بررسی AI
    ai_review = None
    if ZAI_API_KEY:
        print("\n🤖 بررسی توسط AI...")
        ai_decision = AIDecisionMaker()
        ai_review = ai_decision.review_signal(signal)
        if ai_review:
            print(f"✓ AI تأیید کرد: {ai_review.get('approved')}")

    # ارسال
    print("\n📱 ارسال به تلگرام...")
    msg = format_signal_message(signal, ai_review)
    if send_telegram_message(msg):
        print("✅ ارسال شد!")
    else:
        print("❌ خطا در ارسال")


if __name__ == "__main__":
    main()
