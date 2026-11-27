import { describe, expect, it } from 'vitest';
import type { PortfolioPosition, PortfolioRead } from '@/lib/harness/clients/portfolio-client';
import { portfolioReadToStats, statNumber } from './engine-stats';

function position(over: Partial<PortfolioPosition> & { symbol: string }): PortfolioPosition {
  return {
    name: over.symbol,
    assetClass: 'EQUITY',
    sector: 'TECH',
    country: 'US',
    strategy: 'CORE',
    currency: 'USD',
    quantity: '1',
    averageCost: '100',
    costBasis: '100',
    costBasisBase: '100',
    price: '100',
    marketValue: '100',
    marketValueBase: '100',
    fxPerBase: '1',
    unrealizedPnl: '0',
    unrealizedPnlBase: '0',
    realizedPnl: '0',
    realizedPnlBase: '0',
    weight: 100,
    priceFreshness: 'FRESH',
    priceTimestamp: '2026-01-02T00:00:00Z',
    priceSource: 'test',
    ...over,
  };
}

function read(over: Partial<PortfolioRead> = {}): PortfolioRead {
  return {
    portfolio: {
      id: 'pf_test',
      name: 'Test fund',
      baseCurrency: 'USD',
      ownerId: null,
      description: null,
      costBasisMethod: 'HIFO',
      status: 'ACTIVE',
      createdAt: '2026-01-01',
      updatedAt: '2026-01-02',
    },
    positions: [
      position({
        symbol: 'AAA',
        quantity: '10',
        averageCost: '100',
        costBasis: '1000',
        costBasisBase: '1000',
        price: '150',
        marketValue: '1500',
        marketValueBase: '1500',
        unrealizedPnl: '500',
        unrealizedPnlBase: '500',
        realizedPnl: '200',
        realizedPnlBase: '200',
        weight: 50,
      }),
      position({
        symbol: 'BBB',
        quantity: '5',
        averageCost: '200',
        costBasis: '1000',
        costBasisBase: '1000',
        price: '180',
        marketValue: '900',
        marketValueBase: '900',
        unrealizedPnl: '-100',
        unrealizedPnlBase: '-100',
        realizedPnl: '0',
        realizedPnlBase: '0',
        weight: 37.5,
      }),
    ],
    cash: { baseCurrency: 'USD', balance: '1000', netDeposits: '2000', dividends: '50' },
    valuation: {
      baseCurrency: 'USD',
      investedValue: '2400',
      cash: '1000',
      totalValue: '3400',
      dataQuality: {
        status: 'OK',
        missingPrices: [],
        stalePrices: [],
        missingFX: [],
        unpricedPositions: [],
        warnings: [],
      },
    },
    pnl: {
      realizedPnl: '200',
      unrealizedPnl: '400',
      totalPnl: '600',
      costBasis: '2000',
      dividends: '50',
      bySymbol: [],
    },
    dataQuality: {
      status: 'OK',
      missingPrices: [],
      stalePrices: [],
      missingFX: [],
      unpricedPositions: [],
      warnings: [],
    },
    calculationVersion: 'test',
    ...over,
  };
}

describe('portfolioReadToStats (engine is the source of truth)', () => {
  it('uses the engine totals verbatim and never recomputes them from transactions', () => {
    const stats = portfolioReadToStats(read());
    expect(stats.totalValue).toBe(3400);
    expect(stats.costBasis).toBe(2000);
    expect(stats.unrealizedPnL).toBe(400);
    // The engine says 200 realized; a client FIFO pass would also say 200 here,
    // so the next test pins a value only the engine can produce.
    expect(stats.realizedPnL).toBe(200);
    expect(stats.dividendsUsd).toBe(50);
    expect(stats.base).toBe('USD');
    expect(stats.roiPercent).toBeCloseTo(20, 5);
  });

  it('reports engine realized P&L even when a naive trade replay disagrees', () => {
    // HIFO/specific-lot/corporate-action books produce a realized number that a
    // FIFO client cannot derive from these transactions. The UI must show 77.
    const stats = portfolioReadToStats(read({
      pnl: {
        realizedPnl: '77',
        unrealizedPnl: '400',
        totalPnl: '477',
        costBasis: '2000',
        dividends: '50',
        bySymbol: [],
      },
    }));
    expect(stats.realizedPnL).toBe(77);
  });

  it('maps positions and allocates holdings by invested value (cash excluded)', () => {
    const stats = portfolioReadToStats(read());
    expect(stats.holdings.map((h) => h.symbol)).toEqual(['AAA', 'BBB']);
    expect(stats.holdings[0].valueUsd).toBe(1500);
    expect(stats.holdings[0].allocation).toBeCloseTo(62.5, 5);
    expect(stats.holdings[1].allocation).toBeCloseTo(37.5, 5);
    expect(stats.holdings[0].realizedPnLUsd).toBe(200);
    expect(stats.holdings[0].avgCost).toBe(100);
    expect(stats.holdings[0].priced).toBe(true);
  });

  it('derives only the daily move from live quotes', () => {
    const stats = portfolioReadToStats(read(), {
      AAA: { changePercent: 10 },
      BBB: { changePercent: -10 },
    });
    // AAA: 1500 − 1500/1.10; BBB: 900 − 900/0.90
    const expected = (1500 - 1500 / 1.1) + (900 - 900 / 0.9);
    expect(stats.dailyChangeValue).toBeCloseTo(expected, 5);
  });

  it('flags a missing FX rate instead of implying a quote', () => {
    const base = read();
    const stats = portfolioReadToStats({
      ...base,
      dataQuality: { ...base.dataQuality, missingFX: ['USD/EUR'] },
      positions: [position({ symbol: 'EURX', currency: 'EUR', price: '10', marketValue: '100', marketValueBase: '108' })],
    });
    expect(stats.holdings[0].fxMissing).toBe(true);
  });

  it('parses engine decimal strings safely', () => {
    expect(statNumber('1,234.50')).toBe(1234.5);
    expect(statNumber(null)).toBe(0);
    expect(statNumber('nonsense')).toBe(0);
  });
});
