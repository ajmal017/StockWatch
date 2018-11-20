import pickle
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.preprocessing import StandardScaler
import seaborn;seaborn.set()
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
from random import shuffle
import DataBase
from threading import Thread
from multiprocessing import Manager, Pool
import time


class RealTime:
    def __init__(self, n_cores, tickers, offset, queue):
        self.n_cores = n_cores
        self.tickers = tickers
        self.queue = queue
        self.period = 5
        self.extremum_order = 5
        self.order_threshold = 0.2
        self.sell_trigger_long = 'zero crossing'  # 'zero crossing' or 'extremum'
        self.sell_trigger_short = 'zero crossing'
        self.cash = 5000000 / n_cores
        self.start_cash = 75000
        self.min_days_before_abort = 5

        self.offset = offset
        self.past_data = pickle.load(open("FullData_5.p", "rb"))[:-offset]
        self.past_data = self.past_data[~self.past_data.index.duplicated(keep='last')]
        self.future_data = pickle.load(open("FullData_5.p", "rb"))[-offset:]

        self.past_data = self.past_data[self.tickers]
        self.future_data = self.future_data[self.tickers]

        self.positions, self.invested = DataBase.get_positions(self.tickers)
        self.positions.loc[:, 'Invested'][self.positions['Invested'] == 0] = self.start_cash
        self.positions.loc[:, 'Provisioned'][self.positions['Provisioned'] == 0] = self.start_cash
        self.positions['LongID'] = np.nan
        self.positions['ShortID'] = np.nan
        self.positions['LongDiff'] = np.nan
        self.positions['ShortDiff'] = np.nan

        self.start_simulation()

    def start_simulation(self):
        for i in range(self.offset):
            self.ingest(self.future_data.iloc[i])
            cash, invested = self.get_indicators(i)
            self.queue.put((i, cash, invested))

    def get_indicators(self, i):
        shuffle(self.tickers)
        for ticker in self.tickers:
            data = pd.DataFrame(self.past_data[ticker]).dropna()
            data = data[~data.index.duplicated(keep='last')]
            if len(data) > 390 // self.period * 26:
                ema24 = data.iloc[:, 0].ewm(span=26 * 390 // self.period).mean()
                ema12 = data.iloc[:, 0].ewm(span=12 * 390 // self.period).mean()
                macd = ema12 - ema24
                signal = macd.ewm(span=9 * 390 // self.period).mean()
                diff = (macd - signal)
                diff2 = ((macd - signal).fillna(0).diff().ewm(span=0.5 * 390 // self.period).mean()).fillna(0)
                diff = pd.Series(StandardScaler(with_mean=False).fit_transform(diff.values.reshape(-1, 1)).flatten())
                diff2 = pd.Series(StandardScaler(with_mean=False).fit_transform(diff2.values.reshape(-1, 1)).flatten())

                # Buy when diff < -1 and diff2 > 0.75
                if self.positions.loc[ticker, 'LongPosition'] == 0 and self.cash > self.start_cash:
                    if diff.iloc[-1] < -1.5 and diff2.iloc[-1] > 1:
                        self.invested += self.start_cash
                        self.cash -= self.start_cash
                        print('Buying ', ticker, i)
                        self.open_position(ticker, entry_date=data.index[-1], position=1, entry_price=data.iloc[-1, 0], entry_money=self.start_cash, diff=diff.iloc[-1])

                # Sell when diff2 goes above 1.05 then back below 0.95 or diff > 0
                if self.positions.loc[ticker, 'LongPosition']:
                    open_date = self.positions.loc[ticker, 'LongID'].split('|')[1]
                    max_diff2 = diff2.loc[data.index.get_loc(int(open_date)):].max()

                    if diff.iloc[-1] > 0 or (max_diff2 > 1.30 and diff2.iloc[-1] < 1.20):
                        new_money = data.iloc[-1, 0] / self.positions.loc[ticker, 'LongPosition'] * self.positions.loc[ticker, 'Invested']
                        self.invested -= self.positions.loc[ticker, 'Invested']
                        self.cash += new_money
                        print('Selling ', ticker, i)
                        self.close_position(ticker, exit_date=data.index[-1], position=1, exit_price=data.iloc[-1, 0], exit_money=new_money)

                # Short when diff > 1 and diff2 < -0.75
                if self.positions.loc[ticker, 'ShortPosition'] == 0 and self.cash > self.start_cash:
                    if diff.iloc[-1] > 1.5 and diff2.iloc[-1] < -1:
                        self.invested += self.start_cash
                        self.cash -= self.start_cash
                        print('Shorting ', ticker, i)
                        self.open_position(ticker, entry_date=data.index[-1], position=-1, entry_price=data.iloc[-1, 0], entry_money=self.start_cash, diff=diff.iloc[-1])

                # Cover when diff2 goes below -1.05 then back above -0.95 or diff < 0
                if self.positions.loc[ticker, 'ShortPosition']:
                    open_date = self.positions.loc[ticker, 'ShortID'].split('|')[1]
                    min_diff2 = diff2.loc[data.index.get_loc(int(open_date)):].min()

                    if diff.iloc[-1] < 0 or (min_diff2 < -1.30 and diff2.iloc[-1] > -1.20):
                        new_money = self.positions.loc[ticker, 'Provisioned'] / self.positions.loc[ticker, 'ShortPosition'] * (self.positions.loc[ticker, 'ShortPosition'] - data.iloc[-1, 0]) + self.positions.loc[ticker, 'Provisioned']
                        self.invested -= self.positions.loc[ticker, 'Provisioned']
                        self.cash += new_money
                        print('Covering ', ticker, i)
                        self.close_position(ticker, exit_date=data.index[-1], position=-1, exit_price=data.iloc[-1, 0], exit_money=new_money)

        return self.cash, self.invested

    def ingest(self, data_line):
        self.past_data = self.past_data.append(data_line)

    def update_position(self, ticker, long=None, invested=None, short=None, provisioned=None, long_id=None, short_id=None, long_diff=None, short_diff=None):
        long = self.positions.loc[ticker, 'LongPosition'] if long is None else long
        invested = self.positions.loc[ticker, 'Invested'] if invested is None else invested
        short = self.positions.loc[ticker, 'ShortPosition'] if short is None else short
        provisioned = self.positions.loc[ticker, 'Provisioned'] if provisioned is None else provisioned
        long_id = self.positions.loc[ticker, 'LongID'] if long_id is None else long_id
        short_id = self.positions.loc[ticker, 'ShortID'] if short_id is None else short_id
        long_diff = self.positions.loc[ticker, 'LongDiff'] if long_diff is None else long_diff
        short_diff = self.positions.loc[ticker, 'ShortDiff'] if short_diff is None else short_diff

        self.positions.loc[ticker] = [long, invested, short, provisioned, long_id, short_id, long_diff, short_diff]
        # print(self.positions.loc[ticker])
        Thread(target=DataBase.update_position, args=(ticker, long, invested, short, provisioned, long_id, short_id, long_diff, short_diff)).start()

    def open_position(self, ticker, entry_date, position, entry_price, entry_money, diff):
        trade_id = '{}|{}|{}|{}|{}'.format(ticker, entry_date, position, entry_price, entry_money)
        if position == 1:
            self.update_position(ticker, long=entry_price, invested=entry_money, long_id=trade_id, long_diff=diff)
        if position == -1:
            self.update_position(ticker, short=entry_price, provisioned=entry_money, short_id=trade_id, short_diff=diff)

        Thread(target=DataBase.open_position, args=(trade_id, ticker, position, entry_date, entry_price, entry_money)).start()

    def close_position(self, ticker, exit_date, position, exit_price, exit_money):
        assert(position == -1 or position == 1)
        if position == 1:
            trade_id = self.positions.loc[ticker, 'LongID']
            money_in = self.positions.loc[ticker, 'Invested']
            self.update_position(ticker, long=0, invested=exit_money, long_id=np.nan, long_diff=np.nan)
        if position == -1:
            trade_id = self.positions.loc[ticker, 'ShortID']
            money_in = self.positions.loc[ticker, 'Provisioned']
            self.update_position(ticker, short=0, provisioned=exit_money, short_id=np.nan, short_diff=np.nan)

        profit = exit_money / money_in
        Thread(target=DataBase.close_position, args=(trade_id, exit_date, exit_price, exit_money, profit)).start()


n_cores = 3
n_stocks = 300
offset = 10000
past_data = pickle.load(open("FullData_5.p", "rb"))[:-offset]
past_data = past_data[~past_data.index.duplicated(keep='last')]
future_data = pickle.load(open("FullData_5.p", "rb"))[-offset:]


tickers = list(past_data.columns)
shuffle(tickers)
tickers = tickers[:n_stocks]
tickers = [tickers[int(i * len(tickers) / n_cores):int((i + 1) * len(tickers) / n_cores)] for i in range(n_cores)]
money = pd.DataFrame(columns=['Cash', 'Invested', 'Net worth', 'Complete'])
if __name__ == '__main__':
    pool = Pool(processes=n_cores)
    m = Manager()
    q = m.Queue()
    for i in range(n_cores):
        workers = pool.apply_async(RealTime, (n_cores, tickers[i], offset, q))

    last_point = time.time()
    while 1:
        index, cash, invested = q.get()
        if index in money.index:
            money.iloc[index] += [cash, invested, cash+invested, 1]
            if money.iloc[index]['Complete'] == n_cores:
                print(time.time()-last_point)
                last_point = time.time()
        else:
            money.loc[index] = [cash, invested, cash + invested, 1]
        plt.clf()
        plt.plot(money[money['Complete'] == n_cores][['Cash', 'Invested', 'Net worth']])
        plt.pause(0.1)
