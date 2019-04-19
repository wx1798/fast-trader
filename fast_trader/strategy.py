# -*- coding: utf-8 -*-

import datetime
import logging
import collections

from fast_trader.dtp_trade import DTP, Trader, Dispatcher
from fast_trader.dtp_quote import (Transaction, Snapshot,
                                   MarketOrder, Index, OrderQueue)

from fast_trader.dtp import type_pb2 as dtp_type

from fast_trader.dtp_trade import OrderResponse, TradeResponse, CancellationResponse

from fast_trader.position_store import SqlitePositionStore as PositionStore
from fast_trader.utils import timeit, message2tuple, attrdict
from fast_trader.settings import settings


class Market:

    def __init__(self):
        self._strategies = []

    def add_strategy(self, strategy):
        self._strategies.append(strategy)
    
    def remove_strategy(self, strategy):
        self._strategies.remove(strategy)

    def on_quote_message(self, message):
        for ea in self._strategies:
            ea.on_quote_message(message)


def to_timeint(dt):
    return (dt.hour * 10000000 + dt.minute * 100000 
        + dt.second * 1000 + int(dt.microsecond / 1000))


class Strategy:

    def __init__(self, number, *args, **kw):

        self._strategy_id = number

        self._started = False

        self._positions = {}
        self._orders = collections.defaultdict(attrdict)
        self._trades = {}

        self.subscribed_datasources = []

        self.logger = logging.getLogger(
            'fast_trader.strategy.Strategy-{}'.format(number))

    def set_dispatcher(self, dispatcher):
        self.dispatcher = dispatcher

    def set_trader(self, trader):
        self.trader = trader
        self.trader.add_strategy(self)

    def set_market(self, market=None):
        if market is None:
            market = Market()
        self.market = market
        market.add_strategy(self)

    def set_position_store(self, store=None):
        if store is None:
            store = PositionStore()
        self._position_store = store
    
    def set_initial_positions(self, positions):
        """
        设置策略初始持仓
        """
        raise NotImplementedError

    def start(self):

        self.on_start()

        # xx
        self.set_position_store()

        self.trader.start()

        self._account = settings['account']
        self.trader.login(
            account=settings['account'],
            password=settings['password'],
            request_id=self.generate_request_id(),
            sync=True)

        if self.trader.logined:
            self._started = True
            self.on_start()
            self.logger.info('策略启动成功')

            # 启动行情线程
            self.start_market()

            return {'ret_code': 0, 'data': None}

        else:
            err_msg = '策略启动失败 账户<{}>未成功登录'.format(self.account_no)
            self.logger.warning(err_msg)
            return {'ret_code': -1, 'err_msg': err_msg}

    def remove_self(self):
        self.trader.remove_strategy(self)
        self.market.remove_strategy(self)

    def start_market(self):

        for ds in self.subscribed_datasources:
            ds.start()

    @property
    def strategy_id(self):
        return self._strategy_id

    @property
    def account_no(self):
        return self.trader._account

    @property
    def position(self):
        return {p['code']: p['balance'] for p in self._positions
                if p.get('balance', 0) != 0}

    def generate_order_id(self):
        return self.trader.generate_order_id(self._strategy_id)

    def generate_request_id(self):
        return self.trader.generate_request_id(self._strategy_id)

    def _check_owner(self, obj):
        """
        判断该查询响应数据是否属于当前策略
        """
        return int(obj.order_original_id) in self._id_whole_range

    def _get_all_pages(self, handle):
        offset = 0
        size = 200
        all_objs = []
        while True:
            request_id = self.generate_request_id()
            mail = handle(request_id=request_id,
                          sync=True,
                          pagination={
                              'size': size,
                              'offset': offset
                          })

            list_name = ''
            for attr in ['order_list', 'fill_list', 'position_list']:
                if hasattr(mail.body, attr):
                    list_name = attr
                    break

            _objs = mail['body'].get(list_name, [])

            all_objs.extend(_objs)
            if len(_objs) < size:
                break
            offset = mail.body.pagination.offset

        return all_objs

    def get_account_orders(self):
        """
        查询账户报单
        """
        return self._get_all_pages(self.trader.query_orders)
    
    def get_account_open_orders(self):
        """
        查询账户未成交(可撤)报单

        Note
        --------
        enum OrderStatus
        {
            ORDER_STATUS_UNDEFINED = 0;
            ORDER_STATUS_PLACING = 1;               // 正报: 交易所处理中
                                                    // (order_exchange_id已产生)
            ORDER_STATUS_PLACED = 2;                // 已报: 交易所已挂单
            ORDER_STATUS_PARTIAL_FILLED = 3;        // 部分成交
            ORDER_STATUS_FILLED = 4;                // 全部成交
            ORDER_STATUS_CANCELLING = 5;            // 待撤
            ORDER_STATUS_CANCELLED = 6;             // 已撤
            ORDER_STATUS_PARTIAL_CANCELLING = 7;    // 部分成交其余待撤
            ORDER_STATUS_PARTIAL_CANCELLED = 8;     // 部分成交其余已撤
            ORDER_STATUS_FAILED = 9;                // 废单
        }
        """
        orders = self.get_account_orders()
        open_orders = [order for order in orders if order['status'] < 4]
        return open_orders
    
    def get_account_trades(self):
        """
        查询账户成交
        """
        return self._get_all_pages(self.trader.query_trades)

    def get_account_positions(self):
        """
        查询账户持仓
        """
        positions = self._get_all_pages(self.trader.query_positions)
        return positions

    def get_orders(self):
        """
        查询报单
        """
        orders = self.get_account_orders()
        orders = [order for order in orders if self._check_owner(order)]
        return orders

    def get_open_orders(self):
        """
        查询未成交报单
        """
        orders = self.get_account_open_orders()
        orders = [order for order in orders if self._check_owner(order)]
        return orders

    def get_trades(self):
        """
        查询成交（同步）
        """
        trades = self.get_account_trades()
        trades = [trade for trade in trades if self._check_owner(trade)]
        return trades

    def get_capital(self):
        """
        查询资金（同步）
        """
        request_id = self.generate_request_id()
        mail = self.trader.query_capital(request_id=request_id, sync=True)
        return mail['body']

    def get_ration(self):
        """
        查询配售权益
        """
        request_id = self.generate_request_id()
        mail = self.trader.query_ration(request_id=request_id, sync=True)
        return mail['body']

    def get_positions(self):
        """
        查询策略持仓

        从PositionStore中读取数据
        """
        return self._position_store.get_positions(self.strategy_id)

    def get_position_by_code(self, code, exchange=None):
        return self._position_store.get_position_by_code(
                strategy_id=self.strategy_id,
                code=code,
                exchange=exchange)

    def update_position(self, trade):

        code = trade.code
        order_side = trade.order_side
        fill_quantity = trade.fill_quantity
        last_pos = self.get_position_by_code(code)

        last_quantity = last_pos['balance']
        available_quantity = last_pos['available_quantity']
        last_cost_price = last_pos['cost'] or 0.

        _sign = 1 if order_side == dtp_type.ORDER_SIDE_BUY else -1

        quantity = fill_quantity * _sign + last_quantity

        # calc available quantity
        if order_side == dtp_type.ORDER_SIDE_SELL:
            available_quantity -= fill_quantity

        if order_side == dtp_type.ORDER_SIDE_BUY:
            tot_value = (last_quantity * last_cost_price +
                         fill_quantity * float(trade.fill_price))
            tot_quantity = (last_quantity + trade.fill_quantity)
            cost_price = tot_value / tot_quantity
        else:
            cost_price = last_cost_price

        self._position_store.set_positions([
            {
                'strategy_id': self.strategy_id,
                'exchange': trade.exchange,
                'code': trade.code,
                'balance': quantity,
                'available_quantity': available_quantity,
                'cost': cost_price,
                'update_date': datetime.datetime.now().strftime('%Y%m%d'),
                'update_time': trade.fill_time
            }
        ])

    @property
    def positions(self):
        if not hasattr(self, '_position_query_proxy'):
            class _Positions:
                def __getitem__(self_, code):
                    return self.get_position_by_code(code)
            self._position_query_proxy = _Positions()
        return self._position_query_proxy

    def get_exchange(self, code):
        """
        返回交易所代码
        """
        if code.startswith('6'):
            return dtp_type.EXCHANGE_SH_A
        else:
            return dtp_type.EXCHANGE_SZ_A

    def add_datasource(self, datasource):
        """
        添加行情数据源
        """

        name = datasource.name

        dispatcher = self.dispatcher

        # FIXME: bind once only
        try:
            dispatcher.bind('{}_rsp'.format(name),
                            self.market.on_quote_message)

            datasource.add_listener(dispatcher)

        except Exception as e:
            print(e)
            pass

        self.subscribed_datasources.append(datasource)

        if self._started:
            datasource.start()

    def on_quote_message(self, message):

        api_id = message['api_id']

        if api_id == 'trade_feed':
            data = message['content']
            self.on_market_trade(data)

        elif api_id == 'tick_feed':
            data = message['content']
            self.on_market_snapshot(data)

        elif api_id == 'order_feed':
            data = message['content']
            self.on_market_order(data)

        elif api_id == 'queue_feed':
            data = message['content']
            self.on_market_queue(data)

        elif api_id == 'index_feed':
            data = message['content']
            self.on_market_index(data)

    def on_start(self):
        """
        策略启动
        """
        pass

    def on_market_snapshot(self, market_snapshot):
        """
        快照行情
        """
        pass

    def on_market_queue(self, market_queue):
        """
        委托队列行情
        """
        pass

    def on_market_trade(self, market_trade):
        """
        逐笔成交行情
        """
        pass

    def on_market_order(self, market_order):
        """
        逐笔报单行情(上交所无该数据推送)
        """
        pass

    def on_market_index(self, market_index):
        """
        指数行情
        """
        pass

    def _on_trade(self, msg):
        """
        成交回报
        """
        trade = TradeResponse.from_msg(msg.body)
        if msg.header.code == dtp_type.RESPONSE_CODE_OK:
            original_id = trade.order_original_id
            # FIXME: strategy might start before trade responses and after
            # order responses
            order_detail = self._orders[original_id]
            order_detail.update(trade)

            # 成交时，可能只会收到成交回报，而不会收到报单回报，手动更新order_status
            if 0 < trade.total_fill_quantity < trade.quantity:
                if order_detail.status not in [dtp_type.ORDER_STATUS_PARTIAL_CANCELLED]:
                    order_detail['status'] = dtp_type.ORDER_STATUS_PARTIAL_FILLED
            elif trade.total_fill_quantity == trade.quantity: 
                order_detail['status'] = dtp_type.ORDER_STATUS_FILLED

        if msg.body.fill_status != 1:
            self.logger.error(msg)
        else:
            # 更新本地持仓记录
            self.update_position(trade)

            self.on_trade(trade)

    def on_trade(self, data):
        """
        用户策略覆盖此方法以处理成交回报
        """
        pass

    def _on_order(self, msg):
        """
        订单回报
        """
        order = OrderResponse.from_msg(msg.body)
        # if msg.header.code == dtp_type.RESPONSE_CODE_OK:
        original_id = order.order_original_id
        order_detail = self._orders[original_id]
        order_detail.update(order)

        self.on_order(order)

    def on_order(self, data):
        """
        用户策略覆盖此方法以处理委托回报
        """
        pass

    def _on_batch_order_submission(self, msg):
        """
        批量委托响应
        """
        pass

    def _on_order_query(self, orders):
        """
        报单查询
        """
        pass

    def _on_order_cancelation_submission(self, msg):
        """
        撤单提交响应
        """
        pass

    def _on_order_cancelation(self, msg):
        """
        撤单确认回报
        """
        data = CancellationResponse.from_msg(msg.body)
        if msg.header.code == dtp_type.RESPONSE_CODE_OK:
            original_id = data.order_original_id
            order_detail = self._orders[original_id]
            order_detail.update(data)

        self.on_order_cancelation(data)

    def on_order_cancelation(self, msg):
        pass

    def _on_compliance_report(self, report):
        """
        风控回报
        """
        pass

    def _store_order(self, order):
        order['status'] = dtp_type.ORDER_STATUS_UNDEFINED
        order['placed_localtime'] = to_timeint(datetime.datetime.now())
        order = attrdict(order)
        self._orders[order.order_original_id] = order
        return order

    def _insert_many(self, order_side, orders):
        request_id = self.generate_request_id()
        for order in orders:
            order_original_id = self.generate_order_id()
            if 'exchange' not in order:
                order['exchange'] = self.get_exchange(order['code'])
            order['order_side'] = order_side
            order['order_type'] = dtp_type.ORDER_TYPE_LIMIT
            order['order_original_id'] = order_original_id

        self.trader.place_batch_order(request_id=request_id, orders=orders)

        for order in orders:
            order = self._store_order(order)

        return orders

    def buy_many(self, orders):
        """
        批量买入
        """
        return self._insert_many(dtp_type.ORDER_SIDE_BUY, orders)

    def sell_many(self, orders):
        """
        批量买入
        """
        return self._insert_many(dtp_type.ORDER_SIDE_SELL, orders)

    def _insert_order(self, **kw):
        request_id = self.generate_request_id()
        order_original_id = self.generate_order_id()
        exchange = kw['exchange'] or self.get_exchange(kw['code'])
        price = kw['price']

        order = kw.copy()
        order.update({
            'order_original_id': order_original_id,
            'exchange': exchange,
            'price': price,
        })

        self.trader.place_order(request_id=request_id, **order)

        order = self._store_order(order)

        return order

    def buy(self, code, price, quantity, exchange=None):
        """
        委托买入

        Parameters
        ----------
        code: str
        price: float
        quantity: int
        exchange: NoneType | int

        Returns
        ----------
        ret: int
            返回报单结构
        """
        return self._insert_order(order_side=dtp_type.ORDER_SIDE_BUY,
                                  code=code, price=price,
                                  quantity=quantity, exchange=exchange)

    def sell(self, code, price, quantity, exchange=None):
        """
        委托卖出

        Parameters
        ----------
        code: str
        price: float
        quantity: int
        exchange: NoneType | int

        Returns
        ----------
        ret: int
            返回报单结构
        """
        return self._insert_order(order_side=dtp_type.ORDER_SIDE_SELL,
                                  code=code, price=price,
                                  quantity=quantity, exchange=exchange)

    def cancel_order(self, **kw):
        """
        撤单

        Parameters
        ----------
        exchange: int
        order_exchange_id: str
        """
        if 'order_exchange_id' not in kw:
            self.logger.warning('未提供 order_exchange_id , 无法撤单')
            return

        request_id = self.generate_request_id()
        self.trader.cancel_order(request_id=request_id, **kw)

    def cancel_all(self, **kw):
        orders = self.get_open_orders()
        for order in orders:
            self.cancel_order(**order)


class StrategyFactory:
    """
    演示策略实例化流程
    """
    def __init__(self, factory_settings=None):

        # FIXME: settings should be independent for each factory instance
        if factory_settings is not None:
            settings.set(factory_settings)

        # 用于 trader 与 dtp通道 以及 策略实例 间的消息分发
        # 将所有行情数据与柜台回报在同一个线程中进行分发
        self.dispatcher = Dispatcher()

        # dtp通道
        self.dtp = DTP(self.dispatcher)

        # 行情通道
        self.market = Market()

        self.traders = {}

    def generate_strategy(self, StrategyCls, trader_id, strategy_id):

        strategy = StrategyCls(strategy_id)

        if trader_id not in self.traders:
            trader = Trader(self.dispatcher, self.dtp, trader_id)
            self.traders[trader_id] = trader
        else:
            trader = self.traders[trader_id]

        strategy.set_trader(trader)

        strategy.set_dispatcher(self.dispatcher)

        strategy.set_market(self.market)

        return strategy

    def remove_strategy(self, strategy):
        # FIXME: remove trader from dtp
        self.market.remove_strategy(strategy)
        strategy.trader.remove_strategy(strategy)
