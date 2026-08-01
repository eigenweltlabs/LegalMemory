import { useState } from "react";
import { Download, FileText, Play, ReceiptText, Sparkles } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { EmptyState, Metric, SectionHeading } from "../components/Primitives";

export default function CostsPage({ identity }) {
  const costs = useApi("/api/costs", [], Boolean(identity?.is_admin));
  const data = costs.data || { total_usd: 0, input_tokens: 0, output_tokens: 0, calls: 0, unpriced_models: [], by_model: [], by_stage: [] };
  const totalTokens = data.input_tokens + data.output_tokens;
  const unpriced = data.unpriced_models || [];
  // A model the gateway has no rate for consumes real tokens at $0.00. Charting USD
  // would then draw every stage as an empty bar, so the chart measures whatever is
  // actually being spent — dollars when they are known, tokens otherwise.
  const metric = data.total_usd ? "cost_usd" : "input_tokens";
  const peak = Math.max(...data.by_stage.map((row) => row[metric] || 0), 0);
  const exportCsv = () => {
    const rows = [
      ["section", "name", "calls", "input_tokens", "output_tokens", "cost_usd"],
      ...data.by_model.map((item) => ["model", item.model, item.calls, item.input_tokens, item.output_tokens, item.cost_usd]),
      ...data.by_stage.map((item) => ["stage", item.stage, item.calls, item.input_tokens, item.output_tokens, item.cost_usd]),
      ["total", "all", data.calls, data.input_tokens, data.output_tokens, data.total_usd]
    ];
    const url = URL.createObjectURL(new Blob([rows.map((row) => row.map(csvField).join(",")).join("\n")], { type: "text/csv" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: "knowledge-index-costs.csv" });
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <>
      <div className="hero-row compact-hero"><div><h1>Costs</h1></div><div className="hero-actions"><button className="secondary-button" onClick={exportCsv}><Download size={15} /> Export CSV</button></div></div>
      <div className="metric-grid metric-grid-four">
        <Metric label="Total model cost" value={`$${data.total_usd.toFixed(2)}`} note={unpriced.length ? `${unpriced.length} model(s) have no rate` : "all calls priced"} accent />
        <Metric label="Input tokens" value={compact(data.input_tokens)} note={data.input_tokens ? "documents and prompts" : "nothing sent yet"} />
        <Metric label="Output tokens" value={compact(data.output_tokens)} note={data.output_tokens ? "structured extraction" : "nothing returned yet"} />
        <Metric label="Model calls" value={compact(data.calls)} note={data.calls ? `${Math.round(totalTokens / data.calls).toLocaleString()} tokens / call` : "no calls recorded"} />
      </div>
      {unpriced.length > 0 && <div className="form-note">Token counts are measured, USD is not: the gateway has no rate for <code className="mono">{unpriced.join(", ")}</code>. Set <code className="mono">input_cost_per_token</code> / <code className="mono">output_cost_per_token</code> on that model in the gateway config.</div>}

      <section className="panel cost-chart-panel">
        <SectionHeading title={data.total_usd ? "Cost by pipeline stage" : "Input tokens by pipeline stage"} />
        {data.by_stage.length ? <div className="horizontal-chart">{data.by_stage.map((item) => <div className="chart-row" key={item.stage}><span>{human(item.stage)}</span><div><i style={{ width: `${Math.max(3, ((item[metric] || 0) / (peak || 1)) * 100)}%` }} /></div><strong>{data.total_usd ? `$${item.cost_usd.toFixed(2)}` : compact(item.input_tokens)}</strong></div>)}</div> : <EmptyState title="No usage recorded yet" copy="Usage events appear after the next insertion run or search." />}
      </section>

      <section className="panel model-cost-table">
        <SectionHeading title="Usage by model" action={<span className="table-count">{data.by_model.length} model(s)</span>} />
        {data.by_model.length ? <div className="data-table"><div className="table-head cost-head"><span>Model</span><span>Calls</span><span>Input</span><span>Output</span><span>Cost</span></div>{data.by_model.map((item) => <div className="table-row cost-head" key={item.model}><span className="primary-cell"><i className="model-mini-icon"><Sparkles size={14} /></i><span><strong>{item.model}</strong><small>{routedVia(item)}</small></span></span><span className="mono">{item.calls}</span><span className="mono">{item.input_tokens.toLocaleString()}</span><span className="mono">{item.output_tokens.toLocaleString()}</span><span>{item.priced ? <strong>${Number(item.cost_usd).toFixed(4)}</strong> : <small>no rate</small>}</span></div>)}</div> : <EmptyState title="No model has been called yet" copy="Run the insertion pipeline, then come back." />}
      </section>

      {identity?.is_admin && <BillingSection />}
    </>
  );
}

function BillingSection() {
  const invoices = useApi("/api/billing/invoices");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const extract = async () => {
    setBusy(true);
    try { setResult(await api("/api/actions/extract-billing", { method: "POST" })); await invoices.reload(); }
    finally { setBusy(false); }
  };
  return (
    <section className="panel model-cost-table">
      <SectionHeading title="Invoices (LEDES / UTBMS)" action={(invoices.data || []).length ? <button className="secondary-button small" onClick={extract} disabled={busy}><Play size={13} /> {busy ? "Extracting…" : "Extract from invoices"}</button> : <span className="table-count">{(invoices.data || []).length} invoice(s)</span>} />
      {result && <div className="form-note">{result.invoices_inserted} invoice(s), {result.line_items_inserted} line item(s) · {result.duplicates_skipped} duplicate(s) skipped · {result.not_invoices} not invoices.</div>}
      {(invoices.data || []).length ? <div className="data-table"><div className="table-head cost-head"><span>Invoice</span><span>Date</span><span>Matter</span><span>Lines</span><span>Total</span></div>{invoices.data.map((inv) => <div className="table-row cost-head" key={inv.id}><span className="primary-cell"><i className="model-mini-icon"><FileText size={14} /></i><span><strong>{inv.invoice_number}</strong><small>{inv.currency || ""}</small></span></span><span>{inv.invoice_date ? new Date(inv.invoice_date).toLocaleDateString() : "—"}</span><span>{inv.matter || "—"}</span><span className="mono">{inv.line_items}</span><span><strong>{inv.invoice_total != null ? `${inv.invoice_total.toLocaleString()} ${inv.currency || ""}` : "—"}</strong></span></div>)}</div>
        : <EmptyState title="No invoices extracted" copy="Index invoice documents first, then extract." action={<button className="secondary-button" onClick={extract} disabled={busy}><ReceiptText size={14} /> {busy ? "Extracting…" : "Extract from invoices"}</button>} />}
    </section>
  );
}

/**
 * Which gateway aliases routed to the model this row is billed against.
 *
 * Every row used to read "via LiteLLM", which is true of all of them and answers nothing.
 * /api/costs already groups by the resolved model and sends the aliases that reached it,
 * because several aliases may point at one model: without the list, one line of spend
 * cannot be traced back to the assignments that caused it.
 *
 * When the gateway cannot be reached the backend cannot resolve anything and falls back to
 * naming the row `provider/alias` — the row is then an alias wearing a model's name, so it
 * is marked as one rather than repeating the alias underneath itself. That case is inferred
 * from the row's own shape, because /api/costs sends no flag saying whether it resolved;
 * an alias deliberately named after its own upstream model ("gpt-4o-mini" routing to
 * "openai/gpt-4o-mini") would be read as unresolved.
 */
function routedVia(item) {
  const aliases = item.aliases || [];
  if (aliases.some((alias) => String(item.model).endsWith(`/${alias}`))) return "gateway alias — model unresolved";
  return aliases.length ? `via ${aliases.join(", ")}` : "via LiteLLM";
}
function compact(value) { return Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value || 0); }
function csvField(value) { const text = String(value ?? ""); return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text; }
function human(value) { return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
