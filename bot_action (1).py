# -*- coding: utf-8 -*-
"""
ربات سیگنال‌دهی USD/JPY - نسخه v4 حرفه‌ای
USD/JPY Signal Bot - Professional v4

ویژگی‌های v4:
1. تشخیص سشن‌های معاملاتی (London + New York)
2. ورود Market/Limit بر اساس ستاپ
3. فیلترهای دقیق Liquidity Sweep
4. SL با buffer مناسب
5. ستاپ‌های بانکی: OB, FVG, BOS/CHoCH, Sweep, Breakout

ستاپ‌های قابل تشخیص:
- Continuation: BOS + Retest → Market
- Reversal: CHoCH + Sweep → Limit
- Range Scalp: Range Boundaries → Limit
- Breakout: Level Break + Volume → Market
- Pullback: Trend + Pullback to EMA → Limit

سشن‌ها (UTC):
- Asia: 00:00 - 07:00
- London: 07:00 - 16:00
- New York: 12:00 - 21:00
- London+NY Overlap: 12:00 - 16:00 (بهترین زمان ترید)
"""

import os
import sys
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

# ============================================================
# نصب وابستگی‌ها
# ============================================================
def install_dependencies():
    try:
        import yfinance
        import pandas
        import numpy
        import requests
        from dotenv import load_dotenv
        return True
    except ImportError:
        deps = ["yfinance", "pandas", "numpy", "requests", "python-dotenv"]
        for dep in deps:
            os.system(f'pip install {dep} --quiet 2>&1')
            os.system(f'pip3 install {dep} --quiet 2>&1')
            os.system(f'python -m pip install {dep} --quiet 2>&1')
        return True

install_dependencies()

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
    from dotenv import load_dotenv
except ImportError as e:
    print(f"❌ خطا: {e}")
    sys.exit(1)

load_dotenv()

# ============================================================
# Configuration
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
AI_MODEL = "glm-4.5"

SYMBOL = "JPY=X"
SYMBOL_DISPLAY = "USD/JPY"

TF_HTF = "1d"
TF_PATTERN = "1h"
TF_TRIGGER = "15m"

# پارامترهای اندیکاتور
EMA_FAST = 21
EMA_MID = 50
EMA_SLOW = 200
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 2.0  # افزایش از 1.5 به 2.0 برای SL منطقی‌تر
ATR_TP_MULTIPLIER = 6.0   # ریسک/ریوارد 1:3

# فاصله‌های دقیق (درصد)
ZONE_PROXIMITY_PCT = 0.15   # 0.15% نزدیک ناحیه = ورود Market
RANGE_BOUNDARY_PCT = 0.20   # 0.20% نزدیک سقف/کف رنج
SWEEP_THRESHOLD_PCT = 0.10  # 0.10% شکست برای Liquidity Sweep
BREAKOUT_THRESHOLD_PCT = 0.15  # 0.15% شکست برای Breakout

# سشن‌های معاملاتی (UTC) - ساعت شروع و پایان
SESSIONS = {
    'asia': {'start': 0, 'end': 7, 'name': ' آسیا 🌏'},
    'london': {'start': 7, 'end': 16, 'name': ' لندن 🇬🇧'},
    'new_york': {'start': 12, 'end': 21, 'name': ' نیویورک 🇺🇸'},
    'overlap': {'start': 12, 'end': 16, 'name': ' همپوشانی لندن+نیویورک 🔥'},
}

# آستانه‌های سیگنال
MIN_SIGNAL_STRENGTH = 60
STRONG_SIGNAL_STRENGTH = 75

