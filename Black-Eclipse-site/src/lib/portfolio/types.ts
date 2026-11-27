// Shared portfolio data types — consumed by the pure cost engine
// (src/hooks/use-portfolio-engine.ts), the analysis library and the
// Portfolio screen. Kept dependency-free so both tests and app code
// can import it from anywhere.

/** A transaction type the Python engine can store. */
export type TxType =
  | 'BUY'
  | 'SELL'
  | 'DEPOSIT'
  | 'WITHDRAWAL'
  | 'DIVIDEND'
  | 'INTEREST'
  | 'FEE'
  | 'TAX'
  | 'SPLIT'
  | 'REVERSE_SPLIT'
  | 'ADJUSTMENT'
  | 'TRANSFER_IN'
  | 'TRANSFER_OUT'
  | 'SPIN_OFF'
  | 'MERGER'
  | 'SYMBOL_CHANGE'
  | 'COST_BASIS_ADJUSTMENT'
  | 'RETURN_OF_CAPITAL'
  | 'RIGHTS_ISSUE'
  | 'WARRANT_EXERCISE'
  | 'REALIZED_ADJUSTMENT'
  | 'SWAP'
  | 'STAKING_REWARD'
  | 'AIRDROP'
  | 'BRIDGE'
  | 'GAS_FEE'
  | 'STAKE'
  | 'UNSTAKE'
  | 'SLASHING'
  | 'FUNDING';

/**
 * The five types a user can create from the quick-add dialog. The engine
 * stores many more (splits, corporate actions, crypto events); those are
 * read from the engine and must never be silently discarded.
 */
export type UserTxType = 'BUY' | 'SELL' | 'DEPOSIT' | 'WITHDRAWAL' | 'DIVIDEND';

export interface Transaction {
  id: string;
  type: TxType;
  symbol: string;
  quantity: number;
  price: number;
  fee: number;
  /** Sell-side taxes, when the engine recorded them. */
  taxes?: number;
  /** Settled gross amount for cash-only events (dividends, fees, funding). */
  grossAmount?: number;
  date: string;
  notes?: string;
}

/** A fund view model backed by the Portfolio Accounting Engine. */
export interface Fund {
  id: string;
  name: string;
  icon: string;
  accent: string;
  /** Optional goal the user can set for progress display. */
  goalValue?: number | null;
  /** Engine portfolio base currency (display/format hint). */
  baseCurrency?: string;
  /** Engine cost-basis method. */
  costBasisMethod?: 'FIFO' | 'LIFO' | 'AVERAGE_COST' | 'HIFO' | 'SPECIFIC_ID';
  createdAt: string;
  /** Seeded demo funds are protected from deletion. */
  seeded?: boolean;
  transactions: Transaction[];
}

export const newTxId = () => `tx-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
