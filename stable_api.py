import time
import logging
import asyncio
import json
import os
from datetime import datetime
from . import expiration
from . import global_value
from .api import QuotexAPI
from .utils.services import truncate
from .utils.processor import (
    calculate_candles,
    process_candles_v2,
    merge_candles,
    process_tick
)
from .config import (
    load_session,
    update_session,
    resource_path,
    credentials
)
from .utils.indicators import TechnicalIndicators

logger = logging.getLogger(__name__)

# 📁 ملفات السجل
TRADES_LOG_FILE = "trades_log.json"
FIB_LEVELS = [0.2, 0.38, 0.5, 0.62, 0.8, 0.9, 1.0]

class Quotex:
    def __init__(
            self,
            email=None,
            password=None,
            lang="pt",
            user_agent="Quotex/1.0",
            root_path=".",
            user_data_dir="browser",
            asset_default="EURUSD",
            period_default=60
    ):
        self.size = [5, 10]
        self.email = email
        self.password = password
        self.lang = lang
        self.resource_path = root_path
        self.user_data_dir = user_data_dir
        self.asset_default = asset_default
        self.period_default = period_default
        self.subscribe_candle = []
        self.subscribe_candle_all_size = []
        self.subscribe_mood = []
        self.account_is_demo = 1
        self.suspend = 0.2
        self.codes_asset = {}
        self.api = None
        self.duration = None
        self.websocket_client = None
        self.websocket_thread = None
        self.debug_ws_enable = False
        self.resource_path = resource_path(root_path)
        session = load_session(user_agent)
        self.session_data = session
        if not email or not password:
            self.email, self.password = credentials()

        # ✅ تحميل سجل الصفقات
        self.trades_log = self.load_trades_log()

    # ─────────────────────────────────────────────────────
    # 📂 إدارة سجل الصفقات
    # ─────────────────────────────────────────────────────
    def load_trades_log(self):
        """تحميل سجل الصفقات من الملف"""
        if os.path.exists(TRADES_LOG_FILE):
            try:
                with open(TRADES_LOG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return []
        return []

    def save_trades_log(self):
        """حفظ سجل الصفقات في الملف"""
        try:
            with open(TRADES_LOG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.trades_log, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"❌ فشل حفظ سجل الصفقات: {str(e)}")

    def log_trade(self, asset, direction, entry_price, fib_level, result, profit, duration=900):
        """تسجيل تفاصيل الصفقة"""
        trade = {
            "timestamp": datetime.now().isoformat(),
            "asset": asset,
            "direction": direction,
            "entry_price": entry_price,
            "fib_level": fib_level,
            "result": result,
            "profit": profit,
            "duration_seconds": duration
        }
        self.trades_log.append(trade)
        self.save_trades_log()

    # ─────────────────────────────────────────────────────
    # 📊 استراتيجية فيبوناتشي 0.62 + تأكيد الشمعة
    # ─────────────────────────────────────────────────────
    async def detect_fibonacci_62_signal(self, asset: str, timeframe_seconds: int = 300, expiry_seconds: int = 900):
        """
        استراتيجية: فتح صفقة عند إعادة اختبار مستوى 0.62 فيبوناتشي مع تأكيد شمعة.
        """
        try:
            # 1. جلب الشموع (5 دقائق)
            candles = await self.get_candles(asset, time.time(), 100, timeframe_seconds)
            if not candles or len(candles) < 20:
                return None, None, None

            # 2. تحويل إلى DataFrame
            df = pd.DataFrame(candles)
            df = df[['time', 'open', 'high', 'low', 'close']].copy()
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)

            for col in ['open', 'high', 'low', 'close']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df.dropna(inplace=True)

            if len(df) < 10:
                return None, None, None

            # 3. تحديد Swing High و Swing Low (مثل ZigZag مبسط)
            swing_high = df['high'].max()
            swing_low = df['low'].min()

            # 4. حساب مستويات فيبوناتشي
            diff = swing_high - swing_low
            fib_levels = {level: swing_low + diff * level for level in FIB_LEVELS}
            fib_62 = fib_levels[0.62]

            # 5. الشمعة الأخيرة
            last_candle = df.iloc[-1]
            prev_candle = df.iloc[-2]  # للتحقق من التأكيد

            # 6. التحقق من الحركة
            body = abs(last_candle['close'] - last_candle['open'])
            lower_wick = min(last_candle['open'], last_candle['close']) - last_candle['low']
            upper_wick = last_candle['high'] - max(last_candle['open'], last_candle['close'])

            # 7. ⚠ لا تدخل إذا تجاوز 0.62 بدون تأكيد
            if last_candle['high'] > fib_62 and last_candle['low'] > fib_62:
                return None, None, None  # تجاوز مباشر لأعلى
            if last_candle['low'] < fib_62 and last_candle['high'] < fib_62:
                return None, None, None  # تجاوز مباشر لأسفل

            # 8. تحديد الاتجاه العام (آخر 5 شموع)
            recent_closes = df['close'].tail(5)
            price_change = recent_closes.iloc[-1] - recent_closes.iloc[0]
            trend = 'up' if price_change > 0 else 'down'

            # 9. ✅ شرط الشراء (CALL): إعادة اختبار 0.62 + تأكيد صعودي
            if (trend == 'up' and
                last_candle['low'] <= fib_62 <= prev_candle['high'] and
                last_candle['close'] > last_candle['open'] and
                lower_wick > body):
                return "call", expiry_seconds, fib_62

            # 10. ✅ شرط البيع (PUT): إعادة اختبار 0.62 + تأكيد هبوطي
            elif (trend == 'down' and
                  last_candle['high'] >= fib_62 >= prev_candle['low'] and
                  last_candle['close'] < last_candle['open'] and
                  upper_wick > body):
                return "put", expiry_seconds, fib_62

            return None, None, None

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل فيبوناتشي 0.62: {str(e)}")
            return None, None, None

    # ─────────────────────────────────────────────────────
    # 🔧 الدوال الأصلية (بقيت كما هي)
    # ─────────────────────────────────────────────────────
    @property
    def websocket(self):
        return self.websocket_client.wss

    @staticmethod
    async def check_connect():
        await asyncio.sleep(2)
        if global_value.check_accepted_connection == 1:
            return True
        return False

    def set_session(self, user_agent: str, cookies: str = None, ssid: str = None):
        session = {
            "cookies": cookies,
            "token": ssid,
            "user_agent": user_agent
        }
        self.session_data = update_session(session)

    async def re_subscribe_stream(self):
        try:
            for ac in self.subscribe_candle:
                sp = ac.split(",")
                await self.start_candles_one_stream(sp[0], sp[1])
        except:
            pass
        try:
            for ac in self.subscribe_candle_all_size:
                await self.start_candles_all_size_stream(ac)
        except:
            pass
        try:
            for ac in self.subscribe_mood:
                await self.start_mood_stream(ac)
        except:
            pass

    async def get_instruments(self):
        while self.check_connect and self.api.instruments is None:
            await asyncio.sleep(0.2)
        return self.api.instruments or []

    def get_all_asset_name(self):
        if self.api.instruments:
            return [[i[1], i[2].replace("\n", "")] for i in self.api.instruments]

    async def get_available_asset(self, asset_name: str, force_open: bool = False):
        _, asset_open = await self.check_asset_open(asset_name)
        if force_open and (not asset_open or not asset_open[2]):
            condition_otc = "otc" not in asset_name
            refactor_asset = asset_name.replace("_otc", "")
            asset_name = f"{asset_name}_otc" if condition_otc else refactor_asset
            _, asset_open = await self.check_asset_open(asset_name)
        return asset_name, asset_open

    async def check_asset_open(self, asset_name: str):
        instruments = await self.get_instruments()
        for i in instruments:
            if asset_name == i[1]:
                self.api.current_asset = asset_name
                return i, (i[0], i[2].replace("\n", ""), i[14])
        return [None, [None, None, None]]

    async def get_all_assets(self):
        instruments = await self.get_instruments()
        for i in instruments:
            if i[0] != "":
                self.codes_asset[i[1]] = i[0]
        return self.codes_asset

    async def get_candles(self, asset, end_from_time, offset, period, progressive=False):
        if end_from_time is None:
            end_from_time = time.time()
        index = expiration.get_timestamp()
        self.api.candles.candles_data = None
        self.start_candles_stream(asset, period)
        self.api.get_candles(asset, index, end_from_time, offset, period)
        while True:
            while self.check_connect and self.api.candles.candles_data is None:
                await asyncio.sleep(0.1)
            if self.api.candles.candles_data is not None:
                break
        candles = self.prepare_candles(asset, period)
        if progressive:
            return self.api.historical_candles.get("data", {})
        return candles

    async def get_history_line(self, asset, end_from_time, offset):
        if end_from_time is None:
            end_from_time = time.time()
        index = expiration.get_timestamp()
        self.api.current_asset = asset
        self.api.historical_candles = None
        self.start_candles_stream(asset)
        self.api.get_history_line(self.codes_asset[asset], index, end_from_time, offset)
        while True:
            while self.check_connect and self.api.historical_candles is None:
                await asyncio.sleep(0.2)
            if self.api.historical_candles is not None:
                break
        return self.api.historical_candles

    async def get_candle_v2(self, asset, period):
        self.api.candle_v2_data[asset] = None
        self.start_candles_stream(asset, period)
        while self.api.candle_v2_data[asset] is None:
            await asyncio.sleep(0.2)
        candles = self.prepare_candles(asset, period)
        return candles

    def prepare_candles(self, asset: str, period: int):
        candles_data = calculate_candles(self.api.candles.candles_data, period)
        candles_v2_data = process_candles_v2(self.api.candle_v2_data, asset, candles_data)
        new_candles = merge_candles(candles_v2_data)
        return new_candles

    async def connect(self):
        self.api = QuotexAPI(
            "qxbroker.com",
            self.email,
            self.password,
            self.lang,
            resource_path=self.resource_path,
            user_data_dir=self.user_data_dir
        )
        self.close()
        self.api.trace_ws = self.debug_ws_enable
        self.api.session_data = self.session_data
        self.api.current_asset = self.asset_default
        self.api.current_period = self.period_default
        global_value.SSID = self.session_data.get("token")
        if not self.session_data.get("token"):
            await self.api.authenticate()
        check, reason = await self.api.connect(self.account_is_demo)
        if not await self.check_connect():
            logger.debug("Reconnecting on websocket")
            return await self.connect()
        return check, reason

    async def reconnect(self):
        await self.api.authenticate()

    def set_account_mode(self, balance_mode="PRACTICE"):
        if balance_mode.upper() == "REAL":
            self.account_is_demo = 0
        elif balance_mode.upper() == "PRACTICE":
            self.account_is_demo = 1
        else:
            logger.error("ERROR doesn't have this mode")
            exit(1)

    def change_account(self, balance_mode: str):
        self.account_is_demo = 0 if balance_mode.upper() == "REAL" else 1
        self.api.change_account(self.account_is_demo)

    def change_time_offset(self, time_offset):
        return self.api.change_time_offset(time_offset)

    async def edit_practice_balance(self, amount=None):
        self.api.training_balance_edit_request = None
        self.api.edit_training_balance(amount)
        while self.api.training_balance_edit_request is None:
            await asyncio.sleep(0.2)
        return self.api.training_balance_edit_request

    async def get_balance(self):
        while self.api.account_balance is None:
            await asyncio.sleep(0.2)
        balance = self.api.account_balance.get("demoBalance") \
            if self.api.account_type > 0 else self.api.account_balance.get("liveBalance")
        return float(f"{truncate(balance + self.get_profit(), 2):.2f}")

    async def calculate_indicator(self, asset: str, indicator: str, params: dict = None,
                                  history_size: int = 3600, timeframe: int = 60) -> dict:
        valid_timeframes = [60, 300, 900, 1800, 3600, 7200, 14400, 86400]
        if timeframe not in valid_timeframes:
            return {"error": f"Timeframe no válido. Valores permitidos: {valid_timeframes}"}
        adjusted_history = max(history_size, timeframe * 50)
        candles = await self.get_candles(asset, time.time(), adjusted_history, timeframe)
        if not candles:
            return {"error": f"No hay datos disponibles para el activo {asset}"}
        prices = [float(candle["close"]) for candle in candles]
        highs = [float(candle["high"]) for candle in candles]
        lows = [float(candle["low"]) for candle in candles]
        timestamps = [candle["time"] for candle in candles]
        indicators = TechnicalIndicators()
        indicator = indicator.upper()
        try:
            if indicator == "RSI":
                period = params.get("period", 14)
                values = indicators.calculate_rsi(prices, period)
                return {
                    "rsi": values,
                    "current": values[-1] if values else None,
                    "history_size": len(values),
                    "timeframe": timeframe,
                    "timestamps": timestamps[-len(values):] if values else []
                }
            elif indicator == "MACD":
                fast_period = params.get("fast_period", 12)
                slow_period = params.get("slow_period", 26)
                signal_period = params.get("signal_period", 9)
                macd_data = indicators.calculate_macd(prices, fast_period, slow_period, signal_period)
                macd_data["timeframe"] = timeframe
                macd_data["timestamps"] = timestamps[-len(macd_data["macd"]):] if macd_data["macd"] else []
                return macd_data
            elif indicator == "SMA":
                period = params.get("period", 20)
                values = indicators.calculate_sma(prices, period)
                return {
                    "sma": values,
                    "current": values[-1] if values else None,
                    "history_size": len(values),
                    "timeframe": timeframe,
                    "timestamps": timestamps[-len(values):] if values else []
                }
            elif indicator == "EMA":
                period = params.get("period", 20)
                values = indicators.calculate_ema(prices, period)
                return {
                    "ema": values,
                    "current": values[-1] if values else None,
                    "history_size": len(values),
                    "timeframe": timeframe,
                    "timestamps": timestamps[-len(values):] if values else []
                }
            elif indicator == "BOLLINGER":
                period = params.get("period", 20)
                num_std = params.get("std", 2)
                bb_data = indicators.calculate_bollinger_bands(prices, period, num_std)
                bb_data["timeframe"] = timeframe
                bb_data["timestamps"] = timestamps[-len(bb_data["middle"]):] if bb_data["middle"] else []
                return bb_data
            elif indicator == "STOCHASTIC":
                k_period = params.get("k_period", 14)
                d_period = params.get("d_period", 3)
                stoch_data = indicators.calculate_stochastic(prices, highs, lows, k_period, d_period)
                stoch_data["timeframe"] = timeframe
                stoch_data["timestamps"] = timestamps[-len(stoch_data["k"]):] if stoch_data["k"] else []
                return stoch_data
            elif indicator == "ATR":
                period = params.get("period", 14)
                values = indicators.calculate_atr(highs, lows, prices, period)
                return {
                    "atr": values,
                    "current": values[-1] if values else None,
                    "history_size": len(values),
                    "timeframe": timeframe,
                    "timestamps": timestamps[-len(values):] if values else []
                }
            elif indicator == "ADX":
                period = params.get("period", 14)
                adx_data = indicators.calculate_adx(highs, lows, prices, period)
                adx_data["timeframe"] = timeframe
                adx_data["timestamps"] = timestamps[-len(adx_data["adx"]):] if adx_data["adx"] else []
                return adx_data
            elif indicator == "ICHIMOKU":
                tenkan_period = params.get("tenkan_period", 9)
                kijun_period = params.get("kijun_period", 26)
                senkou_b_period = params.get("senkou_b_period", 52)
                ichimoku_data = indicators.calculate_ichimoku(highs, lows, tenkan_period, kijun_period, senkou_b_period)
                ichimoku_data["timeframe"] = timeframe
                ichimoku_data["timestamps"] = timestamps[-len(ichimoku_data["tenkan"]):] if ichimoku_data["tenkan"] else []
                return ichimoku_data
            else:
                return {"error": f"Indicador '{indicator}' no soportado"}
        except Exception as e:
            return {"error": f"Error calculando el indicador: {str(e)}"}

    async def subscribe_indicator(self, asset: str, indicator: str, params: dict = None,
                                  callback=None, timeframe: int = 60):
        if not callback:
            raise ValueError("Debe proporcionar una función callback")
        valid_timeframes = [60, 300, 900, 1800, 3600, 7200, 14400, 86400]
        if timeframe not in valid_timeframes:
            raise ValueError(f"Timeframe no válido. Valores permitidos: {valid_timeframes}")
        try:
            self.start_candles_stream(asset, timeframe)
            while True:
                try:
                    real_time_candles = await self.get_realtime_candles(asset, timeframe)
                    if real_time_candles:
                        candles_list = sorted(real_time_candles.items(), key=lambda x: x[0])
                        prices = [float(candle[1]["close"]) for candle in candles_list]
                        highs = [float(candle[1]["high"]) for candle in candles_list]
                        lows = [float(candle[1]["low"]) for candle in candles_list]
                        min_periods = {
                            "RSI": 14, "MACD": 26, "BOLLINGER": 20, "STOCHASTIC": 14,
                            "ADX": 14, "ATR": 14, "SMA": 20, "EMA": 20, "ICHIMOKU": 52
                        }
                        required_periods = min_periods.get(indicator.upper(), 14)
                        if len(prices) < required_periods:
                            historical_candles = await self.get_candles(
                                asset, time.time(), timeframe * required_periods * 2, timeframe
                            )
                            if historical_candles:
                                prices = [float(candle["close"]) for candle in historical_candles] + prices
                                highs = [float(candle["high"]) for candle in historical_candles] + highs
                                lows = [float(candle["low"]) for candle in historical_candles] + lows
                        indicators = TechnicalIndicators()
                        indicator = indicator.upper()
                        result = {
                            "time": candles_list[-1][0],
                            "timeframe": timeframe,
                            "asset": asset
                        }
                        if indicator == "RSI":
                            period = params.get("period", 14)
                            values = indicators.calculate_rsi(prices, period)
                            result["value"] = values[-1] if values else None
                            result["all_values"] = values
                            result["indicator"] = "RSI"
                        elif indicator == "MACD":
                            fast_period = params.get("fast_period", 12)
                            slow_period = params.get("slow_period", 26)
                            signal_period = params.get("signal_period", 9)
                            macd_data = indicators.calculate_macd(prices, fast_period, slow_period, signal_period)
                            result["value"] = macd_data["current"]
                            result["all_values"] = macd_data
                            result["indicator"] = "MACD"
                        elif indicator == "BOLLINGER":
                            period = params.get("period", 20)
                            num_std = params.get("std", 2)
                            bb_data = indicators.calculate_bollinger_bands(prices, period, num_std)
                            result["value"] = bb_data["current"]
                            result["all_values"] = bb_data
                        elif indicator == "STOCHASTIC":
                            k_period = params.get("k_period", 14)
                            d_period = params.get("d_period", 3)
                            stoch_data = indicators.calculate_stochastic(prices, highs, lows, k_period, d_period)
                            result["value"] = stoch_data["current"]
                            result["all_values"] = stoch_data
                        elif indicator == "ADX":
                            period = params.get("period", 14)
                            adx_data = indicators.calculate_adx(highs, lows, prices, period)
                            result["value"] = adx_data["current"]
                            result["all_values"] = adx_data
                        elif indicator == "ATR":
                            period = params.get("period", 14)
                            values = indicators.calculate_atr(highs, lows, prices, period)
                            result["value"] = values[-1] if values else None
                            result["all_values"] = values
                        elif indicator == "ICHIMOKU":
                            tenkan_period = params.get("tenkan_period", 9)
                            kijun_period = params.get("kijun_period", 26)
                            senkou_b_period = params.get("senkou_b_period", 52)
                            ichimoku_data = indicators.calculate_ichimoku(highs, lows, tenkan_period, kijun_period, senkou_b_period)
                            result["value"] = ichimoku_data["current"]
                            result["all_values"] = ichimoku_data
                        else:
                            result["error"] = f"Indicador '{indicator}' no soportado para tiempo real"
                        await callback(result)
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"Error en la suscripción: {str(e)}")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error en la suscripción: {str(e)}")
        finally:
            try:
                self.stop_candles_stream(asset)
            except:
                pass

    async def get_profile(self):
        return await self.api.get_profile()

    async def get_history(self):
        account_type = "demo" if self.account_is_demo else "live"
        return await self.api.get_trader_history(account_type, page_number=1)

    async def buy(self, amount: float, asset: str, direction: str, duration: int, time_mode: str = "TIME"):
        self.api.buy_id = None
        request_id = expiration.get_timestamp()
        is_fast_option = time_mode.upper() == "TIME"
        self.start_candles_stream(asset, duration)
        self.api.buy(amount, asset, direction, duration, request_id, is_fast_option)
        count = 0.1
        while self.api.buy_id is None:
            count += 0.1
            if count > duration:
                status_buy = False
                break
            await asyncio.sleep(0.2)
            if global_value.check_websocket_if_error:
                return False, global_value.websocket_error_reason
        else:
            status_buy = True
        return status_buy, self.api.buy_successful

    async def open_pending(self, amount: float, asset: str, direction: str, duration: int, open_time: str = None):
        self.api.pending_id = None
        user_settings = await self.get_profile()
        offset_zone = user_settings.offset
        open_time = expiration.get_next_timeframe(
            int(time.time()),
            offset_zone,
            duration,
            open_time
        )
        self.api.open_pending(amount, asset, direction, duration, open_time)
        count = 0.1
        while self.api.pending_id is None:
            count += 0.1
            if count > duration:
                status_buy = False
                break
            await asyncio.sleep(0.2)
            if global_value.check_websocket_if_error:
                return False, global_value.websocket_error_reason
        else:
            status_buy = True
            self.api.instruments_follow(amount, asset, direction, duration, open_time)
        return status_buy, self.api.pending_successful

    async def sell_option(self, options_ids):
        self.api.sell_option(options_ids)
        self.api.sold_options_respond = None
        while self.api.sold_options_respond is None:
            await asyncio.sleep(0.2)
        return self.api.sold_options_respond

    def get_payment(self):
        assets_data = {}
        for i in self.api.instruments:
            assets_data[i[2].replace("\n", "")] = {
                "turbo_payment": i[18],
                "payment": i[5],
                "profit": {
                    "1M": i[-9],
                    "5M": i[-8]
                },
                "open": i[14]
            }
        return assets_data

    def get_payout_by_asset(self, asset_name: str, timeframe: str = "1"):
        assets_data = {}
        for i in self.api.instruments:
            if asset_name == i[1]:
                assets_data[i[1].replace("\n", "")] = {
                    "turbo_payment": i[18],
                    "payment": i[5],
                    "profit": {
                        "24H": i[-10],
                        "1M": i[-9],
                        "5M": i[-8]
                    },
                    "open": i[14]
                }
                break
        data = assets_data.get(asset_name)
        if timeframe == "all":
            return data.get("profit")
        return data.get("profit").get(f"{timeframe}M")

    async def start_remaing_time(self):
        now_stamp = datetime.fromtimestamp(expiration.get_timestamp())
        expiration_stamp = datetime.fromtimestamp(self.api.timesync.server_timestamp)
        remaing_time = int((expiration_stamp - now_stamp).total_seconds())
        while remaing_time >= 0:
            remaing_time -= 1
            print(f"\rRestando {remaing_time if remaing_time > 0 else 0} segundos ...", end="")
            await asyncio.sleep(1)

    async def check_win(self, id_number: int):
        task = asyncio.create_task(self.start_remaing_time())
        while True:
            data_dict = self.api.listinfodata.get(id_number)
            if data_dict and data_dict.get("game_state") == 1:
                break
            await asyncio.sleep(0.2)
        task.cancel()
        self.api.listinfodata.delete(id_number)
        return data_dict["win"]

    def start_candles_stream(self, asset: str = "EURUSD", period: int = 0):
        self.api.current_asset = asset
        self.api.subscribe_realtime_candle(asset, period)
        self.api.chart_notification(asset)
        self.api.follow_candle(asset)

    async def store_settings_apply(self, asset: str = "EURUSD", period: int = 0, time_mode: str = "TIMER",
                                   deal: int = 5, percent_mode: bool = False, percent_deal: int = 1):
        is_fast_option = False if time_mode.upper() == "TIMER" else True
        self.api.current_asset = asset
        self.api.settings_apply(asset, period, is_fast_option=is_fast_option, deal=deal,
                                percent_mode=percent_mode, percent_deal=percent_deal)
        await asyncio.sleep(0.2)
        while True:
            self.api.refresh_settings()
            if self.api.settings_list:
                investments_settings = self.api.settings_list
                break
            await asyncio.sleep(0.2)
        return investments_settings

    def stop_candles_stream(self, asset):
        self.api.unsubscribe_realtime_candle(asset)
        self.api.unfollow_candle(asset)

    async def get_realtime_candles(self, asset: str, period: int = 0):
        data = {}
        self.start_candles_stream(asset, period)
        while True:
            if self.api.realtime_price.get(asset):
                tick = self.api.realtime_candles
                return process_tick(tick, period, data)
            await asyncio.sleep(0.1)

    async def start_realtime_price(self, asset: str, period: int = 0):
        self.start_candles_stream(asset, period)
        while True:
            if self.api.realtime_price.get(asset):
                return self.api.realtime_price
            await asyncio.sleep(0.2)

    async def get_realtime_price(self, asset: str):
        return self.api.realtime_price.get(asset, {})

    async def start_realtime_sentiment(self, asset: str, period: int = 0):
        self.start_candles_stream(asset, period)
        while True:
            if self.api.realtime_sentiment.get(asset):
                return self.api.realtime_sentiment[asset]
            await asyncio.sleep(0.2)

    async def get_realtime_sentiment(self, asset: str):
        return self.api.realtime_sentiment.get(asset, {})

    def get_signal_data(self):
        return self.api.signal_data

    def get_profit(self):
        return self.api.profit_in_operation or 0

    async def get_result(self, operation_id: str):
        data_history = await self.get_history()
        for item in data_history:
            if item.get("ticket") == operation_id:
                profit = float(item.get("profitAmount", 0))
                status = "win" if profit > 0 else "loss"
                return status, item
        return None, "OperationID Not Found."

    async def start_candles_one_stream(self, asset, size):
        if not (str(asset + "," + str(size)) in self.subscribe_candle):
            self.subscribe_candle.append((asset + "," + str(size)))
        start = time.time()
        self.api.candle_generated_check[str(asset)][int(size)] = {}
        while True:
            if time.time() - start > 20:
                logger.error('**error** start_candles_one_stream late for 20 sec')
                return False
            try:
                if self.api.candle_generated_check[str(asset)][int(size)]:
                    return True
            except:
                pass
            try:
                self.api.follow_candle(self.codes_asset[asset])
            except:
                logger.error('**error** start_candles_stream reconnect')
                await self.connect()
            await asyncio.sleep(0.2)

    async def start_candles_all_size_stream(self, asset):
        self.api.candle_generated_all_size_check[str(asset)] = {}
        if not (str(asset) in self.subscribe_candle_all_size):
            self.subscribe_candle_all_size.append(str(asset))
        start = time.time()
        while True:
            if time.time() - start > 20:
                logger.error(f'**error** fail {asset} start_candles_all_size_stream late for 10 sec')
                return False
            try:
                if self.api.candle_generated_all_size_check[str(asset)]:
                    return True
            except:
                pass
            try:
                self.api.subscribe_all_size(self.codes_asset[asset])
            except:
                logger.error('**error** start_candles_all_size_stream reconnect')
                await self.connect()
            await asyncio.sleep(0.2)

    async def start_mood_stream(self, asset, instrument="turbo-option"):
        if asset not in self.subscribe_mood:
            self.subscribe_mood.append(asset)
        while True:
            self.api.subscribe_Traders_mood(asset[asset], instrument)
            try:
                self.api.traders_mood[self.codes_asset[asset]] = self.codes_asset[asset]
                break
            finally:
                await asyncio.sleep(0.2)

    def close(self):
        return self.api.close()
