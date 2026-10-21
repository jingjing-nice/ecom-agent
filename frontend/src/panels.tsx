import React, { useState } from 'react';

type Data = Record<string, any>;
type Api = (path: string, body?: unknown, headers?: Record<string, string>) => Promise<any>;
type Run = (action: () => Promise<void>) => Promise<void>;
type Props = {api: Api; run: Run; busy: boolean};

export function AnalyticsResults({data}: {data: Data}) {
  const columns: string[] = data.columns || [];
  const rows: Data[] = data.rows || [];
  const [label, setLabel] = useState('');
  const [metric, setMetric] = useState('');
  // Column names and units come from the result; never infer a monetary conversion.
  const numeric = columns.filter(c => rows.some(r => r[c] !== null) && rows.every(r => r[c] == null ||
    ((typeof r[c] === 'number' || typeof r[c] === 'string') && String(r[c]).trim() !== '' && Number.isFinite(Number(r[c])))));
  const labels = columns.filter(c => !numeric.includes(c));
  const labelColumn = labels.includes(label) ? label : labels[0];
  const valueColumn = numeric.includes(metric) ? metric : numeric[0];
  const plotted = rows.slice(0, 20);
  const max = Math.max(1, ...plotted.map(r => Math.abs(Number(r[valueColumn]) || 0)));
  return <article className="analytics-results">
    <p>{data.explanation}</p><small>查询编号：{data.query_id || '—'} · {data.duration_ms ?? '—'} ms · {data.row_count ?? rows.length} 行</small>
    <small>数据同步时点：{data.data_as_of || '未提供'}{data.data_freshness ? ` · ${data.data_freshness}` : ''}</small>
    {data.assumptions?.length > 0 && <p>查询假设：{data.assumptions.join('；')}</p>}
    <p className="hint">金额字段保留查询返回单位；字段名含 minor 时单位为分。空值显示为“—”。</p>
    {columns.length > 0 && <div className="table-scroll"><table><thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{columns.map(c => <td key={c}>{r[c] == null ? '—' : String(r[c])}</td>)}</tr>)}</tbody></table></div>}
    {!rows.length && <p>该范围内没有数据。</p>}
    {data.truncated && <p className="warn">结果达到行数上限，请缩小范围。图表仅基于已返回结果。</p>}
    {labelColumn && valueColumn && <div className="chart"><h3>结果对比（前 20 行）</h3>
      <label>分组<select value={labelColumn} onChange={e => setLabel(e.target.value)}>{labels.map(c => <option key={c}>{c}</option>)}</select></label>
      <label>数值<select value={valueColumn} onChange={e => setMetric(e.target.value)}>{numeric.map(c => <option key={c}>{c}</option>)}</select></label>
      {plotted.map((r, i) => <div className="bar-row" key={i}><span>{String(r[labelColumn] ?? '—')}</span><div className="bar-track"><div className="bar" style={{width: `${Math.abs(Number(r[valueColumn]) || 0) / max * 100}%`}}/></div><strong>{String(r[valueColumn] ?? '—')}</strong></div>)}
    </div>}
  </article>;
}

export function AnalyticsPanel({api, run, busy}: Props) {
  const [question, setQuestion] = useState('2026 年 10 月各品类支付件数，包含零销量品类');
  const [metrics, setMetrics] = useState<Data[]>([]);
  const [result, setResult] = useState<Data | null>(null);
  return <section><h2>运营查询与指标口径</h2>
    <button disabled={busy} onClick={() => run(async () => setMetrics(await api('/analytics/metrics')))}>查看 10 项指标定义</button>
    {metrics.length > 0 && <details><summary>指标字典</summary>{metrics.map(m => <p key={m.metric_id}><strong>{m.name}（{m.version}）</strong>：{m.definition}<small>时间字段：{m.time_field}；维度：{m.allowed_dimensions.join('、')}</small></p>)}</details>}
    <label>分析问题<textarea value={question} onChange={e => setQuestion(e.target.value)} /></label>
    <button disabled={busy || !question.trim()} onClick={() => run(async () => {setResult(null); setResult(await api('/analytics/query', {question}));})}>执行受控查询</button>
    {result && <AnalyticsResults data={result}/>}
  </section>;
}

