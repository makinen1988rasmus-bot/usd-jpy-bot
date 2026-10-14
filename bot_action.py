# -*- coding: utf-8 -*-
"""
ربات سیگنال‌دهی USD/JPY - استراتژی واقعی بانکی v3
USD/JPY Signal Bot - Real Bank Strategy v3

اصلاحات نسخه 3:
1. ورود هوشمند: Market برای continuation، Limit فقط برای reversal/range
2. Breakout detection: وقتی قیمت سطح رو می‌شکنه، فوراً Market
3. Momentum entry: اگه مومنتوم قویه، Market نه Limit
4. منطق صحیح: BOS = continuation = Market، CHoCH = reversal = Limit
5. حداقل 1 سیگنال روزانه با کیفیت

قوانین ورود:
- BOS صعودی + RSI>55 → MARKET BUY (روند قوی)
- BOS نزولی + RSI<45 → MARKET SELL (روند قوی)
- CHoCH صعودی → LIMIT BUY at demand zone (ریورسال)
- CHoCH نزولی → LIMIT SELL at supply zone (ریورسال)
- Range → LIMIT at boundaries
- Breakout → MARKET
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

TF_HTF = "1d"
TF_PATTERN = "1h"
TF_TRIGGER = "15m"

EMA_FAST = 21
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5

# آستانه مومنتوم برای ورود Market
MOMENTUM_THRESHOLD = 55  # RSI بالای 55 برای BUY market، زیر 45 برای SELL market

SIGNALS_DB_FILE = "signals_db.json"
LAST_SIGNAL_FILE = "last_signal.json"

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
        """فقط کندل‌های بسته‌شده"""
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
            # حذف کندل آخر (در حال تشکیل)
            df = df.iloc[:-1].copy()

            self._cache[cache_key] = df
            self._cache_time[cache_key] = t.time()
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {timeframe}: {e}")
            raise

    def get_current_price(self):
        """قیمت لحظه‌ای (برای مقایسه)"""
        try:
            df = self.fetch_closed_candles(TF_TRIGGER, count=5)
            return float(df['close'].iloc[-1])
        except:
            return 0


# ============================================================
# Market Context Analyzer (Daily)
# ============================================================
class MarketContextAnalyzer:
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

            bull = bear = 0
            if price > ema200: bull += 1
            else: bear += 1
            if ema21 > ema50: bull += 1
            else: bear += 1
            if ema50 > ema200: bull += 1
            else: bear += 1

            ema_spread = abs(ema21 - ema50) / price * 100
            is_range = ema_spread < 0.3

            swing_highs, swing_lows = self._find_swings(df)

            if is_range and len(swing_highs) >= 2 and len(swing_lows) >= 2:
                range_high = max(swing_highs[-3:]) if len(swing_highs) >= 3 else max(swing_highs)
                range_low = min(swing_lows[-3:]) if len(swing_lows) >= 3 else min(swing_lows)
                return {
                    'trend': 'range',
                    'strength': 50,
                    'description': f"بازار رنج ({range_low:.2f} - {range_high:.2f})",
                    'range_high': range_high,
                    'range_low': range_low,
                    'price': price,
                    'is_range': True,
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
                'is_range': False,
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
    def __init__(self):
        self.fetcher = DataFetcher()

    def analyze(self):
        try:
            df = self.fetcher.fetch_closed_candles(TF_PATTERN, count=200)
            if df.empty or len(df) < 50:
                return {'zones': [], 'structure': {}, 'description': 'داده کافی نیست'}

            cp = df['close'].iloc[-1]

            obs = self._find_order_blocks(df)
            sd = self._find_supply_demand(df)
            structure = self._detect_structure(df)
            liquidity = self._find_liquidity_sweep(df)
            breakout = self._detect_breakout(df)

            zones = []
            for ob in obs['bullish']:
                zones.append({'type': 'demand', 'price': ob['price'], 'source': 'OB', 'age': ob['age']})
            for ob in obs['bearish']:
                zones.append({'type': 'supply', 'price': ob['price'], 'source': 'OB', 'age': ob['age']})
            for z in sd['demand']:
                zones.append({'type': 'demand', 'price': z['mid'], 'source': 'SD', 'age': z['age']})
            for z in sd['supply']:
                zones.append({'type': 'supply', 'price': z['mid'], 'source': 'SD', 'age': z['age']})

            valid_zones = [z for z in zones if z['age'] <= 25]
            valid_zones.sort(key=lambda x: abs(x['price'] - cp))

            return {
                'zones': valid_zones[:5],
                'all_zones': valid_zones,
                'structure': structure,
                'liquidity_sweep': liquidity,
                'breakout': breakout,
                'current_price': cp,
                'description': f"{len(valid_zones)} ناحیه، ساختار: {structure.get('description', 'نامشخص')}",
            }
        except Exception as e:
            logger.error(f"خطا در Pattern: {e}")
            return {'zones': [], 'structure': {}, 'description': f'خطا: {e}'}

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

        result = {'structure': 'neutral', 'description': 'ساختار نامشخص', 'bos': None, 'choch': None, 'type': 'none'}
        try:
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                last_sh, prev_sh = swing_highs[-1], swing_highs[-2]
                last_sl, prev_sl = swing_lows[-1], swing_lows[-2]

                # BOS صعودی: قیمت آخرین سقف رو شکسته (continuation)
                if cp > last_sh['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(structure='bullish', bos='bullish', type='continuation', description='BOS صعودی - ادامه روند صعودی')
                # BOS نزولی: قیمت آخرین کف رو شکسته (continuation)
                elif cp < last_sl['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(structure='bearish', bos='bearish', type='continuation', description='BOS نزولی - ادامه روند نزولی')
                # CHoCH صعودی: ریورسال
                elif cp > last_sh['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(structure='bullish', choch='bullish', type='reversal', description='CHoCH صعودی - تغییر روند به صعودی')
                # CHoCH نزولی: ریورسال
                elif cp < last_sl['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(structure='bearish', choch='bearish', type='reversal', description='CHoCH نزولی - تغییر روند به نزولی')
        except: pass
        return result

    def _find_liquidity_sweep(self, df, lookback=30):
        try:
            recent = df.tail(lookback).copy()
            if len(recent) < 10:
                return {'detected': False}
            recent_high = recent['high'].iloc[:-3].max()
            recent_low = recent['low'].iloc[:-3].min()
            cp = recent['close'].iloc[-1]
            if recent['low'].iloc[-1] < recent_low and cp > recent_low:
                return {'detected': True, 'type': 'bullish', 'description': 'Liquidity Sweep صعودی - شکست فیک کف'}
            if recent['high'].iloc[-1] > recent_high and cp < recent_high:
                return {'detected': True, 'type': 'bearish', 'description': 'Liquidity Sweep نزولی - شکست فیک سقف'}
            return {'detected': False}
        except:
            return {'detected': False}

    def _detect_breakout(self, df, lookback=20):
        """تشخیص breakout - وقتی قیمت سطح مهم رو می‌شکنه"""
        try:
            recent = df.tail(lookback).copy()
            if len(recent) < 10:
                return {'detected': False}

            # سطوح مقاومت و حمایت اخیر
            resistance = recent['high'].iloc[:-3].max()
            support = recent['low'].iloc[:-3].min()
            cp = recent['close'].iloc[-1]
            prev_cp = recent['close'].iloc[-2]

            # Breakout صعودی: قیمت مقاومت رو شکسته و بالاتر بسته شده
            if cp > resistance and prev_cp <= resistance:
                return {'detected': True, 'type': 'bullish', 'description': f'Breakout صعودی - شکست {resistance:.3f}', 'level': resistance}

            # Breakout نزولی
            if cp < support and prev_cp >= support:
                return {'detected': True, 'type': 'bearish', 'description': f'Breakout نزولی - شکست {support:.3f}', 'level': support}

            return {'detected': False}
        except:
            return {'detected': False}


# ============================================================
# Trigger Analyzer (15m)
# ============================================================
class TriggerAnalyzer:
    def __init__(self):
        self.fetcher = DataFetcher()

    def analyze(self):
        try:
            df = self.fetcher.fetch_closed_candles(TF_TRIGGER, count=100)
            if df.empty or len(df) < 20:
                return {'patterns': [], 'rsi': 50, 'ema_trend': 'neutral', 'direction': 'neutral', 'strength': 0, 'description': 'داده کافی نیست'}

            cp = df['close'].iloc[-1]
            patterns = self._detect_patterns(df)
            rsi = self._calc_rsi(df)
            ema_trend = self._calc_ema_trend(df)

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
                'description': f"تریگر: {direction} (RSI: {rsi:.0f}, {len(patterns)} الگو)",
            }
        except Exception as e:
            logger.error(f"خطا در Trigger: {e}")
            return {'patterns': [], 'rsi': 50, 'ema_trend': 'neutral', 'direction': 'neutral', 'strength': 0, 'description': f'خطا: {e}'}

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

        if ls_c > 2 * body_c and ls_c > range_c * 0.6 and body_c > 0:
            patterns.append({'name': 'پین بار صعودی', 'type': 'bullish', 'strength': 75})
        if us_c > 2 * body_c and us_c > range_c * 0.6 and body_c > 0:
            patterns.append({'name': 'پین بار نزولی', 'type': 'bearish', 'strength': 75})

        if p['close'] < p['open'] and c['close'] > c['open'] and c['close'] > p['open'] and c['open'] < p['close']:
            patterns.append({'name': 'پوشای صعودی', 'type': 'bullish', 'strength': 80})
        if p['close'] > p['open'] and c['close'] < c['open'] and c['open'] > p['close'] and c['close'] < p['open']:
            patterns.append({'name': 'پوشای نزولی', 'type': 'bearish', 'strength': 80})

        if ls_c > body_c * 2 and us_c < body_c * 0.3 and body_c > 0:
            patterns.append({'name': 'چکش', 'type': 'bullish', 'strength': 75})
        if us_c > body_c * 2 and ls_c < body_c * 0.3 and body_c > 0:
            patterns.append({'name': 'ستاره دنباله‌دار', 'type': 'bearish', 'strength': 75})

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
# Signal Engine - منطق ورود هوشمند
# ============================================================
class SignalEngine:
    """
    موتور سیگنال با منطق ورود هوشمند

    قوانین ورود:
    1. BOS + Momentum → MARKET (روند قوی)
    2. CHoCH → LIMIT at zone (ریورسال)
    3. Breakout → MARKET (شکست سطح)
    4. Range → LIMIT at boundaries
    5. Liquidity Sweep → MARKET
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

            cp = trigger.get('current_price', 0)
            if cp == 0:
                cp = pattern.get('current_price', 0)

            structure = pattern.get('structure', {})
            struct_type = structure.get('type', 'none')  # continuation یا reversal یا none
            struct_dir = 'bullish' if structure.get('structure') == 'bullish' else 'bearish' if structure.get('structure') == 'bearish' else 'neutral'

            rsi = trigger.get('rsi', 50)
            breakout = pattern.get('breakout', {})
            liquidity = pattern.get('liquidity_sweep', {})

            # ============================================
            # تعیین جهت و نوع ورود - منطق هوشمند
            # ============================================

            direction = 'WAIT'
            entry_type = None
            entry_price = cp
            entry_reason = ""
            strength = 0

            # ============================================
            # حالت 1: Breakout (قوی‌ترین سیگنال)
            # ============================================
            if breakout.get('detected'):
                direction = 'BUY' if breakout['type'] == 'bullish' else 'SELL'
                entry_type = 'market'
                entry_price = cp
                entry_reason = f"Breakout {breakout['type']} - شکست سطح {breakout.get('level', 0):.3f}"
                strength = 85
                print(f"   ✓ Breakout detected: {direction}")

            # ============================================
            # حالت 2: Liquidity Sweep (شکست فیک)
            # ============================================
            elif liquidity.get('detected'):
                direction = 'BUY' if liquidity['type'] == 'bullish' else 'SELL'
                entry_type = 'market'
                entry_price = cp
                entry_reason = f"Liquidity Sweep - {liquidity.get('description', '')}"
                strength = 80
                print(f"   ✓ Liquidity Sweep: {direction}")

            # ============================================
            # حالت 3: BOS + Momentum (continuation - MARKET)
            # ============================================
            elif struct_type == 'continuation' and struct_dir != 'neutral':
                # بررسی مومنتوم
                if struct_dir == 'bullish' and rsi > 50:
                    direction = 'BUY'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"BOS صعودی + Momentum (RSI: {rsi:.0f}) - ورود Market"
                    strength = 75
                    print(f"   ✓ Continuation BUY (Market)")
                elif struct_dir == 'bearish' and rsi < 50:
                    direction = 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"BOS نزولی + Momentum (RSI: {rsi:.0f}) - ورود Market"
                    strength = 75
                    print(f"   ✓ Continuation SELL (Market)")
                else:
                    # continuation ولی مومنتوم ضعیف - صبر کن
                    direction = 'WAIT'
                    entry_reason = f"BOS {struct_dir} اما مومنتوم ضعیف (RSI: {rsi:.0f})"

            # ============================================
            # حالت 4: CHoCH (reversal - LIMIT)
            # ============================================
            elif struct_type == 'reversal' and struct_dir != 'neutral':
                # پیدا کردن ناحیه مناسب برای LIMIT
                zones = pattern.get('all_zones', [])
                if struct_dir == 'bullish':
                    # برای BUY reversal، ناحیه demand زیر قیمت
                    suitable = [z for z in zones if z['type'] == 'demand' and z['price'] < cp]
                else:
                    # برای SELL reversal، ناحیه supply بالای قیمت
                    suitable = [z for z in zones if z['type'] == 'supply' and z['price'] > cp]

                if suitable:
                    nearest = min(suitable, key=lambda x: abs(x['price'] - cp))
                    distance_pct = abs(nearest['price'] - cp) / cp * 100

                    direction = 'BUY' if struct_dir == 'bullish' else 'SELL'
                    entry_type = 'limit'
                    entry_price = nearest['price']
                    entry_reason = f"CHoCH {struct_dir} - Limit at {nearest['source']} zone ({distance_pct:.2f}%)"
                    strength = 70
                    print(f"   ✓ Reversal {direction} (Limit at {entry_price:.3f})")
                else:
                    # ناحیه مناسب نیست - Market
                    direction = 'BUY' if struct_dir == 'bullish' else 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"CHoCH {struct_dir} - ورود Market (ناحیه مناسب نیست)"
                    strength = 65

            # ============================================
            # حالت 5: Range (LIMIT at boundaries)
            # ============================================
            elif context.get('is_range'):
                range_high = context.get('range_high', 0)
                range_low = context.get('range_low', 0)

                # اگر نزدیک کف هست - BUY
                if range_low > 0 and abs(cp - range_low) / range_low * 100 < 0.3:
                    direction = 'BUY'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"در رنج - نزدیک کف ({range_low:.3f})"
                    strength = 65
                    print(f"   ✓ Range BUY (at low)")
                # اگر نزدیک سقف هست - SELL
                elif range_high > 0 and abs(cp - range_high) / range_high * 100 < 0.3:
                    direction = 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"در رنج - نزدیک سقف ({range_high:.3f})"
                    strength = 65
                    print(f"   ✓ Range SELL (at high)")
                # در وسط رنج - LIMIT
                elif range_low > 0 and range_high > 0:
                    if cp < (range_high + range_low) / 2:
                        # نزدیک‌تر به کف - Limit BUY at low
                        direction = 'BUY'
                        entry_type = 'limit'
                        entry_price = range_low
                        entry_reason = f"در رنج - Limit BUY at {range_low:.3f}"
                        strength = 55
                    else:
                        direction = 'SELL'
                        entry_type = 'limit'
                        entry_price = range_high
                        entry_reason = f"در رنج - Limit SELL at {range_high:.3f}"
                        strength = 55
                    print(f"   ✓ Range {direction} (Limit)")

            # ============================================
            # حالت 6: فقط تریگر قوی
            # ============================================
            else:
                trig_dir = trigger.get('direction', 'neutral')
                trig_strength = trigger.get('strength', 0)
                if trig_strength >= 60 and trig_dir != 'neutral':
                    direction = 'BUY' if trig_dir == 'bullish' else 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"تریگر قوی {trig_dir} (قدرت: {trig_strength:.0f}%)"
                    strength = 60
                    print(f"   ✓ Trigger-only {direction}")
                else:
                    direction = 'WAIT'
                    entry_reason = "هیچ سیگنال قوی وجود ندارد"

            # ============================================
            # محاسبه SL/TP
            # ============================================
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

            confidence = 'high' if strength >= 75 else 'medium' if strength >= 60 else 'low'

            # محاسبه درصد صعودی/نزولی
            if direction == 'BUY':
                bull_pct = 50 + strength / 2
                bear_pct = 100 - bull_pct
            elif direction == 'SELL':
                bear_pct = 50 + strength / 2
                bull_pct = 100 - bear_pct
            else:
                bull_pct = bear_pct = 50

            return {
                'direction': direction,
                'strength': strength,
                'confidence': confidence,
                'bull_pct': bull_pct,
                'bear_pct': bear_pct,
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
                'reason': entry_reason,
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
# AI Decision Maker
# ============================================================
class AIDecisionMaker:
    def __init__(self):
        self.enabled = bool(ZAI_API_KEY)
        self.api_key = ZAI_API_KEY

    def review_signal(self, signal):
        if not self.enabled:
            return None
        try:
            context = signal.get('context', {})
            pattern = signal.get('pattern', {})
            trigger = signal.get('trigger', {})

            prompt = f"""
شما یک تریدر حرفه‌ای بانک هستید. این سیگنال USD/JPY را بررسی کنید:

سیگنال: {signal['direction']} (قدرت: {signal['strength']:.0f}%)
قیمت فعلی: {signal.get('current_price', 0):.3f}
نوع ورود: {signal.get('entry_type', 'نامشخص')}
نقطه ورود: {signal.get('entry_price', 0):.3f}

Context: {context.get('description', '')}
Pattern: {pattern.get('description', '')}
Trigger: {trigger.get('description', '')}

دلیل: {signal.get('reason', '')}

به فارسی و مختصر (حداکثر 100 کلمه):
1. آیا موافقی؟
2. نقاط قوت/ضعف
3. توصیه
"""
            response = requests.post(
                "https://api.z.ai/api/paas/v4/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 300},
                timeout=30
            )
            if response.status_code == 200:
                return response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            return None
        except Exception as e:
            logger.error(f"خطا در AI: {e}")
            return None


# ============================================================
# Stats
# ============================================================
class StatsManager:
    def __init__(self):
        self.signals_db = SIGNALS_DB_FILE

    def save_signal(self, signal):
        try:
            signals = []
            if os.path.exists(self.signals_db):
                with open(self.signals_db, 'r') as f:
                    signals = json.load(f)
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


def format_signal_message(signal, ai_analysis=None):
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
    msg += f"🕐 زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    # Context
    context = signal.get('context', {})
    msg += "📊 Context (Daily):\n"
    msg += "-"*30 + "\n"
    msg += f"• {context.get('description', 'نامشخص')}\n\n"

    # Pattern
    pattern = signal.get('pattern', {})
    msg += "📊 Pattern (1H):\n"
    msg += "-"*30 + "\n"
    msg += f"• {pattern.get('description', 'نامشخص')}\n"
    structure = pattern.get('structure', {})
    msg += f"• ساختار: {structure.get('description', 'نامشخص')}\n"
    breakout = pattern.get('breakout', {})
    if breakout.get('detected'):
        msg += f"• {breakout.get('description', '')}\n"
    liq = pattern.get('liquidity_sweep', {})
    if liq.get('detected'):
        msg += f"• {liq.get('description', '')}\n"
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

    if ai_analysis:
        msg += "🤖 تحلیل AI:\n"
        msg += "-"*30 + "\n"
        msg += f"{ai_analysis}\n\n"

    msg += f"💡 دلیل: {signal.get('reason', '')}\n"
    msg += f"\n{'='*30}\n"
    msg += "⚠️ این سیگنال صرفاً تحلیلی است."
    return msg


# ============================================================
# Main
# ============================================================
def main():
    print("\n" + "=" * 60)
    print("🤖 ربات سیگنال USD/JPY - v3 (ورود هوشمند)")
    print("=" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ تنظیمات تلگرام ناقص!")
        sys.exit(1)

    print(f"📊 نماد: {SYMBOL_DISPLAY}")
    print(f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    engine = SignalEngine()
    signal = engine.analyze()

    if not signal:
        send_telegram_message("❌ خطا در تحلیل بازار.")
        return

    print(f"\n📊 نتیجه:")
    print(f"جهت: {signal['direction']}")
    print(f"نوع ورود: {signal.get('entry_type', 'نامشخص')}")
    print(f"قدرت: {signal.get('strength', 0):.0f}%")
    print(f"دلیل: {signal.get('reason', '')}")

    stats = StatsManager()
    stats.save_signal(signal)

    ai_analysis = None
    if ZAI_API_KEY and signal['direction'] != 'WAIT':
        print("\n🤖 تحلیل AI...")
        ai_decision = AIDecisionMaker()
        ai_analysis = ai_decision.review_signal(signal)

    print("\n📱 ارسال به تلگرام...")
    msg = format_signal_message(signal, ai_analysis)
    if send_telegram_message(msg):
        print("✅ ارسال شد!")
    else:
        print("❌ خطا در ارسال")


if __name__ == "__main__":
    main()
