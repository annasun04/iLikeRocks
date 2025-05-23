import json
import math
from abc import abstractmethod
from collections import deque
from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState
from enum import IntEnum
from statistics import NormalDist
from typing import Any, TypeAlias
import numpy as np

JSON: TypeAlias = dict[str, "JSON"] | list["JSON"] | str | int | float | bool | None

# On the Prosperity website there is a description of a rattlesnake called "Rihanna"
# In the provided data there is a trader called "Rhianna"
# I have no clue which to expect in the end-of-round run, so allow for both to be used
RIHIANNAS = ["Rhianna", "Rihanna"]

class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]], conversions: int, trader_data: str) -> None:
        base_length = len(self.to_json([
            self.compress_state(state, ""),
            self.compress_orders(orders),
            conversions,
            "",
            "",
        ]))

        # We truncate state.traderData, trader_data, and self.logs to the same max. length to fit the log limit
        max_item_length = (self.max_log_length - base_length) // 3

        print(self.to_json([
            self.compress_state(state, self.truncate(state.traderData, max_item_length)),
            self.compress_orders(orders),
            conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))

        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp,
            trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        compressed = []
        for listing in listings.values():
            compressed.append([listing.symbol, listing.product, listing.denomination])


        return compressed

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        compressed = {}
        for symbol, order_depth in order_depths.items():
            compressed[symbol] = [order_depth.buy_orders, order_depth.sell_orders]

        return compressed

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for trade in arr:
                compressed.append([
                    trade.symbol,
                    trade.price,
                    trade.quantity,
                    trade.buyer,
                    trade.seller,
                    trade.timestamp,
                ])

        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        for product, observation in observations.conversionObservations.items():
            conversion_observations[product] = [
                observation.bidPrice,
                observation.askPrice,
                observation.transportFees,
                observation.exportTariff,
                observation.importTariff,
                observation.sunlight,
                observation.humidity,
            ]

        return [observations.plainValueObservations, conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for order in arr:
                compressed.append([order.symbol, order.price, order.quantity])

        return compressed

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        if len(value) <= max_length:
            return value

        return value[:max_length - 3] + "..."

logger = Logger()

class Strategy:
    def __init__(self, symbol: str, limit: int) -> None:
        self.symbol = symbol
        self.limit = limit

    @abstractmethod
    def act(self, state: TradingState) -> None:
        raise NotImplementedError()

    def run(self, state: TradingState) -> tuple[list[Order], int]:
        self.orders = []
        self.conversions = 0

        self.act(state)

        return self.orders, self.conversions

    def buy(self, price: int, quantity: int) -> None:
        self.orders.append(Order(self.symbol, price, quantity))

    def sell(self, price: int, quantity: int) -> None:
        self.orders.append(Order(self.symbol, price, -quantity))

    def convert(self, amount: int) -> None:
        self.conversions += amount

    def get_mid_price(self, state: TradingState, symbol: str) -> float:
        order_depth = state.order_depths[symbol]
        buy_orders = sorted(order_depth.buy_orders.items(), reverse=True)
        sell_orders = sorted(order_depth.sell_orders.items())

        popular_buy_price = max(buy_orders, key=lambda tup: tup[1])[0]
        popular_sell_price = min(sell_orders, key=lambda tup: tup[1])[0]

        return (popular_buy_price + popular_sell_price) / 2

    def save(self) -> JSON:
        return None

    def load(self, data: JSON) -> None:
        pass

class Signal(IntEnum):
    NEUTRAL = 0
    SHORT = 1
    LONG = 2

class SignalStrategy(Strategy):
    def __init__(self, symbol: Symbol, limit: int) -> None:
        super().__init__(symbol, limit)

        self.signal = Signal.NEUTRAL

    @abstractmethod
    def get_signal(self, state: TradingState) -> Signal | None:
        raise NotImplementedError()

    def act(self, state: TradingState) -> None:
        new_signal = self.get_signal(state)
        if new_signal is not None:
            self.signal = new_signal

        position = state.position.get(self.symbol, 0)
        order_depth = state.order_depths[self.symbol]

        if self.signal == Signal.NEUTRAL:
            if position < 0:
                self.buy(self.get_buy_price(order_depth), -position)
            elif position > 0:
                self.sell(self.get_sell_price(order_depth), position)
        elif self.signal == Signal.SHORT:
            self.sell(self.get_sell_price(order_depth), self.limit + position)
        elif self.signal == Signal.LONG:
            self.buy(self.get_buy_price(order_depth), self.limit - position)

    def get_buy_price(self, order_depth: OrderDepth) -> int:
        return min(order_depth.sell_orders.keys())

    def get_sell_price(self, order_depth: OrderDepth) -> int:
        return max(order_depth.buy_orders.keys())

    def save(self) -> JSON:
        return self.signal.value

    def load(self, data: JSON) -> None:
        self.signal = Signal(data)

class MarketMakingStrategy(Strategy):
    def __init__(self, symbol: Symbol, limit: int) -> None:
        super().__init__(symbol, limit)

        self.window = deque()
        self.window_size = 10

    @abstractmethod
    def get_true_value(self, state: TradingState) -> int:
        raise NotImplementedError()

    def act(self, state: TradingState) -> None:
        true_value = self.get_true_value(state)

        order_depth = state.order_depths[self.symbol]
        buy_orders = sorted(order_depth.buy_orders.items(), reverse=True)
        sell_orders = sorted(order_depth.sell_orders.items())

        position = state.position.get(self.symbol, 0)
        to_buy = self.limit - position
        to_sell = self.limit + position

        self.window.append(abs(position) == self.limit)
        if len(self.window) > self.window_size:
            self.window.popleft()

        soft_liquidate = len(self.window) == self.window_size and sum(self.window) >= self.window_size / 2 and self.window[-1]
        hard_liquidate = len(self.window) == self.window_size and all(self.window)

        max_buy_price = true_value - 1 if position > self.limit * 0.5 else true_value
        min_sell_price = true_value + 1 if position < self.limit * -0.5 else true_value

        for price, volume in sell_orders:
            if to_buy > 0 and price <= max_buy_price:
                quantity = min(to_buy, -volume)
                self.buy(price, quantity)
                to_buy -= quantity

        if to_buy > 0 and hard_liquidate:
            quantity = to_buy // 2
            self.buy(true_value, quantity)
            to_buy -= quantity

        if to_buy > 0 and soft_liquidate:
            quantity = to_buy // 2
            self.buy(true_value - 2, quantity)
            to_buy -= quantity

        if to_buy > 0:
            popular_buy_price = max(buy_orders, key=lambda tup: tup[1])[0]
            price = min(max_buy_price, popular_buy_price + 1)
            self.buy(price, to_buy)

        for price, volume in buy_orders:
            if to_sell > 0 and price >= min_sell_price:
                quantity = min(to_sell, volume)
                self.sell(price, quantity)
                to_sell -= quantity

        if to_sell > 0 and hard_liquidate:
            quantity = to_sell // 2
            self.sell(true_value, quantity)
            to_sell -= quantity

        if to_sell > 0 and soft_liquidate:
            quantity = to_sell // 2
            self.sell(true_value + 2, quantity)
            to_sell -= quantity

        if to_sell > 0:
            popular_sell_price = min(sell_orders, key=lambda tup: tup[1])[0]
            price = max(min_sell_price, popular_sell_price - 1)
            self.sell(price, to_sell)

    def save(self) -> JSON:
        return list(self.window)

    def load(self, data: JSON) -> None:
        self.window = deque(data)

class AmethystsStrategy(MarketMakingStrategy):
    def get_true_value(self, state: TradingState) -> int:
        return 10_000

class StarfruitStrategy(MarketMakingStrategy):
    def get_true_value(self, state: TradingState) -> int:
        return round(self.get_mid_price(state, self.symbol))
    
class OrchidsStrategy(Strategy):
    def act(self, state: TradingState) -> None:
        position = state.position.get(self.symbol, 0)
        self.convert(-1 * position)

        obs = state.observations.conversionObservations.get(self.symbol, None)
        if obs is None:
            return

        buy_price = obs.askPrice + obs.transportFees + obs.importTariff
        self.sell(max(int(obs.bidPrice - 0.5), int(buy_price + 1)), self.limit)




class VolcanicCouponStrategy(SignalStrategy):
    def __init__(self, symbol: Symbol, limit: int, strike_price: int) -> None:
        super().__init__(symbol, limit)
        self.base_symbol = "VOLCANIC_ROCK"
        self.coupon_symbol = symbol
        self.strike_price = strike_price
        self.mid_prices = {self.base_symbol: []}
        self.window = 30

        self.cdf = NormalDist().cdf

    def calculate_realized_volatility(self, prices: list[float]) -> float:
        if len(prices) < 2:
            return 0.0001  # avoid div-by-zero or empty list
        log_returns = np.diff(np.log(prices))
        return np.std(log_returns, ddof=1) * np.sqrt(365)

    def get_signal(self, state: TradingState) -> Signal | None:
        if self.base_symbol not in state.order_depths:
            return

        if self.coupon_symbol not in state.order_depths:
            return

        base_depth = state.order_depths[self.base_symbol]
        coupon_depth = state.order_depths[self.coupon_symbol]

        if not base_depth.buy_orders or not base_depth.sell_orders:
            return
        if not coupon_depth.buy_orders or not coupon_depth.sell_orders:
            return

        volc = self.get_mid_price(state, self.base_symbol)
        coup = self.get_mid_price(state, self.coupon_symbol)

        self.mid_prices[self.base_symbol].append(volc)
        if len(self.mid_prices[self.base_symbol]) > self.window:
            self.mid_prices[self.base_symbol].pop(0)

        asset_price = volc
        expiration_time = 7 / 365  # one week
        risk_free_rate = 0
        volatility = self.calculate_realized_volatility(self.mid_prices[self.base_symbol])
        expected_price = self.black_scholes(asset_price, self.strike_price, expiration_time, risk_free_rate, volatility)

        if coup > expected_price + 2:
            return Signal.SHORT
        elif coup < expected_price - 2:
            return Signal.LONG

    def black_scholes(
        self,
        asset_price: float,
        strike_price: float,
        expiration_time: float,
        risk_free_rate: float,
        volatility: float,
    ) -> float:
        d1 = (math.log(asset_price / strike_price) + (risk_free_rate + volatility ** 2 / 2) * expiration_time) / (volatility * math.sqrt(expiration_time))
        d2 = d1 - volatility * math.sqrt(expiration_time)
        return asset_price * self.cdf(d1) - strike_price * math.exp(-risk_free_rate * expiration_time) * self.cdf(d2)


class Trader:
    def __init__(self) -> None:
        limits = {
            "KELP": 50,
            "RAINFOREST_RESIN": 50,
            "SQUID_INK": 50,
            "VOLCANIC_ROCK": 400,
            "VOLCANIC_ROCK_VOUCHER_10000": 200,
            "VOLCANIC_ROCK_VOUCHER_10250": 200,
            "VOLCANIC_ROCK_VOUCHER_10500": 200,
            "VOLCANIC_ROCK_VOUCHER_9500": 200,
            "VOLCANIC_ROCK_VOUCHER_9750": 200,
           
        }

        strike_map = {
            "VOLCANIC_ROCK_VOUCHER_9500": 9500,
            "VOLCANIC_ROCK_VOUCHER_9750": 9750,
            "VOLCANIC_ROCK_VOUCHER_10000": 10000,
            "VOLCANIC_ROCK_VOUCHER_10250": 10250,
            "VOLCANIC_ROCK_VOUCHER_10500": 10500,
        }
        other_strats = {
            "RAINFOREST_RESIN": AmethystsStrategy,
            "KELP": StarfruitStrategy,
            #"SQUID_INK":OrchidsStrategy,  

        }
        self.strategies = {}
        for symbol, strike in strike_map.items():
            self.strategies[symbol] = VolcanicCouponStrategy(symbol, limits[symbol], strike)

        # Add other asset strategies
        for symbol, clazz in other_strats.items():
            self.strategies[symbol] = clazz(symbol, limits[symbol])

        """self.strategies: dict[Symbol, Strategy] = {symbol: clazz(symbol, limits[symbol]) for symbol, clazz in {
           "VOLCANIC_ROCK_VOUCHER_10000": VolcanicCouponStrategy,
        }.items()}"""

    def run(self, state: TradingState) -> tuple[dict[Symbol, list[Order]], int, str]:
        orders = {}
        conversions = 0

        old_trader_data = json.loads(state.traderData) if state.traderData != "" else {}
        new_trader_data = {}

        for symbol, strategy in self.strategies.items():
            if symbol in old_trader_data:
                strategy.load(old_trader_data[symbol])

            if symbol in state.order_depths and len(state.order_depths[symbol].buy_orders) > 0 and len(state.order_depths[symbol].sell_orders) > 0:
                strategy_orders, strategy_conversions = strategy.run(state)
                orders[symbol] = strategy_orders
                conversions += strategy_conversions

            new_trader_data[symbol] = strategy.save()

        trader_data = json.dumps(new_trader_data, separators=(",", ":"))

        logger.flush(state, orders, conversions, trader_data)
        return orders, conversions, trader_data