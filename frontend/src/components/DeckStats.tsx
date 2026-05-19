import React, { useMemo } from 'react';
import type { DeckResponse } from '../types';
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts';
import type { ValueType, NameType } from 'recharts/types/component/DefaultTooltipContent';

export interface DeckStatsProps {
  deck: DeckResponse;
}

const MANA_COLORS: Record<string, string> = {
  W: '#f5e6a8',
  U: '#5fa8ff',
  B: '#7b6fd9',
  R: '#ff6b6b',
  G: '#57c56f',
  C: '#9aa6b2',
};

function getTotalPrice(deck: DeckResponse): number {
  /** Sum hydrated card prices to produce a rough deck cost estimate. */
  return deck.decklist.reduce((sum, card) => {
    const price = Number.parseFloat(card.price ?? '0');
    return sum + (price * (card.count || 1));
  }, 0);
}

function parseApproximateManaValue(manaCost?: string): number {
  /** Estimate mana value from mana symbols when no explicit MV field is available. */
  if (!manaCost) return 0;
  const regex = /\{([^}]+)\}/g;
  let total = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(manaCost)) !== null) {
    const symbol = match[1];
    if (/^[0-9]+$/.test(symbol)) total += Number.parseInt(symbol, 10);
    else if (symbol !== 'X' && symbol !== 'Y' && symbol !== 'Z') total += 1;
  }
  return total;
}

function getDeckStats(deck: DeckResponse) {
  /** Build aggregate mana-cost, mana-production, and average-value stats for the deck. */
  const nonLands = deck.decklist.filter((card) => !card.type_line?.toLowerCase().includes('land'));
  const commanderColors = Array.from(
    new Set([...(deck.commander.color_identity || []), ...(deck.secondary_commander?.color_identity || [])]),
  );
  const manaCost: Record<string, number> = {};
  const manaProduction: Record<string, number> = {};

  for (const nonLand of nonLands) {
    if (!nonLand.mana_cost) continue;
    const regex = /\{([^}]+)\}/g;
    let match: RegExpExecArray | null;
    while ((match = regex.exec(nonLand.mana_cost)) !== null) {
      const symbol = match[1];
      if (commanderColors.includes(symbol)) {
        manaCost[symbol] = (manaCost[symbol] || 0) + (nonLand.count || 1);
      } else if (/^[0-9]+$/.test(symbol)) {
        manaCost.C = (manaCost.C || 0) + (Number.parseInt(symbol, 10) * (nonLand.count || 1));
      }
    }
  }

  for (const card of deck.decklist) {
    for (const symbol of card.produced_mana || []) {
      if (commanderColors.includes(symbol) || symbol === 'C') {
        manaProduction[symbol] = (manaProduction[symbol] || 0) + (card.count || 1);
      }
    }
  }

  const totalNonLandCards = nonLands.reduce((sum, card) => sum + (card.count || 1), 0);
  const totalManaValue = nonLands.reduce(
    (sum, card) => sum + (parseApproximateManaValue(card.mana_cost) * (card.count || 1)),
    0,
  );

  return {
    manaCost,
    manaProduction,
    averageManaValue: totalNonLandCards ? totalManaValue / totalNonLandCards : 0,
  };
}

function toPieData(values: Record<string, number>) {
  /** Convert a mana breakdown map into chart-ready slices and totals. */
  return Object.entries(values)
    .filter(([, value]) => value > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([symbol, value]) => ({ symbol, value, fill: MANA_COLORS[symbol] || '#8f9bb3' }));
}

function renderLegend(data: Array<{ symbol: string; value: number; fill: string }>): React.ReactElement | null {
  /** Render a compact legend matching the mana pie chart slice colors. */
  if (!data.length) return null;
  return (
    <div className="deck-stats__legend">
      {data.map((entry) => (
        <div key={entry.symbol} className="deck-stats__legend-item">
          <span className="deck-stats__legend-swatch" style={{ backgroundColor: entry.fill }} />
          <span>{entry.symbol}: {entry.value}</span>
        </div>
      ))}
    </div>
  );
}

const tooltipFormatter: NonNullable<React.ComponentProps<typeof Tooltip>['formatter']> = (
  value: ValueType | undefined,
  _name: NameType | undefined,
  item: { payload?: { symbol?: string } },
) => {
  /** Format the tooltip contents for mana pie charts. */
  return [`${value ?? 0}`, item?.payload?.symbol ?? 'Mana'];
};

function renderPie(
  data: Array<{ symbol: string; value: number; fill: string }>,
  chartKey: string,
): React.ReactElement {
  /** Render a pie chart with a responsive wrapper in the browser and fixed sizing in tests. */
  const chartContent = (
    <>
      <Pie data={data} dataKey="value" nameKey="symbol" innerRadius={48} outerRadius={78} paddingAngle={2} cx="50%" cy="50%">
        {data.map((entry) => (
          <Cell key={`${chartKey}-${entry.symbol}`} fill={entry.fill} stroke="#11161f" strokeWidth={1.5} />
        ))}
      </Pie>
      <Tooltip
        formatter={tooltipFormatter}
        contentStyle={{ background: '#131a23', border: '1px solid #2b3545', borderRadius: '10px', color: '#eef3fb' }}
      />
    </>
  );

  if (typeof ResizeObserver === 'undefined') {
    return <PieChart width={260} height={220}>{chartContent}</PieChart>;
  }

  return (
    <ResponsiveContainer width="100%" height="100%" minWidth={240} minHeight={220}>
      <PieChart>{chartContent}</PieChart>
    </ResponsiveContainer>
  );
}

export default function DeckStats({ deck }: DeckStatsProps): React.ReactElement {
  /** Render styled deck statistics including pie charts for mana cost and production. */
  const totalPrice = useMemo(() => getTotalPrice(deck), [deck]);
  const { manaCost, manaProduction, averageManaValue } = useMemo(() => getDeckStats(deck), [deck]);
  const manaCostData = useMemo(() => toPieData(manaCost), [manaCost]);
  const manaProductionData = useMemo(() => toPieData(manaProduction), [manaProduction]);

  return (
    <section className="deck-stats" aria-label="Deck statistics">
      <h3>Deck statistics</h3>
      <div className="deck-stats__summary">
        <div className="deck-stats__summary-card">
          <span className="deck-stats__summary-label">Total price</span>
          <strong>${totalPrice.toFixed(2)}</strong>
        </div>
        <div className="deck-stats__summary-card">
          <span className="deck-stats__summary-label">Approx. average mana value</span>
          <strong>{averageManaValue.toFixed(2)}</strong>
        </div>
      </div>
      <div className="deck-stats__charts">
        <div className="deck-stats__chart-card">
          <h4>Mana Cost Breakdown</h4>
          <div className="deck-stats__chart-area">
            {renderPie(manaCostData, 'cost')}
          </div>
          {renderLegend(manaCostData)}
        </div>
        <div className="deck-stats__chart-card">
          <h4>Mana Production Breakdown</h4>
          <div className="deck-stats__chart-area">
            {renderPie(manaProductionData, 'production')}
          </div>
          {renderLegend(manaProductionData)}
        </div>
      </div>
      {deck.notes?.length > 0 && (
        <ul className="notes">
          {deck.notes.map((note, idx) => (
            <li key={idx}>{note}</li>
          ))}
        </ul>
      )}
    </section>
  );
}