export function KnowledgePanel({api, run, busy}: Props) {
  const [versions, setVersions] = useState<Data[]>([]);
  const [preview, setPreview] = useState<Data | null>(null);
  const [docId, setDocId] = useState('exchange-policy');
  const [title, setTitle] = useState('换货政策');
  const [version, setVersion] = useState('v2');
  const [scope, setScope] = useState('after_sale');
  const [content, setContent] = useState('');
  const [source, setSource] = useState('企业内部政策');
  const [job, setJob] = useState<Data | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [automaticRules, setAutomaticRules] = useState('');
  async function refresh() {setVersions((await api('/knowledge/versions')).items);}
  async function view(id: string) {setPreview(await api(`/knowledge/${id}`)); setReviewed(false);}
  async function transition(action: string) {
    await api(`/knowledge/${preview!.id}/${action}`, {});
    await view(preview!.id); await refresh();
  }
  return <section><h2>知识版本管理</h2><p className="hint">导入后由聊天模型进行语义切片，再构建索引。请先刷新任务与切片，核对原文后审核发布；缺少切片模型时任务失败，不会偷偷改用规则切片。无 Embedding 配置时索引降级为关键词。</p>
    <label>文档标识<input value={docId} onChange={e => setDocId(e.target.value)}/></label>
    <label>标题<input value={title} onChange={e => setTitle(e.target.value)}/></label>
    <label>版本<input value={version} onChange={e => setVersion(e.target.value)}/></label>
    <label>适用范围<select value={scope} onChange={e => setScope(e.target.value)}>{['after_sale','payment','logistics','membership'].map(s => <option key={s}>{s}</option>)}</select></label>
    <label>来源<input value={source} onChange={e => setSource(e.target.value)}/></label>
    {scope==='after_sale' && <label>自动售后规则 JSON（留空则不开放自动执行）<textarea value={automaticRules} onChange={e=>setAutomaticRules(e.target.value)} placeholder={'{"rules":[{"case_type":"refund","order_statuses":["paid"],"max_days_since_payment":7,"max_amount_minor":50000,"require_evidence":true}]}'}/></label>}
    <label>政策正文<textarea rows={8} value={content} onChange={e => setContent(e.target.value)}/></label>
    <label>或读取 UTF-8 文本文件<input type="file" accept=".txt,.md" disabled={busy} onChange={e => {const f=e.target.files?.[0]; if(f) run(async () => {if(f.size>800000) throw new Error('文本文件过大'); setContent(await f.text());}); e.target.value='';}}/></label>
    <button disabled={busy || !content.trim() || !docId.trim() || !title.trim() || !version.trim()} onClick={() => run(async () => {
      const imported = await api('/knowledge/documents', {doc_id:docId, title, scope, version, source, content, ...(scope==='after_sale' && automaticRules.trim() ? {automation_rules:JSON.parse(automaticRules)} : {})});
      setJob({job_id: imported.job_id, status:'pending'}); await view(imported.version_id); await refresh();
    })}>导入并启动模型切片</button>
    <button disabled={busy} onClick={() => run(refresh)}>刷新版本列表</button>
    {job && <p>切片与索引任务：{job.status} {job.error}<button disabled={busy} onClick={() => run(async () => {setJob(await api(`/jobs/${job.job_id}`)); if(preview) await view(preview.id);})}>刷新任务及索引</button></p>}
    {job?.status==='failed' && <button disabled={busy} onClick={() => run(async () => {setJob(await api(`/jobs/${job.job_id}/retry`, {}));})}>重试索引任务</button>}
    {versions.map(v => <button key={v.id} disabled={busy} onClick={() => run(() => view(v.id))}>{v.title} · {v.version} · {v.status}</button>)}
    {preview && <article><h3>{preview.version} · {preview.status}</h3><small>流水线：{preview.pipeline_status || '—'} · 切片模型：{preview.chunking?.chunk_model || '待处理'} · 算法版本：{preview.chunking?.splitter_version || '—'}</small><p>索引：{preview.index_version || '构建中'}；发布检查：{preview.publish_blocker || '通过'}</p>
      {preview.automation_rules && <pre>{JSON.stringify(preview.automation_rules,null,2)}</pre>}
      {preview.chunks.map((c:Data) => <blockquote key={c.id}>{c.content}<small>原文位置 {c.char_start}–{c.char_end} · {c.model_version || '尚未索引'}</small></blockquote>)}
      <label className="check"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)}/>我已核对政策原文与适用范围</label>
      <button disabled={busy || preview.status!=='draft' || !reviewed || !preview.chunks.length} onClick={() => run(() => transition('review'))}>确认并提交审核</button>
      <button disabled={busy || !!preview.publish_blocker} onClick={() => run(() => transition('publish'))}>发布</button>
      <button disabled={busy || preview.status!=='published'} onClick={() => run(() => transition('revoke'))}>撤销发布</button>
    </article>}
  </section>;
}

export function ConversationPanel({api, run, busy, role, conversation, onSelect, itemId}: Props & {
  role:string; conversation:string; onSelect:(id:string)=>void; itemId:string;
}) {
  const [list, setList] = useState<Data[]>([]);
  const [history, setHistory] = useState<Data[]>([]);
  const [status, setStatus] = useState('');
  const [before, setBefore] = useState<number | null>(null);
  async function load(id: string, older?: number) {
    const data = await api(`/conversations/${id}/messages${older ? `?before=${older}` : ''}`);
    setHistory(old => older ? [...data.items, ...old] : data.items);
    setBefore(data.next_before); setStatus(data.takeover_status);
  }
  return <section><h2>会话记录</h2>
    <button disabled={busy} onClick={() => run(async () => setList((await api('/conversations')).items))}>加载会话</button>
    {role!=='staff' && <button disabled={busy} onClick={() => {onSelect(''); setHistory([]); setStatus('');}}>新建对话</button>}
    <select value={conversation} disabled={busy} onChange={e => {const id=e.target.value; onSelect(id); setHistory([]); setBefore(null); setStatus(''); if(id) run(() => load(id));}}>
      <option value="">选择会话</option>{conversation && !list.some(c=>c.id===conversation) && <option value={conversation}>{conversation}</option>}
      {list.map(c => <option value={c.id} key={c.id}>{c.title || c.id} · {c.takeover_status}</option>)}
    </select>
    {conversation && <><p>会话：{conversation}<small>接管状态：{status || '请刷新'}</small></p>
      <button disabled={busy} onClick={() => run(() => load(conversation))}>刷新消息</button>

      {before && <button disabled={busy} onClick={() => run(() => load(conversation,before))}>加载更早消息</button>}
      {history.map(m => <article className="card" key={m.id}><strong>{m.role==='staff'?'人工客服':m.role==='user'?'用户':'助手'}</strong><p>{m.content}</p><small>{m.created_at}</small>
        {m.role==='assistant' && <details><summary>业务依据与工具记录</summary><pre>{JSON.stringify({cards:m.meta?.cards, evidence:m.meta?.evidence, trace:m.meta?.trace},null,2)}</pre></details>}
      </article>)}
    </>}
  </section>;
}
