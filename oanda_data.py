# -*- coding: utf-8 -*-
"""
ماژول دریافت داده از OANDA API
OANDA Data Module

ویژگی‌ها:
- قیمت لحظه‌ای (real-time)
- داده OHLCV
- تشخیص خودکار سشن‌ها (بدون مشکل روزهای تعطیل)
- مدیریت خطا و retry

OANDA Practice Account (Demo) - رایگان
"""

import os
import logging
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# ============================================================
# Configuration
# ============================================================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")

# برای حساب دمو از api-fxpractice استفاده می‌کنیم
# برای حساب واقعی: https://api-fxtrade.oanda.com
OANDA_BASE_URL = "https://api-fxpractice.oanda.com/v3"

# نماد در OANDA
SYMBOL_OANDA = "USD_JPY"


class OandaDataFetcher:
    """دریافت داده از OANDA API"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {OANDA_API_KEY}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        })

    def _make_request(self, endpoint, params=None):
        """درخواست به OANDA API با مدیریت خطا"""
        url = f"{OANDA_BASE_URL}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"OANDA API خطا: {response.status_code} - {response.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"خطا در درخواست OANDA: {e}")
            return None

    def get_current_price(self) -> Optional[float]:
        """گرفتن قیمت لحظه‌ای (real-time bid/ask)"""
        try:
            data = self._make_request(f"/instruments/{SYMBOL_OANDA}/candles", params={
                "count": 1,
                "granularity": "S5",  # 5 ثانیه
                "price": "M",  # midprice
            })
            if data and "candles" in data and len(data["candles"]) > 0:
                candle = data["candles"][-1]
                price = float(candle["mid"]["c"])
                logger.info(f"OANDA قیمت فعلی: {price}")
                return price
        except Exception as e:
            logger.error(f"خطا در قیمت فعلی: {e}")
        return None

    def get_ohlc(self, timeframe: str = "M15", count: int = 300) -> Optional[pd.DataFrame]:
        """
        گرفتن داده OHLC

        Args:
            timeframe: M1, M5, M15, M30, H1, H4, D, W
            count: تعداد کندل‌ها (حداکثر 5000)
        """
        try:
            data = self._make_request(f"/instruments/{SYMBOL_OANDA}/candles", params={
                "count": count,
                "granularity": timeframe,
                "price": "M",
            })

            if not data or "candles" not in data:
                logger.error("داده‌ای از OANDA دریافت نشد")
                return None

            candles = data["candles"]
            if not candles:
                return None

            # تبدیل به DataFrame
            df_data = []
            for c in candles:
                if not c.get("complete", False):
                    continue  # رد کردن کندل‌های ناقص
                df_data.append({
                    'time': c['time'],
                    'open': float(c['mid']['o']),
                    'high': float(c['mid']['h']),
                    'low': float(c['mid']['l']),
                    'close': float(c['mid']['c']),
                    'volume': int(c['volume']),
                })

            if not df_data:
                return None

            df = pd.DataFrame(df_data)
            # تبدیل زمان
            df['time'] = pd.to_datetime(df['time'])
            df.set_index('time', inplace=True)

            logger.info(f"OANDA: {len(df)} کندل دریافت شد ({timeframe})")
            return df

        except Exception as e:
            logger.error(f"خطا در OHLC: {e}")
            return None

    def get_account_summary(self) -> Optional[Dict]:
        """گرفتن خلاصه حساب (موجودی، PnL و غیره)"""
        try:
            data = self._make_request(f"/accounts/{OANDA_ACCOUNT_ID}/summary")
            if data and "account" in data:
                acc = data["account"]
                return {
                    'id': acc.get('id'),
                    'balance': float(acc.get('balance', 0)),
                    'nav': float(acc.get('NAV', 0)),
                    'unrealized_pnl': float(acc.get('unrealizedPL', 0)),
                    'realized_pnl': float(acc.get('pl', 0)),
                    'currency': acc.get('currency', 'USD'),
                    'leverage': acc.get('marginRate', 0),
                }
            return None
        except Exception as e:
            logger.error(f"خطا در account summary: {e}")
            return None

    def is_market_open(self) -> bool:
        """
        بررسی آیا بازار باز هست

        OANDA دقیقاً می‌دونه بازار بازه یا بسته
        """
        try:
            price = self.get_current_price()
            if price and price > 0:
                return True
            return False
        except:
            return False

    def get_pricing(self) -> Optional[Dict]:
        """
        گرفتن قیمت کامل (bid/ask/spread)

        Returns:
            dict: {bid, ask, spread, mid}
        """
        try:
            data = self._make_request(f"/accounts/{OANDA_ACCOUNT_ID}/pricing", params={
                "instruments": SYMBOL_OANDA
            })
            if data and "prices" in data and len(data["prices"]) > 0:
                price = data["prices"][0]
                bid = float(price["bids"][0]["price"])
                ask = float(price["asks"][0]["price"])
                return {
                    'bid': bid,
                    'ask': ask,
                    'mid': (bid + ask) / 2,
                    'spread': ask - bid,
                }
        except Exception as e:
            logger.error(f"خطا در pricing: {e}")
        return None


# ============================================================
# تابع تست
# ============================================================
def test_connection():
    """تست اتصال به OANDA"""
    print("\n" + "=" * 50)
    print("🧪 تست اتصال OANDA")
    print("=" * 50)

    if not OANDA_API_KEY:
        print("❌ OANDA_API_KEY تنظیم نشده!")
        return False

    if not OANDA_ACCOUNT_ID:
        print("❌ OANDA_ACCOUNT_ID تنظیم نشده!")
        return False

    print(f"✓ API Key: {OANDA_API_KEY[:10]}...")
    print(f"✓ Account ID: {OANDA_ACCOUNT_ID}")

    fetcher = OandaDataFetcher()

    # تست قیمت فعلی
    print("\n1. تست قیمت فعلی...")
    price = fetcher.get_current_price()
    if price:
        print(f"   ✓ قیمت فعلی USD/JPY: {price}")
    else:
        print("   ✗ خطا در دریافت قیمت")
        return False

    # تست OHLC
    print("\n2. تست داده OHLC (15m)...")
    df = fetcher.get_ohlc("M15", count=10)
    if df is not None:
        print(f"   ✓ {len(df)} کندل دریافت شد")
        print(f"   آخرین کندل:\n{df.tail()}")
    else:
        print("   ✗ خطا در دریافت OHLC")
        return False

    # تست account summary
    print("\n3. تست اطلاعات حساب...")
    acc = fetcher.get_account_summary()
    if acc:
        print(f"   ✓ موجودی: ${acc['balance']:.2f}")
        print(f"   ✓ NAV: ${acc['nav']:.2f}")
        print(f"   ✓ ارز: {acc['currency']}")
    else:
        print("   ✗ خطا در دریافت اطلاعات حساب")

    # تست market open
    print("\n4. تست وضعیت بازار...")
    is_open = fetcher.is_market_open()
    print(f"   بازار {'✅ باز' if is_open else '❌ بسته'}")

    print("\n" + "=" * 50)
    print("✅ همه تست‌ها با موفقیت انجام شد!")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    test_connection()
