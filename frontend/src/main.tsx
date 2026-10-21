import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { BuyerChat } from './BuyerChat';
import { ArrowRight } from './icons';
import { AnalyticsPanel, AnalyticsResults, ConversationPanel, KnowledgePanel } from './panels';

type RecordData = Record<string, any>;
function App() {
  const [email, setEmail] = useState('ops@demo.local');
  const [password, setPassword] = useState('Demo123!');
  const [token, setToken] = useState('');
  const [role, setRole] = useState('');
  const [orders, setOrders] = useState<RecordData[]>([]);
  const [itemId, setItemId] = useState('');
  const [conversation, setConversation] = useState('');
  const [message, setMessage] = useState('500 元以内的吸顶灯');
  const [answer, setAnswer] = useState<RecordData | null>(null);
  const [caseInfo, setCaseInfo] = useState<RecordData | null>(null);
  const [caseId, setCaseId] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<string[]>([]);
  const [streaming, setStreaming] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [targetSize, setTargetSize] = useState('');
  const [evidenceKind, setEvidenceKind] = useState<'refund_evidence' | 'product_image'>('refund_evidence');
  const [evidence, setEvidence] = useState<RecordData | null>(null);
  const [manualField, setManualField] = useState('amount_minor');
  const [manualValue, setManualValue] = useState('');
  const [productDetail, setProductDetail] = useState<RecordData | null>(null);
  const [cases, setCases] = useState<RecordData[]>([]);
  const [caseType, setCaseType] = useState('refund');
  const [supplement, setSupplement] = useState('');
  const [cancelReason, setCancelReason] = useState('');
  async function api(path: string, body?: unknown, headers: Record<string,string> = {}) {
    const response = await fetch('/v1'+path, {method: body === undefined ? 'GET':'POST', headers: {'Content-Type':'application/json', ...(token ? {'Authorization':'Bearer '+token}:{}), ...headers}, ...(body === undefined ? {}:{body:JSON.stringify(body)})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || '请求失败');
    return payload.data;
  }
  async function run(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); } catch(e) {setError(e instanceof Error ? e.message : '请求失败');} finally {setBusy(false);}
  }
  async function login() {
    const data = await api('/auth/login', {email,password});
    setToken(data.access_token); setRole(data.role); setOrders([]); setAnswer(null); setConversation(''); setCaseInfo(null); setCaseId(''); setItemId(''); setEvidence(null); setCases([]); setProductDetail(null); setStreaming(''); setEvents([]); setConfirmed(false); setTargetSize('');
  }
  async function chat() {
    const id = conversation || (await api('/conversations',{})).id;
    setConversation(id); setAnswer(null); setEvents([]); setStreaming('');
    const response = await fetch('/v1/chat/stream', {method:'POST', headers:{'Content-Type':'application/json','Authorization':'Bearer '+token}, body: JSON.stringify({conversation_id:id, client_message_id:crypto.randomUUID(), message, inherit_context:true, ...(itemId ? {order_item_id:itemId}:{}), ...(targetSize ? {target_size:targetSize}:{}), ...(evidence?.status==='extracted' && evidence.kind==='product_image' ? {image_id:evidence.id}:{})})});
    if (!response.ok) throw new Error((await response.json()).message);
    const reader = response.body!.getReader(); const decoder = new TextDecoder(); let pending = '';
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      pending += decoder.decode(value, {stream:true});
      let end;
      while ((end = pending.indexOf('\n\n')) !== -1) {
        const block = pending.slice(0,end); pending = pending.slice(end+2);
        const event = block.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim();
        const raw = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
        if (!raw) continue;
        const data = JSON.parse(raw);
        if (event === 'error') throw new Error(data.message);
        if (event === 'answer.delta') { setStreaming(s => s + (data.text || '')); continue; }
        if (event === 'answer.completed') { setAnswer(data); setStreaming(''); }
        else setEvents(old => [...old, event === 'workflow.progress' ? `${data.agent} → ${data.tool} (${data.duration_ms}ms)` : String(event)]);
      }
    }
  }
  async function uploadImage(file: File) {
    if (file.size > 10 * 1024 * 1024 || !/\.(jpe?g|png|webp)$/i.test(file.name)) throw new Error('请选择不超过 10MB 的 jpg、png 或 webp 图片');
    setEvidence(null);
    const ticket = await api('/uploads/presign', {file_name: file.name, kind: evidenceKind});
    const put = await fetch(ticket.upload_url, {method:'PUT', body: file, headers: {'Content-Type': file.type || 'application/octet-stream'}});
    if (!put.ok) throw new Error('对象存储上传失败');
    await api(`/uploads/${ticket.image_id}/complete`, {});
    setEvidence({id: ticket.image_id, status: 'extracting', kind:evidenceKind, extracted: null});
    for (let i = 0; i < 60; i++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const record = await api(`/uploads/${ticket.image_id}`);
      setEvidence(record);
      if (record.status === 'extracted') return;
      if (record.status === 'failed') throw new Error('图片识别失败，请检查 Provider 配置或重新上传');
    }
    throw new Error('识别仍在进行，请稍后重新查询');
  }
  async function refreshEvidence(id: string) { setEvidence(await api(`/uploads/${id}`)); }
  async function verifyEvidence() {
    if (!evidence) return;
    await api(`/uploads/${evidence.id}/verify`, itemId ? {order_item_id: itemId} : {});
    await refreshEvidence(evidence.id);
  }
  async function submitManualField() {
    if (!evidence) return;
    const numeric = Number(manualValue);
    const value = manualField === 'amount_minor' && Number.isInteger(numeric) ? numeric : manualValue;
    await api(`/uploads/${evidence.id}/fields`, {field_name: manualField, value, note: '工作台人工补录'});
    await refreshEvidence(evidence.id);
  }
  if (!token) return <div className="auth-screen"><div className="auth-story"><span className="auth-wordmark">ECOM / ASSISTANT</span><div><span className="welcome-kicker">让每一次购物更简单</span><h1>你的需求，<br/>都值得被认真回应。</h1><p>从挑选好物到售后服务，<br/>在一段对话中，找到答案。</p><div className="auth-tags"><span>智能导购</span><span>订单咨询</span><span>自动售后</span></div></div><small>智能导购 · 订单咨询 · 自动售后</small></div><div className="auth-form-wrap"><form className="auth-form" onSubmit={e=>{e.preventDefault();run(login);}}><div className="welcome-mark">✳</div><h2>欢迎使用智能助手</h2><p className="muted">登录账号，开始你的第一段对话。</p><label>登录角色<select disabled={busy} value={email.split('@')[0]} onChange={e=>{setEmail(e.target.value+'@demo.local');setPassword('Demo123!');}}><option value="buyer">买家 · 对话与购物</option><option value="ops">运营 · 企业统一对话</option><option value="knowledge_admin">知识管理员 · 政策管理</option><option value="staff">客服 · 历史业务查询</option></select></label><label>账号<input autoComplete="username" value={email} onChange={e=>setEmail(e.target.value)} required/></label><label>密码<input type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required/></label>{error&&<p role="alert" className="error">{error}</p>}<button type="submit" disabled={busy}>{busy?'正在登录…':<>进入工作台 <ArrowRight size={14}/></>}</button><small>账号与角色由服务端统一校验</small></form></div></div>;
  if (token && ['buyer','ops'].includes(role)) return <BuyerChat key={token} token={token} role={role} api={api} onSignOut={()=>{setToken('');setRole('');}}/>;
  return <main>
    <header><div><span className="eyebrow">ECOM AGENT</span><h1>客服与运营工作台</h1><p>导购、售后与数据查询，在一个工作台中协作。</p></div><span className="badge">AI 工作流 · 全程可追溯</span></header>
    <section className="login"><label>账号<input value={email} onChange={e=>setEmail(e.target.value)}/></label><label>密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)}/></label><button disabled={busy} onClick={()=>run(login)}>登录 / 切换账号</button><span>{role ? `当前角色：${role}`:'尚未登录'}</span></section>
    <p className="hint">可登录账号：buyer / staff / ops / knowledge_admin @demo.local；密码 Demo123!。角色由服务端验证。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {token && role!=='knowledge_admin' && <div className="grid">
      <section><h2>{role==='ops'?'运营分析':'智能助手'}</h2>
        {(role==='buyer'||role==='staff') && <><button disabled={busy} onClick={()=>run(async()=>{setOrders((await api('/orders')).items);})}>加载授权订单</button><select value={itemId} onChange={e=>setItemId(e.target.value)}><option value="">选择订单商品</option>{orders.flatMap(o=>o.items.map((i:RecordData)=><option key={i.id} value={i.id}>{o.order_no} · {i.title} · ¥{(i.paid_amount_minor/100).toFixed(2)}</option>))}</select></>}
        {role==='buyer' && <label>目标尺寸<input value={targetSize} onChange={e=>setTargetSize(e.target.value)}/></label>}
        <label>你的问题<textarea rows={4} value={message} onChange={e=>setMessage(e.target.value)}/></label>
        <button disabled={busy || !['buyer','ops'].includes(role)} onClick={()=>run(chat)}>{busy?'处理中…':'发送请求'}</button>
        {streaming && !answer && <article className="streaming"><p>{streaming}</p></article>}
        {answer && <article><p>{answer.reply || answer.message}</p>{answer.cards?.map((card:RecordData)=><div className="card" key={card.sku_id}><strong>{card.title}</strong><span>¥{(card.price_minor/100).toFixed(2)} · 库存 {card.available_qty}</span><small>{card.sku_id} · {card.observed_at}</small><button disabled={busy} onClick={()=>run(async()=>setProductDetail(await api(`/products/${encodeURIComponent(card.product_id)}`)))}>查看商品详情</button></div>)}{answer.evidence?.map((e:RecordData)=><blockquote key={e.chunk_id}>{e.content}<small>{e.doc_title} · {e.version} · {e.section_title}</small></blockquote>)}{answer.data && <AnalyticsResults data={answer.data}/>}</article>}
        {productDetail && <article className="card"><h3>{productDetail.title}</h3><p>{productDetail.description}</p><pre>{JSON.stringify(productDetail.attributes,null,2)}</pre><p>评价样本：{productDetail.reviews.sample_count} · 平均评分 {productDetail.reviews.average ?? '—'}</p>{productDetail.reviews.samples.map((text:string,i:number)=><blockquote key={i}>{text}</blockquote>)}<small>{productDetail.reviews.note}</small><button onClick={()=>setProductDetail(null)}>收起详情</button></article>}
        {role==='buyer' && <article className="card evidence-fields"><h3>图片凭证识别与核对</h3>
          <p className="hint">识别不判断凭证真伪与责任；金额与订单号只做精确比对，OCR 误读请人工补录后重新核对。商品图片可作为识图问题随消息发送。</p>
          <label>凭证类型<select value={evidenceKind} onChange={e=>{setEvidenceKind(e.target.value as 'refund_evidence' | 'product_image');setEvidence(null);}}><option value="refund_evidence">退款凭证</option><option value="product_image">商品图片</option></select></label>
          <label>选择图片（jpg / png / webp，≤ 10MB）<input type="file" accept=".jpg,.jpeg,.png,.webp" disabled={busy} onChange={e=>{const file=e.target.files?.[0]; if (file) run(()=>uploadImage(file)); e.target.value='';}}/></label>
          {evidence && <>
            <p>识别状态：<strong>{evidence.status}</strong><small>{evidence.id}</small></p>
            {evidence.error && <p className="warn">{evidence.error}</p>}
            {evidence.status==='failed' && evidence.job_id && <button disabled={busy} onClick={()=>run(async()=>{await api(`/jobs/${evidence.job_id}/retry`,{});await refreshEvidence(evidence.id);})}>重试识别任务</button>}
            {evidence.extracted?.fields?.map((f:RecordData)=><p key={f.field_name}>{f.field_name}：<strong>{f.value === null || f.value === undefined ? '—' : String(f.value)}</strong> · {f.status} · 来源 {f.source || 'ocr'}{f.manual ? `（原值 ${f.manual.previous_value ?? '—'}，由 ${f.manual.by} 补录）` : ''}</p>)}
            {evidence.extracted?.mapping && Object.keys(evidence.extracted.mapping).length > 0 && <p>词表映射：<small>{JSON.stringify(evidence.extracted.mapping)}</small></p>}
            {evidence.extracted?.verification && <p>核对结论：<strong className={evidence.extracted.verification.status === 'matched' ? 'ok' : evidence.extracted.verification.status === 'mismatched' ? 'bad' : 'warn'}>{evidence.extracted.verification.status}</strong> · {evidence.extracted.verification.note}{evidence.extracted.verification.stale && <span className="warn">（已补录，需重新核对）</span>}</p>}
            {evidence.extracted?.verification?.checks?.map((c:RecordData)=><p key={c.field_name}><small>{c.field_name}：OCR {String(c.ocr_value ?? '—')} vs 订单 {String(c.expected ?? '—')} → {c.verification}（{c.reason}）</small></p>)}
            <button disabled={busy} onClick={()=>run(()=>refreshEvidence(evidence.id))}>刷新识别状态</button>
            <button disabled={busy || evidence.kind !== 'refund_evidence' || evidence.status !== 'extracted' || !itemId} onClick={()=>run(verifyEvidence)}>与所选订单精确核对</button>
            <label>补录字段<select value={manualField} onChange={e=>setManualField(e.target.value)}><option value="order_no">订单号</option><option value="amount_minor">金额（整数分）</option><option value="date">日期</option><option value="damage_description">破损说明</option></select></label>
            <label>补录值<input value={manualValue} onChange={e=>setManualValue(e.target.value)} placeholder={manualField === 'amount_minor' ? '如 12800' : '人工确认后的值'}/></label>
            <button disabled={busy || evidence.status !== 'extracted' || !manualValue} onClick={()=>run(submitManualField)}>提交人工补录</button>
          </>}
        </article>}
        <details><summary>工具轨迹</summary>{events.map((e,i)=><p key={i}>{e}</p>)}</details>
      </section>
      {['buyer','staff'].includes(role) && <section><h2>售后自动处理</h2><p className="hint">买家确认后系统按已发布规则自动审核，材料不足时提示补充。CREATED 表示平台工单已创建。</p>
        {role==='buyer' && <><label>申请类型<select value={caseType} onChange={e=>setCaseType(e.target.value)}><option value="exchange">换货</option><option value="refund">退款</option><option value="return_refund">退货退款</option></select></label><button disabled={busy||!itemId||!message.trim()} onClick={()=>run(async()=>{const data=await api('/after-sales/drafts',{order_item_id:itemId,case_type:caseType,reason:message,evidence_ids:evidence?.kind==='refund_evidence' && evidence.status==='extracted'?[evidence.id]:[]});setCaseInfo(data);setCaseId(data.id);setConfirmed(false);})}>生成售后草稿</button><label className="check"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>我已核对并确认提交</label><button disabled={busy||!confirmed||caseInfo?.status!=='DRAFT'} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseId}/submit`,{expected_version:caseInfo!.version,confirmed:true},{'Idempotency-Key':caseId+'-submit'})))}>确认提交</button></>}
        {['buyer','staff'].includes(role) && <><button disabled={busy} onClick={()=>run(async()=>setCases((await api('/after-sales')).items))}>加载售后列表</button>
        <select value={caseId} disabled={busy} onChange={e=>{const id=e.target.value;setCaseId(id);setCaseInfo(null);setConfirmed(false);if(id)run(async()=>setCaseInfo(await api('/after-sales/'+id)));}}><option value="">选择售后申请</option>{caseId&&!cases.some(c=>c.id===caseId)&&<option value={caseId}>{caseId}</option>}{cases.map(c=><option key={c.id} value={c.id}>{c.case_no} · {c.status}</option>)}</select></>}
        <label>售后单 ID<input value={caseId} onChange={e=>{setCaseId(e.target.value);setCaseInfo(null);}}/></label><button disabled={busy||!caseId} onClick={()=>run(async()=>setCaseInfo(await api('/after-sales/'+caseId)))}>查询状态</button>
        {role==='buyer' && caseInfo?.status==='NEED_MORE_INFO' && <><label>补充说明<textarea value={supplement} onChange={e=>setSupplement(e.target.value)}/></label><p className="hint">已识别的退款凭证将随补料提交；文字说明也可单独提交。</p><button disabled={busy||!supplement.trim()} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseId}/supplement`,{expected_version:caseInfo.version,comment:supplement,evidence_ids:evidence?.kind==='refund_evidence'&&evidence.status==='extracted'?[evidence.id]:[]})))}>提交补料并自动重新审核</button></>}
        {role==='buyer' && ['DRAFT','SUBMITTED','REVIEWING','NEED_MORE_INFO'].includes(caseInfo?.status) && <><label>取消原因<input value={cancelReason} onChange={e=>setCancelReason(e.target.value)}/></label><button disabled={busy||!cancelReason.trim()} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseId}/cancel`,{expected_version:caseInfo!.version,reason:cancelReason})))}>取消申请</button></>}

        {caseInfo && <pre>{JSON.stringify(caseInfo,null,2)}</pre>}
      </section>}
    </div>}
    {token && <div className="grid">
      {role==='ops' && <AnalyticsPanel api={api} run={run} busy={busy}/>}
      {role==='knowledge_admin' && <KnowledgePanel key={token} api={api} run={run} busy={busy}/>}
      {['buyer','staff','ops'].includes(role) && <ConversationPanel key={token} api={api} run={run} busy={busy} role={role} conversation={conversation} itemId={itemId} onSelect={id=>{setConversation(id);setAnswer(null);setEvents([]);}}/>}
    </div>}
    <footer>图片识别、模型分析需要配置 Provider；工作流支持 LangGraph 模型工具编排与无 Key 规则模式。</footer>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
