import { useCallback, useEffect, useRef, useState, type CSSProperties, type DragEvent, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { Chart as ChartJS, CategoryScale, LinearScale, LineController, LineElement, PointElement, BarController, BarElement, DoughnutController, ArcElement, Tooltip, type ChartData, type ChartOptions } from 'chart.js';
import { Bar, Doughnut, Line } from 'react-chartjs-2';
import './app.css';

ChartJS.register(CategoryScale, LinearScale, LineController, LineElement, PointElement, BarController, BarElement, DoughnutController, ArcElement, Tooltip);
ChartJS.defaults.font.family = '"Avenir Next", Avenir, "Segoe UI", sans-serif';
ChartJS.defaults.color = '#796957';

type Page = 'overview' | 'over_time' | 'transactions' | 'merchants' | 'review';
type Transaction = {
  id: string; date: string; raw_description: string; normalized_merchant: string;
  amount_cents: number; category: string; classification_source: string;
  needs_review: boolean; possible_duplicate?: boolean; excluded_from_spending?: boolean; source: string;
};
type Mapping = { display_name: string; normalized_merchant: string; category: string; source: string; confidence: number | null; updated_at: string };
type ComparisonRow = { category: string; current_cents: number; previous_cents: number; delta_cents: number };
type Dashboard = {
  month: string; available_months: string[]; total_spending_cents: number; transaction_count: number;
  review_count: number; merchant_count: number; categories: { category: string; cents: number }[];
  weeks: { label: string; cents: number }[]; top_merchants: { name: string; cents: number }[];
  needs_review: Transaction[];
  comparison: { previous_month: string; has_previous_data: boolean; delta_cents: number; categories: ComparisonRow[] };
};
type HistoryMonth = { month: string; total_cents: number; categories: Record<string, number> };
type SpendingHistory = { categories: string[]; months: HistoryMonth[]; total_spending_cents: number; transaction_count: number; months_tracked: number };
type IgnoreRule = { column: string; operator: 'equals' | 'contains' | 'starts_with'; value: string };
type ImportSettings = {
  date_format: 'auto' | 'iso' | 'us'; amount_convention: 'expenses_positive' | 'expenses_negative';
  column_mapping: Record<string, string>; merchant_column: string; time_column: string;
  type_column: string; cardholder_column: string; skip_payments: boolean;
  payment_column: string; payment_value: string; skip_non_posted: boolean;
  status_column: string; posted_value: string; match_rows: boolean; ignore_rules: IgnoreRule[];
};
type ImportPreset = { id: string; label: string; source: string; base_preset: string; settings: ImportSettings; default_settings: ImportSettings };
type Snapshot = { dashboard: Dashboard; history: SpendingHistory; transactions: Transaction[]; merchants: Mapping[]; categories: string[]; demo: boolean; jev_configured: boolean; import_presets: ImportPreset[]; new_card_settings: ImportSettings };
type ImportReport = { rows_imported: number; duplicates_skipped: number; possible_duplicates: number; payments_skipped: number; non_posted_skipped: number; rules_skipped: number; rows_failed: number; jev_attempts: number; jev_failures?: number; jev_skipped_no_key?: number; jev_errors?: string[]; errors?: { line: number; message: string }[] };

type Modal = { kind: 'import' } | { kind: 'add' } | { kind: 'correct'; transaction: Transaction } | { kind: 'delete'; transaction: Transaction } | { kind: 'report'; report: ImportReport } | null;
const colors: Record<string, string> = {
  Groceries: '#cf5824', 'Food + coffee': '#b68700', Transportation: '#3975bd',
  Shopping: '#c33f60', Entertainment: '#8553b4', Travel: '#178b96',
  'Health + personal wellness': '#53833b', Other: '#626b79',
};
const trendColors = colors;
const money = (cents: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
const signedMoney = (cents: number) => `${cents > 0 ? '+' : ''}${money(cents)}`;
const monthName = (month: string, long = false) => month === 'synthetic' ? 'Synthetic' : new Intl.DateTimeFormat('en-US', { month: long ? 'long' : 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${month}-01T12:00:00Z`));
const defaultTransactionDate = (month: string) => {
  if (month === 'synthetic') return '2026-06-15';
  const today = new Date();
  const localDate = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  return localDate.startsWith(month) ? localDate : `${month}-01`;
};
const sourceLabel = (source: string) => ({ jev: 'Jev', merchant_mapping: 'Saved mapping', manual: 'Manual', synthetic: 'Demo', unclassified: 'Needs review' } as Record<string, string>)[source] ?? source;
const categoryStyle = (category: string) => ({ '--category-color': colors[category] ?? colors.Other }) as CSSProperties;

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.error || 'Something went wrong');
  return data as T;
}
function post<T>(url: string, body: object): Promise<T> {
  return request<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
function customColumnMapping(form: FormData): Record<string, string> | null {
  const fields = (['date', 'description', 'amount', 'id'] as const).map(field => [field, String(form.get(`${field}_column`) ?? '').trim()] as const);
  return fields.some(([, name]) => name) ? Object.fromEntries(fields.filter(([, name]) => name)) : null;
}
function Metric({ label, value, icon, color }: { label: string; value: string | number; icon: string; color: string }) {
  return <div className="card metric"><div className={`metric-icon ${color}`} aria-hidden="true">{icon}</div><div><div className="metric-label">{label}</div><div className="metric-value">{value}</div></div></div>;
}
function Comparison({ data }: { data: Dashboard }) {
  const comparison = data.comparison;
  const previous = monthName(comparison.previous_month, true);
  if (!comparison.has_previous_data) return <section className="card comparison-card"><h2>Compared with {previous}</h2><p className="comparison-empty">No transactions for {previous} yet. Import that month's statement to compare categories.</p></section>;
  const maximum = Math.max(1, ...comparison.categories.flatMap(row => [Math.abs(row.current_cents), Math.abs(row.previous_cents)]));
  const tone = (value: number) => value > 0 ? 'increase' : value < 0 ? 'decrease' : 'unchanged';
  return <section className="card comparison-card"><div className="comparison-heading"><div><h2>Compared with {previous}</h2><p>Spending by category · {monthName(data.month, true)} vs {previous}</p></div><div className={`comparison-summary ${tone(comparison.delta_cents)}`}><strong>{signedMoney(comparison.delta_cents)}</strong><span>overall change</span></div></div><div className="comparison-list">{comparison.categories.map(row => <div className="comparison-row" key={row.category}><div className="comparison-category"><span className="category-dot" style={categoryStyle(row.category)} /><strong>{row.category}</strong></div><div className="comparison-bars">{([['current', data.month, row.current_cents], ['previous', comparison.previous_month, row.previous_cents]] as const).map(([kind, month, cents]) => <div className="comparison-bar-line" key={kind}><span>{monthName(month).split(' ')[0]}</span><div className="comparison-track"><i className={`comparison-fill ${kind}`} style={{ width: `${Math.abs(cents) / maximum * 100}%` }} /></div><strong>{money(cents)}</strong></div>)}</div><div className={`comparison-delta ${tone(row.delta_cents)}`}><strong>{signedMoney(row.delta_cents)}</strong><span>{row.delta_cents > 0 ? 'more' : row.delta_cents < 0 ? 'less' : 'no change'}</span></div></div>)}</div></section>;
}
const monthlyTooltipStyle = {
  backgroundColor: '#35291f', titleColor: '#fffdf9', bodyColor: '#f6e8d8',
  titleFont: { size: 12, weight: 'bold' as const }, bodyFont: { size: 12 },
  padding: 10, cornerRadius: 8, bodySpacing: 5,
};
function wrapChartLabel(label: string): string[] {
  const lines = [''];
  for (const word of label.split(' ')) {
    const last = lines.length - 1;
    if (lines[last] && lines[last].length + word.length + 1 > 16) lines.push(word);
    else lines[last] += (lines[last] ? ' ' : '') + word;
  }
  return lines;
}
function Overview({ data, correct }: { data: Dashboard; correct: (transaction: Transaction) => void }) {
  const maxMerchant = Math.max(1, ...data.top_merchants.map(merchant => merchant.cents));
  const positive = data.categories.filter(item => item.cents > 0);
  const totalPositive = positive.reduce((sum, item) => sum + item.cents, 0);
  const weeklyData: ChartData<'bar', number[], string> = {
    labels: data.weeks.map(week => week.label),
    datasets: [{ label: 'Spending', data: data.weeks.map(week => week.cents), backgroundColor: '#d48b56', hoverBackgroundColor: '#ad4d25', borderRadius: 5, maxBarThickness: 65 }],
  };
  const weeklyOptions: ChartOptions<'bar'> = {
    responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
    interaction: { mode: 'index', intersect: false, axis: 'x' },
    plugins: {
      legend: { display: false },
      tooltip: { ...monthlyTooltipStyle, displayColors: false, callbacks: { label: item => 'Spending: ' + money(item.parsed.y ?? 0) } },
    },
    scales: {
      x: { grid: { display: false }, border: { color: '#dacab7' }, ticks: { color: '#796957', font: { size: 11 }, maxRotation: 0, autoSkip: true } },
      y: { beginAtZero: true, grid: { color: '#ece2d5' }, border: { display: false }, ticks: { color: '#796957', font: { size: 11 }, maxTicksLimit: 5, callback: value => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 }).format(Number(value) / 100) } },
    },
  };
  const categoryData: ChartData<'doughnut', number[], string> = {
    labels: positive.map(item => item.category),
    datasets: [{ data: positive.map(item => item.cents), backgroundColor: positive.map(item => colors[item.category] ?? colors.Other), borderColor: '#fffdf9', borderWidth: 2, hoverBorderWidth: 3, hoverOffset: 3 }],
  };
  const categoryOptions: ChartOptions<'doughnut'> = {
    responsive: true, maintainAspectRatio: false, cutout: '58%', layout: { padding: 4 },
    animation: { duration: 250 },
    plugins: {
      legend: { display: false },
      tooltip: {
        ...monthlyTooltipStyle, displayColors: false,
        callbacks: {
          title: items => wrapChartLabel(items[0].label),
          label: item => [money(item.parsed), (item.parsed / totalPositive * 100).toFixed(1) + '% of chart'],
        },
      },
    },
  };
  return <>
    <div className="metric-grid">
      <Metric label="Total spending" value={money(data.total_spending_cents)} icon="▰" color="blue" />
      <Metric label="Transactions" value={data.transaction_count} icon="⇄" color="teal" />
      <Metric label="Needs review" value={data.review_count} icon="▤" color="coral" />
      <Metric label="Merchants" value={data.merchant_count} icon="▣" color="purple" />
    </div>
    <div className="overview-grid">
      <section className="card chart-card">
        <h2>Spending this month</h2><p className="chart-note">By week · {monthName(data.month, true)}</p>
        <div className="weekly-chart-wrap"><Bar data={weeklyData} options={weeklyOptions} role="img" aria-label={'Weekly spending: ' + data.weeks.map(week => week.label + ': ' + money(week.cents)).join('; ')} /></div>
      </section>
      <section className="card chart-card">
        <h2>By category</h2>
        <div className="donut-layout">
          {totalPositive > 0 ? <div className="donut-chart-wrap">
            <Doughnut data={categoryData} options={categoryOptions} role="img" aria-label={'Category spending: ' + positive.map(item => item.category + ': ' + money(item.cents)).join('; ')} />
            <div className="donut-center"><strong>{money(data.total_spending_cents)}</strong><small>Total</small></div>
          </div> : <div className="donut" role="img" aria-label="No positive category spending"><div className="donut-center"><strong>{money(data.total_spending_cents)}</strong><small>Total</small></div></div>}
          <div className="legend">{data.categories.length ? data.categories.map(item => <div className="legend-row" key={item.category}><span className="legend-dot" style={{ background: colors[item.category] ?? colors.Other }} /><span className="legend-name">{item.category}</span><strong>{money(item.cents)}</strong></div>) : <p className="muted">No spending yet</p>}</div>
        </div>
        {data.categories.some(item => item.cents < 0) && <p className="chart-note category-refund-note">Refunds reduce the total. The ring shows categories with positive net spending.</p>}
      </section>
    </div>
    <Comparison data={data} />
    <div className="lower-grid">
      <section className="card list-card"><h2>Top merchants</h2>{data.top_merchants.length ? data.top_merchants.map((item, index) => <div className="list-row" key={item.name}><span className="rank">{index + 1}</span><span className="merchant-name">{item.name}</span><div className="merchant-track" title={item.name + ': ' + money(item.cents)}><div className="merchant-fill" style={{ width: `${Math.max(4, item.cents / maxMerchant * 100)}%` }} /></div><span className="list-amount">{money(item.cents)}</span></div>) : <div className="empty">No merchants yet</div>}</section>
      <section className="card list-card"><h2>Needs review</h2>{data.needs_review.length ? data.needs_review.map(item => <button type="button" className="list-row review-mini" key={item.id} onClick={() => correct(item)}><span className="metric-icon teal" style={{ width: 27, height: 27, fontSize: 14 }}>?</span><span className="merchant-name">{item.raw_description}<span className="review-date"> · {item.date}</span></span><span className="list-amount">{money(item.amount_cents)}</span><span className="arrow">›</span></button>) : <div className="empty">Everything has a category</div>}</section>
    </div>
  </>;
}
function OverTime({ data }: { data: SpendingHistory }) {
  const [visible, setVisible] = useState<Set<string>>(() => new Set(data.categories));
  const months = data.months;
  if (!months.length) return <div className="empty-state"><strong>No spending history yet</strong>Import a CSV to see how each category changes over time.</div>;

  const active = data.categories.filter(category => visible.has(category));
  const categoryTotals = data.categories.map(category => ({
    category,
    cents: months.reduce((sum, item) => sum + item.categories[category], 0),
  }));
  const topCategory = [...categoryTotals].sort((a, b) => b.cents - a.cents)[0];
  const axisMoney = (cents: number) => new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1,
  }).format(cents / 100);

  const chartData: ChartData<'line', number[], string> = {
    labels: months.map(item => monthName(item.month)),
    datasets: active.map(category => ({
      label: category,
      data: months.map(item => item.categories[category]),
      borderColor: trendColors[category],
      backgroundColor: trendColors[category],
      borderWidth: 3,
      pointRadius: 3.5,
      pointHoverRadius: 6,
      pointHitRadius: 18,
      pointBorderWidth: 1.5,
      pointBorderColor: '#fffdf9',
      tension: 0.25,
    })),
  };
  const chartOptions: ChartOptions<'line'> = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    interaction: { mode: 'index', intersect: false, axis: 'x' },
    plugins: {
      legend: { display: false },
      tooltip: {
        enabled: true,
        position: 'nearest',
        backgroundColor: '#35291f',
        titleColor: '#fffdf9',
        bodyColor: '#f6e8d8',
        footerColor: '#fffdf9',
        titleFont: { size: 13, weight: 'bold' },
        bodyFont: { size: 12 },
        footerFont: { size: 12, weight: 'bold' },
        padding: 13,
        cornerRadius: 10,
        bodySpacing: 6,
        boxWidth: 9,
        boxHeight: 9,
        usePointStyle: true,
        callbacks: {
          title: items => monthName(months[items[0].dataIndex].month, true),
          label: item => String(item.dataset.label) + ': ' + money(item.parsed.y ?? 0),
          footer: items => 'Total spending: ' + money(months[items[0].dataIndex].total_cents),
        },
      },
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { color: '#796957', maxRotation: 0, autoSkip: true, maxTicksLimit: 9, padding: 10 },
        border: { color: '#dbcbb8' },
      },
      y: {
        beginAtZero: true,
        grid: { color: '#ece2d5' },
        ticks: { color: '#796957', callback: value => axisMoney(Number(value)), padding: 10 },
        border: { display: false },
      },
    },
  };
  const toggle = (category: string) => setVisible(current => {
    const next = new Set(current);
    if (next.has(category) && next.size > 1) next.delete(category);
    else next.add(category);
    return next;
  });

  return <>
    <p className="section-intro">See how spending across all imported accounts changes month by month.</p>
    <div className="metric-grid">
      <Metric label="All-time spending" value={money(data.total_spending_cents)} icon="▰" color="blue" />
      <Metric label="Months with data" value={data.months_tracked} icon="▦" color="teal" />
      <Metric label="Transactions" value={data.transaction_count} icon="⇄" color="purple" />
      <Metric label="Largest category" value={topCategory?.category ?? '—'} icon="◉" color="coral" />
    </div>
    <section className="card history-card">
      <div className="history-heading">
        <div><h2>Category spending over time</h2><p>Monthly totals · {monthName(months[0].month, true)}–{monthName(months[months.length - 1].month, true)}</p></div>
        <button type="button" className="small-button" onClick={() => setVisible(new Set(data.categories))}>Show all</button>
      </div>
      <div className="trend-legend" aria-label="Categories shown in chart">
        {data.categories.map(category => <button type="button" key={category} className={visible.has(category) ? 'trend-toggle selected' : 'trend-toggle'} aria-pressed={visible.has(category)} onClick={() => toggle(category)}><span className="trend-swatch" style={{ backgroundColor: trendColors[category] }} />{category}</button>)}
      </div>
      <div className="history-chart-scroll"><div className="history-chart-wrap">
        <Line data={chartData} options={chartOptions} role="img" aria-label="Monthly spending by category. Hover over a month for exact values." />
      </div></div>
      <p className="history-note">Hover over a month to see its values. Select categories above to focus the chart; exact monthly values are below.</p>
    </section>
    <section className="card history-table-card">
      <h2>Monthly breakdown</h2>
      <div className="history-table-scroll"><table className="data-table history-table"><thead><tr><th>Month</th><th>Total</th>{data.categories.map(category => <th key={category}>{category}</th>)}</tr></thead><tbody>{[...months].reverse().map(item => <tr key={item.month}><td className="table-main">{monthName(item.month, true)}</td><td className="amount-cell">{money(item.total_cents)}</td>{data.categories.map(category => <td key={category} className="amount-cell">{money(item.categories[category])}</td>)}</tr>)}</tbody></table></div>
    </section>
  </>;
}
function Transactions({ snapshot, add, correct, remove, toggleDuplicate, moveTransaction, moving }: {
  snapshot: Snapshot;
  add: () => void;
  correct: (transaction: Transaction) => void;
  remove: (transaction: Transaction) => void;
  toggleDuplicate: (transaction: Transaction) => void;
  moveTransaction: (transaction: Transaction, category: string) => Promise<void>;
  moving: boolean;
}) {
  const rows = snapshot.transactions;
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dropCategory, setDropCategory] = useState<string | null>(null);
  const dragged = rows.find(item => item.id === draggedId);
  const endDrag = () => { setDraggedId(null); setDropCategory(null); };
  const startDrag = (event: DragEvent<HTMLElement>, item: Transaction) => {
    if (moving) { event.preventDefault(); return; }
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', item.id);
    setDraggedId(item.id);
  };
  const overCategory = (event: DragEvent<HTMLElement>, category: string) => {
    if (!dragged || dragged.category === category || moving) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    if (dropCategory !== category) setDropCategory(category);
  };
  const leaveCategory = (event: DragEvent<HTMLElement>, category: string) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null) && dropCategory === category) setDropCategory(null);
  };
  const drop = (event: DragEvent<HTMLElement>, category: string) => {
    event.preventDefault();
    const item = rows.find(row => row.id === event.dataTransfer.getData('text/plain'));
    endDrag();
    if (item && item.category !== category && !moving) void moveTransaction(item, category);
  };
  if (!rows.length) return <><p className="section-intro">Transactions grouped by category. Correct one purchase or every transaction from its merchant.</p><div className="empty-state"><strong>No transactions for this month</strong><p>Import a CSV or add one transaction.</p><button type="button" className="button primary" onClick={add}>＋ Add transaction</button></div></>;
  return <>
    <p className="section-intro">Drag a transaction into another category to change only that purchase. Use Correct to update every transaction from a merchant.</p>
    <div className="toolbar"><span className="count">{rows.length} transactions · {monthName(snapshot.dashboard.month, true)}</span><div className="transaction-toolbar-actions"><span className="board-hint">Scroll to explore categories →</span><button type="button" className="button primary" onClick={add}>＋ Add transaction</button></div></div>
    <div className="category-board" role="region" aria-label="Transactions by category" tabIndex={0}>
      {snapshot.categories.map(category => {
        const items = rows.filter(item => item.category === category);
        const total = items.reduce((sum, item) => sum + (item.excluded_from_spending ? 0 : item.amount_cents), 0);
        const canDrop = !!dragged && dragged.category !== category && !moving;
        return <section
          className={`category-column ${dropCategory === category && canDrop ? 'drop-target' : ''}`}
          style={categoryStyle(category)} aria-label={`${category} transactions`} key={category}
          onDragOver={event => overCategory(event, category)}
          onDragLeave={event => leaveCategory(event, category)}
          onDrop={event => drop(event, category)}
        >
          <header className="category-column-header"><div className="category-column-title"><span className="category-dot" /><h2>{category}</h2><span className="category-count">{items.length}</span></div><p>{money(total)} total</p></header>
          <div className="category-card-list">
            {items.length ? items.map(item => <article
              className={`transaction-card ${item.needs_review ? 'needs-review' : ''} ${draggedId === item.id ? 'dragging' : ''}`}
              key={item.id} draggable={!moving} onDragStart={event => startDrag(event, item)} onDragEnd={endDrag}
              title="Drag to another category"
            >
              <div className="transaction-card-main"><strong className="transaction-merchant-name">{item.normalized_merchant}</strong><strong className="transaction-amount">{money(item.amount_cents)}</strong></div>
              {item.raw_description.trim().toLocaleLowerCase() !== item.normalized_merchant.trim().toLocaleLowerCase() && <div className="transaction-original" title={item.raw_description}>{item.raw_description}</div>}
              <div className="transaction-card-meta"><time dateTime={item.date}>{item.date}</time><span className={`transaction-source ${item.needs_review ? 'review' : ''}`}>{item.excluded_from_spending ? 'Not in totals' : sourceLabel(item.classification_source)}</span>{item.possible_duplicate && <span className="transaction-source review">{item.excluded_from_spending ? 'Excluded duplicate' : 'Possible duplicate'}</span>}</div>
              <div className="transaction-card-actions">
                {item.possible_duplicate && <button type="button" className="small-button" onClick={() => toggleDuplicate(item)}>{item.excluded_from_spending ? 'Include in totals' : 'Exclude from totals'}</button>}
                <button type="button" className="small-button" onClick={() => correct(item)} aria-label={`Correct ${item.raw_description}`}>Correct</button>
                <button type="button" className="small-button danger" onClick={() => remove(item)} aria-label={`Delete ${item.raw_description} from ${item.date}`}>Delete</button>
              </div>
            </article>) : <p className="category-empty">{canDrop ? `Drop here for ${category}` : 'No transactions this month'}</p>}
          </div>
        </section>;
      })}
    </div>
  </>;
}
function Review({ snapshot, correct, retry, busy }: { snapshot: Snapshot; correct: (transaction: Transaction) => void; retry: () => void; busy: boolean }) {
  const rows = snapshot.transactions.filter(item => item.needs_review);
  const retryable = rows.some(item => item.classification_source === 'unclassified' && !item.source.startsWith('synthetic-'));
  return <><p className="section-intro">Transactions without an accepted category. Correct one purchase or every transaction from its merchant.</p>{rows.length ? <><div className="toolbar"><span className="count">{rows.length} transactions · {monthName(snapshot.dashboard.month, true)}</span>{retryable && (snapshot.jev_configured ? <button type="button" className="small-button" onClick={retry} disabled={busy}>{busy ? 'Classifying…' : 'Retry with Jev'}</button> : <span className="count">Jev key missing — add it to .env, then retry</span>)}</div><div className="card table-card"><table className="data-table"><thead><tr><th>Merchant / description</th><th>Date</th><th>Amount</th><th>Category</th><th>Source</th><th></th></tr></thead><tbody>{rows.map(item => <tr key={item.id}><td><div className="table-main">{item.raw_description}</div><div className="table-sub">{item.normalized_merchant}</div></td><td>{item.date}</td><td className="amount-cell">{money(item.amount_cents)}</td><td><span className="badge review">{item.category}</span></td><td>{item.excluded_from_spending ? 'Excluded from spending' : sourceLabel(item.classification_source)}</td><td><button type="button" className="small-button" onClick={() => correct(item)}>Correct</button></td></tr>)}</tbody></table></div></> : <div className="empty-state"><strong>No transactions need review</strong>All transactions have a category.</div>}</>;
}
function Merchants({ items }: { items: Mapping[] }) {
  if (!items.length) return <div className="empty-state"><strong>No saved merchants yet</strong>Import a file to build your merchant list.</div>;
  return <><p className="section-intro">Saved categories become the default for future transactions from each merchant.</p><div className="toolbar"><span className="count">{items.length} saved merchant mappings</span></div><div className="card table-card"><table className="data-table"><thead><tr><th>Merchant</th><th>Saved category</th><th>Source</th><th>Confidence</th><th>Updated</th></tr></thead><tbody>{items.map(item => <tr key={item.normalized_merchant}><td><div className="table-main">{item.display_name}</div><div className="table-sub">{item.normalized_merchant}</div></td><td><span className={`badge ${item.source === 'manual' ? 'manual' : item.source === 'synthetic' ? 'synthetic' : ''}`}>{item.category}</span></td><td>{sourceLabel(item.source)}</td><td>{item.confidence == null ? '—' : `${Math.round(item.confidence * 100)}%`}</td><td>{item.updated_at.slice(0, 10)}</td></tr>)}</tbody></table></div></>;
}
async function fileBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  return btoa(binary);
}
const copySettings = (settings: ImportSettings): ImportSettings => ({ ...settings, column_mapping: { ...settings.column_mapping }, ignore_rules: settings.ignore_rules.map(rule => ({ ...rule })) });
function PresetSettingsEditor({ preset, settings, change, reset, busy }: {
  preset: ImportPreset; settings: ImportSettings; change: (settings: ImportSettings) => void;
  reset: () => void; busy: boolean;
}) {
  const field = (key: keyof ImportSettings, value: string | boolean) => change({ ...settings, [key]: value });
  const column = (key: string, value: string) => change({ ...settings, column_mapping: { ...settings.column_mapping, [key]: value } });
  const updateRule = (index: number, patch: Partial<IgnoreRule>) => change({ ...settings, ignore_rules: settings.ignore_rules.map((rule, i) => i === index ? { ...rule, ...patch } : rule) });
  return <fieldset className="preset-settings" disabled={busy}>
    <div className="settings-heading"><h3>Import settings</h3><button type="button" className="small-button" onClick={reset}>Reset defaults</button></div>
    <p className="file-hint">Adjust for this import, or choose Save settings to reuse these settings next time.</p>
    <div className="settings-grid">
      <div><label htmlFor="preset-date-format">Date format</label><select id="preset-date-format" value={settings.date_format} onChange={event => field('date_format', event.target.value)}><option value="auto">Auto · ISO, US, or Excel dates</option><option value="iso">YYYY-MM-DD</option><option value="us">MM/DD/YYYY</option></select></div>
      <div><label htmlFor="preset-amount-convention">Expenses in file</label><select id="preset-amount-convention" value={settings.amount_convention} onChange={event => field('amount_convention', event.target.value)}><option value="expenses_positive">Positive amounts</option><option value="expenses_negative">Negative amounts</option></select></div>
    </div>
    <p className="settings-explanation">{settings.amount_convention === 'expenses_negative' ? 'Negative charges become spending. Positive refunds reduce spending.' : 'Positive charges become spending. Negative refunds reduce spending.'}</p>
    <h4>CSV columns</h4>
    <div className="settings-grid">
      {([['date', 'Date column'], ['description', 'Description column'], ['amount', 'Amount column']] as const).map(([key, label]) => <div key={key}><label htmlFor={`preset-column-${key}`}>{label}</label><input id={`preset-column-${key}`} value={settings.column_mapping[key] ?? ''} required onChange={event => column(key, event.target.value)} /></div>)}
      <div><label htmlFor="preset-merchant-column">Merchant column (optional)</label><input id="preset-merchant-column" placeholder="Use description when blank" value={settings.merchant_column} onChange={event => field('merchant_column', event.target.value)} /></div>
    </div>
    <p className="settings-explanation">Enter the CSV header names. The full description is preserved; the merchant column is used for classification.</p>
    <h4>Rows to skip</h4>
    <label className="settings-check"><input type="checkbox" checked={settings.skip_payments} onChange={event => field('skip_payments', event.target.checked)} />Skip card payments</label>
    <div className="settings-grid">
      <div><label htmlFor="preset-payment-column">Payment column</label><input id="preset-payment-column" value={settings.payment_column} disabled={!settings.skip_payments} required={settings.skip_payments} onChange={event => field('payment_column', event.target.value)} /></div>
      <div><label htmlFor="preset-payment-value">Skip when value equals</label><input id="preset-payment-value" value={settings.payment_value} disabled={!settings.skip_payments} required={settings.skip_payments} onChange={event => field('payment_value', event.target.value)} /></div>
    </div>
    <label className="settings-check"><input type="checkbox" checked={settings.skip_non_posted} onChange={event => field('skip_non_posted', event.target.checked)} />Keep only posted transactions</label>
    <div className="settings-grid">
      <div><label htmlFor="preset-status-column">Status column (if present)</label><input id="preset-status-column" value={settings.status_column} disabled={!settings.skip_non_posted} required={settings.skip_non_posted} onChange={event => field('status_column', event.target.value)} /></div>
      <div><label htmlFor="preset-posted-value">Keep when value equals</label><input id="preset-posted-value" value={settings.posted_value} disabled={!settings.skip_non_posted} required={settings.skip_non_posted} onChange={event => field('posted_value', event.target.value)} /></div>
    </div>
    <p className="settings-explanation">Values match exactly, ignoring case and surrounding spaces. If the status column is absent, all statuses are imported.</p>
    <div className="settings-heading ignore-heading"><h4>Additional rows to ignore</h4><button type="button" className="small-button" disabled={settings.ignore_rules.length >= 30} onClick={() => change({ ...settings, ignore_rules: [...settings.ignore_rules, { column: '', operator: 'equals', value: '' }] })}>＋ Add rule</button></div>
    <p className="settings-explanation">Skip a row if it matches any rule. Matches ignore case and surrounding spaces. For example: Description equals ONLINE PAYMENT THANK YOU.</p>
    {settings.ignore_rules.map((rule, index) => <div className="ignore-rule" key={index}>
      <div><label htmlFor={`ignore-column-${index}`}>CSV column</label><input id={`ignore-column-${index}`} value={rule.column} placeholder="e.g. Description" required maxLength={500} onChange={event => updateRule(index, { column: event.target.value })} /></div>
      <div><label htmlFor={`ignore-operator-${index}`}>Match</label><select id={`ignore-operator-${index}`} value={rule.operator} onChange={event => updateRule(index, { operator: event.target.value as IgnoreRule['operator'] })}><option value="equals">Equals</option><option value="contains">Contains</option><option value="starts_with">Starts with</option></select></div>
      <div><label htmlFor={`ignore-value-${index}`}>Value to ignore</label><input id={`ignore-value-${index}`} value={rule.value} placeholder="e.g. CARD PAYMENT" required maxLength={500} onChange={event => updateRule(index, { value: event.target.value })} /></div>
      <button type="button" className="small-button remove-rule" aria-label={`Remove ignore rule ${index + 1}`} onClick={() => change({ ...settings, ignore_rules: settings.ignore_rules.filter((_, i) => i !== index) })}>Remove</button>
    </div>)}
    <details className="settings-advanced"><summary>Transaction IDs and matching</summary>
      <label htmlFor="preset-column-id">Transaction ID column (optional)</label><input id="preset-column-id" placeholder="No transaction ID column" value={settings.column_mapping.id ?? ''} onChange={event => column('id', event.target.value)} />
      {preset.base_preset === 'robinhood' && <>
        <label className="settings-check"><input type="checkbox" checked={settings.match_rows} onChange={event => field('match_rows', event.target.checked)} />Match transactions across overlapping exports</label>
        <p className="settings-explanation">When no transaction ID is supplied, matching also uses these columns, the date, amount, merchant, and description.</p>
        <div className="settings-grid">
          <div><label htmlFor="preset-time-column">Time column</label><input id="preset-time-column" value={settings.time_column} required={settings.match_rows} disabled={!settings.match_rows} onChange={event => field('time_column', event.target.value)} /></div>
          <div><label htmlFor="preset-type-column">Transaction type column</label><input id="preset-type-column" value={settings.type_column} required={settings.match_rows} disabled={!settings.match_rows} onChange={event => field('type_column', event.target.value)} /></div>
          <div><label htmlFor="preset-cardholder-column">Cardholder column (optional)</label><input id="preset-cardholder-column" value={settings.cardholder_column} disabled={!settings.match_rows} onChange={event => field('cardholder_column', event.target.value)} /></div>
        </div>
      </>}
    </details>
  </fieldset>;
}
function Dialog({ modal, snapshot, close, onAdded, onCorrected, onDeleted, onImported, onSettingsSaved, notify }: { modal: Modal; snapshot: Snapshot; close: () => void; onAdded: (transaction: Transaction) => Promise<void>; onCorrected: (scope: string, count: number) => Promise<void>; onDeleted: () => Promise<void>; onImported: (report: ImportReport) => Promise<void>; onSettingsSaved: () => Promise<void>; notify: (message: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [presetId, setPresetId] = useState('');
  const [settings, setSettings] = useState<ImportSettings | null>(null);
  const [account, setAccount] = useState('checking');
  const [presetName, setPresetName] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedPreset, setSavedPreset] = useState<ImportPreset | null>(null);
  const [saveStatus, setSaveStatus] = useState('');
  const preset = presetId === 'new' ? { id: 'new', label: presetName || 'New card', source: account, base_preset: 'custom', settings: snapshot.new_card_settings, default_settings: snapshot.new_card_settings } : snapshot.import_presets.find(item => item.id === presetId) ?? (savedPreset?.id === presetId ? savedPreset : undefined);
  const saveSettings = async () => {
    if (!preset || !settings) return;
    setBusy(true); setSaving(true); setSaveStatus('');
    try {
      const result = await post<ImportPreset>('/api/import-presets', { id: presetId === 'new' ? null : presetId, label: presetName, source: account, base_preset: preset.base_preset, settings });
      setSavedPreset(result); setPresetId(result.id); setSettings(copySettings(result.settings));
      setAccount(result.source); setPresetName(result.label);
      await onSettingsSaved();
      setSaveStatus('Settings saved. This preset is ready for future imports.');
      notify('Import settings saved');
    } catch (error) { notify((error as Error).message); }
    finally { setBusy(false); setSaving(false); }
  };
  if (!modal) return null;
  const adding = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (modal.kind !== 'add') return; setBusy(true); const form = new FormData(event.currentTarget); try { const transaction = await post<Transaction>('/api/transactions', { date: form.get('date'), description: form.get('description'), amount: form.get('amount'), category: form.get('category'), source: form.get('source'), demo: snapshot.dashboard.month === 'synthetic' }); await onAdded(transaction); } catch (error) { notify((error as Error).message); setBusy(false); } };
  const correction = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (modal.kind !== 'correct') return; setBusy(true); const form = new FormData(event.currentTarget); try { const result = await post<{ scope: string; updated_transactions: number }>('/api/correct', { transaction_id: modal.transaction.id, category: form.get('category'), scope: form.get('scope') }); await onCorrected(result.scope, result.updated_transactions); } catch (error) { notify((error as Error).message); setBusy(false); } };
  const deletion = async () => { if (modal.kind !== 'delete' || busy) return; setBusy(true); try { await request(`/api/transactions/${encodeURIComponent(modal.transaction.id)}`, { method: 'DELETE' }); await onDeleted(); } catch (error) { notify((error as Error).message); setBusy(false); } };
  const importing = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget); const file = form.get('file'); if (!(file instanceof File) || !file.size || file.size > 5_000_000) { notify('Choose a file smaller than 5 MB'); setBusy(false); return; } try { const report = await post<ImportReport>('/api/import', { filename: file.name, content_base64: await fileBase64(file), source: form.get('source'), import_preset: presetId === 'new' ? 'custom' : presetId || null, import_settings: preset ? settings : null, date_format: form.get('date_format') ?? 'iso', amount_convention: form.get('amount_convention') ?? 'expenses_positive', column_mapping: preset ? null : customColumnMapping(form), currency: 'USD' }); await onImported(report); } catch (error) { notify((error as Error).message); setBusy(false); } };
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}><div className={`modal ${modal.kind === 'import' ? 'import-modal' : ''}`} role="dialog" aria-modal="true" aria-labelledby="dialog-title">
    {modal.kind === 'add' ? <>
      <h2 id="dialog-title">Add transaction</h2>
      <p>Enter one purchase or refund. It will appear in your transactions and spending views.</p>
      <form onSubmit={adding}>
        <div className="manual-entry-grid"><div><label htmlFor="add-date">Date</label><input id="add-date" name="date" type="date" defaultValue={defaultTransactionDate(snapshot.dashboard.month)} min={snapshot.dashboard.month === 'synthetic' ? '2026-06-01' : undefined} max={snapshot.dashboard.month === 'synthetic' ? '2026-06-30' : undefined} required disabled={busy} /></div><div><label htmlFor="add-amount">Amount ($)</label><input id="add-amount" name="amount" type="text" inputMode="decimal" placeholder="25.00" pattern="-?[0-9]+(\.[0-9]{1,2})?" title="Enter dollars and cents, such as 25.00 or -5.00 for a refund" required disabled={busy} /></div></div>
        <p className="file-hint">Use a positive amount for spending or a negative amount for a refund.</p>
        <label htmlFor="add-description">Merchant or description</label><input id="add-description" name="description" maxLength={500} placeholder="e.g. Neighborhood coffee shop" required disabled={busy} />
        <label htmlFor="add-category">Category</label><select id="add-category" name="category" defaultValue="" required disabled={busy}><option value="" disabled>Choose a category</option>{snapshot.categories.map(category => <option key={category} value={category}>{category}</option>)}</select>
        <label htmlFor="add-source">Account name</label><input id="add-source" name="source" defaultValue="manual" maxLength={100} required disabled={busy} />
        <p className="file-hint">A manual entry does not change saved merchant categories or call Jev. {snapshot.dashboard.month === 'synthetic' ? 'Entries in Synthetic are for this demo session only.' : 'The account name helps identify where this transaction came from.'}</p>
        <div className="modal-actions"><button type="button" className="button secondary" onClick={close} disabled={busy}>Cancel</button><button type="submit" className="button primary" disabled={busy}>{busy ? 'Adding…' : 'Add transaction'}</button></div>
      </form>
    </> : modal.kind === 'correct' ? <><h2 id="dialog-title">Correct category</h2><p><strong>{modal.transaction.raw_description}</strong> is currently in <strong>{modal.transaction.category}</strong>.</p><form onSubmit={correction}><label htmlFor="correct-category">Correct category</label><select id="correct-category" name="category" defaultValue={modal.transaction.category}>{snapshot.categories.map(category => <option key={category}>{category}</option>)}</select><div className="scope-choice"><label><input type="radio" name="scope" value="transaction" defaultChecked /> This transaction only<span>Changes this row. The saved merchant category stays the same.</span></label><label><input type="radio" name="scope" value="merchant" /> All from this merchant<span>Updates existing rows and saves the category for future transactions.</span></label></div><div className="modal-actions"><button type="button" className="button secondary" onClick={close}>Cancel</button><button type="submit" className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save correction'}</button></div></form></> : modal.kind === 'delete' ? <>
      <h2 id="dialog-title">Delete transaction?</h2>
      <p><strong>{modal.transaction.raw_description}</strong> · {modal.transaction.date} · {money(modal.transaction.amount_cents)}</p>
      <p>This removes only this transaction from your spending history and updates the totals. The saved merchant category will stay in place.</p>
      <div className="modal-actions"><button type="button" className="button secondary" onClick={close} disabled={busy}>Cancel</button><button type="button" className="button danger" onClick={deletion} disabled={busy}>{busy ? 'Deleting…' : 'Delete transaction'}</button></div>
    </> : modal.kind === 'import' ? <>
      <h2 id="dialog-title">Import transactions</h2>
      <p>Select a card preset or configure a custom import. Category labels in the file are ignored.</p>
      <form onSubmit={importing}>
        <label htmlFor="import-preset">Card / import format</label>
        <select id="import-preset" value={presetId} disabled={busy} onChange={event => {
          const selected = snapshot.import_presets.find(item => item.id === event.target.value);
          setPresetId(event.target.value);
          setSettings(selected ? copySettings(selected.settings) : event.target.value === 'new' ? copySettings(snapshot.new_card_settings) : null);
          setAccount(selected?.source ?? (event.target.value === 'new' ? '' : 'checking'));
          setPresetName(selected?.label ?? ''); setSaveStatus('');
        }}>
          <option value="">Custom / other account</option>
          <option value="new">＋ New card preset</option>
          {snapshot.import_presets.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
        {preset && <><label htmlFor="preset-name">Preset name</label><input id="preset-name" value={presetName} onChange={event => { setPresetName(event.target.value); setSaveStatus(''); }} maxLength={100} required disabled={busy} /></>}
        <label htmlFor="import-file">{preset ? 'CSV file' : 'CSV or JSON file'}</label>
        <input id="import-file" name="file" type="file" accept={preset ? '.csv' : '.csv,.json'} required disabled={busy} />
        <label htmlFor="import-source">Account name</label>
        <input id="import-source" name="source" value={account} onChange={event => { setAccount(event.target.value); setSaveStatus(''); }} maxLength={100} required disabled={busy} />
        <p className="file-hint">Keep the same account name for future imports from this card. Use different names for different accounts.</p>
        {preset && settings && <PresetSettingsEditor preset={preset} settings={settings} change={value => { setSettings(value); setSaveStatus(''); }} reset={() => { setSettings(copySettings(preset.default_settings)); setSaveStatus(''); }} busy={busy} />}
        {!preset && <>
          <div className="scope-choice"><div><label htmlFor="date-format">Date format</label><select id="date-format" name="date_format"><option value="iso">YYYY-MM-DD</option><option value="us">MM/DD/YYYY</option></select></div><div><label htmlFor="amount-convention">Expenses in file</label><select id="amount-convention" name="amount_convention"><option value="expenses_positive">Positive amounts</option><option value="expenses_negative">Negative amounts</option></select></div></div>
          <details className="column-mapping"><summary>Custom column mapping (optional)</summary><p>Use this when your CSV headers do not match. Enter the exact headers for all three required fields. A successful import saves this mapping for the account name above.</p><div className="column-mapping-fields"><label htmlFor="date-column">Date column</label><input id="date-column" name="date_column" placeholder="e.g. Posted On" /><label htmlFor="description-column">Description column</label><input id="description-column" name="description_column" placeholder="e.g. Transaction Details" /><label htmlFor="amount-column">Amount column</label><input id="amount-column" name="amount_column" placeholder="e.g. Charge" /><label htmlFor="id-column">Transaction ID column (optional)</label><input id="id-column" name="id_column" placeholder="e.g. Reference Number" /></div></details>
        </>}
        <p className="file-hint">{snapshot.jev_configured ? 'Jev key found. Unknown merchants will be sent to Jev.' : 'Jev key missing. Unknown merchants will go to Other for review. Add JEVMODEL_API_KEY to .env to enable Jev.'}</p>
        {saveStatus && <p className="settings-saved" role="status">{saveStatus}</p>}
        <div className="modal-actions import-actions"><button type="button" className="button secondary" onClick={close} disabled={busy}>Cancel</button>{preset && <button type="button" className="button save-settings" disabled={busy} onClick={saveSettings}>{saving ? 'Saving…' : 'Save settings'}</button>}<button type="submit" className="button primary" disabled={busy}>{busy && !saving ? 'Importing…' : 'Import file'}</button></div>
      </form>
    </> : <>
      <h2 id="dialog-title">Import report</h2>
      <p>{modal.report.rows_imported} imported · {modal.report.duplicates_skipped} duplicates skipped · {modal.report.payments_skipped} payments skipped · {modal.report.non_posted_skipped} non-posted skipped · {modal.report.rules_skipped ?? 0} ignored by rules · {modal.report.rows_failed} rejected · {modal.report.jev_attempts} Jev attempts</p>
      {!!modal.report.possible_duplicates && <p><strong>{modal.report.possible_duplicates} possible duplicate transactions were imported.</strong> The file has no reliable transaction IDs, so these rows were kept to avoid losing legitimate repeated purchases. Inspect the marked cards on the Transactions page and use Exclude from totals for any repeated rows.</p>}
      {!!modal.report.jev_skipped_no_key && <p><strong>Jev was not called:</strong> no API key was configured. {modal.report.jev_skipped_no_key} new merchants went to Other. Add the key to .env, then use Retry with Jev on the Review page.</p>}
      {!!modal.report.jev_failures && <p><strong>Jev could not classify {modal.report.jev_failures} merchants.</strong> {(modal.report.jev_errors ?? []).join('; ') || 'Check your key and API balance, then retry.'}</p>}
      {!!modal.report.errors?.length && <ul className="import-errors">{modal.report.errors.slice(0, 10).map((item, index) => <li key={index}>Row {item.line}: {item.message}</li>)}</ul>}
      <div className="modal-actions"><button type="button" className="button primary" onClick={close}>Done</button></div>
    </>}
  </div></div>;
}
function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [page, setPage] = useState<Page>('overview');
  const [lastDashboardPage, setLastDashboardPage] = useState<'overview' | 'over_time'>('overview');
  const [modal, setModal] = useState<Modal>(null);
  const handleImported = async (report: ImportReport) => {
    setModal(null);
    await load();
    notify(`Imported ${report.rows_imported} rows · ${report.duplicates_skipped} duplicates skipped · ${report.payments_skipped} payments skipped · ${report.rows_failed} rejected`);
    if (report.payments_skipped || report.non_posted_skipped || report.rules_skipped || report.possible_duplicates || report.rows_failed || report.jev_failures || report.jev_skipped_no_key) {
      setModal({ kind: 'report', report });
    }
  };
  const [message, setMessage] = useState('');
  const [loadingError, setLoadingError] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [moving, setMoving] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const notify = useCallback((value: string) => { setMessage(value); if (toastTimer.current) clearTimeout(toastTimer.current); toastTimer.current = setTimeout(() => setMessage(''), 4200); }, []);
  const load = useCallback(async (month?: string) => { const data = await request<Snapshot>(`/api/state${month ? `?month=${encodeURIComponent(month)}` : ''}`); setSnapshot(data); setLoadingError(''); const url = new URL(window.location.href); url.searchParams.set('month', data.dashboard.month); window.history.replaceState(null, '', url); }, []);
  useEffect(() => { load(new URLSearchParams(window.location.search).get('month') || undefined).catch(error => setLoadingError((error as Error).message)); }, [load]);
  useEffect(() => { const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setModal(null); }; document.addEventListener('keydown', escape); return () => document.removeEventListener('keydown', escape); }, []);
  const retry = async () => { setRetrying(true); try { const result = await post<{ transactions_updated: number; jev_failures: number; jev_errors?: string[] }>('/api/retry-classification', {}); await load(snapshot?.dashboard.month); notify(result.jev_failures ? `Classified ${result.transactions_updated}; ${result.jev_failures} Jev failures: ${(result.jev_errors ?? []).join('; ')}` : `Classified ${result.transactions_updated} transactions`); } catch (error) { notify((error as Error).message); } finally { setRetrying(false); } };
  const toggleDuplicate = async (transaction: Transaction) => {
    const excluded = !transaction.excluded_from_spending;
    try {
      await post('/api/duplicate-exclusion', { transaction_id: transaction.id, excluded });
      await load(snapshot?.dashboard.month);
      notify(excluded ? 'Excluded the possible duplicate from spending totals' : 'Included the transaction in spending totals');
    } catch (error) { notify((error as Error).message); }
  };
  const moveTransaction = async (transaction: Transaction, category: string) => {
    setMoving(true);
    try {
      await post('/api/correct', { transaction_id: transaction.id, category, scope: 'transaction' });
      await load(snapshot?.dashboard.month);
      notify(`Moved transaction to ${category}`);
    } catch (error) { notify((error as Error).message); }
    finally { setMoving(false); }
  };
  const isDashboard = page === 'overview' || page === 'over_time';
  const selectDashboardView = (view: 'overview' | 'over_time') => { setPage(view); setLastDashboardPage(view); };
  const titles: Record<Page, string> = { overview: 'Dashboard', over_time: 'Dashboard', transactions: 'Transactions', merchants: 'Merchants', review: 'Needs review' };
  return <><div className="app-shell"><aside className="sidebar"><div className="brand">Spend<span>Tracker</span></div><nav aria-label="Main navigation">{([['overview', '⌂'], ['transactions', '▤'], ['merchants', '▣'], ['review', '◎']] as const).map(([item, icon]) => <button type="button" key={item} className={`nav-item ${(item === 'overview' ? isDashboard : page === item) ? 'active' : ''}`} onClick={() => setPage(item === 'overview' ? lastDashboardPage : item)}><span className="nav-icon">{icon}</span>{item === 'review' ? 'Review' : titles[item]}{item === 'review' && !!snapshot?.dashboard.review_count && <span className="nav-count">{snapshot.dashboard.review_count}</span>}</button>)}</nav><div className="sidebar-bottom"><button type="button" className="import-button" onClick={() => snapshot && setModal({ kind: 'import' })}>＋ Import CSV</button><button type="button" className="manual-entry-button" onClick={() => snapshot && setModal({ kind: 'add' })}>＋ Add transaction</button><p>Private, local spending view</p></div></aside><main className="main"><header className="topbar"><div><p className="eyebrow">{isDashboard ? 'PERSONAL FINANCE' : 'SPEND TRACKER'}</p><h1>{titles[page]}</h1></div><div className="top-actions">{snapshot?.demo && <span className="demo-pill">SYNTHETIC DATA</span>}{<label className="month-select">Month <select aria-label="Select month" value={snapshot?.dashboard.month ?? ''} onChange={event => load(event.target.value).catch(error => notify((error as Error).message))}>{(snapshot?.dashboard.available_months ?? []).map(month => <option value={month} key={month}>{monthName(month)}</option>)}</select></label>}</div></header>{isDashboard && <nav className="view-nav" aria-label="Dashboard views"><button type="button" className={page === 'overview' ? 'view-tab active' : 'view-tab'} aria-current={page === 'overview' ? 'page' : undefined} onClick={() => selectDashboardView('overview')}>Monthly view</button><button type="button" className={page === 'over_time' ? 'view-tab active' : 'view-tab'} aria-current={page === 'over_time' ? 'page' : undefined} onClick={() => selectDashboardView('over_time')}>Over time</button></nav>}<div className="content" aria-live="polite">{snapshot ? page === 'overview' ? <Overview data={snapshot.dashboard} correct={transaction => setModal({ kind: 'correct', transaction })} /> : page === 'over_time' ? <OverTime data={snapshot.history} /> : page === 'transactions' ? <Transactions snapshot={snapshot} add={() => setModal({ kind: 'add' })} correct={transaction => setModal({ kind: 'correct', transaction })} remove={transaction => setModal({ kind: 'delete', transaction })} toggleDuplicate={toggleDuplicate} moveTransaction={moveTransaction} moving={moving} /> : page === 'merchants' ? <Merchants items={snapshot.merchants} /> : <Review snapshot={snapshot} correct={transaction => setModal({ kind: 'correct', transaction })} retry={retry} busy={retrying} /> : <div className="empty-state"><strong>{loadingError ? 'Could not load Spend Tracker' : 'Loading Spend Tracker…'}</strong>{loadingError}</div>}</div></main></div>{snapshot && <Dialog key={modal?.kind === 'correct' || modal?.kind === 'delete' ? `${modal.kind}-${modal.transaction.id}` : modal?.kind ?? 'none'} modal={modal} snapshot={snapshot} close={() => setModal(null)} notify={notify} onAdded={async transaction => { setModal(null); await load(snapshot.dashboard.month === 'synthetic' ? 'synthetic' : transaction.date.slice(0, 7)); setPage('transactions'); notify('Added transaction'); }} onCorrected={async (scope, count) => { setModal(null); await load(snapshot.dashboard.month); notify(scope === 'merchant' ? `Updated ${count} ${count === 1 ? 'transaction' : 'transactions'} and saved the merchant category` : 'Updated this transaction'); }} onDeleted={async () => { setModal(null); await load(snapshot.dashboard.month); notify('Deleted transaction'); }} onImported={handleImported} onSettingsSaved={async () => { await load(snapshot.dashboard.month); }} />}{message && <div id="toast" className="show" role="status" aria-live="polite">{message}</div>}</>;
}

createRoot(document.getElementById('root')!).render(<App />);