SIGNALS_DB_FILE = "signals_db.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================
# Session Detector
# ============================================================
class SessionDetector:
    """تشخیص سشن معاملاتی فعلی - با تشخیص روزهای تعطیل"""

    def get_current_session(self):
        try:
            # زمان فعلی UTC
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
            weekday = now_utc.weekday()  # 0=دوشنبه، 5=شنبه، 6=یکشنبه

            # ============================================
            # فیلتر روزهای تعطیل فارکس
            # فارکس: دوشنبه 00:00 تا جمعه 22:00 UTC (برای بروکرهای استاندارد)
            # شنبه و یکشنبه: بسته
            # جمعه بعد از 22:00 UTC: بسته
            # دوشنبه قبل از 00:00 UTC: بسته (هنوز باز نشده)
            # ============================================
            is_closed_day = False
            close_reason = ""

            if weekday == 5:  # شنبه
                is_closed_day = True
                close_reason = "شنبه - بازار تعطیل"
            elif weekday == 6:  # یکشنبه
                is_closed_day = True
                close_reason = "یکشنبه - بازار تعطیل"
            elif weekday == 4 and hour >= 22:  # جمعه بعد از 22:00 UTC
                is_closed_day = True
                close_reason = "جمعه شب - بازار بسته شد"

            if is_closed_day:
                return {
                    'name': f'بسته 🌙 ({close_reason})',
                    'is_active': False,
                    'is_best_time': False,
                    'description': f'بازار بسته - {close_reason}',
                    'weekday': weekday,
                    'hour_utc': hour,
                }

            # ============================================
            # بررسی سشن‌های فعال (فقط در روزهای کاری)
            # ============================================
            sessions = []

            # همپوشانی (بهترین زمان)
            if SESSIONS['overlap']['start'] <= hour < SESSIONS['overlap']['end']:
                sessions.append('overlap')

            # لندن
            if SESSIONS['london']['start'] <= hour < SESSIONS['london']['end']:
                sessions.append('london')

            # نیویورک
            if SESSIONS['new_york']['start'] <= hour < SESSIONS['new_york']['end']:
                sessions.append('new_york')

            # آسیا
            if SESSIONS['asia']['start'] <= hour < SESSIONS['asia']['end']:
                sessions.append('asia')

            if not sessions:
                return {
                    'name': 'بسته 🌙',
                    'is_active': False,
                    'is_best_time': False,
                    'description': 'بازار بسته - ترید توصیه نمی‌شه',
                    'weekday': weekday,
                    'hour_utc': hour,
                }

            # اولویت: overlap > london > new_york > asia
            if 'overlap' in sessions:
                primary = 'overlap'
            elif 'london' in sessions:
                primary = 'london'
            elif 'new_york' in sessions:
                primary = 'new_york'
            else:
                primary = 'asia'

            is_best = primary in ['overlap', 'london', 'new_york']

            return {
                'name': SESSIONS[primary]['name'],
                'is_active': True,
                'is_best_time': is_best,
                'primary': primary,
                'all_active': sessions,
                'weekday': weekday,
                'hour_utc': hour,
                'description': f"سشن {SESSIONS[primary]['name']} (UTC: {hour}:00)",
            }
        except Exception as e:
            return {'name': 'نامشخص', 'is_active': True, 'is_best_time': True, 'description': f'خطا: {e}'}


