import { describe, expect, it } from 'vitest';
import { CASH_EVENT_TYPES, engineTransactionToView, isCashEventType } from './engine-transactions';

const row = (over: Record<string, unknown>) => ({
  id: 'tx-1',
  type: 'BUY',
  symbol: 'XYZ',
  quantity: '10',
  price: '100',
  fees: '2',
  tradeDate: '2026-01-02T00:00:00Z',
  ...over,
});

describe('engine transaction → view', () => {
  it('preserves every engine transaction type instead of dropping unknown rows', () => {
    const types = [
      'BUY', 'SELL', 'TRANSFER_IN', 'TRANSFER_OUT', 'DEPOSIT', 'WITHDRAWAL',
      'DIVIDEND', 'INTEREST', 'FEE', 'TAX', 'SPLIT', 'REVERSE_SPLIT',
      'ADJUSTMENT', 'SPIN_OFF', 'MERGER', 'SYMBOL_CHANGE',
      'COST_BASIS_ADJUSTMENT', 'RETURN_OF_CAPITAL', 'RIGHTS_ISSUE',
      'WARRANT_EXERCISE', 'REALIZED_ADJUSTMENT', 'SWAP', 'STAKING_REWARD',
      'AIRDROP', 'BRIDGE', 'GAS_FEE', 'STAKE', 'UNSTAKE', 'SLASHING', 'FUNDING',
    ];
    const views = types.map((type) => engineTransactionToView(row({ type })));
    expect(views).toHaveLength(types.length);
    expect(views.map((v) => v.type)).toEqual(types);
  });

  it('keeps a split’s quantity and price (a dropped split corrupts the book)', () => {
    const view = engineTransactionToView(row({ type: 'SPLIT', quantity: '20', price: '50' }));
    expect(view.type).toBe('SPLIT');
    expect(view.quantity).toBe(20);
    expect(view.price).toBe(50);
  });

  it('does not relabel an in-kind transfer as a buy/sell', () => {
    expect(engineTransactionToView(row({ type: 'TRANSFER_IN' })).type).toBe('TRANSFER_IN');
    expect(engineTransactionToView(row({ type: 'TRANSFER_OUT' })).type).toBe('TRANSFER_OUT');
  });

  it('maps a dividend’s settled gross amount to 1 × gross for income', () => {
    const view = engineTransactionToView(
      row({ type: 'DIVIDEND', symbol: 'XYZ', grossAmount: '42.50', quantity: null, price: null }),
    );
    expect(view.type).toBe('DIVIDEND');
    expect(view.quantity).toBe(1);
    expect(view.price).toBe(42.5);
    expect(view.grossAmount).toBe(42.5);
  });

  it('keeps fees and taxes', () => {
    const view = engineTransactionToView(row({ type: 'SELL', fees: '1.25', taxes: '3.75' }));
    expect(view.fee).toBe(1.25);
    expect(view.taxes).toBe(3.75);
  });

  it('classifies cash-only events explicitly', () => {
    expect(isCashEventType('dividend')).toBe(true);
    expect(isCashEventType('FEE')).toBe(true);
    expect(isCashEventType('BUY')).toBe(false);
    expect(CASH_EVENT_TYPES.has('FUNDING')).toBe(true);
  });
});