# ============================================================
# Data Fetcher (ترکیبی: OANDA + Yahoo Finance fallback)
# ============================================================
class DataFetcher:
    """دریافت داده از OANDA (اصلی) با fallback به Yahoo Finance"""

    def __init__(self):
        self._cache = {}
        self._cache_time = {}
        self.oanda_fetcher = None
        self.yahoo_ticker = None

        # تلاش برای اتصال به OANDA
        try:
            from oanda_data import OandaDataFetcher
            if OANDA_API_KEY and OANDA_ACCOUNT_ID:
                self.oanda_fetcher = OandaDataFetcher()
                logger.info("✓ OANDA متصل شد")
        except ImportError:
            logger.warning("oanda_data.py موجود نیست - استفاده از Yahoo Finance")

        # Yahoo Finance به‌عنوان fallback
        try:
            self.yahoo_ticker = yf.Ticker(SYMBOL)
        except:
            pass

    def fetch_closed_candles(self, timeframe, count=300):
        """فقط کندل‌های بسته‌شده"""
        import time as t
        cache_key = f"{timeframe}_{count}"
        if cache_key in self._cache and cache_key in self._cache_time:
            if t.time() - self._cache_time[cache_key] < 300:
                return self._cache[cache_key]

        # ============================================
        # روش 1: تلاش با OANDA (اولویت اول)
        # ============================================
        if self.oanda_fetcher:
            try:
                # تبدیل تایم‌فریم به فرمت OANDA
                tf_map = {
                    "15m": "M15", "1h": "H1", "60m": "H1",
                    "4h": "H4", "1d": "D", "1wk": "W"
                }
                oanda_tf = tf_map.get(timeframe, "M15")
                df = self.oanda_fetcher.get_ohlc(oanda_tf, count=count)

                if df is not None and not df.empty:
                    # تبدیل index به ستون time (مثل Yahoo)
                    df = df.reset_index()
                    if 'time' in df.columns:
                        df['datetime'] = pd.to_datetime(df['time'])
                        df = df.set_index('datetime')
                        df = df.drop('time', axis=1, errors='ignore')
                    # اطمینان از ستون‌ها
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        if col not in df.columns:
                            df[col] = 0

                    self._cache[cache_key] = df
                    self._cache_time[cache_key] = t.time()
                    logger.info(f"✓ داده از OANDA دریافت شد: {len(df)} کندل ({timeframe})")
                    return df
                else:
                    logger.warning("OANDA داده خالی برگردوند، fallback به Yahoo")
            except Exception as e:
                logger.warning(f"خطا در OANDA، fallback به Yahoo: {e}")

        # ============================================
        # روش 2: Fallback به Yahoo Finance
        # ============================================
        try:
            period_map = {"15m": "60d", "1h": "730d", "60m": "730d", "4h": "60d", "1d": "2y", "1wk": "5y"}
            period = period_map.get(timeframe, "60d")
            df = self.yahoo_ticker.history(period=period, interval=timeframe, auto_adjust=False, prepost=False)
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
            df = df.iloc[:-1].copy()  # حذف کندل در حال تشکیل

            self._cache[cache_key] = df
            self._cache_time[cache_key] = t.time()
            logger.info(f"✓ داده از Yahoo دریافت شد: {len(df)} کندل ({timeframe})")
            return df
        except Exception as e:
            logger.error(f"خطا در Yahoo Finance: {e}")
            raise

    def get_current_price(self):
        """قیمت لحظه‌ای - اولویت با OANDA"""
        # OANDA
        if self.oanda_fetcher:
            try:
                price = self.oanda_fetcher.get_current_price()
                if price and price > 0:
                    return price
            except:
                pass

        # Yahoo fallback
        try:
            df = self.yahoo_ticker.history(period="1d", interval="1m")
            if not df.empty:
                return float(df['Close'].iloc[-1])
        except:
            pass
        return None


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
                # محاسبه جایگاه قیمت در رنج
                range_size = range_high - range_low
                if range_size > 0:
                    position_in_range = (price - range_low) / range_size * 100
                else:
                    position_in_range = 50

                return {
                    'trend': 'range',
                    'strength': 50,
                    'description': f"بازار رنج ({range_low:.2f} - {range_high:.2f})",
                    'range_high': range_high,
                    'range_low': range_low,
                    'range_size': range_size,
                    'position_in_range': position_in_range,
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
            pullback = self._detect_pullback(df)

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
                'pullback': pullback,
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

                if cp > last_sh['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(structure='bullish', bos='bullish', type='continuation', description='BOS صعودی - ادامه روند صعودی')
                elif cp < last_sl['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(structure='bearish', bos='bearish', type='continuation', description='BOS نزولی - ادامه روند نزولی')
                elif cp > last_sh['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(structure='bullish', choch='bullish', type='reversal', description='CHoCH صعودی - تغییر روند به صعودی')
                elif cp < last_sl['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(structure='bearish', choch='bearish', type='reversal', description='CHoCH نزولی - تغییر روند به نزولی')
        except: pass
        return result

    def _find_liquidity_sweep(self, df, lookback=30):
        """Liquidity Sweep - شکست فیک با فیلتر دقیق"""
        try:
            recent = df.tail(lookback).copy()
            if len(recent) < 10:
                return {'detected': False}

            # سطح مهم اخیر (5 کندل قبل)
            recent_high = recent['high'].iloc[:-3].max()
            recent_low = recent['low'].iloc[:-3].min()
            cp = recent['close'].iloc[-1]

            # محاسبه فاصله
            high_dist_pct = abs(recent['high'].iloc[-1] - recent_high) / recent_high * 100
            low_dist_pct = abs(recent['low'].iloc[-1] - recent_low) / recent_low * 100

            # Bullish Sweep: قیمت کف رو شکسته (با حداقل فاصله) ولی بسته‌شده بالاتر
            if (recent['low'].iloc[-1] < recent_low and
                low_dist_pct > 0.05 and  # حداقل شکست 0.05%
                cp > recent_low):
                return {'detected': True, 'type': 'bullish', 'description': f'Liquidity Sweep صعودی - شکست فیک کف ({recent_low:.3f})', 'level': recent_low}

            # Bearish Sweep: قیمت سقف رو شکسته ولی بسته‌شده پایین‌تر
            if (recent['high'].iloc[-1] > recent_high and
                high_dist_pct > 0.05 and
                cp < recent_high):
                return {'detected': True, 'type': 'bearish', 'description': f'Liquidity Sweep نزولی - شکست فیک سقف ({recent_high:.3f})', 'level': recent_high}

            return {'detected': False}
        except:
            return {'detected': False}

    def _detect_breakout(self, df, lookback=20):
        """Breakout با فیلتر دقیق"""
        try:
            recent = df.tail(lookback).copy()
            if len(recent) < 10:
                return {'detected': False}

            resistance = recent['high'].iloc[:-3].max()
            support = recent['low'].iloc[:-3].min()
            cp = recent['close'].iloc[-1]
            prev_cp = recent['close'].iloc[-2]

            # فاصله شکست
            breakout_dist = abs(cp - resistance) / resistance * 100 if cp > resistance else abs(cp - support) / support * 100

            # Breakout صعودی
            if (cp > resistance and
                prev_cp <= resistance and
                breakout_dist > BREAKOUT_THRESHOLD_PCT / 2):
                return {'detected': True, 'type': 'bullish', 'description': f'Breakout صعودی - شکست {resistance:.3f}', 'level': resistance}

            # Breakout نزولی
            if (cp < support and
                prev_cp >= support and
                breakout_dist > BREAKOUT_THRESHOLD_PCT / 2):
                return {'detected': True, 'type': 'bearish', 'description': f'Breakout نزولی - شکست {support:.3f}', 'level': support}

            return {'detected': False}
        except:
            return {'detected': False}

    def _detect_pullback(self, df, lookback=50):
        """تشخیص Pullback به EMA در روند"""
        try:
            recent = df.tail(lookback).copy()
            if len(recent) < EMA_SLOW:
                return {'detected': False}

            ema21 = recent['close'].ewm(span=EMA_FAST, adjust=False).mean()
            ema50 = recent['close'].ewm(span=EMA_MID, adjust=False).mean()
            ema200 = recent['close'].ewm(span=EMA_SLOW, adjust=False).mean()

            cp = recent['close'].iloc[-1]

            # روند صعودی: EMA50 > EMA200
            if ema50.iloc[-1] > ema200.iloc[-1]:
                # Pullback به EMA21 یا EMA50
                dist_to_ema21 = abs(cp - ema21.iloc[-1]) / cp * 100
                dist_to_ema50 = abs(cp - ema50.iloc[-1]) / cp * 100

                if dist_to_ema21 < 0.1:
                    return {'detected': True, 'type': 'bullish', 'description': f'Pullback به EMA21 ({ema21.iloc[-1]:.3f})', 'level': ema21.iloc[-1]}
                if dist_to_ema50 < 0.15:
                    return {'detected': True, 'type': 'bullish', 'description': f'Pullback به EMA50 ({ema50.iloc[-1]:.3f})', 'level': ema50.iloc[-1]}

            # روند نزولی: EMA50 < EMA200
            if ema50.iloc[-1] < ema200.iloc[-1]:
                dist_to_ema21 = abs(cp - ema21.iloc[-1]) / cp * 100
                dist_to_ema50 = abs(cp - ema50.iloc[-1]) / cp * 100

                if dist_to_ema21 < 0.1:
                    return {'detected': True, 'type': 'bearish', 'description': f'Pullback به EMA21 ({ema21.iloc[-1]:.3f})', 'level': ema21.iloc[-1]}
                if dist_to_ema50 < 0.15:
                    return {'detected': True, 'type': 'bearish', 'description': f'Pullback به EMA50 ({ema50.iloc[-1]:.3f})', 'level': ema50.iloc[-1]}

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
            momentum = self._calc_momentum(df)

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

            if momentum > 0:
                bull_score += min(0.5, abs(momentum) * 0.1)
            elif momentum < 0:
                bear_score += min(0.5, abs(momentum) * 0.1)

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
                'momentum': momentum,
                'direction': direction,
                'strength': strength,
                'current_price': cp,
                'description': f"تریگر: {direction} (RSI: {rsi:.0f}, Mom: {momentum:.1f}, {len(patterns)} الگو)",
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

    def _calc_momentum(self, df):
        """محاسبه مومنتوم (تغییرات 5 کندل)"""
        try:
            if len(df) < 5:
                return 0
            cp = df['close'].iloc[-1]
            past = df['close'].iloc[-5]
            return float((cp - past) / past * 1000)  # ضریب 1000 برای مقیاس
        except:
            return 0


# ============================================================
# Signal Engine v4 - منطق هوشمند و تهاجمی
# ============================================================
class SignalEngine:
    """
    موتور سیگنال v4

    ستاپ‌های قابل تشخیص:
    1. Breakout + Continuation → MARKET
    2. Liquidity Sweep + Reversal → MARKET
    3. BOS + Momentum → MARKET
    4. CHoCH + Zone → LIMIT
    5. Pullback to EMA in Trend → LIMIT
    6. Range Scalp → LIMIT at boundaries
    """

    def __init__(self):
        self.fetcher = DataFetcher()
        self.session = SessionDetector()
        self.context = MarketContextAnalyzer()
        self.pattern = PatternAnalyzer()
        self.trigger = TriggerAnalyzer()

    def analyze(self):
        try:
            print("\n📊 سشن معاملاتی...")
            session = self.session.get_current_session()
            print(f"   {session['description']}")

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
            struct_type = structure.get('type', 'none')
            struct_dir = 'bullish' if structure.get('structure') == 'bullish' else 'bearish' if structure.get('structure') == 'bearish' else 'neutral'

            rsi = trigger.get('rsi', 50)
            momentum = trigger.get('momentum', 0)
            breakout = pattern.get('breakout', {})
            liquidity = pattern.get('liquidity_sweep', {})
            pullback = pattern.get('pullback', {})

            # ============================================
            # تصمیم‌گیری هوشمند - اولویت‌بندی ستاپ‌ها
            # ============================================

            direction = 'WAIT'
            entry_type = None
            entry_price = cp
            entry_reason = ""
            strength = 0
            setup_type = ""

            # ============================================
            # ستاپ 1: Breakout (قوی‌ترین) - MARKET
            # ============================================
            if breakout.get('detected'):
                direction = 'BUY' if breakout['type'] == 'bullish' else 'SELL'
                entry_type = 'market'
                entry_price = cp
                entry_reason = f"🔥 Breakout {breakout['type']} - {breakout.get('description', '')}"
                strength = 85
                setup_type = "Breakout"
                print(f"   ✓ Breakout Setup: {direction}")

            # ============================================
            # ستاپ 2: Liquidity Sweep + Reversal - MARKET
            # ============================================
            elif liquidity.get('detected'):
                sweep_dir = liquidity.get('type')
                # تأیید با RSI
                if sweep_dir == 'bullish' and rsi < 60:
                    direction = 'BUY'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"🔥 Sweep صعودی + RSI {rsi:.0f} - {liquidity.get('description', '')}"
                    strength = 80
                    setup_type = "Liquidity Sweep"
                    print(f"   ✓ Sweep Setup: {direction}")
                elif sweep_dir == 'bearish' and rsi > 40:
                    direction = 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"🔥 Sweep نزولی + RSI {rsi:.0f} - {liquidity.get('description', '')}"
                    strength = 80
                    setup_type = "Liquidity Sweep"
                    print(f"   ✓ Sweep Setup: {direction}")
                else:
                    # sweep هست اما RSI تأیید نمی‌کنه
                    direction = 'WAIT'
                    entry_reason = f"Sweep {sweep_dir} اما RSI {rsi:.0f} تأیید نمی‌کنه"

            # ============================================
            # ستاپ 3: BOS + Momentum (Continuation) - MARKET
            # ============================================
            elif struct_type == 'continuation' and struct_dir != 'neutral':
                if struct_dir == 'bullish' and rsi > 50 and momentum > 0:
                    direction = 'BUY'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"🚀 BOS صعودی + Momentum ({momentum:.1f}) - ادامه روند صعودی"
                    strength = 75
                    setup_type = "Continuation"
                    print(f"   ✓ Continuation BUY: {direction}")
                elif struct_dir == 'bearish' and rsi < 50 and momentum < 0:
                    direction = 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"🚀 BOS نزولی + Momentum ({momentum:.1f}) - ادامه روند نزولی"
                    strength = 75
                    setup_type = "Continuation"
                    print(f"   ✓ Continuation SELL: {direction}")
                else:
                    direction = 'WAIT'
                    entry_reason = f"BOS {struct_dir} اما مومنتوم ضعیف (RSI: {rsi:.0f}, Mom: {momentum:.1f})"

            # ============================================
            # ستاپ 4: CHoCH (Reversal) - LIMIT
            # ============================================
            elif struct_type == 'reversal' and struct_dir != 'neutral':
                zones = pattern.get('all_zones', [])
                if struct_dir == 'bullish':
                    suitable = [z for z in zones if z['type'] == 'demand' and z['price'] < cp]
                else:
                    suitable = [z for z in zones if z['type'] == 'supply' and z['price'] > cp]

                if suitable:
                    nearest = min(suitable, key=lambda x: abs(x['price'] - cp))
                    distance_pct = abs(nearest['price'] - cp) / cp * 100

                    direction = 'BUY' if struct_dir == 'bullish' else 'SELL'
                    entry_type = 'limit'
                    entry_price = nearest['price']
                    entry_reason = f"🔄 CHoCH {struct_dir} - Limit در {nearest['source']} ({distance_pct:.2f}%)"
                    strength = 70
                    setup_type = "Reversal"
                    print(f"   ✓ Reversal {direction} (Limit at {entry_price:.3f})")
                else:
                    # ناحیه مناسب نیست - Market
                    direction = 'BUY' if struct_dir == 'bullish' else 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"🔄 CHoCH {struct_dir} - ورود Market (ناحیه مناسب نیست)"
                    strength = 65
                    setup_type = "Reversal"

            # ============================================
            # ستاپ 5: Pullback به EMA در روند - LIMIT
            # ============================================
            elif pullback.get('detected'):
                direction = 'BUY' if pullback['type'] == 'bullish' else 'SELL'
                entry_type = 'market'  # چون قیمت الان نزدیک EMA هست
                entry_price = cp
                entry_reason = f"📊 Pullback Setup - {pullback.get('description', '')}"
                strength = 70
                setup_type = "Pullback"
                print(f"   ✓ Pullback Setup: {direction}")

            # ============================================
            # ستاپ 6: Range Scalp - LIMIT
            # ============================================
            elif context.get('is_range'):
                range_high = context.get('range_high', 0)
                range_low = context.get('range_low', 0)
                position = context.get('position_in_range', 50)

                # نزدیک کف رنج - BUY
                if position < 20:
                    direction = 'BUY'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"📊 Range Scalp - نزدیک کف رنج (موقعیت: {position:.0f}%)"
                    strength = 65
                    setup_type = "Range Scalp"
                    print(f"   ✓ Range BUY (near low)")
                # نزدیک سقف رنج - SELL
                elif position > 80:
                    direction = 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"📊 Range Scalp - نزدیک سقف رنج (موقعیت: {position:.0f}%)"
                    strength = 65
                    setup_type = "Range Scalp"
                    print(f"   ✓ Range SELL (near high)")
                # در وسط رنج - صبر کن
                else:
                    direction = 'WAIT'
                    entry_reason = f"در وسط رنج (موقعیت: {position:.0f}%) - ترید نمی‌شه"

            # ============================================
            # ستاپ 7: Trigger قوی (fallback)
            # ============================================
            else:
                trig_dir = trigger.get('direction', 'neutral')
                trig_strength = trigger.get('strength', 0)
                if trig_strength >= MIN_SIGNAL_STRENGTH and trig_dir != 'neutral':
                    direction = 'BUY' if trig_dir == 'bullish' else 'SELL'
                    entry_type = 'market'
                    entry_price = cp
                    entry_reason = f"⚡ تریگر قوی {trig_dir} (قدرت: {trig_strength:.0f}%)"
                    strength = trig_strength
                    setup_type = "Trigger"
                    print(f"   ✓ Trigger-only: {direction}")
                else:
                    direction = 'WAIT'
                    entry_reason = "هیچ ستاپ معتبری پیدا نشد"

            # ============================================
            # فیلتر سشن: اگه بازار بسته باشه، فقط WAIT
            # ============================================
            if not session.get('is_active', True) and direction != 'WAIT':
                direction = 'WAIT'
                entry_reason = f"بازار بسته - {entry_reason}"

            # ============================================
            # فیلتر تأیید جهت (منطق هوشمند)
            # اگه تریگر قوی و برخلاف سیگنال باشه → ریورسال به جهت تریگر
            # ============================================
            if direction != 'WAIT':
                trig_dir = trigger.get('direction', 'neutral')
                trig_strength = trigger.get('strength', 0)

                # تبدیل جهت سیگنال به bullish/bearish
                signal_dir = 'bullish' if direction == 'BUY' else 'bearish'

                # بررسی تناقض
                if trig_dir != 'neutral' and trig_dir != signal_dir:
                    # تریگر برخلاف سیگنال هست
                    # اگه تریگر قوی باشه (≥60%)، سیگنال رو به جهت تریگر تغییر بده (ریورسال)
                    if trig_strength >= 60:
                        old_direction = direction
                        direction = 'BUY' if trig_dir == 'bullish' else 'SELL'
                        entry_type = 'market'
                        entry_price = cp
                        # قدرت جدید بر اساس قدرت تریگر
                        strength = max(65, trig_strength)
                        entry_reason = f"🔄 ریورسال: تریگر قوی {trig_dir} ({trig_strength:.0f}%) - سیگنال از {old_direction} به {direction} تغییر کرد"
                        setup_type = "Reversal (Trigger)"
                        print(f"   🔄 Reversal: {old_direction} → {direction} (trigger {trig_dir} {trig_strength:.0f}%)")
                    # اگه تریگر ضعیف باشه، سیگنال رو نگه دار ولی هشدار بده
                    else:
                        strength = max(40, strength - 15)
                        entry_reason += f" | ⚠️ تریگر برخلاف ({trig_dir} ضعیف)"
                        print(f"   ⚠️ Weak conflict: trigger={trig_dir} ({trig_strength:.0f}%)")

            # ============================================
            # محاسبه SL/TP با buffer مناسب
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

            # اطمینان
            confidence = 'high' if strength >= STRONG_SIGNAL_STRENGTH else 'medium' if strength >= MIN_SIGNAL_STRENGTH else 'low'

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
                'session': session,
                'context': context,
                'pattern': pattern,
                'trigger': trigger,
                'setup_type': setup_type,
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
            session = signal.get('session', {})

            prompt = f"""
شما یک تریدر حرفه‌ای بانک هستید. این سیگنال USD/JPY را بررسی کنید:

سیگنال: {signal['direction']} (قدرت: {signal['strength']:.0f}%)
ستاپ: {signal.get('setup_type', 'نامشخص')}
نوع ورود: {signal.get('entry_type', 'نامشخص')}
قیمت فعلی: {signal.get('current_price', 0):.3f}

سشن: {session.get('description', '')}
Context: {context.get('description', '')}
Pattern: {pattern.get('description', '')}
Trigger: {trigger.get('description', '')}

دلیل: {signal.get('reason', '')}

به فارسی و مختصر (حداکثر 100 کلمه):
1. آیا موافقی؟
2. نقاط قوت/ضعف
3. توصیه نهایی
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
                'setup': signal.get('setup_type'),
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

    msg = f"{icon} سیگنال {dir_fa} USD/JPY (v4)\n"
    msg += f"{'='*30}\n\n"

    # سشن
    session = signal.get('session', {})
    msg += f"🌐 سشن: {session.get('name', 'نامشخص')}\n"
    if session.get('is_best_time'):
        msg += "🔥 زمان طلایی ترید!\n"

    msg += f"\n💰 قیمت فعلی: {signal.get('current_price', 0):.3f}\n"
    if direction in ['BUY', 'SELL']:
        entry_type = signal.get('entry_type', 'market')
        entry_icon = "⚡" if entry_type == 'market' else "📋"
        msg += f"{entry_icon} نوع ورود: {entry_type.upper()}\n"
        msg += f"🔹 نقطه ورود: {signal.get('entry_price', 0):.3f}\n"
        setup = signal.get('setup_type', '')
        if setup:
            msg += f"🎯 ستاپ: {setup}\n"
    msg += f"💪 قدرت: {signal.get('strength', 0):.0f}%\n"
    msg += f"{conf_icon} اطمینان: {signal.get('confidence', 'low')}\n"
    msg += f"🕐 زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    # Context
    context = signal.get('context', {})
    msg += "📊 Context (Daily):\n"
    msg += "-"*30 + "\n"
    msg += f"• {context.get('description', 'نامشخص')}\n"
    if context.get('is_range'):
        msg += f"• موقعیت در رنج: {context.get('position_in_range', 0):.0f}%\n"
    msg += "\n"

    # Pattern
    pattern = signal.get('pattern', {})
    msg += "📊 Pattern (1H):\n"
    msg += "-"*30 + "\n"
    msg += f"• {pattern.get('description', 'نامشخص')}\n"
    structure = pattern.get('structure', {})
    msg += f"• ساختار: {structure.get('description', 'نامشخص')}\n"
    breakout = pattern.get('breakout', {})
    if breakout.get('detected'):
        msg += f"• 🔥 {breakout.get('description', '')}\n"
    liq = pattern.get('liquidity_sweep', {})
    if liq.get('detected'):
        msg += f"• 🔥 {liq.get('description', '')}\n"
    pullback = pattern.get('pullback', {})
    if pullback.get('detected'):
        msg += f"• 📊 {pullback.get('description', '')}\n"
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
            sl_pips = abs(signal.get('entry_price', 0) - signal['stop_loss']) * 100
            msg += f"🛑 حد ضرر: {signal['stop_loss']:.3f} ({sl_pips:.1f} پیپ)\n"
        if signal.get('tp1'):
            msg += f"🎯 TP1: {signal['tp1']:.3f} (+{abs(signal.get('entry_price', 0) - signal['tp1'])*100:.1f} پیپ)\n"
        if signal.get('tp2'):
            msg += f"🎯 TP2: {signal['tp2']:.3f} (+{abs(signal.get('entry_price', 0) - signal['tp2'])*100:.1f} پیپ)\n"
        if signal.get('tp3'):
            msg += f"🎯 TP3: {signal['tp3']:.3f} (+{abs(signal.get('entry_price', 0) - signal['tp3'])*100:.1f} پیپ)\n"
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
    print("🤖 ربات سیگنال USD/JPY - v4 حرفه‌ای")
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
    print(f"ستاپ: {signal.get('setup_type', 'نامشخص')}")
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

    # ============================================
    # مانیتورینگ پوزیشن و کارنامه هفتگی
    # کد جداگانه در position_tracker.py - فراخوانی مستقیم با try/except
    # اگه خراب شه، ربات اصلی کار می‌کنه
    # ============================================
    try:
        print("\n📊 مانیتورینگ پوزیشن...")
        from position_tracker import track_signal

        result = track_signal(signal, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

        if result.get('success'):
            print(f"  ✓ مانیتورینگ کامل شد")
            print(f"  💳 موجودی: ${result.get('balance', 0):.2f}")
            print(f"  📂 پوزیشن‌های باز: {result.get('open_positions', 0)}")
            print(f"  📨 پیام‌های ارسالی: {result.get('messages_sent', 0)}")
            if result.get('report_sent'):
                print(f"  📊 کارنامه هفتگی ارسال شد!")
        else:
            print(f"  ⚠️ خطا: {result.get('error', 'نامشخص')}")

    except ImportError:
        print("  ⚠️ position_tracker.py موجود نیست - آپلودش کنی!")
    except Exception as e:
        print(f"  ⚠️ خطا در مانیتورینگ (ربات اصلی ادامه می‌ده): {e}")


if __name__ == "__main__":
    main()
